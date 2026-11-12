"""THE boundary to Blizzard's armory (armory link, phase 2 — spec §5.3, §9): the one place the bot reads a guild, its
roster, a character and its professions. The API is undocumented: every answer is validated here and a surprise is an
ArmoryShape, never a KeyError further in. Every call is BLOCKING (network + the shared rate limiter): callers run it in
`asyncio.to_thread`. One token and one limiter for the whole process (battlenet.TOKEN / battlenet.LIMITER, shared with
the recipe importer). Requests carry only the Authorization header (and If-Modified-Since when asked) and the
namespace / locale query; the token never reaches a log, an exception or a return value.

The version's facts are data (profiles/<version>/integrations.yaml `armory`): region, namespaces, the class-id and
ruleset-id tables; the profession ids come from professions.yaml `blizzard_id`. A version without `armory` has none.
The guild rank is never read (owner decision D7)."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from oibot_gm import battlenet

HOST = "https://{region}.api.blizzard.com"
LOCALE = "en_US"
TIMEOUT = 20
NOT_MODIFIED: Any = type("NotModified", (), {"__repr__": lambda self: "NOT_MODIFIED", "__bool__": lambda self: False})()
"""What a conditional read returns when the armory answers 304 (nothing changed since `since`)."""

DOWN_WORDS = "The armory can't be reached right now. Try again in a few minutes."
SHAPE_WORDS = "The armory answered in a way the bot doesn't understand. The last saved details are kept."
ACCESS_WORDS = "The bot can't sign in to the armory. The host has to check the Blizzard keys."
GUILD_MISSING_WORDS = "The armory doesn't find a guild by that name."
CHARACTER_MISSING_WORDS = "The armory doesn't find that character. A character shows there from level 10, after logging out once."
MISSING_WORDS = "The armory doesn't find that."
NO_ARMORY_WORDS = "This game version has no armory."


class ArmoryError(Exception):
    """The base. `.words` = one sentence a person can read; str() = what happened, for a log (never the token)."""

    def __init__(self, words: str, detail: str = ""):
        super().__init__(detail or words)
        self.words = words


class ArmoryMissing(ArmoryError):
    """404: no such guild or character (a character is missing below level 10 or before its first logout)."""


class ArmoryDown(ArmoryError):
    """The armory can't be reached: network, 5xx, 429 (`retry_after` seconds when it said so), or an unexpected status."""

    def __init__(self, words: str = DOWN_WORDS, detail: str = "", retry_after: float | None = None):
        super().__init__(words, detail)
        self.retry_after = retry_after


class ArmoryShape(ArmoryError):
    """An answer that failed validation: stop that step, keep the last data."""

    def __init__(self, path: str, what: str):
        super().__init__(SHAPE_WORDS, f"unexpected shape at {path}: {what}")


class ArmoryAccess(ArmoryError):
    """The bot's credentials are missing or refused: retrying will not help until the host fixes .env."""


@dataclass(frozen=True)
class Reply:
    """One HTTP answer as the transport gives it: header names lowercase, body raw."""
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class Fetched:
    data: dict
    modified: str | None  # the Last-Modified header verbatim (send it back as `since`)


@dataclass(frozen=True)
class GuildFacts:
    id: int
    slug: str
    name: str
    faction: str  # "Horde" | "Alliance" (the armory's own word)
    kind: str  # "PvP" | "Normal" | "Roleplay" | "Roleplay PvP" (the ruleset table; an id not in it = the armory's name)
    members: int
    created: datetime  # UTC


@dataclass(frozen=True)
class RosterRow:
    id: int
    name: str
    surname: str | None
    slug: str  # as the armory gives it (not percent-encoded)
    level: int
    cls: str | None  # the profile's class key (classes.yaml), None for a class id not in the table


@dataclass(frozen=True)
class CharacterFacts:
    id: int
    name: str
    surname: str | None
    cls: str | None  # the profile's class key, None for a class id not in the table
    level: int
    guild_id: int | None
    modified: str | None  # Last-Modified verbatim (profile and professions share it)


