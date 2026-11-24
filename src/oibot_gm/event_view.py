"""The Events page's read model (design §7a, phase 4): the list (Open · Coming up · History), one event in sections, and
a coming-up event — pure builders over the existing readers (rc.event_parts, run_lineup, needs, fill_state_text,
occurrences, open_row_refusal, log_rows, Registry.local12, the confirmation boxes). Nothing here solves, writes or
composes a fact of its own: it picks the readers' answers and the words of constants.PAGE_WORDS. An "event" is a first
part and its chain; a later part is never a row of its own and a dropped part is no part. Test runs are never listed. A
run key travels only as an id (`key`), never inside a word. The routes (web/api_events.py) run these in a thread."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from . import consequences as cq
from . import raidcycle as rc
from .constants import MARK_WORDS, PAGE_WORDS
from .registry import LINEUP_MAX, LINEUP_MAX_WORDS, Registry, RegistryError

CLOSED = ("done", "cancelled")
HISTORY_PAGE = 20  # history rows per page, newest first
COMING_PER_SCHEDULE = 3  # each active schedule's next three nights with no event yet
# a coming-up night that is not one any more: off its schedule (edited, paused or removed), or opened since
COMING_GONE = "The schedule no longer has an event on that night."
COMING_OPENED = "That night has its own event now."


# ---------------------------------------------------------------- shared pieces

def listed(reg: Registry, e: "rc.RaidEvent") -> bool:
    """Does `e` head an event the page lists: a first raid (rc._first_part, the predicate the night finder uses too),
    not taken out of a lineup, never a rehearsal (rc.rehearsal_run: an old finished one without its flag too)."""
    return rc._first_part(e) and not e.dropped and not rc.rehearsal_run(reg, e)


def coming_nights(reg: Registry, rs: "rc.RaidStore", now: datetime, find=None, sid: str | None = None) -> list[tuple[dict, datetime, str]]:
    """(schedule, start, occurrence) of each active schedule's next COMING_PER_SCHEDULE nights with no event (no run at
    all, nor one standing for it), soonest first, read through rc.next_runs_of, so a schedule whose raid first opens
    months ahead shows its first nights. Asks for three plus the nights already taken, until three are free. The list
    and a coming-up event both read it, so a listed night always opens. `find` = an rc.occurrence_index; `sid` = one
    schedule only."""
    find = find or rc.occurrence_index(reg, rs)
    out = []
    for s in reg.all_schedules():
        if (sid and s["id"] != sid) or not s.get("active", True) or s["kind"] == "pickup" or rc.gone_raids(reg, s["raid"], s):
            continue
        n, free = COMING_PER_SCHEDULE, []
        for _ in range(12):  # each round asks for as many more as it found taken; a schedule has few taken nights
            starts = rc.next_runs_of(reg, s["raid"], s, n, now)
            free = [(t, occ) for t in starts if find(s["id"], occ := rc.occurrence_stamp(reg, t)) is None]
            if len(free) >= COMING_PER_SCHEDULE or len(starts) < n:
                break
            n = COMING_PER_SCHEDULE + len(starts) - len(free)
        out += [(s, t, occ) for t, occ in free[:COMING_PER_SCHEDULE]]
    return sorted(out, key=lambda x: (x[1], x[0]["id"]))


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
    return [{"raid": p.instance, "rosters": len(p.all_rosters) or rc.run_rosters(reg, p),
             "starts": 0 if p is first else (p.starts or round((p.start - first.start).total_seconds() / 60))} for p in parts]


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
    schedule's next COMING_PER_SCHEDULE nights with no event (coming_nights). Never solves; the run store is read once
    (rc.occurrence_index, one event_id pass)."""
    events = [e for e in rs.events.values() if not e.dropped]
    live_ids = {rc.event_id(e) for e in events if e.state not in CLOSED}
    firsts = [e for e in events if listed(reg, e)]
    live = sorted((e for e in firsts if rc.event_id(e) in live_ids), key=lambda e: e.start)
    closed = sorted((e for e in firsts if rc.event_id(e) not in live_ids), key=lambda e: e.start, reverse=True)
    offset = max(0, int(offset or 0))
    page = closed[offset:offset + HISTORY_PAGE]
    coming = [{"schedule": s["id"], "occurrence": occ, "title": reg.local12(t), "subtitle": s.get("name") or reg.series_label(s["id"])}
              for s, t, occ in coming_nights(reg, rs, now, rc.occurrence_index(reg, rs))]
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


