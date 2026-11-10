"""The guild guide library (design §5.63): any member
uploads a leveling guide the guild made itself, unlocked once by the BattleTag it was locked to; the bot keeps the
unlocked guide texts as a VERSION per faction line (Alliance, Horde, Both), named by when it was uploaded; any member gets
a version locked to their own BattleTag. Officers withdraw and restore versions; every upload is kept.

Native guild state in the data repo (store.py), one commit per upload / withdraw / restore:

    <guild>/guides/index.yaml                    versions: [GuideVersion]
    <guild>/guides/<version id>/guide-001.txt    the unlocked guide texts, one file per guide, in order

D7: NO BattleTag and NO import string is written anywhere: not to the repo, a commit message, a log line or an
exception's words. The tags live only in the call. Every function here is sync (decode / encode and a git commit are
slow): callers run them in asyncio.to_thread. Rights (officers for withdraw / restore) are the caller's check, made live
on the surface, as appeals.record_resolve trusts its callers."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from .build import EMPTY_TEXT, TEXT_MAX, TOO_BIG, validate_battletag
from .reissue import SLOTS
from .rxp_decode import GuideError, decode_sections, encode, trailer

log = logging.getLogger(__name__)

FACTIONS = ("alliance", "horde", "both")
FACTION_WORDS = {"alliance": "Alliance", "horde": "Horde", "both": "Both factions"}
_NOUN = {"alliance": "Alliance guide", "horde": "Horde guide", "both": "guide for both factions"}  # inside a sentence
UPLOADS_PER_DAY = 5  # per member per guild, on the guild's day
# What one upload may keep, in characters of unlocked guide text: a full 1–60 route is a few MB, so 12 MB is past any
# real guide. The import string's own cap (TEXT_MAX) doesn't bound this: a small string can unpack to tens of MB
STORED_MAX = 12 * 1024 * 1024
# What the library may hold in all (every version's `size`, withdrawn ones too: nothing deletes a version)
LIBRARY_MAX = 200 * 1024 * 1024

GUIDES_DIR = "guides"
INDEX = "index.yaml"

NO_TOOL = "There is no leveling-guide tool for this game version yet."  # every surface's answer when available() is off
BAD_FACTION = "Pick Alliance, Horde or Both factions for the guide."
DAILY_CAP = f"You've uploaded {UPLOADS_PER_DAY} guild guides today. Try again tomorrow."
NO_VERSION = "That guild guide version isn't in the library."
WITHDRAWN = "That guild guide version was withdrawn by an officer. Pick the latest one instead."
NOT_WITHDRAWN = "That guild guide version isn't withdrawn."
BUSY = "Other guides are being locked right now. Try again in a minute."
STORED_TOO_BIG = "That guide is bigger than any leveling guide. Upload the guild's guide alone."
LIBRARY_FULL = "The guild guide library is full, so nothing more can be uploaded. Ask an officer."

_ID = re.compile(r"(alliance|horde|both)-\d{8}-\d{6}(?:-\d{1,3})?")

# fn(guild key) after every committed write (the web's `guides` topic); a listener never breaks a write
GUIDE_LISTENERS: list = []

_LOCKS: dict[str, threading.Lock] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock(reg) -> threading.Lock:
    return _LOCKS.setdefault(reg.key, threading.Lock())


class GuideVersion(BaseModel):
    id: str
    faction: str
    uploaded_at: str  # ISO, UTC
    by_id: int
    by_name: str  # the uploader's display name at upload (member text: escaped on Discord, fenced in a prompt)
    guides: int
    sections: int  # the uploaded string's section count (older records: the issue splits evenly by it)
    layout: list[int] = []  # the guide count of each section of the upload, in order: every issue keeps it exactly
    base: int | None = None  # the number the upload ended with (`|40000`, the addon version it was made for): every issue ends with it
    size: int  # characters of guide text
    withdrawn_at: str | None = None
    withdrawn_by: str | None = None
    withdrawn_by_id: int | None = None


def available(reg) -> bool:
    """The library is on wherever the guide tool is: the game profile has its tables (api_guides.game_of)."""
    return bool((getattr(reg.profile, "guides", None) or {}).get("zones"))


# ---------------------------------------------------------------- reads

def _dir(reg) -> Path:
    return Path(reg.key) / GUIDES_DIR


def _load(reg) -> list[GuideVersion]:
    p = reg.store.root / _dir(reg) / INDEX
    if not p.exists():
        return []
    doc = yaml.safe_load(p.read_text()) or {}
    return [GuideVersion.model_validate(x) for x in doc.get("versions") or []]


def _save_index(reg, vs: list[GuideVersion]) -> None:
    reg.store.write_text(_dir(reg) / INDEX, yaml.safe_dump({"versions": [v.model_dump() for v in vs]}, sort_keys=False, allow_unicode=True))


def _newest_first(vs: list[GuideVersion]) -> list[GuideVersion]:
    return sorted(reversed(vs), key=lambda v: v.uploaded_at, reverse=True)  # stable: the later upload of one second first


def versions(reg, faction: str | None = None, live_only: bool = False) -> list[GuideVersion]:
    """Every version, newest first; `faction` = one line; `live_only` = not withdrawn."""
    return [v for v in _newest_first(_load(reg)) if (faction is None or v.faction == faction) and not (live_only and v.withdrawn_at)]


def latest(reg, faction: str) -> GuideVersion | None:
    """The newest version of a line that is not withdrawn."""
    return next(iter(versions(reg, faction, live_only=True)), None)


def get(reg, vid: str) -> GuideVersion | None:
    if not _ID.fullmatch(vid or ""):
        return None
    return next((v for v in _load(reg) if v.id == vid), None)


def _texts(reg, v: GuideVersion) -> list[str]:
    d = reg.store.root / _dir(reg) / v.id
    return [(d / f"guide-{i:03d}.txt").read_text() for i in range(1, v.guides + 1)]


# ---------------------------------------------------------------- words

def when(reg, v: GuideVersion) -> str:
    return reg.local12(v.uploaded_at)


def version_words(reg, v: GuideVersion) -> str:
    """'Horde guide of Mon 9 Nov 3:00 PM' (after 'the' / 'your')."""
    return f"{_NOUN[v.faction]} of {when(reg, v)}"


def issue_filename(reg, v: GuideVersion, battletag: str) -> str:
    """GuildGuide-<Faction>-<YYYYMMDD>-<Name>.txt: the guild's day of the upload (what every surface shows), the
    requester's name part kept to letters, digits, _ and -."""
    name = re.sub(r"[^\w-]+", "_", (battletag or "").strip().split("#", 1)[0]).strip("_") or "account"
    day = f"{_guild_day(reg, v.uploaded_at):%Y%m%d}"
    return f"GuildGuide-{v.faction.title()}-{day}-{name}.txt"


