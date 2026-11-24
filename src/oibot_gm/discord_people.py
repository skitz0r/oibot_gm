"""People verbs on the client (dual interfaces phase 2: dual interface design §3.3, docs/design.md §5.41): the ONE
character verb with its ripple into runs, "I'm back" (an absence cleared, its sheets re-opened), recording an absence, and the owner transfer.

Every surface is a caller: the Me and Members pages (web/api.py), the Discord wizards (/me char, /roster char, /roster
back, the absences card, /gm config owner), plain text (configops' `character` / `absence_clear` ops) and MCP (through
the web routes). The module functions take the client as their first argument and reach for its Discord side through
getattr, so a bare test client (the wizard harness, the web tests' FakeBot) runs the same code minus the cards;
PeopleMixin exposes them as `bot.save_characters`, `bot.im_back`, `bot.record_absence`, `bot.transfer_owner`.

The DM rule (owner decision 2026-10-15) holds throughout: nothing here DMs anyone. A seat a character change frees is
handed back through RaidMixin.drop_seated (its ask card EDITED, never a new DM); a role-flip respec on a locked recurring
run is told to the officers on the run's card and the ops line."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging

from . import ops as ops_mod
from . import raidcycle as rc
from .registry import Member, Registry, RegistryError

log = logging.getLogger(__name__)


def _rs(bot, reg: Registry):
    raids = getattr(bot, "raids", None)
    return raids.store(reg) if raids is not None else None


async def _audit(bot, reg: Registry, by: str, line: str, *, via: str, by_id: int | None, level: str = "info") -> None:
    ops = getattr(bot, "ops", None)
    if ops is None:
        return
    try:
        await ops_mod.audited(ops, reg, by, line, via=via, level=level, by_id=by_id)
    except Exception:  # noqa: BLE001 — the audit line never undoes a saved change
        log.exception("ops audit failed")


async def _call(bot, name: str, *args, **kwargs):
    fn = getattr(bot, name, None)
    if fn is None:
        return None
    try:
        return await fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — a card that can't be redrawn never undoes the change
        log.exception("%s failed", name)
        return None


# ---------------------------------------------------------------- characters

def _runs_of(rs, m: Member) -> list[str]:
    """The live runs a member's character change can rewrite: those with their answer, seat or an ask on them."""
    uid = str(m.discord_id)
    return sorted(ev.key for ev in rs.live() if uid in ev.signups or any(a.discord_id == m.discord_id for a in ev.fill_asks))


@contextlib.asynccontextmanager
async def _board_locks(bot, reg: Registry, keys: list[str]):
    """Hold RaidMixin.board_lock of every run in `keys` (sorted: one order everywhere, no lock cycle) — the lock a run's
    lock_run, board writes, cancel and reopen hold across their awaits — so the ripple's rewrite of a run file waits for
    a lock in flight instead of being overwritten by it (or landing in the middle of it). A client without boards (the
    wizard harness) holds nothing."""
    lock_of = getattr(bot, "board_lock", None)
    async with contextlib.AsyncExitStack() as stack:
        if lock_of is not None:
            for key in sorted(keys):
                await stack.enter_async_context(lock_of(reg, key))
        yield


def character_preview(bot, reg: Registry, uid: int, rows: list[dict]) -> list[str]:
    """The ripple sentences of a table save, nothing written (rc.character_preview over the live runs)."""
    rs = _rs(bot, reg)
    m = reg.members.get(uid)
    if rs is None or m is None:
        return []
    return rc.character_preview(reg, rs, m, rc.character_changes(reg, m, rows))