def _part_view(reg: Registry, rs, p: "rc.RaidEvent", sheet: Callable | None, viewer: int | None = None,
               board: tuple[str, dict | None] | None = None) -> dict:
    """One raid of an event as the Board section draws it. `sheet(p, rosters=None, open_seats=True)` = ev_json for
    BoardView's input. `board` = event_board's (state, previews) for the event. Before the lock the board is the
    lock's placement: `board_state` ready (current), working (being placed again: the last picture with the officers'
    current moves on top — rc.overlay_board —, else the officers' layout) or failed (the layout, `board_error` = the
    sentence); a locked or finished raid's board is its roster as it stands (locked). Open seats are drawn only on a
    ready or locked board. `mine` = the names the viewer placed by hand, `undo_box` their Undo my moves box,
    `layout` the officers' own placements."""
    name = reg.raid_def(p.instance).get("name", p.instance) if p.instance in reg.profile.raids else str(p.instance)
    locked = p.state != "open" and bool(p.all_rosters)
    joined = p.by_status("in")
    names = {s.display_name for s in joined}
    rosters, state, error = None, "locked" if locked or p.state in CLOSED else "ready", None
    if state == "ready" and board is not None:
        st, previews = board
        pv = (previews or {}).get(p.key)
        if isinstance(pv, str):
            state, error = "failed", pv
        elif isinstance(pv, list) and st == "working":  # the last picture with the officers' moves now: a drag never snaps back
            rosters, state = rc.overlay_board(reg, p, pv), "working"
        elif isinstance(pv, list):
            rosters = pv
        else:
            state = "working"
    if locked:
        placed = len(p.seated())
    elif rosters is not None:
        placed = sum(1 for r in rosters for x in r.selected if x.signup_name in names)
    else:
        placed = sum(1 for g in rc.board_layout(reg, p) for n in g if n in names)
    n = len(p.all_rosters) if locked else len(rosters) if rosters is not None else rc.run_rosters(reg, p)
    live = p.state == "open"
    mine = rc.own_moves(p, viewer) if live else []
    return {"key": p.key, "raid": p.instance, "raid_name": name, "label": f"{reg.local12(p.start, '%I:%M %p')} · {name}",
            "rosters_words": PAGE_WORDS["rosters_of"].format(n=n, size=rc.run_size(reg, p)),
            "placed_words": PAGE_WORDS["placed_of"].format(n=placed, m=len(joined)),
            "short": dict(rc.needs(reg, p)["roles"]) if p.state not in CLOSED else {},
            # open seats only on a current picture (or a locked roster): a picture being placed again counts none
            "sheet": sheet(p, rosters, state in ("ready", "locked")) if sheet is not None else None,
            "board_state": state, "board_error": error, "mine": mine,
            "undo_box": cq.undo_box_for(reg, p, viewer) if mine else None,
            "layout": rc.board_layout(reg, p) if live else None}


