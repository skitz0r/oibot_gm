"""The Raids page's read model (design §7a, phase 4): one raid's Rules (Details, Group make-up), Priority and Readiness
— pure builders over the existing readers (Registry.raid_def / minimums / first_open / all_schedules, rc.groups_per_roster,
comp.readiness / pool_players, the confirmation boxes). A minimum is ONE answer everywhere: the make-up's Minimum and
Readiness' Minimum read Registry.minimums alone — a class the buff matrix would like is never listed as a minimum. No
sheet timing and no run time is a raid's (they are each schedule's); a schedule id travels only as an href."""
from __future__ import annotations

from . import comp
from . import consequences as cq
from . import raidcycle as rc
from .constants import LEVEL_WORDS, PAGE_WORDS, ROLES
from .registry import Registry, min_entry, row_lineup, schedule_name


def _fact(label: str, value: str, href: str | None = None, **extra) -> dict:
    return {"label": label, "value": value, **({"href": href} if href else {}), **extra}


def _plural(n: float, one: str) -> str:
    return f"{n:g} {one}{'' if n == 1 else 's'}"


def _slot_order(reg: Registry, slot: str) -> tuple:
    roles = [*ROLES, "dps"]
    if slot in roles:
        return (0, roles.index(slot), "")
    classes = list(reg.profile.classes)
    cls = slot.split(":")[0]
    return (1, classes.index(cls) if cls in classes else 99, slot)


def minimum_rows(reg: Registry, rid: str) -> list[dict]:
    """[{slot, label ('Tanks'), min, note}] — every minimum a run of the raid at its full size reads (Registry.minimums),
    roles first, then classes in the profile's order; `note` = the stored reason (the guild's, else the profile's)."""
    stored = reg.raid_def(rid).get("minimums") or {}
    own = {s for s, e in ((reg.config.raids.get(rid) or {}).get("minimums") or {}).items() if min_entry(e)["min"] > 0}
    mins = reg.minimums(rid)
    return [{"slot": slot, "label": reg.comp_slot_words(slot), "min": n, "note": min_entry(stored[slot]).get("note") if slot in stored else None,
             "own": slot in own}  # the guild set it itself, not the game
            for slot, n in sorted(mins.items(), key=lambda kv: _slot_order(reg, kv[0]))]


def _played_in(reg: Registry, rid: str) -> dict:
    """'Played in': every schedule whose lineup holds the raid, by name, each with its href."""
    links = []
    for s in reg.all_schedules():
        if any(e.get("raid") == rid for e in row_lineup(s["raid"], s)):
            links.append({"name": schedule_name(s), "href": f"/schedules/{s['id']}"})
    if not links:
        return _fact("Played in", PAGE_WORDS["played_nowhere"], links=[])
    return _fact("Played in", ", ".join(x["name"] for x in links), links[0]["href"] if len(links) == 1 else None, links=links)


def raid_row(reg: Registry, rs: "rc.RaidStore", rid: str) -> dict:
    """RaidPage: {details [Fact] (Size, Groups, Lockout, Length, First opens, Played in), make_up [{slot, label, min,
    note}] (minimum_rows), make_up_empty (a sentence when there is none), priority [{key, label, value}] (the seat
    weights, highest first), used_when, one_off_rule, open_events (live events playing it), boxes {reset}}."""
    rd = reg.raid_def(rid)
    size = int(rd.get("size") or 0)
    gsize = int(reg.profile.comp_rules["group_size"])
    fo = reg.first_open(rid)
    now = reg.now_local()
    details = [_fact("Size", f"{size} players"), _fact("Groups", f"{rc.groups_per_roster(reg, size)} of {gsize}"),
               _fact("Lockout", _plural(int(rd.get("lockout_days") or 7), "day")),
               _fact("Length", "About " + _plural(float(rd.get("duration_hours") or 0), "hour")),
               _fact("First opens", reg.local12(fo) if fo is not None and fo > now else PAGE_WORDS["open_now"]),
               _played_in(reg, rid)]
    weights = dict(rd.get("weights") or {})
    order = list(PAGE_WORDS["priority"])
    priority = [{"key": k, "label": PAGE_WORDS["priority"][k], "value": float(weights.get(k) or 0)}
                for k in sorted(order, key=lambda k: (-float(weights.get(k) or 0), order.index(k)))]
    rows = minimum_rows(reg, rid)
    return {"details": details, "make_up": rows, "make_up_empty": None if rows else PAGE_WORDS["no_minimums"],
            "priority": priority, "used_when": PAGE_WORDS["used_when"], "one_off_rule": PAGE_WORDS["one_off_rule"],
            "open_events": cq.open_events_of_raid(reg, rs, rid), "boxes": {"reset": cq.raid_reset_box(reg, rid)}}


