"""What an officer's verb does to people, in one sentence, from code (dual interfaces §2.3).

Every surface that confirms a run verb — the Discord `decide` screen (wizard.Wizard.decide), the site's `useConfirm`
(SheetCard reads `ev_json.consequences`), the plain-text diff and an MCP preview — prints the SAME words, and none of
them composes the sentence itself: the browser never promised a DM the bot would not send (the old SheetCard lock text
said "everyone rostered gets a confirmation DM" on ad hoc runs, where nobody is asked).

`consequence(verb, reg, ev)` → the lines for lock / cancel / clear / release; `consequences_json(reg, ev)` → the four
strings `ev_json` carries. Numbers come from the run as it stands now (who joined, who has DMs off, the layout), so a
preview drawn after the sheet moved says so by its numbers, not by a stale cached line."""
from __future__ import annotations

from . import raidcycle as rc
from .registry import Registry

VERBS = ("lock", "cancel", "clear", "release")
NAME_TOKEN = "{name}"
"""The placeholder in `consequences_json["release_named"]`: the board's drag-to-bank names the member it is about, and
the only thing the browser does to the sentence is put that name where the token is (it never composes the sentence)."""


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def consequence(verb: str, reg: Registry, ev: rc.RaidEvent, *, name: str | None = None) -> list[str]:
    """The sentence(s) the confirm screen shows before `verb` on `ev`. `name`: the member a `release` is about (the
    board's drag-to-bank); without it the sentence stays general. Unknown verbs → []."""
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
    """`ev_json.consequences`: {lock, cancel, clear, release} as one string each (the lines joined by a space), plus
    `release_named`: the release sentence about one member with NAME_TOKEN where the name goes (the board fills it in
    for the member being dragged out; before lock it is the same no-name sentence)."""
    out = {v: " ".join(consequence(v, reg, ev)) for v in VERBS}
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


def _release(reg: Registry, ev: rc.RaidEvent, name: str | None) -> list[str]:
    if ev.state == "open":
        return ["Nothing is sent before lock — the seat only comes off the board."]
    told = "on their confirmation card and the site, no DM"
    if name:
        return [f"{name} sees the seat was released — {told} — and the bench is asked to fill it."]
    return [f"The seat is released: they see it — {told} — and the bench is asked to fill it."]


__all__ = ["NAME_TOKEN", "VERBS", "consequence", "consequences_json"]