async def save_characters(bot, reg: Registry, uid: int, rows: list[dict], by: str, *, by_id: int | None = None, officer: bool = False,
                          rev: str | None = None, via: str = "discord", preview: bool = False) -> dict:
    """THE character verb (design §3.3): Registry.save_character_table (validated on a copy first; `rev` = the
    characters_rev the editor loaded; `officer` False refuses rank and confirm) in a worker thread, then the ripple on the
    loop — sheet answers rewritten or cleared, a seat on a retired / deleted / respecced character handed back and the
    bench asked (RaidMixin.drop_seated), a role flip on a locked recurring run kept and told to the officers — and one
    audit line. `preview` writes nothing: the same refusals, and the ripple sentences (consequences.save_consequence —
    also as `sentences`, the confirm's words). Returns {"lines": what changed on the record, "ripple": the sentences,
    "message": one line for a reply}."""
    m = reg.members.get(uid)
    who = m.display_name if m else by
    rs = _rs(bot, reg)
    if preview:
        from .consequences import save_consequence

        ripple = save_consequence(reg, rs, m, rows)  # THE Save confirm: one sentence per fate in the live runs (design §5.58)
        await asyncio.to_thread(reg.check_character_table, uid, rows, by, officer=officer, by_id=by_id, rev=rev)
        return {"lines": [], "ripple": ripple, "sentences": list(ripple),
                "message": f"preview — nothing saved: {who}" + (": " + "; ".join(ripple) if ripple else ": nothing else changes")}

    def locked_save():
        # the changes are read from the record the save applies to — under the same config_lock, so a save that lands
        # in between (another web worker, a Discord write) can't leave the ripple working from a stale record
        with reg.config_lock:
            before = reg.members.get(uid)
            return rc.character_changes(reg, before, rows), reg.save_character_table(uid, rows, by, officer=officer, by_id=by_id, rev=rev)

    changes, lines = await asyncio.to_thread(locked_save)
    m = reg.members.get(uid)
    done: list[str] = []
    if rs is not None and m is not None and changes:
        done = await after_character_change(bot, reg, rs, m, changes, by_id=by_id)
    message = (f"{who}: " + "; ".join(lines)) if lines else f"{who}: no changes"
    if lines:
        await _audit(bot, reg, by, message + ("" if not done else " — " + "; ".join(done)), via=via, by_id=by_id)
    return {"lines": lines, "ripple": done, "message": message + ("".join(f"\n{d}" for d in done))}


async def after_character_change(bot, reg: Registry, rs, m: Member, changes: list[dict], *, by_id: int | None = None) -> list[str]:
    """The Discord side of a saved table's ripple (rc.after_character_change does the run files): asks close
    (release_asks_of), seats go back through drop_seated (free_seat + the ask card edited + the fill engine), a kept seat
    is told to the officers on the run's updates and the ops line — no DM — and every touched sheet and card is redrawn.
    `by_id` = who saved: the member's own save writes them no "seat released" notice about their own act (ids decide)."""
    notice = not (by_id is not None and int(by_id) == int(m.discord_id))
    async with _board_locks(bot, reg, _runs_of(rs, m)):  # the run-file rewrite never interleaves with a lock or a board write
        res = rc.after_character_change(reg, rs, m, changes)
    freed = {ev.key for ev, _why in res["free"]}
    for ev in res["release"]:
        released = await _call(bot, "release_asks_of", reg, rs, ev, m, "their character changed")
        if ev.key not in freed:
            await _call(bot, "refill_after_release", reg, rs, ev, bool(released))
    for ev, why in res["free"]:
        if getattr(bot, "drop_seated", None) is not None:
            await _call(bot, "drop_seated", reg, rs, ev, rc.run_team(reg, ev), m, why, None, **({} if notice else {"notice": False}))
        else:
            rc.free_seat(reg, rs, ev, m, why, notice=notice)
    for ev, text in res["keep"]:
        await _call(bot, "post_run_update", reg, ev, f"⚠️ {m.display_name}: {text}")
        ops = getattr(bot, "ops", None)
        if ops is not None:
            try:
                await ops.emit(reg.config, "warn", f"{m.display_name} respecced — {text}")
            except Exception:  # noqa: BLE001
                log.exception("ops emit failed")
    for ev in res["touched"]:
        if ev.key in freed:
            continue  # drop_seated redrew them
        await _call(bot, "refresh_sheet", reg, ev)
        if ev.cards:
            await _call(bot, "refresh_cards", reg, ev)
    return res["lines"]


# ---------------------------------------------------------------- absences

LINEUP_NAME_WORDS = 7
"""An event's lineup names it in an absence sentence ('Hyjal Summit, then Barrow Deeps × 2') up to this many words;
a longer lineup is 'the <first raid> event', so every sentence stays within the Confirm rule's twenty words."""