def rows(reg, officer: bool = False) -> list[dict]:
    """What every surface lists, newest first: members never see a withdrawn version; officers see it and by whom."""
    live = {f: latest(reg, f) for f in FACTIONS}
    out = []
    for v in versions(reg):
        if v.withdrawn_at and not officer:
            continue
        r = {"id": v.id, "faction": v.faction, "faction_words": FACTION_WORDS[v.faction], "when": when(reg, v), "by": v.by_name,
             "guides": v.guides, "latest": bool(live[v.faction] and live[v.faction].id == v.id), "withdrawn": bool(v.withdrawn_at)}
        if officer:
            r["withdrawn_by"] = v.withdrawn_by
            r["withdrawn_when"] = reg.local12(v.withdrawn_at) if v.withdrawn_at else None
        out.append(r)
    return out


# ---------------------------------------------------------------- the confirm sentences (the Confirm rule)

def upload_consequence(reg, faction: str) -> str:
    prep = "in" if faction == "both" else "for"
    return f"This becomes the latest {_NOUN.get(faction, 'guild guide')} {prep} {reg.config.name}. Nobody gets a DM. An officer can withdraw it."


def _gets_notice(reg, v: GuideVersion, by_id: int | None) -> bool:
    """withdraw() notices the uploader when someone else withdraws and they still have a member record (reg.notify
    gives a notice to a known member only)."""
    return (by_id is None or int(by_id) != v.by_id) and v.by_id in reg.members


