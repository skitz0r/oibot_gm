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
    if fx.get("lead"):  # a follow-on has no sheet of its own and the night's sheet stays: only its rosters go
        mine = reg.raid_def(ev.instance).get("name", ev.instance)
        r = len(fx["rostered"])
        head = (f"The {mine} follow-on is called off for the people on it." if not fx["locked"] else
                f"The {mine} follow-on is called off for the one person rostered on it." if r == 1 else
                f"The {mine} follow-on is called off for the {r} people rostered on it.")
        return [head, f"The {fx['lead']} run goes ahead, and nobody gets a DM.", "A cancelled run can't be reopened."]
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
    if fx.get("follow"):
        return [head, f"The {fx['follow']} follow-on is cancelled with it, and nobody gets a DM.", "A cancelled run can't be reopened."]
    return [head, "Nobody gets a DM, but the sheet and every ask card say Cancelled.", "A cancelled run can't be reopened."]


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
    after = {p.id: p for p in reg.check_professions(entries, keep_unmatched=reg.stored_unmatched(c))}
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
            tail = f" with {n} notable recipe{'s' if n != 1 else ''}" if n else ""
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
    it released stays released (discord_people.absence_clear_lines — one sentence per run)."""
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
    what is recorded, one sentence per live sheet it answers (discord_people.absence_lines) and how to undo it."""
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
    return [found, change, "You can change or unlink it at any time."]


def armory_guild_clear_consequence(reg: Registry) -> list[str]:
    """The confirm before unlinking the server's guild (§5.1): every character keeps its link; only "in the guild" goes."""
    old = reg.config.armory
    if old is None:
        return ["No guild is linked to this server."]
    return [f"{old.name} stops being this server's guild in the armory.",
            "Every character keeps its link, and who is in the guild stops being known.",
            "You can link a guild again at any time."]



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
    Registry.move_character): what moves with it — its past loot and attendance among it — who is told, and the way
    back. Each person is named once."""
    whose = f"{m_from.display_name}'s main {c.label}" if c.is_main else f"{m_from.display_name}'s {c.label}"
    return [f"{whose} moves to {m_to.display_name} with its past loot, attendance, signups, wishlist and regular spots.",
            "Both see a notice on their Me page.",
            "You can move it back the same way."]

__all__ = ["BOARD_VERBS", "CONFIRM_DM", "NAME_TOKEN", "NOBODY_DM", "NO_SHEET_CHANGES", "NO_SHEET_IN_DAYS", "OTHER_TOKEN", "OWN_CONFIRM", "OWN_RANK", "PEOPLE_VERBS", "RANK_CHOICES", "TO_TOKEN",
           "VERBS", "absence_consequence", "armory_created_words", "armory_link_consequence", "armory_unlink_consequence", "armory_guild_clear_consequence", "armory_guild_consequence", "back_consequence", "confirm_consequence", "consequence", "consequences_json", "delete_consequence", "dm_consequence", "ended_line",
           "flex_consequence", "forget_consequence", "main_consequence", "memory_off_consequence", "memory_shared_consequence", "move_consequence", "people_consequence", "profession_consequence", "profession_sync_consequence", "rank_consequence", "regular_remove", "reopen_sentence",
           "retire_consequence", "save_consequence", "series_words", "spec_consequence"]