def _event_names(reg: Registry, evs: list) -> dict[str, str]:
    """Each event (by its first raid's key) as an absence sentence names it, once: its lineup's words or 'the <first
    raid> event', then ' on <day>'; two events that would read the same name their start time too."""
    names = {}
    for ev in evs:
        lineup = rc.event_lineup(reg, ev) or [{"raid": ev.instance}]
        first = rc.lineup_words(reg, [{"raid": lineup[0].get("raid")}])
        whole = rc.lineup_words(reg, lineup)
        short = first if len(lineup) == 1 else f"the {first} event"
        names[ev.key] = (whole if len(lineup) > 1 and len(whole.split()) <= LINEUP_NAME_WORDS else short, short, ev)
    seen: dict[tuple, int] = {}
    for words, _s, ev in names.values():
        seen[(words, rc.run_day(reg, ev))] = seen.get((words, rc.run_day(reg, ev)), 0) + 1
    out = {}
    for key, (words, short, ev) in names.items():
        day = reg.day_label(rc.run_day(reg, ev))
        if seen[(words, rc.run_day(reg, ev))] > 1:  # two events that would read the same: the short name and the time
            out[key] = f"{short} at {reg.local12(ev.start, '%I:%M %p')} on {day}"
        else:
            out[key] = f"{words} on {day}"
    return out


def _span_words(reg: Registry, start: str, end: str) -> str:
    return "that day" if start == end else "in those days"


def absence_lines(reg: Registry, rs, m: Member | None, start: str, end: str) -> list[str]:
    """What recording an absence does to each live event, one sentence per EVENT (a first raid and its later raids are
    one event, named once: 'The sheet for Barrow Deeps on Tue 6 Oct is answered No thanks.') — the Confirm rule's words
    (design §5.57). When those pass three sentences in all, they are summed: a released seat is always said (a loss),
    then the answers that become No thanks as one count."""
    if rs is None:
        return []
    hits = [(ev, what) for ev, what in rc.absence_effects(reg, rs, m, start, end) if not rc.is_follow(ev)]
    names = _event_names(reg, [ev for ev, _w in hits])
    out = []
    for ev, what in hits:
        run = names[ev.key]
        if what == "seat freed":
            out.append(f"The seat for {run} is released. {rc.refill_sentence(reg, ev)}")
        elif what == "already No thanks":
            out.append(f"The sheet for {run} already says No thanks.")
        else:
            out.append(f"The sheet for {run} is answered No thanks.")
    if len(hits) + sum(1 for _e, w in hits if w == "seat freed") <= 3:
        return out
    return _absence_sum(reg, names, hits, _span_words(reg, start, end))


def _absence_sum(reg: Registry, names: dict[str, str], hits: list, span: str) -> list[str]:
    """Four events or more: at most three sentences. A released seat first (one event by name, several as a count) with
    who fills it, then the sheets answered No thanks as a count; the sheets that already said so only when room is left."""
    from .consequences import count_words

    freed = [ev for ev, w in hits if w == "seat freed"]
    answered = sum(1 for _e, w in hits if w == "No thanks")
    already = sum(1 for _e, w in hits if w == "already No thanks")
    out = []
    if len(freed) == 1:
        out.append(f"The seat for {names[freed[0].key]} is released. {rc.refill_sentence(reg, freed[0])}")
    elif freed:
        fills = {rc.refill_sentence(reg, ev, "them") for ev in freed}
        tail = fills.pop() if len(fills) == 1 else "Each event fills its seat the way its own asks are set."
        out.append(f"The seats for {count_words(len(freed))} events {span} are released. {tail}")
    other = "other " if freed else ""
    if answered:
        out.append(f"The sheets for {count_words(answered)} {other}event{'s' if answered != 1 else ''} {span} are answered No thanks."
                   if answered != 1 else f"The sheet for one {other}event {span} is answered No thanks.")
    if already and len(out) + (1 if freed else 0) < 3:
        out.append(f"The sheets for {count_words(already)} more events already say No thanks." if already != 1 else "The sheet for one more event already says No thanks.")
    return out


def absence_clear_lines(reg: Registry, rs, m: Member, a) -> list[str]:
    """What "I'm back" does, without doing it (RaidMixin.after_absence_cleared does it): an open sheet the absence had
    answered No thanks re-opens for them; a seat it handed back stays handed back. One sentence per EVENT (its first
    raid holds the sheet); more than three are summed into two counts."""
    if rs is None:
        return []
    hits = []
    for ev in rs.live():
        if rc.is_follow(ev):  # a later raid's answers are its first raid's
            continue
        day = rc.run_day(reg, ev)
        if not (a.start <= day <= a.end) or any(x is not a and x.start <= day <= x.end for x in m.absences):
            continue
        sg = ev.signups.get(str(m.discord_id))
        if not sg or sg.status != "out" or sg.source != "absence":
            continue
        hits.append(ev)
    names = _event_names(reg, hits)
    if len(hits) <= 3:
        return [f"The sheet for {names[ev.key]} opens again for an answer." if ev.state == "open"
                else f"The seat for {names[ev.key]} that the absence released stays released." for ev in hits]
    from .consequences import count_words

    opens = sum(1 for ev in hits if ev.state == "open")
    stays = len(hits) - opens
    out = []
    if opens:
        out.append(f"The sheets for {count_words(opens)} events open again for an answer." if opens != 1 else "The sheet for one event opens again for an answer.")
    if stays:
        out.append(f"The seats the absence released on {count_words(stays)} events stay released." if stays != 1 else "The seat the absence released on one event stays released.")
    return out


