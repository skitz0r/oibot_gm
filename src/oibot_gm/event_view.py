"""The Events page's read model (design §7a, phase 4): the list (Open · Coming up · History), one event in sections, and
a coming-up event — pure builders over the existing readers (rc.event_parts, run_lineup, needs, fill_state_text,
occurrences, open_row_refusal, log_rows, Registry.local12, the confirmation boxes). Nothing here solves, writes or
composes a fact of its own: it picks the readers' answers and the words of constants.PAGE_WORDS. An "event" is a first
part and its chain; a later part is never a row of its own and a dropped part is no part. Test runs are never listed. A
run key travels only as an id (`key`), never inside a word. The routes (web/api_events.py) run these in a thread."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable

from . import consequences as cq
from . import raidcycle as rc
from .constants import MARK_WORDS, PAGE_WORDS
from .registry import Registry, RegistryError

CLOSED = ("done", "cancelled")
HISTORY_PAGE = 20  # history rows per page, newest first
COMING_PER_SCHEDULE = 3  # each active schedule's next three nights with no event yet
COMING_HOURS = 24 * 7 * 8  # how far ahead Coming up looks for them (eight weeks: three nights of any weekly or lockout schedule)
# a coming-up night that is not on its schedule any more (the schedule was edited or removed), and one opened since
COMING_GONE = "That night is no longer on the schedule."
COMING_OPENED = "That night has a signup sheet now."


# ---------------------------------------------------------------- shared pieces

def night_index(reg: Registry, rs: "rc.RaidStore") -> Callable[[str, str], "rc.RaidEvent | None"]:
    """rc.event_of_occurrence for many nights at once: ONE pass over the run store, then a lookup per (schedule id,
    occurrence) answering exactly as the finder does — a stamped first part by its occurrence or the night it stands for,
    a run file from before the stamps by its start and schedule, else a live run of the schedule moved onto that minute;
    the newest created when several. tests/test_events_api.py pins that both answer alike."""
    stamped: dict[tuple, list] = defaultdict(list)
    moved: dict[tuple, list] = defaultdict(list)
    for e in rs.events.values():
        if not rc._first_part(e):
            continue
        if e.event:
            if e.test:
                continue
            stamped[(e.schedule, e.occurrence)].append(e)
            sf = rc.stands_for_of(reg, e)
            if sf and sf != e.occurrence:
                stamped[(e.schedule, sf)].append(e)
            if e.state not in CLOSED:
                moved[(e.schedule, rc.occurrence_stamp(reg, e.start))].append(e)
        elif not rc.is_test_run(reg, e):
            stamped[(rc.run_schedule_id(reg, e), rc.occurrence_stamp(reg, e.start))].append(e)

    def find(sid: str, occurrence: str):
        hits = stamped.get((sid, occurrence)) or moved.get((sid, occurrence)) or []
        return max(hits, key=lambda e: e.created_at) if hits else None
    return find


def _state(parts: list) -> str:
    """An event's state from its parts: live while any part is (locked when one is), else finished when one played."""
    live = [p for p in parts if p.state not in CLOSED]
    if live:
        return "locked" if any(p.state == "locked" for p in live) else "open"
    return "done" if any(p.state == "done" for p in parts) else "cancelled"


def _schedule_of(reg: Registry, first: "rc.RaidEvent") -> tuple[str | None, str | None, bool]:
    """(schedule id, its name, still a schedule?) of an event's first part; (None, None, False) for a one-off. A
    removed schedule keeps its id and reads by Registry.series_label ('A removed Barrow Deeps schedule')."""
    sid = rc.schedule_stamp(rc.run_schedule_id(reg, first))
    if not sid:
        return None, None, False
    try:
        entry = reg.schedule_entry(sid)
    except RegistryError:
        return sid, reg.series_label(sid), False
    return sid, entry.get("name") or reg.series_label(sid), True


def _lineup(reg: Registry, rs, first, parts: list) -> list[dict]:
    """The raids an event plays: rc.run_lineup while it is live (the run files of its live raids), else every part it
    had (a closed event's run dicts are gone, so each part's rosters are the ones it locked)."""
    if _state(parts) not in CLOSED:
        return rc.run_lineup(reg, rs, first)
    return [{"raid": p.instance, "rosters": len(p.all_rosters) or rc.run_rosters(reg, p)} for p in parts]


