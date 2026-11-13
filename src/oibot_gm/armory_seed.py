"""The seed (armory link, spec §5.5; decisions D8 and §10): the guild's registered characters set against the linked
guild's roster, shown to officers before anything is written — Sure / Likely / No match, and the roster's characters
nobody here claims — then the picks an officer confirms, linked one by one.

`propose` is pure: no request to the armory, no write, no clock (the roster and when it was read are passed in). Only
ACTIVE, named characters of members still in the server who are not puppets, and not linked yet, are proposed; a roster
row some record already holds is never a candidate and never "not claimed".

- Sure: the first name (case-folded, accents kept) and the class match exactly one roster row (every row counts, held
  or not), that row is not held, no other registered character has that first name and class, and a registered surname,
  when set, is the row's. So two characters are never sure for one row, and one character never for two rows.
- Likely: same class, an unheld row, and a close name (scored below); at most three candidates per character, best first,
  ties by the row's name. A row already proposed as another character's sure match is not offered again.
- No match: everything else (candidate None, why "").

Likely, in plain words. Names are compared folded: case and accents dropped, spaces and other non-letters removed. The
registered name (first, and first + surname) and, failing that, each word of the member's Discord display name and the
whole display name, are set against the row's first + surname, first name and surname:
  1. "same name" — the registered first name (and surname, when both sides have one) folds to the row's;
  2. a close name — equal when folded (a joined spelling of first + surname), or one starts with the other, or one
     contains the other, the shorter at least four letters;
  3. a close name — difflib's ratio at least 0.8.
Best first: same name, then equal / starts with / contains, then the ratio; a match by the character's own name before
one by the display name. `why` is "same name", "close name", or "display name" (only the display name matched).

`apply` is the officers' confirm: each pick (uid, label, armory id) is re-checked against the CURRENT roster snapshot and
linked through discord_armory.link_character on its own — a refusal is one sentence in its result row and never undoes
or stops the others. Nothing is linked that an officer did not pick (D8: even a sure match waits for the one confirm).
No DM, no notice (the link's own audit line is the record). The surfaces (Discord, the site, MCP) compare `rev` themselves."""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from . import discord_armory
from .armory import ArmoryError, RosterRow
from .registry import Member, RegisteredCharacter, RegistryError

log = logging.getLogger(__name__)

OFFICER_ONLY = "Only an officer links characters to the armory for the whole guild."
ROW_GONE = "That character is no longer on the guild's roster, so nothing was linked."
ANOTHER_CHARACTER = "The armory shows another character under that name now, so nothing was linked."
FAILED = "Something went wrong while linking it, so nothing was linked."

CANDIDATES = 3  # at most this many candidates per likely character
MIN_PART = 4  # "starts with" / "contains" needs the shorter name to have at least this many letters
MIN_RATIO = 0.8  # difflib's ratio for a close name


@dataclass
class SeedRow:
    uid: int
    member: str  # the member's display name
    label: str  # the character as registered
    cls: str  # its class (the profile's key)
    candidate: RosterRow | None  # the best roster row (None: no match)
    why: str  # "same name" | "close name" | "display name" ("" for no match)
    others: list[RosterRow] = field(default_factory=list)  # likely only: the next candidates, best first (≤ 2)


@dataclass
class Seed:
    sure: list[SeedRow]
    likely: list[SeedRow]
    none: list[SeedRow]
    unclaimed: list[RosterRow]  # the roster's characters nobody holds and no sure match takes
    rev: str  # a short hash of the proposal (what the officer saw)


