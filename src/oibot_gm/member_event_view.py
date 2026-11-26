"""The members' Events page read model (design 2026-11-25 §2): what the Discord signup sheet (raid_views.sheet_message)
already shows everyone, and nothing more. Built from the readers the sheet reads — each raid's signups by answer, the
Away split (raid_views.split_away), the locked rosters' groups, the confirmation marks (rc.confirmations, only while
locked and only when the event asks for confirmations) and the joiners the lock left off — never by stripping
event_view.event_json, so no officer-only fact (who has not answered, absence reasons, callout notes, pins, fill asks,
the officers' board before the lock, role switches, the activity log, priority scores) can reach it by accident. Pure:
nothing here writes or solves. The routes (web/api_member_events.py) run these in a thread and add the viewer's own
answer rows and asks."""
from __future__ import annotations

from datetime import datetime

from . import event_view
from . import raidcycle as rc
from .constants import PAGE_WORDS
from .raid_views import OUT_MARK, run_times, split_away
from .registry import Registry


class NotListed(LookupError):
    """A key the members' page does not show: unknown, finished, cancelled, a raid taken out of its lineup, or another
    tester's rehearsal. The route answers PAGE_WORDS["event_over_member"] alone (a non-tester learns nothing)."""


def visible(reg: Registry, e: "rc.RaidEvent", viewer: int) -> bool:
    """Does `e` head an event this member's page lists: a first raid (rc._first_part), not taken out of a lineup, and not
    a rehearsal — unless it is the viewer's own (its run dict's `test_by`)."""
    if not rc._first_part(e) or e.dropped:
        return False
    if not rc.rehearsal_run(reg, e):
        return True
    tester = (reg.config.team(e.team) or {}).get("test_by")
    return tester is not None and int(tester) == int(viewer)


def _seat_roster(reg: Registry, ev: "rc.RaidEvent", m) -> int | None:
    """The viewer's roster number (1-based) on a locked raid, as the Me page reads it (ev.seat_of)."""
    seat = ev.seat_of(m) if m is not None and ev.state != "open" else None
    return (seat[0] + 1) if seat else None


def _answer(reg: Registry, first: "rc.RaidEvent", viewer: int) -> str:
    """The viewer's own standing on the event in the Me page's word (rc.answer_word on the first raid)."""
    s = first.signups.get(str(viewer))
    return rc.answer_word(first, s.status if s else None, _seat_roster(reg, first, reg.members.get(int(viewer))))


def member_event_list(reg: Registry, rs: "rc.RaidStore", now: datetime, viewer: int) -> dict:
    """{open: [MemberRow], coming: [{title, subtitle}], tz_words, empty}. MemberRow = {key (the first raid's), title (its
    start), subtitle (the raids it plays), state, chip, answer (the viewer's own, rc.answer_word)}; live events only (open
    and locked), soonest first — no History. Coming up = event_view.coming_nights (each active schedule's next nights
    with no event), plain text: its start and its schedule's name."""
    events = [e for e in rs.events.values() if not e.dropped]
    live_ids = {rc.event_id(e) for e in events if e.state not in event_view.CLOSED}
    live = sorted((e for e in events if visible(reg, e, viewer) and rc.event_id(e) in live_ids), key=lambda e: e.start)
    rows = []
    for first in live:
        parts = rc.event_parts(rs, first)
        state = event_view._state(parts)
        rows.append({"key": first.key, "title": reg.local12(first.start),
                     "subtitle": event_view._raids_words(reg, event_view._lineup(reg, rs, first, parts)),
                     "state": state, "chip": rc.RUN_STATE_WORDS[state], "answer": _answer(reg, first, viewer)})
    coming = [{"title": reg.local12(t), "subtitle": s.get("name") or reg.series_label(s["id"])}
              for s, t, _occ in event_view.coming_nights(reg, rs, now)]
    return {"open": rows, "coming": coming, "tz_words": event_view.tz_words(reg), "empty": PAGE_WORDS["member_events_empty"]}


def _person(s, callout: bool = False) -> dict:
    return {"display_name": s.display_name, "character": s.character, "cls": s.cls, "spec": s.spec, "callout": callout}