def _raids_words(reg: Registry, lineup: list[dict]) -> str:
    """'Hyjal Summit, then Barrow Deeps' — a list row's raids (rc.lineup_words without the roster counts)."""
    return rc.lineup_words(reg, [{"raid": e["raid"]} for e in lineup])


def _fact(label: str, value: str, href: str | None = None) -> dict:
    return {"label": label, "value": value, **({"href": href} if href else {})}


def _sheet_url(reg: Registry, ev: "rc.RaidEvent") -> str | None:
    if not (ev.channel_id and ev.message_id):
        return None
    return f"https://discord.com/channels/{reg.config.discord_guild_id}/{ev.channel_id}/{ev.message_id}"


# ---------------------------------------------------------------- the list

def _row(reg: Registry, rs, first, *, history: bool) -> dict:
    parts = rc.event_parts(rs, first)
    state = _state(parts)
    sid, name, _ = _schedule_of(reg, first)
    raids = _raids_words(reg, _lineup(reg, rs, first, parts))
    if history:
        subtitle = f"{raids} · {rc.RUN_STATE_WORDS[state]}"
    else:
        subtitle = name if sid else raids
    return {"key": first.key, "title": reg.local12(first.start), "subtitle": subtitle, "state": state, "chip": rc.RUN_STATE_WORDS[state]}


def event_list(reg: Registry, rs: "rc.RaidStore", now: datetime, offset: int = 0) -> dict:
    """{open: [Row], coming: [ComingRow], history: [Row], history_more}. Row = {key (part 0's), title (its start),
    subtitle, state, chip}; open = live events (open and locked) soonest first; history = HISTORY_PAGE finished or
    cancelled events from `offset`, newest first. ComingRow = {schedule, occurrence, title, subtitle} = each active
    schedule's next COMING_PER_SCHEDULE nights (rc.occurrences) with no event — no event at all, nor one standing for it.
    Never solves; the run store is read once (night_index, one event_id pass)."""
    events = [e for e in rs.events.values() if not e.dropped]
    live_ids = {rc.event_id(e) for e in events if e.state not in CLOSED}
    firsts = [e for e in events if not rc.is_follow(e) and not rc.is_test_run(reg, e)]
    live = sorted((e for e in firsts if rc.event_id(e) in live_ids), key=lambda e: e.start)
    closed = sorted((e for e in firsts if rc.event_id(e) not in live_ids), key=lambda e: e.start, reverse=True)
    offset = max(0, int(offset or 0))
    page = closed[offset:offset + HISTORY_PAGE]
    find = night_index(reg, rs)
    coming, seen = [], defaultdict(int)
    for s, t in rc.occurrences(reg, now, COMING_HOURS):
        if seen[s["id"]] >= COMING_PER_SCHEDULE:
            continue
        occ = rc.occurrence_stamp(reg, t)
        if find(s["id"], occ) is not None:
            continue
        seen[s["id"]] += 1
        coming.append({"schedule": s["id"], "occurrence": occ, "title": reg.local12(t), "subtitle": s.get("name") or reg.series_label(s["id"])})
    return {"open": [_row(reg, rs, e, history=False) for e in live], "coming": coming,
            "history": [_row(reg, rs, e, history=True) for e in page], "history_more": offset + HISTORY_PAGE < len(closed)}


# ---------------------------------------------------------------- one event

def _who(s) -> dict:
    return {"uid": str(s.discord_id), "display_name": s.display_name, "character": s.character, "cls": s.cls, "spec": s.spec}


def _confirmed_words(reg: Registry, ev: "rc.RaidEvent") -> str:
    """The confirmations tally in the marks' words ('12 confirmed · 3 waiting · 1 can't make it'), as the lock card
    counts them (rc.confirmations' answers)."""
    answers = [c["answer"] for c in rc.confirmations(reg, ev)]
    parts = [f"{n} {MARK_WORDS[k]}" for k in ("yes", None, "no", "expired") if (n := sum(1 for a in answers if a == k))]
    return " · ".join(parts) if parts else rc.NO_CONFIRMATIONS