def event_json(reg: Registry, rs: "rc.RaidStore", ev: "rc.RaidEvent", viewer: int, *, sheet: Callable | None = None,
               board: tuple[str, dict | None] | None = None) -> dict:
    """EventView: {key (part 0's), title, state, chip, kind (one_off | scheduled), schedule {id, name} | null, details
    [Fact], signups {joined, bench, no_thanks, no_answer, rows {in, sub, out, none}}, parts [PartView], activity [{when,
    line}] (every part's log, newest first), boxes {lock, cancel, reschedule (the pick box)}, actions, lineup, start_iso, timing {setting: value}
    (rc.EVENT_TIMING's settings, what POST /api/run/{key}/reschedule takes back), people (members with no answer:
    Answer for someone), switches (rc.event_switches: the Role switches section; empty `short` = not drawn), sheet_url,
    today (the guild's date: Reschedule's lower bound), locks (Reschedule's Locks choices, the event's own among them)}.
    Any part's key answers as its event. `viewer` = the officer looking (their own board
    moves: each part's `mine` and Undo box). `board` = event_board.BoardCache.get's (state, previews): the open raids'
    boards as the lock would place them (None: the officers' layout, as the Rosters page draws it)."""
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
        rostered = {rc.player_uid(part, p) or p.signup_name for part in parts for p in part.seated()}  # a person once, whatever they played
        details = [_fact("Lineup", rc.lineup_words(reg, lineup)), came, _fact("Rostered", str(len(rostered)))]
        if first.closed_at:
            details.append(_fact("Ended", reg.local12(first.closed_at)))
    rows = {st: [_who(s) for s in sorted(first.by_status(st), key=lambda s: s.updated_at)] for st in ("in", "sub", "out")}
    waiting = rc.pool_unanswered(reg, first) if live else []
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
    timing = rc.run_timing(reg, first)
    can_move = live and not started
    return {"key": first.key, "title": reg.local12(first.start), "state": state, "chip": rc.RUN_STATE_WORDS[state],
            "kind": "scheduled" if sid else "one_off", "schedule": {"id": sid, "name": sname} if sid else None,
            "details": details,
            "signups": {"joined": len(rows["in"]), "bench": len(rows["sub"]), "no_thanks": len(rows["out"]), "no_answer": len(rows["none"]), "rows": rows},
            "parts": [_part_view(reg, rs, p, sheet, viewer, board) for p in parts],
            "activity": [{"when": r["when"], "line": r["text"]} for r in activity],
            "boxes": {"lock": cq.lock_box(reg, first, rs) if state == "open" else None, "cancel": cq.cancel_box(reg, first, rs) if live else None,
                      "reschedule": cq.reschedule_pick_box(reg, rs, first) if can_move else None},
            "actions": {"reschedule": can_move, "edit_lineup": state == "open" and first.state == "open",
                        "lock": state == "open" and first.state == "open", "cancel": live, "open_now": False},
            "switches": rc.event_switches(reg, rs, first, viewer=viewer) if live else {"short": [], "rows": [], "revs": {}},
            "lineup": lineup, "start_iso": first.start.isoformat(), "timing": timing, "people": people, "sheet_url": url,
            # Reschedule's picker: the Day's lower bound is the GUILD's date; Locks offers the event's own lock among the choices
            "today": reg.now_local().date().isoformat(),
            "locks": lock_choices(float(timing["lock_hours_before"])) if can_move and timing.get("lock_hours_before") is not None else []}


# ---------------------------------------------------------------- a coming-up event

class ComingGone(RegistryError):
    """The night is not a coming-up one any more: `key` = the event that holds it now (None: off the schedule)."""

    def __init__(self, sentence: str, key: str | None = None):
        super().__init__(sentence)
        self.key = key


