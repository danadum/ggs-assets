#!/usr/bin/env python3
"""Generate empire-rankings events.json from Goodgame's published files (dll + items + translations)."""
import argparse
import json
import re
import sys
import urllib.request

HTML5 = "https://empire-html5.goodgamestudios.com/default/"
TRANSLATIONS = "https://translations-api-test.public.ggs-ep.com/12/en"
E4K_DATA = "https://raw.githubusercontent.com/476f6f6467616d6553747564696f73/E4K-data/main/data/"
UA = {"User-Agent": "events-json-generator"}

# ---- hand-kept table (each line explained in diff.md / final report) ----
# 1. Permanent lists that belong to no event: LT constant -> (side, title key, extra keys).
GENERAL = [
    ("PLAYER_ACHIEVEMENT_POINTS", "player", "dialog_playerInfo_infoAchievements", {}),
    ("PLAYER_WEEKLY_LOOT", "player", "dialog_BeggingKnights_nobilityPoints", {}),
    ("PLAYER_HONOR", "player", "honorPoints", {}),
    ("PLAYER_MIGHT_POINTS", "player", "playerMight", {}),
    ("PLAYER_LEGEND", "player", "legendLevel", {"leagues": [6]}),
    ("ALLIANCE_HONOR", "alliance", "allianceHonor", {}),
    ("ALLIANCE_MIGHT_POINTS", "alliance", "allianceMight", {}),
    ("ALLIANCE_LANDMARKS", "alliance", "dominionPoints", {}),
    ("ALLIANCE_AQUA_POINTS", "alliance", "cargo_points", {"offset": 1}),
]
GENERAL_PAIRS = [("honorPoints", "allianceHonor"), ("playerMight", "allianceMight")]
# 2. LT <-> event links whose constant names do not follow the naming convention.
LINK = {
    "SAMURAI_PLAYER": "EVENTTYPE_SAMURAI_INVASION",
    "SAMURAI_ALLIANCE": "EVENTTYPE_SAMURAI_INVASION",
    "LUCKY_WHEEL_SALE_DAYS": "EVENTTYPE_LUCKYWHEEL_SD",
}
# 3. Lists never published: not readable with hgh/llsp, or no distinct title.
EXCLUDE = {
    "COLOSSUS": "read with che (C2SColossusGetHighscoreVO), not hgh",
    "KINGDOMS_LEAGUE_SEASON_EVENT": "season round rank, same title as the season list",
    "ALLIANCE_KINGDOMS_LEAGUE_SEASON_EVENT": "season round rank, same title as the season list",
}
# 4. Facts the files do not carry.
NOPOINTS_EVENTS = {"EVENTTYPE_FACTION_INVASION"}
DIVISION_LABELS = {
    "EVENTTYPE_ALLIANCE_MOBILISATION": "popup_ame_rewards_division_name_{}",
    "EVENTTYPE_ALLIANCE_RAID": "popup_arme_rewards_division_name_{}",
}
# 5. Entries copied as-is (lists of the outer-realms server and an LT missing from HighscoreConst).
LEGACY = {
    "player": {
        "dialog_tempServer_scoring_header_might": {"id": 61, "categories": [{"id": 1, "name": "dialog_ci_filter01_all"}]},
        "temp_server_name": {"categories": [
            {"eventid": 76, "id": 1, "name": "dialog_combinedReward_eventOver"},
            {"eventid": 62, "id": 1, "name": "currentEvent", "placeholder": ":", "value": "", "isCurrentOuter": True}]},
        "event_title_97": {"id": 60, "categories": [
            {"id": 3, "name": "level_placeholder", "value": "55-69"},
            {"id": 4, "name": "legendaryLevel_placeholder", "value": "1-950"}]},
    },
    "alliance": {},
}
LEGACY_LTS = {61, 62, 76, 60}
# 6. Title keys already published, kept so links, saved sessions and third-party label maps stay valid.
TITLE = {
    "LONG_TERM_POINT_EVENT": "dialog_longPointsEvent_seasonalPoints",
    "ALLIANCE_RED_ALIEN_INVASION_PLAYER": "dialog_redAlienInvasion_message_header",
    "ALLIANCE_RED_ALIEN_INVASION_ALLIANCE": "dialog_redAlienInvasion_message_header",
    "DONATION_EVENT": "eventBuilding_DonationEvent",
}


class Fatal(Exception):
    pass