def _part_view(reg: Registry, rs, p: "rc.RaidEvent", sheet: Callable | None) -> dict:
    """One raid of an event as the Board section draws it. `sheet` = ev_json (the route's) for BoardView's input."""
    name = reg.raid_def(p.instance).get("name", p.instance) if p.instance in reg.profile.raids else str(p.instance)
    locked = p.state != "open" and bool(p.all_rosters)
    joined = p.by_status("in")
    if locked:
        placed = len(p.seated())
    else:
        names = {s.display_name for s in joined}
        placed = sum(1 for g in rc.board_layout(reg, p) for n in g if n in names)
    n = len(p.all_rosters) if locked else rc.run_rosters(reg, p)
    return {"key": p.key, "raid": p.instance, "raid_name": name, "label": f"{reg.local12(p.start, '%I:%M %p')} · {name}",
            "rosters_words": PAGE_WORDS["rosters_of"].format(n=n, size=rc.run_size(reg, p)),
            "placed_words": PAGE_WORDS["placed_of"].format(n=placed, m=len(joined)),
            "short": dict(rc.needs(reg, p)["roles"]) if p.state not in CLOSED else {},
            "sheet": sheet(p) if sheet is not None else None,
            "board_state": "locked" if locked or p.state in CLOSED else "ready", "board_error": None, "mine": [], "undo_box": None}