def _signups(reg: Registry, first: "rc.RaidEvent") -> dict:
    """The sheet's answer rows: Joined, Bench, No thanks and Away (split_away: a registered absence that day, its reason
    never), each in ev.by_status order; `callout` = the sheet's ⚑ on a No thanks (OUT_MARK: called out after answering)."""
    away, declined = split_away(reg, first, first.by_status("out"))
    rows = {"in": [_person(s) for s in first.by_status("in")], "sub": [_person(s) for s in first.by_status("sub")],
            "out": [_person(s, s.source in OUT_MARK) for s in declined], "away": [_person(s) for s in away]}
    return {"counts": {k: len(v) for k, v in rows.items()}, "rows": rows}


def _part(reg: Registry, p: "rc.RaidEvent") -> dict:
    """One locked (or finished) raid of the event as the locked sheet draws it: each roster's groups, one member per
    line, the confirmation marks only while locked and only when the event asks for them, and Not rostered = the
    joiners the lock left off (rosters[0].benched ∩ the joiners). Never the officers' board."""
    name = reg.raid_def(p.instance).get("name", p.instance) if p.instance in reg.profile.raids else str(p.instance)
    rosters = p.all_rosters
    marks = bool(rosters) and p.state == "locked" and rc.run_confirms(reg, p)
    conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, p)} if marks else {}
    out = []
    for i, r in enumerate(rosters):
        by = {pl.signup_name: pl for pl in r.selected}
        groups = []
        for g in r.groups:
            seats = []
            for n in g:
                if n not in by:
                    continue
                pl = by[n]
                seat = {"display_name": pl.signup_name, "character": pl.character or pl.signup_name, "cls": pl.cls, "spec": pl.spec, "role": pl.role}
                if marks:
                    seat["answer"] = conf.get(pl.signup_name)
                seats.append(seat)
            groups.append(seats)
        out.append({"n": i + 1, "groups": groups})
    joined = {s.display_name for s in p.by_status("in")}
    left_off = [pl.signup_name for pl in rosters[0].benched if pl.signup_name in joined] if rosters else []
    return {"key": p.key, "raid_name": name, "label": f"{reg.local12(p.start, '%I:%M %p')} · {name}", "rosters": out,
            "not_rostered": left_off, "marks": marks}


def member_event_json(reg: Registry, rs: "rc.RaidStore", ev: "rc.RaidEvent", viewer: int) -> dict:
    """MemberEvent: {key (the first raid's), title, subtitle, state, chip, test_chip, details [Fact], signups {counts
    {in, sub, out, away}, rows {in, sub, out, away: [MemberPerson]}}, rosters [MemberPart] (one per locked or finished
    raid; [] before the lock), part_keys (every raid of the event, the first first)}. Details are the sheet's public
    facts: Lineup, Priority, Locks, Confirm by (only when the event asks for confirmations), Signup sheet. Any raid's key
    answers as its event. NotListed for what `visible` refuses, and for a finished or cancelled event."""
    first = rc.first_part(rs, ev)
    if ev.dropped or not visible(reg, first, viewer):
        raise NotListed(ev.key)
    parts = rc.event_parts(rs, first)
    state = event_view._state(parts)
    if state in event_view.CLOSED:
        raise NotListed(ev.key)
    lineup = event_view._lineup(reg, rs, first, parts)
    _soft, hard, confirm = run_times(reg, first, rc.run_team(reg, first))
    details = [event_view._fact("Lineup", rc.lineup_words(reg, lineup)), event_view._fact("Priority", rc.priority_words(reg, first)),
               event_view._fact("Locks", reg.local12(first.locked_at or hard))]
    if rc.run_confirms(reg, first):
        details.append(event_view._fact("Confirm by", reg.local12(confirm)))
    url = event_view._sheet_url(reg, first)
    details.append(event_view._fact("Signup sheet", PAGE_WORDS["open_in_discord"], url) if url
                   else event_view._fact("Signup sheet", PAGE_WORDS["sheet_not_posted"]))
    return {"key": first.key, "title": reg.local12(first.start), "subtitle": event_view._raids_words(reg, lineup),
            "state": state, "chip": rc.RUN_STATE_WORDS[state],
            "test_chip": PAGE_WORDS["test_run"] if rc.rehearsal_run(reg, first) else None,
            "details": details, "signups": _signups(reg, first),
            "rosters": [_part(reg, p) for p in parts if p.state in ("locked", "done")],
            "part_keys": [p.key for p in parts]}
