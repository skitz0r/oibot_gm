"""A character's armory link and the check that keeps it current (armory link, spec §5.2 / §5.3; design §5.64).

The verbs — `find_for_link`, `link_character`, `unlink_character`, `set_profession_sync`, `sync_character` (Sync now) —
and the engine — `sync_guild` (hourly), `roster_refresh` (every three hours), `armory_tick` (the scheduler's hook) — are
module functions taking the client, like discord_people.py: they reach for its Discord side through getattr, so a bare
test client runs the same code minus the cards. Every surface (the Discord wizards, the site's routes, MCP — Tasks
12–14) is a thin caller of these.

The Registry holds the writes (armory_holder / link_refusal / link_armory / unlink_armory / set_profession_sync /
armory_update — one verb per fact, one commit each); the rename is Registry.rename_character, run on the loop under the
member's runs' board locks with the bot's own RaidStore, so live sheets take the new name and are redrawn. Requests to
the armory run in `asyncio.to_thread` (armory.Armory blocks), under one asyncio lock per guild key: a guild's run, its
roster read and a link never interleave; Sync now waits for the lock.

What is kept in memory only (per guild key; a restart forgets it, and nothing here is in the data repo): the last check
time and the last Last-Modified read per armory character (a check that finds nothing new writes nothing — the stored
`checked_at` / `modified` move only with a commit that happens anyway), Sync now's 5-minute limit, the hourly warning
throttle, the roster's last try. The roster snapshot is derived, foreign data: `out/armory/<guild key>/roster.json`
(the rows, when taken, the roster's Last-Modified, the guild id), loaded at first use after a restart.

The DM rule holds throughout: nothing here DMs anyone or writes a notice. Failures reach the guild's ops feed: at most
one warning per guild per hour for the armory being down / refusing the keys / answering oddly (the run stops, the last
data stays, the next hourly tick resumes), once per stranger guild under the server's guild address, once per recipe id
the game's list lacks ("the recipe list is behind the game")."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import armory
from . import ops as ops_mod
from . import raidcycle as rc
from .armory import ArmoryError, ArmoryMissing, RosterRow
from .registry import ArmoryLink, Member, RegisteredCharacter, Registry, RegistryError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "armory"  # read at call time (tests point it elsewhere)

SYNC_EVERY = timedelta(hours=1)  # each linked character's conditional check (D10)
ROSTER_EVERY = timedelta(hours=3)  # the roster (Blizzard's own cache is 3 h)
SYNC_NOW_EVERY = timedelta(minutes=5)  # Sync now, per character
WARN_EVERY = timedelta(hours=1)  # the armory down / refusing / odd: one ops warning per guild per hour
MISSING_AFTER = timedelta(hours=24)  # "the armory no longer finds it" is shown to officers after a day

NOT_FOUND = "The armory has no character by that name. Reach level 10 and log out once, then try again."
SYNC_LIMITED = "Updated a moment ago. Try again in a few minutes."
# a read view's empty state (the site's map / seed, Discord's /roster armory): what is missing and how it fills
NO_GUILD_YET = "No guild in the game is linked to this server yet. The owner links it on the Config page or with /gm config armory."
NOT_READ_YET = "The guild's roster hasn't been read from the armory yet, and the bot keeps trying."
LOOKUPS_PER_MINUTE = 6  # armory lookups per person a minute (the site's find / link, Discord's link box): never the host's quota
TOO_MANY_LOOKUPS = "Wait a moment before looking up another name."
# the refusal when the presser is neither the character's member nor an officer (rights decided live: PRIV_LIVE)
OFFICER_ONLY = {"link": "Only an officer links another member's character.",
                "unlink": "Only an officer unlinks another member's character.",
                "switch": "Only an officer changes how another member's professions are kept.",
                "sync": "Only an officer updates another member's character from the armory.",
                "move": "Only an officer moves a character to another member."}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


@dataclass(frozen=True)
class Found:
    """An armory character a person may link: from its profile (level 10 and up), else the linked guild's roster."""
    id: int
    slug: str  # the armory's address, unquoted and lowercase ('ébba-thistle')
    name: str
    surname: str | None
    cls: str | None  # the profile's class key, None for a class the profile's table lacks
    level: int
    source: str  # "profile" | "roster"
    guild_id: int | None = None  # the guild the armory puts it in (profile), the linked guild (roster)
    modified: str | None = None  # the profile's Last-Modified (None from the roster)

    @property
    def label(self) -> str:
        return f"{self.name} {self.surname}" if self.surname else self.name