def withdraw_consequence(reg, v: GuideVersion, by_id: int | None = None, esc=None) -> str:
    """What Withdraw does. `by_id` = the officer: the uploader is named when they get a notice (someone else withdraws
    and they have a member record). `esc` = the surface's escape for the uploader's name (Discord markdown)."""
    head = f"The {version_words(reg, v)} is withdrawn"
    head += f" and {(esc or str)(v.by_name)} gets a notice." if _gets_notice(reg, v, by_id) else "."
    now = latest(reg, v.faction)
    if now is None or now.id == v.id:
        nxt = next((x for x in versions(reg, v.faction, live_only=True) if x.id != v.id), None)
        then = f"Members get the one from {when(reg, nxt)} as the latest." if nxt else f"No {_NOUN[v.faction]} is left for members."
    else:
        then = f"The latest stays the one from {when(reg, now)}."
    return f"{head} {then} You can restore it."


def restore_consequence(reg, v: GuideVersion) -> str:
    out = f"The {version_words(reg, v)} is back."
    after = [x for x in versions(reg, v.faction) if not x.withdrawn_at or x.id == v.id]  # the line once it is restored
    if after and after[0].id == v.id:
        out += " It becomes the latest again."
    return out


def notice_text(reg, v: GuideVersion, by: str) -> str:
    return f"Your {version_words(reg, v)} was withdrawn by {by}."


# ---------------------------------------------------------------- writes (each on a worker thread: a git commit)

def _guild_day(reg, iso: str):
    return datetime.fromisoformat(iso).astimezone(reg.tz).date()


def _check_room(vs: list[GuideVersion], size: int) -> None:
    """The stored caps, checked before anything is written: one upload's text, then the library's total."""
    if size > STORED_MAX:
        raise GuideError(STORED_TOO_BIG)
    if sum(v.size for v in vs) + size > LIBRARY_MAX:
        raise GuideError(LIBRARY_FULL)


def _check_cap(reg, by_id: int, stamp: datetime) -> None:
    today = stamp.astimezone(reg.tz).date()
    n = sum(1 for v in _load(reg) if v.by_id == int(by_id) and _guild_day(reg, v.uploaded_at) == today)
    if n >= UPLOADS_PER_DAY:
        raise GuideError(DAILY_CAP)


def _published(reg) -> None:
    for fn in list(GUIDE_LISTENERS):
        try:
            fn(reg.key)
        except Exception:  # noqa: BLE001 — a listener never breaks a write
            log.debug("guide listener failed", exc_info=True)


