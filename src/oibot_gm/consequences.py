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
cached line. A board sentence is about one member and a group: the server writes it with NAME_TOKEN / OTHER_TOKEN /
TO_TOKEN where those go and the board only puts the names in (it never composes a word of it)."""
from __future__ import annotations

from . import raidcycle as rc
from .registry import Registry

VERBS = ("lock", "cancel", "clear", "release", "move", "roster", "swap", "substitute", "remind", "end")
BOARD_VERBS = ("move", "roster", "swap", "substitute")  # the board's moves: sentences about {name} / {other} / {to}
NAME_TOKEN = "{name}"
"""The placeholder in `consequences_json["release_named"]` and the board verbs: the board names the member it is about,
and the only thing the browser does to the sentence is put that name where the token is (it never composes the sentence)."""
OTHER_TOKEN = "{other}"  # the member already in the seat (swap: the one trading places; substitute: the one whose seat it was)
TO_TOKEN = "{to}"  # the group the move goes to, as the board names it ("Group 2")


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def consequence(verb: str, reg: Registry, ev: rc.RaidEvent, *, name: str | None = None, other: str | None = None,
                to: str | None = None) -> list[str]:
    """The sentence(s) the confirm screen shows before `verb` on `ev`. `name`: the member a `release` or a board move
    is about; `other`: the member already in the seat (swap / substitute); `to`: the group ("Group 2"). Without them the
    sentence stays general. Unknown verbs → []."""
    if verb in BOARD_VERBS:
        return _board(verb, reg, ev, name or "They", other or "the other", to or "the group")
    if verb == "remind":
        return _remind(reg, ev)
    if verb == "end":
        return _end(reg, ev)
    if verb == "lock":
        return _lock(reg, ev)
    if verb == "cancel":
        return _cancel(reg, ev)
    if verb == "clear":
        return _clear(reg, ev)
    if verb == "release":
        return _release(reg, ev, name)
    return []


def consequences_json(reg: Registry, ev: rc.RaidEvent) -> dict[str, str]:
    """`ev_json.consequences`: every verb in VERBS as one string (the lines joined by a space) — the board verbs with
    NAME_TOKEN / OTHER_TOKEN / TO_TOKEN where the names go — plus `release_named`: the release sentence about one member
    with NAME_TOKEN where the name goes (the board fills it in for the member being dragged out; before lock it is the
    same no-name sentence)."""
    out = {v: " ".join(consequence(v, reg, ev, name=NAME_TOKEN, other=OTHER_TOKEN, to=TO_TOKEN) if v in BOARD_VERBS else consequence(v, reg, ev)) for v in VERBS}
    out["release_named"] = " ".join(consequence("release", reg, ev, name=NAME_TOKEN))
    return out


def _dms_off(reg: Registry, ids) -> int:
    return sum(1 for uid in ids if (m := reg.members.get(int(uid))) is not None and m.dm_opt_out)


CONFIRM_DM = "the Confirm or Can't make it DM"
"""The confirmation card by its two buttons, in every confirm sentence (the Confirm rule, design §5.57)."""


def _closed(ev: rc.RaidEvent) -> list[str] | None:
    """A run already finished or cancelled: every verb's confirm says so, in one sentence."""
    if ev.state in ("done", "cancelled"):
        return [f"This run is already {rc.RUN_STATE_WORDS[ev.state]}."]
    return None


def _lock(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    from .raid_views import run_times

    if ev.state != "open":
        return _closed(ev) or ["This run is already locked."]
    joined = ev.by_status("in")
    seats = rc.run_size(reg, ev) * max(1, rc.run_rosters(reg, ev))
    n = min(len(joined), seats)
    lines = ["The board becomes the roster."]
    test = rc.is_test_run(reg, ev)
    if not rc.run_confirms(reg, ev):  # ad hoc (D5): Join is the commitment
        lines.append(rc.no_confirm_sentence(reg, ev))
        if test:
            lines.append("On a test run the puppets' messages come to the tester.")
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


def _cancel(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    """Who the cancel touches, from rc.cancel_effects. Nobody is DMed (the DM rule): the sheet is edited to its final
    state and every open ask card says Cancelled; the screen's own choice says whether a line goes to the signup channel."""
    if (closed := _closed(ev)):
        return closed
    fx = rc.cancel_effects(reg, ev)
    fills = len(fx["fill_asks"])
    if not fx["locked"]:
        n = fx["answered"]
        head = (f"The sheet closes for the {_n(n, 'person', 'people')} who answered" if n > 1 else
                "The sheet closes for the one person who answered" if n else "The sheet closes before anyone answered")
        head += f", and {_n(fills, 'open ask')} close{'s' if fills == 1 else ''}." if fills else "."
    else:
        r, asks = len(fx["rostered"]), len(fx["open_asks"]) + fills
        head = (f"The sheet closes, the {r} rostered lose their seats" if r != 1 else "The sheet closes, the one person rostered loses their seat")
        head += f" and {_n(asks, 'open ask')} close{'s' if asks == 1 else ''}." if asks else "."
        if not asks:
            head = head.replace("The sheet closes, the", "The sheet closes and the", 1)
    return [head, "Nobody gets a DM, but the sheet and every ask card say Cancelled.", "A cancelled run can't be reopened."]


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
        return [f"{name} and {other} swap places and both stay rostered.", "Nobody is told."]
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


def series_words(reg: Registry, key: str) -> str:
    """A series key as a sentence names it: '<raid> <schedule name>' ('Barrow Deeps Raid nights')."""
    rid, _, sid = str(key).partition(":")
    raid = reg.raid_def(rid).get("name", rid) if rid in reg.profile.raids else rid
    s = next((x for x in reg.schedules(rid) if x["id"] == sid), None) if rid in reg.profile.raids else None
    return f"{raid} {s.get('name') or sid}" if s else f"{raid} {sid}"


def regular_remove(reg: Registry, m, key: str) -> list[str]:
    """Taking `m` off the regulars of series `key` (Registry.regular_remove — the Rosters page's remove and the orphan
    clear, Discord's /roster remove, plain text, MCP): no DM and no notice; their sheets stop expecting them and the lock
    stops weighting them as a regular (rc.seat_bonus). A removed schedule's leftovers change nothing else."""
    chars = [c.label for c in m.characters if key in c.rosters] or [m.display_name]
    who = " and ".join(chars)
    if key not in dict(reg.series("recurring")):
        rid = str(key).partition(":")[0]
        raid = reg.raid_def(rid).get("name", rid) if rid in reg.profile.raids else rid
        return [f"{who} stops being a regular of a removed {raid} schedule.", "Nothing else changes, and nobody is told."]
    return [f"{who} stops being a regular of {series_words(reg, key)}.",
            "Its sheets no longer wait for their answer, and the lock no longer weights them as a regular.",
            "They can still join any sheet, and nobody is told."]


__all__ = ["BOARD_VERBS", "CONFIRM_DM", "NAME_TOKEN", "OTHER_TOKEN", "TO_TOKEN", "VERBS", "consequence", "consequences_json", "ended_line",
           "regular_remove", "reopen_sentence", "series_words"]

