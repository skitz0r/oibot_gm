"""What an officer's verb does to people, in one sentence, from code (dual interfaces §2.3).

Every surface that confirms a run verb — the Discord `decide` screen (wizard.Wizard.decide), the site's `useConfirm`
(SheetCard reads `ev_json.consequences`), the plain-text diff and an MCP preview — prints the SAME words, and none of
them composes the sentence itself: the browser never promised a DM the bot would not send (the old SheetCard lock text
said "everyone rostered gets a confirmation DM" on ad hoc runs, where nobody is asked).

`consequence(verb, reg, ev)` → the lines for lock / cancel / clear / release, the board's moves (move / roster / swap /
substitute — design §5.56 phase 3: the browser used to promise "{name} gets a confirmation DM" on ad hoc runs and "the
bench is asked" where a substitute takes the seat), remind and end; `regular_remove(reg, m, key)` for a regular taken off
a schedule. `consequences_json(reg, ev)` → the strings `ev_json` carries. Numbers come from the run as it stands now (who
joined, who has DMs off, the layout), so a preview drawn after the sheet moved says so by its numbers, not by a stale
cached line. The people verbs (design §5.58) live here too: `people_consequence(reg, rs, m, verb, by_id=…)` → one
generator per verb (retire, delete, main, spec, flex, rank, confirm, dm, back, save), for the Members and Me pages and
the Discord character, confirm and I'm back screens alike. A board sentence is about one member and a group: the server writes it with NAME_TOKEN / OTHER_TOKEN /
TO_TOKEN where those go and the board only puts the names in (it never composes a word of it)."""
from __future__ import annotations

from datetime import timedelta

from . import raidcycle as rc
from .registry import RANKS, Registry, RegistryError

VERBS = ("lock", "cancel", "clear", "release", "move", "roster", "swap", "substitute", "remind", "end")
BOARD_VERBS = ("move", "roster", "swap", "substitute")  # the board's moves: sentences about {name} / {other} / {to}
NAME_TOKEN = "{name}"
"""The placeholder in `consequences_json["release_named"]` and the board verbs: the board names the member it is about,
and the only thing the browser does to the sentence is put that name where the token is (it never composes the sentence)."""
OTHER_TOKEN = "{other}"  # the member already in the seat (swap: the one trading places; substitute: the one whose seat it was)
TO_TOKEN = "{to}"  # the group the move goes to, as the board names it ("Group 2")


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- the confirmation box (design §7b, D18)
# Every press that asks first on the site prints ONE shape, built only here: a title naming the action and its object,
# at most three sentences under the Confirm rule, the verb (None = a refusal: only Back), red only when it cannot be
# undone, the edit's "label: old → new" lines, and a fingerprint the action route re-checks (`seen` → 409 with the
# fresh box when the facts it printed changed while it was open).

def box(title: str, sentences: list[str], verb: str | None, *, danger: bool = False, diff=(), rev: str = "") -> dict:
    """{title, sentences, verb, danger, diff, fp}. `fp` = sha1 of the title, the diff lines and the sentences joined by
    newlines (first 12 hex) — and of `rev` when given: the fingerprint of what the write can clobber that the sentences
    don't print (rc.event_rev for a run, config_rev('schedule:<id>') for a schedule, config_rev('raid_all:<id>'))."""
    import hashlib

    sentences, diff = [str(s) for s in sentences if s], [str(x) for x in diff]
    blob = "\n".join([title, *diff, *sentences] + ([rev] if rev else []))
    return {"title": title, "sentences": sentences, "verb": verb, "danger": bool(danger), "diff": diff,
            "fp": hashlib.sha1(blob.encode()).hexdigest()[:12]}


def refusal_box(title: str, sentence: str) -> dict:
    """A refused action: the same box with one sentence and only Back."""
    return box(title, [sentence], None)


REFUSAL_TITLES = {"past": "That time has passed", "clash": "That time is taken", "closed": "This event is over",
                  "locked": "The lineup is fixed", "other": "This can't go ahead"}


def refusal_kind(sentence) -> str:
    """Which REFUSAL_TITLES a verb's refusal answers to: the kind a built sentence carries (rc.Refusal — a past start,
    a clash), else the refusal constant it equals; anything else is 'other'. Never a match on words inside a sentence."""
    kind = getattr(sentence, "kind", None)
    if kind in REFUSAL_TITLES:
        return kind
    s = str(sentence or "")
    if s in (rc.MOVE_CLOSED, rc.LINEUP_CLOSED, *(_closed_sentence(st) for st in ("done", "cancelled"))):
        return "closed"
    if s == rc.LINEUP_LOCKED:
        return "locked"
    return "other"


def refusal_box_for(sentence: str) -> dict:
    """The box a route answers a refusal with: the title its kind gives, the verb's own sentence."""
    return refusal_box(REFUSAL_TITLES[refusal_kind(sentence)], str(sentence))


PAST_TAIL = "{new} is in the past, so the event stays at {old}."


def move_past_sentence(reg: Registry, new, old) -> str:
    """Reschedule into the past, refused: 'Fri 20 Nov 7:00 PM is in the past, so the event stays at Thu 10 Dec 6:30 PM.'
    (an rc.Refusal of kind 'past')."""
    return rc.Refusal(PAST_TAIL.format(new=reg.local12(new), old=reg.local12(old)), "past")


LEAVE_TEMPLATE = {"one": "Your one edit to {name} is not saved yet.", "many": "Your {n} edits to {name} are not saved yet.",
                  # an edit mode that doesn't say what it holds (the older pages' useDirty without a count)
                  "page": "Your edits on this page are not saved yet."}


def leave_unsaved_sentence(n: int, name: str) -> str:
    """Leaving a page with unsaved edits (the site fills LEAVE_TEMPLATE itself; this is the same words in code); no
    name = an edit mode that doesn't say what it holds."""
    if not name:
        return LEAVE_TEMPLATE["page"]
    return LEAVE_TEMPLATE["one"].format(name=name) if n == 1 else LEAVE_TEMPLATE["many"].format(n=n, name=name)


def leave_box(n: int, name: str) -> dict:
    """Box 20: Keep editing in place of Back, Discard edits red."""
    from .constants import CONFIRM_WORDS

    return box(CONFIRM_WORDS["leave_title"], [leave_unsaved_sentence(n, name)], CONFIRM_WORDS["discard"], danger=True)


_COUNT = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def count_words(n: int) -> str:
    """one–ten spelled, then digits."""
    return _COUNT[n] if 0 <= n <= 10 else str(n)


def event_title(reg: Registry, ev: rc.RaidEvent) -> str:
    """An event as a box title names it: its first raid's start in local12 words ('Thu 10 Dec 6:30 PM'); any raid of
    the event stands for its first."""
    first_start = ev.start - timedelta(minutes=int(ev.starts or 0)) if rc.is_follow(ev) else ev.start
    return reg.local12(first_start)


def _raid(reg: Registry, rid: str | None) -> str:
    return reg.raid_def(rid).get("name", rid) if rid in reg.profile.raids else str(rid or "")


def consequence(verb: str, reg: Registry, ev: rc.RaidEvent, *, name: str | None = None, other: str | None = None,
                to: str | None = None, rs=None) -> list[str]:
    """The sentence(s) the confirm screen shows before `verb` on `ev`. `name`: the member a `release` or a board move
    is about; `other`: the member already in the seat (swap / substitute); `to`: the group ("Group 2"). Without them the
    sentence stays general. `rs`: the run store, so a cancel names only the raids of the run still live. Unknown verbs → []."""
    if verb in BOARD_VERBS:
        return _board(verb, reg, ev, name or "They", other or "the other", to or "the group")
    if verb == "remind":
        return _remind(reg, ev)
    if verb == "end":
        return _end(reg, ev)
    if verb == "lock":
        return _lock(reg, ev)
    if verb == "cancel":
        return _cancel(reg, ev, rs)
    if verb == "clear":
        return _clear(reg, ev)
    if verb == "release":
        return _release(reg, ev, name)
    return []


def consequences_json(reg: Registry, ev: rc.RaidEvent, rs=None) -> dict[str, str]:
    """`ev_json.consequences`: every verb in VERBS as one string (the lines joined by a space) — the board verbs with
    NAME_TOKEN / OTHER_TOKEN / TO_TOKEN where the names go — plus `release_named`: the release sentence about one member
    with NAME_TOKEN where the name goes (the board fills it in for the member being dragged out; before lock it is the
    same no-name sentence)."""
    out = {v: " ".join(consequence(v, reg, ev, name=NAME_TOKEN, other=OTHER_TOKEN, to=TO_TOKEN) if v in BOARD_VERBS else consequence(v, reg, ev, rs=rs)) for v in VERBS}
    out["release_named"] = " ".join(consequence("release", reg, ev, name=NAME_TOKEN))
    return out


def _dms_off(reg: Registry, ids) -> int:
    return sum(1 for uid in ids if (m := reg.members.get(int(uid))) is not None and m.dm_opt_out)


CONFIRM_DM = "the Confirm or Can't make it DM"
"""The confirmation card by its two buttons, in every confirm sentence (the Confirm rule, design §5.57)."""


def _closed_sentence(state: str) -> str:
    return f"This run is already {rc.RUN_STATE_WORDS[state]}."


def _closed(ev: rc.RaidEvent) -> list[str] | None:
    """A run already finished or cancelled: every verb's confirm says so, in one sentence."""
    if ev.state in ("done", "cancelled"):
        return [_closed_sentence(ev.state)]
    return None