def fetch(src, binary=False):
    if re.match(r"https?://", src):
        req = urllib.request.Request(src, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    else:
        with open(src, "rb") as f:
            data = f.read()
    return data if binary else data.decode("utf-8")


def latest_gge_urls():
    index = fetch(HTML5 + "index.html")
    m = re.findall(r"dll/ggs\.dll\.([0-9a-f]+)\.js", index)
    if len(set(m)) != 1:
        raise Fatal("index.html: expected one dll/ggs.dll.<hash>.js, found %r" % m)
    props = fetch(HTML5 + "items/ItemsVersion.properties")
    v = re.search(r"CastleItemXMLVersion=([\w.]+)", props)
    if not v:
        raise Fatal("ItemsVersion.properties: no CastleItemXMLVersion in %r" % props[:200])
    return HTML5 + "dll/ggs.dll.%s.js" % m[0], HTML5 + "items/items_v%s.json" % v.group(1)


def class_block(js, cls):
    """Return (alias, text) of the transpiled static class `cls` in the dll."""
    m = re.search(r"([\w$]+)\.__class\s*=\s*\"%s\"" % cls, js)
    if not m:
        raise Fatal("dll: class %s not found" % cls)
    start = js.rfind("function %s(" % cls, 0, m.start())
    if start < 0:
        raise Fatal("dll: constructor of %s not found" % cls)
    return m.group(1), js[start:m.end()]


def int_consts(alias, block):
    out = []
    for name, val in re.findall(r"(?<![\w$])%s\.([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)\s*[,;)]" % re.escape(alias), block):
        out.append((name, int(val)))
    return out


def name_list(block, prop):
    m = re.search(r"HighscoreConst\.%s\s*=\s*\[([^\]]*)\]" % prop, block)
    if not m:
        raise Fatal("dll: HighscoreConst.%s not found" % prop)
    return re.findall(r"HighscoreConst\.([A-Z0-9_]+)", m.group(1))


NOT_LT = re.compile(r"ENTRIES|_TOP$|OFFSET|^POINTS_|INITIAL|^INVALID|^NO_|^RANK_ONE$|^MY_RANK$|SIZE|COOLDOWN")


def parse_dll(js):
    hs_alias, hs_block = class_block(js, "HighscoreConst")
    ec_alias, ec_block = class_block(js, "EventConst")
    lts, aliases = {}, {}
    for name, val in int_consts(hs_alias, hs_block):
        if val <= 0 or NOT_LT.search(name):
            continue
        if val in lts:
            aliases[name] = val
            continue
        lts[val] = name
    events = {n: v for n, v in int_consts(ec_alias, ec_block) if n.startswith("EVENTTYPE_")}
    subtypes = {n: v for n, v in int_consts(ec_alias, ec_block) if n.startswith("LEAGUETYPE_EVENT_SUBTYPE_")}
    by_name = {n: v for v, n in lts.items()}
    by_name.update(aliases)
    anchors = {"PLAYER_MIGHT_POINTS": 6, "ALLIANCE_AQUA_POINTS": 13, "LONG_TERM_POINT_EVENT": 53,
               "FACTION_INVASION_PLAYER_BLUE": 54, "DECO_GACHA_EVENT": 80}
    for n, v in anchors.items():
        if by_name.get(n) != v:
            raise Fatal("dll: HighscoreConst.%s = %r, expected %d (parser broken?)" % (n, by_name.get(n), v))
    for n, v in {"EVENTTYPE_LONGTERM_POINT_EVENT": 83, "EVENTTYPE_DECO_GACHA": 126, "EVENTTYPE_FACTION": 3}.items():
        if events.get(n) != v:
            raise Fatal("dll: EventConst.%s = %r, expected %d (parser broken?)" % (n, events.get(n), v))
    if len(lts) < 45 or len(events) < 100:
        raise Fatal("dll: only %d list types / %d event types parsed" % (len(lts), len(events)))
    return {
        "lts": lts, "by_name": by_name, "events": events, "subtypes": subtypes,
        "leagued": name_list(hs_block, "LEAGUED_HIGHSCORES"),
        "prev_run_on_main": name_list(hs_block, "GLOBALSERVER_PREVIOUS_RUN_HIGHSCORES_ON_MAINSERVER"),
    }


def load_items_gge(src):
    d = json.loads(fetch(src))
    return {k: d.get(k) for k in ("events", "leaguetypes", "leaderboardRewards")} | {"version": d.get("versionInfo", {})}


def load_items_e4k(base):
    base = base if base.endswith("/") else base + "/"
    out = {k: json.loads(fetch(base + k + ".json")) for k in ("events", "leaguetypes", "leaderboardRewards")}
    out["version"] = json.loads(fetch(base + "versionInfo.json"))
    return out


def canon(name):
    toks = name.split("_")
    flag = "ALLIANCE" in toks
    return "".join(t for t in toks if t not in ("ALLIANCE", "PLAYER", "EVENT", "EVENTTYPE")), flag


def side_of(lt_name):
    toks = lt_name.split("_")
    if "PLAYER" in toks:
        return "player"
    return "alliance" if "ALLIANCE" in toks else "player"


def match_event(lt_name, events):
    if lt_name in LINK:
        return LINK[lt_name] if LINK[lt_name] in events else None
    c, flag = canon(lt_name)
    best, score = None, None
    for ev in events:
        ec, eflag = canon(ev)
        if ec and c.startswith(ec) and (flag or not eflag):
            sc = (len(ec), eflag == flag)
            if best is None or sc > score:
                best, score = ev, sc
            elif sc == score:
                raise Fatal("LT %s matches both %s and %s" % (lt_name, best, ev))
    return best


def lt_label(lo, hi, top):
    lo, hi = int(lo), int(hi)
    if lo == 70 and hi == top:
        return "level_placeholder", "70"
    if lo >= 70:
        a, b = max(lo - 70, 1), hi - 70
        return "legendaryLevel_placeholder", str(a) if a == b else "%d-%d" % (a, b)
    lo = max(lo, 1)
    return "level_placeholder", str(lo) if lo == hi else "%d-%d" % (lo, hi)


def leagues_for(items, event_id, subtype):
    rows = [r for r in items["leaguetypes"]
            if int(r["eventID"]) == event_id and int(r.get("subType", 0) or 0) == subtype]
    return sorted(((int(r["leaguetypeID"]), int(r["minLevel"]), int(r["maxLevel"])) for r in rows))


def level_categories(leagues, top):
    if len(leagues) <= 1:
        return [{"id": leagues[0][0] if leagues else 1, "name": "dialog_ci_filter01_all"}]
    cats = []
    for lid, lo, hi in leagues:
        key, val = lt_label(lo, hi, top)
        cats.append({"id": lid, "name": key, "value": val})
    return cats


def generate(dll, items, texts, game):
    warnings, notes = [], []
    lts, by_name, evconst = dll["lts"], dll["by_name"], dll["events"]
    for n in [g[0] for g in GENERAL]:
        if n not in by_name:
            raise Fatal("permanent list HighscoreConst.%s no longer exists" % n)
    for n in list(LINK) + list(EXCLUDE) + list(TITLE):
        if n not in by_name:
            warnings.append("override names HighscoreConst.%s which no longer exists (unused line)" % n)
    for n in list(LINK.values()) + list(NOPOINTS_EVENTS) + list(DIVISION_LABELS):
        if n not in evconst:
            warnings.append("override names EventConst.%s which no longer exists (unused line)" % n)
    for sect in ("events", "leaguetypes", "leaderboardRewards"):
        if not items.get(sect):
            raise Fatal("items: section %s missing or empty" % sect)
    top = max(int(r["maxLevel"]) for r in items["leaguetypes"])
    ev_types = {int(e["eventID"]): e.get("eventType") for e in items["events"]}
    global_events = {}
    for r in items["leaderboardRewards"]:
        lid = r.get("leagueID")
        global_events.setdefault(int(r["eventTypeID"]), set()).add(None if lid in (None, "") else int(lid))
    general_lts = {by_name[g[0]] for g in GENERAL}
    prev_run = {by_name[n] for n in dll["prev_run_on_main"]}
    blue = dll["subtypes"].get("LEAGUETYPE_EVENT_SUBTYPE_FACTION_INVASION_BLUE")
    red = dll["subtypes"].get("LEAGUETYPE_EVENT_SUBTYPE_FACTION_INVASION_RED")

    out = {"player": {}, "alliance": {}, "player_to_alliance": []}

    def need_key(k, where):
        if k not in texts:
            raise Fatal("translation key %r (%s) missing from the translations file" % (k, where))

    for name, side, key, extra in GENERAL:
        lt = by_name[name]
        need_key(key, name)
        if "leagues" in extra:
            leagues = [x for x in leagues_for(items, -1, 0) if x[0] in extra["leagues"]]
            cats = [{"id": l[0], "name": lt_label(l[1], l[2], top)[0], "value": lt_label(l[1], l[2], top)[1]} for l in leagues]
        elif name in dll["leagued"]:
            cats = level_categories(leagues_for(items, -1, 0), top)
        else:
            cats = [{"id": 1, "name": "dialog_ci_filter01_all"}]
        entry = {"id": lt, "categories": cats}
        if "offset" in extra:
            entry["offset"] = extra["offset"]
        out[side][key] = entry

    # LT -> event links
    links, unmapped = {}, []
    for lt, name in sorted(lts.items()):
        if lt in general_lts:
            continue
        ev = match_event(name, evconst)
        if ev is None:
            unmapped.append((lt, name))
            continue
        links[lt] = (name, ev, evconst[ev])
    by_event = {}
    for lt, (name, ev, eid) in links.items():
        by_event.setdefault(eid, []).append(lt)

    skipped = []
    entries = []  # (sort LT, side, key, entry, eid)
    for lt, (name, ev, eid) in sorted(links.items()):
        side = side_of(name)
        why = None
        if name in EXCLUDE:
            why = "excluded: " + EXCLUDE[name]
        elif lt in LEGACY_LTS:
            why = "legacy entry"
        elif eid not in ev_types:
            why = "event %d (%s) absent from items events" % (eid, ev)
        elif any(o in prev_run for o in by_event[eid]) and lt not in prev_run:
            why = "lives on the event's own server (only %s are readable on main servers)" % sorted(prev_run & set(by_event[eid]))
        if why:
            skipped.append((lt, name, ev, why))
            continue
        etype = ev_types[eid] or ""
        key = TITLE.get(name, "event_title_%d" % eid)
        if key not in texts:
            alt = "dialog_title_" + etype.lower()
            if alt in texts:
                key = alt
            else:
                warnings.append("LT %d %s: no title key (tried event_title_%d, %s) - not published" % (lt, name, eid, alt))
                skipped.append((lt, name, ev, "no title key"))
                continue
        entry = {}
        if eid in global_events:
            entry["global"] = True
            leagues = sorted(global_events[eid], key=lambda x: -1 if x is None else x)
            known = {l[0]: l for l in leagues_for(items, eid, 0)}
            if leagues == [None]:
                cats = [{"id": None, "name": "dialog_ci_filter01_all"}]
            elif ev in DIVISION_LABELS:
                cats = [{"id": l, "name": DIVISION_LABELS[ev].format(l)} for l in leagues]
            elif all(l in known for l in leagues):
                cats = level_categories([known[l] for l in leagues], top)
            else:
                raise Fatal("global LT %d %s: leagues %s have no label rule" % (lt, name, leagues))
        elif ev == "EVENTTYPE_FACTION":
            cats = []
            for r in sorted(items["leaguetypes"], key=lambda r: int(r["leaguetypeID"])):
                if int(r["eventID"]) != eid:
                    continue
                colour = "blue" if int(r.get("subType", 0) or 0) == 0 else "red"
                legend = "_legendary" if int(r["minLevel"]) >= 70 else ""
                cats.append({"id": int(r["leaguetypeID"]), "name": "dialog_factionhighscore_%sList%s" % (colour, legend)})
        elif ev == "EVENTTYPE_FACTION_INVASION" and side == "player":
            sub = blue if name.endswith("_BLUE") else red
            colour = "blue" if sub == blue else "red"
            cats = []
            for lid, lo, hi in leagues_for(items, eid, sub):
                c = {"eventid": lt, "id": lid}
                if lo >= 70:
                    c["name"] = "dialog_factionhighscore_%sList_legendary" % colour
                else:
                    c.update({"name": "dialog_factionhighscore_%sList" % colour, "placeholder": "40", "value": lo})
                cats.append(c)
        else:
            sub = 1 if side == "alliance" else 0
            cats = level_categories(leagues_for(items, eid, sub), top)
        if "KINGDOMS_LEAGUE" in name:
            entry["isLeague"] = True
            # The server ignores LID here: LID 1 and 2 return the same rows and LR (EmpireEx_3, 2026-11-27).
            cats = [{"id": 1, "name": "dialog_ci_filter01_all"}]
        if ev in NOPOINTS_EVENTS and side == "player":
            entry["nopoints"] = True
        entries.append((lt, side, key, entry, cats, eid))

    # merge several LTs of the same event and side into one entry (categories carry their LT in eventid)
    merged = {}
    for lt, side, key, entry, cats, eid in entries:
        k = (side, key)
        if k not in merged:
            merged[k] = (lt, entry, [], [])
        merged[k][2].append(lt)
        merged[k][3].extend(cats)
    ordered = sorted(merged.items(), key=lambda kv: kv[1][0])
    for (side, key), (lt, entry, lt_list, cats) in ordered:
        if len(lt_list) > 1:
            if not all("eventid" in c for c in cats):
                raise Fatal("%s %s: LTs %s share one title and no merge rule" % (side, key, lt_list))
            e = dict(entry)
            e["categories"] = cats
        else:
            e = dict(entry)
            e["id"] = lt
            e["categories"] = cats
        if key in out[side]:
            raise Fatal("duplicate %s key %s" % (side, key))
        out[side][key] = e
        for c in cats:
            need_key(c["name"], "%s LT %s" % (key, lt_list))

    for side, ents in LEGACY.items():
        for key, e in ents.items():
            need_key(key, "legacy")
            out[side][key] = e

    # stable order: general first, then by LT (entries without id sort by their first eventid)
    def lt_of(e):
        return e.get("id") if e.get("id") is not None else min(c.get("eventid", 9999) for c in e["categories"])
    for side in ("player", "alliance"):
        gen = [g[2] for g in GENERAL if g[1] == side]
        rest = sorted((k for k in out[side] if k not in gen), key=lambda k: lt_of(out[side][k]))
        out[side] = {k: out[side][k] for k in gen + rest}
        for k, e in out[side].items():
            if e.get("global"):
                out[side][k] = {"global": True, **{x: y for x, y in e.items() if x != "global"}}

    pairs = [list(p) for p in GENERAL_PAIRS]
    pairs += [[k, k] for k in out["player"] if k in out["alliance"] and k not in dict(GENERAL_PAIRS)]
    out["player_to_alliance"] = pairs

    for lt, name in unmapped:
        notes.append("LT %d %s: no event linked (not published)" % (lt, name))
    return out, warnings, notes, skipped, links


def previous_check(prev, new, items, dll, links):
    """Split LTs published before and missing now into (unexplained, explained by the game dropping them)."""
    def lts(d):
        s = set()
        for side in ("player", "alliance"):
            for e in d[side].values():
                if e.get("id") is not None:
                    s.add(e["id"])
                for c in e["categories"]:
                    if "eventid" in c:
                        s.add(c["eventid"])
        return s
    event_ids = {int(e["eventID"]) for e in items["events"]}
    unexplained, explained = [], []
    for lt in sorted(lts(prev) - lts(new)):
        if lt not in dll["lts"]:
            explained.append("LT %d: constant removed from HighscoreConst" % lt)
        elif lt in links and links[lt][2] not in event_ids:
            explained.append("LT %d: event %d removed from items events" % (lt, links[lt][2]))
        else:
            unexplained.append(lt)
    return unexplained, explained


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game", choices=("gge", "e4k"), default="gge")
    ap.add_argument("--dll", help="ggs.dll.<hash>.js path or URL (default: latest from index.html)")
    ap.add_argument("--items", help="GGE items_v*.json path or URL (default: latest)")
    ap.add_argument("--e4k-data", default=E4K_DATA, help="E4K-data data/ dir or URL (for --game e4k)")
    ap.add_argument("--texts", default=TRANSLATIONS, help="translations JSON of network 12 (path or URL)")
    ap.add_argument("--previous", help="currently published events.json (path or URL): fail if a list disappears")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        dll_src, items_src = a.dll, a.items
        if not dll_src or (a.game == "gge" and not items_src):
            d_url, i_url = latest_gge_urls()
            dll_src = dll_src or d_url
            items_src = items_src or i_url
        print("dll:", dll_src, file=sys.stderr)
        dll = parse_dll(fetch(dll_src))
        if a.game == "gge":
            print("items:", items_src, file=sys.stderr)
            items = load_items_gge(items_src)
        else:
            print("items: E4K-data", a.e4k_data, file=sys.stderr)
            items = load_items_e4k(a.e4k_data)
        texts = json.loads(fetch(a.texts))
        if len(texts) < 1000:
            raise Fatal("translations: only %d keys" % len(texts))
        out, warnings, notes, skipped, links = generate(dll, items, texts, a.game)
        if a.previous:
            gone, dropped = previous_check(json.loads(fetch(a.previous)), out, items, dll, links)
            if gone:
                raise Fatal("lists published before and missing now, game still has them: LT %s" % gone)
            warnings.extend(dropped)
    except Fatal as e:
        print("FATAL:", e, file=sys.stderr)
        return 1
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, indent=4, ensure_ascii=False) + "\n")
    print("items version:", items["version"], file=sys.stderr)
    for lt, (name, ev, eid) in sorted(links.items()):
        print("  link LT %3d %-45s -> %s (%d)" % (lt, name, ev, eid), file=sys.stderr)
    for lt, name, ev, why in skipped:
        print("  skip LT %3d %-45s %s" % (lt, name, why), file=sys.stderr)
    for n in notes:
        print("  note", n, file=sys.stderr)
    for w in warnings:
        print("WARNING", w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