@dataclass
class _Snapshot:
    rows: list[RosterRow]
    taken_at: str  # UTC ISO
    modified: str | None
    guild_id: int


# ------------------------------------------------------------------ in-memory state (per guild key)

_STATE: dict[str, dict] = {}
_LOCKS: dict[str, tuple[object, asyncio.Lock]] = {}
_UNLOADED = object()


def reset_state() -> None:
    """Forget everything kept in memory (a restart; tests)."""
    _STATE.clear()
    _LOCKS.clear()


def _st(reg) -> dict:
    return _STATE.setdefault(reg.key, {"checked": {}, "modified": {}, "forced": {}, "warned_at": {}, "strangers": set(),
                                       "reported": None, "roster": _UNLOADED, "roster_try": None, "last_sync": None, "task": None})


def lookup_allowed(reg, by_id) -> bool:
    """THE lookup limit: at most LOOKUPS_PER_MINUTE armory lookups a minute per person per guild (memory; a refusal is
    TOO_MANY_LOOKUPS — the site answers it as a 429, Discord on the link box's screen). A lookup allowed is counted."""
    now = time.monotonic()
    looked = _st(reg).setdefault("looked", {})
    key = int(by_id) if by_id is not None else None
    recent = [t for t in looked.get(key, []) if now - t < 60]
    if len(recent) >= LOOKUPS_PER_MINUTE:
        looked[key] = recent
        return False
    looked[key] = [*recent, now]
    return True


def _lock(reg) -> asyncio.Lock:
    """One lock per guild key, for the running loop (a lock never crosses event loops)."""
    loop = asyncio.get_running_loop()
    held = _LOCKS.get(reg.key)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _LOCKS[reg.key] = held
    return held[1]


def _norm(slug: str | None) -> str:
    return urllib.parse.unquote(str(slug or "")).lower()


def last_checked(reg, c: RegisteredCharacter) -> str | None:
    """When the armory was last asked about `c` (UTC ISO): this process's last check, else the stored one — what
    "Updated from the armory <when>" shows. None for a character that isn't linked."""
    if c.armory is None:
        return None
    seen = _st(reg)["checked"].get(c.armory.id)
    stored = c.armory.checked_at
    return max(x for x in (seen, stored) if x) if (seen or stored) else None


def updated_words(reg, c: RegisteredCharacter) -> str:
    """'Updated from the armory Thu 12 Nov 3:00 PM' (Registry.local12), or the words for a link not checked yet."""
    at = last_checked(reg, c)
    return f"Updated from the armory {reg.local12(at)}" if at else "Not updated from the armory yet"


def in_guild(reg, c: RegisteredCharacter) -> bool | None:
    """In the server's guild — DERIVED, never stored (R9): True when the character's armory id is on the last roster read
    of the guild linked NOW, False when that roster was read and it is not there, None (not known) when the character
    isn't linked, no guild is linked, or that guild's roster hasn't been read yet. A guild linked, switched or unlinked
    changes the answer at once, with no write."""
    if c.armory is None:
        return None
    snap = _load_snapshot(reg)  # only the snapshot of the guild linked now answers
    if snap is None:
        return None
    return any(r.id == c.armory.id for r in snap.rows)


def flags(reg, c: RegisteredCharacter) -> list[str]:
    """What officers see on a linked character (spec §5.4): "class" (the armory's class differs — never written),
    "missing" (the armory has not found it for a day), "outside" (not in the server's guild — information, not an error)."""
    a = c.armory
    if a is None:
        return []
    out = []
    if a.cls and a.cls != c.cls:
        out.append("class")
    if a.missing_since and _now() - datetime.fromisoformat(a.missing_since) >= MISSING_AFTER:
        out.append("missing")
    if in_guild(reg, c) is False:
        out.append("outside")
    return out


# ------------------------------------------------------------------ the roster snapshot (out/, never the data repo)


def _snap_path(reg) -> Path:
    return OUT / reg.key / "roster.json"


def _load_snapshot(reg) -> _Snapshot | None:
    st = _st(reg)
    if st["roster"] is _UNLOADED:
        snap = None
        try:
            d = json.loads(_snap_path(reg).read_text(encoding="utf-8"))
            snap = _Snapshot(rows=[RosterRow(**r) for r in d["rows"]], taken_at=str(d["taken_at"]), modified=d.get("modified"),
                             guild_id=int(d["guild_id"]))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a damaged derived file is read again from the armory
            log.warning("armory roster snapshot for %s unreadable; it is read again", reg.key)
        st["roster"] = snap
    snap = st["roster"]
    g = reg.config.armory
    return snap if snap is not None and g is not None and snap.guild_id == int(g.guild_id) else None


