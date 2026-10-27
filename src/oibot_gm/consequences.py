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


def _lock(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    from .raid_views import run_times

    if ev.state != "open":
        return ["Already locked."]
    joined = ev.by_status("in")
    seats = rc.run_size(reg, ev) * max(1, rc.run_rosters(reg, ev))
    n = min(len(joined), seats)
    lines = ["The board becomes the roster."]
    if not rc.run_confirms(reg, ev):  # ad hoc (D5): Join is the commitment
        lines.append(rc.NO_CONFIRMATIONS[0].upper() + rc.NO_CONFIRMATIONS[1:] + ".")
    elif n == 0:
        lines.append("Nobody has joined yet — nobody is asked to confirm.")
    else:
        off = _dms_off(reg, (s.discord_id for s in joined))
        _, _, confirm = run_times(reg, ev, rc.run_team(reg, ev))
        lines.append(f"{n} rostered get a confirmation DM" + (f" ({_n(off, 'with DMs off sees it on the site', 'with DMs off see it on the site')})" if off else "")
                     + f". Confirm by {reg.local12(confirm)}.")
    if rc.is_test_run(reg, ev):
        lines.append("Test run: the puppets' DMs arrive in the tester's DMs.")
    return lines


def _cancel(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    """Who the cancel touches, from rc.cancel_effects. Nobody is DMed (the DM rule): the sheet is edited to its final
    state, every open ask card says Cancelled, and the officer may post one fixed line in the signup channel."""
    if ev.state in ("done", "cancelled"):
        return [f"This run is already {ev.state}."]
    fx = rc.cancel_effects(reg, ev)
    tail = "Nobody is DMed: the sheet and every open ask card say Cancelled, and a fixed line goes to the signup channel unless you choose no line."
    fills = len(fx["fill_asks"])
    if not fx["locked"]:
        n = fx["answered"]
        head = f"The sheet closes for the {_n(n, 'person', 'people')} who answered" if n else "The sheet closes; nobody had answered"
        return [head + (f"; {_n(fills, 'ask')} still out close" if fills else "") + ".", tail]
    asks = len(fx["open_asks"])
    return [f"The sheet closes; {_n(len(fx['rostered']), 'rostered seat')} are released" + (f", {_n(asks, 'open confirmation ask')}" if asks else "")
            + (f" and {_n(fills, 'fill ask')}" if fills else "") + (" close" if asks or fills else "") + ".", tail]


def _clear(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    if ev.state == "open":
        placed = sum(len(g) for g in (ev.layout or []))
        if not placed:
            return ["The board is empty — nothing to clear."]
        return [f"{_n(placed, 'placement')} go back to not placed." if placed > 1 else "1 placement goes back to not placed.", "Nobody is told — nothing has been sent before lock."]
    rostered = len(ev.seated())
    if not rostered:
        return ["The roster is empty — nothing to clear."]
    return [f"Every seat is released: {rostered} rostered see it on their confirmation card and the site (no DM), and the bench is asked to fill them."]


def _seen(reg: Registry, ev: rc.RaidEvent) -> str:
    """Where a member whose seat is released sees it (the DM rule: never a DM): their Confirm card is edited when the run
    asks for confirmations (RaidMixin.seat_released), and the site's notice either way (rc.seat_gone)."""
    return "on their confirmation card and the site, no DM" if rc.run_confirms(reg, ev) else "on the site, no DM"


def _refill(reg: Registry, ev: rc.RaidEvent) -> str:
    """What happens to a seat a release leaves open: the bench is asked only when the run's automatic asks are on and
    not paused (RaidMixin.after_board_change / drop_seated → run_fill); otherwise it waits for an officer's Fill seats…."""
    from .raid_buttons import RUN_WORDS

    if not rc.team_setting(rc.run_team(reg, ev), "autofill"):
        return f"the seat waits for {RUN_WORDS['fill']}"
    if ev.autofill_paused:
        return f"automatic asks are paused, so the seat waits for {RUN_WORDS['fill']}"
    return "the bench is asked to fill it"


def _asked(reg: Registry, ev: rc.RaidEvent, who: str) -> str:
    """What someone brought onto a locked roster is told (RaidMixin.confirm_one): a Confirm / Can't DM — or the ask on
    the site with their DMs off — when the run asks for confirmations; nothing on an ad hoc run (D5)."""
    if not rc.run_confirms(reg, ev):
        return f"{rc.NO_CONFIRMATIONS[0].upper() + rc.NO_CONFIRMATIONS[1:]}: the seat is {who}'s now, with no DM."
    return f"{who} gets a confirmation DM (on the site if their DMs are off); the seat is theirs once they say yes."


def _release(reg: Registry, ev: rc.RaidEvent, name: str | None) -> list[str]:
    if ev.state == "open":
        return ["Nothing is sent before lock — the seat only comes off the board."]
    if name:
        return [f"{name} sees the seat was released — {_seen(reg, ev)} — and {_refill(reg, ev)}."]
    return [f"The seat is released: they see it — {_seen(reg, ev)} — and {_refill(reg, ev)}."]


BEFORE_LOCK = "Before the lock nobody is told — the roster is decided at the lock."


def _board(verb: str, reg: Registry, ev: rc.RaidEvent, name: str, other: str, to: str) -> list[str]:
    """One move on the board (RaidMixin.board_move — a drag or Move to…). Before the lock the board is only the layout the
    lock will use: nobody is told. After it the board IS the roster (rc.apply_layout_locked): moving between groups is
    free, someone brought in from Not rostered is asked to confirm (confirm_one) and someone whose seat goes is released
    (free_seat → their card edited and a notice — never a DM)."""
    if ev.state == "open":
        return {"move": [f"{name} takes an open seat in {to}.", BEFORE_LOCK],
                "roster": [f"{name} takes an open seat in {to}.", BEFORE_LOCK],
                "swap": [f"{name} and {other} trade places.", BEFORE_LOCK],
                "substitute": [f"{name} takes {other}'s place in {to}; {other} goes back to not placed.", BEFORE_LOCK]}[verb]
    if verb == "move":
        return [f"{name} moves to {to} and stays rostered — nobody is told."]
    if verb == "swap":
        return [f"{name} and {other} trade places and both stay rostered — nobody is told."]
    if verb == "roster":
        return [f"{name} takes an open seat in {to}.", _asked(reg, ev, name)]
    # substitute: the seat goes straight to the newcomer, so the bench is not asked for it
    return [f"{other}'s seat goes to {name}: {other} sees it was released — {_seen(reg, ev)}.", _asked(reg, ev, name)]


def _remind(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    """Remind the unanswered (RaidMixin.remind): the Confirm / Can't card again, against the existing ask, for everyone a
    DM can reach; DMs-off asks already wait on the site. The refusal (rc.remind_refusal) is the sentence when it can't go."""
    why = rc.remind_refusal(reg, ev)
    if why:
        return [why]
    rows = rc.open_placement_rows(reg, ev)
    dm = sum(1 for m, a in rows if a.get("channel", "dm") == "dm" and not m.dm_opt_out)
    site = len(rows) - dm
    lines = [f"{_n(dm, 'rostered member')} who {'hasn' if dm == 1 else 'haven'}'t answered {'gets' if dm == 1 else 'get'} the Confirm / Can't DM again — the same card, the same deadline."]
    if site:
        lines.append(f"{_n(site, 'more waits', 'more wait')} on the site with DMs off — no DM.")
    lines.append("Once an hour per run.")
    return lines


def _end(reg: Registry, ev: rc.RaidEvent) -> list[str]:
    """End the run (LootMixin.end_run; the loot council's part of the sentence is LootMixin.end_consequence's): the run
    is marked finished, its sheet and lock card close, and Reopen works for rc.REOPEN_GRACE_HOURS (rc.reopen_window)."""
    if ev.state in ("done", "cancelled"):
        return [f"This run is already {rc.RUN_STATE_WORDS[ev.state]}."]
    return ["The run is marked finished: the sheet and the lock card close and no more fill asks go out.",
            f"Pressed by mistake? Reopen works for {rc.reopen_window()} after."]


def regular_remove(reg: Registry, m, key: str) -> list[str]:
    """Taking `m` off the regulars of series `key` (Registry.regular_remove — the Rosters page's remove and the orphan
    clear, Discord's /roster remove, plain text, MCP): no DM and no notice; their sheets stop expecting them and the lock
    stops weighting them as a regular (rc.seat_bonus). A removed schedule's leftovers change nothing else."""
    chars = [c.label for c in m.characters if key in c.rosters] or [m.display_name]
    who = " and ".join(chars)
    label = reg.series_label(key)
    if key not in dict(reg.series("recurring")):
        return [f"{who} stops being a regular of {label}.", "That schedule no longer runs, so nothing else changes; nobody is told."]
    return [f"{who} stops being a regular of {label}: its sheets no longer expect them or list them as not answered, and at the lock "
            "they no longer get a regular's seat weight.", "They can still Join any sheet; nobody is told."]


__all__ = ["BOARD_VERBS", "NAME_TOKEN", "OTHER_TOKEN", "TO_TOKEN", "VERBS", "consequence", "consequences_json", "regular_remove"]
