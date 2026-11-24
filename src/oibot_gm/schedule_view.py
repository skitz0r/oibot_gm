"""The Schedules page's read model (design §7a, phase 4): the list (Active · Paused) and one schedule in sections —
pure builders over the existing readers (Registry.all_schedules / schedule_def / schedule_runs_open / regulars /
lineup_offsets, rc.next_runs_of, rc.occurrence_index, rc.series_outlook, rc.gone_raids, rc.priority_words /
confirmation_words, the confirmation boxes). Nothing here writes or composes a fact of its own: it picks the readers'
answers and the words of constants.PAGE_WORDS. A schedule id travels only as an id (`id`, an href), never inside a
word; every time is Registry.local12 words; "5 days before" is constants.hours_words. The routes
(web/api_schedules.py) run these in a thread."""
from __future__ import annotations

from datetime import datetime

from . import consequences as cq
from . import raidcycle as rc
from .constants import PAGE_WORDS, hours_words
from .presets import CADENCE_FIELDS, PRESETS
from .registry import (
    ADHOC_DEFAULTS,
    ADHOC_WEIGHTS,
    RAID_WEIGHT_DEFAULTS,
    RESERVED_SCHEDULE_IDS,
    SCHEDULE_CADENCE,
    SCHEDULE_TIMING,
    SPLIT_POLICIES,
    Registry,
    RegistryError,
    parse_lineup,
    row_lineup,
    schedule_class,
    schedule_name,
    schedule_value,
)

NEXT_EVENTS = 3  # Next events: the schedule's next three nights
CHIPS = {True: "active", False: "paused"}
GONE_TAIL = ", so this schedule opens no events"  # after rc.gone_sentence's raids: the one sentence the page shows
# what the editor sends back (`stored`): every setting set_schedule takes, the lineup in place of rosters / follow / then
STORED_FIELDS = ("name", "kind", "active", "slots", "days", "time", "weights", "lineup") + SCHEDULE_CADENCE
# the editor's sheet-timing choices (hours before the start; a fill ask's wait in hours), each in the words the page
# prints — a value a schedule, a raid's suggestion or a preset holds is added in its place, so a stored value is never
# missing from its select
HOUR_CHOICES = (1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48, 60, 72, 96, 120, 144, 168)
ASK_CHOICES = (1, 2, 3, 4, 6, 8, 12, 24)
MAX_WEIGHT = 6  # a priority weight's highest choice (the Raids page's Priority selects too)


def _fact(label: str, value: str, href: str | None = None) -> dict:
    return {"label": label, "value": value, **({"href": href} if href else {})}


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def repeats_words(reg: Registry, entry: dict, *, full: bool = False) -> str:
    """When a schedule's events start, as the Schedules page reads it: 'Every Tuesday at 6:30 PM', 'Day 1 of each
    lockout at 8:00 PM' (`full`: 'of each 5-day lockout', the Details' Repeats) — consequences.repeat_words, the create
    box's words; a pickup is opened by an officer; a schedule with no run times says how to add them."""
    if (entry.get("kind") or "weekly") == "pickup":
        return PAGE_WORDS["by_an_officer"]
    w = cq.repeat_words(reg, entry, lockout_length=full)
    return _cap(w.removeprefix("on ")) if w else PAGE_WORDS["no_times"]


def _name(entry: dict) -> str:
    return schedule_name(entry)


def _row(reg: Registry, s: dict) -> dict:
    return {"id": s["id"], "name": _name(s), "repeats": repeats_words(reg, s)}


def schedule_list(reg: Registry) -> dict:
    """{active: [SRow], paused: [SRow], reserved_ids}: SRow = {id, name, repeats}, in the guild's order — a schedule whose
    raid left the game listed too (its Details say so). `reserved_ids` = the ids no new schedule takes
    (registry.RESERVED_SCHEDULE_IDS: the editor never holds its own copy)."""
    rows = reg.all_schedules()
    return {"active": [_row(reg, s) for s in rows if s.get("active", True)],
            "paused": [_row(reg, s) for s in rows if not s.get("active", True)],
            "reserved_ids": list(RESERVED_SCHEDULE_IDS), "orphans": orphans(reg), "editor": editor_options(reg)}


def orphans(reg: Registry) -> list[dict]:
    """A removed schedule's leftover regulars (Registry.orphan_regulars — an older file's; a removal today takes its
    regulars off): {key (the series key the clear sends back), label (Registry.series_label: never the key), uid,
    display_name, character, cls, spec, box (Remove's box, consequences.regular_remove_box), rev (the record's)}."""
    return [{"key": k, "label": reg.series_label(k), "uid": str(m.discord_id), "display_name": m.display_name, "character": c.label,
             "cls": c.cls, "spec": c.spec, "box": cq.regular_remove_box(reg, m, k), "rev": reg.characters_rev(m)}
            for k, m, c in reg.orphan_regulars()]