Fetch = Callable[[str, dict[str, str]], Reply]


def urllib_fetch(url: str, headers: dict[str, str]) -> Reply:
    """The real transport. Any HTTP status comes back as a Reply; only a network failure raises (OSError)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as r:
            return Reply(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Reply(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")


# ------------------------------------------------------------------ names → addresses


def address(name: str, surname: str | None) -> str:
    """A character's address: 'Bram', 'Kettle' → 'bram-kettle'; lowercase, spaces → '-', percent-encoded ('ébba' → '%C3%A9bba')."""
    raw = "-".join(p for p in (name.strip(), (surname or "").strip()) if p).lower().replace(" ", "-")
    return urllib.parse.quote(raw, safe="-")


def guild_slug(name: str) -> str:
    """A guild's address from its typed name: lowercase, apostrophes dropped, every other non-alphanumeric run → '-'."""
    s = re.sub(r"['’`]", "", name.strip().lower())
    return re.sub(r"[\W_]+", "-", s).strip("-")


def _in_path(slug: str) -> str:
    """A slug into a URL path: raw ('ébba-thistle') and already-encoded ('%C3%A9bba-thistle') both work."""
    return urllib.parse.quote(slug, safe="-%")


# ------------------------------------------------------------------ validation


def _need(ok: bool, path: str, what: str) -> None:
    if not ok:
        raise ArmoryShape(path, what)


def _int(d: dict, key: str, path: str) -> int:
    v = d.get(key)
    _need(isinstance(v, int) and not isinstance(v, bool), path, key)
    return v


def _str(d: dict, key: str, path: str) -> str:
    v = d.get(key)
    _need(isinstance(v, str) and bool(v.strip()), path, key)
    return v.strip()


def _opt_str(d: dict, key: str, path: str) -> str | None:
    v = d.get(key)
    _need(v is None or isinstance(v, str), path, key)
    return (v or "").strip() or None


def _dict(d: dict, key: str, path: str) -> dict:
    v = d.get(key)
    _need(isinstance(v, dict), path, key)
    return v


def _list(d: dict, key: str, path: str, optional: bool = False) -> list:
    v = d.get(key)
    if v is None and optional:
        return []
    _need(isinstance(v, list), path, key)
    return v


# ------------------------------------------------------------------ the client