def _fold(s: str | None) -> str:
    """Case and accents dropped, only letters kept: "Ébba Thistle" → "ebbathistle"."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^a-z]", "", "".join(ch for ch in s if not unicodedata.combining(ch)).casefold())


def _cf(s: str | None) -> str:
    return str(s or "").strip().casefold()


def _row_key(r: RosterRow) -> tuple:
    return (_cf(r.name), _cf(r.surname), r.id)


def _row_label(r: RosterRow) -> str:
    return f"{r.name} {r.surname}" if r.surname else r.name


def _eligible(reg) -> list[tuple[Member, RegisteredCharacter]]:
    """What is proposed: active, named, unlinked characters of real members still here — members by display name, then label."""
    out = [(m, c) for m in reg.members.values() if not m.test and not m.left_at
           for c in m.characters if c.status == "active" and c.name and c.armory is None]
    return sorted(out, key=lambda mc: (_cf(mc[0].display_name), _cf(mc[1].label), mc[0].discord_id))


def _closeness(source: str, row: RosterRow) -> tuple[int, float] | None:
    """How close one folded name is to a row: (tier, ratio), higher is better; None when not close.
    Tier 2 = equal / starts with / contains; tier 1 = the ratio alone."""
    if len(source) < 2:
        return None
    targets = [t for t in (_fold(_row_label(row)), _fold(row.name), _fold(row.surname)) if t]
    best: tuple[int, float] | None = None
    for t in targets:
        ratio = difflib.SequenceMatcher(None, source, t).ratio()
        short, long_ = sorted((source, t), key=len)
        if source == t or (len(short) >= MIN_PART and short in long_):
            hit = (2, ratio)
        elif ratio >= MIN_RATIO:
            hit = (1, ratio)
        else:
            continue
        best = hit if best is None or hit > best else best
    return best


def _score(m: Member, c: RegisteredCharacter, row: RosterRow) -> tuple[tuple, str] | None:
    """(sort key — higher is better —, why) for a likely candidate, or None."""
    if _fold(c.name) == _fold(row.name) and (not c.surname or not row.surname or _fold(c.surname) == _fold(row.surname)):
        return (3, 1, 1.0), "same name"
    own = [s for s in (_fold(c.label), _fold(c.name)) if s]
    hits = [h for h in (_closeness(s, row) for s in dict.fromkeys(own)) if h]
    if hits:
        tier, ratio = max(hits)
        return (tier, 1, ratio), "close name"
    words = [_fold(w) for w in re.split(r"[\s|/\\,._\-()\[\]]+", m.display_name or "")]
    shown = [s for s in dict.fromkeys([_fold(m.display_name), *words]) if len(s) >= 2]
    hits = [h for h in (_closeness(s, row) for s in shown) if h]
    if hits:
        tier, ratio = max(hits)
        return (tier, 0, ratio), "display name"
    return None


def propose(reg, roster: list[RosterRow], *, taken_at: str | None = None) -> Seed:
    """The seed's proposal (pure: no request, no write). `roster` = discord_armory.roster_snapshot(reg)'s rows, `taken_at`
    its time (it goes into `rev` only)."""
    held = {r.id for r in roster if reg.armory_holder(r.id) is not None}
    free = [r for r in roster if r.id not in held]
    people = _eligible(reg)
    # every registered character counts for "no other registered character has that first name and class" (linked
    # ones too: conservative), puppets aside
    named: dict[tuple[str, str], int] = {}
    for m in reg.members.values():
        if m.test:
            continue
        for c in m.characters:
            if c.status == "active" and c.name:
                named[(_cf(c.name), c.cls)] = named.get((_cf(c.name), c.cls), 0) + 1
    on_roster: dict[tuple[str, str | None], list[RosterRow]] = {}
    for r in roster:
        on_roster.setdefault((_cf(r.name), r.cls), []).append(r)

    sure: list[SeedRow] = []
    rest: list[tuple[Member, RegisteredCharacter]] = []
    for m, c in people:
        rows = on_roster.get((_cf(c.name), c.cls), [])
        row = rows[0] if len(rows) == 1 else None
        if (row is not None and row.id not in held and named.get((_cf(c.name), c.cls), 0) == 1
                and (not c.surname or _cf(c.surname) == _cf(row.surname))):
            sure.append(SeedRow(m.discord_id, m.display_name, c.label, c.cls, row, "same name"))
        else:
            rest.append((m, c))
    taken = {r.candidate.id for r in sure}

    likely: list[SeedRow] = []
    none: list[SeedRow] = []
    for m, c in rest:
        scored = []
        for r in free:
            if r.cls != c.cls or r.id in taken:
                continue
            s = _score(m, c, r)
            if s is not None:
                scored.append((s[0], r, s[1]))
        scored.sort(key=lambda x: (tuple(-v for v in x[0]), _row_key(x[1])))
        if scored:
            top = scored[:CANDIDATES]
            likely.append(SeedRow(m.discord_id, m.display_name, c.label, c.cls, top[0][1], top[0][2], [r for _, r, _ in top[1:]]))
        else:
            none.append(SeedRow(m.discord_id, m.display_name, c.label, c.cls, None, ""))

    unclaimed = sorted((r for r in free if r.id not in taken), key=_row_key)
    content = {"sure": [[r.uid, r.label, r.candidate.id] for r in sure],
               "likely": [[r.uid, r.label, r.candidate.id, [o.id for o in r.others], r.why] for r in likely],
               "none": [[r.uid, r.label] for r in none],
               "unclaimed": [r.id for r in unclaimed], "taken_at": taken_at}
    rev = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    return Seed(sure, likely, none, unclaimed, rev)


async def apply(bot, reg, picks: list[tuple[int, str, int]], by: str, *, by_id, via=None) -> list[dict]:
    """Link what an officer picked: (uid, label, armory id) each. Officers only, decided live. Every pick is checked
    against the current roster snapshot (the row must still be there; a second claim is refused as the link refuses it)
    and linked through discord_armory.link_character on its own. → [{uid, label, id, ok, text}] in the picks' order;
    `text` is one sentence. Raises RegistryError(OFFICER_ONLY) for anyone else, nothing written."""
    if not await discord_armory._officer(bot, reg, by_id):
        raise RegistryError(OFFICER_ONLY)
    rows = {r.id: r for r in discord_armory.roster_snapshot(reg)[0]}
    out: list[dict] = []
    for uid, label, aid in picks:
        res = {"uid": int(uid), "label": label, "id": int(aid), "ok": False, "text": ""}
        out.append(res)
        try:
            row = rows.get(int(aid))
            if row is None:
                res["text"] = ROW_GONE
                continue
            why = reg.link_refusal(int(uid), label, row.id, row.name, row.surname, by_id=by_id, officer=True)
            if why:  # refused before asking the armory anything
                res["text"] = why
                continue
            found = await discord_armory.find_for_link(bot, reg, row.name, row.surname)
            if found.id != row.id:
                res["text"] = ANOTHER_CHARACTER
                continue
            m = reg.members[int(uid)]
            c = await discord_armory.link_character(bot, reg, int(uid), label, found, by, by_id=by_id, via=via)
            res["ok"], res["text"] = True, f"{m.display_name}'s {label} is linked to {c.label} in the armory."
        except RegistryError as e:
            res["text"] = str(e)
        except ArmoryError as e:
            res["text"] = e.words
        except Exception:  # noqa: BLE001 — one pick's failure never stops the others
            log.exception("armory seed: linking %s's %s to %s failed", uid, label, aid)
            res["text"] = FAILED
    return out