def _num(h):
    return int(h) if float(h).is_integer() else float(h)


def editor_options(reg: Registry) -> dict:
    """What the schedule editor picks from (New schedule and Edit): {raids (the palette — New event's — with each raid's
    lockout_days, weights (the raid's own, what "Use the raid's" falls back to) and timing (the game's suggestion a new
    schedule starts from)), adhoc {timing, weights} (a pickup's), lineups (Start from › Copy a lineup: New event's),
    lineup_max, lineup_full, hours {field: [{value, words}]} ("5 days before"; a fill ask's wait "2 hours"), presets
    [{name, values}] (presets.PRESETS), split_policies, max_weight}. Every word the editor's selects show is here."""
    from .event_view import new_event_options

    base = new_event_options(reg)
    raids = []
    for r in base["raids"]:
        rd = reg.raid_def(r["id"])
        raids.append({**r, "lockout_days": int(rd.get("lockout_days") or 7),
                      "weights": {k: _num((rd.get("weights") or {}).get(k, v)) for k, v in RAID_WEIGHT_DEFAULTS.items()},
                      "timing": reg.timing_suggestion(r["id"])})
    seen: dict[str, set] = {f: set() for f in ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours")}
    timings = [r["timing"] for r in raids] + [ADHOC_DEFAULTS]
    for s in reg.all_schedules():
        try:
            timings.append(reg.schedule_def(s["raid"], s["id"]))
        except (RegistryError, KeyError):
            continue
    for t in timings:
        for f in seen:
            if t.get(f) is not None:
                seen[f].add(float(t[f]))
    for p in PRESETS:
        for f, v in zip(CADENCE_FIELDS, p[2:]):
            seen[f].add(float(v))
    hours = {f: [{"value": _num(h), "words": hours_words(h) if f == "fill_ask_hours" else f"{hours_words(h)} before"}
                 for h in sorted(set(map(float, ASK_CHOICES if f == "fill_ask_hours" else HOUR_CHOICES)) | v)]
             for f, v in seen.items()}
    return {"raids": raids, "adhoc": {"timing": {k: ADHOC_DEFAULTS[k] for k in SCHEDULE_TIMING if k in ADHOC_DEFAULTS},
                                      "weights": dict(ADHOC_WEIGHTS)},
            "lineups": base["lineups"], "lineup_max": base["lineup_max"], "lineup_full": base["lineup_full"], "hours": hours,
            "presets": [{"name": p[1], "values": dict(zip(CADENCE_FIELDS, p[2:]))} for p in PRESETS],
            "split_policies": list(SPLIT_POLICIES), "max_weight": MAX_WEIGHT}


def stored_entry(s: dict) -> dict:
    """One schedule in machine form for the editor: every STORED_FIELDS value as the schedule holds it (the lineup as
    parse_lineup reads it). Sent back unchanged it is no edit (configops._schedule_ops leaves out what equals this)."""
    out = {f: s.get(f) for f in STORED_FIELDS if f != "lineup"}
    out["slots"], out["days"] = list(s.get("slots") or []), list(s.get("days") or [])
    try:
        out["lineup"] = parse_lineup(row_lineup(s["raid"], s))
    except RegistryError:
        out["lineup"] = row_lineup(s["raid"], s)
    return out


def unchanged(s: dict, field: str, value) -> bool:
    """Does the editor's `value` for `field` equal what schedule `s` (an all_schedules row) holds — read through the
    verb's own normalizer (schedule_value / parse_lineup)? A value the verb would refuse is never 'unchanged'."""
    try:
        if field == "lineup":
            return parse_lineup(value) == parse_lineup(row_lineup(s["raid"], s))
        return schedule_value(field, value) == schedule_value(field, s.get(field))
    except (RegistryError, TypeError, ValueError):
        return False


def _raid_name(reg: Registry, rid: str) -> str:
    return reg.raid_def(rid).get("name", rid) if rid in reg.profile.raids else rc.gone_name(reg, rid)


def _lineup(reg: Registry, s: dict, first: datetime | None) -> list[dict]:
    """[{raid, name, rosters, starts, time_words, seats}] — each raid of the lineup, its start in minutes after the first
    (Registry.lineup_offsets), the clock it starts at on the next night ('9:30 PM'; '' with no night to read it from),
    and the seats of one roster."""
    lineup = row_lineup(s["raid"], s)
    try:
        offsets = reg.lineup_offsets(lineup)
    except RegistryError:
        offsets = [0] * len(lineup)
    out = []
    for e, off in zip(lineup, offsets):
        rid = e.get("raid")
        size = int(reg.raid_def(rid).get("size") or 0) if rid in reg.profile.raids else 0
        out.append({"raid": rid, "name": _raid_name(reg, rid), "rosters": int(e.get("rosters") or 1), "starts": int(off),
                    "time_words": reg.local12(rc.after_minutes(first, off), "%I:%M %p") if first is not None else "", "seats": size})
    return out


def _regulars(reg: Registry, rs, s: dict, sd: dict) -> dict | None:
    """The Regulars section of a recurring schedule (None for a pickup: first come): the count against the first raid's
    seats, who they are with Remove's box (Task 1 #18) and the record's rev, the coverage short (Registry.series_coverage,
    None with no regulars) and the next four weeks (rc.series_forecast — a paused schedule's as if resumed; absent while
    it has none)."""
    if schedule_class(s.get("kind")) != "recurring":
        return None
    sid = s["id"]
    regs = reg.regulars(sid)
    row = next((r for r in (reg.series_coverage(s["raid"]) if s["raid"] in reg.profile.raids else []) if r["id"] == sid), None)
    # the seats the coverage counts against (series_coverage); a schedule whose first raid left the game has no row
    seats = row["seats"] if row is not None else int(sd.get("size") or 0) * int(s.get("rosters") or 1)
    out: dict = {"count": len(regs), "words": PAGE_WORDS["for_seats"].format(n=len(regs), seats=seats), "short": None}
    if not regs:
        out["expected"] = PAGE_WORDS["every_main"]
    if row is not None and regs:
        out["short"] = dict(row["short"]) or None
        # the forecast (rc.series_forecast, as series_outlook attaches it); a paused schedule's as if resumed, shown
        # beside "Next event if resumed"
        forecast = rc.series_forecast(reg, sid, if_resumed=not s.get("active", True))
        out["forecast"] = _forecast_rows(reg, rs, s, sd, forecast) if forecast else []
        if forecast:
            n = sum(1 for f in forecast if f["short"])
            out["outlook"] = (PAGE_WORDS["ready_every_week"] if n == 0 else PAGE_WORDS["short_every_week"] if n == len(forecast)
                              else PAGE_WORDS["short_weeks"].format(n=n))
    out["rows"] = [{"uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec,
                    "box": cq.regular_remove_box(reg, m, sid, rs=rs), "rev": reg.characters_rev(m)} for m, c in regs]
    return out


def _forecast_rows(reg: Registry, rs, s: dict, sd: dict, forecast: list[dict]) -> list[dict]:
    """The regulars' next four weeks, a row per night (rc.series_forecast): {day, when, short {role: n}, away [names],
    expected, occurrence, key (the event open at that night | null), can_open, why} — Open this run early opens a short
    night's sheet by hand (POST /api/raid/{first raid}/open with the occurrence) while `can_open`; `why` is
    rc.open_row_refusal's sentence otherwise (past the opening horizon, a closed night). A paused schedule's rows (as if
    resumed) never open."""
    find = rc.occurrence_index(reg, rs)
    now = reg.now_local()
    active = bool(s.get("active", True))
    out = []
    for f in forecast:
        t = datetime.fromisoformat(f["start"])
        occ = rc.occurrence_stamp(reg, t)
        hit = find(s["id"], occ)
        key = hit.key if hit is not None and not hit.dropped else None
        why = None
        if key is None and active:
            opens, _locks = rc.sheet_times(t, sd)
            why = rc.open_row_refusal(reg, rs, s["raid"], t, s["id"], opens, now)
        out.append({"day": f["day"], "when": f["when"], "short": f["short"], "away": f["away"], "expected": f["expected"],
                    "start_iso": f["start"], "occurrence": occ, "key": key, "can_open": key is None and active and why is None,
                    "why": why})
    return out


def _people(reg: Registry, sid: str) -> list[dict]:
    """Add a member: every real member still in the server with an active character who isn't a regular of `sid`,
    by name, with their characters (the main first)."""
    taken = {m.discord_id for m, _c in reg.regulars(sid)}
    out = []
    for m in sorted(reg.members.values(), key=lambda x: x.display_name.lower()):
        if m.test or m.left_at or m.discord_id in taken or not m.active():
            continue
        chars = sorted(m.active(), key=lambda c: c is not m.main)
        out.append({"uid": str(m.discord_id), "display_name": m.display_name,
                    "characters": [{"label": c.label, "cls": c.cls, "spec": c.spec, "main": c is m.main} for c in chars]})
    return out


def schedule_json(reg: Registry, rs: "rc.RaidStore", sid: str, now: datetime | None = None) -> dict:
    """ScheduleView: {id, name, active, chip, gone (a sentence when a raid of its lineup left the game, else null),
    details [Fact], lineup [{raid, name, rosters, starts, time_words, seats}], timing [Fact], next [{starts, opens, locks,
    key | null}] (empty while paused or gone; `next_empty` says why), regulars (see _regulars) | null, people, boxes
    {pause | resume, remove}, stored (stored_entry), rev (config_rev('schedule:<id>'))}. RegistryError(NO_SCHEDULE) for
    an id no schedule has."""
    s = reg.schedule_entry(sid)
    sid = s["id"]
    now = now or reg.now_local()
    active = bool(s.get("active", True))
    gone = rc.gone_raids(reg, s["raid"], s)
    sd = reg.schedule_def(s["raid"], sid)
    pickup = (s.get("kind") or "weekly") == "pickup"
    starts = [] if gone or pickup else rc.next_runs_of(reg, s["raid"], s, NEXT_EVENTS, now)
    lineup = _lineup(reg, s, starts[0] if starts else None)

    details = [_fact("Repeats", repeats_words(reg, s, full=True))]
    if not pickup and not gone:
        first = reg.local12(starts[0]) if starts else PAGE_WORDS["no_times"]
        details.append(_fact("First event" if active else "Next event if resumed", first))
    details += [_fact("Priority", rc.priority_words(reg, {"kind": s.get("kind"), "weights": sd.get("weights")})),
                _fact("Confirmation", rc.confirmation_words(bool(sd.get("confirm"))))]
    if not pickup:
        details.append(_fact("Open seats after the lock", PAGE_WORDS["bot_asks"] if sd.get("autofill") else PAGE_WORDS["officer_asks"]))
    if len(lineup) > 1:
        details.append(_fact("Answers", PAGE_WORDS["answers_one"]))

    def before(h) -> str:
        return f"{hours_words(h)} before"

    open_n = len(reg.schedule_runs_open(sid, rs))
    timing = [_fact("Sheet opens", before(sd["signup_lead_hours"])),
              _fact("Reminder", before(sd["nudge_hours_before"]) if sd.get("nudge") else PAGE_WORDS["no_reminder"]),
              _fact("Locks", before(sd["lock_hours_before"])),
              _fact("Confirm by", before(sd["confirm_hours_before"]) if sd.get("confirm") else PAGE_WORDS["not_asked"])]
    if active or open_n:
        timing.append(_fact("Open events keeping their own timing", str(open_n)))

    nxt, next_empty = [], None
    if gone:
        next_empty = rc.gone_sentence(reg, gone, GONE_TAIL)
    elif not active:
        next_empty = PAGE_WORDS["paused_next"]
    elif pickup:
        next_empty = PAGE_WORDS["by_an_officer"]
    elif not starts:
        next_empty = PAGE_WORDS["no_times"]
    else:
        find = rc.occurrence_index(reg, rs)  # one pass over the run store for the three nights
        for t in starts:
            opens, locks = rc.sheet_times(t, sd)
            occ = rc.occurrence_stamp(reg, t)
            hit = find(sid, occ)
            nxt.append({"starts": reg.local12(t), "opens": reg.local12(opens), "locks": reg.local12(locks),
                        "start_iso": t.isoformat(), "occurrence": occ, "key": hit.key if hit is not None and not hit.dropped else None})

    boxes = {("pause" if active else "resume"): (cq.schedule_pause_box if active else cq.schedule_resume_box)(reg, rs, sid),
             "remove": cq.schedule_remove_box(reg, rs, sid)}
    return {"id": sid, "name": _name(s), "active": active, "chip": CHIPS[active], "kind": s.get("kind") or "weekly",
            "gone": rc.gone_sentence(reg, gone, GONE_TAIL) if gone else None,
            "details": details, "lineup": lineup, "timing": timing, "next": nxt, "next_empty": next_empty,
            "regulars": _regulars(reg, rs, s, sd), "people": _people(reg, sid) if schedule_class(s.get("kind")) == "recurring" else [],
            "boxes": boxes, "stored": stored_entry(s), "rev": reg.config_rev(f"schedule:{sid}")}