class Armory:
    """One region's armory. Every method BLOCKS (network, the shared limiter, at most one retry after 1 s on a 5xx or a
    network failure): call it through `asyncio.to_thread`. `fetch(url, headers) -> Reply` stands in for the network
    (tests); with an injected fetch and no `token`, no Authorization header is sent. `token() -> str` and `drop(value)`
    default to the process's shared token; `classes` (armory class id → profile class key), `rulesets` (ruleset id →
    kind) and `professions` (kept as `profession_ids`: Blizzard profession id → profile profession id) come from the profile (client_for)."""

    def __init__(self, region: str, profile_ns: str, static_ns: str, fetch: Fetch | None = None, *,
                 token: Callable[[], str] | None = None, drop: Callable[[str], None] | None = None,
                 classes: dict[int, str] | None = None, rulesets: dict[int, str] | None = None,
                 professions: dict[int, str] | None = None, limiter: battlenet.Limiter | None = None):
        self.region, self.profile_ns, self.static_ns = region, profile_ns, static_ns
        self.classes = {int(k): str(v) for k, v in (classes or {}).items()}
        self.rulesets = {int(k): str(v) for k, v in (rulesets or {}).items()}
        self.profession_ids = {int(k): str(v) for k, v in (professions or {}).items()}
        self._fetch = fetch or urllib_fetch
        if token is None and fetch is None:
            token, drop = battlenet.TOKEN.get, battlenet.TOKEN.drop
        self._token, self._drop = token, drop
        self._limiter = limiter or battlenet.LIMITER

    # -- the one request

    def _bearer(self) -> str | None:
        if self._token is None:
            return None
        refused = failed = None
        try:
            return self._token()
        except battlenet.TokenRefused as e:
            refused = str(e)
        except Exception as e:  # noqa: BLE001 — the token service is down or odd; the detail names the type only
            failed = type(e).__name__
        if refused is not None:  # raised outside the except: nothing chained
            raise ArmoryAccess(ACCESS_WORDS, refused)
        raise ArmoryDown(detail=f"no token: {failed}")

    def _scrub(self, text: str, token: str | None) -> str:
        return text.replace(token, "<token>") if token else text

    def _get(self, path: str, ns: str, since: str | None = None, missing: str = MISSING_WORDS) -> Fetched | Any:
        """GET path → Fetched, or NOT_MODIFIED on a 304 to `since`. Raises ArmoryMissing / ArmoryDown / ArmoryShape /
        ArmoryAccess, each raised outside any except block (no chained exception can carry a header)."""
        url = f"{HOST.format(region=self.region)}{path}?{urllib.parse.urlencode({'namespace': ns, 'locale': LOCALE})}"
        renewed = retried = False
        while True:
            token = self._bearer()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            if since:
                headers["If-Modified-Since"] = since
            self._limiter.acquire()
            failure = None
            try:
                reply = self._fetch(url, headers)
            except Exception as e:  # noqa: BLE001 — any transport failure is "down"
                failure = self._scrub(f"GET {path}: {type(e).__name__}: {e}", token)
            if failure is not None:  # handled outside the except: the transport's exception is never chained
                if not retried:
                    retried = True
                    time.sleep(1)
                    continue
                raise ArmoryDown(detail=failure)
            status = reply.status
            if status == 401 and token and not renewed:  # the token expired early: one new one
                renewed = True
                if self._drop:
                    self._drop(token)
                continue
            if status >= 500 and not retried:
                retried = True
                time.sleep(1)
                continue
            break
        modified = reply.headers.get("last-modified")
        if status == 304:
            return NOT_MODIFIED
        if status == 404:
            raise ArmoryMissing(missing, f"GET {path}: 404")
        if status == 429:
            raise ArmoryDown(detail=f"GET {path}: 429", retry_after=_retry_after(reply.headers.get("retry-after")))
        if status in (401, 403):
            raise ArmoryAccess(ACCESS_WORDS, f"GET {path}: {status}")
        if status != 200:
            raise ArmoryDown(detail=f"GET {path}: {status}")
        try:
            data = json.loads(reply.body.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError is one
            data = None
        _need(isinstance(data, dict), path, "not a JSON object")
        return Fetched(data, modified)

    # -- the reads

    def guild(self, slug: str) -> GuildFacts:
        """The guild at `slug` (guild_slug of a typed name). Raises ArmoryMissing when there is none."""
        path = f"/data/wow/guild/{_in_path(slug)}"
        d = self._get(path, self.profile_ns, missing=GUILD_MISSING_WORDS).data
        rs = _dict(d, "ruleset", path)
        rid = _int(rs, "id", path)
        created = _int(d, "created_timestamp", path)
        return GuildFacts(id=_int(d, "id", path), slug=_str(d, "slug", path), name=_str(d, "name", path),
                          faction=_str(_dict(d, "faction", path), "name", path),
                          kind=self.rulesets.get(rid) or _str(rs, "name", path), members=_int(d, "member_count", path),
                          created=datetime.fromtimestamp(created / 1000, tz=timezone.utc))

    def roster(self, slug: str, since: str | None = None) -> tuple[list[RosterRow] | Any, str | None]:
        """The guild's members → (rows, Last-Modified); (NOT_MODIFIED, Last-Modified or `since`) on a 304 to `since`.
        The rank is not read."""
        path = f"/data/wow/guild/{_in_path(slug)}/roster"
        got = self._get(path, self.profile_ns, since, missing=GUILD_MISSING_WORDS)
        if got is NOT_MODIFIED:
            return NOT_MODIFIED, since
        rows = []
        for m in _list(got.data, "members", path):
            _need(isinstance(m, dict), path, "member")
            c = _dict(m, "character", path)
            rows.append(RosterRow(id=_int(c, "id", path), name=_str(c, "name", path), surname=_opt_str(c, "surname", path),
                                  slug=_str(c, "slug", path), level=_int(c, "level", path),
                                  cls=self.classes.get(_int(_dict(c, "playable_class", path), "id", path))))
        return rows, got.modified

    def character(self, slug: str, since: str | None = None) -> CharacterFacts | Any:
        """A character's profile (`slug` = address(name, surname) or a roster row's slug); NOT_MODIFIED on a 304 to
        `since`. ArmoryMissing below level 10 or before the character's first logout."""
        path = f"/profile/wow/character/{_in_path(slug)}"
        got = self._get(path, self.profile_ns, since, missing=CHARACTER_MISSING_WORDS)
        if got is NOT_MODIFIED:
            return NOT_MODIFIED
        d = got.data
        g = d.get("guild")
        _need(g is None or isinstance(g, dict), path, "guild")
        return CharacterFacts(id=_int(d, "id", path), name=_str(d, "name", path), surname=_opt_str(d, "surname", path),
                              cls=self.classes.get(_int(_dict(d, "character_class", path), "id", path)),
                              level=_int(d, "level", path), guild_id=_int(g, "id", path) if g else None, modified=got.modified)

    def professions(self, slug: str) -> list[dict]:
        """[{id: profile profession id, skill: int, recipes: [{id, name}]}] in the armory's order (primaries, then
        secondaries); a profession the profile doesn't map is skipped. skill = the highest tier's skill points; recipes
        from every tier, once each, ids as the armory gives them (an id the recipe list lacks is kept)."""
        path = f"/profile/wow/character/{_in_path(slug)}/professions"
        d = self._get(path, self.profile_ns, missing=CHARACTER_MISSING_WORDS).data
        out = []
        for group in ("primaries", "secondaries"):
            for p in _list(d, group, path, optional=True):
                _need(isinstance(p, dict), path, group)
                bid = _int(_dict(p, "profession", path), "id", path)
                skill, recipes, seen = 0, [], set()
                for t in _list(p, "tiers", path):
                    _need(isinstance(t, dict), path, "tier")
                    skill = max(skill, _int(t, "skill_points", path))
                    for r in _list(t, "known_recipes", path, optional=True):
                        _need(isinstance(r, dict), path, "known recipe")
                        rid, name = _int(r, "id", path), _str(r, "name", path)
                        if rid not in seen:
                            seen.add(rid)
                            recipes.append({"id": rid, "name": name})
                if bid in self.profession_ids:
                    out.append({"id": self.profession_ids[bid], "skill": skill, "recipes": recipes})
        return out


def _retry_after(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


# ------------------------------------------------------------------ per guild


def available(reg) -> bool:
    """The guild's game version has an armory (integrations.yaml) and the host's credentials are in .env."""
    return bool(reg.profile.armory) and battlenet.credentials_set()


_CLIENTS: dict[tuple, Armory] = {}
_CLIENTS_LOCK = threading.Lock()


def client_for(reg) -> Armory:
    """The process's Armory for the guild's version and region (one per region; the shared token and limiter)."""
    a = reg.profile.armory
    if not a:
        raise ArmoryError(NO_ARMORY_WORDS, f"profile {reg.profile.name} has no armory")
    region = str(a["region"])
    key = (reg.profile.name, region)
    with _CLIENTS_LOCK:
        if key not in _CLIENTS:
            _CLIENTS[key] = Armory(region, str(a["profile_namespace"]).format(region=region), str(a["static_namespace"]).format(region=region),
                                   classes=a.get("classes") or {}, rulesets=a.get("rulesets") or {},
                                   professions={p.blizzard_id: p.id for p in reg.profile.professions.values() if p.blizzard_id is not None})
        return _CLIENTS[key]