ROSTER_LISTENERS: list = []  # fn(guild key) after every roster read is saved (the web publishes `member`: the map's "in the guild")


def _save_snapshot(reg, snap: _Snapshot) -> None:
    _st(reg)["roster"] = snap
    p = _snap_path(reg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"taken_at": snap.taken_at, "modified": snap.modified, "guild_id": snap.guild_id,
                                   "rows": [asdict(r) for r in snap.rows]}, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        log.exception("armory roster snapshot for %s not saved", reg.key)
    for fn in list(ROSTER_LISTENERS):
        try:
            fn(reg.key)
        except Exception:  # noqa: BLE001 — a listener never stops the roster read
            log.exception("armory roster listener failed")


def roster_snapshot(reg) -> tuple[list[RosterRow], str | None]:
    """The linked guild's roster as last read (rows, when — UTC ISO; the caller words it with Registry.local12), or
    ([], None): no guild linked, or not read yet."""
    snap = _load_snapshot(reg)
    return (list(snap.rows), snap.taken_at) if snap else ([], None)


def _roster_row(reg, armory_id: int) -> RosterRow | None:
    snap = _load_snapshot(reg)
    return next((r for r in snap.rows if r.id == int(armory_id)), None) if snap else None


def unclaimed(reg) -> list[RosterRow]:
    """The guild's characters nobody here has linked ("In the guild, not claimed"), as the last roster read shows them."""
    return [r for r in roster_snapshot(reg)[0] if reg.armory_holder(r.id) is None]


# ------------------------------------------------------------------ ops lines


async def _emit(bot, reg, level: str, text: str) -> None:
    ops = getattr(bot, "ops", None)
    if ops is None:
        log.warning("[%s] %s", reg.key, text)
        return
    try:
        await ops.emit(reg.config, level, text)
    except Exception:  # noqa: BLE001 — an ops line never stops the check
        log.exception("ops emit failed")


async def _warn_hourly(bot, reg, kind: str, text: str) -> None:
    """At most one ops warning per guild per hour for each `kind` ("armory" = down / refusing the keys / answering oddly,
    "roster-missing", "professions:<armory id>", "name:<armory id>"): one character's warning never hides another's, nor
    an outage."""
    warned = _st(reg)["warned_at"]
    last = warned.get(kind)
    if last is not None and _now() - last < WARN_EVERY:
        log.info("[%s] %s (warned this hour already)", reg.key, text)
        return
    warned[kind] = _now()
    await _emit(bot, reg, "warn", text)


async def _audit(bot, reg, by: str, line: str, *, via, by_id) -> None:
    ops = getattr(bot, "ops", None)
    if ops is None:
        return
    try:
        await ops_mod.audited(ops, reg, by, line, via=via or "discord", by_id=by_id)
    except Exception:  # noqa: BLE001 — the audit line never undoes a saved change
        log.exception("ops audit failed")


def _reported_path(reg) -> Path:
    return OUT / reg.key / "recipes_reported.json"