def coming_json(reg: Registry, rs: "rc.RaidStore", sid: str, occurrence: str, now: datetime) -> dict:
    """ComingView: {title, chip, schedule {id, name}, details [Lineup, Comes from, Priority, Confirmation, Sheet opens,
    Opening by hand, Locks], can_open, why (rc.open_row_refusal's sentence | None), open {start_iso, schedule,
    occurrence, raid (the first raid: the route's {rid})} (what POST /api/raid/{rid}/open carries back), box (consequences.open_now_box | None)}. ComingGone when
    the night has an event now (its key) or is not one of coming_nights any more."""
    find = rc.occurrence_index(reg, rs)
    hit = find(sid, occurrence)
    if hit is not None:
        raise ComingGone(COMING_OPENED, rc.first_part(rs, hit).key)
    t = next((t for _s, t, occ in coming_nights(reg, rs, now, find, sid) if occ == occurrence), None)
    if t is None:
        raise ComingGone(COMING_GONE)
    entry = reg.schedule_entry(sid)
    raid = entry["raid"]
    sd = reg.schedule_def(raid, sid)
    lineup = entry.get("lineup") or rc.schedule_lineup(raid, entry)
    opens, lock_at = rc.sheet_times(t, sd)
    name = entry.get("name") or reg.series_label(sid)
    why = rc.open_row_refusal(reg, rs, raid, t, sid, opens, now)
    details = [_fact("Lineup", rc.lineup_words(reg, lineup)), _fact("Comes from", name, f"/schedules/{sid}"),
               _fact("Priority", rc.priority_words(reg, sd)), _fact("Confirmation", rc.confirmation_words(bool(sd.get("confirm", True)))),
               _fact("Sheet opens", reg.local12(opens)),
               _fact("Opening by hand", PAGE_WORDS["from"].format(when=reg.local12(t - timedelta(days=rc.OPEN_HORIZON_DAYS)))),
               _fact("Locks", reg.local12(lock_at))]
    box = None if why else cq.open_now_box(reg, t, opens, lock_at, lineup=lineup, channel=bool(reg.config.signup_channel_id))
    return {"title": reg.local12(t), "chip": PAGE_WORDS["no_sheet_yet"], "schedule": {"id": sid, "name": name}, "details": details,
            "can_open": why is None, "why": why, "open": {"start_iso": t.isoformat(), "schedule": sid, "occurrence": occurrence, "raid": raid},
            "box": box}


# ---------------------------------------------------------------- New event (phase 4, Task 7)

NEW_EVENT_LOCKS = (2, 24)  # the New event page's Locks choice for a one-off: hours before the start
LOCK_CHOICE_REFUSAL = "Pick when the sheet locks from the list."
SCHEDULED_NIGHT = "That time is an event of {name}, so it opens with that schedule's lineup and timing."


def lock_words(hours: float) -> str:
    """'2 hours before', '1 hour before', '1.5 hours before' — a Locks choice in the words the page prints."""
    h = int(hours) if float(hours).is_integer() else float(hours)
    return f"{h} hour before" if h == 1 else f"{h} hours before"


def lock_choices(own: float | None = None) -> list[dict]:
    """[{hours, words}]: the Locks select ('2 hours before', '24 hours before'), in the words the page prints; `own` =
    an event's own lock (the Events page's Reschedule), added in its place when it is not one of them."""
    hours = sorted({*NEW_EVENT_LOCKS, *([own] if own is not None else [])})
    return [{"hours": int(h) if float(h).is_integer() else h, "words": lock_words(h)} for h in hours]


def new_event_options(reg: Registry) -> dict:
    """What the New event page picks from (GET /api/event-new): {today (the GUILD's date, the Day picker's lower bound —
    never the browser's), raids [{id, name, size, hours}] (the palette), lineups [{id, name, lineup [{raid, rosters,
    starts}]}] (Start from › Copy a lineup: each active schedule — never a pickup template — whose raids are all in the game, starts in minutes after
    the first — Registry.lineup_offsets), locks (lock_choices), lineup_max (registry.LINEUP_MAX: the builder holds no copy), lineup_full (LINEUP_MAX_WORDS: the builder's
    line at a full lineup)}."""
    raids = [{"id": rid, "name": reg.raid_def(rid).get("name", rid), "size": int(reg.raid_def(rid).get("size") or 0),
              "hours": float(reg.raid_def(rid).get("duration_hours") or 0)} for rid in reg.profile.raids]
    lineups = []
    for s in reg.all_schedules():
        if not s.get("active", True) or s.get("kind") == "pickup":
            continue  # a pickup template's lineup without its timing would open something else than the template
        lineup = [{"raid": str(e.get("raid") or ""), "rosters": int(e.get("rosters") or 1), **({"starts": e["starts"]} if e.get("starts") is not None else {})}
                  for e in (s.get("lineup") or rc.schedule_lineup(s["raid"], s))]
        if not lineup or any(e["raid"] not in reg.profile.raids for e in lineup):
            continue  # a raid the game no longer has: nothing to copy (the Schedules page says so)
        try:
            offsets = reg.lineup_offsets(lineup)
        except RegistryError:
            continue
        lineups.append({"id": s["id"], "name": s.get("name") or s["id"],
                        "lineup": [{"raid": e["raid"], "rosters": e["rosters"], "starts": off} for e, off in zip(lineup, offsets)]})
    return {"today": reg.now_local().date().isoformat(), "raids": raids, "lineups": lineups, "locks": lock_choices(), "lineup_max": LINEUP_MAX,
            "lineup_full": LINEUP_MAX_WORDS}