def _lock(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    from .raid_views import run_times

    if ev.state != "open":
        return _closed(ev) or ["This run is already locked."]
    off = set(rc.lock_forces(reg, None, ev).kept_off)  # THE solve's kept-off list: the count never promises them a DM
    joined = [s for s in ev.by_status("in") if s.display_name not in off]
    seats = rc.run_size(reg, ev) * max(1, rc.run_rosters(reg, ev))
    n = min(len(joined), seats)
    lineup = rc.event_lineup(reg, ev)
    head = f"The board becomes the roster for {rc.lineup_roster_words(reg, lineup)}."
    lines = [head if _words(head) <= 20 else f"The board becomes the roster for all {len(lineup)} raids."]
    test = rc.is_test_run(reg, ev)
    if not rc.run_confirms(reg, ev):  # a one-off (D5): Join is the commitment
        lines.append("Nobody is asked to confirm.")
        lines.append("On a test run the puppets' messages come to the tester." if test else "You can still move people after the lock.")
        return lines
    if n == 0:
        lines.append("Nobody has joined yet, so nobody is asked to confirm.")
        return lines
    off = _dms_off(reg, (s.discord_id for s in joined))
    _, _, confirm = run_times(reg, ev, rc.run_team(reg, ev))
    who = "The one person rostered gets" if n == 1 else f"The {n} rostered get"
    if test:
        lines.append(f"{who} {CONFIRM_DM}, and the puppets' DMs come to the tester.")
    elif off and off >= n:
        lines.append(("The one person rostered sees" if n == 1 else f"The {n} rostered see") + " a Confirm or Can't make it ask on the site, with DMs off.")
    else:
        k = n - off
        lines.append(f"{who} {CONFIRM_DM}." if not off else
                     f"{k} of the {n} rostered {'gets' if k == 1 else 'get'} {CONFIRM_DM} and {off} {'sees' if off == 1 else 'see'} it on the site.")
    lines.append(f"They have until {reg.local12(confirm)} to answer.")
    return lines


def lock_refusal(reg: Registry, ev: rc.RaidEvent) -> str | None:
    """Why the lock can't go ahead before it solves (None: it can): everyone who joined is kept off the roster (an out
    pin, a drag onto Not placed, the comp policy) — the lock would roster nobody. A run nobody joined still locks."""
    joined = {s.display_name for s in ev.by_status("in")}
    if ev.state == "open" and joined and joined <= set(rc.lock_forces(reg, None, ev).kept_off):
        return rc.SOLVER_WORDS["all_off"]
    return None


def lock_box(reg: Registry, ev: rc.RaidEvent, rs=None) -> dict:
    """Boxes 1 and 2: Lock an event (`rs` adds the event's fingerprint, so a box whose run changed is refused). An event
    already locked, finished or cancelled answers with a refusal box; so does one whose every joiner is kept off."""
    if ev.state != "open":
        return refusal_box_for(_lock(reg, ev)[0])
    if (why := lock_refusal(reg, ev)) is not None:
        return refusal_box_for(why)
    return box(f"Lock {event_title(reg, ev)}?", _lock(reg, ev), "Lock", rev=rc.event_rev(reg, rs, ev) if rs is not None else "")


def _cancel(reg: Registry, ev: rc.RaidEvent, rs=None) -> list[str]:
    """Cancel an event (a cancel on any raid of it cancels every live raid of it — RaidMixin.cancel_run): how many raids
    go, who sees it, and that it can't be reopened. Nobody is DMed (the DM rule): the sheet is edited to its final state
    and every open ask card says Cancelled. The answers live on the first raid, so who sees it is read there."""
    if (closed := _closed(ev)):
        return closed
    fx = rc.cancel_effects(reg, ev, rs)
    gone = "A cancelled event cannot be reopened."
    if fx.get("first_closed"):  # a later raid once the first raid has finished: the raids still to play go, no sheet changes
        names = [n for n in fx["later"] if n] or [_raid(reg, ev.instance)]
        return [f"The {rc.join_words(names)} raid{'s of this event are' if len(names) > 1 else ' of this event is'} cancelled.",
                "Nobody gets a DM, but every ask card says Cancelled.", gone]
    k = 1 + len(fx["later"])
    head = ("This event is cancelled." if k == 1 else "Both raids of this event are cancelled." if k == 2
            else f"All {k} raids of this event are cancelled.")
    first = rc.first_part(rs, ev) if rs is not None else ev
    ffx = fx if first is ev else rc.cancel_effects(reg, first, rs)
    if not ffx["locked"]:
        n, asks = len(first.by_status("in")), len(ffx["fill_asks"])
        who = ("Nobody has joined" if n == 0 else "The one who joined sees it on the sheet" if n == 1
               else f"The {n} who joined see it on the sheet")
        tail = (f", and {asks} open ask card{'s say' if asks > 1 else ' says'} Cancelled." if asks else ", and nobody gets a DM.")
        return [head, who + tail, gone]
    r = len(ffx["rostered"])
    where = "on their card" if rc.run_confirms(reg, first) else "on the sheet"
    who = ("Nobody is rostered" if r == 0 else f"The one person rostered sees it {where}" if r == 1
           else f"The {r} rostered see it {where.replace('their card', 'their cards')}")
    asks = len([n for n in ffx["fill_asks"] if n not in set(ffx["rostered"])])  # fill asks out to people not rostered
    tail = (f", and {asks} open ask card{'s say' if asks > 1 else ' says'} Cancelled." if asks else ", and nobody gets a DM.")
    return [head, who + tail, gone]


def cancel_box(reg: Registry, ev: rc.RaidEvent, rs=None) -> dict:
    """Box 5: Cancel an event — red, it cannot be undone. A finished or cancelled event answers with a refusal box."""
    if (closed := _closed(ev)):
        return refusal_box_for(closed[0])
    return box(f"Cancel {event_title(reg, ev)}?", _cancel(reg, ev, rs), "Cancel event", danger=True,
               rev=rc.event_rev(reg, rs, ev) if rs is not None else "")


def before_first_open_sentence(reg: Registry, lineup: list[dict], start, only: set[str] | None = None) -> str | None:
    """C9: the open confirm's line when a raid of the run's `lineup` (the whole lineup, the first raid first) would begin
    before that raid first opens — the first such raid, in words; None when every raid is open by its start. `only`:
    the raids to look at (a lineup edit's ADDED raids), else every raid."""
    try:
        offsets = reg.lineup_offsets(lineup)
    except RegistryError:
        return None
    for e, at in zip(lineup, offsets):
        if only is not None and e["raid"] not in only:
            continue
        fo = reg.first_open(e["raid"])
        if fo is not None and fo > rc.after_minutes(start, at):
            return f"{reg.raid_def(e['raid']).get('name', e['raid'])} first opens {reg.local12(fo)}, after this run starts."
    return None


TIMING_NAMES = {"cutoff_hard_hours": "lock time", "cutoff_soft_hours": "reminder time", "confirm_hours": "confirm-by",
                "reminders": "reminder", "autofill": "automatic asks", "fill_ask_hours": "fill ask wait"}
TIMING_ORDER_OF = tuple(TIMING_NAMES)


def _words(text: str) -> int:
    return len(text.split())


def _timing_phrases(reg: Registry, start, changed: dict, team: dict) -> list[str]:
    """One phrase per run setting that changes, in the manual's words, with the new time where it is a time. The
    reminder is one thing: turned off, its time isn't named; turned on, it says when it goes out."""
    out = []
    for k in TIMING_ORDER_OF:
        if k not in changed:
            continue
        v = changed[k]
        if k == "cutoff_hard_hours":
            out.append(f"the sheet locks {reg.local12(start - timedelta(hours=float(v)))}")
        elif k == "cutoff_soft_hours":
            if changed.get("reminders") == rc.OFF:
                continue  # no reminder: its time says nothing
            out.append(f"the reminder goes out {reg.local12(start - timedelta(hours=float(v)))}")
        elif k == "confirm_hours":
            out.append(f"confirm-by is {reg.local12(start - timedelta(hours=float(v)))}")
        elif k == "reminders":
            if v == rc.OFF:
                out.append("there is no reminder")
            elif "cutoff_soft_hours" not in changed:  # turned on: when it goes out (a new time was said above)
                soft = float(rc.team_setting(team, "cutoff_soft_hours"))
                out.append(f"the reminder goes out {reg.local12(start - timedelta(hours=soft))}")
        elif k == "autofill":
            out.append("automatic asks are on" if v else "automatic asks are off")
        else:
            out.append(f"a fill ask waits {rc.hours_words(float(v))}")
    return out


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _what_changes(reg: Registry, name: str, first: rc.RaidEvent, start, moves: bool, changed: dict) -> str:
    """The confirm's first sentence, built from what actually changes: the move, this run's own settings, or both."""
    names = [TIMING_NAMES[k] for k in TIMING_ORDER_OF if k in changed
             and not (k == "cutoff_soft_hours" and "reminders" in changed)]  # the reminder's time and switch: "reminder"
    if moves:
        head = f"The event moves to {reg.local12(start)}"
        if not names:
            return head + "."
        full = f"{head} with its own {_and(names)}."
        return full if _words(full) <= 20 else f"{head} with its own sheet timing."
    detail = "For this run, " + _and(_timing_phrases(reg, first.start, changed, rc.run_team(reg, first))) + "."
    if _words(detail) <= 20:
        return detail
    listed = f"This run takes its own {_and(names)}."
    return listed if _words(listed) <= 20 else "This run takes its own sheet timing."


def reschedule_consequence(reg: Registry, rs, ev: rc.RaidEvent, start, timing: dict | None) -> list[str]:
    """The confirm of moving a run (any raid of it stands for its first) to `start` and / or giving it its own `timing`
    (rc.reschedule's preview — nothing written), at most three sentences: what changes (the move, the run's own
    settings that actually change, or both), who is told, then the first of: the run locking at once, the schedule's
    not-yet-opened run of that start being this one, people also on another run that night, away answers worked out
    again, or that it can move again. A refusal is rc.reschedule_refusal's own sentence, not this."""
    first = rc.first_part(rs, ev)
    mv = rc.reschedule(reg, rs, first, start=start, timing=timing, by="preview", save=False)
    name = reg.raid_def(first.instance).get("name", first.instance)
    moves = start is not None and start != first.start
    out = [_what_changes(reg, name, first, start, moves, mv.timing)]
    n = len(mv.told)
    if first.state == "locked":
        out.append("The roster stands." if n == 0 else "The roster stands, and the one who answered gets a notice." if n == 1
                   else f"The roster stands, and the {n} who answered each get a notice.")
    elif not moves:  # the start stays: nobody's night changes, so nobody is told
        out.append("Answers stand.")
    else:
        out.append("Nobody has answered yet." if n == 0 else "The one answer stands, and that person gets a notice." if n == 1
                   else f"All {n} answers stand, and each person gets a notice.")
    absorbed = rc.absorbed_schedule_run(reg, rs, first, start) if moves else None
    if mv.locks_now:
        out.append("It locks within a minute, because its lock time has passed.")
    elif absorbed:
        out.append(f"That night's {absorbed} run is this one.")
    elif not moves:  # the start stays: nobody's night changes, and the timing is fixed at the lock
        out.append("You can change it again until the roster locks.")
    elif mv.conflicts:
        uid, key = next(iter(mv.conflicts.items()))
        sg = first.signups.get(str(uid))
        who = sg.display_name if sg else "Someone"
        other = rs.events.get(key)
        title = f"{reg.raid_def(other.instance).get('name', other.instance)} {reg.local12(other.start)}" if other else "another run"
        k = len(mv.conflicts) - 1
        out.append(f"{who} is also on {title} that night." if k == 0 else f"{who} and {_n(k, 'other')} are also on {title} that night.")
    elif mv.absence_cleared or mv.absence_written:
        out.append("Away answers are worked out again for the new day.")
    else:
        out.append("The sheet in Discord shows the new time.")
    return out


def reschedule_pick_box(reg: Registry, rs, ev: rc.RaidEvent) -> dict:
    """Box 3 before anything is picked (the Events page's Reschedule opens on it): the title naming the event and one
    sentence asking for the new day and time, only Back — the picker's first change brings reschedule_box."""
    return box(f"Move {event_title(reg, rc.first_part(rs, ev))}?", [rc.MOVE_WHEN], None)


def reschedule_box(reg: Registry, rs, ev: rc.RaidEvent, start, timing: dict | None = None) -> dict:
    """Box 3: Move an event (reschedule_consequence's sentences); a refusal is refusal_box_for(rc.reschedule_refusal)."""
    why = rc.reschedule_refusal(reg, rs, ev, start, timing)
    if why:
        return refusal_box_for(why)
    return box(f"Move {event_title(reg, rc.first_part(rs, ev))}?", reschedule_consequence(reg, rs, ev, start, timing), "Move",
               rev=rc.event_rev(reg, rs, ev))


def _clock_on(reg: Registry, day_of, t) -> str:
    """A later raid's start as a person reads it: '9:30 PM' on the first raid's day, the whole local12 on another day."""
    same = t.astimezone(reg.tz).date() == day_of.astimezone(reg.tz).date()
    return reg.local12(t, "%I:%M %p") if same else reg.local12(t)


def at_on(words: str) -> str:
    """'at 9:30 PM' for a time alone, 'on Wed 9 Dec 1:30 AM' for a day and a time (_clock_on's words)."""
    return f"at {words}" if len(words.split()) <= 2 else f"on {words}"


def _moved_at(reg: Registry, rs, first: rc.RaidEvent, ch, *, clock_only: bool = False) -> dict[str, str]:
    """raid id → the new start of each KEPT raid whose start changes, as a person reads it (_clock_on; `clock_only`:
    the clock alone even on another day), in lineup order."""
    out = {}
    for k, at in ch.moved.items():
        t = rc.after_minutes(first.start, at)
        out[rs.events[k].instance] = reg.local12(t, "%I:%M %p") if clock_only else _clock_on(reg, first.start, t)
    return out


def lineup_words(reg: Registry, rs, first: rc.RaidEvent, ch, *, times: bool = True, clock_only: bool = False) -> str:
    """THE new lineup in words — 'Hyjal Summit, then Onyxia's Lair at 9:30 PM, then Barrow Deeps ×2' with each kept raid's
    new start when `times` (on another day 'on Wed 9 Dec 1:30 AM'; `clock_only`: 'at 1:30 AM'), or 'Hyjal Summit alone,
    with 2 rosters' — the confirm (lineup_consequence) and RaidMixin.edit_lineup's done and thread lines alike."""
    name0 = reg.raid_def(first.instance).get("name", first.instance)
    later = ch.lineup[1:]
    if not later:
        n0 = int(ch.lineup[0]["rosters"])
        return f"{name0} alone, with {n0} rosters" if n0 > 1 else f"{name0} alone"
    when = _moved_at(reg, rs, first, ch, clock_only=clock_only) if times else {}
    return f"{reg.follow_words(ch.lineup[0])}, then " + ", then ".join(
        reg.follow_words(e) + (f" {at_on(when[e['raid']])}" if e["raid"] in when else "") for e in later)


def _one_or_n(n: int, one: str, many: str | None = None) -> str:
    """'board place' for one (after 'its' / 'their' a count of one, alone in its sentence, reads as the thing alone),
    '3 board places'."""
    return one if n == 1 else f"{n} {many or one + 's'}"


def _rosters_words(n: int) -> str:
    return f"{count_words(n)} roster{'' if n == 1 else 's'}"


def _first_change(reg: Registry, old: list[dict], new: list[dict]) -> str:
    """The first thing an edited lineup changes, in one sentence (the diff lines list every change): a raid taken out,
    a raid's roster count, a raid added, the order, else a later raid's start."""
    new_by = {e["raid"]: e for e in new}
    old_by = {e["raid"]: e for e in old}
    for e in old:
        if e["raid"] not in new_by:
            return f"{_raid(reg, e['raid'])} leaves the lineup."
        a, b = int(e.get("rosters") or 1), int(new_by[e["raid"]].get("rosters") or 1)
        if a != b:
            return f"{_raid(reg, e['raid'])} goes from {_rosters_words(a)} to {count_words(b)}."
    for i, e in enumerate(new):
        if e["raid"] not in old_by:
            return f"{_raid(reg, e['raid'])} joins the lineup after {_raid(reg, new[i - 1]['raid'])}."
    if [e["raid"] for e in old] != [e["raid"] for e in new]:
        head = f"The lineup becomes {rc.lineup_words(reg, new)}."
        return head if _words(head) <= 20 else "The raids of the lineup change order."
    return "Some raids of the lineup start at new times."


def lineup_consequence(reg: Registry, rs, ev: rc.RaidEvent, lineup: list[dict]) -> list[str]:
    """The confirm of editing a run's lineup before the lock (rc.plan_lineup — nothing written): at most three
    sentences of twenty words or fewer, ranked so that a loss is NEVER dropped, in the Confirm rule's order:
    1. the first change (_first_change; the box's diff lines list every change);
    2. the losses — what a removed raid takes with it (its board places, pins, plans to leave early and open asks,
       counted apart, falling back to "…taken out with everything on its board") and the moves on a roster that goes;
       a removed raid's loss can stand for the change itself, and when they don't fit the change sentence goes;
    3. who is told: that every answer stands — and, when a kept raid's start changes, that each person who answered
       gets reschedule's `moved` notice;
    then, with room left: an ADDED raid starting before it first opens (C9: allowed, said); else the kept raids' new
    starts; else, for a schedule's run, that its schedule keeps its own lineup.
    A refusal is rc.lineup_refusal's own sentence, not this."""
    first = rc.first_part(rs, ev)
    ch = rc.plan_lineup(reg, rs, first, lineup)
    old = rc.run_lineup(reg, rs, first)
    n = len(rc.answered_ids(first))
    out = [_first_change(reg, old, ch.lineup)]
    if ch.told:
        out.append("Nobody has answered yet." if n == 0 else "The one answer stands, and that person gets a notice." if n == 1
                   else f"All {n} answers stand, and each person gets a notice.")
    else:
        out.append("Nobody has answered yet." if n == 0 else "The one answer stands." if n == 1 else f"All {n} answers stand.")
    # 3. what is taken out with a removed raid
    loss = None
    held = [rs.events[k] for k in ch.removed
            if ch.dropped_places.get(k) or ch.dropped_pins.get(k) or ch.dropped_leaving.get(k) or ch.dropped_asks.get(k)]
    if held:
        one = len(held) == 1
        its = "its" if one else "their"
        raids = rc.join_words([_raid(reg, p.instance) for p in held])
        counts = [(sum(ch.dropped_places.values()), "board place", None), (sum(ch.dropped_pins.values()), "pin", None),
                  (sum(ch.dropped_leaving.values()), "plan to leave early", "plans to leave early"),
                  (sum(ch.dropped_asks.values()), "open ask", None)]
        counts = [c for c in counts if c[0]]
        alone = len(counts) == 1  # "its open ask" for one thing alone; in a list every thing keeps its number
        items = _and([_one_or_n(k, one_w, many) if alone else _n(k, one_w, many) for k, one_w, many in counts])
        full = f"{raids} {'is' if one else 'are'} taken out with {its} {items}."
        short = f"Taking out {raids} drops {its} {items}."
        loss = (full if _words(full) <= 20 else short if _words(short) <= 20
                else f"{raids} {'is' if one else 'are'} taken out with everything on {its} board.")
    added = {e["raid"] for e in ch.added}
    early = before_first_open_sentence(reg, [ch.lineup[0], *ch.added], first.start, only=added) if added else None
    times = None
    if ch.moved:
        when = _moved_at(reg, rs, first, ch)
        said = _and([f"{_raid(reg, r)} {'now starts ' if i == 0 else ''}{at_on(w)}" for i, (r, w) in enumerate(when.items())]) + "."
        times = said if _words(said) <= 20 else "Some raids start at new times."
    # a roster that goes takes the board places on it (rc.roster_trim: apply_lineup drops them, the lock places the
    # people again) — a loss too
    def _was(k: str) -> int:
        p = rs.events.get(k)
        return int(((reg.config.team(p.team) if p is not None else None) or {}).get("rosters") or 1)
    gone_rosters = sum(_was(k) - n for k, n in ch.rosters.items() if k in ch.trimmed_places or k in ch.trimmed_pins)
    trim = None
    if ch.trimmed_places or ch.trimmed_pins:
        trim = "Moves made on the removed rosters are dropped." if gone_rosters > 1 else "Moves made on the removed roster are dropped."
    losses = [x for x in (loss, trim) if x]
    if loss and out[0] in {f"{_raid(reg, p.instance)} leaves the lineup." for p in held}:
        out[0] = loss  # the loss names the raid taken out: it says the change itself, with room for one more
        losses.remove(loss)
    # the Confirm rule's order: what changes (the change, then its losses) before who is told; when they don't all fit,
    # the change sentence goes — the losses say it — and who is told stays last
    change, told = out
    out = [change, *losses, told] if 1 + len(losses) + 1 <= 3 else [*losses, told]
    if len(out) < 3:
        extra = early or times or ("Its schedule keeps its own lineup." if first.schedule else None)
        if extra:
            out.append(extra)
    assert all(x in out for x in (loss, trim) if x), "a removed raid's or roster's losses are never dropped from the confirm"
    return out


def lineup_diff_lines(reg: Registry, rs, ev: rc.RaidEvent, lineup: list[dict]) -> list[str]:
    """An edited lineup's "label: old → new" lines (the Save lineup box): the order or the raids when they change, each
    kept raid's roster count, each kept later raid's start."""
    first = rc.first_part(rs, ev)
    ch = rc.plan_lineup(reg, rs, first, lineup)
    old = rc.run_lineup(reg, rs, first)
    out = []
    if [e["raid"] for e in old] != [e["raid"] for e in ch.lineup]:
        out.append(f"Lineup: {rc.lineup_words(reg, old)} → {rc.lineup_words(reg, ch.lineup)}")
    new_by = {e["raid"]: e for e in ch.lineup}
    for e in old:
        b = new_by.get(e["raid"])
        if b is None:
            continue
        x, y = int(e.get("rosters") or 1), int(b.get("rosters") or 1)
        if x != y:
            out.append(f"{_raid(reg, e['raid'])}: {_n(x, 'roster')} → {_n(y, 'roster')}")
        if e.get("starts") and int(b["starts"]) != int(e["starts"]):
            was, now_ = rc.after_minutes(first.start, int(e["starts"])), rc.after_minutes(first.start, int(b["starts"]))
            out.append(f"{_raid(reg, e['raid'])} starts: {_clock_on(reg, first.start, was)} → {_clock_on(reg, first.start, now_)}")
    return out


def lineup_box(reg: Registry, rs, ev: rc.RaidEvent, lineup: list[dict]) -> dict:
    """Box 4: Edit lineup's Save; a refusal is refusal_box_for(rc.lineup_refusal)."""
    why = rc.lineup_refusal(reg, rs, ev, lineup)
    if why:
        return refusal_box_for(why)
    return box("Save this lineup?", lineup_consequence(reg, rs, ev, lineup), "Save lineup",
               diff=lineup_diff_lines(reg, rs, ev, lineup), rev=rc.event_rev(reg, rs, ev))


def later_places_sentence(reg: Registry, held: list) -> str:
    """A first raid's Can't make it frees the member's seat on every later raid of the run they hold one on
    (rc.held_later): the one sentence that says so, naming them — the Me page's confirm (web/api_me.cant_consequence)
    and /raid out's form alike. '' when they hold none."""
    names = [reg.raid_def(p.instance).get("name", p.instance) for p in held]
    if not names:
        return ""
    return f"Your {rc.join_words(names)} place{'s go' if len(names) > 1 else ' goes'} too."


def leave_follow_consequence(reg: Registry, ev: rc.RaidEvent, seated: bool, lead_seated: bool) -> list[str]:
    """Leaving after the lead (design §5.62): the Me page's and /raid out's confirm on a follow-on.
    `seated`: rostered on the follow-on; `lead_seated`: rostered on the lead (a lead bench joiner may cover a follow-on
    gap, so one doesn't imply the other). Under the Confirm rule."""
    lead = (rc.lead_dict(reg, ev) or {}).get("instance")
    lead_name = reg.raid_def(lead).get("name", "the first raid")
    mine = reg.raid_def(ev.instance).get("name", ev.instance)
    first = f"You stay on the {lead_name} roster." if lead_seated else f"Your {lead_name} answer stands."
    if seated:
        return [first, f"Your {mine} place is freed.", rc.refill_sentence(reg, ev)]
    return [first, f"You are out of the {mine} follow-on."]


def _clear(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    if (closed := _closed(ev)):
        return closed
    if ev.state == "open":
        placed = sum(len(g) for g in (ev.layout or []))
        if not placed:
            return ["The board is already empty."]
        return [(f"The {placed} people on the board go back to {rc.BANK_WORDS['open']}." if placed > 1 else
                 f"The one person on the board goes back to {rc.BANK_WORDS['open']}."), "Nothing is sent before the lock."]
    rostered = len(ev.seated())
    if not rostered:
        return ["The roster is already empty."]
    where = "their Confirm card and their Me page" if rc.run_confirms(reg, ev) else "their Me page"
    return [f"All {rostered} seats are released." if rostered > 1 else "The one seat is released.",
            f"The {rostered} rostered see it on {where}." if rostered > 1 else f"Its holder sees it on {where}.",
            rc.refill_sentence(reg, ev, "them" if rostered > 1 else "it")]


def _seen(reg: Registry, ev: rc.RaidEvent) -> str:
    """Where a member whose seat is released sees it (the DM rule: never a DM): their Confirm card is edited when the run
    asks for confirmations (RaidMixin.seat_released), and the site's notice either way (rc.seat_gone)."""
    return "their card and their Me page" if rc.run_confirms(reg, ev) else "their Me page"


def _release(reg: Registry, ev: rc.RaidEvent, name: str | None) -> list[str]:
    if (closed := _closed(ev)):
        return closed
    if ev.state == "open":
        return [f"{name} comes off the board." if name else "The seat comes off the board.", "Nothing is sent before the lock."]
    if name:
        return [f"{name}'s seat is released.", f"They see it on {_seen(reg, ev)}.", rc.refill_sentence(reg, ev)]
    return ["The seat is released.", f"Whoever held it sees it on {_seen(reg, ev)}.", rc.refill_sentence(reg, ev)]


BEFORE_LOCK = "Nothing is sent before the lock."


def _board(verb: str, reg: Registry, ev: rc.RaidEvent, name: str, other: str, to: str) -> list[str]:
    """One move on the board (RaidMixin.board_move — a drag or Move to…). Before the lock the board is only the layout the
    lock will use: nobody is told. After it the board IS the roster (rc.apply_layout_locked): moving between groups is
    free, someone brought in from Not rostered is asked to confirm (confirm_one) and someone whose seat goes is released
    (free_seat → their card edited and a notice — never a DM)."""
    if (closed := _closed(ev)):
        return closed
    if ev.state == "open":
        return {"move": [f"{name} takes an open seat in {to}.", BEFORE_LOCK],
                "roster": [f"{name} takes an open seat in {to}.", BEFORE_LOCK],
                "swap": [f"{name} and {other} swap places.", BEFORE_LOCK],
                "substitute": [f"{name} takes {other}'s place in {to}.", f"{other} goes back to {rc.BANK_WORDS['open']}.", BEFORE_LOCK]}[verb]
    if verb == "move":
        return [f"{name} moves to {to} and stays rostered.", "Nobody is told."]
    if verb == "swap":
        return [f"{name} and {other} swap seats.", "Both see the change on the roster card.", "You can swap them back."]
    confirms = rc.run_confirms(reg, ev)
    if verb == "roster":
        if not confirms:
            return [f"{name} takes an open seat in {to}.", rc.no_confirm_sentence(reg, ev, "the seat is theirs now")]
        return [f"{name} takes an open seat in {to} and gets {CONFIRM_DM}.", "With DMs off it waits on their Me page.",
                "The seat is theirs once they say yes."]
    # substitute: the seat goes straight to the newcomer, so the bench is not asked for it
    if not confirms:
        return [f"{name} takes {other}'s seat in {to}.", "On an ad hoc run they aren't asked to confirm." if rc.run_kind(reg, ev) == "adhoc"
                else "On this run they aren't asked to confirm.", f"{other} sees the seat was released on {_seen(reg, ev)}."]
    return [f"{name} takes {other}'s seat in {to} and gets {CONFIRM_DM}.", "With DMs off it waits on their Me page.",
            f"{other} sees the seat was released on {_seen(reg, ev)}."]


def _remind(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    """Remind the unanswered (RaidMixin.remind): the Confirm / Can't card again, against the existing ask, for everyone a
    DM can reach; DMs-off asks already wait on the site. The refusal (rc.remind_refusal) is the sentence when it can't go."""
    why = rc.remind_refusal(reg, ev)
    if why:
        return [why]
    rows = rc.open_placement_rows(reg, ev)
    dm = sum(1 for m, a in rows if a.get("channel", "dm") == "dm" and not m.dm_opt_out)
    site = len(rows) - dm
    lines = ["The one rostered member who hasn't answered gets their Confirm or Can't make it DM again." if dm == 1 else
             f"The {dm} rostered who haven't answered get their Confirm or Can't make it DM again."]
    if site:
        lines.append("One more waits on the site with DMs off." if site == 1 else f"{site} more wait on the site with DMs off.")
    lines.append("Remind works once an hour.")
    return lines


def _end(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    """End the run (LootMixin.end_run; the loot council's part of the sentence is LootMixin.end_consequence's): the run
    is marked finished, its sheet and lock card close, and Reopen works for rc.REOPEN_GRACE_HOURS (rc.reopen_window)."""
    if (closed := _closed(ev)):
        return closed
    return ["The run is finished.", "The sheet and the lock card close and no more fill asks go out.", reopen_sentence()]


def reopen_sentence() -> str:
    """'Reopen works for an hour if this was a mistake.' — End's confirm and the loot council's End (rc.reopen_window)."""
    return f"Reopen works for {rc.reopen_window()} if this was a mistake."


def ended_line(reg: Registry, ev: rc.RaidEvent) -> str:
    """The line after End (the lock card's End, /raid end): the run in words and how long Reopen works."""
    return f"{rc.run_on(reg, ev)} is finished. If that was a mistake, Reopen on the closed card works for {rc.reopen_window()}."


def schedule_keeps_sentence(n: int) -> str | None:
    """C10: what an edit of a schedule's run times or week does to the `n` runs it already opened — they keep theirs.
    None when it has none open (nothing to say)."""
    if n <= 0:
        return None
    if n == 1:
        return "The open run keeps its old times."
    return f"The {n} open runs keep their old times."


def series_words(reg: Registry, key: str) -> str:
    """A schedule id as a sentence names it: '<raid> <schedule name>' ('Barrow Deeps Raid nights'); one no schedule
    answers to: 'a removed Barrow Deeps schedule' (Registry.series_label's words, never the key)."""
    sid = reg.config.schedule_aliases.get(str(key), str(key))
    try:
        s = reg.schedule_entry(sid)
    except RegistryError:
        label = reg.series_label(key)
        return label[:1].lower() + label[1:]
    return f"{reg.raid_def(s['raid']).get('name', s['raid'])} {s.get('name') or sid}"


def _schedule_name(reg: Registry, sid: str) -> str:
    try:
        e = reg.schedule_entry(sid)
    except RegistryError:
        return str(sid)
    return e.get("name") or e["id"]


def _store(reg: Registry, rs):
    """The run store a box reads: the caller's, else one read from disk ONCE for the whole box."""
    return rs if rs is not None else rc.RaidStore(reg.store, reg.key)


def _open_events(reg: Registry, rs, sid: str) -> list[rc.RaidEvent]:
    """The live events schedule `sid` opened, their first raids only, soonest first (test runs left out)."""
    out = []
    for ev in reg._live_runs(rs):
        if rc.is_follow(ev) or ev.test:
            continue
        t = reg.config.team(ev.team) or {}
        if str(t.get("schedule_id") or ev.schedule or "") == sid and not t.get("test"):
            out.append(ev)
    return sorted(out, key=lambda e: e.start)


def regular_remove(reg: Registry, m, key: str, *, rs=None) -> list[str]:
    """Taking `m` off the regulars of schedule `key` (Registry.regular_remove — the Schedules page's remove and the
    orphan clear, Discord's /roster remove, plain text, MCP): no DM and no notice; the lock stops weighting them as a
    regular (rc.seat_bonus) and their answer on an open event stays. A removed schedule's leftovers change nothing else."""
    name = m.display_name
    rs = _store(reg, rs)
    sid = reg.config.schedule_aliases.get(str(key), str(key))
    if key not in dict(reg.series("recurring")):
        return [f"{name} is no longer a regular of {series_words(reg, key)}.", "Nothing else changes."]
    out = [f"{name} is no longer a regular of {_schedule_name(reg, sid)}."]
    uid = str(m.discord_id)
    if any(uid in ev.signups and ev.signups[uid].source != "absence" for ev in _open_events(reg, rs, sid)):
        out.append("They keep their answer on the open event.")
    out.append("You can add them again.")
    return out


def regular_remove_box(reg: Registry, m, key: str, *, rs=None) -> dict:
    """Box 18: take a member off a schedule's regulars."""
    return box(f"Take {m.display_name} off the regulars?", regular_remove(reg, m, key, rs=rs), "Remove")


# Split strategy (the Events page's More › Split strategy…): what each strategy does to a raid's rosters, said of that
# raid, and the box's other words. The names are the Discord wizards' (flows_config.SPLIT_WORDS).
STRATEGY_DOES = {"balanced": "The rosters of {raid} are made equally strong.",
                 "first": "Roster 1 of {raid} gets the best players, and the next roster the rest.",
                 "rotation": "Whoever sat out or played the weaker roster last time moves up on {raid}."}
STRATEGY_PICK = "Pick how the rosters of {raid} are split."
STRATEGY_SAME = "That is already how {raid} is split."
STRATEGY_LOCKED = "The roster is locked, so its split can't change."
STRATEGY_AFTER = ["The board is placed again this way, and the lock uses it.", "You can change it until the lock."]


def strategy_name(strategy: str) -> str:
    from .flows_config import SPLIT_WORDS

    return SPLIT_WORDS.get(strategy, (strategy,))[0]


def strategy_choices() -> list[dict]:
    """[{id, label, words}]: the split strategies in the Discord wizards' words (flows_config.SPLIT_WORDS)."""
    from .flows_config import SPLIT_WORDS
    from .registry import SPLIT_POLICIES

    return [{"id": s, "label": SPLIT_WORDS[s][0], "words": SPLIT_WORDS[s][1]} for s in SPLIT_POLICIES]


def strategy_refusal(reg: Registry, ev: rc.RaidEvent) -> str | None:
    """Why a raid's split can't change: once its roster is locked (or the event is over), STRATEGY_LOCKED."""
    return STRATEGY_LOCKED if ev.state != "open" else None


def strategy_box(reg: Registry, ev: rc.RaidEvent, strategy: str | None) -> dict:
    """More › Split strategy… for one raid of an event: no pick yet (or the strategy it already has) → the pick box,
    Back only; locked → the refusal; else 'Split {raid} by {name}?' with what the strategy does, that the board is
    placed again and the lock uses it, and that it can change until the lock. Its fp carries the run's rev and the
    split it has NOW (rc.run_strategy): another officer's change while the box is open answers 409 with the fresh box."""
    raid = reg.raid_def(ev.instance).get("name", ev.instance)
    title = f"Split {raid} by {strategy_name(strategy)}?" if strategy in STRATEGY_DOES else f"Split {raid}?"
    if (why := strategy_refusal(reg, ev)):
        return refusal_box(title, why)
    if strategy not in STRATEGY_DOES:
        return box(title, [STRATEGY_PICK.format(raid=raid)], None)
    if strategy == rc.run_strategy(reg, ev):
        return box(title, [STRATEGY_SAME.format(raid=raid)], None)
    return box(title, [STRATEGY_DOES[strategy].format(raid=raid), *STRATEGY_AFTER], "Use this split",
               rev=f"{rc.board_rev(reg, ev)}:{rc.run_strategy(reg, ev)}")


def remind_box(reg: Registry, ev: rc.RaidEvent, rs=None) -> dict:
    """More › Remind (RaidMixin.remind) asked first (§7b): consequence("remind")'s sentences — who gets the Confirm or
    Can't make it DM again, who waits on the site. Refused (rc.remind_refusal, one sentence, only Back) when it can't go.
    Its fp carries the run's rev; the counts are in the sentences, so an answer meanwhile changes it too."""
    if (closed := _closed(ev)):
        return refusal_box_for(closed[0])
    why = rc.remind_refusal(reg, ev)
    if why:
        return refusal_box_for(why)
    return box(f"Send the reminder for {event_title(reg, ev)}?", _remind(reg, ev), "Send reminder",
               rev=rc.event_rev(reg, rs, ev) if rs is not None else "")


END_REFUSAL = "No locked run to end."


def end_box(reg: Registry, ev: rc.RaidEvent, sentences: list[str], rev: str = "") -> dict:
    """More › End the run asked first (§7b): LootMixin.end_sentences (the run's own, or its loot council's awards and
    what its summary still lists) — not red, Reopen works for an hour. `rev` = the council's state (LootMixin.end_box):
    a council that changed while the box was open answers 409 with the fresh box. A run that can't end: the refusal."""
    if (closed := _closed(ev)):
        return refusal_box_for(closed[0])
    return box(f"End {event_title(reg, ev)}?", sentences, "End the run", rev=rev)


BOARD_MOVE_TITLES = {"move": "Move {name}?", "roster": "Put {name} on the roster?", "swap": "Swap {name} and {other}?",
                     "substitute": "Put {name} in {other}'s seat?", "release": "Take {name} off the roster?"}
BOARD_MOVE_VERBS = {"move": "Move", "roster": "Put on the roster", "swap": "Swap", "substitute": "Put in the seat",
                    "release": "Take off the roster"}
BOARD_NO_CHANGE = "{name} is already there, so nothing changes."


def board_move_box(reg: Registry, ev: rc.RaidEvent, name: str, to: tuple[int, int] | None) -> dict:
    """A move on a LOCKED event's board asked first (§7b; the Events page's Move to…, a drag, Take off the roster):
    which move it is on the board as it stands (rc.board_move_kind) with consequence()'s sentences for it — move,
    roster, swap, substitute, release. Its fp carries the board's rev (rc.board_rev): a board that changed while the box
    was open answers 409 with the fresh box. A move that can't be made: its refusal, one sentence. Before the lock the
    same box says nothing is sent before the lock."""
    if (closed := _closed(ev)):
        return refusal_box_for(closed[0])
    try:
        kind, other, where = rc.board_move_kind(reg, ev, name, to)
    except ValueError as e:
        why = str(e)
        return refusal_box_for(why[:1].upper() + why[1:] + ("" if why.endswith(".") else "."))
    if kind is None:
        return refusal_box_for(BOARD_NO_CHANGE.format(name=name))
    said = (consequence("release", reg, ev, name=name) if kind == "release"
            else consequence(kind, reg, ev, name=name, other=other, to=where))
    return box(BOARD_MOVE_TITLES[kind].format(name=name, other=other), said, BOARD_MOVE_VERBS[kind], rev=rc.board_rev(reg, ev))


def undo_box(n: int, rev: str = "") -> dict:
    """Box 9: Undo my moves (`rev`: the board's fingerprint, rc.board_rev)."""
    head = "Your one move on this board is dropped." if n == 1 else f"Your {n} moves on this board are dropped."
    return box("Undo your moves?", [head, "Everyone goes back to where the lock would place them."], "Undo moves", rev=rev)


def undo_box_for(reg: Registry, ev: rc.RaidEvent, viewer: int | None) -> dict | None:
    """Box 9 for officer `viewer` on raid `ev`'s board: the count is THEIR moves as the server records them
    (rc.own_moves), the fingerprint the board and those moves (rc.undo_rev). None when they have none."""
    n = len(rc.own_moves(ev, viewer))
    return undo_box(n, rev=rc.undo_rev(reg, ev, viewer)) if n else None


def switch_box(reg: Registry, ev: rc.RaidEvent, m, character: str | None, spec: str, raids, *, locked: bool = False, swap: bool = True,
               own: bool = False, rev: str = "") -> dict:
    """Boxes 6 and 7: a role switch. Before the lock it changes the answer (a notice); after it, it sends an ask (a DM
    with Yes and No, or on the Me page with DMs off) and the seat changes only on a yes. `swap=False` (after the lock):
    the ask is for someone without a seat on the roster, who takes one only on a yes. `own` (before the lock): the
    officer's own answer — said to them, nobody told."""
    name = m.display_name
    where = rc.join_words([_raid(reg, r) for r in raids]) or _raid(reg, ev.instance)
    if locked:
        told = "They get a DM with Yes and No." if rc.reachable(m) else "It waits on their Me page."
        return box(f"Ask {name} to play {spec}?", [f"{name} is asked to play {spec} for {where}.", told,
                                                  "Their seat changes only when they say yes." if swap else
                                                  "They take a seat only when they say yes."], "Ask", rev=rev)
    sg = ev.signups.get(str(m.discord_id))
    old_char = sg.character if sg else (m.main.label if m.main else None)
    old_spec = sg.spec if sg else (m.main.spec if m.main else None)
    if own:
        joined = sg is not None and sg.status == "in"
        plays = f"{character}, {spec}" if character and old_char and character != old_char else spec
        if not joined:
            head = f"You join as {plays} for {where}."
        elif plays == spec and old_spec and old_spec != spec:
            head = f"You play {spec} for {where}, not {old_spec}."
        else:
            head = f"You play {plays} for {where}."
        return box(f"Switch to {spec}?", [head, "You can switch back until the lock."], "Switch", rev=rev)
    if character and old_char and character != old_char:
        head = f"{name} plays {character}, {spec} for {where}."
    else:
        head = f"{name} plays {spec} for {where}, not {old_spec}." if old_spec and old_spec != spec else f"{name} plays {spec} for {where}."
    return box(f"Switch {name} to {spec}?", [head, "They get a notice.", "You can switch them back until the lock."], "Switch", rev=rev)


def answer_box(reg: Registry, ev: rc.RaidEvent, m, key: str, character: str | None = None, spec: str | None = None, *,
               by_id: int | None = None, rev: str = "") -> dict:
    """Box 8: Answer for someone / Answer for them (inbox.action_lines' sentences)."""
    from .inbox import action_lines

    return box("Answer for someone", action_lines(reg, ev, m, key, character, spec=spec, by_id=by_id), "Record answer", rev=rev)


def open_now_box(reg: Registry, start, opens, lock_at, *, lineup: list[dict] | None = None, channel: bool = True, rev: str = "") -> dict:
    """Box 11: Open the sheet now, on a coming-up event, however far ahead (no horizon since the owner's tweaks,
    2026-11-24): how early and that the sheet stays up until the event, where it posts (or C9's line when a raid of the
    `lineup` first opens after it starts), and when it still locks — with the schedule not opening that night again."""
    hours = (opens - reg.now_local()).total_seconds() / 3600
    if hours >= 24:
        d = round(hours / 24)
        head = f"The sheet opens now, {count_words(d)} day{'' if d == 1 else 's'} early, and stays up until the event."
    elif round(hours) >= 1:
        h = round(hours)
        head = f"The sheet opens now, {count_words(h)} hour{'' if h == 1 else 's'} early, and stays up until the event."
    else:
        head = "The sheet opens now."
    where = (before_first_open_sentence(reg, lineup, start) if lineup else None) or (
        "It posts in the signup channel." if channel else "No signup channel is set, so the sheet waits until one is.")
    last = f"It locks {reg.local12(lock_at)}, and the schedule won't open that night again." if hours >= 1 else f"It still locks {reg.local12(lock_at)}."
    return box(f"Open the sheet for {reg.local12(start, '%a %d %b')}?", [head, where, last], "Open the sheet", rev=rev)


def already_open_sentence(reg: Registry, ev) -> "rc.Refusal":
    """New event at a time that already has a live event of that raid (a 'clash' refusal): the event named by its own
    title, never its key — 'Hyjal Summit Thu 10 Dec 6:30 PM is already open. Pick another time.'"""
    name = reg.raid_def(ev.instance).get("name", ev.instance)
    return rc.Refusal(f"{name} {reg.local12(ev.start)} is already open. Pick another time.", "clash")


def new_event_past_sentence(reg: Registry, start) -> "rc.Refusal":
    """New event at a time already past (a 'past' refusal): 'Fri 20 Nov 7:00 PM is in the past. Pick a later time.'"""
    return rc.Refusal(f"{reg.local12(start)} is in the past. Pick a later time.", "past")


def _schedule_rev(reg: Registry, sid: str) -> str:
    try:
        return reg.config_rev(f"schedule:{sid}")
    except RegistryError:
        return ""


def _day(reg: Registry, t) -> str:
    return reg.local12(t, "%a %d %b")


_DAY_NAMES = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}


def repeat_words(reg: Registry, entry: dict, *, lockout_length: bool = True) -> str | None:
    """When a schedule's events start, as the create box says it: 'every Tuesday at 6:30 PM', 'every Tuesday and
    Thursday at 7:30 PM', 'on day 1 of each 7-day lockout at 8:00 PM' ('of each lockout' without `lockout_length`: the
    Schedules list's short line); None for a pickup or a schedule with no times."""
    kind = entry.get("kind") or "weekly"
    if kind == "lockout":
        days = list(entry.get("days") or [])
        if not days:
            return None
        lockout = int(reg.raid_def(entry.get("raid")).get("lockout_days") or 7)
        each = f"each {lockout}-day lockout" if lockout_length else "each lockout"
        return f"on day{'s' if len(days) > 1 else ''} {rc.join_words([str(d) for d in days])} of {each} at {reg.time_label(entry.get('time'))}"
    if kind != "weekly":
        return None
    order = list(_DAY_NAMES)
    by_time: dict[str, list[str]] = {}
    for slot in sorted(entry.get("slots") or [], key=lambda x: (order.index(x.split()[0][:3].title()) if x.split()[0][:3].title() in order else 9)):
        day, hm = slot.split()
        by_time.setdefault(hm, []).append(_DAY_NAMES.get(day[:3].title(), day))
    if not by_time:
        return None
    return " and ".join(f"every {_and(days)} at {reg.time_label(hm)}" for hm, days in by_time.items())


def _next_unopened(reg: Registry, rs, entry: dict, sid: str):
    """The schedule's first coming start no sheet has opened for yet, else None."""
    try:
        starts = rc.next_runs_of(reg, entry["raid"], entry, n=12)
    except Exception:  # noqa: BLE001 — a schedule the engine can't read has no next event to name
        return None
    open_ = {(ev.occurrence or "") for ev in _open_events(reg, rs, sid)}
    return next((t for t in starts if rc.occurrence_stamp(reg, t) not in open_), None)


def _lead_hours(reg: Registry, entry: dict) -> float:
    try:
        return float(reg.schedule_def(entry["raid"], entry["id"])["signup_lead_hours"])
    except (RegistryError, KeyError, TypeError, ValueError):
        return 0.0


def schedule_create_box(reg: Registry, entry: dict) -> dict:
    """Box 13: a new schedule's Save (`entry` = the schedule as it would be: an all_schedules row)."""
    name = entry.get("name") or entry.get("id")
    rep = repeat_words(reg, entry)
    if (entry.get("kind") or "weekly") == "pickup":
        lines = ["It opens no event by itself.", "An officer opens each event from it with Open the sheet.",
                 "You can edit or remove the schedule at any time."]
    elif rep is None:
        lines = ["No event opens until the schedule has run times.", "You can pause or edit the schedule at any time."]
    else:
        first = (rc.next_runs_of(reg, entry["raid"], entry, n=1) or [None])[0]
        head = f"An event opens {rep}" + (f", starting {_day(reg, first)}." if first else ".")
        if _words(head) > 20:
            head = f"An event opens at each of its run times, starting {_day(reg, first)}." if first else "An event opens at each of its run times."
        lines = [head]
        if first is not None:
            opens = first - timedelta(hours=_lead_hours(reg, entry))
            lines.append("Its first sheet opens within a minute." if opens <= reg.now_local() else f"Its first sheet opens {reg.local12(opens)}.")
        lines.append("You can pause or edit the schedule at any time.")
    return box(f"Create {name}?", lines, "Create schedule", rev=_schedule_rev(reg, entry.get("id") or ""))


def _open_sentence(reg: Registry, opened: list, one: str, many: str) -> str | None:
    """'{day}' / '{n}' filled: one sentence about the events a schedule has open, None with none."""
    if not opened:
        return None
    return one.format(day=_day(reg, opened[0].start)) if len(opened) == 1 else many.format(n=len(opened))


PRIORITY_FIELDS = {"weights"}  # read live at the lock (rc.run_weights): a change reaches the events open now


LINEUP_FIELDS = {"lineup", "rosters", "follow", "then"}  # copied onto an event when its sheet opens, like its timing
WORDS_FIELDS = {"name", "notes"}  # read where they show: nothing waits for a sheet


def schedule_save_box(reg: Registry, rs, sid: str, fields, diff=()) -> dict:
    """Box 14: a schedule edit's Save. Its timing, lineup and rosters are copied onto an event when its sheet opens, so
    the change starts with the first event without a sheet, and an open one keeps its timing (configops.
    SCHEDULE_TIME_SETTINGS) or its lineup — said only when that is what changed; a priority change is read at each
    lock, so it reaches the events open now; a new name or note shows at once."""
    from .configops import SCHEDULE_TIME_SETTINGS

    entry = reg.schedule_entry(sid)
    name = entry.get("name") or entry["id"]
    rs = _store(reg, rs)
    keys = {str(k).split(".")[0] for k in (fields or [])}
    if keys and keys <= WORDS_FIELDS:
        return box(f"Save {name}?", ["The change shows from now on."], "Save", diff=diff, rev=_schedule_rev(reg, entry["id"]))
    opened = _open_events(reg, rs, entry["id"])
    if keys and keys <= PRIORITY_FIELDS:
        lines = ["The new priority applies at the next lock."]
        said = _open_sentence(reg, opened, "That includes the event open now.", "That includes the {n} events open now.")
    else:
        nxt = _next_unopened(reg, rs, entry, entry["id"])
        lines = [f"The change starts with the {_day(reg, nxt)} event." if nxt else "The change starts with the next event it opens."]
        kept = [w for w, f in (("timing", SCHEDULE_TIME_SETTINGS), ("lineup", LINEUP_FIELDS)) if keys & f]
        what = " and ".join(kept)
        said = _open_sentence(reg, opened, "The {day} event is already open and keeps its " + what + ".",
                              "The {n} open events keep their " + what + ".") if kept else None
    if said:
        lines.append(said)
    return box(f"Save {name}?", lines, "Save", diff=diff, rev=_schedule_rev(reg, entry["id"]))


def schedule_pause_box(reg: Registry, rs, sid: str) -> dict:
    """Box 15: Pause a schedule."""
    rs = _store(reg, rs)
    entry = reg.schedule_entry(sid)
    name = entry.get("name") or entry["id"]
    opened = _open_events(reg, rs, entry["id"])
    lines = ["No new sheet opens from this schedule.",
             _open_sentence(reg, opened, "The event already open stays open.", "The {n} events already open stay open."),
             "Regulars keep their spots."]
    return box(f"Pause {name}?", [x for x in lines if x], "Pause", rev=_schedule_rev(reg, entry["id"]))


def schedule_resume_box(reg: Registry, rs, sid: str) -> dict:
    """Box 16: Resume a schedule — when its next sheet opens, and for which event."""
    rs = _store(reg, rs)
    entry = reg.schedule_entry(sid)
    name = entry.get("name") or entry["id"]
    lines = ["Sheets open from this schedule again."]
    nxt = _next_unopened(reg, rs, entry, entry["id"]) if (entry.get("kind") or "weekly") != "pickup" else None
    if nxt is not None:
        opens = nxt - timedelta(hours=_lead_hours(reg, entry))
        lines.append(f"The next one opens now, for {reg.local12(nxt)}." if opens <= reg.now_local()
                     else f"The next one opens {reg.local12(opens)}, for {reg.local12(nxt)}.")
    return box(f"Resume {name}?", lines, "Resume", rev=_schedule_rev(reg, entry["id"]))


def schedule_remove_box(reg: Registry, rs, sid: str) -> dict:
    """Box 17: Remove a schedule (counts from Registry.schedule_removal_effects) — red, it cannot be brought back. What
    the open events no longer get at their lock (Effects.loses) is never dropped."""
    rs = _store(reg, rs)
    entry = reg.schedule_entry(sid)
    name = entry.get("name") or entry["id"]
    fx = reg.schedule_removal_effects(entry["id"], rs)
    n = len(fx.regulars)
    lines = [(f"The schedule and its {n} regular spots are removed, and each regular sees a notice." if n > 1 else
              "The schedule and its one regular spot are removed, and that regular sees a notice." if n == 1 else "The schedule is removed.")]
    k = len(_open_events(reg, rs, entry["id"])) or len(fx.open_runs)
    if k:
        keep = "The open event keeps its sheet and its answers" if k == 1 else f"The {k} open events keep their sheets and their answers"
        if fx.loses:
            keep = keep.replace(" and its answers", " and answers").replace(" and their answers", " and answers")
            keep += f", but lock{'s' if k == 1 else ''} with {rc.join_words(fx.loses)}"
        lines.append(keep + ".")
    lines.append("A removed schedule cannot be brought back.")
    return box(f"Remove {name}?", lines, "Remove schedule", danger=True, rev=_schedule_rev(reg, entry["id"]))


LOCKOUT_FIELDS = {"lockout_days", "first_open", "duration_hours"}


def open_events_of_raid(reg: Registry, rs, rid: str) -> int:
    """How many live events (test runs left out) play raid `rid` in any part."""
    return len({rc.event_id(ev) for ev in reg._live_runs(rs) if ev.instance == rid and not ev.test})


def raid_save_box(reg: Registry, rs, rid: str, changed, diff=()) -> dict:
    """Box 19: a raid edit's Save. `changed`: what changes — 'minimum:<slot>' per minimum, 'weight:<key>' (or
    'weights') per priority weight, a setting's name otherwise (lockout_days, first_open, duration_hours…)."""
    rs = _store(reg, rs)
    changed = {str(c) for c in changed or ()}
    mins = {c for c in changed if c.startswith("minimum")}
    weights = {c for c in changed if c.startswith("weight")}
    lockout = changed & LOCKOUT_FIELDS
    rest = changed - mins - weights - lockout
    kinds = [x for x in (mins, weights, lockout, rest) if x]
    n = open_events_of_raid(reg, rs, rid)
    reaches = "That includes the event open now." if n == 1 else f"That includes the {n} events open now." if n else None
    if len(kinds) > 1:
        head = "The changes apply at the next lock."
    elif mins:
        head = "The new minimums apply at the next lock." if len(mins) > 1 else "The new minimum applies at the next lock."
    elif weights:
        head = "The new priority applies at the next lock."
    elif lockout:
        head = "Lockout checks use the new values from now on."
    else:
        head, reaches = "The new value applies from now on.", None
    lines = [head] + ([reaches] if reaches else [])
    return box(f"Save {_raid(reg, rid)}?", lines, "Save", diff=diff, rev=reg.config_rev(f"raids:{rid}"))


def raid_reset_box(reg: Registry, rid: str, *, unsaved: int = 0) -> dict:
    """A raid's Reset (carried: the server's sentence, checked against `raid_all:<rid>`) — red, the stored changes go."""
    lines = raid_reset_consequence(reg, rid, unsaved=unsaved)
    return box(f"Reset {_raid(reg, rid)}?", lines, "Reset" if len(lines) > 1 else None, danger=len(lines) > 1,
               rev=reg.config_rev(f"raid_all:{rid}"))


# ---------------------------------------------------------------- people (design §5.58): one generator per people verb
# The Members page's ⋮ menus, its per-member Save and its Confirm characters… walk, the Me page, and Discord's /me char ·
# /roster char · /roster rank · /roster set-main · /roster confirm · /roster back decide screens print exactly these
# (the browser and the wizards never compose one). `by_id` decides the voice: the member's own record reads "your"
# ("Your character Ashgrove retires."), anyone else's names the member once ("Bramble's character Ashgrove retires.")
# and then says "they". A ripple into runs is one sentence per fate (rc.character_effects), printed after the first
# sentence and before the closing ones. A change that can't go raises RegistryError with one sentence (the route answers
# 400 with it, the wizard shows it as its refusal).

PEOPLE_VERBS = ("retire", "delete", "main", "spec", "flex", "professions", "rank", "confirm", "dm", "back", "save")
RANK_CHOICES = tuple(r for r in RANKS if r != "alt")
"""The ranks an officer gives a member (the Members page's Rank…, /roster rank): the player ranks and social — never
'alt', which only an alt reads (D25)."""
NOBODY_DM = "Nobody gets a DM."
NO_SHEET_CHANGES = "No sheet had been answered for it, so no sheet changes."


class _Voice:
    """Who a people sentence is about, from who presses: their own record ("your") or someone else's ("<name>'s", then
    "they")."""

    def __init__(self, reg: Registry, m, by_id: int | None):
        self.own = by_id is not None and m is not None and int(by_id) == int(m.discord_id)
        self.owner = by_id is not None and reg.is_owner_id(int(by_id))
        self.Whose = "Your" if self.own else f"{m.display_name}'s"
        self.whose = "your" if self.own else f"{m.display_name}'s"
        self.their = "your" if self.own else "their"
        self.they = "you" if self.own else "they"
        self.theyre = "you're" if self.own else "they're"


def _a(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _char(reg: Registry, m, label: str | None, v: _Voice):
    c = next((x for x in m.active() if x.label == label), None) if m is not None and label else None
    if c is None:
        raise RegistryError(f"{label or 'That character'} is no longer one of {v.whose} characters.")
    return c


def _effects(reg: Registry, rs, m, changes: list[dict]) -> list[dict]:
    """rc.character_effects of every change, each sentence once."""
    if rs is None or m is None:
        return []
    out, seen = [], set()
    for ch in changes:
        for e in rc.character_effects(reg, rs, m, ch):
            if e["text"] not in seen:
                seen.add(e["text"])
                out.append(e)
    return out


def _freed(fx: list[dict]) -> bool:
    return any(e["kind"] == "seat" and e["action"] == "free" for e in fx)


def _seat_told(v: _Voice, fx: list[dict]) -> list[str]:
    """Someone else's change that hands a seat back reaches the member as a notice on their Me page (rc.free_seat),
    never a DM; their own change writes them nothing."""
    if v.own:
        return []
    return ["They see the released seat on their Me page."] if _freed(fx) else [NOBODY_DM]


def _main_tail(reg: Registry, m, c, v: _Voice) -> str:
    """How a retire or delete ends its first sentence: who becomes the main when it was the main (the registry picks the
    next active character and moves the rank to it — settle_ranks)."""
    if not c.is_main:
        return "."
    nxt = next((x for x in m.active() if x is not c), None)
    if nxt is None:
        return f", and {v.they} have no main until {v.they} add a character."
    return f", and {nxt.label} becomes {v.their} main with rank {m.rank}."


def retire_consequence(reg: Registry, rs, m, label: str, *, by_id: int | None = None) -> list[str]:
    """Retire one character (a table row {retire: true}): it leaves sheets, seats and regulars and stays on the record
    as retired."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    fx = _effects(reg, rs, m, [{"kind": "retire", "label": c.label}])
    return [f"{v.Whose} character {c.label} retires{_main_tail(reg, m, c, v)}", *[e["text"] for e in fx], *_seat_told(v, fx),
            "It stays on the record as retired, so its history is kept."]


def delete_consequence(reg: Registry, rs, m, label: str, *, by_id: int | None = None) -> list[str]:
    """Delete one character (a table row {delete: true}): the same ripple as a retire, and it leaves the record."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    fx = _effects(reg, rs, m, [{"kind": "delete", "label": c.label}])
    return [f"{v.Whose} character {c.label} is deleted{_main_tail(reg, m, c, v)}", *[e["text"] for e in fx], *_seat_told(v, fx),
            "This can't be undone, so retire it to keep it on the record."]


def main_consequence(reg: Registry, rs, m, label: str, *, by_id: int | None = None) -> list[str]:
    """Make one character the main: the rank moves with it (D25), the old main reads alt."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    if c.is_main:
        raise RegistryError(f"{c.label} is already {v.whose} main.")
    old = m.main
    fx = _effects(reg, rs, m, [{"kind": "main", "label": c.label}])
    return [f"{c.label} becomes {v.whose} main" + (f" instead of {old.label}." if old else "."), *[e["text"] for e in fx],
            *([f"Make {old.label} the main again to undo it."] if old else [])]


def spec_consequence(reg: Registry, rs, m, label: str, spec: str, offspec: str | None = None, *, by_id: int | None = None) -> list[str]:
    """A new spec and / or offspec for one character: sheet answers follow it, a seat in the same role follows it, a role
    flip keeps a recurring run's seat for the officers to decide and hands an ad hoc run's back (rc.character_effects)."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    try:
        new = reg.profile.spec(c.cls, spec).spec
        off = reg.profile.spec(c.cls, offspec).spec if offspec else None
    except KeyError:
        raise RegistryError(f"{c.cls} has no spec called {offspec if spec in reg.profile.classes.get(c.cls, {}) else spec}.") from None
    if (new, off) == (c.spec, c.offspec):
        raise RegistryError(f"{c.label} is already {new}" + (f" with {off} as offspec." if off else "."))
    tail = f", with {off} as offspec." if off else ", with no offspec."
    head = f"{v.Whose} character {c.label} " + (f"switches to {new}" if new != c.spec else f"keeps {new}") + tail
    fx = _effects(reg, rs, m, rc.character_changes(reg, m, [{"label": c.label, "cls": c.cls, "spec": new, "offspec": off}]))
    lines = [head, *[e["text"] for e in fx], *(_seat_told(v, fx) if _freed(fx) else [])]
    if _freed(fx):
        lines.append(f"A released seat stays released if {v.they} switch back.")
    return lines


def flex_consequence(reg: Registry, rs, m, label: str, roles: list[str], *, by_id: int | None = None) -> list[str]:
    """The roles a character could also play if asked (Registry.set_flex drops the ones its spec and offspec already
    give); the planner's Suggest… and Fill seats… ask a character to cover them."""
    from .constants import ROLES
    from .registry import join_words

    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    own = {reg.profile.spec(c.cls, c.spec).role} | ({reg.profile.spec(c.cls, c.offspec).role} if c.offspec else set())
    extra = [r for r in ROLES if r in {str(x).strip().lower() for x in roles or []} and r not in own]
    if set(extra) == set(c.flex):
        raise RegistryError(f"{c.label} can already play {join_words(extra)} when asked." if extra else f"{c.label} already plays only what its spec and offspec give.")
    if extra:
        return [f"{v.Whose} character {c.label} could also play {join_words(extra)} when asked.",
                "Fill seats can ask it to cover those roles when a run is short."]
    return [f"{v.Whose} character {c.label} plays only what its spec and offspec give.", "Fill seats stops asking it to cover other roles."]


def profession_consequence(reg: Registry, m, label: str, entries: list[dict], *, by_id: int | None = None) -> list[str]:
    """A character's professions as the save will leave them (design §5.60): one sentence per profession recorded or
    removed — folded under the Confirm rule: three or more recorded make one sentence without skills, every removal
    one sentence. Nothing about runs: professions touch no seat. Unchanged → refused."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    after = {p.id: p for p in reg.check_hand_professions(c, entries)}  # the write's own check: a synced character refuses here
    before = {p.id: p for p in c.professions}
    if {k: p.model_dump() for k, p in after.items()} == {k: p.model_dump() for k, p in before.items()}:
        raise RegistryError(f"{c.label}'s professions are already as given.")

    def names(ids) -> str:
        ns = [reg.profile.profession(pid).name for pid in ids]
        return ns[0] if len(ns) == 1 else ", ".join(ns[:-1]) + " and " + ns[-1]

    changed = [pid for pid, p in after.items() if pid not in before or before[pid].model_dump() != p.model_dump()]
    removed = [pid for pid in before if pid not in after]
    out = []
    if len(changed) > 2:  # the Confirm rule: folded, skills left out
        out.append(f"{v.Whose} {names(changed)} are recorded.")
    else:
        for pid in changed:
            p = after[pid]
            n = len(p.recipes)
            tail = f" with {n} recipe{'s' if n != 1 else ''}" if n else ""
            at = f" at {p.skill}" if p.skill is not None else ""  # a skill not said yet: recorded without one
            out.append(f"{v.Whose} {reg.profile.profession(pid).name} is recorded{at}{tail}.")
    if removed:
        out.append(f"{v.Whose} {names(removed)} {'is' if len(removed) == 1 else 'are'} removed from {v.their} record.")
    return out


OWN_RANK = "Another officer sets your rank."
OWN_CONFIRM = "Another officer confirms your characters."
"""The refusals on your own record (the owner aside): the Members page greys Rank… and Confirm with these words
(GET /api/members `own`), and the confirm walk skips your own characters with OWN_CONFIRM."""


def rank_consequence(reg: Registry, m, rank: str, *, by_id: int | None = None) -> list[str]:
    """The member's rank (D25: on the main; officers only, never your own — the owner aside — and never alt)."""
    v = _Voice(reg, m, by_id)
    if v.own and not v.owner:
        raise RegistryError(OWN_RANK)
    rank = str(rank or "").strip().lower()
    if rank not in RANK_CHOICES:
        raise RegistryError(f"A rank is {', '.join(RANK_CHOICES[:-1])} or {RANK_CHOICES[-1]}.")
    if m is None or m.main is None:
        raise RegistryError(f"{m.display_name if m else 'They'} has no main yet, and rank lives on the main.")
    if m.rank == rank:
        raise RegistryError(f"{v.Whose} rank is already {rank}.")
    return [f"{v.Whose} rank goes from {m.rank} to {rank}.", f"It lives on {v.their} main and counts for who gets a seat from the next lock."]


def confirm_consequence(reg: Registry, m, label: str, *, by_id: int | None = None) -> list[str]:
    """An officer's check that a character is real and theirs (Registry.confirm): never your own (the owner aside);
    the member hears it on their Me page (a notice — never a DM)."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    if v.own and not v.owner:
        raise RegistryError(OWN_CONFIRM)
    if c.status != "active" or not c.name:
        raise RegistryError("A planned character is confirmed once it has a name.")
    if c.confirmed_by:
        raise RegistryError(f"{c.label} was already confirmed by {c.confirmed_by}.")
    return [f"{v.Whose} character {c.label} is confirmed as {_a(c.spec)} {c.cls}.", *([] if v.own else ["They see it on their Me page."])]


def dm_consequence(reg: Registry, m, on: bool, *, by_id: int | None = None) -> list[str]:
    """DMs on or off (Registry.set_dm) — the Members page's Turn DMs on / off for <name>…."""
    v = _Voice(reg, m, by_id)
    if bool(on) == (not m.dm_opt_out):
        raise RegistryError(f"{v.Whose} DMs are already {'on' if on else 'off'}.")
    if on:
        return [f"{v.Whose} Confirm and fill asks arrive as a DM with two buttons.", f"They also show on {v.their} Me page.",
                "DMs can be turned off again at any time."]
    return [f"{v.Whose} Confirm and fill asks wait on {v.their} Me page and the sheet.", f"The bot sends {'you' if v.own else 'them'} no DM.",
            "DMs can be turned on again at any time."]


def back_consequence(reg: Registry, rs, m, start: str, *, by_id: int | None = None) -> list[str]:
    """I'm back (discord_people.im_back): the absence goes, an open sheet it had answered No thanks opens again, a seat
    it released stays released (discord_people.absence_clear_lines — one sentence per event, summed past three)."""
    from . import discord_people

    v = _Voice(reg, m, by_id)
    a = next((x for x in (m.absences if m else []) if x.start == str(start).strip()), None)
    if a is None:
        raise RegistryError(f"No absence starting {reg.day_label(str(start).strip())}.")
    when = f"on {reg.day_label(a.start)}" if a.end == a.start else f"from {reg.day_label(a.start)} to {reg.day_label(a.end)}"
    lines = discord_people.absence_clear_lines(reg, rs, m, a)
    return [f"{v.Whose} absence {when} is cleared.", *(lines or [NO_SHEET_CHANGES]), f"Record it again if {v.theyre} still away."]


NO_SHEET_IN_DAYS = "No live sheet falls in those days."


def absence_consequence(reg: Registry, rs, m, start: str, end: str, *, by_id: int | None = None) -> list[str]:
    """An absence before it is recorded (the Me page's and the Members page's Add the absence, design §5.58 phase 3):
    what is recorded, one sentence per live event it answers (discord_people.absence_lines, summed past three) and how to undo it."""
    from . import discord_people

    v = _Voice(reg, m, by_id)
    when = f"on {reg.day_label(start)}" if end == start else f"from {reg.day_label(start)} to {reg.day_label(end)}"
    lines = discord_people.absence_lines(reg, rs, m, start, end)
    return [f"{v.Whose} absence {when} is recorded.", *(lines or [NO_SHEET_IN_DAYS]), f"Press I'm back if {v.their} plans change."]


def save_consequence(reg: Registry, rs, m, rows: list[dict]) -> list[str]:
    """A member's edited rows saved in one go (the Members page's Save <name>'s characters, the Me page's Save): one
    sentence per fate in the live runs (rc.character_preview); [] = nothing beyond the record changes, and the first
    press saves."""
    if rs is None or m is None:
        return []
    return rc.character_preview(reg, rs, m, rc.character_changes(reg, m, rows))


def people_consequence(reg: Registry, rs, m, verb: str, *, by_id: int | None = None, label: str | None = None, spec: str | None = None,
                       offspec: str | None = None, roles: list[str] | None = None, rank: str | None = None, on: bool | None = None,
                       start: str | None = None, rows: list[dict] | None = None, professions: list[dict] | None = None) -> list[str]:
    """THE people confirm: the generator of `verb` (PEOPLE_VERBS) with its arguments. Raises RegistryError (one sentence)
    when the change can't go."""
    if m is None:
        raise RegistryError("They have nothing on file.")
    if verb == "retire":
        return retire_consequence(reg, rs, m, label, by_id=by_id)
    if verb == "delete":
        return delete_consequence(reg, rs, m, label, by_id=by_id)
    if verb == "main":
        return main_consequence(reg, rs, m, label, by_id=by_id)
    if verb == "spec":
        return spec_consequence(reg, rs, m, label, spec, offspec, by_id=by_id)
    if verb == "flex":
        return flex_consequence(reg, rs, m, label, roles or [], by_id=by_id)
    if verb == "professions":
        return profession_consequence(reg, m, label, professions or [], by_id=by_id)
    if verb == "rank":
        return rank_consequence(reg, m, rank, by_id=by_id)
    if verb == "confirm":
        return confirm_consequence(reg, m, label, by_id=by_id)
    if verb == "dm":
        return dm_consequence(reg, m, bool(on), by_id=by_id)
    if verb == "back":
        return back_consequence(reg, rs, m, start, by_id=by_id)
    if verb == "save":
        return save_consequence(reg, rs, m, rows or [])
    raise ValueError(f"unknown people verb {verb}")


def forget_consequence(m, by_id: int | None, tz=None) -> str:
    """The Forget confirm (the Me page's own words, the Ops page's any): what goes, and that it stays gone. `tz` = the
    guild's zone (Registry.tz), so the day matches the Ops row's."""
    whose = "what you said" if by_id is not None and int(by_id) == int(m.author_id) else f"what {m.author} said"
    return f"The bot forgets {whose} on {m.when(tz)}. It never repeats it again."


def memory_off_consequence(reg, cid, n: int) -> str:
    """Turning a channel's Memory off forgets what it held (the setting means what it says)."""
    if n <= 0:
        return f"The bot remembers nothing from <#{cid}> yet, so nothing is forgotten."
    things = "the one thing it remembers" if n == 1 else f"the {n} things it remembers"
    return f"The bot forgets {things} from <#{cid}>."


def memory_shared_consequence(reg, cid) -> str:
    """An officers' channel set to Remembers (shared): what is said there can reach any member anywhere."""
    return f"What is said in <#{cid}> can then be repeated to any member in any channel."


def armory_created_words(reg: Registry, facts) -> str:
    """The day a guild was created, in the guild's zone and in words ('Mon 26 Oct 2026'), never ISO."""
    return reg.local12(facts.created, "%a %d %b %Y")


def armory_guild_consequence(reg: Registry, facts) -> list[str]:
    """The confirm before linking the server to the guild a lookup found (armory link §5.1, D1): what was found — name,
    faction, kind, members, created — so a stranger's guild of the same name is seen before the press, then what changes,
    who is told and what can be undone."""
    faction = str(facts.faction)
    article = "an" if faction[:1].upper() in "AEIOU" else "a"
    n = int(facts.members)
    found = (f"{facts.name} is {article} {faction} {facts.kind} guild with {n} member{'' if n == 1 else 's'}, "
             f"created {armory_created_words(reg, facts)}.")
    old = reg.config.armory
    if old is not None and int(old.guild_id) == int(facts.id):
        change = "It is already linked, so only its name and faction are refreshed."
    elif old is not None:
        change = f"It replaces {old.name}, and every character keeps its link."
    else:
        change = "The bot then reads its roster to show who is in the guild."
    if reg.config.market is not None and not reg.market_survives(facts):
        return [found, change, PRICES_OFF]  # the prices line takes the undo's place: at most three sentences
    return [found, change, "You can change or unlink it at any time."]


def armory_guild_clear_consequence(reg: Registry) -> list[str]:
    """The confirm before unlinking the server's guild (§5.1): every character keeps its link; only "in the guild" goes."""
    old = reg.config.armory
    if old is None:
        return ["No guild is linked to this server."]
    return [f"{old.name} stops being this server's guild in the armory.",
            "Every character keeps its link, and who is in the guild stops being known.",
            PRICES_OFF if reg.config.market is not None else "You can link a guild again at any time."]


# ---- the guild's auction house (auction prices §4.1): the pick and turning prices off
PRICES_OFF = "Prices turn off until you pick the auction house again."


def market_consequence(reg: Registry, house) -> list[str]:
    """The confirm before picking `house` (auctions.House) as the guild's auction house: what changes, who sees it, what
    can be undone. Two houses of a ruleset share a name, so a change of house says "instead", never the old name."""
    old = reg.config.market
    if old is not None and int(old.house_id) == int(house.id):
        return [f"The {house.name} is already the guild's auction house, so nothing changes."]
    change = (f"The guild's prices come from this {house.name} instead of the one picked before." if old is not None
              else f"The guild's prices come from the {house.name} from now on.")
    return [change, "Members see them on the Professions page within about ten minutes.",
            "You can pick another auction house or turn prices off at any time."]


RESET_WORDS = (("schedules", "schedules"), ("minimums", "minimums"), ("weights", "seat weights"), ("notes", "notes"))


def raid_reset_consequence(reg: Registry, raid: str, *, unsaved: int = 0) -> list[str]:
    """The confirm before a raid goes back to the game's defaults (the raid wizard's Reset): what goes, named from the
    guild's stored changes, and the wizard's own unsaved edits when there are some."""
    name = reg.raid_def(raid).get("name", raid)
    over = reg.raid_override(raid)
    if not over:
        return [f"{name} is already on the game's defaults."]
    parts = [words for key, words in RESET_WORDS if over.get(key)]
    if any(v not in (None, [], {}, "") for key, v in over.items() if key not in dict(RESET_WORDS)):
        parts.append("other settings" if parts else "settings")
    edits = "so does your unsaved change" if unsaved == 1 else f"so do your {unsaved} unsaved changes"

    def said(ps: list[str]) -> str:
        goes = "Its " + ((", ".join(ps[:-1]) + " and " + ps[-1]) if len(ps) > 1 else (ps[0] if ps else "settings"))
        return f"{goes} go" + (f", and {edits}." if unsaved else ".")
    second = said(parts)
    if "minimums" in parts:  # carried (phase 2 review): the minimums that go are named, where the sentence has room
        slots = [reg.comp_slot_words(k) for k in (over.get("minimums") or {})]
        named = said([f"minimums for {_and(slots)}" if p == "minimums" else p for p in parts]) if slots else second
        second = named if _words(named) <= 20 else second
    lines = [f"{name} goes back to the game's defaults.", second]
    if over.get("schedules"):
        lines.append("No sheet opens for this raid until someone adds a schedule again.")
    return lines


def market_clear_consequence(reg: Registry) -> list[str]:
    """The confirm before turning prices off (forgetting the guild's auction house)."""
    if reg.config.market is None:
        return ["No auction house is picked for this guild."]
    return ["Prices turn off on the Professions page and in Discord.", "You can pick the auction house again at any time."]



# ---- a character's armory link (armory link §5.2): the link, the unlink and the "keep professions by hand" switch
def _whose(m, by_id, *, start: bool) -> str:
    """"Your" / "your" on your own character, else "<name>'s" (the person named once; later sentences say "they")."""
    if by_id is not None and int(by_id) == int(m.discord_id):
        return "Your" if start else "your"
    return f"{m.display_name}'s"


def armory_link_consequence(reg: Registry, m, c, found, by_id) -> list[str]:
    """The confirm before linking `c` (member `m`'s character) to the armory character a lookup found (`found`:
    discord_armory.Found): what the armory shows, that the record takes that name and its professions from the armory,
    then who sees it and what can be undone. Found on the guild's roster (below level 10) the professions follow from
    level 10, when the armory first shows them."""
    full = f"{found.name} {found.surname}" if found.surname else found.name
    what = f"a level {int(found.level)} {found.cls}" if found.cls else f"a level {int(found.level)} character"
    seen = (f"The guild's roster shows {full}, {what}." if found.source == "roster" else f"The armory shows {full}, {what}.")
    own = by_id is not None and int(by_id) == int(m.discord_id)
    who = _whose(m, by_id, start=True)
    if found.source == "roster":
        change = (f"{who} {c.label} takes that name, and its professions follow the armory from level 10." if full != c.label
                  else f"{who} {c.label} follows the armory, and its professions follow it from level 10.")
    else:
        change = (f"{who} {c.label} takes that name, and the armory's professions replace its own." if full != c.label
                  else f"The armory's professions replace those on {_whose(m, by_id, start=False)} {c.label}.")
    told = "You can unlink it at any time." if own else "They see the link on their Me page, and you can unlink it at any time."
    return [seen, change, told]


def armory_unlink_consequence(reg: Registry, m, c, by_id) -> list[str]:
    """The confirm before unlinking `c` (spec §5.2 "Unlink"): the record keeps what it last had, its professions are
    kept by hand from now on."""
    return [f"{_whose(m, by_id, start=True)} {c.label} stops following the armory.",
            "Its professions stay as they are and are kept by hand from now on.",
            "You can link it again at any time."]


def profession_sync_consequence(on: bool, label: str | None = None, *, current: bool | None = None) -> list[str]:
    """The confirm of the switch (D5): off = kept by hand, edited from the game's list; on = the armory's professions
    replace the hand edits now, and those can't be brought back. `current` = the switch as it stands: already in that
    state → one sentence saying so (nothing would change)."""
    name = label or "This character"
    if current is not None and bool(current) == bool(on):
        return [f"{name}'s professions already follow the armory." if on else f"{name}'s professions are already kept by hand."]
    if not on:
        return [f"{name}'s professions are kept by hand from now on.",
                "The armory stops changing them, and you edit them from the game's list.",
                "You can let the armory keep them again at any time."]
    return [f"The armory's professions replace the hand edits on {label or 'this character'}.",
            "Each update from the armory keeps them current after that.",
            "The hand edits can't be brought back."]


def move_consequence(reg: Registry, m_from, c, m_to) -> list[str]:
    """The confirm before an officer moves member `m_from`'s character `c` to member `m_to` (armory link §5.4,
    Registry.move_character), at most three sentences: what moves with it (its past loot and attendance among it; its
    regular spots only when it keeps one); then, only when they apply (Registry.move_effects), the regular spot it gives
    up because the new member already holds one in that schedule, and whose main changes; then who is told — left out
    when both special cases take the room (they are what the officer must hear). Each person is named once; there is
    no "move it back" line, because a spot given up does not come back."""
    eff = reg.move_effects(m_from, c, m_to)
    whose = f"{m_from.display_name}'s main {c.label}" if c.is_main else f"{m_from.display_name}'s {c.label}"
    carries = "loot history, attendance and regular spots" if eff["kept"] else "loot history and attendance"
    first = f"{whose} moves to {m_to.display_name} with its {carries}."
    if len(first.split()) > 20:  # long display names: the whole record, never a sentence too long to read aloud
        first = f"{whose} moves to {m_to.display_name} with everything on its record."
    out = [first]
    if eff["dropped"]:
        words = " and ".join(series_words(reg, k) for k in eff["dropped"])
        said = (f"It gives up its regular spot on {words}, where the new member already has one." if len(eff["dropped"]) == 1
                else f"It gives up its spots on {words}, which the new member holds.")
        if len(said.split()) > 20:  # long schedule names: the count, never a sentence too long to read aloud
            said = ("It gives up its regular spot, because the new member already holds one in that schedule." if len(eff["dropped"]) == 1
                    else f"It gives up {len(eff['dropped'])} regular spots, because the new member already holds them.")
        out.append(said)
    nxt = eff["next_main"]
    gone = f"{nxt.label} becomes the old member's main" if nxt is not None else "the old member has no main left"
    if c.is_main and eff["arrives_main"]:
        out.append(f"It becomes the new member's main, and {gone}.")
    elif c.is_main:
        out.append(gone[0].upper() + gone[1:] + ".")
    elif eff["arrives_main"]:
        out.append("It becomes the new member's main.")
    if len(out) < 3:
        out.append("Both see a notice on their Me page.")
    return out


def armory_give_consequence(reg: Registry, m, found, *, main: bool) -> list[str]:
    """The confirm before an officer registers a guild character nobody claims for member `m` and links it (armory link
    §5.4, Give to a member…): what the armory shows and where it goes, where its professions come from, who sees it and
    the way back. `found` = discord_armory.Found; `main` = it becomes their main (they have none yet)."""
    what = f"a level {int(found.level)} {found.cls}" if found.cls else f"a level {int(found.level)} character"
    slot = "their main" if main else "an alt"
    profs = ("Its professions follow the armory from level 10." if found.source == "roster"
             else "Its professions come from the armory.")
    return [f"{found.label}, {what}, is registered to {m.display_name} as {slot}.", profs,
            "They see it on their Me page, and you can unlink it at any time."]


def armory_seed_consequence(n: int, refused: int = 0) -> list[str]:
    """The confirm before an officer links the picked characters of the seed (armory link §5.5): `n` = the picks that
    pass every check made before asking the armory (armory_seed.check), `refused` = the ones that don't — named, so the
    officer knows before the press; each link goes on its own; members see their links; each can be unlinked."""
    if n == 0:
        return ["No character is linked, because every pick is refused."]
    first = ("One character is linked to the armory." if n == 1
             else f"{n} characters are linked to the armory, each on its own.")
    told = "Each member sees the link on their Me page."
    if refused:
        return [first, "One pick is refused, and its reason is shown." if refused == 1 else f"{refused} picks are refused, and each reason is shown.", told]
    return [first, told, "You can unlink it at any time." if n == 1 else "You can unlink any of them at any time."]


def armory_sync_sentence(label: str, result: str) -> str:
    """What Sync now did to `label` (discord_armory.sync_character's result), in one sentence."""
    from .discord_armory import SYNC_LIMITED

    return {"updated": f"{label} is updated from the armory.", "unchanged": f"{label} already matches the armory.",
            "missing": f"The armory no longer finds {label}, so it keeps what it last had.", "limited": SYNC_LIMITED}.get(result, SYNC_LIMITED)

__all__ = ["BOARD_VERBS", "CONFIRM_DM", "NAME_TOKEN", "NOBODY_DM", "NO_SHEET_CHANGES", "NO_SHEET_IN_DAYS", "OTHER_TOKEN", "OWN_CONFIRM", "OWN_RANK", "PEOPLE_VERBS", "RANK_CHOICES", "TO_TOKEN",
           "VERBS", "absence_consequence", "armory_created_words", "armory_give_consequence", "armory_seed_consequence", "armory_sync_sentence", "armory_link_consequence", "armory_unlink_consequence", "armory_guild_clear_consequence", "armory_guild_consequence", "back_consequence", "confirm_consequence", "consequence", "consequences_json", "delete_consequence", "dm_consequence", "ended_line",
           "flex_consequence", "forget_consequence", "main_consequence", "memory_off_consequence", "memory_shared_consequence", "move_consequence", "people_consequence", "profession_consequence", "profession_sync_consequence", "rank_consequence", "regular_remove", "reopen_sentence",
           "retire_consequence", "save_consequence", "series_words", "spec_consequence"]