async def _report_unknown_recipes(bot, reg, m: Member, c: RegisteredCharacter, profs) -> None:
    """A recipe id the game's list lacks is kept by its id; it is told to ops ONCE per guild (kept under out/ so a
    restart doesn't tell it again): the recipe list is behind the game."""
    if not profs or not reg.profile.has_recipe_list:  # without the game's list there is nothing to be behind
        return
    st = _st(reg)
    if st["reported"] is None:
        try:
            st["reported"] = set(json.loads(_reported_path(reg).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            st["reported"] = set()
    new = [(p, r) for p in profs for r in p.recipes if r.id is not None and r.id not in reg.profile.recipes and r.id not in st["reported"]]
    for p, r in new:
        st["reported"].add(r.id)
        await _emit(bot, reg, "warn", f"The recipe list is behind the game: {reg.profile.profession(p.id).name} {r.name} "
                                      f"(id {r.id}) on {m.display_name}'s {c.label} is kept by its id.")
    if new:
        try:
            _reported_path(reg).parent.mkdir(parents=True, exist_ok=True)
            _reported_path(reg).write_text(json.dumps(sorted(st["reported"])), encoding="utf-8")
        except OSError:
            log.exception("armory reported recipes for %s not saved", reg.key)


# ------------------------------------------------------------------ rights and the rename


async def _officer(bot, reg, by_id) -> bool:
    from . import guild_routing

    if by_id is None:
        return False
    return (await guild_routing.resolve_role(bot, reg, int(by_id), max_age=guild_routing.PRIV_LIVE)).role in ("owner", "officer")


async def _rights(bot, reg, uid: int, by_id, verb: str) -> bool:
    """Own character, or an officer (decided live). → whether the presser is an officer (it picks a refusal's words)."""
    officer = await _officer(bot, reg, by_id)
    if not officer and (by_id is None or int(by_id) != int(uid)):
        raise RegistryError(OFFICER_ONLY[verb])
    return officer


def _rs(bot, reg):
    raids = getattr(bot, "raids", None)
    return raids.store(reg) if raids is not None else rc.RaidStore(reg.store, reg.key)


def _character(reg, uid: int, label: str) -> tuple[Member, RegisteredCharacter]:
    m = reg.members.get(uid)
    if m is None:
        raise RegistryError("They have no registered characters yet.")
    c = reg.own_character([x for x in m.active() if x.name], label)
    if c is None:
        raise RegistryError(f"{m.display_name} has no character named {str(label or '').strip() or '?'}.")
    return m, c


character_of = _character  # the public name (the site's routes): (member, named active character) or the verbs' refusal


async def _redraw(bot, reg, rs, keys: list[str]) -> None:
    for key in dict.fromkeys(keys):
        ev = rs.events.get(key)
        if ev is None:
            continue
        for name in ("refresh_sheet", "refresh_cards") if ev.cards else ("refresh_sheet",):
            fn = getattr(bot, name, None)
            if fn is None:
                continue
            try:
                await fn(reg, ev)
            except Exception:  # noqa: BLE001 — a sheet that can't be redrawn never undoes the change
                log.exception("%s failed", name)


async def _write_named(bot, reg, uid: int, label: str, name: str, surname: str | None, by: str, write, *, message: str,
                       by_id=None, officer: bool = True):
    """`write()` (a Registry write of this member) with the character renamed first when the armory's name differs —
    both in ONE commit (`message`), on the loop under the member's runs' board locks with the bot's own RaidStore (the
    rename rewrites live run files), then the sheets it touched redrawn. Returns write()'s result."""
    from .discord_people import _board_locks, _runs_of

    rs = _rs(bot, reg)
    m = reg.members[uid]
    keys: list[str] = []
    async with _board_locks(bot, reg, _runs_of(rs, m)):
        with reg.store.batch(f"{reg.key}: {message}"):
            reg.rename_character(uid, label, name, surname, by, rs=rs, by_id=by_id, officer=officer, relabelled=keys)
            out = write()
    await _redraw(bot, reg, rs, keys)
    return out


def _trusted(reg, raw) -> list | None:
    """The armory's professions as the record's (check_professions(trusted=True): the armory is the truth for a synced
    character — no trade-slot, skill-cap or on-the-list check; an id the list lacks is kept with the armory's name)."""
    if raw is None:
        return None
    entries = [{"id": p["id"], "skill": p["skill"] if int(p.get("skill") or 0) >= 1 else None,
                "recipes": [{"id": r["id"], "name": r["name"]} for r in p.get("recipes") or []]} for p in raw]
    return reg.check_professions(entries, trusted=True)


# ------------------------------------------------------------------ finding and linking


async def find_for_link(bot, reg, name: str, surname: str | None) -> Found:
    """The armory character a person means (spec §5.2 "How"): its profile first, else — below level 10, or before its
    first logout — the linked guild's roster (read if the snapshot is older than three hours). No gate on the guild.
    Raises RegistryError: an invalid name (prose, validated), or NOT_FOUND; an ArmoryError (`.words`) when the armory
    can't answer. Writes nothing."""
    name = reg.normalise_name(str(name or ""))
    surname = reg.normalise_name(str(surname)) if surname and str(surname).strip() else None
    cli = armory.client_for(reg)
    slug = armory.address(name, surname)
    try:
        f = await asyncio.to_thread(cli.character, slug)
    except ArmoryMissing:
        f = None
    if f is not None and f is not armory.NOT_MODIFIED:
        return Found(id=f.id, slug=_norm(slug), name=f.name, surname=f.surname, cls=f.cls, level=f.level, source="profile",
                     guild_id=f.guild_id, modified=f.modified)
    g = reg.config.armory
    rows = await roster_refresh(bot, reg) if g is not None else None
    want = (name.lower(), (surname or "").lower())
    row = next((r for r in rows or [] if (r.name.lower(), (r.surname or "").lower()) == want), None)
    if row is None:
        raise RegistryError(NOT_FOUND)
    return Found(id=row.id, slug=_norm(row.slug), name=row.name, surname=row.surname, cls=row.cls, level=row.level, source="roster",
                 guild_id=int(g.guild_id))


async def link_character(bot, reg, uid: int, label: str, found: Found, by: str, *, by_id, via=None, preview: bool = False):
    """THE link (spec §5.2): member `uid`'s character `label` follows armory character `found` — it takes the armory's
    name (Registry.rename_character: the old name stays an alias) and, from a profile, the armory's professions replace
    its own; the first check's facts are stored. Rights: their own character, or an officer (live). Refused, nothing
    written: link_refusal (a second claim names the holder to an officer only; a name another record holds) or the
    armory not answering. `preview` → the confirm sentences (consequences.armory_link_consequence), nothing written.
    The rename and the link are ONE commit; one audit line; no DM, no notice. → the linked character."""
    from .consequences import armory_link_consequence

    officer = await _rights(bot, reg, uid, by_id, "link")
    m, c = _character(reg, uid, label)
    why = reg.link_refusal(uid, c.label, found.id, found.name, found.surname, by_id=by_id, officer=officer)
    if why:
        raise RegistryError(why)
    if preview:
        return armory_link_consequence(reg, m, c, found, by_id)
    async with _lock(reg):
        m, c = _character(reg, uid, label)  # again: the record may have moved while the lock was waited for
        old = c.label
        why = reg.link_refusal(uid, old, found.id, found.name, found.surname, by_id=by_id, officer=officer)
        if why:
            raise RegistryError(why)
        profs = None
        if found.source == "profile":
            try:
                profs = _trusted(reg, await asyncio.to_thread(armory.client_for(reg).professions, found.slug))
            except ArmoryMissing:
                profs = None
        at = _iso(_now())
        link = ArmoryLink(id=found.id, slug=_norm(found.slug), linked_by=by, linked_at=at, checked_at=at, modified=found.modified,
                          level=found.level, cls=found.cls, missing_since=None)

        def write():
            return reg.link_armory(uid, found.label, link, by, professions=profs, by_id=by_id, officer=officer)

        c = await _write_named(bot, reg, uid, old, found.name, found.surname, by, write,
                               message=f"{m.display_name}: {found.label} linked to the armory (by {by})", by_id=by_id, officer=officer)
        st = _st(reg)
        st["checked"][found.id] = at
        if found.modified:
            st["modified"][found.id] = found.modified
    await _report_unknown_recipes(bot, reg, m, c, profs)
    named = f" as {c.label}" if c.label != old else ""
    await _audit(bot, reg, by, f"linked {m.display_name}'s {old} to the armory{named} (id {found.id})", via=via, by_id=by_id)
    return c


async def unlink_character(bot, reg, uid: int, label: str, by: str, *, by_id, via=None, preview: bool = False):
    """THE unlink (spec §5.2): the record keeps what it last had and its professions are kept by hand from now on.
    Own character or an officer. `preview` → consequences.armory_unlink_consequence. One commit, one audit line."""
    from .consequences import armory_unlink_consequence

    await _rights(bot, reg, uid, by_id, "unlink")
    m, c = _character(reg, uid, label)
    if c.armory is None:
        raise RegistryError(f"{c.label} isn't linked to the armory.")
    if preview:
        return armory_unlink_consequence(reg, m, c, by_id)
    async with _lock(reg):
        m, c = _character(reg, uid, label)  # again: another unlink may have landed while the lock was waited for
        if c.armory is None:
            raise RegistryError(f"{c.label} isn't linked to the armory.")
        aid = c.armory.id
        c = await asyncio.to_thread(reg.unlink_armory, uid, label, by, by_id=by_id)
    await _audit(bot, reg, by, f"unlinked {m.display_name}'s {c.label} from the armory (id {aid})", via=via, by_id=by_id)
    return c


async def set_profession_sync(bot, reg, uid: int, label: str, on: bool, by: str, *, by_id, via=None, preview: bool = False):
    """THE switch (D5, spec §5.2): off = this character's professions are kept by hand (edited from the game's list);
    on = the armory's replace the hand edits — at once (an unconditional check, outside Sync now's limit; if the armory
    can't answer, the next hourly check does it). Own character or an officer. `preview` →
    consequences.profession_sync_consequence. → the character."""
    from .consequences import profession_sync_consequence

    await _rights(bot, reg, uid, by_id, "switch")
    m, c = _character(reg, uid, label)
    if c.armory is None:
        raise RegistryError(f"{c.label} isn't linked to the armory.")
    if preview:
        return profession_sync_consequence(bool(on), c.label, current=c.profession_sync)
    changed = await asyncio.to_thread(reg.set_profession_sync, uid, label, bool(on), by, by_id=by_id)
    if changed:
        await _audit(bot, reg, by, f"{m.display_name}'s {c.label}: professions " + ("follow the armory" if on else "kept by hand"),
                     via=via, by_id=by_id)
        if on:
            try:
                async with _lock(reg):
                    await _sync_one(bot, reg, uid, c.armory.id, force=True)
            except ArmoryError as e:
                log.info("[%s] professions follow the armory again; the check waits: %s", reg.key, e)
    return reg.armory_character(uid, c.armory.id) or c


async def move_character(bot, reg, from_id: int, label: str, to_id: int, by: str, *, by_id, via=None, preview: bool = False):
    """THE move to another member (spec §5.4, the owner's decision 2026-11-12): member `from_id`'s character `label` becomes
    member `to_id`'s, and everything follows it — the record (Registry.move_character), its past loot and attendance
    (resolved through the moves file, Registry.owner_of), its signups and seats on live runs. Officers only, decided live
    (PRIV_LIVE); moving a character to yourself is refused by the verb (the owner aside). Refused, nothing written:
    Registry.move_refusal's sentence. `preview` → consequences.move_consequence, nothing written. The write runs on the
    loop under the board locks of every live run it may touch, with the bot's own RaidStore, then those sheets are
    redrawn; one audit line. Both members get a notice (the verb writes them), nobody a DM. → the character."""
    from .consequences import move_consequence
    from .discord_people import _board_locks

    if not await _officer(bot, reg, by_id):
        raise RegistryError(OFFICER_ONLY["move"])
    rs = _rs(bot, reg)
    why = reg.move_refusal(from_id, label, to_id, rs=rs, by_id=by_id)
    if why:
        raise RegistryError(why)
    m_from, m_to = reg.members[int(from_id)], reg.members[int(to_id)]
    c = reg.own_character([x for x in m_from.characters if x.name], label)
    if preview:
        return move_consequence(reg, m_from, c, m_to)
    keys: list[str] = []
    async with _board_locks(bot, reg, reg.move_runs(from_id, label, to_id, rs=rs)):
        c = reg.move_character(from_id, label, to_id, by, rs=rs, by_id=by_id, moved=keys)  # re-checks every refusal
    await _redraw(bot, reg, rs, keys)
    await _audit(bot, reg, by, f"moved {m_from.display_name}'s {c.label} to {m_to.display_name}", via=via, by_id=by_id)
    return c


# ------------------------------------------------------------------ the check (spec §5.3)


async def sync_character(bot, reg, uid: int, label: str, *, force: bool = False, by_id=None) -> str:
    """One character's check. `force` = Sync now: unconditional, at most once per character per 5 minutes ("limited" —
    say SYNC_LIMITED). `by_id` (a person pressing Sync now) → own character or an officer, else the caller decided.
    → "unchanged" | "updated" | "missing" | "limited". An ArmoryError (`.words`) when the armory can't answer: nothing
    written, the last data stays."""
    if by_id is not None:
        await _rights(bot, reg, uid, by_id, "sync")
    m, c = _character(reg, uid, label)
    if c.armory is None:
        raise RegistryError(f"{c.label} isn't linked to the armory.")
    aid = c.armory.id
    if force:
        last = _st(reg)["forced"].get(aid)
        if last is not None and _now() - last < SYNC_NOW_EVERY:
            return "limited"
    async with _lock(reg):
        res = await _sync_one(bot, reg, uid, aid, force=force)  # an ArmoryError leaves no stamp: the next press tries again
    if force:
        _st(reg)["forced"][aid] = _now()
    return res


async def _sync_one(bot, reg, uid: int, aid: int, *, force: bool) -> str:
    """The step of spec §5.3 for one linked character (the guild's lock held). ArmoryMissing is handled here (a 404 =
    the roster, else missing_since); every other ArmoryError goes to the caller with nothing written."""
    c = reg.armory_character(uid, aid)
    if c is None:
        return "unchanged"
    m = reg.members[uid]
    link = c.armory
    st = _st(reg)
    cli = armory.client_for(reg)
    since = None if force else (st["modified"].get(aid) or link.modified)
    try:
        facts = await asyncio.to_thread(cli.character, link.slug, since)
    except ArmoryMissing:
        facts = None
    stamp = _iso(_now())
    if facts is armory.NOT_MODIFIED:
        st["checked"][aid] = stamp
        done = link.missing_since is not None and await asyncio.to_thread(
            reg.armory_update, uid, aid, {"missing_since": None}, ride={"checked_at": stamp})
        return "updated" if done else "unchanged"
    if facts is not None and facts.id != aid:
        facts = None  # another character holds that name now: the linked one is looked for by its id
    slug = _norm(link.slug)
    row = None
    if facts is None:
        row = _roster_row(reg, aid)
        if row is not None and _norm(row.slug) != slug:  # renamed in the game: the roster still knows it by its id
            slug = _norm(row.slug)
            try:
                facts = await asyncio.to_thread(cli.character, slug)
            except ArmoryMissing:
                facts = None
            if facts is not None and (facts is armory.NOT_MODIFIED or facts.id != aid):
                facts = None
        if facts is None and row is None:
            st["checked"][aid] = stamp
            if not link.missing_since:
                await asyncio.to_thread(reg.armory_update, uid, aid, {"missing_since": stamp}, ride={"checked_at": stamp})
            return "missing"
    profs = None
    if facts is not None and c.profession_sync:
        try:
            raw = await asyncio.to_thread(cli.professions, slug)
        except ArmoryMissing:
            raw = None
        try:
            profs = _trusted(reg, raw)
        except RegistryError as e:  # the armory's list didn't fit the record: keep the last one, tell ops (hourly)
            await _warn_hourly(bot, reg, f"professions:{aid}", f"{m.display_name}'s {c.label}: the armory's professions were not kept. {e}")
            profs = None
    name, surname = (facts.name, facts.surname) if facts is not None else (row.name, row.surname)
    level = facts.level if facts is not None else row.level
    cls = (facts.cls if facts is not None else row.cls) or link.cls
    changes = {"level": level, "cls": cls, "slug": slug, "missing_since": None}
    ride = {"checked_at": stamp, **({"modified": facts.modified} if facts is not None and facts.modified else {})}
    full = f"{name} {surname}" if surname else name

    def write():
        return reg.armory_update(uid, aid, changes, professions=profs, ride=ride)

    renamed = False
    if full != c.label:
        try:
            done = await _write_named(bot, reg, uid, c.label, name, surname, "the armory", write,
                                      message=f"{m.display_name}: {full} updated from the armory", by_id=None, officer=True)
            renamed = True
        except RegistryError as e:  # the name is another record's, or one the bot can't store: the link follows the id anyway
            await _warn_hourly(bot, reg, f"name:{aid}", f"The armory calls {m.display_name}'s {c.label} {full} now, and the record keeps its name. {e}")
            done = await asyncio.to_thread(write)
    else:
        done = await asyncio.to_thread(write)
    st["checked"][aid] = stamp
    if facts is not None and facts.modified:
        st["modified"][aid] = facts.modified
    if done and profs is not None:
        await _report_unknown_recipes(bot, reg, m, reg.armory_character(uid, aid) or c, profs)
    return "updated" if (done or renamed) else "unchanged"


async def sync_guild(bot, reg) -> dict:
    """The hourly step: every linked character of the guild (Registry.armory_linked — no puppets, nobody who left, no
    retired character), each its own conditional check. The first ArmoryDown / ArmoryAccess / ArmoryShape stops the
    run (the last data stays; one ops warning per guild per hour; the next hourly tick starts again); a missing
    character is counted and the run goes on. → {"checked", "updated", "missing", "stopped"}."""
    out = {"checked": 0, "updated": 0, "missing": 0, "stopped": False}
    async with _lock(reg):
        _st(reg)["last_sync"] = _now()
        for m, c in reg.armory_linked():
            try:
                res = await _sync_one(bot, reg, m.discord_id, c.armory.id, force=False)
            except ArmoryError as e:
                out["stopped"] = True
                await _warn_hourly(bot, reg, "armory", f"Updating characters from the armory stopped. {e.words}")
                log.info("[%s] armory check stopped: %s", reg.key, e)
                break
            out["checked"] += 1
            if res in ("updated", "missing"):
                out[res] += 1
    return out


async def roster_refresh(bot, reg, *, force: bool = False) -> list[RosterRow] | None:
    """The linked guild's roster, read at most every three hours (`force` reads now), conditional on its own
    Last-Modified; the snapshot goes to out/armory/<key>/roster.json. The guild at the stored address must answer with
    the stored id: another guild there stops roster work and warns once (spec §5.1 — never follow a stranger's guild).
    → the rows, or None: no guild linked, a stranger, or the armory can't answer (warned at most once an hour)."""
    if reg.config.armory is None:
        return None
    async with _lock(reg):
        return await _roster_refresh(bot, reg, force)


async def _roster_refresh(bot, reg, force: bool) -> list[RosterRow] | None:
    g = reg.config.armory
    if g is None:
        return None
    st = _st(reg)
    snap = _load_snapshot(reg)
    if snap is not None and not force and _now() - datetime.fromisoformat(snap.taken_at) < ROSTER_EVERY:
        return list(snap.rows)
    st["roster_try"] = (int(g.guild_id), _now())
    cli = armory.client_for(reg)
    try:
        facts = await asyncio.to_thread(cli.guild, g.slug)
        if facts.id != int(g.guild_id):
            if facts.id not in st["strangers"]:
                st["strangers"].add(facts.id)
                await _emit(bot, reg, "warn", f"The armory now shows another guild at {g.name}'s address, so its roster is not read. "
                                              "The owner links the guild again to fix it.")
            return None
        rows, modified = await asyncio.to_thread(cli.roster, g.slug, None if force or snap is None else snap.modified)
    except ArmoryMissing:
        await _warn_hourly(bot, reg, "roster-missing", f"The armory no longer finds {g.name}, so its roster is not read.")
        return None
    except ArmoryError as e:
        await _warn_hourly(bot, reg, "armory", f"Reading {g.name}'s roster from the armory stopped. {e.words}")
        return None
    if rows is armory.NOT_MODIFIED:
        rows = list(snap.rows) if snap is not None else []
    _save_snapshot(reg, _Snapshot(rows=list(rows), taken_at=_iso(_now()), modified=modified, guild_id=int(g.guild_id)))
    return list(rows)


# ------------------------------------------------------------------ the scheduler's hook


def _due(reg) -> tuple[bool, bool]:
    st = _st(reg)
    now = _now()
    sync = st["last_sync"] is None or now - st["last_sync"] >= SYNC_EVERY
    roster = False
    if reg.config.armory is not None:
        snap = _load_snapshot(reg)
        stale = snap is None or now - datetime.fromisoformat(snap.taken_at) >= ROSTER_EVERY
        tried = st["roster_try"]  # a failed read of THIS guild waits an hour; a guild linked or switched since is read now
        roster = stale and (tried is None or tried[0] != int(reg.config.armory.guild_id) or now - tried[1] >= SYNC_EVERY)
    return sync, roster


async def armory_tick(bot, reg):
    """raid_scheduler.scheduler_tick_guild's hook, every minute: when due, ONE background task for the guild reads the
    roster (every three hours; a failed read is tried again an hour later) and then checks every linked character
    (hourly). Never waits for the armory itself (the minute loop goes on). Off where armory.available(reg) is false,
    and when the guild has neither a linked guild nor a linked character. → the task started, or None."""
    if not armory.available(reg):
        return None
    if reg.config.armory is None and not any(c.armory is not None for m in reg.members.values() for c in m.characters):
        return None
    st = _st(reg)
    if st["task"] is not None and not st["task"].done():
        return None
    sync, roster = _due(reg)
    if not (sync or roster):
        return None

    async def go():
        try:
            if roster:
                await roster_refresh(bot, reg)
            if sync:
                await sync_guild(bot, reg)
        except Exception:  # noqa: BLE001 — the hourly check never takes the scheduler down
            log.exception("armory tick failed for %s", reg.key)

    st["task"] = asyncio.get_running_loop().create_task(go())
    return st["task"]


__all__ = ["Found", "MISSING_AFTER", "NOT_FOUND", "OFFICER_ONLY", "ROSTER_EVERY", "SYNC_EVERY", "SYNC_LIMITED", "SYNC_NOW_EVERY",
           "armory_tick", "find_for_link", "flags", "in_guild", "last_checked", "link_character", "reset_state", "roster_refresh",
           "roster_snapshot", "set_profession_sync", "sync_character", "sync_guild", "unclaimed", "unlink_character", "updated_words"]