def seats_words(reg: Registry, lineup: list[dict]) -> str:
    """'20, then 10 + 10' — each raid's seats, a roster each, in lineup order."""
    def one(e: dict) -> str:
        size = int(reg.raid_def(e["raid"]).get("size") or 0) if e.get("raid") in reg.profile.raids else 0
        return " + ".join([str(size)] * int(e.get("rosters") or 1))
    return ", then ".join(one(e) for e in lineup)


def new_event_view(reg: Registry, rs: "rc.RaidStore", rid: str, p: dict, first_rosters: int) -> dict:
    """The New event page's "What this makes" from rc.open_preview's answer `p` (nothing written): {facts [One signup
    sheet, Comes from (a schedule's night), Seats, Priority, Confirmation], lineup [{raid, name, rosters, starts,
    time_words, seats}] (each step as the open would make it — a schedule's night: ITS lineup), notes (the DST line, the
    schedule's night, a raid that first opens later, no signup channel — sentences), refusal_box (a closed run at that
    time, an event already open then, a past time — else None)}."""
    scheduled = p.get("kind") == "scheduled" or (p.get("schedule") not in (None, rc.ADHOC_SCHEDULE))
    sd = reg.schedule_def(rid, p.get("schedule") if scheduled else None)
    start = datetime.fromisoformat(p["start"])
    live = rc.find_run(reg, rs, rid, start, p.get("schedule")) if p.get("exists") else None
    if live is not None:
        first_n = int((reg.config.team(live.team) or {}).get("rosters") or 1)
    else:
        first_n = int(sd.get("rosters") or 1) if scheduled else int(first_rosters or 1)
    steps = [{"raid": rid, "rosters": first_n, "starts": 0}] + [{"raid": e["raid"], "rosters": int(e.get("rosters") or 1), "starts": int(e.get("starts") or 0)}
                                                                  for e in p.get("later") or []]
    times = [reg.local12(start, "%I:%M %p")] + [e.get("time") or "" for e in p.get("later") or []]
    lineup = [{**e, "name": reg.raid_def(e["raid"]).get("name", e["raid"]), "time_words": t,
               "seats": int(reg.raid_def(e["raid"]).get("size") or 0)} for e, t in zip(steps, times)]
    facts = [_fact("One signup sheet", rc.lineup_words(reg, steps))]
    if scheduled:
        name = p.get("schedule_name") or p.get("schedule")
        facts.append(_fact("Comes from", name, f"/schedules/{p['schedule']}"))
    priority = rc.priority_words(reg, {"kind": (sd.get("schedule") or {}).get("kind"), "weights": sd.get("weights")})  # a one-off: first to join
    facts += [_fact("Seats", seats_words(reg, steps)), _fact("Priority", priority),
              _fact("Confirmation", rc.confirmation_words(bool(sd.get("confirm", True))))]  # what ensure_run freezes on the run
    notes = [p.get("when_note") or "", SCHEDULED_NIGHT.format(name=p.get("schedule_name") or "its schedule") if scheduled else "",
             p.get("before_open") or "", "" if p.get("channel") else (p.get("posts") or "")]
    refusal = None
    if p.get("refusal"):
        refusal = cq.refusal_box_for(p["refusal"])
    elif live is not None:
        refusal = cq.refusal_box_for(cq.already_open_sentence(reg, live))
    elif p.get("past"):
        refusal = cq.refusal_box_for(cq.new_event_past_sentence(reg, start))
    return {"facts": facts, "lineup": lineup, "notes": [n for n in notes if n], "refusal_box": refusal}