def upload(reg, text: str, battletag: str, faction: str, by_id: int, by_name: str) -> GuideVersion:
    """Unlock `text` with `battletag` and keep its guide texts as a new version of `faction`'s line, in one commit.
    GuideError = a refusal in the person's words (the tool's own words for the string and the tag)."""
    if faction not in FACTIONS:
        raise GuideError(BAD_FACTION)
    if not (text or "").strip():
        raise GuideError(EMPTY_TEXT)
    if len(text) > TEXT_MAX:
        raise GuideError(TOO_BIG)
    tag = validate_battletag(battletag)
    _check_cap(reg, by_id, _now())  # cheap refusal before the unlock; checked again under the lock
    if not SLOTS.acquire(blocking=False):
        raise GuideError(BUSY)
    try:
        secs = decode_sections(text, tag)
    finally:
        SLOTS.release()
    texts = [t for sec in secs for t in sec]
    layout = [len(sec) for sec in secs]
    size = sum(len(t) for t in texts)
    _check_room(_load(reg), size)  # cheap refusal before the lock; checked again under it
    name = str(by_name or "").strip()[:64] or "a member"
    with _lock(reg):
        stamp = _now()
        _check_cap(reg, by_id, stamp)
        vs = _load(reg)
        _check_room(vs, size)
        taken = {v.id for v in vs}
        base = f"{faction}-{stamp.astimezone(timezone.utc):%Y%m%d-%H%M%S}"
        vid, i = base, 1
        while vid in taken:
            i += 1
            vid = f"{base}-{i}"
        v = GuideVersion(id=vid, faction=faction, uploaded_at=stamp.astimezone(timezone.utc).isoformat(timespec="seconds"), by_id=int(by_id),
                         by_name=name, guides=len(texts), sections=len(layout), layout=layout, base=trailer(text), size=size)
        msg = f"{reg.key}: guild guide {vid} uploaded by {name}"
        with reg.store.batch(msg):
            for n, t in enumerate(texts, 1):
                reg.store.write_text(_dir(reg) / vid / f"guide-{n:03d}.txt", t)
            _save_index(reg, vs + [v])
            reg.store.commit(msg)
    _published(reg)
    return v


def issue(reg, vid: str, battletag: str, *, officer: bool = False) -> str:
    """The version's guide texts locked to `battletag`, in the upload's section layout (older records: its count). Nothing is written. A withdrawn
    version is refused to a member (WITHDRAWN); an officer may still get it."""
    tag = validate_battletag(battletag)
    v = get(reg, vid)
    if v is None:
        raise GuideError(NO_VERSION)
    if v.withdrawn_at and not officer:
        raise GuideError(WITHDRAWN)
    if not SLOTS.acquire(blocking=False):
        raise GuideError(BUSY)
    try:
        return encode(_texts(reg, v), tag, sections=v.sections, layout=v.layout or None, base=v.base)
    finally:
        SLOTS.release()


def withdraw(reg, vid: str, by: str, by_id: int | None) -> GuideVersion:
    """Officers: take a version out of members' reach (kept, restorable). The uploader gets a notice, never a DM, unless
    they withdrew it themselves."""
    with _lock(reg):
        vs = _load(reg)
        v = next((x for x in vs if x.id == vid), None) if _ID.fullmatch(vid or "") else None
        if v is None:
            raise GuideError(NO_VERSION)
        if v.withdrawn_at:
            raise GuideError(f"Already withdrawn by {v.withdrawn_by or 'an officer'} on {reg.local12(v.withdrawn_at)}.")
        v.withdrawn_at, v.withdrawn_by, v.withdrawn_by_id = _now().isoformat(timespec="seconds"), by, by_id
        msg = f"{reg.key}: guild guide {vid} withdrawn by {by}"
        with reg.store.batch(msg):
            _save_index(reg, vs)
            reg.store.commit(msg)
            if _gets_notice(reg, v, by_id):
                reg.notify(v.by_id, "guide", notice_text(reg, v, by))
    _published(reg)
    return v


def restore(reg, vid: str, by: str, by_id: int | None) -> GuideVersion:
    """Officers: a withdrawn version back in members' reach."""
    with _lock(reg):
        vs = _load(reg)
        v = next((x for x in vs if x.id == vid), None) if _ID.fullmatch(vid or "") else None
        if v is None:
            raise GuideError(NO_VERSION)
        if not v.withdrawn_at:
            raise GuideError(NOT_WITHDRAWN)
        v.withdrawn_at = v.withdrawn_by = v.withdrawn_by_id = None
        msg = f"{reg.key}: guild guide {vid} restored by {by}"
        with reg.store.batch(msg):
            _save_index(reg, vs)
            reg.store.commit(msg)
    _published(reg)
    return v