def readiness_view(reg: Registry, rid: str, r: dict, players=None) -> dict:
    """The Raids page's Readiness view from comp.readiness' answer `r`: who_could_play [Fact] (Standing, Mains '27 for 20
    seats', Alts, Could switch role), make_up [{slot, label, have, min, own, level}] — the SAME rows as the Rules'
    make-up (minimum_rows: Registry.minimums alone), each with how many mains the guild has for it (comp's own counter
    over the pool) — `level` / `standing_why`: see standing. `players` = comp.pool_players, read once per request."""
    players = comp.pool_players(reg) if players is None else players
    hc = r["headcount"]
    rows = [{**row, "have": comp.slot_count(players, row["slot"], reg.profile)} for row in minimum_rows(reg, rid)]
    level, why, row_levels = standing(reg, r, rows)
    # who could switch role: comp's cover data by Discord id (an offspec or a declared extra role), each person once
    switch = {u for ro in r.get("roles") or [] for k in ("offspec", "flex") for u in (ro.get("cover_ids") or {}).get(k) or []}
    who = [_fact("Standing", LEVEL_WORDS[level], level=level),
           _fact("Mains", PAGE_WORDS["for_seats"].format(n=hc["mains"], seats=hc["size"])),
           _fact("Alts", str(hc["alts"])), _fact("Could switch role", str(len(switch)))]
    return {"who_could_play": who, "level": level, "standing_why": why,
            "make_up": [{**{k: x[k] for k in ("slot", "label", "have", "min", "own")}, "level": lv} for x, lv in zip(rows, row_levels)],
            "make_up_empty": None if rows else PAGE_WORDS["no_minimums"]}


LEVEL_ORDER = ("green", "amber", "red")


def standing(reg: Registry, r: dict, rows: list[dict]) -> tuple[str, str | None, list[str]]:
    """Readiness' Standing from exactly what the page prints (the "one answer" ruling): the worst of the headcount's
    level, the tank and healer levels (comp.readiness' roles) and each minimum row's level (comp's own line for that
    slot: have against the minimum, offspec switches counted as comp counts them). The buff matrix's class wants are
    not minimums and stay out of it. → (level, the sentence naming the first printed row at that level — None when
    ready —, each row's level)."""
    roles = {ro["role"]: ro for ro in r.get("roles") or []}
    lines = {c["key"]: c for c in r.get("comp") or []}
    parts: list[tuple[str, str | None]] = [(r["headcount"]["level"], PAGE_WORDS["standing_mains"])]
    row_levels = []
    for row in rows:
        slot = row["slot"]
        if slot in roles and roles[slot].get("need"):
            lv = roles[slot]["level"]
        elif slot in lines and int(lines[slot]["want"]) == int(row["min"]):
            lv = lines[slot]["level"]
        else:  # no line of comp's for it: met or not
            lv = "green" if row["have"] >= row["min"] else "red"
        row_levels.append(lv)
        parts.append((lv, PAGE_WORDS["standing_under"].format(slot=row["label"])))
    worst = max((lv for lv, _ in parts), key=LEVEL_ORDER.index)
    why = None if worst == "green" else next(w for lv, w in parts if lv == worst)
    return worst, why, row_levels