def event_json(reg: Registry, rs: "rc.RaidStore", ev: "rc.RaidEvent", viewer: int, *, sheet: Callable | None = None) -> dict:
    """EventView: {key (part 0's), title, state, chip, kind (one_off | scheduled), schedule {id, name} | null, details
    [Fact], signups {joined, bench, no_thanks, no_answer, rows {in, sub, out, none}}, parts [PartView], activity [{when,
    line}] (every part's log, newest first), boxes {lock, cancel}, actions, lineup, start_iso, timing {setting: value}
    (rc.EVENT_TIMING's settings, what POST /api/run/{key}/reschedule takes back), people (members with no answer:
    Answer for someone), sheet_url}. Any part's key answers as its event. `viewer` = the officer looking (their own board
    moves are Task 3's)."""
    first = rc.first_part(rs, ev)
    parts = rc.event_parts(rs, first)
    state = _state(parts)
    live = state not in CLOSED
    team = rc.run_team(reg, first)
    sid, sname, exists = _schedule_of(reg, first)
    lineup = _lineup(reg, rs, first, parts)
    came = _fact("Comes from", sname, f"/schedules/{sid}" if exists else None) if sid else _fact("Comes from", PAGE_WORDS["one_off"])
    url = _sheet_url(reg, first)
    if live:
        from .raid_views import run_times

        _soft, hard, _confirm = run_times(reg, first, team)
        opened = (datetime.fromisoformat(first.created_at) if first.created_at
                  else first.start - timedelta(hours=float(reg.raid_def(first.instance)["signup_lead_hours"])))
        details = [_fact("Lineup", rc.lineup_words(reg, lineup)), came, _fact("Priority", rc.priority_words(reg, first)),
                   _fact("Confirmation", rc.confirmation_words(rc.run_confirms(reg, first))),
                   _fact("Sheet opened", reg.local12(opened)), _fact("Locks", reg.local12(first.locked_at or hard))]
        if state == "locked":
            if rc.run_confirms(reg, first):
                details.append(_fact("Confirmed", _confirmed_words(reg, first)))
            details.append(_fact("Open seats", rc.fill_state_text(reg, first)))
        if url:
            details.append(_fact("Signup sheet", PAGE_WORDS["open_in_discord"], url))
        else:
            details.append(_fact("Signup sheet", PAGE_WORDS["sheet_not_posted"] if reg.config.signup_channel_id else PAGE_WORDS["sheet_waits"]))
    else:
        rostered = {p.signup_name for part in parts for p in part.seated()}
        details = [_fact("Lineup", rc.lineup_words(reg, lineup)), came, _fact("Rostered", str(len(rostered)))]
        if first.closed_at:
            details.append(_fact("Ended", reg.local12(first.closed_at)))
    rows = {st: [_who(s) for s in sorted(first.by_status(st), key=lambda s: s.updated_at)] for st in ("in", "sub", "out")}
    waiting = rc.not_answered(reg, first) if live else []
    rows["none"] = [{"uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec} for m, c in waiting]
    activity = []
    for p in parts:
        activity += list(reversed(rc.log_rows(reg, p)))
    activity.sort(key=lambda r: r["at"] or "", reverse=True)
    people = [{"uid": str(m.discord_id), "display_name": m.display_name,
               "characters": [{"label": c.label, "cls": c.cls, "spec": c.spec, "role": reg.profile.spec(c.cls, c.spec).role, "is_main": c.is_main}
                              for c in sorted(m.active(), key=lambda c: (not c.is_main, c.label.lower()))]}
              for m in sorted(reg.members.values(), key=lambda m: m.display_name.lower())
              if not m.test and m.active() and str(m.discord_id) not in first.signups] if live else []
    started = first.start <= reg.now_local()
    timing = {k: (team.get(v) if v in team else rc.team_setting(team, v)) for k, v in rc.EVENT_TIMING.items()}
    timing["nudge"] = timing["nudge"] not in (None, rc.OFF, False)
    timing["autofill"] = bool(timing["autofill"])
    return {"key": first.key, "title": reg.local12(first.start), "state": state, "chip": rc.RUN_STATE_WORDS[state],
            "kind": "scheduled" if sid else "one_off", "schedule": {"id": sid, "name": sname} if sid else None,
            "details": details,
            "signups": {"joined": len(rows["in"]), "bench": len(rows["sub"]), "no_thanks": len(rows["out"]), "no_answer": len(rows["none"]), "rows": rows},
            "parts": [_part_view(reg, rs, p, sheet) for p in parts],
            "activity": [{"when": r["when"], "line": r["text"]} for r in activity],
            "boxes": {"lock": cq.lock_box(reg, first, rs) if state == "open" else None, "cancel": cq.cancel_box(reg, first, rs) if live else None},
            "actions": {"reschedule": live and not started, "edit_lineup": state == "open" and first.state == "open",
                        "lock": state == "open" and first.state == "open", "cancel": live, "open_now": False},
            "lineup": lineup, "start_iso": first.start.isoformat(), "timing": timing, "people": people, "sheet_url": url}


# ---------------------------------------------------------------- a coming-up event

class ComingGone(RegistryError):
    """The night is not a coming-up one any more: `key` = the event that holds it now (None: off the schedule)."""

    def __init__(self, sentence: str, key: str | None = None):
        super().__init__(sentence)
        self.key = key


def coming_json(reg: Registry, rs: "rc.RaidStore", sid: str, occurrence: str, now: datetime) -> dict:
    """ComingView: {title, chip, schedule {id, name}, details [Lineup, Comes from, Priority, Confirmation, Sheet opens,
    Opening by hand, Locks], can_open, why (rc.open_row_refusal's sentence | None), open {start_iso, schedule,
    occurrence} (what POST /api/raid/{rid}/open carries back), box (consequences.open_now_box | None)}. ComingGone when
    the night has an event now (its key) or is no longer one of the schedule's nights within COMING_HOURS."""
    hit = night_index(reg, rs)(sid, occurrence)
    if hit is not None:
        raise ComingGone(COMING_OPENED, rc.first_part(rs, hit).key)
    t = next((t for s, t in rc.occurrences(reg, now, COMING_HOURS) if s["id"] == sid and rc.occurrence_stamp(reg, t) == occurrence), None)
    if t is None:
        raise ComingGone(COMING_GONE)
    entry = reg.schedule_entry(sid)
    raid = entry["raid"]
    sd = reg.schedule_def(raid, sid)
    lineup = entry.get("lineup") or rc.schedule_lineup(raid, entry)
    opens = t - timedelta(hours=float(sd["signup_lead_hours"]))
    lock_at = t - timedelta(hours=float(sd["lock_hours_before"]))
    name = entry.get("name") or reg.series_label(sid)
    why = rc.open_row_refusal(reg, rs, raid, t, sid, opens, now)
    details = [_fact("Lineup", rc.lineup_words(reg, lineup)), _fact("Comes from", name, f"/schedules/{sid}"),
               _fact("Priority", rc.priority_words(reg, sd)), _fact("Confirmation", rc.confirmation_words(bool(sd.get("confirm", True)))),
               _fact("Sheet opens", reg.local12(opens)),
               _fact("Opening by hand", PAGE_WORDS["from"].format(when=reg.local12(t - timedelta(days=rc.OPEN_HORIZON_DAYS)))),
               _fact("Locks", reg.local12(lock_at))]
    box = None if why else cq.open_now_box(reg, t, opens, lock_at, lineup=lineup, channel=bool(reg.config.signup_channel_id))
    return {"title": reg.local12(t), "chip": PAGE_WORDS["no_sheet_yet"], "schedule": {"id": sid, "name": name}, "details": details,
            "can_open": why is None, "why": why, "open": {"start_iso": t.isoformat(), "schedule": sid, "occurrence": occurrence}, "box": box}