async def record_absence(bot, reg: Registry, uid: int, start: str, end: str | None, reason: str | None, by: str, *, by_id: int | None = None,
                         display_name: str | None = None, via: str = "discord") -> tuple[Member, object, list[str]]:
    """Registry.add_absence (overlap refused, reason capped, a puppet only by its tester) + announce_absence (sheets
    marked, a rostered seat handed back, the absences card) — the absences card, /me absent add, /roster absent, the Me
    and Members pages, plain text and MCP. Returns (member, absence, touched run keys)."""
    m, a = await asyncio.to_thread(reg.add_absence, uid, start, end, reason, by, display_name, by_id=by_id)
    touched = await _call(bot, "announce_absence", reg, m, a, by, by_id=by_id) or []  # by_id: their own act writes them no notice
    return m, a, list(touched)


async def im_back(bot, reg: Registry, uid: int, start: str, by: str, *, by_id: int | None = None, via: str = "discord") -> tuple[object, list[str]]:
    """THE "I'm back" verb (the absences card, /me absent clear, /roster back, the Me and Members pages, plain text's
    absence_clear, MCP clear_absence): the absence starting `start` goes and the sheets it had answered No thanks on
    re-open for them (absence_cleared → after_absence_cleared). Returns (the absence, the member-facing lines)."""
    m = reg.members.get(uid)
    if m is None:
        raise RegistryError("They have nothing on file.")
    a = await asyncio.to_thread(functools.partial(reg.clear_absence, uid, start, by, by_id=by_id))  # a puppet only by its tester
    lines = list(await _call(bot, "absence_cleared", reg, m, a, by) or [])
    return a, lines


# ---------------------------------------------------------------- owner transfer (D50)

async def transfer_owner(bot, reg: Registry, uid: int, by: str, *, by_id: int | None, name: str | None = None, is_bot: bool | None = None,
                         membership: str | None = None, via: str = "discord") -> str:
    """Hand the guild to `uid` — the /gm config owner decide screen (Discord only, design §2.1). The caller checked the presser
    is the owner (Registry.is_owner_id: the owner or a host superuser). Refused (Registry.owner_refusal): a bot, the
    current owner, yourself, someone outside the server or a Discord lookup that can't say. One warn-level audit line;
    nobody is DMed (D50, the DM rule). `membership`: the Discord answer the caller already has (the wizard's member
    picker hands over a server member); None = look it up now."""
    from . import guild_routing

    answer, member = (membership, None) if membership else await guild_routing.member_of(bot, reg, uid, timeout=None)
    if member is not None:
        name = name or getattr(member, "display_name", None)
        is_bot = bool(getattr(member, "bot", False)) if is_bot is None else is_bot
    line = await asyncio.to_thread(reg.set_owner, uid, by, by_id=by_id, bot=bool(is_bot), membership=answer, name=name)
    await _audit(bot, reg, by, f"owner → {name or uid}", via=via, by_id=by_id, level="warn")
    return line


class PeopleMixin:
    """The people verbs on the client (OibotGM gets them through AbsencesMixin)."""

    async def save_characters(self, reg: Registry, uid: int, rows: list[dict], by: str, **kw) -> dict:
        return await save_characters(self, reg, uid, rows, by, **kw)

    async def im_back(self, reg: Registry, uid: int, start: str, by: str, **kw):
        return await im_back(self, reg, uid, start, by, **kw)

    async def record_absence(self, reg: Registry, uid: int, start: str, end: str | None, reason: str | None, by: str, **kw):
        return await record_absence(self, reg, uid, start, end, reason, by, **kw)

    async def transfer_owner(self, reg: Registry, uid: int, by: str, **kw) -> str:
        return await transfer_owner(self, reg, uid, by, **kw)


__all__ = ["PeopleMixin", "save_characters", "character_preview", "after_character_change", "record_absence", "im_back", "transfer_owner",
           "absence_lines", "absence_clear_lines"]
