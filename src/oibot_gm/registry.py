"""Member/character registry backed by the git store.

One JSON file per Discord member under <guild>/members/<discord_id>.json.
Characters belong to exactly one member; one of them is the main. Officers
confirm characters and set ranks. Every change is a store commit."""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .constants import RANK_ORDER, ROLES
from .profiles import GameProfile
from .store import GitStore

log = logging.getLogger(__name__)

RANKS = ("trial", "raider", "core", "alt", "social")
NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ]{2,12}$")
PLACEMENT_ASK_HISTORY = 12  # placement asks kept per member
# the member's inbox (design §2.5 / D26): an outcome a person should know reaches them as a NOTICE on the Me page and
# /me view — never a courtesy DM (the DM rule). Stored on the member file, oldest first; capped and expired here.
NOTICE_CAP = 20
NOTICE_DAYS = 30
NOTICE_TEXT_MAX = 300
NOTICE_KINDS = ("not_rostered", "released", "answered_for_you", "cancelled", "schedule_removed", "application", "loot_award", "ask_closed", "confirmed",
                "appeal")  # appeal: the council answered a member's question about an award (appeals.py, design §5.49)
MAX_ABSENCE_DAYS = 120
ABSENCE_REASON_MAX = 200  # an absence's reason (officer-only prose), capped at the verb so every surface refuses alike
PLAYER_RANKS = ("trial", "raider", "core")
DEFAULT_RAID_SIZE = 20  # when neither the roster nor the raid says


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


WISHLIST_MAX = 10  # items per character (D5)
WISH_NOTE_MAX = 120
TIER_VALUES = ("S+", "S", "A", "B")  # what Loot › Items writes per spec key ("-" clears a cell)


class WishlistEntry(BaseModel):
    """One wanted item on a character's wishlist (D5: shaped like a ThatsMyBIS wishlist row — per character, priority
    order, main/offspec, a note — so a TMB export imports into it one row to one entry). `item_id` 0 = an item the
    tables don't know yet (kept by name, matched by name until it is learned). `source` native | tmb: an import replaces
    only its own earlier rows and never deletes a native one. Received is never stored: the API shows an entry the
    ledger already awarded to this character as received, so the next Save can't undo it."""

    item_id: int = 0
    name: str
    rank: int = 1  # 1 = most wanted (TMB's sort order)
    offspec: bool = False  # MS (False) / OS (True)
    note: Optional[str] = None
    source: str = "native"  # native | tmb
    added_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def key(self) -> str:
        return item_key(self.item_id, self.name)


class RegisteredCharacter(BaseModel):
    name: Optional[str] = None  # first name; None until the character exists (pre-launch "planned")
    surname: Optional[str] = None  # WoW: Forever names are first + last
    cls: str
    spec: str
    offspec: Optional[str] = None
    is_main: bool = False
    rank: str = "trial"
    status: str = "active"  # planned | active | retired
    rosters: list[str] = Field(default_factory=list)  # regular-series keys "<raid>:<schedule>" (seats on runs live in the run files)
    flex: list[str] = Field(default_factory=list)  # extra roles this character can play besides its spec/offspec
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    note: Optional[str] = None  # officer-only
    wishlist: list[WishlistEntry] = Field(default_factory=list)  # ranked wanted items (Registry.set_wishlist; cap WISHLIST_MAX)

    @property
    def label(self) -> str:
        if self.name:
            return f"{self.name} {self.surname}" if self.surname else self.name
        return f"{self.cls} ({self.spec})"

    def matches(self, ref: str) -> bool:
        r = ref.strip().lower()
        return bool(r) and (self.label.lower() == r or (self.name or "").lower() == r)


class Absence(BaseModel):
    start: str  # YYYY-MM-DD inclusive
    end: str  # YYYY-MM-DD inclusive
    reason: Optional[str] = None  # officer-only
    by: str  # who recorded it (member or officer)
    created_at: str = Field(default_factory=now)


class Member(BaseModel):
    """Legacy keys in old member files (availability, role_prefs, slot_prefs, week, teams) are ignored on load;
    `reload()` migrates role_prefs.flex and teams onto the main character once."""

    model_config = ConfigDict(extra="ignore")

    discord_id: int
    display_name: str
    characters: list[RegisteredCharacter] = Field(default_factory=list)
    absences: list[Absence] = Field(default_factory=list)
    placement_asks: list[dict] = Field(default_factory=list)  # {roster, character, asked_at, answer yes|no|None, answered_at, by}
    # the inbox (D26): {id, kind (NOTICE_KINDS), text, run (run key or None), at (ISO), read_at (ISO or None)} — outcomes
    # the person should know (not rostered, seat released, answered for you, cancelled, …); Registry.notify writes them
    notices: list[dict] = Field(default_factory=list)
    dm_opt_out: bool = False
    # set when they leave the Discord server (on_member_remove, needs the members intent); cleared when they're seen again.
    # Officers then see "left the server" and may Remove record (Registry.remove_record) — members never delete themselves (D29)
    left_at: Optional[str] = None
    test: bool = False  # seeded by /gm test: the bot puppets them (DMs go to the roster channel; officers answer for them)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    def upcoming_absences(self, today: str) -> list[Absence]:
        return sorted([a for a in self.absences if a.end >= today], key=lambda a: a.start)

    def absent_on(self, day: str) -> Optional[Absence]:
        return next((a for a in self.absences if a.start <= day <= a.end), None)

    @property
    def main(self) -> Optional[RegisteredCharacter]:
        return next((c for c in self.characters if c.is_main and c.status in ("active", "planned")), None)

    def active(self) -> list[RegisteredCharacter]:
        """Characters that count for sheets and planning (planned ones have no name yet)."""
        return [c for c in self.characters if c.status in ("active", "planned")]

    def planned(self) -> list[RegisteredCharacter]:
        return [c for c in self.characters if c.status == "planned"]

    @property
    def rank(self) -> Optional[str]:
        """The member's rank (D25: rank is per MEMBER, stored on the main; every other character reads 'alt')."""
        return self.main.rank if self.main else None


def settle_ranks(m: Member, rank: str | None = None) -> None:
    """Rank lives on the member (owner decision D25): the main carries it, every other character — alts, retired ones —
    reads 'alt'. `rank` = the member's rank to put on the main (None keeps what the main has). Called by every verb that
    moves the main (set_main, retire, delete, add/plan a main) so the standing follows the player, never the character."""
    main = m.main
    for c in m.characters:
        if c is main:
            if rank:
                c.rank = rank
        elif c.rank != "alt":
            c.rank = "alt"


def member_rank(m: Member | None) -> str:
    """The rank a new main inherits: the current main's, else 'trial' (a first character)."""
    return m.main.rank if m is not None and m.main is not None else "trial"


class Question(BaseModel):
    """One of the guild's own application questions. Discord caps a modal label at 45 characters and a description at
    100, so the question is a short label plus an optional hint; answers are prose (validated for length, never parsed)."""
    id: str
    label: str  # ≤ 45
    hint: Optional[str] = None  # ≤ 100
    required: bool = False


class Recruitment(BaseModel):
    open: bool = True  # closed: /apply and the Apply button explain that instead of opening the form
    note: Optional[str] = None  # one line shown on the Apply screen (e.g. "looking for healers")
    questions: list[Question] = Field(default_factory=list)


QUESTIONS_MAX = 15  # three pages of five in Discord's modals
ANSWER_MAX = 1000
DECISION_NOTE_MAX = 400  # the officer's note to an applicant (the decide screen's box and the web's)
ERROR_DM_MAX = 5  # people who get the error alerts (GuildConfig.error_dm_ids)
FEED_TOKEN_MAX = 10  # companion tokens a guild may hold at once (unrevoked)
FEED_LABEL_MAX = 40  # a companion token's label ("Jon's PC"): prose, validated, never parsed
FEED_SEEN_SAVE_S = 900  # a token's last_seen reaches guild.yaml at most this often (it is updated in memory on every frame)


class FeedToken(BaseModel):
    """A companion token of this guild (design §5.50; out/dual_interface_design.md §3.1 L08/S-03). The token itself is a
    SECRET: only its hash is stored (`hash` = feed.hash_token — sha256 of a 256-bit random value), the value is shown once
    at issue (DMed to the raid leader, or in the site's one response) and never logged, committed or audited. `id` and
    `label` are what every line names."""

    id: str
    hash: str
    label: str
    issued_by: str
    issued_by_id: Optional[int] = None
    issued_at: str
    sent_to: Optional[str] = None  # the display name it was DMed to (Discord's Issue), None when shown on the site
    revoked_at: Optional[str] = None
    revoked_by: Optional[str] = None
    last_seen: Optional[str] = None  # the companion's last frame with it (UTC ISO; saved at most every FEED_SEEN_SAVE_S)

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def prior_row(a: "Applicant") -> dict:
    """An application's verdict as a history row (what the next application of the same person carries)."""
    return {"label": a.label, "status": a.status, "by": a.decided_by, "at": a.decided_at or a.created_at, "note": a.decision_note}


class Applicant(BaseModel):
    @property
    def label(self) -> str:
        return f"{self.name} {self.surname}" if self.surname else self.name


    discord_id: int
    display_name: str
    name: str
    cls: str
    spec: str
    offspec: Optional[str] = None
    logs_url: Optional[str] = None
    availability: Optional[str] = None
    about: Optional[str] = None
    surname: Optional[str] = None  # Forever names are two words; carried onto the character on accept
    schedules: list[str] = Field(default_factory=list)  # "<raid id>:<schedule id>" the applicant can make
    answers: list[dict] = Field(default_factory=list)  # [{label, answer}] — the guild's questions as asked (a snapshot: later edits don't reword it)
    source: str = "discord"  # discord (the /apply wizard / the card's Apply button)
    status: str = "open"  # open | accepted | declined | withdrawn
    created_at: str = Field(default_factory=now)
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decision_note: Optional[str] = None
    message_id: Optional[int] = None  # review card in the applications channel
    # earlier applications of the same person, oldest first: {label, status, by, at, note} — the card's and the
    # Applications page's "Applied before: declined 12 Mar by Jonny — full on healers" (Registry.prior_decision_line)
    history: list[dict] = Field(default_factory=list)
    role_note: Optional[str] = None  # what Accept did about the member role (D31): "gave them @Member" / why it couldn't

    @property
    def label(self) -> str:
        return f"{self.name} {self.surname}" if self.surname else self.name


# ---- plain-text permissions (design §5.25): what a sentence to the bot may DO, by capability group, by who, by where.
# Every configops op belongs to exactly one group (configops.OP_GROUP); an op a member aims at their own record is
# "self" instead of its usual group (configops.bind_self decides that in code, never the model).
# id -> (label, default who, what a person could type)
PLAIN_GROUPS: dict[str, tuple[str, list[str], str]] = {
    "self": ("Own record", ["registered"], "I'll be away Nov 1–3 · make Xanthe my main · I can't make tonight"),
    "members": ("Other people's records", ["officers"], "rank Jonny raider · Mira is away next week · turn Kessa's DMs off"),
    "runs": ("Runs", ["officers"], "lock tonight's Barrow Deeps · Xanthe confirmed for tonight · pin Jonny in tonight · fill the empty seats"),
    "comp": ("Comp & policy", ["officers"], "we want 3–4 healers in Barrow Deeps · add a loot rule"),
    "raids": ("Raids & auras", ["owner"], "Barrow Deeps runs Tue and Thu 7:30 pm · fortitude is party-wide"),
    "setup": ("Guild setup", ["owner"], "post raid sheets in #signups · add @Raid Lead as an officer role"),
    "test": ("Test bench", ["officers"], "seed 20 puppets · clear the test bench"),
    "loot": ("Loot council", ["officers"], "award the Syphon to Eltam instead — tanks progression · confirm the rest · tier the Robe S for holy priests"),
}
PLAIN_WHO = ("everyone", "registered", "officers", "owner")  # plus "role:<discord role id>"
# act = anything the person's groups allow · self = own record only (other requests are refused with where to go)
# answer = questions only · ignore = the bot doesn't answer @mentions there at all
PLAIN_MODES = ("act", "self", "answer", "ignore")
PLAIN_WEB = "web"  # POST /api/ops/change (MCP plain_change): not a Discord channel, always "act"
# Listening (design §5.59): channels the bot reads without being tagged; a decision service says whether a message is for it.
# watch = it only notes what it would have answered · reply = a message that is clearly for it is handled like an @mention
LISTEN_MODES = ("watch", "reply")
LISTEN_WORDS = {"watch": "Watches", "reply": "Replies"}


class PlainPolicy(BaseModel):
    """Owner-set (web Ops page, MCP set_plain_permissions, the owner's plain-text `plain` op). Missing groups use their PLAIN_GROUPS default; unlisted channels use `default`,
    except the ops and analytics channels, which act unless listed."""
    groups: dict[str, list[str]] = Field(default_factory=dict)
    channels: dict[str, str] = Field(default_factory=dict)  # channel id (as a string) -> mode
    dm: str = "self"
    default: str = "self"
    listen: dict[str, str] = Field(default_factory=dict)  # channel id (as a string) -> watch | reply (§5.59); absent = not listening


SUPERUSERS_ENV = "OIBOT_SUPERUSERS"  # .env: Discord user ids (comma/space separated) with owner rights in EVERY guild the bot serves


def superusers() -> frozenset[int]:
    """The host's superusers: owner of every guild (configure anything, claim any server the bot is in), read per call."""
    return frozenset(int(x) for x in re.split(r"[\s,]+", os.environ.get(SUPERUSERS_ENV, "")) if x.strip().isdigit())


def is_superuser(uid) -> bool:
    try:
        return uid is not None and int(uid) in superusers()
    except (TypeError, ValueError):
        return False


class GuildConfig(BaseModel):
    key: str
    name: str
    game_profile: str
    discord_guild_id: int
    owner_discord_id: Optional[int] = None
    ops_channel_id: Optional[int] = None
    applications_channel_id: Optional[int] = None  # defaults to the ops channel
    signup_channel_id: Optional[int] = None  # where raid sheets are posted (public)
    roster_channel_id: Optional[int] = None  # officer channel: overview, proposals, officer health cards
    registration_channel_id: Optional[int] = None  # public, read-only: the bot keeps the registration card here
    registration_message_id: Optional[int] = None
    analytics_channel_id: Optional[int] = None  # officer: live pool-readiness cards (edited on every change) + change log
    analytics_message_ids: dict[str, int] = Field(default_factory=dict)  # roster key -> card message id
    absences_channel_id: Optional[int] = None  # public: 'I'll be away' card + one line per absence (reason stays officer-only)
    absences_message_id: Optional[int] = None
    news_channel_id: Optional[int] = None  # officer/read-only: a webhook posts game news here; the bot keeps the relevant items (news.py)
    loot_channel_id: Optional[int] = None  # public loot log: one line per confirmed award (no channel: the run's updates thread)
    loot_log: bool = True  # the public loot log is on (off: awards are recorded and shown on the site only)
    news_keywords: list[str] = Field(default_factory=list)  # extra words that make a news item relevant (over the profile's news.yaml)
    timezone: str = "America/Los_Angeles"  # guild time: every schedule, window and displayed clock uses it
    ask_audience: str = "registered"  # who may ask the LLM free-form questions: officers | confirmed | registered | everyone
    about: Optional[str] = None  # short public blurb for the static guide (owner-set)
    invite_url: Optional[str] = None  # the public Discord invite the homepage's Join button opens (anyone can join; members apply inside)
    # listed (B10, design §5.49): the guild has a card on the public homepage, the not-a-member page, GET /api/public and a
    # public /schedule page. False = UNLISTED: none of those name it (its members still log in and see it in their own list)
    public: bool = True
    recruitment: Recruitment = Field(default_factory=Recruitment)  # the Discord application: open/closed, a note, the guild's questions
    # people holding one of these Discord roles register without applying (existing members who haven't registered yet); officers always can
    member_role_ids: list[int] = Field(default_factory=list)
    # officer roles are Discord role IDS (a rename keeps them; a same-named role made by someone with Manage Roles does not count).
    # `officer_roles` (names) is the legacy field: migrated to ids once by Registry.resolve_officer_roles when the bot sees the guild;
    # until then, and only while no id is configured, names still match so nobody is locked out on the first start after the upgrade.
    officer_role_ids: list[int] = Field(default_factory=list)
    officer_roles: list[str] = Field(default_factory=list)  # legacy names; empty once resolved (unresolvable names stay, for the ops warning)
    # rosters: {key, name, size, schedule: "Tue 19:30", instance, cutoff_soft_hours, cutoff_hard_hours, open_days_before, reminders, open_dm}
    rosters: list[dict] = Field(default_factory=list)
    # raids: guild overrides per instance id over profiles/<game>/raids.yaml: {lockout_days, duration_hours, comp: {tank/healer/dps: {min,max}}, notes}
    raids: dict[str, dict] = Field(default_factory=dict)
    # auras: what the guild has learned about Forever's buffs, over profiles/<game>/buffs.yaml
    buffs: dict[str, dict] = Field(default_factory=dict)  # buff id -> {scope, family, strength, status, note}
    families: dict[str, dict] = Field(default_factory=dict)  # family id -> {name, value: {key: points}, status, note}
    plain: PlainPolicy = Field(default_factory=PlainPolicy)  # who may act through plain text, and where
    # loot scoring overrides over profiles/<game>/loot_defaults.yaml (Registry.loot_cfg merges them; untiered_weight, …)
    loot: dict = Field(default_factory=dict)
    # who gets the error alerts (ops.Ops.emit, one DM per distinct error per 30 min — the DM rule's one exception): None = the
    # owner (the default); a list = those Discord users; [] = nobody (errors stay in the ops channel). Registry.error_dm_targets
    error_dm_ids: Optional[list[int]] = None
    # the bot was removed from the server (on_guild_remove → Guilds.detach): the guild is parked — out of routing, the
    # scheduler, the feed, the picker and the homepage — until the bot is back in that server (on_guild_join / available)
    detached_at: Optional[str] = None
    # companion tokens (design §5.50): HASHED, issued and revoked by the owner (Registry.issue_feed_token / revoke_feed_token);
    # read by the feed listener at every hello and frame, so an issue or a revoke takes effect without a restart
    feed_tokens: list[FeedToken] = Field(default_factory=list)

    def __init__(self, **data):
        if "raid_teams" in data and not data.get("rosters"):
            data["rosters"] = data.pop("raid_teams")  # pre-roster config files
        data.pop("raid_teams", None)
        super().__init__(**data)

    # ---- officer roles
    def officer_by_roles(self, roles) -> bool:
        """Does this set of Discord roles (objects with .id and .name) carry officer rights? Ids decide; the legacy
        names are consulted only while no id has been configured yet (first start before resolve_officer_roles ran)."""
        if self.officer_role_ids:
            ids = set(self.officer_role_ids)
            return any(getattr(r, "id", None) in ids for r in roles)
        if self.officer_roles:
            names = set(self.officer_roles)
            return any(getattr(r, "name", None) in names for r in roles)
        return False

    def officer_roles_pending(self) -> list[str]:
        """Legacy names not yet turned into ids (the bot resolves them against the guild's roles on ready)."""
        return list(self.officer_roles)

    # canonical names
    def roster_keys(self) -> list[str]:
        """The per-run roster dicts' keys (live runs). No "main" fallback any more: regular rosters are series keys
        (Registry.series), never a config roster."""
        return [t["key"] for t in self.rosters]

    def roster(self, key: str) -> Optional[dict]:
        return next((t for t in self.rosters if t["key"] == key), None)

    # compatibility aliases (older call sites)
    @property
    def raid_teams(self) -> list[dict]:
        return self.rosters

    @raid_teams.setter
    def raid_teams(self, v: list[dict]) -> None:
        self.rosters = v

    def team_keys(self) -> list[str]:
        return self.roster_keys()

    def team(self, key: str) -> Optional[dict]:
        return self.roster(key)


class RegistryError(ValueError):
    pass


class ConfigChanged(RegistryError):
    """A config write made against a section that changed since its author looked (its `rev` is stale) — the sibling of
    rc.BoardChanged for guild settings (design §2.4). Nothing was written; the caller re-reads and shows the current state."""

    def __init__(self, section: str, msg: str | None = None):
        self.section = section
        super().__init__(msg or f"The {section_words(section)} changed since you looked — check them and try again.")


class CharactersChanged(RegistryError):
    """A character table save carried a `characters_rev` that isn't the member's any more: someone else (an officer,
    the member on another device, a wizard) changed their characters since the editor loaded them. Nothing was written."""

    def __init__(self, m: "Member"):
        self.member = m
        super().__init__(f"{m.display_name}'s characters changed since you loaded them — reload, then make your change again.")


class WishlistChanged(RegistryError):
    """A whole-wishlist save made against a list that changed since its author opened it (a stale wishlist_rev)."""


def feed_token_id(used: set[str] | None = None) -> str:
    """A short id for a companion token record ('ft-3fa9c1'): what lines, buttons and routes name — never the value."""
    import secrets

    used = used or set()
    while True:
        tid = f"ft-{secrets.token_hex(3)}"
        if tid not in used:
            return tid


# guild.yaml fields no person or prompt reads as raw config: the companion token records (hashes, labels, who got them —
# the owner sees them on the Companions hub only, design §5.50)
PRIVATE_CONFIG_FIELDS = ("feed_tokens",)


def readable_config(cfg: "GuildConfig") -> dict:
    """The guild config as a dict a person or a prompt may read (the site's 'As stored', the config parser's context):
    model_dump() without PRIVATE_CONFIG_FIELDS. guild.yaml itself (save_config) keeps them."""
    out = cfg.model_dump()
    for f in PRIVATE_CONFIG_FIELDS:
        out.pop(f, None)
    return out


def section_words(section: str) -> str:
    if section.startswith("raids:"):
        return f"{section[6:]} raid settings"
    return {"settings": "guild settings", "buffs": "aura settings", "recruitment": "recruitment settings", "plain": "plain-text permissions",
            "loot": "loot scoring weights"}.get(section, section)


def _config_verb(section):
    """A mutate-validate-save config verb: runs under the Registry's `config_lock` (web mutations run in worker threads
    while Discord mutates on the loop) and accepts `rev=` — the section rev its author saw (Registry.config_rev); a
    different one raises ConfigChanged before anything is touched. `section` is the name or a callable of the call's
    arguments (raids:<id>)."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, rev=None, **kwargs):
            with self.config_lock:
                self.check_rev(section(self, *args, **kwargs) if callable(section) else section, rev)
                return fn(self, *args, **kwargs)

        return wrapper

    return deco


def _member_write(fn):
    """A member-record write (registration, plans, characters, regulars, placement asks): under the Registry's
    `config_lock`, like the character table and the config verbs — the web saves in worker threads while Discord writes
    on the loop (design §2.4), and a read-modify-save that interleaves with another loses one of them. Re-entrant: the
    table's save calls these under the same lock."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.config_lock:
            return fn(self, *args, **kwargs)

    return wrapper


def _raid_section(self, instance, *a, **k) -> str:
    return f"raids:{instance}"


# ---- guild.yaml `loot:` (the scoring weights' overrides): nested maps addressed by dotted paths ('wishlist.rank_bonus.1')
def _loot_key(d: dict, seg: str):
    """The key of `d` whose text is `seg` (rank_bonus keys are ints in the profile, text in a path), else `seg` itself."""
    return next((k for k in d if str(k) == seg), seg) if isinstance(d, dict) else seg


def _merge_loot(base: dict, over: dict) -> None:
    """Merge the guild's overrides into the profile's settings at every depth, matching keys by their text."""
    for k, v in (over or {}).items():
        bk = _loot_key(base, str(k))
        if isinstance(v, dict) and isinstance(base.get(bk), dict):
            _merge_loot(base[bk], v)
        else:
            base[bk] = v


def _loot_dig(d: dict, path: str):
    """The value at a dotted path, or None."""
    cur = d
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        k = _loot_key(cur, seg)
        if k not in cur:
            return None
        cur = cur[k]
    return cur


def _loot_keys(profile_loot: dict, path: str) -> list:
    """A dotted path as the profile's own keys (an int stays an int, so the stored override lands where scoring reads)."""
    keys, cur = [], profile_loot
    for seg in path.split("."):
        k = _loot_key(cur, seg)
        keys.append(k)
        cur = cur.get(k) if isinstance(cur, dict) else None
    return keys


def _loot_put(over: dict, keys: list, value) -> None:
    """Set (value) or remove (None) one override, creating / leaving nested maps as needed (an override's own key that
    reads the same as the profile's — '1' for 1 — is replaced, never kept beside it)."""
    cur = over
    for k in keys[:-1]:
        hit = _loot_key(cur, str(k))
        if hit != k and hit in cur:
            cur[k] = cur.pop(hit)
        cur = cur.setdefault(k, {})
        if not isinstance(cur, dict):
            return
    last = keys[-1]
    hit = _loot_key(cur, str(last))
    if hit in cur:
        cur.pop(hit)
    if value is not None:
        cur[last] = value


def _loot_prune(d: dict) -> dict:
    """Overrides without the empty maps a removal leaves."""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            v = _loot_prune(v)
            if not v:
                continue
        out[k] = v
    return out


# ---- guild identity (design §5.32): the folder name IS the key; it never changes after creation. 1–16 characters of
# [a-z0-9_-], so a key fits every persistent custom id (`runact.<gk>:<run>:…` stays under Discord's 100) and can never
# contain the `.`/`:` separators. Everything outside Discord (custom ids, ?g=, the cookie, MCP guild=) names a guild by it.
GUILD_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,15}")
GUILD_KEY_SLUG = 12  # the slug part of a picked key; a "-N" suffix keeps it within 16
RESERVED_GUILD_KEYS = frozenset({"out", "fixtures", "profiles"})


def guild_key_for(name: str, taken: set[str] | frozenset[str]) -> str:
    """A new guild's key from its Discord server name: casefolded, transliterated to ASCII, `[a-z0-9]` runs joined
    with '-', cut to 12 and stripped of '-' ('guild' when nothing is left); a taken key gets -2, -3, … . `taken` = every
    directory in the data root (guild or not), the mock fixtures' key and the reserved names. Nobody ever types a key."""
    import unicodedata

    ascii_name = unicodedata.normalize("NFKD", str(name or "").casefold()).encode("ascii", "ignore").decode()
    slug = "-".join(re.findall(r"[a-z0-9]+", ascii_name))[:GUILD_KEY_SLUG].strip("-") or "guild"
    taken = {str(t).casefold() for t in taken} | set(RESERVED_GUILD_KEYS)
    if slug not in taken:
        return slug
    n = 2
    while True:
        suffix = f"-{n}"
        key = slug[:16 - len(suffix)].strip("-") + suffix
        if key not in taken:
            return key
        n += 1


def create_guild(store: GitStore, cfg: GuildConfig, profile: GameProfile, code_root: Path | None = None) -> "Registry":
    """The one way a guild comes into being (the claim wizard, move tooling, tests): write <key>/guild.yaml exactly as
    Registry.save_config writes it, one commit, and return its Registry. Refuses a bad or taken key and a data root
    under the code repo's fixtures/ (a dev run must never write a real guild into the public repo)."""
    from .store import is_fixture_root

    code_root = code_root or Path(__file__).resolve().parents[2]
    if is_fixture_root(code_root, store.root):
        raise RegistryError("the data root is the code repo's fixtures/ — a guild can't be created there")
    if not GUILD_KEY_RE.fullmatch(cfg.key or "") or cfg.key in RESERVED_GUILD_KEYS:
        raise RegistryError(f"bad guild key {cfg.key!r}")
    if (store.root / cfg.key).exists():
        raise RegistryError(f"{cfg.key} already exists in the data repo")
    cfg = GuildConfig.model_validate(cfg.model_dump())
    store.write_text(Path(cfg.key) / "guild.yaml", "# oibot_GM guild config — edit via /gm config or by PR.\n" + yaml.safe_dump(cfg.model_dump(), sort_keys=False))
    store.commit(f"{cfg.key}: guild created for {cfg.name}")
    return Registry(store, cfg.key, profile)


SAVE_LISTENERS: list = []  # process-wide fn(reg, kind: "member"|"config", lines) after every commit (the web's event stream)
CHANGES_KEEP = 200
RECENT_CHANGES: "deque[tuple[str, str, str, str]]" = deque(maxlen=CHANGES_KEEP)  # (guild key, ISO time, kind, line) — every registry change line, newest last


def recent_changes(guild_key: str | None = None, limit: int = 50) -> list[dict]:
    """The last registry change lines (what the analytics change log posts), newest first: {time, kind, text}.
    In memory only (since the bot started); the data repo's git log is the full audit trail."""
    rows = [r for r in reversed(RECENT_CHANGES) if guild_key is None or r[0] == guild_key]
    return [{"time": t, "kind": k, "text": text} for _, t, k, text in rows[:max(0, int(limit))]]
# seat weights at lock (raidcycle.seat_bonus). `regular`: a regular of the run's series signed with their regular character
# (roster redesign D9: a weight worth more than one rank step — the rank term moves in steps of `rank` = 3 — so a strong
# outsider can still win a role seat, but between equals the regular sits)
RAID_WEIGHT_DEFAULTS = {"rank": 3, "main": 2, "sat_out": 2, "signup_order": 1, "regular": 4}
RAID_HOURS_FIELDS = ("signup_lead_hours", "lock_hours_before", "confirm_hours_before", "nudge_hours_before", "fill_ask_hours")
# true/false raid settings (command + API layers parse them the same way). `confirm`: the lock asks everyone rostered to
# Confirm / Can't make it (off: Join is the commitment, nobody is asked and no seat expires — the ad hoc default, D5)
RAID_BOOL_FIELDS = ("nudge", "autofill", "open_dm", "confirm")
# loot, per raid (not per schedule): loot_auto_distribute = a companion's drops are distributed ~20 s after a boss's last
# drop (design §3.1 decision 6, default on — raid_def fills the default)
RAID_LOOT_FIELDS = ("loot_auto_distribute",)
# effective defaults under profiles/<game>/raids.yaml and guild overrides (nudge_hours_before is derived: halfway between open and lock)
RAID_DEFAULTS = {"lockout_days": 7, "duration_hours": 3, "signup_lead_hours": 120, "lock_hours_before": 24, "confirm_hours_before": 6,
                 "split_policy": "balanced", "nudge": True, "fill_ask_hours": 4, "autofill": True, "open_dm": False, "confirm": True}
# ---- ad hoc (roster redesign §2, D4/D5/D17): a pickup template or a one-off run takes the raid's IDENTITY (size, comp,
# lockout, duration, first opening) but none of its recurring timing — Barrow Deeps' 24 h lock and 36 h reminder are
# Tuesday decisions. These are guild habits, not game data, so they live here next to RAID_DEFAULTS (not in profiles/).
# No reminder, no open DM, no confirmations, lock 2 h before, fill asks 1 h, first come first seated (signup order is
# the weight that counts; seat_bonus reads it per position for an ad hoc run).
ADHOC_DEFAULTS = {"signup_lead_hours": 168, "nudge_hours_before": 3, "lock_hours_before": 2, "confirm_hours_before": 1, "fill_ask_hours": 1,
                  "nudge": False, "open_dm": False, "confirm": False, "autofill": True, "split_policy": "balanced"}
ADHOC_WEIGHTS = {"rank": 0, "main": 1, "sat_out": 0, "signup_order": 6, "regular": 0}
RAID_IDENTITY = ("name", "size", "comp", "lockout_days", "duration_hours", "first_open", "notes", "comp_targets", "comp_groups", "slots", "schedules")
SPLIT_POLICIES = ("balanced", "first", "rotation")  # how a slot with more joiners than one run seats is split at the scheduled lock
CLOCK12 = "%a %d %b %I:%M %p"  # the one human-facing clock format; %H:%M is for machines
UNTIERED_WEIGHT = 0.5  # loot: the upgrade value of an item with no tier data (Registry.loot_cfg `untiered_weight`)
ATTENDANCE_WINDOW_DAYS = 30  # attendance window when the loot policy names none (loot_defaults.yaml attendance.window_days)
ATTENDANCE_MIN_RUNS = 3  # below this many real done runs in the window attendance is unknown: neutral in loot scoring


def item_key(item_id, name: str | None = None) -> str:
    """An item's key in Registry.items() and its file name under <guild>/items/: the id as text, or 'n-<slug of the
    name>' for an item known only by name (a human ticked 'Not on the table')."""
    if item_id not in (None, "", 0, "0"):
        return str(int(item_id))
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60]
    return f"n-{slug or 'unnamed'}"


def _same_wish(id_a, name_a, id_b, name_b) -> bool:
    """One item on a wishlist: the same id (both known), or the same name (either known only by name)."""
    if id_a and id_b:
        return int(id_a) == int(id_b)
    return str(name_a or "").strip().lower() == str(name_b or "").strip().lower()


def _wish_rows(entries) -> list[dict]:
    return [{"item_id": e.item_id, "name": e.name, "rank": e.rank, "offspec": e.offspec, "note": e.note, "source": e.source} for e in entries]


def _wish_diff(label: str, old, new) -> str | None:
    """'Xanthe's wishlist: + Robe of Tests (OS); − Wand; re-ordered' — None when nothing changed."""
    def sig(es):
        return [(e.item_id, e.name.lower(), e.offspec, e.note or "") for e in es]
    if sig(old) == sig(new):
        return None
    added = [e for e in new if not any(_same_wish(o.item_id, o.name, e.item_id, e.name) for o in old)]
    gone = [o for o in old if not any(_same_wish(o.item_id, o.name, e.item_id, e.name) for e in new)]
    parts = [f"+ {e.name}" + (" (OS)" if e.offspec else "") for e in added] + [f"− {o.name}" for o in gone]
    kept_old = [o.name.lower() for o in old if o not in gone]
    kept_new = [e.name.lower() for e in new if e not in added]
    if kept_old != kept_new:
        parts.append("re-ordered")
    elif not parts:
        parts.append("MS/OS or notes changed")
    return f"{label}'s wishlist: " + "; ".join(parts) + f" ({len(new)}/{WISHLIST_MAX})"
TRUE_WORDS = ("true", "yes", "on", "1")


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_WORDS


def default_nudge_hours(lead_hours: float, lock_hours: float) -> float:
    """The nudge lands halfway between signups opening and the lock, never after the lock."""
    return max(float(lock_hours), float(lock_hours) + (float(lead_hours) - float(lock_hours)) / 2)
_SLOT_RE = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+([01]?\d|2[0-3]):([0-5]\d)$", re.I)


def parse_slots(text) -> list[str]:
    """'Tue 19:30, Thu 20:00' (or a list) → normalised ['Tue 19:30', 'Thu 20:00']; raises RegistryError on junk."""
    raw = text if isinstance(text, list) else [x for x in str(text or "").replace(";", ",").split(",")]
    out = []
    for x in raw:
        x = str(x).strip()
        if not x:
            continue
        m = _SLOT_RE.match(x)
        if not m:
            raise RegistryError(f"slot {x!r} should look like 'Tue 19:30'")
        out.append(f"{m.group(1)[:3].title()} {int(m.group(2)):02d}:{m.group(3)}")
    if len(set(out)) != len(out):
        raise RegistryError("a slot is listed twice")
    return out


# ---- schedules: WHEN a raid runs, as entries under the raid (design.md §5.27). A raid keeps its identity (size, comp,
# lockout, weights) and its cadence fields as the DEFAULTS; each schedule says when runs happen and may override any
# cadence field. The raid's own `slots` are the implicit weekly schedule `default`, so a raid that only has slots behaves
# exactly as before (same starts, same run keys); guild.yaml `raids.<id>.schedules` holds the rest (and `default`'s own
# settings — never its slots, which stay on the raid).
DEFAULT_SCHEDULE = "default"
SCHEDULE_KINDS = ("weekly", "lockout", "pickup")  # weekly slots · day N of each lockout · a template opened on demand
SCHEDULE_CADENCE = ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours",
                    "nudge", "autofill", "open_dm", "confirm", "split_policy")  # unset = the raid's value (ad hoc: ADHOC_DEFAULTS)
# `weights`: a partial seat-order map {rank, main, sat_out, signup_order, regular} merged per key over the raid's (ad hoc: ADHOC_WEIGHTS)
SCHEDULE_FIELDS = ("name", "kind", "active", "slots", "days", "time", "rosters", "weights") + SCHEDULE_CADENCE
MAX_SCHEDULE_ROSTERS = 4  # = raidcycle.MAX_ROSTERS_PER_SLOT
SCHEDULE_NAMES = {"weekly": "Weekly", "lockout": "Lockout days", "pickup": "Pickup"}
DEFAULT_SCHEDULE_NAME = "Raid nights"  # D10: members see "regulars", so the raid's own run times are no longer "Regular nights"
# ---- classes (roster redesign §2): derived from the kind, never set by hand. Recurring = weekly or lockout (opens on its
# own, has REGULARS, counts toward fairness); ad hoc = a pickup template or a one-off run (opened by an officer at a
# picked time, no regulars, first come, ADHOC_DEFAULTS).
SCHEDULE_CLASS = {"weekly": "recurring", "lockout": "recurring", "pickup": "adhoc"}
CLASSES = ("recurring", "adhoc")
CLASS_WORDS = {"recurring": "Recurring", "adhoc": "Ad hoc"}
# A one-off run ("Another day and time…", a picked time that is none of the upcoming runs) comes from this VIRTUAL schedule:
# schedule_def_of knows it (ad hoc defaults), schedules_of never lists it (listings, wizards, the config dump don't change),
# and run_key gives it the '-adhoc' suffix, so a one-off is never mistaken for a run of the raid's own run times.
ADHOC_SCHEDULE = "adhoc"
ADHOC_SCHEDULE_NAME = "Ad hoc"


def schedule_class(kind: str | None) -> str:
    """'recurring' | 'adhoc' for a schedule kind (weekly / lockout → recurring, pickup → ad hoc)."""
    return SCHEDULE_CLASS.get(kind or "weekly", "recurring")


def can_cover(people: list[set[str]], need: list[str]) -> bool:
    """Distinct people for every role seat in `need` (bipartite matching, people → the roles they can play). The one
    matching the planner (raidcycle.role_plan), another_run_hint and the regulars coverage level use."""
    match: dict[int, int] = {}  # seat → person

    def place(seat: int, seen: set[int]) -> bool:
        for i, roles in enumerate(people):
            if need[seat] in roles and i not in seen:
                seen.add(i)
                holder = next((s for s, p in match.items() if p == i), None)
                if holder is None or place(holder, seen):
                    match[seat] = i
                    return True
        return False

    return all(place(s, set()) for s in range(len(need)))


def series_key(instance: str, sid: str | None) -> str:
    """A recurring series' key: '<raid id>:<schedule id>' — what RegisteredCharacter.rosters (regulars), Applicant.schedules
    and wizard_opts.schedules store. Run keys never contain ':', so the two key spaces can't collide."""
    return f"{instance}:{sid or DEFAULT_SCHEDULE}"


def parse_series(key: str) -> tuple[str, str]:
    """'<raid id>:<schedule id>' → (raid id, schedule id)."""
    rid, sep, sid = str(key or "").partition(":")
    if not sep or not rid or not sid:
        raise RegistryError(f"{key or '?'} isn't a schedule (raid:schedule)")
    return rid, sid
INHERIT_WORDS = ("", "inherit", "raid", "default", "same", "clear", "none")  # a cadence override set to one of these goes back to the raid's
_SCHED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\s*(am|pm)?$", re.I)  # a picker may add :ss
DAY_PLURAL = {"Mon": "Mondays", "Tue": "Tuesdays", "Wed": "Wednesdays", "Thu": "Thursdays", "Fri": "Fridays", "Sat": "Saturdays", "Sun": "Sundays"}


def parse_time(value) -> str:
    """'20:00', '8:00 pm', '8:00PM' → '20:00' (the stored 24-hour form; people only ever read time_label)."""
    m = _TIME_RE.match(str(value or "").strip())
    if not m:
        raise RegistryError(f"time {value!r} should look like 20:00")
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap:
        if not 1 <= h <= 12:
            raise RegistryError(f"time {value!r}: a 12-hour clock runs 1–12")
        h = h % 12 + (12 if ap == "pm" else 0)
    return f"{h:02d}:{mi:02d}"


def parse_days(value) -> list[int]:
    """'1, 3' (or a list) → [1, 3]: days of a lockout, counted from 1 = the day it resets."""
    raw = value if isinstance(value, list) else str(value or "").replace(";", ",").replace(" and ", ",").split(",")
    out = set()
    for x in raw:
        x = str(x).strip().lower().removeprefix("day").strip()
        if not x:
            continue
        if not x.isdigit() or not 1 <= int(x) <= 28:
            raise RegistryError(f"lockout day {x!r} should be a number from 1")
        out.add(int(x))
    return sorted(out)


def schedule_value(field: str, value):
    """One schedule setting, validated and in its stored form. None = not set (a cadence field then inherits the raid's)."""
    if field in SCHEDULE_CADENCE and (value is None or str(value).strip().lower() in INHERIT_WORDS):
        return None
    if field == "weights":
        return schedule_weights(value)
    if field == "name":
        return str(value or "").strip()[:40] or None
    if field == "kind":
        if value not in SCHEDULE_KINDS:
            raise RegistryError(f"a schedule's kind is one of {', '.join(SCHEDULE_KINDS)}")
        return value
    if field == "active":
        return None if value is None else parse_bool(value)
    if field == "slots":
        return parse_slots(value)
    if field == "days":
        return parse_days(value)
    if field == "time":
        return parse_time(value) if value not in (None, "") else None
    if field == "rosters":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise RegistryError("rosters must be a whole number")
        if not 1 <= n <= MAX_SCHEDULE_ROSTERS:
            raise RegistryError(f"a run holds 1 to {MAX_SCHEDULE_ROSTERS} rosters")
        return n
    if field in RAID_HOURS_FIELDS:
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise RegistryError(f"{field} must be a number of hours")
        if num < 0:
            raise RegistryError(f"{field} can't be negative")
        return int(num) if num == int(num) else num
    if field in RAID_BOOL_FIELDS:
        return parse_bool(value)
    if field == "split_policy":
        if value not in SPLIT_POLICIES:
            raise RegistryError(f"split_policy must be one of {', '.join(SPLIT_POLICIES)}")
        return value
    raise RegistryError(f"unknown schedule setting {field} (one of {', '.join(SCHEDULE_FIELDS)})")


def schedule_weights(value) -> dict | None:
    """A schedule's own seat weights: a partial {rank, main, sat_out, signup_order, regular} map of whole numbers ≥ 0
    (a key set to 'inherit' / None / '' drops out and follows the raid's — or, ad hoc, ADHOC_WEIGHTS). Text like
    'regular 6, rank 0' works too. None = nothing of its own."""
    if value is None or (isinstance(value, str) and value.strip().lower() in INHERIT_WORDS):
        return None
    if isinstance(value, str):
        pairs = {}
        for part in value.replace(";", ",").split(","):
            bits = part.replace("=", " ").replace(":", " ").split()
            if not bits:
                continue
            if len(bits) != 2:
                raise RegistryError(f"weights look like 'regular 6, rank 0' (got {part.strip()!r})")
            pairs[bits[0]] = bits[1]
        value = pairs
    if not isinstance(value, dict):
        raise RegistryError("weights is a map of seat weights, e.g. {regular: 6, rank: 0}")
    out = {}
    for k, v in value.items():
        k = str(k).strip().lower().removeprefix("weight_")
        if k not in RAID_WEIGHT_DEFAULTS:
            raise RegistryError(f"unknown seat weight {k} (one of {', '.join(RAID_WEIGHT_DEFAULTS)})")
        if v is None or str(v).strip().lower() in INHERIT_WORDS:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise RegistryError(f"weight {k} must be a whole number")
        if n < 0:
            raise RegistryError(f"weight {k} can't be negative")
        out[k] = n
    return out or None


def norm_schedule(row: dict) -> dict:
    """A stored schedule entry, validated: id + only the settings that are set. The ordering rules that need the raid
    (cadence, lockout days, slots) are Registry._check_raid's, on the merged result."""
    if not isinstance(row, dict):
        raise RegistryError("a schedule is a mapping of settings")
    sid = str(row.get("id") or "").strip().lower()
    if not _SCHED_ID_RE.match(sid):
        raise RegistryError(f"schedule id {sid or '(empty)'!r}: a short key of letters, digits, - or _ (at most 16)")
    out: dict = {"id": sid}
    for k, v in row.items():
        if k == "id":
            continue
        nv = schedule_value(k, v)
        if nv is not None and not ((k == "active" and nv is True) or (k == "rosters" and nv == 1)):  # the defaults aren't stored
            out[k] = nv
    if sid == ADHOC_SCHEDULE:
        raise RegistryError(f"schedule id {ADHOC_SCHEDULE!r} is kept for one-off runs; pick another id")
    if sid == "test":  # raidcycle.TEST_SCHEDULE: the test runs' key space ('bd-1209-1930-test'), never a real schedule's
        raise RegistryError("schedule id 'test' is kept for test runs; pick another id")
    if sid == DEFAULT_SCHEDULE:
        if out.pop("kind", "weekly") != "weekly":
            raise RegistryError("the default schedule is the raid's own weekly run times; add another schedule for other kinds")
    else:
        out.setdefault("kind", "weekly")
    return out


def schedules_of(rd: dict) -> list[dict]:
    """The effective schedules of an effective raid definition (Registry.raid_def), every key present: `default` first
    (from the raid's slots, listed when it has slots or settings of its own), then the stored ones in order."""
    stored = [s for s in (rd.get("schedules") or []) if isinstance(s, dict) and s.get("id")]
    dflt = next((s for s in stored if s["id"] == DEFAULT_SCHEDULE), None)
    base = {"active": True, "rosters": 1, "slots": [], "days": [], "time": None}
    out = []
    if rd.get("slots") or dflt:
        out.append({**base, "name": DEFAULT_SCHEDULE_NAME, **(dflt or {}), "id": DEFAULT_SCHEDULE, "kind": "weekly", "slots": list(rd.get("slots") or [])})
    for s in stored:
        if s["id"] != DEFAULT_SCHEDULE:
            kind = s.get("kind") or "weekly"
            out.append({**base, "name": SCHEDULE_NAMES.get(kind, kind), **s, "kind": kind})
    for s in out:
        s["active"] = parse_bool(s.get("active", True))
    return out


def schedule_def_of(rd: dict, sid: str | None) -> dict:
    """The raid definition as one schedule sees it: the raid's fields with the schedule's cadence overrides on top, and
    the schedule itself under "schedule" (its overrides under ["schedule"]["own"], where each cadence value comes from
    under ["schedule"]["source"]: own | raid | adhoc). A schedule that moves the opening or the lock without its own
    nudge gets the nudge halfway again. None / "" / "default" = the raid's own cadence.
    The CLASS follows the kind (eff["class"]): a recurring schedule resolves own → the raid's (profile + guild override →
    RAID_DEFAULTS) as before; an ad hoc one (a pickup template, or ADHOC_SCHEDULE = a one-off) resolves own →
    ADHOC_DEFAULTS, and takes only the raid's identity (size, comp, lockout, duration, first opening) — never its
    recurring timing. Weights merge per key the same way (raid weights, or ADHOC_WEIGHTS, then the schedule's own)."""
    sid = sid or DEFAULT_SCHEDULE
    s = next((x for x in schedules_of(rd) if x["id"] == sid), None)
    if s is None:
        if sid == ADHOC_SCHEDULE:
            s = {"id": ADHOC_SCHEDULE, "name": ADHOC_SCHEDULE_NAME, "kind": "pickup", "active": True, "rosters": 1, "slots": [], "days": [], "time": None, "virtual": True}
        elif sid != DEFAULT_SCHEDULE:
            raise RegistryError(f"no schedule {sid} on {rd.get('name', 'this raid')}")
        else:
            s = {"id": DEFAULT_SCHEDULE, "name": DEFAULT_SCHEDULE_NAME, "kind": "weekly", "active": True, "rosters": 1, "slots": list(rd.get("slots") or []), "days": [], "time": None}
    cls = schedule_class(s.get("kind"))
    own = {k: s[k] for k in SCHEDULE_CADENCE if s.get(k) is not None}
    own_w = dict(s.get("weights") or {})
    if cls == "adhoc":
        eff = {**rd, **ADHOC_DEFAULTS, **own}
        base_w, inherited = ADHOC_WEIGHTS, "adhoc"
    else:
        eff = {**rd, **own}
        base_w, inherited = dict(rd.get("weights") or RAID_WEIGHT_DEFAULTS), "raid"
    for k, v in RAID_DEFAULTS.items():  # a hand-built rd (a wizard's draft) may lack a newer default
        eff.setdefault(k, v)
    if "nudge_hours_before" not in own and ("signup_lead_hours" in own or "lock_hours_before" in own):
        if cls == "adhoc":  # ad hoc: the officer card an hour before the lock (never past the opening)
            eff["nudge_hours_before"] = min(float(eff["lock_hours_before"]) + 1, float(eff["signup_lead_hours"]))
        else:
            eff["nudge_hours_before"] = default_nudge_hours(eff["signup_lead_hours"], eff["lock_hours_before"])
    for k in RAID_BOOL_FIELDS:
        eff[k] = parse_bool(eff[k])
    eff["weights"] = {**RAID_WEIGHT_DEFAULTS, **base_w, **own_w}
    eff["class"] = cls
    source = {k: ("own" if k in own else inherited) for k in SCHEDULE_CADENCE}
    source["weights"] = {k: ("own" if k in own_w else inherited) for k in RAID_WEIGHT_DEFAULTS}
    eff["schedule"] = {**s, "own": own, "own_weights": own_w, "class": cls, "source": source}
    return eff


def join_words(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""


def _clabel(c: dict) -> str:
    if c.get("name"):
        return f"{c['name']} {c['surname']}" if c.get("surname") else c["name"]
    return f"{c['cls']} ({c['spec']})"


def diff_member(old: dict | None, new: dict) -> list[str]:
    """Human lines describing what changed between two saved states of a member (for the change log)."""
    who = new["display_name"]
    if old is None:
        lines = [f"➕ **{who}** registered — one more who could play"]
        for c in new["characters"]:
            lines.append(f"➕ {who}: {'main' if c['is_main'] else 'alt'} {_clabel(c)} · {c['cls']} {c['spec']}" + (f"/{c['offspec']}" if c.get("offspec") else "") + f" · {c['status']}")
        return lines
    lines: list[str] = []
    if old["display_name"] != who:
        lines.append(f"{old['display_name']} is now **{who}**")
    oc = {c["created_at"]: c for c in old["characters"]}
    nc = {c["created_at"]: c for c in new["characters"]}
    for k, c in nc.items():
        o = oc.get(k)
        if o is None:
            lines.append(f"➕ {who}: {'main' if c['is_main'] else 'alt'} {_clabel(c)} · {c['cls']} {c['spec']}" + (f"/{c['offspec']}" if c.get("offspec") else "") + f" · {c['status']}")
            continue
        ch = []
        if (o.get("name"), o.get("surname")) != (c.get("name"), c.get("surname")):
            ch.append(f"named **{_clabel(c)}**" if c.get("name") else "name cleared")
        if (o["cls"], o["spec"]) != (c["cls"], c["spec"]):
            ch.append(f"{o['cls']} {o['spec']} → **{c['cls']} {c['spec']}**")
        if o.get("offspec") != c.get("offspec"):
            ch.append(f"offspec {o.get('offspec') or '—'} → {c.get('offspec') or '—'}")
        if o["is_main"] != c["is_main"]:
            ch.append("now **main**" if c["is_main"] else "now alt")
        if o["status"] != c["status"]:
            ch.append(f"{o['status']} → **{c['status']}**")
        if o["rank"] != c["rank"]:
            ch.append(f"rank {o['rank']} → **{c['rank']}**")
        added, removed = sorted(set(c["rosters"]) - set(o["rosters"])), sorted(set(o["rosters"]) - set(c["rosters"]))
        if added:
            ch.append("roster +" + ", +".join(added))
        if removed:
            ch.append("roster −" + ", −".join(removed))
        if not o.get("confirmed_by") and c.get("confirmed_by"):
            ch.append(f"confirmed by {c['confirmed_by']}")
        if sorted(o.get("flex", [])) != sorted(c.get("flex", [])):
            ch.append(("extra roles " + ", ".join(c["flex"])) if c.get("flex") else "no extra roles")
        if ch:
            lines.append(f"{who} · {_clabel(o)}: " + " · ".join(ch))
    for k, o in oc.items():
        if k not in nc:
            lines.append(f"➖ {who}: {_clabel(o)} removed")
    oa = {(a["start"], a["end"]) for a in old.get("absences", [])}
    na = {(a["start"], a["end"]) for a in new.get("absences", [])}
    for s, e in sorted(na - oa):
        lines.append(f"{who}: absent {s}" + (f" → {e}" if e != s else ""))
    for s, e in sorted(oa - na):
        lines.append(f"{who}: absence {s} cleared")
    oa_ = [(a["roster"], a.get("answer")) for a in old.get("placement_asks", [])]
    na_ = [(a["roster"], a.get("answer")) for a in new.get("placement_asks", [])]
    if oa_ != na_:
        last = new.get("placement_asks", [])[-1] if new.get("placement_asks") else None
        if last:
            lines.append(f"{who}: placement on {last['roster']} " + ({"yes": "**confirmed**", "no": "**declined**"}.get(last.get("answer"), "asked to confirm")))
    if old.get("dm_opt_out") != new.get("dm_opt_out"):
        lines.append(f"{who}: DMs {'off' if new['dm_opt_out'] else 'on'}")
    return lines


def pool_health_data(reg: "Registry", roster: dict, placed: set[int] | None = None) -> dict:
    """Readiness of the *potential* pool (every planned/active main) against a roster's size, in the same
    shape as raidcycle.health_data (the analytics cards read it). No sheet involved. `placed` = discord ids holding a
    seat on a locked live run of this raid (comp.readiness reads them from the run files); "on roster" counts them."""
    profile = reg.profile
    size = int(roster.get("size") or reg.raid_def(roster.get("instance")).get("size") or DEFAULT_RAID_SIZE)
    placed = placed or set()
    mains = [(m, m.main) for m in reg.members.values() if m.main]
    on_roster = [(m, c) for m, c in mains if m.discord_id in placed]
    alts = [(m, c) for m in reg.members.values() for c in m.active() if not c.is_main]
    bounds = reg.role_bounds(roster.get("instance"), size)

    def role_of(m: Member, c: RegisteredCharacter) -> str:
        return reg.roles_of(m)[0] or profile.spec(c.cls, c.spec).role

    counts = {r: sum(1 for m, c in mains if role_of(m, c) == r) for r in ROLES}
    need = {"tank": bounds["tank"]["min"], "healer": bounds["healer"]["min"], "melee": 0, "ranged": 0}
    roles = []
    for r in ROLES:
        have, n = counts[r], need[r]
        cover = {"offspec": [], "flex": [], "alt": []}
        if n and have < n:
            for m, c in mains:
                if role_of(m, c) == r:
                    continue
                if c.offspec and profile.spec(c.cls, c.offspec).role == r:
                    cover["offspec"].append(f"{m.display_name} ({c.offspec})")
                elif r in reg.roles_of(m)[1]:
                    cover["flex"].append(m.display_name)
                elif any(not a.is_main and profile.spec(a.cls, a.spec).role == r for a in m.active()):
                    cover["alt"].append(m.display_name)
        coverable = sum(len(v) for v in cover.values())
        level = "green" if not n or have >= n else ("amber" if have + coverable >= n else "red")
        hint = " · ".join(f"+{len(v)} {k}: {', '.join(x.split(' (')[0] for x in v[:3])}" for k, v in cover.items() if v) if (n and have < n) else ""
        if n and have < n and not coverable:
            hint = f"short {n - have} → recruit"
        roles.append({"role": r, "have": have, "need": n, "level": level, "hint": hint, "cover": cover})
    buffs = []
    seen_slots: set[str] = set()
    for b in profile.party_buffs():
        if b.slot:  # one badge per totem element: the highest-value totem stands for the slot
            best = max((x for x in profile.party_buffs() if x.slot == b.slot), key=lambda x: x.max_benefit)
            if b.slot in seen_slots or best.id != b.id or best.max_benefit < 2:
                continue
            seen_slots.add(b.slot)
            providers = [m.display_name for m, c in mains if any(x.provided_by(profile.spec(c.cls, c.spec)) for x in profile.party_buffs() if x.slot == b.slot)]
            buffs.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "name": b.short, "providers": providers})
            continue
        providers = [m.display_name for m, c in mains if b.provided_by(profile.spec(c.cls, c.spec))]
        buffs.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "name": b.short, "providers": providers})
    not_on_roster = [m.display_name for m, c in mains if m.discord_id not in placed]
    unnamed = sum(1 for m, c in mains if not c.name)
    hc_level = "green" if len(mains) >= size else ("amber" if len(mains) + len(alts) >= size else "red")
    return {"headcount": (len(mains), size, len(alts), len(on_roster)), "headcount_level": hc_level, "roles": roles, "buffs": buffs,
            "unresponsive": not_on_roster, "unnamed": unnamed, "mains": len(mains), "on_roster": len(on_roster), "alts": len(alts)}


COMP_NOTE_MAX = 200  # an officer's reason for a comp target (prose, shown on the readiness card)
COMP_ROLE_WORDS = {"tank": ("tank", "tanks"), "healer": ("healer", "healers"), "melee": ("melee", "melee"), "ranged": ("ranged", "ranged"), "dps": ("damage dealer", "damage dealers")}


def _comp_words(role: str, n: int) -> str:
    one, many = COMP_ROLE_WORDS.get(role, (role, role))
    return f"{n} {one if n == 1 else many}"


class Effects(BaseModel):
    """What removing a schedule does beyond the schedule itself (dual interfaces §3.5 RD-11) — ONE computation for the
    wizard's Review line, the web's confirm, describe_ops' consequences and MCP's preview. `regulars` = the display names
    who come off it (D30: automatically, a notice and no DM — D47); `open_runs` = the names of the runs already open from
    it (they keep their time and their sheet); `loses` = what those runs no longer get at their lock once the schedule is
    gone, in words ("the raid's weights instead of the schedule's own", "no regulars" — rc.run_weights falls back by class
    and the series' regulars come off with it), empty when nothing changes for them."""
    name: str
    label: str = ""
    regulars: list[str] = Field(default_factory=list)
    open_runs: list[str] = Field(default_factory=list)
    loses: list[str] = Field(default_factory=list)

    def lines(self) -> list[str]:
        """At most two sentences (the Confirm rule, design §5.57): who comes off it and how they hear, then what the runs
        already open keep and lose. Nothing to say → [] (removing a schedule is reviewed anyway: WARN_OPS)."""
        out = []
        n = len(self.regulars)
        if n:
            who = f"{join_words(self.regulars)} come{'s' if n == 1 else ''}" if n <= 3 else f"Its {n} regulars come"
            out.append(f"{who} off its regulars and see{'s' if n == 1 else ''} a notice on their Me page.")
        k = len(self.open_runs)
        if k:
            head = f"{self.open_runs[0]} is already open and keeps" if k == 1 else f"The {k} runs already open keep"
            fate = f", but lock{'s' if k == 1 else ''} with {join_words(self.loses)}" if self.loses else ""
            out.append(f"{head} {'its' if k == 1 else 'their'} time and sheet{fate}.")
        return out

    def sentence(self) -> str:
        return " ".join([f"The {self.name} schedule is removed."] + self.lines())


class Registry:
    # set by discord_registry.Guilds for every guild it serves: True while the process serves several, so a DM or a card
    # names its guild (design §5.32); with one guild every message reads exactly as before
    many_guilds: bool = False

    def is_owner_id(self, uid) -> bool:
        """Owner rights here: the guild's owner, or a host superuser (OIBOT_SUPERUSERS)."""
        return uid is not None and ((self.config.owner_discord_id is not None and uid == self.config.owner_discord_id) or is_superuser(uid))

    # ---- owner transfer (design §3.5 O-03, owner decision D50): one verb for /gm config owner and the Config page
    def owner_refusal(self, uid: int, *, by_id: int | None = None, bot: bool = False, membership: str = "yes", name: str | None = None) -> str | None:
        """Why `uid` can't become the owner now (None = they can): a bot, the current owner, yourself, someone outside the
        guild's Discord server — or 'unknown' (Discord couldn't say): a write that moves owner rights needs certainty."""
        who, guild = name or "They", self.config.name
        if bot:
            return "A bot can't own a guild. Pick a person."
        if self.config.owner_discord_id is not None and int(uid) == int(self.config.owner_discord_id):
            return f"{who} already own{'s' if name else ''} {guild}."
        if by_id is not None and int(uid) == int(by_id):
            return "That's you. Pick the person who takes over."
        if membership == "no":
            return f"{who} {'is' if name else 'are'} not in {guild}'s Discord server, and the owner must be a member of it."
        if membership != "yes":
            return "Discord couldn't be checked just now, so nothing changed. Try again in a minute."
        return None

    def owner_consequence(self, name: str, by_id: int | None = None) -> str:
        """THE confirm sentence of an owner transfer (the Discord decide screen and the Config page print it)."""
        host = by_id is not None and is_superuser(by_id) and by_id != self.config.owner_discord_id
        lead = [f"**{name}** becomes the owner of **{self.config.name}** on every surface."]
        lead.append("You act here as the host, so your own rights don't change." if host else
                    "You lose owner rights here and keep officer rights only if your Discord roles grant them.")
        return " ".join(lead + ["Nobody gets a DM, and a host superuser can reassign it."])

    @_config_verb("settings")
    def set_owner(self, uid: int, by: str, *, by_id: int | None = None, bot: bool = False, membership: str = "yes", name: str | None = None) -> str:
        """Hand the guild to `uid` (the caller resolved `bot` and `membership` from Discord). Refused per owner_refusal;
        one settings commit. Callers check the presser is the owner (Registry.is_owner_id: the owner or a superuser)."""
        why = self.owner_refusal(uid, by_id=by_id, bot=bot, membership=membership, name=name)
        if why:
            raise RegistryError(why)
        self.config.owner_discord_id = int(uid)
        self.save_config(f"owner → {name or uid} (by {by})")
        return f"{name or uid} owns {self.config.name} now"

    # ---- error alerts (design §3.5 OPS-01, D46): who the ops feed DMs about an error
    def error_dm_targets(self) -> list[int]:
        """The Discord ids ops.Ops.emit DMs about an error: `error_dm_ids` when set (possibly nobody), else the owner."""
        ids = self.config.error_dm_ids
        if ids is None:
            return [int(self.config.owner_discord_id)] if self.config.owner_discord_id else []
        return [int(x) for x in ids]

    @_config_verb("settings")
    def set_error_dms(self, ids, by: str) -> str:
        """Who gets the error alerts: a list of Discord ids ([] = nobody — errors stay in the ops channel) or None (the owner,
        the default). One settings commit. Errors only: info and warn lines never DM anyone (D46)."""
        if ids is not None:
            out: list[int] = []
            for x in ids:
                try:
                    uid = int(str(x).strip().removeprefix("<@").removeprefix("!").removesuffix(">"))
                except ValueError:
                    raise RegistryError(f"error alerts: {x} isn't a Discord user (pick the member)") from None
                if uid not in out:
                    out.append(uid)
            if len(out) > ERROR_DM_MAX:
                raise RegistryError(f"At most {ERROR_DM_MAX} people get the error alerts.")
            ids = out
        self.config.error_dm_ids = ids
        self.save_config(f"error alerts → {self.error_dm_words()} (by {by})")
        return f"error alerts go to {self.error_dm_words()}"

    def error_dm_words(self) -> str:
        """'the owner' · '<@1>, <@2>' · 'nobody (the ops channel only)' — mentions stay ids (each surface renders them)."""
        ids = self.config.error_dm_ids
        if ids is None:
            return "the owner"
        return ", ".join(f"<@{i}>" for i in ids) if ids else "nobody (the ops channel only)"

    # ---- companion tokens (design §5.50, out/dual_interface_design.md §3.1 L08/S-03): hashed, shown once, revocable
    def feed_tokens(self, *, include_revoked: bool = True) -> list[FeedToken]:
        """The guild's companion tokens, newest first (records only: a hash and words, never a value)."""
        rows = [(n, t) for n, t in enumerate(self.config.feed_tokens) if include_revoked or t.active]
        return [t for _, t in sorted(rows, key=lambda r: (r[1].issued_at, r[0]), reverse=True)]  # issued in the same second: the later one first

    def feed_token(self, ref: str | None) -> FeedToken | None:
        """A token record by its id, else by its label (case-insensitive; an active one first); None when unknown."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        rows = self.config.feed_tokens
        hit = next((t for t in rows if t.id == ref), None)
        if hit is not None:
            return hit
        low = ref.casefold()
        named = [t for t in rows if t.label.casefold() == low]
        return next((t for t in named if t.active), named[0] if named else None)

    def issue_feed_token(self, label: str, by: str, by_id: int | None = None, *, sent_to: str | None = None) -> tuple[str, FeedToken]:
        """Issue a companion token for this guild → (the token, its record). The token is returned EXACTLY ONCE and
        never stored: guild.yaml keeps its hash only, the commit message names the label and the id. The label is prose
        (≤ FEED_LABEL_MAX, one line) and unique among the active tokens; at most FEED_TOKEN_MAX active at once. Callers
        check the presser is the owner (Registry.is_owner_id) and write the audit line (ops.audited) — never the value."""
        from .feed import hash_token, new_token

        label = " ".join(str(label or "").split())
        if not label:
            raise RegistryError("Give the token a label — whose PC runs the companion (\"Jon's PC\").")
        if len(label) > FEED_LABEL_MAX:
            raise RegistryError(f"A label is at most {FEED_LABEL_MAX} characters.")
        with self.config_lock:
            active = [t for t in self.config.feed_tokens if t.active]
            if any(t.label.casefold() == label.casefold() for t in active):
                raise RegistryError(f"A token labelled “{label}” is already in use — revoke it first, or pick another label.")
            if len(active) >= FEED_TOKEN_MAX:
                raise RegistryError(f"{self.config.name} has {FEED_TOKEN_MAX} companion tokens — revoke one before issuing another.")
            token = new_token()
            used = {t.id for t in self.config.feed_tokens}
            tid = feed_token_id(used)
            rec = FeedToken(id=tid, hash=hash_token(token), label=label, issued_by=str(by or "?"), issued_by_id=int(by_id) if by_id is not None else None,
                            issued_at=now(), sent_to=sent_to)
            self.config.feed_tokens.append(rec)
            try:
                self.save_config(f"companion token “{label}” ({tid}) issued (by {by})")
            except Exception:
                self.config.feed_tokens = [t for t in self.config.feed_tokens if t.id != tid]
                raise
        return token, rec

    def revoke_feed_token(self, ref: str, by: str, by_id: int | None = None) -> FeedToken:
        """Revoke a companion token (by id, or its label): the feed refuses it at the companion's next frame, no restart.
        Refused when unknown or already revoked. One commit; the record stays (who issued it, when it was last seen)."""
        with self.config_lock:
            rec = self.feed_token(ref)
            if rec is None:
                raise RegistryError(f"No companion token {ref!r} — the Companions list shows the labels.")
            if not rec.active:
                raise RegistryError(f"“{rec.label}” was already revoked.")
            rec.revoked_at, rec.revoked_by = now(), str(by or "?")
            try:
                self.save_config(f"companion token “{rec.label}” ({rec.id}) revoked (by {by})")
            except Exception:
                rec.revoked_at = rec.revoked_by = None
                raise
        return rec

    def match_feed_token(self, token: str | None) -> FeedToken | None:
        """The ACTIVE token record whose hash is this token's, else None. Every stored hash is compared in constant
        time (hmac.compare_digest), all of them, so the answer's timing says nothing about which one matched."""
        from .feed import hash_token

        if not token:
            return None
        digest = hash_token(str(token))
        hit = None
        for t in list(self.config.feed_tokens):
            if hmac.compare_digest(digest.encode(), str(t.hash).encode()) and t.active and hit is None:
                hit = t
        return hit

    def feed_token_active(self, tid: str | None) -> bool:
        """Is the token `tid` still valid (issued here, not revoked)? Read at every frame the companion sends."""
        rec = next((t for t in self.config.feed_tokens if t.id == tid), None) if tid else None
        return rec is not None and rec.active

    def touch_feed_token(self, tid: str, *, save_after: float = FEED_SEEN_SAVE_S) -> bool:
        """A frame arrived with token `tid`: its last_seen moves in memory at once; guild.yaml gets it when the stored
        value is older than `save_after` seconds (a commit at most every 15 minutes per token, quietly — no change-log
        line). True when it was written."""
        with self.config_lock:
            rec = next((t for t in self.config.feed_tokens if t.id == tid), None)
            if rec is None:
                return False
            stamp = now()
            saved = getattr(self, "_feed_seen_saved", None)
            if saved is None:
                saved = self._feed_seen_saved = {}
            last = saved.get(tid) or (datetime.fromisoformat(rec.last_seen).timestamp() if rec.last_seen else 0.0)
            rec.last_seen = stamp
            if datetime.now(timezone.utc).timestamp() - last < save_after:
                return False
            saved[tid] = datetime.now(timezone.utc).timestamp()
            self.save_config(f"companion token “{rec.label}” ({rec.id}) seen", notify=False)
            return True

    # ---- detached guilds (design §3.5 S-05/S-06): the bot was removed from the server
    def set_detached(self, at: str | None, by: str) -> None:
        """Park (at = an ISO stamp) or re-attach (None) the guild — Guilds.detach / attach own the routing; this is the
        stored mark, so a restart keeps a removed server parked until the bot is seen in it again."""
        with self.config_lock:
            if self.config.detached_at == at:
                return
            self.config.detached_at = at
            self.save_config(("detached: the bot left the server" if at else "re-attached: the bot is back in the server") + f" ({by})", notify=False)

    # ---- people who left the server (design §3.3 cluster D): left_at, the officers' Remove record (D29: never self-service)
    @_member_write
    def member_left(self, discord_id: int, left: bool = True) -> Member | None:
        """Mark (or clear) that a member left the Discord server. None for an unknown person. No notice, no DM."""
        m = self.members.get(int(discord_id))
        if m is None or bool(m.left_at) == bool(left):
            return m
        m.left_at = now() if left else None
        self.save_quiet(m, f"{m.display_name} {'left' if left else 'is back in'} the server")
        return m

    @_member_write
    def remove_record(self, discord_id: int, by: str, *, seats: list[str] | None = None) -> str:
        """THE officers' Remove record: the member file is deleted (git keeps it). Only for someone who LEFT the server —
        a member still there retires their characters instead — and never while they hold a seat on a live run (`seats`:
        the caller's run titles from the run files). Members never delete themselves (owner decision D29)."""
        m = self.members.get(int(discord_id))
        if m is None:
            raise RegistryError("No record for that member.")
        if m.test:
            raise RegistryError(f"{m.display_name} is a test member — the test bench clears them.")
        if not m.left_at:
            raise RegistryError(f"{m.display_name} is still in the server — retire their characters instead (the record stays).")
        if seats:
            raise RegistryError(f"{m.display_name} is rostered on {', '.join(seats)} — release that seat first.")
        path = self.store.root / self.key / "members" / f"{m.discord_id}.json"
        if path.exists():
            path.unlink()
        self.members.pop(m.discord_id, None)
        self._snap.pop(m.discord_id, None)
        self.store.commit(f"{self.key}: record of {m.display_name} removed (left the server; by {by})")
        self._notify("member", [f"{m.display_name}'s record removed (by {by})"])
        return f"{m.display_name}'s record removed"

    def __init__(self, store: GitStore, guild_key: str, profile: GameProfile):
        self.store = store
        self.key = guild_key
        self.base_profile = profile  # the game version's defaults; self.profile carries the guild's aura overrides
        self.profile = profile
        self.members: dict[int, Member] = {}
        # change listeners: fn(kind: "member"|"config", lines: list[str]); called synchronously after each commit
        self.listeners: list = []
        self._snap: dict[int, dict] = {}  # last saved state per member, for diff lines
        self.role_names: dict[int, str] = {}  # Discord role id -> name, cached by resolve_officer_roles (display + name lookups off the bot thread)
        # every mutate-validate-save config verb and save_config hold this: the web mutates in worker threads while
        # Discord mutates on the loop, and store.lock guards only the file write (design §2.4)
        self.config_lock = threading.RLock()
        self.config = self.load_config()
        self.refresh_profile()
        self.reload()

    # ---- officer roles (ids are the truth; see GuildConfig.officer_by_roles)
    def cache_roles(self, guild) -> None:
        """Remember the guild's role names by id (any object with `.roles` of `.id`/`.name`, i.e. a discord.Guild or a test double)."""
        roles = getattr(guild, "roles", None) or []
        self.role_names = {int(r.id): str(r.name) for r in roles}

    def resolve_officer_roles(self, guild) -> list[str]:
        """Migrate legacy officer role names to ids against the guild's roles (one commit), refresh the name cache,
        and return the configured officer role names. Called by the bot on ready and whenever roles are edited.
        Names that match no role stay in `officer_roles` (nothing is dropped silently); they no longer grant anything
        once at least one id is configured."""
        self.cache_roles(guild)
        cfg = self.config
        if cfg.officer_roles:
            by_name = {name: rid for rid, name in self.role_names.items()}
            resolved = {name: by_name[name] for name in cfg.officer_roles if name in by_name}
            if resolved:
                ids = list(cfg.officer_role_ids)
                for name, rid in resolved.items():
                    if rid not in ids:
                        ids.append(rid)
                cfg.officer_role_ids = ids
                cfg.officer_roles = [n for n in cfg.officer_roles if n not in resolved]
                self.save_config("officer roles: " + ", ".join(f"{n} → role id {resolved[n]}" for n in resolved) + (f" (unresolved: {cfg.officer_roles})" if cfg.officer_roles else ""))
            if cfg.officer_roles:
                log.warning("%s: officer role names not found in the guild: %s", self.key, cfg.officer_roles)
        return self.officer_role_names()

    def officer_role_names(self) -> list[str]:
        """Officer roles for display: names from the cache (a stale id shows as `role:<id>`), then any unresolved legacy names."""
        names = [self.role_names.get(rid, f"role:{rid}") for rid in self.config.officer_role_ids]
        return names + [n for n in self.config.officer_roles if n not in names]

    def role_id_for(self, value) -> int | None:
        """A role reference — id, `<@&id>` mention, or a name known to the cache — as an id; None when unknown."""
        s = str(value or "").strip()
        if not s:
            return None
        digits = s[3:-1] if s.startswith("<@&") and s.endswith(">") else s
        if digits.isdigit():
            return int(digits)
        low = s.lstrip("@").lower()
        return next((rid for rid, name in self.role_names.items() if name.lower() == low), None)

    @_config_verb("settings")
    def set_officer_role(self, value, add: bool, by: str) -> str:
        """Add or remove one officer role by id, mention, or name. A name the cache can't resolve (no bot yet) is parked in
        the legacy list, granting nothing until the bot resolves it on its next ready/role event. Returns the one-line result."""
        cfg = self.config
        rid = self.role_id_for(value)
        name = str(value or "").strip().lstrip("@")
        if rid is not None:
            ids = [i for i in cfg.officer_role_ids if i != rid]
            if add:
                ids.append(rid)
            cfg.officer_role_ids = ids
            cfg.officer_roles = [n for n in cfg.officer_roles if n != name and n != self.role_names.get(rid)]
            shown = self.role_names.get(rid, f"role:{rid}")
        elif name:
            if add:
                if name not in cfg.officer_roles:
                    cfg.officer_roles.append(name)
            else:
                cfg.officer_roles = [n for n in cfg.officer_roles if n != name]
            shown = f"{name} (by name; resolved to a role id when the bot next sees the server)"
        else:
            raise RegistryError("officer role: give a role mention, id or name")
        self.save_config(f"officer roles {'+' if add else '−'} {shown} (by {by})")
        return f"officer roles = {', '.join(self.officer_role_names()) or '(none; Manage Server only)'}"

    def refresh_profile(self) -> None:
        self.profile = self.base_profile.with_overrides(self.config.buffs, self.config.families)

    # ---- guild time
    @property
    def tz(self):
        from zoneinfo import ZoneInfo

        return ZoneInfo(self.config.timezone)

    def now_local(self) -> datetime:
        """Aware 'now' in the guild's timezone: use it for anything shown to people or compared with a schedule."""
        return datetime.now(self.tz)

    def local(self, value, fmt: str = "%a %d %b %H:%M") -> str:
        """Render an aware datetime or ISO string (UTC audit stamps, raid starts, windows) in guild time."""
        t = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
        if t.tzinfo is None:
            t = t.replace(tzinfo=self.tz)
        return t.astimezone(self.tz).strftime(fmt)

    # ---- persistence
    def slot_label(self, slot: str) -> str:
        """A stored weekly slot as a person reads it: 'Tue 19:30' -> 'Tue 7:30 PM'. The stored form stays 24-hour."""
        try:
            day, hm = str(slot).split()
            h, mi = (int(x) for x in hm.split(":"))
        except ValueError:
            return str(slot)
        return f"{day} {h % 12 or 12}:{mi:02d} {'AM' if h < 12 else 'PM'}"

    def time_label(self, hm: str | None) -> str:
        """A stored time of day as a person reads it: '20:00' -> '8:00 PM'."""
        return self.slot_label(f"Mon {hm}").removeprefix("Mon ") if hm else "no time set"

    def schedule_label(self, instance: str | None, s: dict) -> str:
        """A schedule in plain words: 'Tuesdays and Thursdays 7:30 PM · 2 rosters', 'Day 1 of each 3-day lockout, 8:00 PM',
        'Pickup template'. Never a 24-hour time."""
        kind, n = s.get("kind") or "weekly", int(s.get("rosters") or 1)
        if kind == "pickup":
            text = "Pickup template: opened by an officer"
        elif kind == "lockout":
            days = list(s.get("days") or [])
            lockout = int(self.raid_def(instance).get("lockout_days") or 7)
            text = (f"Day{'s' if len(days) > 1 else ''} {join_words([str(d) for d in days])} of each {lockout}-day lockout, {self.time_label(s.get('time'))}"
                    if days else "No lockout days yet")
        else:
            order = {d: i for i, d in enumerate(DAY_PLURAL)}
            by_time: dict[str, list[str]] = {}
            for slot in sorted(s.get("slots") or [], key=lambda x: (order.get(x.split()[0][:3].title(), 9), x.split()[-1])):
                day, hm = slot.split()
                by_time.setdefault(hm, []).append(DAY_PLURAL.get(day[:3].title(), day))
            text = ", ".join(f"{join_words(days)} {self.time_label(hm)}" for hm, days in sorted(by_time.items(), key=lambda kv: order.get(kv[1][0][:3], 9))) or "No run times yet"
        return text + (f" · {n} rosters" if n > 1 else "") + ("" if s.get("active", True) else " · paused")

    def day_label(self, iso_day: str, with_year: bool = False) -> str:
        """'2026-12-24' -> 'Thu 24 Dec' (a date a person reads; ISO stays for the data repo and the API)."""
        from datetime import date as _d

        try:
            d = _d.fromisoformat(str(iso_day))
        except ValueError:
            return str(iso_day)
        from .constants import fmt12

        return fmt12(d, "%a %d %b %Y" if with_year else "%a %d %b")  # same day form as CLOCK12 ('Wed 9 Dec')

    def span_label(self, start: str, end: str | None) -> str:
        return self.day_label(start) + (f" → {self.day_label(end)}" if end and end != start else "")

    def local12(self, value, fmt: str = CLOCK12) -> str:
        """Guild-local time the way every surface a PERSON reads shows it: 12-hour, no leading zero. ISO and 24-hour
        stay for machines only (datetime-local inputs, comparisons, the data repo). See CLAUDE.md: no military time."""
        from .constants import fmt12

        t = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
        if t.tzinfo is None:
            t = t.replace(tzinfo=self.tz)
        return fmt12(t.astimezone(self.tz), fmt)  # THE 12-hour formatter (constants.fmt12) in the guild's zone

    def _live_runs(self, rs=None) -> list:
        """The guild's live runs (open or locked) from `rs` (a raidcycle.RaidStore), else read from the store."""
        if rs is None:
            from .raidcycle import RaidStore

            rs = RaidStore(self.store, self.key)
        return list(rs.live())

    TZ_MODES = ("", "keep", "move")  # a timezone change with live runs: (refuse a locked one) · keep them at their moment · move the open ones

    def _tz_zone(self, zone: str):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        zone = str(zone or "").strip()
        try:
            return zone, ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise RegistryError(f"{zone or '(empty)'} isn't a timezone; pick one from the list") from None

    def timezone_runs(self, zone: str, rs=None, now_: datetime | None = None) -> list[dict]:
        """The live runs whose clock reading a move to `zone` changes — one computation for timezone_change_effects (the
        words), the refusal of a locked one and rc.move_runs_with_clock. Each row: {ev, name, old (the start on today's
        clock), new (the same moment on `zone`'s clock), moved (the same WALL CLOCK on `zone`: where "move with the
        clock" puts it, DST-safe), locked, test, movable (an open real run whose lock would still be ahead once
        moved)}. Empty when the zone is the current one. A zone that doesn't exist raises RegistryError."""
        zone, z = self._tz_zone(zone)
        if zone == self.config.timezone:
            return []
        now_ = now_ or datetime.now(timezone.utc)
        out = []
        for ev in self._live_runs(rs):
            try:
                start = datetime.fromisoformat(ev.starts_at)
            except ValueError:
                continue
            old, new = start.astimezone(self.tz), start.astimezone(z)
            if old.replace(tzinfo=None) == new.replace(tzinfo=None):
                continue  # the same reading on both clocks (same offset at that moment)
            naive = old.replace(tzinfo=None)
            a, b = naive.replace(tzinfo=z, fold=0), naive.replace(tzinfo=z, fold=1)
            moved = (naive + timedelta(hours=1)).replace(tzinfo=z) if a.utcoffset() != b.utcoffset() and a.timestamp() > b.timestamp() else a  # the gap: past it
            t = self.config.roster(ev.team) or {}
            test = bool(t.get("test") or getattr(ev, "test", False))
            hard = float(t.get("cutoff_hard_hours") or 0)
            name = self.raid_def(ev.instance).get("name", ev.instance) if ev.instance in self.profile.raids else (ev.instance or ev.key)
            out.append({"ev": ev, "name": name, "old": old, "new": new, "moved": moved, "locked": ev.state == "locked", "test": test,
                        "movable": ev.state == "open" and not test and moved - timedelta(hours=hard) > now_})
        return out

    def timezone_change_effects(self, zone: str, rs=None, mode: str = "") -> list[str]:
        """What moving the guild to `zone` does to the runs already open or locked (dual interfaces §3.5 "Settings edit
        mode"), from code — a single run in two sentences with both clock readings, several in one sentence per fate (the
        Confirm rule, design §5.57) — the TimezoneWizard's confirm, the Config
        page's review and MCP's preview. A run's key and sheet were made from the old wall clock, so by default (`mode`
        "" or "keep") it stays at its real moment and its reading changes; `mode` "move" moves every OPEN real run with
        the clock (the same wall-clock time on the new zone, its signups kept — rc.move_runs_with_clock). A LOCKED run
        never moves (its roster confirmed that moment), and with mode "" the change is refused while one is affected
        (timezone_refusal). Empty when the zone is the current one or nothing is live; a bad zone raises RegistryError."""
        from .constants import fmt12

        zone, _z = self._tz_zone(zone)
        mode = str(mode or "").strip().lower()
        out = []
        rows = self.timezone_runs(zone, rs)
        if len(rows) == 1:  # one run: its own two sentences, with both clock readings
            r = rows[0]
            was, now_ = f"{r['name']} {fmt12(r['old'])}", f"That reads {fmt12(r['new'])} in {zone}."
            if r["locked"]:
                return [f"{was} is locked, so it stays at its real moment. {now_}"]
            if r["test"]:
                return [f"{was} is a test run, so it stays at its real moment. {now_}"]
            if mode == "move" and r["movable"]:
                return [f"{was} moves with the clock and keeps its signups. It still starts at {fmt12(r['moved'])} on the new clock."]
            if mode == "move":
                return [f"{was} can't move with the clock, because its lock would already be past. {now_}"]
            return [f"{was} stays at its real moment. {now_}" + ("" if mode == "keep" else " Move open runs with the clock to keep its time.")]
        if not rows:
            return out
        # several runs: one sentence per fate, at most three (the Confirm rule, design §5.57)

        def runs(n: int, what: str) -> str:
            return f"the {n} {what} runs" if n > 1 else f"the one {what} run"

        fixed = [r for r in rows if r["locked"] or r["test"]]
        locked = [r for r in fixed if r["locked"]]
        tests = [r for r in fixed if not r["locked"]]
        opened = [r for r in rows if r not in fixed]
        if fixed:
            parts = ([runs(len(locked), "locked")] if locked else []) + ([runs(len(tests), "test")] if tests else [])
            head = " and ".join(parts)
            out.append(head[:1].upper() + head[1:] + f" stay{'s' if len(fixed) == 1 else ''} at {'its' if len(fixed) == 1 else 'their'} real moment.")
        if opened and mode == "move":
            moved = [r for r in opened if r["movable"]]
            stay = [r for r in opened if not r["movable"]]
            if moved:
                h = runs(len(moved), "open")
                out.append(h[:1].upper() + h[1:] + f" move{'s' if len(moved) == 1 else ''} with the clock and keep{'s' if len(moved) == 1 else ''} "
                           f"{'its' if len(moved) == 1 else 'their'} time and signups.")
            if stay:
                h = runs(len(stay), "open")
                out.append(h[:1].upper() + h[1:] + f" whose lock would pass stay{'s' if len(stay) == 1 else ''} at {'its' if len(stay) == 1 else 'their'} real moment.")
        elif opened:
            h = runs(len(opened), "open")
            out.append(h[:1].upper() + h[1:] + f" stay{'s' if len(opened) == 1 else ''} at {'its' if len(opened) == 1 else 'their'} real moment, "
                       f"so {'its' if len(opened) == 1 else 'their'} clock time changes.")
            if mode != "keep":
                out.append("Move open runs with the clock to keep their times.")
        return out

    def timezone_refusal(self, zone: str, rs=None, mode: str = "") -> str | None:
        """Why a timezone change is refused as asked, or None: a LOCKED run whose clock reading would change, with no
        `mode` saying what to do with the live runs ("keep" / "move") — its roster was confirmed for that moment, so
        the owner must have seen it. Every surface sends the mode its confirm showed; plain text says "keep"."""
        if str(mode or "").strip().lower() not in self.TZ_MODES:
            raise RegistryError("what to do with the runs already open: keep (they stay at their real moment) or move (open runs move with the clock)")
        if str(mode or "").strip():
            return None
        locked = [f"{r['name']} {self.local12(r['old'])}" for r in self.timezone_runs(zone, rs) if r["locked"]]
        if not locked:
            return None
        head = f"{locked[0]} is locked" if len(locked) == 1 else f"{len(locked)} runs are locked"
        return (f"{head}, and {zone} reads {'its' if len(locked) == 1 else 'their'} time differently. "
                "Say keep to change it anyway, and locked runs stay at their real moment. "
                "Say move to also move the open runs with the clock.")

    def load_config(self) -> GuildConfig:
        p = self.store.root / self.key / "guild.yaml"
        return GuildConfig(**yaml.safe_load(p.read_text()))

    def save_config(self, message: str, notify: bool = True) -> None:
        with self.config_lock:
            self.refresh_profile()
            if getattr(self, "_shadow", False):  # a preview copy (Registry.shadow): validated, never written
                return
            p = Path(self.key) / "guild.yaml"
            self.store.write_text(p, "# oibot_GM guild config — edit via /gm config or by PR.\n" + yaml.safe_dump(self.config.model_dump(), sort_keys=False))
            self.store.commit(f"{self.key}: {message}")
        if notify:
            self._notify("config", [message])

    # ---- section revs (design §2.4): computed on demand from the stored shape, never a stored field bumped by hand
    REV_SECTIONS = ("settings", "buffs", "recruitment", "plain", "loot")  # plus raids:<instance id>; loot = the scoring weights (guild.yaml loot:)
    # what no rev covers: the scheduler's own bookkeeping (run rosters, card and sheet message ids) — an owner's edit during a
    # raid week must never be refused because a card was re-posted
    REV_SKIP = ("rosters", "feed_tokens")  # feed_tokens: a companion's last_seen must never 409 an owner's settings edit

    def _section_blob(self, section: str):
        cfg = self.config.model_dump()
        own = {"raids", "buffs", "families", "recruitment", "invite_url", "member_role_ids", "plain", "loot"}
        if section.startswith("raids:"):
            rid = section[6:]
            if rid not in self.profile.raids and self.config.roster(rid) is not None:
                # a RUN key (set_comp_targets on one run): its own comp targets live on the run's entry in `rosters`, which
                # REV_SKIP leaves out of every other section — so the rev covers exactly what a run-level target edits
                return {"comp_targets": (self.config.roster(rid) or {}).get("comp_targets") or {}}
            return cfg.get("raids", {}).get(rid) or {}
        if section == "buffs":
            return {"buffs": cfg.get("buffs") or {}, "families": cfg.get("families") or {}}
        if section == "recruitment":
            return {"recruitment": cfg.get("recruitment"), "invite_url": cfg.get("invite_url"), "member_role_ids": cfg.get("member_role_ids")}
        if section == "plain":
            return cfg.get("plain")
        if section == "loot":
            return cfg.get("loot") or {}
        if section == "settings":
            return {k: v for k, v in cfg.items() if k not in own and k not in self.REV_SKIP and "_message_id" not in k}
        raise RegistryError(f"unknown config section {section} (one of {', '.join(self.REV_SECTIONS)}, raids:<id>)")

    def config_rev(self, section: str) -> str:
        """A fingerprint of one config section as stored: `settings` (channels, timezone, roles, owner, about…),
        `raids:<id>`, `buffs` (auras and families), `recruitment`, `plain`, `loot` (the scoring weights). A write carrying an older one is stale."""
        blob = yaml.safe_dump(self._section_blob(section), sort_keys=True, default_flow_style=False)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def check_rev(self, section: str, rev: str | None) -> None:
        """Raise ConfigChanged when `rev` (what the author saw) isn't the section's rev now; None checks nothing."""
        if rev and rev != self.config_rev(section):
            raise ConfigChanged(section)

    def characters_rev(self, m: Member) -> str:
        """A fingerprint of a member's character list (what a table save edits) — not `updated_at`, which every DM
        toggle and placement ask bumps."""
        rows = [c.model_dump() for c in m.characters]
        return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def preview(self, fn):
        """Run `fn(shadow)` on a throw-away copy of this registry (`shadow`) and return its result: the config as it would
        be after a change, validated by the same verbs, with the live config never touched (design §2.4)."""
        return fn(self.shadow())

    def shadow(self) -> "Registry":
        """A throw-away copy for previews: its own deep-copied config (members shared, read-only by convention), whose
        save_config validates and refreshes the profile but never writes, commits or notifies. Apply a change to it
        with the normal verbs (set_raid_overrides, set_schedule, config.timezone = ...) and read the result."""
        import copy

        sh = copy.copy(self)
        sh.config = self.config.model_copy(deep=True)
        sh.listeners = []
        sh.config_lock = threading.RLock()  # its own: a preview never waits on, nor holds, the live lock
        sh._shadow = True
        sh.refresh_profile()
        return sh

    def _notify(self, kind: str, lines: list[str]) -> None:
        stamp = now()
        for line in lines or []:
            RECENT_CHANGES.append((self.key, stamp, kind, str(line)))
        for fn in list(self.listeners) + list(SAVE_LISTENERS):
            try:
                fn(self, kind, lines)
            except Exception:  # noqa: BLE001
                log.exception("registry listener failed (%s)", kind)

    def reload(self) -> None:
        d = self.store.root / self.key / "members"
        self.members = {}
        self._snap = {}
        if d.exists():
            for f in d.glob("*.json"):
                raw = json.loads(f.read_text())
                m = Member.model_validate(raw)
                # one-time migrations off retired member fields (the rewrite drops the old keys)
                legacy_flex = (raw.get("role_prefs") or {}).get("flex") or []  # flex used to live on the member; it belongs to the character
                legacy_teams = raw.get("teams") or []  # pre-roster files kept membership on the member
                if m.main and (legacy_flex or legacy_teams):
                    for r in legacy_flex:
                        if r not in m.main.flex:
                            m.main.flex.append(r)
                    for k in legacy_teams:  # regular-series keys only: a bare team key ("main") is a phantom (D11)
                        if ":" in k and k not in m.main.rosters:
                            m.main.rosters.append(k)
                    self.store.write_text(Path(self.key) / "members" / f"{m.discord_id}.json", m.model_dump_json(indent=1))
                self.members[m.discord_id] = m
                self._snap[m.discord_id] = m.model_dump()
        self.applicants: dict[int, Applicant] = {}
        ad = self.store.root / self.key / "applicants"
        if ad.exists():
            for f in ad.glob("*.json"):
                a = Applicant.model_validate_json(f.read_text())
                self.applicants[a.discord_id] = a

    def save_applicant(self, a: Applicant, message: str) -> None:
        self.store.write_text(Path(self.key) / "applicants" / f"{a.discord_id}.json", a.model_dump_json(indent=1))
        self.applicants[a.discord_id] = a
        self.store.commit(f"{self.key}: {message}")

    # ---- applicants
    def apply(self, discord_id: int, display_name: str, name: str, cls: str, spec: str, offspec: str | None, logs_url: str | None, availability: str | None, about: str | None,
              *, surname: str | None = None, schedules: list[str] | None = None, answers: list[dict] | None = None, source: str = "discord") -> Applicant:
        if not self.config.recruitment.open:
            raise RegistryError(f"{self.config.name} isn't taking applications right now.")
        if discord_id in self.members and self.members[discord_id].active():
            raise RegistryError("You're already a member; use /register or /me char → Add an alt for more characters.")
        name = self.normalise_name(name)
        surname = self.normalise_name(surname) if surname else None
        if cls not in self.profile.classes:
            raise RegistryError(f"Unknown class {cls}.")
        spec = self.validate_spec(cls, spec)
        offspec = self.validate_spec(cls, offspec) if offspec else None
        full = f"{name} {surname}" if surname else name
        if self.find(full):
            raise RegistryError(f"{full} is already registered to a member.")
        answers = self.check_answers(answers or [])
        known = {f"{rid}:{sc['id']}" for rid in self.profile.raids for sc in self.schedules(rid)}
        schedules = [x for x in (schedules or []) if x in known]
        clash = next((x for x in self.applicants.values() if x.status == "open" and x.name.lower() == name.lower() and x.discord_id != discord_id), None)
        if clash:
            raise RegistryError(f"There is already an open application for {name}. If that character is yours, ask an officer.")
        existing = self.applicants.get(discord_id)
        if existing and existing.status == "open":
            raise RegistryError(f"You already have an open application for {existing.name}. An officer will get to it.")
        history = list(existing.history) + [prior_row(existing)] if existing else []  # one file per person: the earlier verdicts ride along
        a = Applicant(discord_id=discord_id, display_name=display_name, name=name, cls=cls, spec=spec, offspec=offspec, logs_url=(logs_url or "").strip() or None,
                      availability=(availability or "").strip() or None, about=(about or "").strip() or None, surname=surname, schedules=schedules, answers=answers, source=source,
                      history=history)
        self.save_applicant(a, f"application from {display_name}: {a.label} ({cls} {spec})")
        return a

    def check_answers(self, answers: list[dict]) -> list[dict]:
        """The answers as asked: every required question answered, each within ANSWER_MAX. Keyed by label, in the
        guild's question order; an answer to a question that no longer exists is dropped."""
        given = {str(x.get("label") or ""): str(x.get("answer") or "").strip() for x in answers}
        out = []
        for q in self.config.recruitment.questions:
            ans = given.get(q.label, "")
            if q.required and not ans:
                raise RegistryError(f"Please answer “{q.label}”.")
            if len(ans) > ANSWER_MAX:
                raise RegistryError(f"“{q.label}” is limited to {ANSWER_MAX} characters.")
            if ans:
                out.append({"label": q.label, "answer": ans})
        return out

    # ---- recruitment settings (owner): open/closed, the note, the questions, the invite
    @_config_verb("recruitment")
    def set_recruitment(self, by: str, *, open: bool | None = None, note: str | None = None, questions: list[dict] | None = None,
                        invite_url: str | None = None, member_role_ids: list[int] | None = None) -> str:
        """One validated write for any mix of the recruitment settings (the Config page, /gm config recruitment, plain text,
        MCP). `note=""` / `invite_url=""` clear them. Nothing is saved when any part is refused."""
        r = self.config.recruitment.model_copy(deep=True)
        said = []
        if open is not None:
            r.open = bool(open)
            said.append("applications " + ("open" if r.open else "closed"))
        if note is not None:
            note = note.strip()
            if len(note) > 200:
                raise RegistryError("The recruitment note is limited to 200 characters.")
            r.note = note or None
            said.append("note " + ("cleared" if not note else "set"))
        if questions is not None:
            r.questions = self._check_questions(questions)
            said.append(f"{len(r.questions)} question(s)")
        url = self.config.invite_url
        if invite_url is not None:
            url = invite_url.strip() or None
            if url and not re.fullmatch(r"https://(discord\.gg|discord\.com/invite)/[A-Za-z0-9-]{2,32}", url):
                raise RegistryError("An invite link looks like https://discord.gg/abc123.")
            said.append("invite " + ("cleared" if not url else "set"))
        roles = self.config.member_role_ids if member_role_ids is None else [int(x) for x in member_role_ids]
        if member_role_ids is not None:
            said.append(f"{len(roles)} member role(s)")
        self.config.recruitment, self.config.invite_url, self.config.member_role_ids = r, url, roles
        line = "recruitment: " + ", ".join(said) if said else "recruitment: no change"
        self.save_config(f"{line} (by {by})")
        return line

    def _check_questions(self, questions: list[dict]) -> list[Question]:
        if len(questions) > QUESTIONS_MAX:
            raise RegistryError(f"At most {QUESTIONS_MAX} questions (three pages of five in Discord).")
        out, seen = [], set()
        for i, q in enumerate(questions):
            label = str(q.get("label") or "").strip()
            hint = str(q.get("hint") or "").strip() or None
            if not label:
                raise RegistryError(f"Question {i + 1} has no text.")
            if len(label) > 45:
                raise RegistryError(f"“{label[:45]}…” is over Discord's 45 characters; move the rest into the hint.")
            if hint and len(hint) > 100:
                raise RegistryError(f"The hint for “{label}” is over Discord's 100 characters.")
            if label.lower() in seen:
                raise RegistryError(f"“{label}” is asked twice.")
            seen.add(label.lower())
            qid = str(q.get("id") or "").strip() or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or f"q{i + 1}"
            out.append(Question(id=qid, label=label, hint=hint, required=bool(q.get("required"))))
        return out

    def must_apply(self, discord_id: int, *, officer: bool, role_ids=()) -> bool:
        """A newcomer applies instead of registering: nothing registered yet (no character, not even a planned one), not
        an officer, holding none of the configured member roles, and no accepted application on file."""
        if officer or set(int(r) for r in role_ids) & set(self.config.member_role_ids):
            return False
        m = self.members.get(discord_id)
        if m and any(c.status in ("active", "planned") for c in m.characters):
            return False
        a = self.applicants.get(discord_id)
        return not (a and a.status == "accepted")

    def open_applicants(self) -> list[Applicant]:
        return sorted([a for a in self.applicants.values() if a.status == "open"], key=lambda a: a.created_at)

    def decided_refusal(self, a: Applicant | None) -> str:
        """'Already declined by Jonny (Tue 12 Mar 8:02 PM).' — a second decision names who made the first (every surface)."""
        if not a:
            return "No open application for that member."
        when = f" on {self.local12(a.decided_at)}" if a.decided_at else ""
        return f"{a.decided_by} already {a.status} it{when}." if a.decided_by else f"That application is already {a.status}."

    def application_state(self, discord_id: int) -> dict | None:
        """A person's OWN application as they read it (GET /api/me.application, the Me page's card, /me view, and the
        registration paths, which check it BEFORE whether applications are open — a pending applicant is never told
        "isn't taking applications"): {state open | accepted | declined | withdrawn, label, when, note, text}. None =
        never applied. The applicant reads the decision and its date, never the officer who made it (officers see that
        on the Applications page)."""
        a = self.applicants.get(int(discord_id))
        if a is None:
            return None
        sent = self.local12(a.created_at)
        when = self.local12(a.decided_at) if a.decided_at else None
        if a.status == "open":
            text = f"Your application for {a.label} is waiting for an officer (sent {sent}). You'll get a DM with the decision."
        elif a.status == "accepted":
            text = f"Your application for {a.label} was accepted" + (f" ({when})." if when else ".")
        elif a.status == "declined":
            text = (f"Your application for {a.label} wasn't accepted this time" + (f" ({when})" if when else "") + "."
                    + (f" Note: {a.decision_note}" if a.decision_note else "") + " You can apply again" + (" later." if not self.config.recruitment.open else "."))
        else:
            text = f"Your application for {a.label} was withdrawn."
        return {"state": a.status, "label": a.label, "sent": sent, "when": when, "note": a.decision_note if a.status != "open" else None, "text": text}

    def prior_decision_line(self, a: Applicant) -> str | None:
        """'Applied before: declined 12 Mar by Jonny — full on healers' (the newest earlier verdict; '(+1 earlier)' when
        there are more), or None for a first application. The review card, /roster applicants and the Applications page."""
        rows = [r for r in a.history if r.get("status") in ("accepted", "declined", "withdrawn")]
        if not rows:
            return None
        r = rows[-1]
        when = self.local(r["at"], "%d %b").lstrip("0") if r.get("at") else ""
        line = f"Applied before: {r['status']}" + (f" {when}" if when else "") + (f" by {r['by']}" if r.get("by") else "")
        if r.get("note"):
            line += f" — {r['note']}"
        if len(rows) > 1:
            line += f" (+{len(rows) - 1} earlier)"
        return line

    @_member_write
    def decide(self, discord_id: int, accept: bool, by: str, note: str | None = None) -> tuple[Applicant, Optional[RegisteredCharacter]]:
        """Accept (the main registered as a confirmed trial) or decline an open application. The character is added
        FIRST: a refusal there (a name taken since) leaves the application open on disk and in memory — never accepted
        in memory and open on disk. The note is capped here (DECISION_NOTE_MAX); a second decision names the first."""
        a = self.applicants.get(discord_id)
        if not a or a.status != "open":
            raise RegistryError(self.decided_refusal(a))
        note = (note or "").strip() or None
        if note and len(note) > DECISION_NOTE_MAX:
            raise RegistryError(f"The note is limited to {DECISION_NOTE_MAX} characters (it goes to the applicant).")
        char = None
        if accept:
            m, char = self.add_character(a.discord_id, a.display_name, a.name, a.cls, a.spec, a.offspec, True, surname=a.surname)
            char.confirmed_by, char.confirmed_at = by, now()
            self.save(m, f"{by} accepted {a.display_name} as {a.name}")
        a.status = "accepted" if accept else "declined"
        a.decided_by, a.decided_at, a.decision_note = by, now(), note
        self.save_applicant(a, f"{by} {a.status} application from {a.display_name} ({a.name})")
        # the decision's copy on the Me page (D26; the decision DM itself is one they wait on, the bot's to send) — the
        # decision and its date, never the officer's name (application_state)
        self.notify(a.discord_id, "application", (f"Your application for {a.label} was accepted — welcome." if accept
                                                  else f"Your application for {a.label} wasn't accepted this time.")
                    + (f" Note: {a.decision_note}" if a.decision_note else ""))
        return a, char

    def save(self, m: Member, message: str) -> None:
        m.updated_at = now()
        self.store.write_text(Path(self.key) / "members" / f"{m.discord_id}.json", m.model_dump_json(indent=1))
        self.members[m.discord_id] = m
        self.store.commit(f"{self.key}: {message}")
        new = m.model_dump()
        lines = diff_member(self._snap.get(m.discord_id), new) or [message]
        self._snap[m.discord_id] = new
        self._notify("member", lines)

    # ---- lookups
    def member(self, discord_id: int, display_name: str | None = None, create: bool = False) -> Member:
        """The member by id. `display_name` names a NEW record only (`create`); an existing member's name is never changed
        here — a rename is its own explicit step (`rename`), so a verb can't relabel someone as a side effect while runs
        and asks still carry the old name (design §2.2)."""
        m = self.members.get(discord_id)
        if m is None:
            if not create:
                raise RegistryError("You have no registered characters yet. Use /register first.")
            m = Member(discord_id=discord_id, display_name=display_name or str(discord_id))
        return m

    @_member_write
    def rename(self, discord_id: int, display_name: str, by: str) -> Member:
        """The one way a member's display name changes (a nick change an officer or the person records). Seats, callouts
        and asks are keyed by id, so nothing else moves; the change is its own commit."""
        m = self.member(discord_id)
        name = str(display_name or "").strip()
        if not name:
            raise RegistryError("A display name can't be empty.")
        if name == m.display_name:
            return m
        old = m.display_name
        m.display_name = name
        self.save(m, f"{old} is now {name} (by {by})")
        return m

    def find(self, name: str) -> tuple[Member, RegisteredCharacter] | None:
        """By character name; planned (unnamed) characters match by their label, e.g. 'Shaman (Enhancement)'."""
        n = name.strip().lower()
        named = [(m, c) for m in self.members.values() for c in m.characters if c.name]
        exact = next(((m, c) for m, c in named if c.label.lower() == n), None)  # a full name is unique
        if exact:
            return exact
        first = [(m, c) for m, c in named if c.name.lower() == n]
        if len({(m.discord_id, c.label) for m, c in first}) > 1:  # "Jon" when there's a Jon Smith and a Jon Jones
            raise RegistryError(f"More than one character is called {name.strip()}: " + ", ".join(f"{c.label} ({m.display_name})" for m, c in first) + ". Use the full name.")
        if first:
            return first[0]
        for m in self.members.values():
            for c in m.characters:
                if not c.name and c.status == "planned" and c.label.lower() == n:
                    return m, c
        return None

    def find_unique(self, name: str) -> tuple[Member, RegisteredCharacter] | None:
        """`find` for a WRITE that names a character without its member (plain text's rank / confirm, the site's bare
        confirm): a planned label ('Priest (Holy)') is shared by everyone who plans that spec, so when more than one
        member plans it the write is refused with each named instead of landing on whoever `find` meets first."""
        hit = self.find(name)
        if hit is None or hit[1].name:
            return hit
        n = name.strip().lower()
        owners = [m for m in self.members.values() if any(not c.name and c.status == "planned" and c.label.lower() == n for c in m.characters)]
        if len(owners) > 1:
            raise RegistryError(f"{len(owners)} members plan {hit[1].label}: " + ", ".join(sorted(m.display_name for m in owners))
                                + ". Name the member too.")
        return hit

    # ---- pre-launch plans (no character names yet)
    @_member_write
    def set_plan(self, discord_id: int, display_name: str, cls: str, spec: str, offspec: str | None, slot: str) -> tuple[Member, RegisteredCharacter]:
        """slot: main | alt. Replaces any existing *planned* character in that slot."""
        if cls not in self.profile.classes:
            raise RegistryError(f"Unknown class {cls}.")
        spec = self.validate_spec(cls, spec)
        offspec = self.validate_spec(cls, offspec) if offspec else None
        m = self.member(discord_id, display_name, create=True)
        is_main = slot == "main"
        prev = next((c for c in m.characters if c.status == "planned" and c.is_main == is_main), None)
        rank = prev.rank if prev and is_main else member_rank(m)  # D25: the member's standing moves to the new main
        m.characters = [c for c in m.characters if c is not prev]
        if is_main:
            for c in m.characters:
                c.is_main = False
        c = RegisteredCharacter(cls=cls, spec=spec, offspec=offspec, is_main=is_main, status="planned", rank=rank if is_main else "alt")
        if prev:  # a re-plan keeps roster placement and identity (the change log shows it as a spec change)
            c.rosters, c.created_at, c.note = list(prev.rosters), prev.created_at, prev.note
        m.characters.append(c)
        settle_ranks(m)
        self.save(m, f"{display_name} plans {slot}: {cls} {spec}{'/' + offspec if offspec else ''}")
        return m, c

    @_member_write
    def set_flex(self, discord_id: int, label: str, roles: list[str]) -> RegisteredCharacter:
        """Extra roles a character can play (besides the roles its spec/offspec already imply)."""
        if any(r not in ROLES for r in roles):
            raise RegistryError(f"Roles are {', '.join(ROLES)}.")
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(label)), None)
        if not c:
            raise RegistryError(f"You have no active character named {label}.")
        own = {self.profile.spec(c.cls, c.spec).role} | ({self.profile.spec(c.cls, c.offspec).role} if c.offspec else set())
        new = sorted({r for r in roles} - own)
        if new == c.flex:
            return c
        c.flex, c.updated_at = new, now()
        self.save(m, f"{m.display_name}: {c.label} flex {', '.join(new) or 'none'}")
        return c

    @_member_write
    def set_roles(self, discord_id: int, display_name: str, primary: str, flex: list[str]) -> Member:
        """The registration wizard's role step: the primary is ignored (it follows the spec); flex goes on the main.
        (/me plan roles is gone — /me char → Flex sets a character's extra roles.)"""
        m = self.member(discord_id, display_name, create=True)
        if not m.main:
            raise RegistryError("Plan or register a main first.")
        self.set_flex(discord_id, m.main.label, [f for f in flex if f != primary] + ([primary] if primary else []))
        return m

    @_member_write
    def name_character(self, discord_id: int, name: str, slot: str = "main", surname: str | None = None, label: str | None = None) -> tuple[Member, RegisteredCharacter]:
        """At launch: give a planned character its real name; it becomes active (pending confirmation)."""
        name = self.normalise_name(name)
        surname = self.normalise_name(surname) if surname else None
        full = f"{name} {surname}" if surname else name
        if self.find(full):
            raise RegistryError(f"{full} is already registered.")
        m = self.member(discord_id)
        c = next((c for c in m.planned() if c.matches(label)), None) if label else next((c for c in m.planned() if c.is_main == (slot == "main")), None)
        if not c:
            raise RegistryError(f"You have no planned {slot} to name. Use /register instead.")
        c.name, c.surname, c.status, c.updated_at = name, surname, "active", now()
        self.save(m, f"{m.display_name} named planned {slot} → {c.label}")
        return m, c

    # ---- items (loot, design §3.1 decision 4): the profile's item tables + what the guild learned (<guild>/items/*.yaml)
    def items(self) -> dict:
        """Every item the guild's loot council knows, by item key (`item_key`: the id as text, or 'n-<slug>' for an item
        known only by name): the profile's tables, then the guild's own files layered on top — a guild file's `tiers`
        override the profile's per spec key, and an item the profile lacks (a learned drop) is an Item with
        `tiers: {}` (untiered) until an officer tiers it. Cached against a stamp — the profile object (a reload or a
        guild override builds a new one) and every item file's name, size and mtime — so a tier written by any path
        (Loot › Items, a data-repo pull, a hand edit) is read on the next call; `learn_item` also resets it."""
        from .profiles import Item

        d = self.store.root / self.key / "items"
        files = sorted(d.glob("*.yaml")) if d.is_dir() else []
        stamp = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            stamp.append((f.name, st.st_size, st.st_mtime_ns))
        cache = self.__dict__.get("_items_cache")
        held = self.__dict__.get("_items_stamp")
        if cache is not None and held is not None and held[0] is self.profile and held[1] == tuple(stamp):
            return cache
        out: dict = {str(i): it for i, it in self.profile.items.items()}
        meta: dict[str, dict] = {}
        if files:
            for f in files:
                try:
                    doc = yaml.safe_load(f.read_text()) or {}
                except yaml.YAMLError:
                    log.warning("%s: unreadable item file %s", self.key, f.name)
                    continue
                iid = int(doc.get("id") or 0)
                key = item_key(iid, doc.get("name"))
                base = out.get(key)
                tiers = {**(base.tiers if base else {}), **{str(k): str(v) for k, v in (doc.get("tiers") or {}).items()}}
                out[key] = Item(id=iid, name=str(doc.get("name") or (base.name if base else key)),
                                slot=str(doc.get("slot") or (base.slot if base else "unknown")),
                                type=str(doc.get("type") or (base.type if base else "unknown")),
                                boss=str(doc.get("boss") or (base.boss if base else "?")), tiers=tiers,
                                usable=list(doc.get("usable") or (base.usable if base else [])),
                                token_group=doc.get("token_group") or (base.token_group if base else None),
                                note=str(doc.get("note") or (base.note if base else "")))
                meta[key] = {**(doc.get("learned") or {}), "file": f.name}
        self.__dict__["_items_cache"] = out
        self.__dict__["_items_meta"] = meta
        self.__dict__["_items_stamp"] = (self.profile, tuple(stamp))  # holds the profile object: compared by identity, never a reused id
        return out

    def item_meta(self, key: str) -> dict:
        """What the guild recorded about a learned item (first_seen, times_dropped, first_run …); {} for a profile item."""
        self.items()
        return dict(self.__dict__.get("_items_meta", {}).get(key) or {})

    def item(self, ref=None, name: str | None = None):
        """An item by id (int or its text) or by name (case-insensitive), from `items()`; None when unknown."""
        items = self.items()
        if ref not in (None, "", 0, "0"):
            hit = items.get(str(ref))
            if hit is not None:
                return hit
        if name:
            n = str(name).strip().lower()
            return next((it for it in items.values() if it.name.lower() == n), None)
        return None

    def item_tier(self, item, spec) -> str | None:
        """The tier of `item` (an Item, or an id / name `item()` resolves) for `spec` (a SpecInfo or 'Class:Spec'): the
        guild's tiers over the profile's (`items()` merged them), most specific key first (Item.tier_for). None = no
        tier data for that spec."""
        it = item if hasattr(item, "tier_for") else self.item(item, item if isinstance(item, str) else None)
        if it is None:
            return None
        if isinstance(spec, str):
            cls, _, sp = spec.partition(":")
            spec = self.profile.spec(cls, sp)
        return it.tier_for(spec)

    def learn_item(self, item_id: int | None, name: str | None, *, boss: str | None = None, instance: str | None = None,
                   run: str | None = None, source: str = "companion", times: int = 1):
        """A drop the item tables don't know (or know untiered): remember it in <guild>/items/<key>.yaml as learned
        (first seen, times dropped, boss, the run) so it can be tiered later on Loot › Items. Returns the Item
        (untiered until someone tiers it). An item the profile knows WITH tiers is returned as is and nothing is
        written. Commits (inside the caller's GitStore.batch, one commit with the drop). `times`: how many drops this
        call records (a loot history import learns an item once for all its rows; source 'import')."""
        known = self.item(item_id, name)
        if known is not None and known.tiers:
            return known
        iid = int(item_id or 0) or (known.id if known else 0)
        nm = str(name or (known.name if known else "") or f"item {iid}").strip()
        key = item_key(iid, nm)
        rel = Path(self.key) / "items" / f"{key}.yaml"
        p = self.store.root / rel
        doc = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {"id": iid or None, "name": nm, "tiers": {}}
        doc.setdefault("name", nm)
        if boss and not doc.get("boss"):
            doc["boss"] = boss
        if instance and not doc.get("instance"):
            doc["instance"] = instance
        learned = doc.setdefault("learned", {})
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        learned.setdefault("first_seen", stamp)
        learned.setdefault("first_run", run)
        learned.setdefault("source", source)
        learned["last_seen"] = stamp
        learned["times_dropped"] = int(learned.get("times_dropped") or 0) + max(1, int(times))
        self.store.write_text(rel, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        self.store.commit(f"{self.key}: learned item {nm}" + (f" ({iid})" if iid else ""))
        self.__dict__.pop("_items_cache", None)
        return self.item(iid, nm)

    def loot_cfg(self) -> dict:
        """The loot scoring settings: profiles/<game>/loot_defaults.yaml with the guild's `loot:` overrides merged (at
        every depth: a dict value merges per key, so 'wishlist.rank_bonus.1' alone keeps #2–#5), plus `untiered_weight`
        (default UNTIERED_WEIGHT) — the upgrade an item without tier data scores for everyone. A key is matched by its
        text (an override's '1' lands on the profile's 1: scoring reads `rank_bonus[1]`)."""
        import copy

        out = copy.deepcopy(self.profile.loot or {})
        _merge_loot(out, copy.deepcopy(self.config.loot or {}))
        out.setdefault("untiered_weight", UNTIERED_WEIGHT)
        return out

    # ---- loot scoring weights (design §3.1 "Weights" L06; dual interfaces phase 4, design §5.48): Config › Loot scoring,
    # the `loot_weights` op (plain text, MCP set_loot_weights) — one validated write of guild.yaml `loot:` per Save
    LOOT_WEIGHT_SKIP = ("distribution",)  # not a number (the judging order) — set in the profile, not on the card

    def loot_weight_defaults(self) -> dict[str, float]:
        """Every number Config › Loot scoring may set, by dotted path ('tier_weight.S+', 'wishlist.rank_bonus.1',
        'untiered_weight'), with the game profile's value (profiles/<game>/loot_defaults.yaml) — the default shown beside
        each weight, and what clearing an override goes back to."""
        base = {k: v for k, v in (self.profile.loot or {}).items() if k not in self.LOOT_WEIGHT_SKIP}
        base.setdefault("untiered_weight", UNTIERED_WEIGHT)
        out: dict[str, float] = {}

        def walk(d: dict, prefix: str) -> None:
            for k, v in d.items():
                if isinstance(v, dict):
                    walk(v, f"{prefix}{k}.")
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[f"{prefix}{k}"] = float(v)

        walk(base, "")
        return out

    def loot_weights(self) -> dict[str, float]:
        """The effective number per path (loot_cfg: the guild's override, else the profile's)."""
        cfg = self.loot_cfg()
        out = {}
        for path, default in self.loot_weight_defaults().items():
            cur = _loot_dig(cfg, path)
            out[path] = float(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else default
        return out

    def loot_overridden(self) -> set[str]:
        """The paths the guild set itself (guild.yaml `loot:`) — the card marks them and offers 'back to the default'."""
        own = self.config.loot or {}
        return {p for p in self.loot_weight_defaults() if _loot_dig(own, p) is not None}

    @staticmethod
    def loot_weight_check(path: str, value) -> float | None:
        """A weight as stored: None (back to the default) for '' / None / 'default'; else a number — a window in whole
        days 1–365, anything else 0–100. RegistryError in words otherwise."""
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "default", "reset")):
            return None
        try:
            n = float(str(value).strip()) if not isinstance(value, (int, float)) or isinstance(value, bool) else float(value)
        except ValueError:
            raise RegistryError(f"{path}: '{value}' isn't a number") from None
        if isinstance(value, bool) or n != n:  # NaN
            raise RegistryError(f"{path}: '{value}' isn't a number")
        if path.endswith("window_days"):
            if n != int(n) or not 1 <= n <= 365:
                raise RegistryError(f"{path}: a window is a whole number of days from 1 to 365")
            return int(n)
        if not 0 <= n <= 100:
            raise RegistryError(f"{path}: a weight is from 0 to 100")
        return round(n, 4)

    @_config_verb("loot")
    def set_loot_weights(self, fields: dict, by: str) -> list[str]:
        """Config › Loot scoring's one Save (and the `loot_weights` op): {path: number | None} — None / '' puts the
        profile's default back. Validated as a whole (an unknown path or a bad number refuses the Save and nothing is
        written), then ONE save of guild.yaml `loot:` under config_lock (the decorator; `rev=` = config_rev('loot')).
        Changes scores from the next judgement on — never a ledger row (who got what, and what it weighed, stay as
        recorded). Returns the change lines ([] = nothing changed, nothing written)."""
        import copy

        defaults = self.loot_weight_defaults()
        cur = self.loot_weights()
        over = copy.deepcopy(self.config.loot or {})
        lines: list[str] = []
        for path, value in (fields or {}).items():
            p = str(path).strip()
            if p not in defaults:
                raise RegistryError(f"{p}: not a loot weight (Config › Loot scoring lists them: tier_weight.S, attendance.weight, …)")
            new = self.loot_weight_check(p, value)
            if new is not None and float(new) == defaults[p]:
                new = None  # the default itself: no override kept (the profile's number stands)
            keys = _loot_keys(self.profile.loot or {}, p)
            _loot_put(over, keys, new)
            after = defaults[p] if new is None else float(new)
            if after != cur[p] or (new is None and _loot_dig(self.config.loot or {}, p) is not None):
                lines.append(f"{p} {cur[p]:g} → {after:g}" + (" (the default)" if new is None else ""))
        over = _loot_prune(over)
        if over == (self.config.loot or {}):
            return []
        self.config.loot = over
        self.save_config(f"loot weights: {'; '.join(lines) or "the guild's changes tidied"} ({by})"[:200])
        return lines or ["loot weights: no number changed (the guild's changes tidied)"]

    # ---- loot history imports (design §3.1 "Data on-ramp" L07; dual interfaces phase 4, design §5.48)
    def _award_pairs(self, names, item_id, item_name) -> set[tuple[str, str]]:
        """What makes two awards the same award for an IMPORT, the day aside: (character, item — by id and by name, and
        the item the tables resolve either to). A row recorded by id and a file naming the item match each other."""
        items = set()
        if item_id:
            items.add(f"i:{int(item_id)}")
        if item_name:
            items.add(f"n:{str(item_name).strip().lower()}")
        it = self.item(int(item_id or 0) or None, item_name)
        if it is not None:
            if it.id:
                items.add(f"i:{int(it.id)}")
            items.add(f"n:{it.name.strip().lower()}")
        return {(str(n).strip().lower(), i) for n in names if str(n or "").strip() for i in items}

    def import_day(self, row):
        """The guild day an import row was received on: a moment the file stated with its zone, moved to the guild's
        timezone (a UTC export of a 10 PM Pacific award is the evening before); else the day as written."""
        at = getattr(row, "received_at", None)
        return at.astimezone(self.tz).date() if at is not None else row.received

    def _import_plan(self, rows, ledger_rows, cfg: dict, kind: str, by: str = "") -> tuple[dict, list]:
        """The dedupe and the weights of an import (no lock, no write): (report, new [(award, row, shown)]).

        A file row is a DUPLICATE when it matches a live (not voided) award row of the ledger: the same character, the
        same item (by id or by name) and the same guild day — or, when the file gave no zone for its time (or no time),
        the day before or after (the export may have written another zone's day). Matching is BY OCCURRENCE: each
        ledger row answers for one file row only, so the N-th copy of (character, item, day) in the file matches the
        N-th in the ledger and two real copies both survive. Same-day matches are taken first (in file order), then
        the ±1 ones; rows of the same file never cancel each other."""
        from .loot import scoring
        from .lootctx import fold_ledger
        from .models import LootAward

        untiered = float(cfg.get("untiered_weight", UNTIERED_WEIGHT))
        awards, _ = fold_ledger(ledger_rows)
        index: dict[tuple[str, str], list[int]] = {}
        held_day: dict[int, object] = {}
        for i, a in enumerate(awards):
            if a.void:
                continue  # a voided award (an undone import, a corrected one) no longer holds the row: the fixed file imports
            held_day[i] = a.received
            for p in self._award_pairs([a.raider], a.item_id, a.item_name):
                index.setdefault(p, []).append(i)
        prepared = []
        for r in rows:
            try:
                hit = self.find(r.raider)
            except RegistryError:
                hit = None
            m, c = hit if hit else (None, None)
            names = [r.raider] + ([c.label] if c is not None else [])
            prepared.append((r, hit, m, c, self._award_pairs(names, r.item_id, r.item_name), self.import_day(r), getattr(r, "received_at", None) is not None))
        used: set[int] = set()
        matched: dict[int, int] = {}  # file row position → ledger award position

        def take(n: int, pairs, days) -> None:
            cands = sorted({i for p in pairs for i in index.get(p, ()) if i not in used and held_day[i] in days})
            if cands:
                used.add(cands[0])
                matched[n] = cands[0]

        for n, (_r, _h, _m, _c, pairs, d, _exact) in enumerate(prepared):  # the same day first, in file order
            take(n, pairs, {d})
        for n, (_r, _h, _m, _c, pairs, d, exact) in enumerate(prepared):  # then a day either side, for a day of unknown zone
            if n not in matched and not exact:
                take(n, pairs, {d - timedelta(days=1), d + timedelta(days=1)})
        new: list[tuple] = []
        dups: list[dict] = []
        unknown_items: dict[str, tuple[str, int]] = {}  # lower-cased name → (as first written, rows)
        unknown_chars: list[str] = []
        for n, (r, hit, m, c, _pairs, d, _exact) in enumerate(prepared):
            it = self.item(int(r.item_id or 0) or None, r.item_name)
            label = it.name if it is not None else (r.item_name or f"item {r.item_id}")
            shown = {"line": r.line, "character": c.label if c is not None else r.raider, "item": label, "received": d.isoformat(),
                     "received_label": self.day_label(d.isoformat(), with_year=True),  # what a person reads (ISO stays for machines)
                     "boss": r.boss or None, "offspec": r.offspec, "known_item": it is not None, "known_character": hit is not None}
            if n in matched:
                on = held_day[matched[n]]
                why = "already in the ledger" if on == d else (f"already in the ledger on {self.day_label(on.isoformat(), with_year=True)} "
                                                               "(the file's day can be another time zone's)")
                dups.append({**shown, "why": why})
                continue
            if it is None:  # grouped regardless of case ('Sunfire Pauldrons' and 'sunfire pauldrons' are one unknown item)
                shown_as, count = unknown_items.get(label.lower(), (label, 0))
                unknown_items[label.lower()] = (shown_as, count + 1)
            if hit is None and r.raider not in unknown_chars:
                unknown_chars.append(r.raider)
            spec = None
            if c is not None:
                try:
                    spec = self.profile.spec(c.cls, c.spec)
                except KeyError:
                    spec = None
            if r.tier and r.total_weight is not None:
                tier, weight, tiered = r.tier, float(r.total_weight), True  # the guild's earlier council tiered it (BisCouncil)
            elif it is not None:
                tier, weight = scoring.weight_for(self.profile, it, spec, cfg)
                tiered = bool(it.tiers)
            else:
                tier, weight, tiered = "?", untiered, False
            award = LootAward(raider=c.label if c is not None else r.raider, item_id=int(it.id or 0) if it is not None else int(r.item_id or 0),
                              item_name=it.name if it is not None else r.item_name, tier=tier, total_weight=weight, offspec=r.offspec,
                              received=d, instance=r.instance or "", boss=r.boss or "", discord_id=m.discord_id if m is not None else None,
                              tiered=tiered, by=by, source="import")
            new.append((award, r, {**shown, "tier": tier, "weight": round(weight, 3)}))
        new_rev = hashlib.sha1(json.dumps([[a.raider, a.item_id, a.item_name, a.received.isoformat()] for a, _, _ in new],
                                          sort_keys=True).encode()).hexdigest()[:12]
        report = {"kind": kind, "rows": len(rows), "new": len(new), "duplicates": len(dups), "unknown_items": len(unknown_items),
                  "unknown_characters": unknown_chars[:50],
                  "groups": {"new": [s for _, _, s in new][:300], "duplicates": dups[:300],
                             "unknown_items": [{"item": k, "rows": v} for k, v in sorted(unknown_items.values(), key=lambda kv: (-kv[1], kv[0].lower()))][:300]},
                  "rev": new_rev}
        report["line"] = (f"{len(rows)} row(s), {len(dups)} duplicate(s), {len(unknown_items)} unknown item(s) (recorded by name)"
                          + (f", {len(unknown_chars)} character(s) nobody registered (recorded by name)" if unknown_chars else ""))
        return report, new

    @staticmethod
    def live_council_refusal(live_councils) -> str | None:
        """THE refusal of an import apply while a council of the guild is live (its run is locked): the council writes
        award rows as they're confirmed, so the file's dedupe could have read the ledger before tonight's rows — and
        count them twice. None when no council is live. `live_councils`: the runs' names as a person reads them."""
        names = [str(n) for n in dict.fromkeys(live_councils or []) if str(n or "").strip()]
        if not names:
            return None
        return (f"A loot council is live for {join_words(names)}. It records awards as they're confirmed, so an import now could count "
                "them twice. Import after the council ends, and use the preview meanwhile.")

    def import_awards(self, rows, by: str, by_id: int | None = None, *, preview: bool = False, kind: str = "generic", rev: str | None = None,
                      live_councils=()) -> dict:
        """A loot history file's rows (importers/ledger_csv.ImportRow) into the guild's ledger — Loot › Items › Import,
        MCP import_ledger. For IMPORTS only, a row is a duplicate when it matches a live award row of the ledger
        (_import_plan: same character, item by id or name, and guild day — a day either side when the file gave no zone;
        by occurrence, so two real copies both import) — the council's own awards dedupe by their import_id as before.
        New rows are APPENDED (source 'import', `by`, the file's kind, its note);
        nothing in the ledger is ever edited or deleted. An item the tables don't know is recorded by name (D4: untiered,
        `untiered_weight`) and, on an apply, learned (learn_item, source 'import', once per item for all its rows) so it is
        listed on Loot › Items › Untiered and can be tiered there — its awards then re-weight on the D12 question like
        any learned drop's (an untiered item the tables know is learned the same way); a known item is weighed tier × slot for the character's spec (the file's own tier and weight
        when it carries them — BisCouncil); a character nobody registered is recorded by name. `preview`: nothing written.
        One commit. `rev` (an apply): the preview's rev — when the rows that would be added differ now (the ledger moved
        since the preview), nothing is written and the report says `changed`. `live_councils` (the caller's live,
        non-test councils of this guild, by run name): an apply is refused while any is live (live_council_refusal);
        a preview is not. The dedupe runs on a snapshot of the ledger outside `config_lock`; the lock is held only to
        check the ledger is still that snapshot (else the dedupe runs again on what is there) and append. Returns
        {rows, new, duplicates,
        unknown_items, unknown_characters, groups{new, duplicates, unknown_items} (each row with `received` ISO and
        `received_label` — the day a person reads), rev, changed, learned (an apply: items learned), line: 'N rows, M
        duplicates, K unknown items (recorded by name)'}."""
        rows = list(rows or [])
        rel = Path(self.key) / "ledger.jsonl"
        cfg = self.loot_cfg()
        refusal = self.live_council_refusal(live_councils)
        if refusal and not preview:
            raise RegistryError(refusal)
        snap = self.store.read_jsonl(rel)
        report, new = self._import_plan(rows, snap, cfg, kind, by)
        report["changed"] = bool(rev) and rev != report["rev"]
        if refusal:
            report["live_council"] = refusal  # the preview says why Confirm will wait
        if preview or not new or report["changed"]:
            return report
        with self.config_lock:  # two imports at once never both pass the dedupe: the check that the ledger is the snapshot and the append are one step
            now_rows = self.store.read_jsonl(rel)
            if now_rows != snap:  # the ledger moved since the snapshot: the dedupe again, on what is there now
                report, new = self._import_plan(rows, now_rows, cfg, kind, by)
                report["changed"] = bool(rev) and rev != report["rev"]
                if not new or report["changed"]:
                    return report
            new_rev = report["rev"]
            stamp = now()
            # every imported row gets a stable import_id ('<batch>-<n>') and the batch id, so the import can be undone
            # (undo_import: one void row per award, the shape the council's corrections write — lootctx.void_row)
            batch = f"imp-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{new_rev[:6]}"
            learn: dict[str, list] = {}  # item key → [item_id, name, boss, instance, rows]: untiered items land on Loot › Items › Untiered
            for a, r, _ in new:
                it = self.item(a.item_id or None, a.item_name)
                if it is not None and it.tiers:
                    continue
                k = item_key(a.item_id or (it.id if it is not None else 0), a.item_name or (it.name if it is not None else None))
                hold = learn.setdefault(k, [a.item_id or None, a.item_name, a.boss or None, a.instance or None, 0])
                hold[4] += 1
            with self.store.batch(f"{self.key}: {len(new)} award(s) imported from a {kind} file by {by}"):
                for n, (a, r, _) in enumerate(new, 1):
                    a.import_id = f"{batch}-{n}"
                    row = a.model_dump(mode="json", exclude_none=True)
                    row.update(imported=kind, imported_at=stamp, import_batch=batch, **({"note": r.note} if r.note else {}),
                               **({"by_id": by_id} if by_id else {}))
                    self.store.append_jsonl(rel, row)
                for iid, nm, boss, inst, n in learn.values():
                    self.learn_item(iid, nm, boss=boss, instance=inst, source="import", times=n)
                self.store.commit(f"{self.key}: {len(new)} award(s) imported from a {kind} file by {by}")
            report["learned"] = len(learn)
            report["batch"] = batch
        return report

    def import_batches(self, limit: int = 20) -> list[dict]:
        """The ledger's imports, newest first (Loot › Items › Import's 'Earlier imports', MCP import_ledger_batches):
        {batch, kind, at (ISO), at_label (what a person reads), by, rows, live (rows not voided), consequence (the Undo
        confirm's sentence, composed here)}. Only rows written since imports carry an import_id (imp-<stamp>-<rev>-<n>)."""
        from .lootctx import fold_ledger

        rows = self.store.read_jsonl(Path(self.key) / "ledger.jsonl")
        awards, _ = fold_ledger(rows)
        live = {a.import_id for a in awards if a.import_id and not a.void}
        out: dict[str, dict] = {}
        for r in rows:
            b = r.get("import_batch")
            if r.get("kind") or not b:
                continue
            o = out.setdefault(b, {"batch": b, "kind": r.get("imported"), "at": r.get("imported_at"), "by": r.get("by"), "rows": 0, "live": 0})
            o["rows"] += 1
            o["live"] += 1 if r.get("import_id") in live else 0
        res = sorted(out.values(), key=lambda o: str(o["at"] or ""), reverse=True)[:limit]
        for o in res:
            o["at_label"] = self.local12(o["at"]) if o["at"] else ""
            o["consequence"] = self.undo_consequence(o)
        return res

    @staticmethod
    def undo_consequence(o: dict) -> str:
        if not o.get("live"):
            return "Every award this import added is already voided, so there is nothing to undo."
        n = int(o["live"])
        return (f"The {n} award{'s' if n != 1 else ''} this import added stop counting in recent loot and in 'already has'. "
                "The rows stay in the ledger, each with a void row beside it. Importing a fixed file afterwards adds them again.")

    def undo_import(self, batch: str, by: str, reason: str = "", by_id: int | None = None) -> dict:
        """Undo one import: a `kind: void` row (lootctx.void_row — the council's correction path) for every award of the
        batch that is still live; nothing is edited or deleted. One commit; idempotent (an undone import voids nothing).
        Returns {batch, voided, line}. RegistryError for a batch the ledger doesn't hold. The caller reloads the council's
        context (RegistryLootContext.reload)."""
        from .lootctx import fold_ledger, void_row

        batch = str(batch or "").strip()
        rel = Path(self.key) / "ledger.jsonl"
        why = (str(reason or "").strip() or "import undone")[:200]
        with self.config_lock:
            rows = self.store.read_jsonl(rel)
            ids = [r["import_id"] for r in rows if not r.get("kind") and r.get("import_batch") == batch and r.get("import_id")]
            if not batch or not ids:
                raise RegistryError(f"{batch or '?'}: no import with that id in the ledger")
            awards, _ = fold_ledger(rows)
            live = {a.import_id for a in awards if a.import_id and not a.void}
            todo = [i for i in ids if i in live]
            if todo:
                at = now()
                msg = f"{self.key}: import {batch} undone by {by} ({len(todo)} award(s) voided)"
                with self.store.batch(msg):
                    for i in todo:
                        self.store.append_jsonl(rel, {**void_row(i, why, by, at), **({"by_id": by_id} if by_id else {})})
                    self.store.commit(msg)
        line = (f"import {batch} undone: {len(todo)} award(s) voided — {why}" if todo else f"import {batch}: already undone, nothing voided")
        return {"batch": batch, "voided": len(todo), "line": line}

    # ---- attendance (design §3.5 L35/M43, D49): from DONE runs, keyed by the member (every character rolls up)
    def attendance_window(self, window_days: int | None = None) -> int:
        """The attendance window in days: `window_days`, else the loot policy's (Registry.loot_cfg `attendance.window_days`,
        D49 "window = the policy's"), else ATTENDANCE_WINDOW_DAYS."""
        if window_days:
            return max(1, int(window_days))
        att = self.loot_cfg().get("attendance") or {}
        return max(1, int(att.get("window_days") or ATTENDANCE_WINDOW_DAYS))

    def done_runs(self) -> list:
        """Every DONE run of the guild (raidcycle.RaidEvent), oldest first, parsed from <guild>/raids/*.json — cached
        against each file's name, size and mtime, so a run a worker thread just closed is read on the next call."""
        from .raidcycle import RaidEvent

        d = self.store.root / self.key / "raids"
        files = sorted(d.glob("*.json")) if d.is_dir() else []
        stamp = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            stamp.append((f.name, st.st_size, st.st_mtime_ns))
        held = self.__dict__.get("_done_runs")
        if held is not None and held[0] == tuple(stamp):
            return held[1]
        out = []
        for f in files:
            try:
                ev = RaidEvent.model_validate_json(f.read_text())
            except Exception:  # noqa: BLE001 — one unreadable run never hides the others
                log.warning("%s: unreadable run file %s", self.key, f.name)
                continue
            if ev.state == "done":
                out.append(ev)
        out.sort(key=lambda e: e.starts_at)
        self.__dict__["_done_runs"] = (tuple(stamp), out)
        return out

    def attendance_runs_in(self, window_days: int | None = None, at: datetime | None = None) -> list:
        """The runs attendance counts over: done, not a test run, with activity (somebody held a seat), started within
        the window before `at` (default now). Cancelled runs never count."""
        from . import raidcycle as rc

        t = at or self.now_local()
        lo = t - timedelta(days=self.attendance_window(window_days))
        return [ev for ev in self.done_runs() if lo < ev.start <= t and not rc.is_test_run(self, ev) and ev.seated()]

    def attendance_tracked(self, window_days: int | None = None, at: datetime | None = None) -> dict:
        """{runs: real done runs in the window, first: ISO start of the guild's first real done run ever (None: none
        yet), known: whether the window holds ATTENDANCE_MIN_RUNS runs — below that attendance is unknown for everyone,
        neutral in loot scoring (§3.1 decision 10), never a penalty}."""
        from . import raidcycle as rc

        runs = self.attendance_runs_in(window_days, at)
        first = next((ev.starts_at for ev in self.done_runs() if not rc.is_test_run(self, ev) and ev.seated()), None)
        return {"runs": len(runs), "first": first, "known": len(runs) >= ATTENDANCE_MIN_RUNS}

    def _attendance_row(self, m: "Member", ev) -> dict | None:
        """One done run as `m`'s attendance sees it, or None when the run wasn't theirs to attend (D49):
        - in the pool: a regular of the run when it opened (recurring), or they Joined / Benched, held a seat or called
          out; an ad hoc run counts only for those who joined it (No thanks to a pickup costs nothing);
        - away = an absence on file for the day BEFORE the run started (the sheet's 'away' answer, or an absence record
          created before the start — `_away_with_notice`): shown, never counted; one recorded afterwards is no notice;
        - attended (the loot factor's): rostered when the run ended AND, on a recurring run that asks for confirmations,
          confirmed (a seat that came after the lock — a fill yes, an officer's add — carries no placement ask: the yes
          was the commitment). The attendance VIEW also counts the bench (`attended_view`)."""
        from . import raidcycle as rc

        uid = m.discord_id
        sg = ev.signups.get(str(uid))
        seat = ev.seat_of(uid)
        called = any(c.discord_id == uid for c in ev.callouts)
        adhoc = rc.run_kind(self, ev) == "adhoc"
        joined = bool(sg and sg.status in ("in", "sub"))
        regular = (not adhoc) and uid in (ev.regulars or [])
        if not (regular or joined or seat is not None or called):
            return None
        away = bool(sg and sg.source == "absence") or self._away_with_notice(m, ev)
        if seat is not None:
            p = seat[1]
            character, cls, spec = p.character or p.signup_name, p.cls, p.spec
        elif sg is not None:
            character, cls, spec = sg.character, sg.cls, sg.spec
        else:
            c = m.main
            character, cls, spec = (c.label, c.cls, c.spec) if c else ("", "", "")
        if seat is not None:
            outcome = "rostered"
            if not adhoc and rc.run_confirms(self, ev):
                ask = next((a for a in reversed(m.placement_asks) if a.get("roster") == ev.team), None)
                if ask is not None and ask.get("answer") != "yes":
                    outcome = "unconfirmed"
        elif away:
            outcome = "away"
        elif called:
            outcome = "called_out"
        elif joined:
            outcome = "benched"
        elif sg is not None and sg.status == "out":
            outcome = "no_thanks"
        else:
            outcome = "no_answer"
        if adhoc and outcome == "away":
            return None  # an ad hoc run was never theirs to miss
        counted = outcome != "away"
        try:
            raid = self.raid_def(ev.instance).get("name", ev.instance) if ev.instance else ev.team
        except Exception:  # noqa: BLE001 — a raid removed from the profile since
            raid = ev.instance or ev.team
        return {"key": ev.key, "start": ev.starts_at, "when": self.local12(ev.start, "%a %d %b"), "raid": raid,
                "kind": "adhoc" if adhoc else "recurring", "outcome": outcome, "counted": counted,
                "attended": counted and outcome == "rostered", "attended_view": counted and outcome in ("rostered", "benched"),
                "character": character, "cls": cls, "spec": spec}

    @staticmethod
    def _away_with_notice(m: "Member", ev) -> bool:
        """Whether `m` was away WITH NOTICE for `ev` (D49): an absence covering the run's day that was on file before the
        run started. An absence recorded afterwards (a no-show written up the next day) never takes the miss out of the
        total. A legacy record without a readable `created_at` counts as given in time."""
        day = ev.start.date().isoformat()
        start = ev.start if ev.start.tzinfo else ev.start.replace(tzinfo=timezone.utc)
        for a in m.absences:
            if not (a.start <= day <= a.end):
                continue
            try:
                made = datetime.fromisoformat(a.created_at)
            except (TypeError, ValueError):
                return True
            if made.tzinfo is None:
                made = made.replace(tzinfo=timezone.utc)
            if made <= start:
                return True
        return False

    def _attendance_member(self, member) -> "Member | None":
        if isinstance(member, Member):
            return member
        try:
            return self.members.get(int(member))
        except (TypeError, ValueError):
            return None

    def attendance_runs(self, member, window_days: int | None = None, at: datetime | None = None) -> list[dict]:
        """`member`'s (a Member or a Discord id) rows over the window, newest first: {key, start, when, raid, kind,
        outcome rostered|unconfirmed|benched|away|called_out|no_thanks|no_answer, counted, attended (loot factor),
        attended_view (bench counts), character, cls, spec}. Every character of theirs counts (the rollup is by Discord
        id, never by character name)."""
        m = self._attendance_member(member)
        if m is None:
            return []
        rows = [r for ev in self.attendance_runs_in(window_days, at) if (r := self._attendance_row(m, ev)) is not None]
        return sorted(rows, key=lambda r: r["start"], reverse=True)

    def attendance(self, member, window_days: int | None = None, *, bench: bool = False, at: datetime | None = None) -> tuple[int, int]:
        """(attended, total) for `member` (a Member or a Discord id) over the done runs of the window (default the loot
        policy's): total = the runs they were in the pool for and not away; attended = rostered (+ confirmed on a
        recurring run) — `bench` True also counts the bench (the attendance VIEW, D49; the loot factor never does).
        (0, 0) = nothing to count: unknown, which loot scoring treats as neutral and leaves out."""
        rows = self.attendance_runs(member, window_days, at)
        key = "attended_view" if bench else "attended"
        return sum(1 for r in rows if r[key]), sum(1 for r in rows if r["counted"])

    def attendance_known(self, member, window_days: int | None = None, at: datetime | None = None) -> tuple[int, int] | None:
        """The loot factor's input: (attended, total) when it is known — the window holds ATTENDANCE_MIN_RUNS real
        runs and the member had at least one — else None (neutral, excluded; §3.1 decision 10)."""
        if not self.attendance_tracked(window_days, at)["known"]:
            return None
        att, total = self.attendance(member, window_days, at=at)
        return (att, total) if total else None

    def attendance_text(self, member, window_days: int | None = None, at: datetime | None = None) -> str:
        """The attendance sentence every surface prints (Me › Attendance, /me view, MCP get_member), computed — never
        a hard-coded date: 'not tracked yet — counts from the first run that ends' / '5 of 6 in the last 30 days' /
        '2 of 2 since Tue 9 Dec' (the window reaches back past the guild's first run) / 'no runs for you in the last 30
        days'. Counts the bench (the attendance view, D49)."""
        days = self.attendance_window(window_days)
        tracked = self.attendance_tracked(days, at)
        if tracked["first"] is None:
            return "not tracked yet — counts from the first run that ends"
        att, total = self.attendance(member, days, bench=True, at=at)
        t = at or self.now_local()
        first = datetime.fromisoformat(tracked["first"])
        span = f"since {self.local12(first, '%a %d %b')}" if first > t - timedelta(days=days) else f"in the last {days} days"
        return f"{att} of {total} {span}" if total else f"no runs for you {span}"

    def set_item_tiers(self, ref, tiers: dict, by: str) -> tuple[str, dict]:
        """Loot › Items, the `tier` op and MCP tier_item: write tiers per spec key into the guild's item file
        (<guild>/items/<key>.yaml — a learned item's file keeps its `learned` facts; a profile item gets a file of its
        own that layers over the profile's tiers). `tiers`: {spec key: tier | None}; a spec key is 'Class:Spec',
        'Class:*' or 'role:<role>', a tier one of TIER_VALUES, None or '-' clears a tier the guild set. Returns
        (the change line, {key: (before, after)}). Re-weighting past awards is the caller's (D12: asked, default no).
        The read and the write run under ONE config_lock (an RLock: a CSV import already holding it nests), so a CSV
        import's rev check and batch and a grid Save never interleave — each Save reads the tiers as they are."""
        with self.config_lock:
            return self._set_item_tiers(ref, tiers, by)

    def _set_item_tiers(self, ref, tiers: dict, by: str) -> tuple[str, dict]:
        it = self.item(ref, ref if isinstance(ref, str) else None)
        if it is None:
            raise RegistryError(f"No item {ref} on the tables or among the learned items.")
        key = item_key(it.id, it.name)
        rel = Path(self.key) / "items" / f"{key}.yaml"
        p = self.store.root / rel
        doc = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {"id": it.id or None, "name": it.name, "slot": it.slot, "type": it.type, "boss": it.boss}
        own = {str(k): str(v) for k, v in (doc.get("tiers") or {}).items()}
        prof = self.profile.items.get(int(it.id)) if it.id else None
        changes: dict[str, tuple] = {}
        for spec_key, value in (tiers or {}).items():
            sk = self._tier_key(str(spec_key))
            v = None if value in (None, "", "-", "–") else str(value).strip().upper()
            if v is not None and v not in TIER_VALUES:
                raise RegistryError(f"{spec_key}: a tier is one of {', '.join(TIER_VALUES)} (or - to clear).")
            before = it.tiers.get(sk)
            if v is None:
                if sk not in own:
                    if prof is not None and sk in prof.tiers:
                        raise RegistryError(f"{sk}: that tier comes from the game profile — set another tier instead of clearing it.")
                    continue
                own.pop(sk)
            else:
                own[sk] = v
            after = v if v is not None else (prof.tiers.get(sk) if prof is not None else None)
            if before != after:
                changes[sk] = (before, after)
        if not changes:
            return f"{it.name}: tiers unchanged", {}
        doc["tiers"] = own
        doc.setdefault("name", it.name)
        self.store.write_text(rel, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        self.store.commit(f"{self.key}: tiers for {it.name} ({by})")
        self.__dict__.pop("_items_cache", None)
        words = ", ".join(f"{k} {a or '—'}" for k, (_, a) in changes.items())
        return f"{it.name}: {words}", changes

    def _tier_key(self, spec_key: str) -> str:
        """'Priest:Holy' / 'Priest:*' / 'role:healer' checked against the profile; anything else is refused."""
        sk = spec_key.strip()
        if sk.lower().startswith("role:"):
            role = sk.split(":", 1)[1].strip().lower()
            if role not in ROLES:
                raise RegistryError(f"{sk}: a role is one of {', '.join(ROLES)}.")
            return f"role:{role}"
        cls, _, spec = sk.partition(":")
        cls = next((c for c in self.profile.classes if c.lower() == cls.strip().lower()), None)
        if cls is None:
            raise RegistryError(f"{sk}: name a class and spec like 'Priest:Holy', 'Priest:*' or 'role:healer'.")
        spec = spec.strip()
        if spec == "*":
            return f"{cls}:*"
        sp = next((s for s in self.profile.classes[cls] if s.lower() == spec.lower()), None)
        if sp is None:
            raise RegistryError(f"{sk}: {cls} has no spec {spec or '?'} ({', '.join(self.profile.classes[cls])}).")
        return f"{cls}:{sp}"

    # ---- wishlists (D5: native, shaped for a ThatsMyBIS import — per character, priority order, MS/OS, a note)
    def wishlist_rev(self, m: Member, label: str) -> str:
        """A fingerprint of one character's wishlist (what Me › Wishlist saves whole): computed, never stored."""
        c = next((c for c in m.characters if c.label == label), None)
        rows = [e.model_dump(exclude={"added_at"}) for e in (c.wishlist if c else [])]
        return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def wish_character(self, discord_id: int, ref: str | None = None) -> tuple[Member, RegisteredCharacter]:
        """The character a wishlist write is for: named (label or first name), else the only one, else the main."""
        m = self.members.get(discord_id)
        chars = [c for c in (m.characters if m else []) if c.status in ("active", "planned")]
        if not chars:
            raise RegistryError("You have no registered characters yet. Use /register first.")
        r = str(ref or "").strip()
        if r:
            c = next((c for c in chars if c.label == r), None) or next((c for c in chars if c.matches(r)), None)
            if c is None:
                raise RegistryError(f"{r} isn't one of {m.display_name}'s characters ({', '.join(c.label for c in chars)}).")
            return m, c
        if len(chars) == 1:
            return m, chars[0]
        if m.main is not None:
            return m, m.main
        raise RegistryError("Which character? " + ", ".join(c.label for c in chars))

    def wish_item(self, item_id=None, name: str | None = None) -> tuple[int, str]:
        """(item id, name) for a wishlist row: the tables' or a learned item's when known (by id or name); an item the
        tables don't know yet is kept by name with id 0 (D4); an unknown id with no name is refused."""
        nm = str(name or "").strip()[:100]
        iid = int(item_id) if str(item_id or "").strip().lstrip("-").isdigit() else 0
        it = self.item(iid or None, nm or None)
        if it is not None:
            return int(it.id or 0), it.name
        if not nm:
            raise RegistryError(f"No item {iid} on the tables — give the item's name.")
        return iid, nm

    def set_wishlist(self, discord_id: int, character: str | None, rows: list[dict], by: str, *, rev: str | None = None) -> str:
        """The whole wishlist of one character (Me › Wishlist's one Save, MCP set_wishlist, and the wizard / plain-text
        add and remove through wish_add / wish_remove). Rows in priority order: {item_id?, name?, rank?, offspec? |
        kind: ms|os, note?}; a given rank orders them, the list position breaks ties, ranks are renumbered 1..n. At most
        WISHLIST_MAX; an item twice is refused. `rev` (wishlist_rev, what the author saw): a list someone changed
        since → WishlistChanged, nothing written. An entry kept from before keeps its source and when it was added."""
        with self.config_lock:
            m, c = self.wish_character(discord_id, character)
            if rev is not None and rev != self.wishlist_rev(m, c.label):
                raise WishlistChanged(f"{c.label}'s wishlist changed since you opened it — reload it and try again.")
            old = list(c.wishlist)
            entries: list[tuple[int, int, WishlistEntry]] = []
            for i, row in enumerate(rows or []):
                if not isinstance(row, dict):
                    raise RegistryError("wishlist rows must be objects")
                iid, nm = self.wish_item(row.get("item_id"), row.get("name"))
                if any(_same_wish(e.item_id, e.name, iid, nm) for _, _, e in entries):
                    raise RegistryError(f"{nm} is on {c.label}'s wishlist twice.")
                kind = str(row.get("kind") or "").strip().lower()
                offspec = kind == "os" if kind in ("ms", "os") else bool(row.get("offspec"))
                note = re.sub(r"\s+", " ", str(row.get("note") or "")).strip()[:WISH_NOTE_MAX] or None
                prev = next((e for e in old if _same_wish(e.item_id, e.name, iid, nm)), None)
                try:
                    rank = int(row.get("rank") or (i + 1))
                except (TypeError, ValueError):
                    raise RegistryError("rank must be a number") from None
                e = WishlistEntry(item_id=iid, name=nm, offspec=offspec, note=note, source=prev.source if prev else str(row.get("source") or "native"),
                                  **({"added_at": prev.added_at} if prev else {}))
                entries.append((rank, i, e))
            if len(entries) > WISHLIST_MAX:
                raise RegistryError(f"A wishlist holds up to {WISHLIST_MAX} items per character — drop one first.")
            new = [e for _, _, e in sorted(entries, key=lambda t: (t[0], t[1]))]
            for n, e in enumerate(new, 1):
                e.rank = n
            line = _wish_diff(c.label, old, new)
            if line is None:
                return f"{c.label}'s wishlist: unchanged"
            c.wishlist = new
            c.updated_at = now()
            self.save(m, f"{line} (by {by})"[:150])
            return line

    def wish_add(self, discord_id: int, character: str | None, item, *, by: str, rank: int | None = None, offspec: bool = False,
                 note: str | None = None, name: str | None = None) -> str:
        """One item onto a character's wishlist at `rank` (default: after the last). Already on it: moved to that
        rank with the new MS/OS and note. `item`: an id or a name (`name` when both are known)."""
        m, c = self.wish_character(discord_id, character)
        ref_id = item if isinstance(item, int) or str(item or "").strip().isdigit() else None
        iid, nm = self.wish_item(ref_id, name or (None if ref_id is not None else item))
        rows = [r for r in _wish_rows(c.wishlist) if not _same_wish(r["item_id"], r["name"], iid, nm)]
        at = max(1, min(int(rank or (len(rows) + 1)), len(rows) + 1))
        row = {"item_id": iid, "name": nm, "offspec": offspec, "note": note}
        rows.insert(at - 1, row)
        for n, r in enumerate(rows, 1):
            r["rank"] = n
        return self.set_wishlist(discord_id, c.label, rows, by)

    def wish_remove(self, discord_id: int, character: str | None, item, *, by: str) -> str:
        m, c = self.wish_character(discord_id, character)
        ref = str(item or "").strip()
        rows = _wish_rows(c.wishlist)
        keep = [r for r in rows if not (str(r["item_id"]) == ref and r["item_id"]) and r["name"].lower() != ref.lower()]
        if len(keep) == len(rows):
            raise RegistryError(f"{ref} isn't on {c.label}'s wishlist.")
        return self.set_wishlist(discord_id, c.label, keep, by)

    def wishlists(self) -> dict[str, list[dict]]:
        """What scoring expects (scoring.candidates' `wishlists`): {character label: [{item: id or name, rank, offspec}]},
        from every active character's native wishlist. An entry kept by name whose item is known by now scores by id."""
        out: dict[str, list[dict]] = {}
        for m in self.members.values():
            for c in m.characters:
                if c.status == "retired" or not c.wishlist:
                    continue
                rows = []
                for e in c.wishlist:
                    item = e.item_id
                    if not item:
                        hit = self.item(None, e.name)
                        item = int(hit.id) if hit is not None and hit.id else e.name
                    rows.append({"item": item, "rank": e.rank, "offspec": e.offspec})
                out[c.label] = rows
        return out

    def import_tmb_wishlists(self, text: str, by: str, *, preview: bool = False) -> dict:
        """A ThatsMyBIS wishlist export (CSV: character_name, item_name, item_id, sort_order, is_offspec, note,
        received_at; a `type` column keeps only `wishlist` rows) into the native wishlists (D5). Per character: the rows
        in TMB's order, deduped (first wins), received rows skipped; an item already on the native list is a duplicate
        (the native entry stays); the character's earlier TMB rows are replaced; a native entry is never deleted; past
        WISHLIST_MAX the rest is skipped and counted. Unknown characters are named, not created. `preview`: nothing
        written. One commit. Returns {rows, characters, added, replaced, duplicates, received, over_cap, unknown, lines,
        per_character: [{character, added, kept, replaced, duplicates, over_cap} — item names: what the preview shows per
        character, "nothing deleted" made visible]}."""
        import csv
        import io

        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        norm = [{str(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]
        report = {"rows": len(norm), "characters": 0, "added": 0, "replaced": 0, "duplicates": 0, "received": 0, "over_cap": 0, "unknown": [], "lines": [],
                  "per_character": []}
        per: dict[tuple[int, str], list[tuple[float, int, dict]]] = {}
        for i, r in enumerate(norm):
            if r.get("type") and r["type"].lower() not in ("wishlist", "wish"):
                continue
            who = r.get("character_name") or r.get("character") or ""
            if not who:
                continue
            if r.get("received_at"):
                report["received"] += 1
                continue
            try:
                hit = self.find(who)
            except RegistryError:
                hit = None
            if hit is None:
                if who not in report["unknown"]:
                    report["unknown"].append(who)
                continue
            m, c = hit
            try:
                order = float(r.get("sort_order") or r.get("order") or (i + 1))
            except ValueError:
                order = float(i + 1)
            per.setdefault((m.discord_id, c.label), []).append((order, i, r))
        writes: list[tuple[Member, RegisteredCharacter, list[WishlistEntry]]] = []
        for (uid, label), got in per.items():
            m = self.members[uid]
            c = next(x for x in m.characters if x.label == label)
            # copies: the renumbering below must not touch the live list (a preview writes nothing, not even in memory)
            natives = [e.model_copy() for e in c.wishlist if e.source != "tmb"]
            report["replaced"] += sum(1 for e in c.wishlist if e.source == "tmb")
            imported: list[WishlistEntry] = []
            dup_names: list[str] = []
            cap_names: list[str] = []
            for _, _, r in sorted(got, key=lambda t: (t[0], t[1])):
                try:
                    iid, nm = self.wish_item(r.get("item_id"), r.get("item_name") or r.get("name"))
                except RegistryError:
                    continue
                if any(_same_wish(e.item_id, e.name, iid, nm) for e in natives + imported):
                    report["duplicates"] += 1
                    dup_names.append(nm)
                    continue
                if len(natives) + len(imported) >= WISHLIST_MAX:
                    report["over_cap"] += 1
                    cap_names.append(nm)
                    continue
                off = r.get("is_offspec", "").lower() in TRUE_WORDS
                note = re.sub(r"\s+", " ", r.get("note") or "").strip()[:WISH_NOTE_MAX] or None
                imported.append(WishlistEntry(item_id=iid, name=nm, offspec=off, note=note, source="tmb"))
            new = natives + imported
            for n, e in enumerate(new, 1):
                e.rank = n
            report["added"] += len(imported)
            report["characters"] += 1
            report["lines"].append(f"{label}: {len(imported)} from TMB" + (f", {len(natives)} kept from the site" if natives else ""))
            report["per_character"].append({"character": label, "added": [e.name for e in imported], "kept": [e.name for e in natives],
                                            "replaced": [e.name for e in c.wishlist if e.source == "tmb"], "duplicates": dup_names, "over_cap": cap_names})
            writes.append((m, c, new))
        if not preview and writes:
            with self.config_lock, self.store.batch(f"{self.key}: TMB wishlists imported by {by} ({report['characters']} character(s))"):
                for m, c, new in writes:
                    c.wishlist = new
                    c.updated_at = now()
                    self.save(m, f"{c.label}'s wishlist imported from ThatsMyBIS (by {by})")
        return report

    # ---- raids (instances) = defaults from the game profile + guild overrides
    RAID_FIELDS = ("lockout_days", "duration_hours", "notes")

    def raid_def(self, instance: str | None) -> dict:
        """Effective raid definition: profile raids.yaml merged with guild overrides (comp merged per role)."""
        base = dict(self.profile.raids.get(instance or "", {}) or {})
        over = self.config.raids.get(instance or "", {}) if instance else {}
        out = {**base}
        for k, v in over.items():
            if k == "comp":
                comp = {r: dict(b) for r, b in (base.get("comp") or {}).items()}
                for r, b in (v or {}).items():
                    comp[r] = {**comp.get(r, {}), **b}
                out["comp"] = comp
            elif k == "weights":
                out["weights"] = {**(base.get("weights") or {}), **(v or {})}
            else:
                out[k] = v
        out.setdefault("slots", [])
        for k, v in RAID_DEFAULTS.items():  # nudge: DM the mains who haven't answered, once, at nudge_hours_before;
            out.setdefault(k, v)  # fill_ask_hours: a fill DM with no answer counts as no after this long (never later than the run start)
        out.setdefault("nudge_hours_before", default_nudge_hours(out["signup_lead_hours"], out["lock_hours_before"]))
        out["loot_auto_distribute"] = parse_bool(out.get("loot_auto_distribute", True))
        for k in RAID_BOOL_FIELDS:
            out[k] = parse_bool(out[k])
        out["weights"] = {**RAID_WEIGHT_DEFAULTS, **(out.get("weights") or {})}
        return out

    def raid_shell(self, instance: str) -> dict:
        """A roster-shaped dict for a raid definition, so pool/comp/group analytics can run per raid."""
        rd = self.raid_def(instance)
        over = self.config.raids.get(instance, {})
        return {"key": instance, "name": rd.get("name", instance), "size": int(rd.get("size") or DEFAULT_RAID_SIZE), "instance": instance,
                "comp_targets": over.get("comp_targets") or {}, "comp_groups": over.get("comp_groups") or []}

    def first_open(self, instance: str | None):
        """Anchor datetime (aware) for the instance's lockout windows, or None."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z

        raw = self.raid_def(instance).get("first_open")
        if not raw:
            return None
        try:
            t = _dt.fromisoformat(str(raw))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=_Z(self.config.timezone))

    def lockout_window(self, instance: str | None, at) -> tuple:
        """(start, end) of the lockout window containing `at`: anchored on first_open + k × lockout_days when the
        raid has an anchor (before it opens: the first window), else a rolling window ending at `at`."""
        from datetime import timedelta as _td

        days = int(self.raid_def(instance).get("lockout_days", 7))
        anchor = self.first_open(instance)
        if anchor is None:
            return at - _td(days=days), at
        if at < anchor:
            return anchor, anchor + _td(days=days)
        k = int((at - anchor).total_seconds() // (days * 86400))
        start = anchor + _td(days=days * k)
        return start, start + _td(days=days)

    def role_bounds(self, instance: str | None, size: int) -> dict[str, dict[str, int]]:
        """tank/healer/dps {min,max} for a run: the raid's desired comp when it has one (scaled if the roster
        size differs from the raid's), else the generic comp_rules scaled to the size."""
        from .roster.solver import scaled_role_bounds

        rd = self.raid_def(instance)
        comp = rd.get("comp")
        if comp and comp.get("tank") and comp.get("healer"):
            f = size / int(rd.get("size") or size or 1) if rd.get("size") else 1.0
            out = {}
            for role in ("tank", "healer", "dps"):
                b = comp.get(role) or {}
                lo, hi = int(b.get("min", 0)), int(b.get("max", size))
                out[role] = {"min": max(0, round(lo * f)), "max": max(1, round(hi * f))}
            return out
        b = scaled_role_bounds(self.profile.comp_rules, size)
        return {"tank": b["tank"], "healer": b["healer"], "dps": {"min": 0, "max": size}}

    def schedules(self, instance: str | None) -> list[dict]:
        """The raid's effective schedules (see schedules_of)."""
        return schedules_of(self.raid_def(instance))

    def schedule_def(self, instance: str | None, sid: str | None = None) -> dict:
        """The raid definition as schedule `sid` sees it (see schedule_def_of)."""
        return schedule_def_of(self.raid_def(instance), sid)

    @_config_verb(_raid_section)
    def set_schedule(self, instance: str, sid: str, fields: dict, by: str) -> list[str]:
        """Change (or create) one schedule of a raid: `fields` = {setting: value} from SCHEDULE_FIELDS, a cadence value
        of None / 'inherit' going back to the raid's. A new id needs what it is first — a kind, run times, or lockout
        days and a time. One set_raid_overrides call: validated as a whole, one commit, a refusal leaves config as it was."""
        import copy

        if instance not in self.profile.raids:
            raise RegistryError(f"unknown raid {instance}; known: {', '.join(self.profile.raids)}")
        sid = str(sid or "").strip().lower()
        if not fields:
            return []
        bad = [f for f in fields if f not in SCHEDULE_FIELDS]
        if bad:
            raise RegistryError(f"unknown schedule setting {bad[0]} (one of {', '.join(SCHEDULE_FIELDS)})")
        rd = self.raid_def(instance)
        stored = copy.deepcopy([s for s in (rd.get("schedules") or []) if isinstance(s, dict)])
        entry = next((s for s in stored if s.get("id") == sid), None)
        before = next((s for s in schedules_of(rd) if s["id"] == sid), None)
        if before is None and entry is None:
            kind = fields.get("kind") or ("weekly" if "slots" in fields else "lockout" if ("days" in fields or "time" in fields) else None)
            if not kind:
                raise RegistryError(f"{rd.get('name', instance)} has no schedule {sid or '?'}; start it with its kind (weekly, lockout or pickup) or its run times")
            entry = {"id": sid, "kind": kind} if sid != DEFAULT_SCHEDULE else {"id": sid}
            stored.append(entry)
        want: dict = {}
        for f, v in fields.items():
            if f == "slots" and sid == DEFAULT_SCHEDULE:
                want["slots"] = v  # the default schedule's run times ARE the raid's slots
                continue
            if entry is None:
                entry = {"id": sid}
                stored.append(entry)
            if f == "weights" and isinstance(v, dict):  # per key: a key set to inherit / None drops back to the default
                cur = dict(entry.get("weights") or {})
                for k, val in v.items():
                    k = str(k).strip().lower().removeprefix("weight_")
                    if val is None or str(val).strip().lower() in INHERIT_WORDS:
                        cur.pop(k, None)
                    else:
                        cur.update(schedule_weights({k: val}) or {})
                v = cur or None
            nv = schedule_value(f, v)
            if nv is None:
                entry.pop(f, None)
            else:
                entry[f] = nv
        want["schedules"] = stored
        self.set_raid_overrides(instance, want, by, message=f"raid {instance}: schedule {sid} {', '.join(fields)} (by {by})")
        after = next((s for s in self.schedules(instance) if s["id"] == sid), None) or {"id": sid}
        return [f"{rd.get('name', instance)} · {after.get('name') or sid}: " + ("new schedule, " if before is None else "") + self.schedule_label(instance, after)]

    def schedule_regulars(self, instance: str, sid: str) -> list[tuple[Member, RegisteredCharacter]]:
        """Everyone holding the series "<raid>:<schedule>" on any character (retired ones too) — what removing the
        schedule takes off (D30), by display name."""
        key = series_key(instance, str(sid or "").strip().lower())
        out = [(m, c) for m in self.members.values() for c in m.characters if key in c.rosters]
        return sorted(out, key=lambda mc: mc[0].display_name.lower())

    def schedule_runs_open(self, instance: str, sid: str, rs=None) -> list[dict]:
        """The run dicts (config.rosters) of the live runs a schedule opened — test runs left out. `rs` = the run store
        that says which runs are still live (read from the store when not given)."""
        sid = str(sid or "").strip().lower() or DEFAULT_SCHEDULE
        live = {ev.team for ev in self._live_runs(rs)}
        return [t for t in self.config.rosters if t.get("instance") == instance and str(t.get("schedule_id") or DEFAULT_SCHEDULE) == sid
                and not t.get("test") and t.get("key") in live]

    def schedule_removal_effects(self, instance: str, sid: str, rs=None) -> Effects:
        """Effects(regulars, open_runs) of removing a raid's schedule — what remove_schedule will do, computed before it
        does it, in code (see Effects). Unknown raid or schedule → RegistryError (the verb's own words)."""
        if instance not in self.profile.raids:
            raise RegistryError(f"unknown raid {instance}; known: {', '.join(self.profile.raids)}")
        sid = str(sid or "").strip().lower()
        s = next((x for x in self.schedules(instance) if x["id"] == sid), None)
        if s is None:
            rd = self.raid_def(instance)
            raise RegistryError(f"{rd.get('name', instance)} has no schedule {sid or '?'} (it has {', '.join(x['id'] for x in schedules_of(rd)) or 'none'})")
        regs = sorted({m.display_name for m, _c in self.schedule_regulars(instance, sid)}, key=str.lower)
        runs = [str(t.get("name") or t.get("key")) for t in self.schedule_runs_open(instance, sid, rs)]
        # what the open runs' lock reads once the schedule is gone (rc.run_weights: the class's fallback; the series'
        # regulars come off with it) — named so nobody expects the schedule's own weights or its regulars on them
        loses: list[str] = []
        if runs:
            adhoc = schedule_class(s.get("kind")) == "adhoc"
            if s.get("weights") and not adhoc:
                loses.append("the raid's weights")
            elif s.get("weights"):
                loses.append("the ad hoc weights")
            if regs and not adhoc:
                loses.append("no regulars")
        return Effects(name=s.get("name") or sid, label=self.schedule_label(instance, s), regulars=regs, open_runs=runs, loses=loses)

    @_config_verb(_raid_section)
    def remove_schedule(self, instance: str, sid: str, by: str) -> str:
        """Drop one schedule. `default` is the raid's own run times: removing it clears the raid's slots. Runs already
        opened from it keep their times. Its REGULARS come off with it (owner decision D30 — no orphans, and no DM to
        them: the DM rule); the line names them. One commit for the schedule and the members."""
        import copy

        if instance not in self.profile.raids:
            raise RegistryError(f"unknown raid {instance}; known: {', '.join(self.profile.raids)}")
        sid = str(sid or "").strip().lower()
        rd = self.raid_def(instance)
        have = next((s for s in schedules_of(rd) if s["id"] == sid), None)
        if have is None:
            raise RegistryError(f"{rd.get('name', instance)} has no schedule {sid or '?'} (it has {', '.join(s['id'] for s in schedules_of(rd)) or 'none'})")
        stored = [copy.deepcopy(s) for s in (rd.get("schedules") or []) if isinstance(s, dict) and s.get("id") != sid]
        want: dict = {"schedules": stored}
        if sid == DEFAULT_SCHEDULE:
            want["slots"] = []
        key = series_key(instance, sid)
        held = self.schedule_regulars(instance, sid) if not getattr(self, "_shadow", False) else []
        label = self.schedule_label(instance, have)  # read before the override is written
        with self.store.batch(f"{self.key}: raid {instance}: schedule {sid} removed (by {by})"):
            self.set_raid_overrides(instance, want, by, message=f"raid {instance}: schedule {sid} removed (by {by})")
            gone: list[str] = []
            for m in {mm.discord_id: mm for mm, _c in held}.values():
                for c in m.characters:
                    if key in c.rosters:
                        c.rosters.remove(key)
                # a notice, never a DM (D47 = no): the Me page and /me view say it — in the same member save
                self.notify(m.discord_id, "schedule_removed", f"{label} was removed by {by}: no more sheets come from it, so nobody expects you on them.", save=False)
                self.save(m, f"{by}: {m.display_name} off the regulars of {key} (schedule removed)")
                gone.append(m.display_name)
        return (f"{rd.get('name', instance)}: schedule {have.get('name') or sid} removed"
                + (f" — {len(gone)} regular{'s' if len(gone) != 1 else ''} off it ({join_words(gone)})" if gone else ""))

    @_config_verb(_raid_section)
    def set_raid_override(self, instance: str, field: str, value, by: str) -> str:
        """Owner override for a raid (validated): a refused value never stays in memory — the override dict is
        restored, so the next save_config can't commit it."""
        import copy

        before = copy.deepcopy(self.config.raids.get(instance))
        try:
            return self._apply_raid_override(instance, field, value, by)
        except RegistryError:
            if before is None:
                self.config.raids.pop(instance, None)
            else:
                self.config.raids[instance] = before
            raise

    @_config_verb(_raid_section)
    def set_raid_overrides(self, instance: str, fields: dict, by: str, message: str | None = None) -> list[str]:
        """Several raid fields as ONE change: every field is applied, the cross-field rules are checked once on the
        result, and it is saved in a single commit. A refused value leaves the stored config exactly as it was — no
        field commits on its own. This is what every multi-field editor must call (the Discord wizard, the web Raids
        page); calling set_raid_override in a loop commits the first fields and then fails on a later one against the
        STORED values, leaving a half-applied raid in git."""
        import copy

        if not fields:
            return []
        before = copy.deepcopy(self.config.raids.get(instance))
        try:
            lines = [self._apply_raid_override(instance, f, v, by, commit=False) for f, v in fields.items()]
            self._check_raid(instance)
        except RegistryError:
            if before is None:
                self.config.raids.pop(instance, None)
            else:
                self.config.raids[instance] = before
            raise
        self.save_config(message or f"raid {instance}: {', '.join(fields)} (by {by})")
        return lines

    def comp_slot(self, slot: str) -> str:
        """A comp target's slot as stored: a role (tank, healer, melee, ranged), a class ('Paladin') or a spec
        ('Paladin:Holy'); anything else → RegistryError."""
        slot = str(slot or "").strip()
        if slot in ROLES:
            return slot
        cls, _, spec = slot.partition(":")
        if cls not in self.profile.classes or (spec and spec not in self.profile.classes[cls]):
            raise RegistryError(f"unknown comp slot '{slot}' (a role, a class or Class:Spec)")
        return slot

    def comp_slot_words(self, slot: str) -> str:
        """A slot in words: 'healer' → 'Healers', 'Paladin' → 'Paladins', 'Paladin:Holy' → 'Holy Paladins'."""
        if slot in ROLES:
            return COMP_ROLE_WORDS[slot][1].capitalize()
        cls, _, spec = slot.partition(":")
        return f"{spec} {cls}s" if spec else f"{cls}s"

    @_config_verb(_raid_section)
    def set_comp_targets(self, raid: str, targets: dict, by: str) -> list[str]:
        """Officers' comp TARGETS ("we aim for", what readiness measures — dual interfaces §3.3 Cluster C, RD-12) for a
        raid (its runs inherit them) or one run (its key): {slot: {min, max, note} | None} — None removes the target so
        the derived value applies again; a target is the whole entry (min, an optional max, an optional reason).
        Bounds ("at least / at most" the solver seats) stay the owner's (raid_set); a target outside them is refused
        with the owner pointer. Every slot is validated before anything changes; one save_config, one commit."""
        key = str(raid or "").strip()
        run = self.config.roster(key)
        if run is None and key not in self.profile.raids:
            raise RegistryError(f"no raid or run {key or '?'}")
        instance = (run or {}).get("instance") or key
        size = int((run or {}).get("size") or self.raid_def(instance).get("size") or DEFAULT_RAID_SIZE)
        bounds = self.role_bounds(instance, size)
        holder = run if run is not None else (self.config.raids.get(key) or {})
        cur = {k: dict(v) for k, v in (holder.get("comp_targets") or {}).items()}
        lines = []
        for raw, t in (targets or {}).items():
            slot = self.comp_slot(raw)
            words = self.comp_slot_words(slot)
            if t is None:
                if slot in cur:
                    cur.pop(slot)
                    lines.append(f"{words}: back to the default aim")
                continue
            if not isinstance(t, dict):
                raise RegistryError(f"{words}: an aim is {{min, max, note}}")
            try:
                lo = int(t.get("min") or 0)
                hi = None if t.get("max") in (None, "") else int(t["max"])
            except (TypeError, ValueError):
                raise RegistryError(f"{words}: at least / at most are whole numbers") from None
            note = str(t.get("note") or "").strip() or None
            if lo < 0 or (hi is not None and hi < 0):
                raise RegistryError(f"{words}: an aim can't be negative")
            if hi is not None and hi < lo:
                raise RegistryError(f"{words}: at most {hi} is below at least {lo}")
            if lo > size:
                raise RegistryError(f"{words}: at least {lo} but a run seats {size}")
            role = slot if slot in ("tank", "healer") else ("dps" if slot in ("melee", "ranged") else None)
            if role and role in bounds:
                b = bounds[role]
                if lo > int(b["max"]):
                    raise RegistryError(f"at least {_comp_words(slot, lo)} but the raid allows at most {int(b['max'])} — change the limits first (owner)")
                if hi is not None and slot == role and hi < int(b["min"]):
                    raise RegistryError(f"at most {_comp_words(slot, hi)} but the raid takes at least {int(b['min'])} — change the limits first (owner)")
            if note and len(note) > COMP_NOTE_MAX:
                raise RegistryError(f"{words}: the reason holds {COMP_NOTE_MAX} characters ({len(note)} given)")
            entry = {"min": lo, **({"max": hi} if hi is not None else {}), **({"note": note} if note else {})}
            if cur.get(slot) == entry:
                continue
            cur[slot] = entry
            lines.append(f"{words}: at least {lo}" + (f", at most {hi}" if hi is not None else "") + (f" ({note})" if note else ""))
        if not lines:
            return []
        if run is None:
            holder = self.config.raids.setdefault(key, {})
        if cur:
            holder["comp_targets"] = cur
        else:
            holder.pop("comp_targets", None)
            if run is None and not holder:
                self.config.raids.pop(key, None)
        name = (run or {}).get("name") or self.raid_def(instance).get("name", instance)
        self.save_config(f"{key} aims: {'; '.join(lines)} (by {by})"[:400])
        return [f"{name} · {ln}" for ln in lines]

    def _check_raid(self, instance: str) -> None:
        """The cross-field rules, on the merged result (profile → guild override → defaults), never on one field at a
        time: that way a change that is only valid as a whole (lead and nudge raised together) is accepted."""
        eff = self.raid_def(instance)
        if eff["lock_hours_before"] < eff["confirm_hours_before"]:
            raise RegistryError("lock must come before the confirmation deadline (lock_hours_before ≥ confirm_hours_before)")
        if eff["signup_lead_hours"] <= eff["lock_hours_before"]:
            raise RegistryError("signups must open before they lock (signup_lead_hours > lock_hours_before)")
        if not (eff["lock_hours_before"] <= float(eff["nudge_hours_before"]) <= eff["signup_lead_hours"]):
            raise RegistryError("the nudge must fall between signup opening and lock (lock_hours_before ≤ nudge_hours_before ≤ signup_lead_hours)")
        for role, b in (eff.get("comp") or {}).items():
            if b.get("min", 0) > b.get("max", 999):
                raise RegistryError(f"{role}: min {b['min']} above max {b['max']}")
        self._check_schedules(eff)

    def _check_schedules(self, eff: dict) -> None:
        """Every schedule on the merged raid: unique ids, the cadence ordering with its overrides, slots for a weekly
        one, first_open + days within the lockout for a lockout one, 1–4 rosters."""
        scheds = schedules_of(eff)
        ids = [s["id"] for s in scheds]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise RegistryError(f"schedule id {dup[0]} is used twice")
        lockout = int(eff.get("lockout_days") or 7)
        for s in scheds:
            name = s.get("name") or s["id"]
            se = schedule_def_of(eff, s["id"])
            if se["lock_hours_before"] < se["confirm_hours_before"]:
                raise RegistryError(f"{name}: lock must come before the confirmation deadline (lock_hours_before ≥ confirm_hours_before)")
            if se["signup_lead_hours"] <= se["lock_hours_before"]:
                raise RegistryError(f"{name}: signups must open before they lock (signup_lead_hours > lock_hours_before)")
            if not (se["lock_hours_before"] <= float(se["nudge_hours_before"]) <= se["signup_lead_hours"]):
                raise RegistryError(f"{name}: the nudge must fall between signup opening and lock (lock_hours_before ≤ nudge_hours_before ≤ signup_lead_hours)")
            if not 1 <= int(s.get("rosters") or 1) <= MAX_SCHEDULE_ROSTERS:
                raise RegistryError(f"{name}: a run holds 1 to {MAX_SCHEDULE_ROSTERS} rosters")
            if s["kind"] == "weekly" and s["id"] != DEFAULT_SCHEDULE and not s.get("slots"):
                raise RegistryError(f"{name}: a weekly schedule needs at least one run time")
            if s["kind"] == "lockout":
                if not eff.get("first_open"):
                    raise RegistryError(f"{name}: a lockout schedule counts from the raid's first opening; set that first")
                if not s.get("days") or not s.get("time"):
                    raise RegistryError(f"{name}: a lockout schedule needs its day(s) and a start time")
                if max(s["days"]) > lockout:
                    raise RegistryError(f"{name}: day {max(s['days'])} is past the end of a {lockout}-day lockout")

    def _apply_raid_override(self, instance: str, field: str, value, by: str, commit: bool = True) -> str:
        """Owner override for a raid: lockout_days | duration_hours | first_open | notes | auto | tank_min/max | healer_min/max | dps_min/max
        | slots | signup_lead_hours | lock_hours_before | confirm_hours_before | nudge_hours_before | fill_ask_hours | split_policy
        | nudge / autofill / open_dm (true|false) | weight_rank/main/sat_out/signup_order | schedules (the whole list)."""
        if instance not in self.profile.raids:
            raise RegistryError(f"unknown raid {instance}; known: {', '.join(self.profile.raids)}")
        over = self.config.raids.setdefault(instance, {})
        if field == "slots":
            over["slots"] = parse_slots(value)
            value = ", ".join(self.slot_label(x) for x in over["slots"]) or "no run times"  # the reply and change log read 12-hour
        elif field == "schedules":  # the whole stored list (the wizard's and set_schedule's one write); `default`'s slots go to the raid
            if not isinstance(value, list):
                raise RegistryError("schedules must be a list of schedules")
            rows = []
            for row in value:
                e = norm_schedule(row)
                if e["id"] == DEFAULT_SCHEDULE:
                    if "slots" in e:
                        over["slots"] = e.pop("slots")
                    if len(e) == 1:
                        continue  # nothing of its own left: the raid's slots are the whole of it
                rows.append(e)
            if rows:
                over["schedules"] = rows
            else:
                over.pop("schedules", None)
            value = ", ".join(e["id"] for e in rows) or "no schedules of its own"
        elif field in RAID_HOURS_FIELDS:
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise RegistryError(f"{field} must be a number of hours")
            if num < 0:
                raise RegistryError(f"{field} can't be negative")
            over[field] = int(num) if num == int(num) else num
        elif field in RAID_BOOL_FIELDS or field in RAID_LOOT_FIELDS:
            over[field] = parse_bool(value)
        elif field == "split_policy":
            if value not in SPLIT_POLICIES:
                raise RegistryError(f"split_policy must be one of {', '.join(SPLIT_POLICIES)}")
            over["split_policy"] = value
        elif field.startswith("weight_") and field[7:] in RAID_WEIGHT_DEFAULTS:
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise RegistryError(f"{field} must be a whole number")
            if n < 0:
                raise RegistryError(f"{field} can't be negative")
            over.setdefault("weights", {})[field[7:]] = n
        elif field in ("lockout_days", "duration_hours"):
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise RegistryError(f"{field} must be a number")
            if num <= 0:
                raise RegistryError(f"{field} must be positive")
            over[field] = int(num) if field == "lockout_days" else num
        elif field == "notes":
            over["notes"] = str(value or "").strip()
        elif field == "auto":
            over["auto"] = str(value).lower() in ("true", "yes", "on", "1")
        elif field == "first_open":
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _Z

            raw = str(value or "").strip().replace(" ", "T", 1) if value else ""
            if not raw:
                over.pop("first_open", None)
            else:
                try:
                    t = _dt.fromisoformat(raw)
                except ValueError:
                    raise RegistryError("first_open must be a date and a time in the guild timezone (pick it on the Raids page)")
                if t.tzinfo is None:
                    t = t.replace(tzinfo=_Z(self.config.timezone))
                over["first_open"] = t.isoformat()
            value = self.local12(over["first_open"]) if over.get("first_open") else "no first lockout set — weekly runs start at the next run time; a day-of-lockout schedule needs one"  # stored ISO, shown in words
        elif field in ("tank_min", "tank_max", "healer_min", "healer_max", "dps_min", "dps_max"):
            role, bound = field.split("_")
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise RegistryError(f"{field} must be a whole number")
            if n < 0:
                raise RegistryError(f"{field} can't be negative")
            over.setdefault("comp", {}).setdefault(role, {})[bound] = n
        else:
            raise RegistryError(f"unknown raid field {field}")
        if commit:
            self._check_raid(instance)
            self.save_config(f"raid {instance} {field} → {value} (by {by})")
        return f"{instance}: {field} = {value}"

    # ---- test bench: fake members the bot puppets so the whole cycle can be rehearsed in Discord
    TEST_BASE = 900_000_000_000_000_000  # fake Discord ids start here (far above any real snowflake in use)
    TEST_NAMES = ["Ashvane", "Brightmoor", "Cinderfall", "Duskbane", "Emberlyn", "Fenwick", "Glimmer", "Hollowell", "Ironbell", "Juniper", "Kestrel", "Lorath", "Marrow", "Nightsong", "Oakenshield", "Pemberly", "Quillon", "Rookwood", "Sablewind", "Thornfield", "Umbra", "Valeria", "Wrenna", "Xanthe", "Yarrow", "Zephyrine", "Alderic", "Briarwood", "Coalbrook", "Dunmore"]
    TEST_MIX = [("Warrior", "Protection", "core"), ("Druid", "Guardian", "raider"), ("Paladin", "Holy", "raider"), ("Priest", "Holy", "trial"), ("Shaman", "Restoration", "trial"), ("Druid", "Restoration", "trial"), ("Priest", "Discipline", "core"),
                ("Rogue", "Combat", "core"), ("Warrior", "Fury", "raider"), ("Shaman", "Enhancement", "raider"), ("Paladin", "Retribution", "trial"), ("Druid", "Feral", "trial"), ("Hunter", "Marksmanship", "raider"), ("Hunter", "BeastMastery", "trial"),
                ("Mage", "Fire", "core"), ("Warlock", "Destruction", "raider"), ("Shaman", "Elemental", "trial"), ("Druid", "Balance", "trial"), ("Priest", "Shadow", "raider"), ("Mage", "Frost", "trial"), ("Warlock", "Affliction", "trial"), ("Rogue", "Assassination", "trial"), ("Warrior", "Protection", "trial"), ("Paladin", "Protection", "raider"), ("Priest", "Holy", "trial"), ("Mage", "Arcane", "trial"), ("Hunter", "Survival", "trial"), ("Warlock", "Demonology", "trial"), ("Rogue", "Subtlety", "trial"), ("Shaman", "Restoration", "raider")]

    def test_members(self) -> list[Member]:
        return [m for m in self.members.values() if m.test]

    def test_roster(self) -> list[dict]:
        """The shape of the test bench. `<guild>/test_roster.yaml` in the PRIVATE data repo wins when it exists: a list of
        {name, character?, cls, spec, rank?} so a guild can rehearse against its own real signup shape. Real names must
        never be hard-coded here — this repo is public — so the built-in fallback is the invented TEST_NAMES/TEST_MIX."""
        p = self.store.root / self.key / "test_roster.yaml"
        if p.exists():
            rows = yaml.safe_load(p.read_text()) or []
            return [r for r in rows if isinstance(r, dict) and r.get("name") and r.get("cls")]
        return [{"name": n, "cls": c, "spec": sp, "rank": rk}
                for n, (c, sp, rk) in zip(self.TEST_NAMES, [self.TEST_MIX[i % len(self.TEST_MIX)] for i in range(len(self.TEST_NAMES))])]

    @_member_write
    def seed_test_members(self, count: int, by: str) -> list[Member]:
        """Create `count` test members from `test_roster()` (idempotent: existing ones are kept)."""
        roster = self.test_roster()
        count = max(1, min(count, len(roster)))
        made = []
        with self.store.batch(f"{self.key}: test bench seeded by {by}"):
            for i in range(count):
                uid = self.TEST_BASE + i
                if uid in self.members:
                    continue
                row = roster[i]
                name, cls, spec, rank = row["name"], row["cls"], row.get("spec") or "", row.get("rank") or "trial"
                if spec not in self.profile.classes.get(cls, {}):
                    spec = next(iter(self.profile.classes[cls]))
                # a Discord display name may hold spaces, digits and punctuation; a character name may not, and the
                # roster file is hand-edited, so derive a legal one rather than letting the whole seed raise
                character = row.get("character") or re.sub(r"[^A-Za-z]", "", name)[:12] or f"Puppet{i:02d}"
                m, c = self.add_character(uid, name, character, cls, spec, None, True)
                m.test = True
                c.rank = rank
                c.confirmed_by = by
                self.save(m, f"test member {name} seeded (by {by})")
                made.append(m)
        return made

    TEST_COMPOSED_BASE = TEST_BASE + 1_000  # composed puppets (add_test_members) never take a seed_test_members id
    TEST_MAX = 80  # enough for two 40-player benches; a guard against a typo like 400
    _PUPPET_HEADS = ["Ash", "Bright", "Cinder", "Dusk", "Ember", "Fen", "Glim", "Hollow", "Iron", "Juni", "Kes", "Lor", "Mar", "Night", "Oak",
                     "Pem", "Quil", "Rook", "Sable", "Thorn", "Um", "Val", "Wren", "Yar", "Zeph", "Alder", "Briar", "Coal", "Dun", "Elder"]
    _PUPPET_TAILS = ["vane", "moor", "fall", "bane", "lyn", "wick", "mere", "well", "bell", "per", "trel", "ath", "row", "song", "shield",
                     "ley", "quin", "wood", "wind", "field"]

    def _puppet_names(self):
        """Invented, legal, unused names (letters only, ≤12): the built-in test names first, then head+tail pairs."""
        taken = {m.display_name.lower() for m in self.members.values()} | {(c.name or "").lower() for m in self.members.values() for c in m.characters}
        for n in [*self.TEST_NAMES, *(h + t for t in self._PUPPET_TAILS for h in self._PUPPET_HEADS)]:
            if n.lower() not in taken and 2 <= len(n) <= 12:
                taken.add(n.lower())
                yield n

    @_member_write
    def add_test_members(self, rows: list[dict], by: str) -> list[Member]:
        """Puppets by composition: rows of {cls, spec, offspec?, count}. Checked in full before anything is written
        (the profile's classes and specs, the TEST_MAX cap); then one commit for the lot. Mains, rank raider, confirmed."""
        want = []
        for r in rows:
            cls, spec, off, n = str(r.get("cls") or ""), str(r.get("spec") or ""), r.get("offspec") or None, int(r.get("count") or 0)
            if cls not in self.profile.classes:
                raise RegistryError(f"{cls or '?'} isn't a class in this game version.")
            if spec not in self.profile.classes[cls]:
                raise RegistryError(f"{cls} has no spec called {spec or '?'}.")
            if off is not None and (off not in self.profile.classes[cls] or off == spec):
                raise RegistryError(f"{off} isn't another {cls} spec.")
            if n < 0:
                raise RegistryError("A count can't be negative.")
            want += [(cls, spec, off)] * n
        have = len(self.test_members())
        if have + len(want) > self.TEST_MAX:
            raise RegistryError(f"That would make {have + len(want)} test members; the bench holds {self.TEST_MAX}.")
        names = self._puppet_names()
        free = (uid for uid in range(self.TEST_COMPOSED_BASE, self.TEST_COMPOSED_BASE + 10_000) if uid not in self.members)
        made = []
        with self.store.batch(f"{self.key}: {len(want)} test members added by {by}"):
            for cls, spec, off in want:
                name, uid = next(names), next(free)
                m, c = self.add_character(uid, name, name, cls, spec, off, True)
                m.test, c.rank, c.confirmed_by = True, "raider", by
                self.save(m, f"test member {name} added ({cls} {spec}, by {by})")
                made.append(m)
        return made

    def remove_test_members(self, uids: list[int], by: str) -> int:
        """Remove the chosen puppets (real members are refused, never skipped silently): one commit for the lot."""
        ms = [self.members.get(int(u)) for u in uids]
        bad = [str(u) for u, m in zip(uids, ms) if m is None or not m.test]
        if bad:
            raise RegistryError("Only test members can be removed here.")
        with self.store.batch(f"{self.key}: {len(ms)} test members removed by {by}"):
            for m in ms:
                path = self.store.root / self.key / "members" / f"{m.discord_id}.json"
                del self.members[m.discord_id]
                self._snap.pop(m.discord_id, None)
                if path.exists():
                    path.unlink()
                    self.store.commit(f"{self.key}: test member {m.display_name} removed (by {by})")
        if ms:
            self._notify("member", [f"test bench: {len(ms)} test members removed (by {by})"])
        return len(ms)

    def clear_test_members(self, by: str) -> int:
        """Remove every test member (their placements go with them): one commit for the lot."""
        gone = 0
        with self.store.batch(f"{self.key}: test bench cleared by {by}"):
            for m in list(self.members.values()):
                if not m.test:
                    continue
                path = self.store.root / self.key / "members" / f"{m.discord_id}.json"
                del self.members[m.discord_id]
                self._snap.pop(m.discord_id, None)
                if path.exists():
                    path.unlink()
                    self.store.commit(f"{self.key}: test member {m.display_name} removed (by {by})")
                gone += 1
        if gone:
            self._notify("member", [f"test bench: {gone} test members removed (by {by})"])
        return gone

    # ---- auras: what the guild learns about the game's buffs (stacking families, scope, who benefits)
    BUFF_SCOPES = ("party", "raid", "class", "self")
    STATUSES = ("confirmed", "reported", "assumed")
    VALUE_KEYS = ("all", "physical", "spell", "mana", "melee", "ranged", "healer", "tank")

    def buff(self, bid: str):
        b = next((x for x in self.profile.buffs if x.id == bid), None)
        if b is None:
            raise RegistryError(f"unknown buff {bid}; known: {', '.join(x.id for x in self.profile.buffs)}")
        return b

    def _value_key(self, key: str) -> str:
        k = key.strip()
        if k in self.VALUE_KEYS:
            return k
        if k.lower().startswith("spec:"):
            spec = k[5:].strip()
            for cls, specs in self.profile.classes.items():
                for sname in specs:
                    if sname.lower() == spec.lower():
                        return f"spec:{sname}"
            raise RegistryError(f"unknown spec {spec}")
        raise RegistryError(f"benefit key {key!r} must be one of {', '.join(self.VALUE_KEYS)} or spec:<Name>")

    BUFF_FIELDS = ("scope", "family", "strength", "status", "note")
    FAMILY_FIELDS = ("name", "status", "note", "value")  # plus value:<key> (one beneficiary)

    def _buff_field(self, bid: str, over: dict, field: str, value) -> str:
        """One buff field validated into `over` (the buff's override dict); nothing saved. Returns the reply line."""
        if field == "scope":
            if value not in self.BUFF_SCOPES:
                raise RegistryError(f"scope must be one of {', '.join(self.BUFF_SCOPES)}")
            over["scope"] = value
        elif field == "family":
            fid = str(value or "").strip().lower().replace(" ", "_")
            if fid in ("", "own", "none", "self"):
                over["family"] = bid
            else:
                if fid not in self.profile.families and fid not in {x.id for x in self.profile.buffs}:
                    raise RegistryError(f"unknown family {fid}; known: {', '.join(sorted(self.profile.families))} — or create it first")
                over["family"] = fid
        elif field == "strength":
            try:
                n = float(value)
            except (TypeError, ValueError):
                raise RegistryError("strength must be a number (1 = the family's full value)")
            if n < 0:
                raise RegistryError("strength can't be negative")
            over["strength"] = n
        elif field == "status":
            if value not in self.STATUSES:
                raise RegistryError(f"status must be one of {', '.join(self.STATUSES)}")
            over["status"] = value
        elif field == "note":
            over["note"] = str(value or "").strip()
        else:
            raise RegistryError(f"unknown buff field {field} (one of {', '.join(self.BUFF_FIELDS)})")
        return f"{bid}: {field} = {value}"

    @_config_verb("buffs")
    def set_buff_override(self, bid: str, field: str, value, by: str) -> str:
        """scope | family (a family id, another buff's id, or 'own' to stand alone) | strength | status | note."""
        return self.set_buff_overrides(bid, {field: value}, by)[0]

    @_config_verb("buffs")
    def set_buff_overrides(self, bid: str, fields: dict, by: str) -> list[str]:
        """Several fields of ONE buff as one change (the Auras page's row, the AuraWizard's Save this buff, a structured
        batch's coalesced aura_set ops): every field validated, one save_config, one commit. A refused field leaves the
        buff exactly as it was — the set_raid_overrides pattern, so a half-saved buff never reaches git."""
        import copy

        self.buff(bid)
        if not fields:
            return []
        before = copy.deepcopy(self.config.buffs.get(bid))
        try:
            over = self.config.buffs.setdefault(bid, {})
            lines = [self._buff_field(bid, over, f, v) for f, v in fields.items()]
        except RegistryError:
            if before is None:
                self.config.buffs.pop(bid, None)
            else:
                self.config.buffs[bid] = before
            raise
        self._prune_buff_defaults(bid)
        self.save_config(f"aura {bid} " + ", ".join(f"{f} → {v}" for f, v in fields.items()) + f" (by {by})")
        return lines

    def _prune_buff_defaults(self, bid: str) -> None:
        """A field written back to the game's own value is no override: amber on the Auras page means "differs from the
        game", and the reset sentence counts only real changes. Seen live on 2026-10-24: a plain-text change set Moonkin
        Aura's family to the family the profile already gave it, and the row went amber for nothing."""
        over = self.config.buffs.get(bid)
        base = next((b for b in self.base_profile.buffs if b.id == bid), None)
        if not over or base is None:
            return
        defaults = {"scope": base.scope, "family": base.family_id, "strength": float(base.strength), "status": base.status, "note": base.note}
        for f, v in list(over.items()):
            if f in defaults and v == defaults[f]:
                over.pop(f)
        if not over:
            self.config.buffs.pop(bid, None)

    def _family_field(self, fid: str, over: dict, field: str, value) -> str:
        """One family field validated into `over` (the family's override dict); nothing saved."""
        if field == "name":
            over["name"] = str(value or "").strip() or fid
        elif field == "status":
            if value not in self.STATUSES:
                raise RegistryError(f"status must be one of {', '.join(self.STATUSES)}")
            over["status"] = value
        elif field == "note":
            over["note"] = str(value or "").strip()
        elif field == "value":
            new: dict[str, float] = {}
            raw = value if isinstance(value, dict) else {kv.partition(":")[0]: kv.partition(":")[2] for kv in str(value or "").replace(";", ",").split(",") if kv.strip()}
            for k, v in raw.items():
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    raise RegistryError(f"{k}: {v!r} isn't a number")
                if n < 0:
                    raise RegistryError(f"{k}: can't be negative")
                new[self._value_key(str(k))] = n  # an explicit 0 on a spec key means "this spec gets nothing"
            over["value"] = new
        elif field.startswith("value:"):
            key = self._value_key(field[6:])
            if over.get("value") is not None:
                cur = dict(over["value"])
            elif fid in self.profile.families:
                cur = dict(self.profile.families[fid].value)
            else:  # a buff's own family that doesn't exist yet (the buff stands alone from now on): its own value map, as link_families builds it
                own = next((b for b in self.profile.buffs if b.id == fid), None)
                cur = dict(own.value or {}) if own is not None else {}
            try:
                n = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                raise RegistryError(f"{key}: {value!r} isn't a number")
            if n is not None and n < 0:
                raise RegistryError(f"{key}: can't be negative")
            if n is None:  # blank removes the entry
                cur.pop(key, None)
            elif n > 0 or key.startswith("spec:"):  # an explicit 0 on a spec key means "this spec gets nothing"
                cur[key] = n
            else:
                cur.pop(key, None)
            over["value"] = cur
        else:
            raise RegistryError(f"unknown family field {field} (one of {', '.join(self.FAMILY_FIELDS)}, value:<key>)")
        return f"family {fid}: {field} = {value}"

    @_config_verb("buffs")
    def set_family_override(self, fid: str, field: str, value, by: str) -> str:
        """name | status | note | value (whole map as 'all: 3, mana: 2') | value:<key> (one entry; 0 or blank removes it)."""
        return self.set_family_overrides(fid, {field: value}, by)[0]

    @_config_verb("buffs")
    def set_family_overrides(self, fid: str, fields: dict, by: str) -> list[str]:
        """Several fields of ONE stacking family as one change (a new family's name and beneficiaries together, the web's
        per-key deltas, the AuraWizard's draft): every field validated, one save_config; a refusal restores the family.
        An id that doesn't exist yet is created (named after itself unless `name` is given) — a new id comes from
        `family_slug(name)`, never typed."""
        import copy

        fid = str(fid or "").strip().lower().replace(" ", "_")
        if not fid:
            raise RegistryError("which family?")
        if not fields:
            return []
        before = copy.deepcopy(self.config.families.get(fid))
        try:
            over = self.config.families.setdefault(fid, {})
            if fid not in self.profile.families and "name" not in over and "name" not in fields:
                over["name"] = fid.replace("_", " ").replace("-", " ").title()
            lines = [self._family_field(fid, over, f, v) for f, v in fields.items()]
        except RegistryError:
            if before is None:
                self.config.families.pop(fid, None)
            else:
                self.config.families[fid] = before
            raise
        self.save_config(f"aura family {fid} " + ", ".join(f"{f} → {v}" for f, v in fields.items()) + f" (by {by})")
        return lines

    def family_slug(self, name: str) -> str:
        """A new family's id from its name (family ids are never typed — design §3.5 A-03): lower-case words joined by
        '_'; '-2', '-3', … when that id is already a family's or a buff's."""
        base = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:24].strip("_") or "family"
        taken = set(self.profile.families) | set(self.config.families) | {b.id for b in self.profile.buffs}
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def aura_override_counts(self) -> dict:
        """What 'Reset everything to game defaults' drops: the buffs and families the guild changed (names, in order)."""
        buffs = [b.short for b in self.profile.buffs if self.config.buffs.get(b.id)]
        fams = [self.profile.families[f].name if f in self.profile.families else f for f, o in self.config.families.items() if o]
        return {"buffs": len(buffs), "families": len(fams), "buff_names": buffs, "family_names": fams}

    def aura_reset_sentence(self) -> str:
        """The one sentence every surface prints before an aura reset-all (the wizard's confirm, the Auras page's review,
        MCP's preview, the plain-text diff)."""
        c = self.aura_override_counts()
        if not c["buffs"] and not c["families"]:
            return "Every aura already reads the game defaults, so there is nothing to reset."
        parts = [f"{c['buffs']} buff{'' if c['buffs'] == 1 else 's'}" if c["buffs"] else "",
                 f"{c['families']} famil{'y' if c['families'] == 1 else 'ies'}" if c["families"] else ""]
        names = [re.sub(r"\s*\(.*?\)", "", n) for n in c["buff_names"] + c["family_names"]]
        what = join_words(names) if len(names) <= 3 else " and ".join(p for p in parts if p)
        return (f"The guild drops its changes to {what}, families it declared included. "
                "From the next run on, the groups, cards and board use the game's defaults. A roster already locked stays as it is.")

    @_config_verb("buffs")
    def clear_aura_overrides(self, by: str, bid: str | None = None) -> str:
        if bid:
            self.config.buffs.pop(bid, None)
            self.config.families.pop(bid, None)
            self.save_config(f"aura {bid}: the guild's changes cleared (by {by})")
            return f"{bid}: back to the game defaults"
        self.config.buffs, self.config.families = {}, {}
        self.save_config(f"auras: the guild's changes cleared (by {by})")
        return "auras: back to the game defaults"

    @_config_verb(_raid_section)
    def clear_raid_override(self, instance: str, by: str) -> str:
        self.config.raids.pop(instance, None)
        self.save_config(f"raid {instance}: the guild's changes cleared (by {by})")
        return f"{instance}: back to profile defaults"

    def verification(self, discord_id: int) -> str:
        """unregistered | registered (planned/active character) | confirmed (an officer confirmed one)."""
        m = self.members.get(discord_id)
        if not m or not m.active():
            return "unregistered"
        return "confirmed" if any(c.confirmed_by for c in m.active()) else "registered"

    def may_ask(self, discord_id: int, is_officer: bool) -> bool:
        aud = self.config.ask_audience
        if is_officer or aud == "everyone":
            return True
        lvl = self.verification(discord_id)
        return (aud == "registered" and lvl in ("registered", "confirmed")) or (aud == "confirmed" and lvl == "confirmed")

    # ---- plain-text permissions (PLAIN_GROUPS / PlainPolicy; design §5.25)
    def plain_who(self, group: str) -> list[str]:
        """Who may use a capability group: the owner's list, or the group's default. An empty list = the owner only."""
        groups = self.config.plain.groups
        return list(groups[group]) if group in groups else list(PLAIN_GROUPS[group][1])

    def plain_mode(self, channel) -> str:
        """act | self | answer | ignore for a channel id (int or str), None = DMs, PLAIN_WEB = the site / MCP."""
        if channel == PLAIN_WEB:
            return "act"
        p = self.config.plain
        if channel is None:
            return p.dm
        listed = p.channels.get(str(channel))
        if listed:
            return listed
        if str(channel) in (str(self.config.ops_channel_id), str(self.config.analytics_channel_id)):
            return "act"
        return p.default

    def plain_act_channels(self) -> list[str]:
        """The channels where plain text acts on anything (explicitly listed, or ops/analytics by default)."""
        p = self.config.plain
        ids = [c for c, mode in p.channels.items() if mode == "act"]
        for cid in (self.config.ops_channel_id, self.config.analytics_channel_id):
            if cid and str(cid) not in p.channels and str(cid) not in ids:
                ids.append(str(cid))
        return ids

    def listen_mode(self, channel) -> str | None:
        """watch | reply for a channel the bot listens in (design §5.59), None = not listening (DMs never are)."""
        if channel is None or channel == PLAIN_WEB:
            return None
        return self.config.plain.listen.get(str(channel)) or None

    def listen_channels(self) -> dict[str, str]:
        """Every listening channel: channel id (string) → watch | reply."""
        return dict(self.config.plain.listen)

    def plain_allows(self, group: str, author_id: int, officer: bool, role_ids=()) -> bool:
        if self.is_owner_id(author_id):
            return True  # the owner can always do everything
        roles = {str(r) for r in role_ids}
        for tok in self.plain_who(group):
            if tok == "everyone" or (tok == "officers" and officer) or (tok == "registered" and self.verification(author_id) != "unregistered"):
                return True
            if tok.startswith("role:") and tok[5:] in roles:
                return True
        return False

    def plain_who_text(self, group: str) -> str:
        names = {"everyone": "everyone", "registered": "registered members", "officers": "officers", "owner": "the owner"}
        out = [names.get(t) or ("@" + self.role_names.get(int(t[5:]), f"role {t[5:]}") if t.startswith("role:") and t[5:].isdigit() else t) for t in self.plain_who(group)]
        return " or ".join(dict.fromkeys(out)) or "the owner"

    def may_plain(self, group: str, author_id: int, *, officer: bool, role_ids=(), channel=None, what: str | None = None, fallback: str | None = None) -> str | None:
        """THE plain-text permission check, used by every plain-text path (Discord @mention and DMs, /gm change, MCP
        plain_change). `group`: the op's capability group ("self" when it acts on the author's own record, see
        configops.bind_self); `fallback`: the group that would allow it on anyone (an officer's own absence passes as
        "members" even if "self" were closed to them); `channel`: channel id, None = DMs, PLAIN_WEB. Returns None when
        allowed, otherwise the refusal line: who may, and where."""
        what = what or PLAIN_GROUPS[group][0].lower()
        mode = self.plain_mode(channel)
        owner = self.is_owner_id(author_id)
        where = "in DMs" if channel is None else f"in <#{channel}>"
        act = self.plain_act_channels()
        go = ("use " + " or ".join(f"<#{c}>" for c in act)) if act else "use the slash commands or the site"
        if not (self.plain_allows(group, author_id, officer, role_ids) or (fallback and self.plain_allows(fallback, author_id, officer, role_ids))):
            hint = " — /register first" if group == "self" and "registered" in self.plain_who(group) and self.verification(author_id) == "unregistered" else ""
            return f"Only {self.plain_who_text(group)} can change {what}{hint}."  # who first: it holds in every channel
        if mode in ("ignore", "answer") and not owner:  # the owner can always do everything, anywhere the bot listens
            return f"Plain text only answers questions {where} — {go}."
        if mode == "self" and group != "self" and not owner:
            return f"Plain text can't change {what} {where} — {go}."
        return None

    def merge_plain_policy(self, patch: dict) -> dict:
        """The whole policy with `patch` laid over the current one — what set_plain_policy takes. Patch keys:
        `groups` {group: [who...]} (only the groups named change), `channels` (the WHOLE per-channel map, as the Ops page
        sends it), `set_channels` {channel id: mode | None} (merged into the current map; None = back to the default),
        `dm`, `default`, `listen` (the WHOLE listening map, §5.59) and `set_listen` {channel id: watch | reply | None}
        (None / "off" = stop listening there). Anything left out keeps its value. Shared by the web route, MCP and plain text."""
        cur = self.config.plain
        patch = patch or {}
        groups = {**{g: self.plain_who(g) for g in cur.groups}, **(patch.get("groups") or {})} if "groups" in patch else dict(cur.groups)
        channels = dict(patch["channels"]) if "channels" in patch else dict(cur.channels)
        for cid, mode in (patch.get("set_channels") or {}).items():
            key = str(cid).strip("<#>")
            if mode in (None, "", "clear", "default"):
                channels.pop(key, None)
            else:
                channels[key] = mode
        listen = {str(c).strip("<#>"): m for c, m in patch["listen"].items()} if "listen" in patch else dict(cur.listen)
        for cid, mode in (patch.get("set_listen") or {}).items():
            key = str(cid).strip("<#>")
            if mode in (None, "", "off", "stop", "clear"):
                listen.pop(key, None)
            else:
                listen[key] = mode
        return {"groups": groups, "channels": channels, "dm": patch.get("dm") or cur.dm, "default": patch.get("default") or cur.default,
                "listen": listen}

    @_config_verb("plain")
    def set_plain_policy(self, policy: dict, by: str) -> str:
        """Owner: replace the plain-text policy (validated: known groups, who tokens, modes). One commit."""
        p = PlainPolicy.model_validate(policy or {})
        bad = [g for g in p.groups if g not in PLAIN_GROUPS]
        if bad:
            raise RegistryError(f"unknown capability group {', '.join(bad)} (one of {', '.join(PLAIN_GROUPS)})")
        for g, who in p.groups.items():
            wrong = [t for t in who if t not in PLAIN_WHO and not (t.startswith("role:") and t[5:].isdigit())]
            if wrong:
                raise RegistryError(f"{g}: who is {', '.join(PLAIN_WHO)} or role:<id>, not {', '.join(wrong)}")
            p.groups[g] = list(dict.fromkeys(who))
        modes = {"dm": p.dm, "default": p.default, **{f"<#{c}>": m for c, m in p.channels.items()}}
        wrong = [f"{k}={m}" for k, m in modes.items() if m not in PLAIN_MODES]
        if wrong:
            raise RegistryError(f"channel mode is one of {', '.join(PLAIN_MODES)}, not {', '.join(wrong)}")
        if any(not c.isdigit() for c in p.channels):
            raise RegistryError("channels are keyed by channel id")
        if any(not str(c).isdigit() for c in p.listen):
            raise RegistryError("listening is set per channel, by channel id — DMs aren't a channel")
        wrong = [f"<#{c}>={m}" for c, m in p.listen.items() if m not in LISTEN_MODES]
        if wrong:
            raise RegistryError(f"listening is {' or '.join(LISTEN_MODES)}, not {', '.join(wrong)}")
        for c in p.listen:
            listed = p.channels.get(c)
            mode = listed or ("act" if c in (str(self.config.ops_channel_id), str(self.config.analytics_channel_id)) else p.default)
            if mode == "ignore":
                raise RegistryError(f"<#{c}> ignores plain text, so the bot can't listen there — set its plain-text mode first")
        # a group left at its default is not stored, so a changed default later reaches it
        p.groups = {g: w for g, w in p.groups.items() if w != PLAIN_GROUPS[g][1]}
        before = self.config.plain
        self.config.plain = p
        changed = [g for g in PLAIN_GROUPS if self.plain_who(g) != (list(before.groups[g]) if g in before.groups else PLAIN_GROUPS[g][1])]
        chans = sorted(set(p.channels.items()) ^ set(before.channels.items()))
        line = "plain-text permissions: " + ("; ".join(f"{PLAIN_GROUPS[g][0]} → {self.plain_who_text(g)}" for g in changed) or "groups unchanged") \
            + (f"; channels {len({c for c, _ in chans})} changed" if chans else "") + (f"; DMs {before.dm} → {p.dm}" if before.dm != p.dm else "") \
            + (f"; other channels {before.default} → {p.default}" if before.default != p.default else "") \
            + (f"; listening {len({c for c, _ in set(p.listen.items()) ^ set(before.listen.items())})} changed" if p.listen != before.listen else "")
        self.save_config(f"{line} (by {by})")
        return line

    def roles_of(self, m: Member) -> tuple[str | None, list[str]]:
        """(primary, flex). Primary always follows the main's current spec — a stored preference from an earlier
        plan must not linger after a re-spec. Stored preferences (/me plan roles) and the offspec's role are flex."""
        main = m.main
        primary = self.profile.spec(main.cls, main.spec).role if main else None
        flex = set(main.flex) if main else set()
        if main and main.offspec:
            try:
                r = self.profile.spec(main.cls, main.offspec).role
                if r != primary:
                    flex.add(r)
            except KeyError:
                pass
        flex.discard(primary)
        return primary, sorted(flex)

    def plan_summary(self) -> dict:
        """Class/spec/role distribution across planned+active mains, plus flex and buff providers."""
        mains = [(m, m.main) for m in self.members.values() if m.main]
        by_cls: dict[str, list[str]] = {}
        by_role: dict[str, int] = {r: 0 for r in ROLES}
        flex: dict[str, list[str]] = {r: [] for r in by_role}
        for m, c in mains:
            by_cls.setdefault(c.cls, []).append(f"{m.display_name} · {c.spec}" + (f"/{c.offspec}" if c.offspec else ""))
            role, fl = self.roles_of(m)
            by_role[role] = by_role.get(role, 0) + 1
            for f in fl:
                flex.setdefault(f, []).append(m.display_name)
        providers: dict[str, list[str]] = {}
        for b in self.profile.party_buffs():
            who = [m.display_name for m, c in mains if b.provided_by(self.profile.spec(c.cls, c.spec))]
            providers[b.name.split(" (")[0]] = who
        alts = [(m.display_name, f"{c.cls} {c.spec}") for m in self.members.values() for c in m.active() if not c.is_main]
        return {"mains": len(mains), "by_class": by_cls, "by_role": by_role, "flex": flex, "providers": providers, "alts": alts}

    def all_characters(self, active_only: bool = True) -> list[tuple[Member, RegisteredCharacter]]:
        out = []
        for m in self.members.values():
            for c in m.characters:
                if not active_only or c.status == "active":
                    out.append((m, c))
        return sorted(out, key=lambda mc: (mc[1].cls, mc[1].name))

    def pending(self) -> list[tuple[Member, RegisteredCharacter]]:
        return [(m, c) for m, c in self.all_characters() if not c.confirmed_by]

    # ---- validation
    def normalise_name(self, name: str) -> str:
        name = name.strip()
        if not NAME_RE.match(name):
            raise RegistryError("Character names are 2–12 letters, no spaces or numbers.")
        return name[0].upper() + name[1:].lower()

    def validate_spec(self, cls: str, spec: str) -> str:
        try:
            return self.profile.spec(cls, spec).spec
        except KeyError:
            valid = ", ".join(self.profile.classes[cls]) if cls in self.profile.classes else "?"
            raise RegistryError(f"{spec} is not a {cls} spec. Valid: {valid}.")

    # ---- member actions
    @_member_write
    def add_character(self, discord_id: int, display_name: str, name: str, cls: str, spec: str, offspec: str | None, make_main: bool, surname: str | None = None) -> tuple[Member, RegisteredCharacter]:
        name = self.normalise_name(name)
        surname = self.normalise_name(surname) if surname else None
        if cls not in self.profile.classes:
            raise RegistryError(f"Unknown class {cls}.")
        spec = self.validate_spec(cls, spec)
        offspec = self.validate_spec(cls, offspec) if offspec else None
        full = f"{name} {surname}" if surname else name
        hit = self.find(full)
        if hit:
            owner, _ = hit
            raise RegistryError(f"{full} is already registered" + (" to you." if owner.discord_id == discord_id else f" to {owner.display_name}. Ask an officer if that's wrong."))
        m = self.member(discord_id, display_name, create=True)
        c = RegisteredCharacter(name=name, surname=surname, cls=cls, spec=spec, offspec=offspec)
        if make_main or m.main is None:
            rank = member_rank(m)  # D25: the member's rank moves to the new main (a first character starts as trial)
            for other in m.characters:
                other.is_main = False
            c.is_main = True
            m.characters.append(c)
            settle_ranks(m, rank)
        else:
            c.rank = "alt"
            m.characters.append(c)
        self.save(m, f"{display_name} registered {c.label} ({cls} {spec}{', main' if c.is_main else ', alt'})")
        return m, c

    # ---- a roster from a signup list (design §5.53): officers register the people who signed up, by their Discord ids
    IMPORT_MAX = 200  # a 40-player guild's whole signup list several times over; a guard against a pasted log

    @_member_write
    def import_members(self, rows: list[dict], by: str, *, by_id: int | None = None, preview: bool = False) -> dict:
        """Register the people of a signup list: each row {discord_id, name, character, cls, spec, offspec?, rank?} becomes
        the member with that Discord id (a record already on file keeps its display name) with the character as their
        main — the record /register writes, so the person finds it on the Me page. Someone who already has an active
        character is never changed. A row that can't be applied is SKIPPED with its reason, never a refusal of the whole
        list (a signup list always holds someone already registered): a missing or test id, a second row for one person,
        a bad class or spec, a character name already registered, an alt rank. Rows without a rank start as trial (D25).
        `preview` writes nothing. Returns {added: [lines], skipped: [{name, why}], preview}."""
        if not isinstance(rows, list) or not rows:
            raise RegistryError("Nothing to import: the list is empty.")
        if len(rows) > self.IMPORT_MAX:
            raise RegistryError(f"That list has {len(rows)} rows; one import takes at most {self.IMPORT_MAX}.")
        adds, skipped, seen_ids, seen_names = [], [], set(), set()
        for r in rows:
            if not isinstance(r, dict):
                raise RegistryError("rows must be objects")
            label = str(r.get("name") or r.get("character") or r.get("discord_id") or "?").strip()
            try:
                raw = str(r.get("discord_id") or "").strip()
                if not raw.isdigit():
                    raise RegistryError("no Discord id")
                uid = int(raw)
                if uid >= self.TEST_BASE:
                    raise RegistryError("a test id")
                if uid in seen_ids:
                    raise RegistryError("a second row for the same person")
                name = str(r.get("name") or "").strip()
                if not name:
                    raise RegistryError("no display name")
                character = self.normalise_name(str(r.get("character") or ""))
                cls = str(r.get("cls") or "")
                if cls not in self.profile.classes:
                    raise RegistryError(f"{cls or '?'} isn't a class in this game version")
                spec = self.validate_spec(cls, str(r.get("spec") or ""))
                offspec = self.validate_spec(cls, str(r["offspec"])) if r.get("offspec") else None
                if offspec == spec:
                    offspec = None
                rank = str(r.get("rank") or "").strip().lower() or None
                if rank and rank not in RANKS:
                    raise RegistryError(f"{rank} isn't a rank ({', '.join(RANKS)})")
                if rank == "alt":
                    raise RegistryError("a first character is a main, never an alt")
                m = self.members.get(uid)
                if m is not None and m.test:
                    raise RegistryError("a test puppet")
                if m is not None and m.active():
                    raise RegistryError(f"already registered as {', '.join(c.label for c in m.active())}")
                hit = self.find(character)
                if hit:
                    raise RegistryError(f"{character} is already registered to {hit[0].display_name}")
                if character.lower() in seen_names:
                    raise RegistryError(f"{character} is already in this list")
            except RegistryError as e:
                skipped.append({"name": label, "why": str(e)})
                continue
            seen_ids.add(uid)
            seen_names.add(character.lower())
            adds.append((uid, name, character, cls, spec, offspec, rank, m.display_name if m else name))
        lines = [f"{shown}: {character} ({cls} {spec}{'/' + offspec if offspec else ''}, {rank or 'trial'})" for uid, name, character, cls, spec, offspec, rank, shown in adds]
        if preview or not adds:
            return {"added": lines, "skipped": skipped, "preview": preview}
        with self.store.batch(f"{self.key}: {len(adds)} members imported from a signup list by {by}"):
            for uid, name, character, cls, spec, offspec, rank, _shown in adds:
                m, c = self.add_character(uid, name, character, cls, spec, offspec, True)
                if rank:
                    c.rank = rank
                self.save(m, f"{m.display_name}: {c.label} imported from a signup list (by {by})")
        return {"added": lines, "skipped": skipped, "preview": False}

    @_member_write
    def set_main(self, discord_id: int, name: str) -> tuple[Member, RegisteredCharacter, RegisteredCharacter | None]:
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(name)), None)
        if not c:
            raise RegistryError(f"You have no active character named {name}.")
        old = m.main
        if old is c:
            raise RegistryError(f"{c.label} is already your main.")
        rank = member_rank(m)
        for other in m.characters:
            other.is_main = False
        c.is_main = True
        settle_ranks(m, rank)  # D25: rank is the player's — it moves to the new main, every other character reads alt
        c.updated_at = now()
        self.save(m, f"{m.display_name} main {old.label if old else '-'} → {c.label}")
        return m, c, old

    @_member_write
    def set_spec(self, discord_id: int, name: str, spec: str, offspec: str | None) -> RegisteredCharacter:
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(name)), None)
        if not c:
            raise RegistryError(f"You have no active character named {name}.")
        c.spec = self.validate_spec(c.cls, spec)
        c.offspec = self.validate_spec(c.cls, offspec) if offspec else None
        c.updated_at = now()
        self.save(m, f"{m.display_name}: {c.label} spec {c.spec}{'/' + c.offspec if c.offspec else ''}")
        return c

    @_member_write
    def delete_character(self, discord_id: int, label: str) -> RegisteredCharacter:
        """Remove a character outright (roster placements go with it); the next character becomes main if needed."""
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(label)), None)
        if not c:
            raise RegistryError(f"You have no active character named {label}.")
        rank = member_rank(m)
        m.characters = [x for x in m.characters if x is not c]
        if c.is_main:
            nxt = next((x for x in m.characters if x.status in ("active", "planned")), None)
            if nxt:
                nxt.is_main = True
        settle_ranks(m, rank)  # the player's standing follows them to the next main
        self.save(m, f"{m.display_name} deleted {c.label}")
        return c

    @_member_write
    def retire(self, discord_id: int, name: str) -> RegisteredCharacter:
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(name)), None)
        if not c:
            raise RegistryError(f"You have no active character named {name}.")
        rank = member_rank(m)
        c.status = "retired"
        c.updated_at = now()
        if c.is_main:
            c.is_main = False
            nxt = next((x for x in m.active()), None)
            if nxt:
                nxt.is_main = True
        settle_ranks(m, rank)  # the player's standing follows them to the new main; the retired one reads alt
        self.save(m, f"{m.display_name} retired {c.label}")
        return c

    # ---- officer actions: by (discord id, label), never a guild-wide name lookup (two members may share a first name)
    def testers(self) -> set[int]:
        """Who runs a rehearsal right now: the `test_by` of every live test run's roster."""
        return {int(r["test_by"]) for r in (self.config.rosters or []) if r.get("test") and str(r.get("test_by") or "").isdigit()}

    def puppet_refusal(self, m: Member, by_id: int | None) -> str | None:
        """A test puppet belongs to the rehearsal: while a test run is live only its tester (or the owner) changes a
        puppet's record; with none live, any officer may (the bench is idle). None = allowed."""
        if not m.test or by_id is None or self.is_owner_id(by_id):
            return None
        testers = self.testers()
        if not testers or int(by_id) in testers:
            return None
        return f"{m.display_name} is a test puppet in another officer's rehearsal. Only that officer or the owner changes it."

    def _own_character(self, discord_id: int, label: str) -> tuple[Member, RegisteredCharacter]:
        m = self.members.get(discord_id)
        if m is None:
            raise RegistryError("They have no registered characters yet.")
        ref = str(label or "").strip()
        c = next((c for c in m.active() if c.label == ref), None) or next((c for c in m.active() if c.matches(ref)), None)
        if c is None:
            raise RegistryError(f"{m.display_name} has no active character named {ref or '?'} (theirs: {', '.join(x.label for x in m.active()) or 'none'}).")
        return m, c

    def confirm(self, discord_id: int, label: str, by: str, *, by_id: int | None = None) -> tuple[Member, RegisteredCharacter]:
        """An officer verifies one of THIS member's characters. Refused for your own character (self-confirm defeats the
        badge; the owner excepted), for a planned (unnamed) one, and when someone already confirmed it."""
        with self.config_lock:  # people writes run in web worker threads and on the loop (design §2.4)
            m, c = self._own_character(discord_id, label)
            why = self.puppet_refusal(m, by_id)
            if why:
                raise RegistryError(why)
            if by_id is not None and int(by_id) == m.discord_id and not self.is_owner_id(by_id):
                raise RegistryError("You can't confirm your own character — another officer does that.")
            if c.status != "active" or not c.name:
                raise RegistryError(f"{c.label} has no name yet — a planned character is confirmed once it is named.")
            if c.confirmed_by:  # re-confirming used to overwrite who confirmed it, silently
                raise RegistryError(f"{c.label} was already confirmed by {c.confirmed_by}.")
            c.confirmed_by, c.confirmed_at, c.updated_at = by, now(), now()
            # the member hears it on their Me page (D26) — every surface's confirm, one commit; never a "welcome" DM (the DM rule)
            self.notify(m.discord_id, "confirmed", f"{c.label} was confirmed by {by}. Welcome aboard.", save=False)
            self.save(m, f"{by} confirmed {c.label}")
            return m, c

    def set_rank(self, discord_id: int, rank: str, by: str, *, by_id: int | None = None) -> tuple[Member, RegisteredCharacter]:
        """The member's rank (D25: per MEMBER, stored on the main; alts always read 'alt'). Refused for a member with no
        main, for your own rank (the owner excepted) and for another officer's test puppet. Returns (member, main)."""
        with self.config_lock:  # people writes run in web worker threads and on the loop (design §2.4)
            rank = str(rank or "").strip().lower()
            if rank not in RANKS:
                raise RegistryError(f"Rank must be one of {', '.join(RANKS)}.")
            m = self.members.get(discord_id)
            if m is None:
                raise RegistryError("They have no registered characters yet.")
            why = self.puppet_refusal(m, by_id)
            if why:
                raise RegistryError(why)
            if by_id is not None and int(by_id) == m.discord_id and not self.is_owner_id(by_id):
                raise RegistryError("You can't set your own rank — another officer does that.")
            main = m.main
            if main is None:
                raise RegistryError(f"{m.display_name} has no main yet — rank lives on the main (they register or plan one first).")
            if main.rank == rank and all(c.rank == "alt" for c in m.characters if c is not main):
                return m, main
            settle_ranks(m, rank)
            main.updated_at = now()
            self.save(m, f"{by} set {m.display_name}'s rank {rank}")
            return m, main

    def migrate_ranks(self, by: str = "migrate", dry_run: bool = False) -> list[str]:
        """D25 (rank per member): every member's non-main characters read 'alt', the main keeps the member's rank (an alt
        that out-ranked a main reading 'alt' gives the main its rank). Idempotent; one data commit for the lot. Run it
        with scripts/migrate_ranks.py while the bot is stopped."""
        lines: list[str] = []
        with self.store.batch(f"{self.key}: migrate: rank per member ({by})"):
            for m in sorted(self.members.values(), key=lambda x: x.display_name.lower()):
                main = m.main
                if main is None:
                    stray = [c for c in m.characters if c.rank != "alt"]
                    if stray:
                        lines.append(f"{m.display_name}: no main — {', '.join(c.label for c in stray)} → alt")
                        if not dry_run:
                            for c in stray:
                                c.rank = "alt"
                            self.save(m, f"migrate: {m.display_name} rank per member")
                    continue
                best = main.rank
                if best == "alt":  # the standing sat on an alt: it belongs to the player, so to the main
                    ranked = [c.rank for c in m.characters if c is not main and c.rank in PLAYER_RANKS]
                    best = max(ranked, key=PLAYER_RANKS.index) if ranked else best
                stray = [c for c in m.characters if c is not main and c.rank != "alt"]
                if best == main.rank and not stray:
                    continue
                lines.append(f"{m.display_name}: rank {best} on {main.label}" + (f"; {', '.join(c.label for c in stray)} → alt" if stray else ""))
                if not dry_run:
                    settle_ranks(m, best)
                    self.save(m, f"migrate: {m.display_name} rank per member")
        return lines

    def set_dm(self, discord_id: int, on: bool, by: str) -> str:
        """DMs from the bot on or off — the ONE verb behind `/me dms`, the Me page's switch, the Members page, plain text
        (op `dm`) and MCP set_dm. Off: Confirm and fill asks wait on the Me page (inbox.DM_OFF_SENTENCE). Returns the line."""
        with self.config_lock:  # people writes run in web worker threads and on the loop (design §2.4)
            m = self.member(discord_id)
            if m.dm_opt_out == (not on):
                return f"{m.display_name}: DMs already {'on' if on else 'off'}"
            m.dm_opt_out = not on
            self.save(m, f"{m.display_name} DMs {'on' if on else 'off'}" + (f" (by {by})" if by != m.display_name else ""))
            return f"{m.display_name}: DMs {'on' if on else 'off'}"

    # ---- regulars (roster redesign Phase 1): officer-curated, per recurring SERIES "<raid>:<schedule>", one character per
    # member per series, stored on the character (RegisteredCharacter.rosters). Regulars are expected on every sheet of the
    # series (officers see who hasn't answered; the nudge goes to them), weigh more at lock, and are asked first by the
    # fill engine. Nothing is pre-filled (owner decision D1): a regular starts unanswered like everyone else.
    def series(self, cls: str | None = "recurring") -> list[tuple[str, str]]:
        """(series key, label) for every schedule of every raid of class `cls` (None = all), in profile order:
        ('barrow_deeps:default', 'Barrow Deeps · Raid nights'). Paused schedules are included (they keep their regulars)."""
        out = []
        for rid in self.profile.raids:
            name = self.raid_def(rid).get("name", rid)
            for s in self.schedules(rid):
                if cls is None or schedule_class(s["kind"]) == cls:
                    out.append((series_key(rid, s["id"]), f"{name} · {s.get('name') or s['id']}"))
        return out

    def series_label(self, key: str) -> str:
        """A series key as a person reads it ('Barrow Deeps · Raid nights'); a removed schedule reads as its raid + id."""
        hit = next((label for k, label in self.series(None) if k == key), None)
        if hit:
            return hit
        rid, _, sid = str(key).partition(":")
        return f"{self.raid_def(rid).get('name', rid) if rid in self.profile.raids else rid} · {sid} (removed schedule)"

    def _recurring_series(self, key: str) -> str:
        key = str(key or "").strip()
        known = dict(self.series("recurring"))
        if key not in known:
            if key in dict(self.series("adhoc")):
                raise RegistryError(f"{self.series_label(key)} is ad hoc (a pickup template): ad hoc runs have no regulars — first come, first rostered")
            raise RegistryError(f"no recurring schedule {key or '?'}" + (f" (recurring: {', '.join(known.values())})" if known else " — add a weekly or lockout schedule first"))
        return key

    def regulars(self, key: str) -> list[tuple[Member, RegisteredCharacter]]:
        """The series' regulars with the character each raids on (active characters only: a retired one drops off),
        real members only (test puppets are never regulars), by display name."""
        out = [(m, c) for m in self.members.values() if not m.test for c in m.active() if key in c.rosters]
        return sorted(out, key=lambda mc: mc[0].display_name.lower())

    def regular_series_of(self, m: Member) -> list[tuple[str, RegisteredCharacter]]:
        """(series key, character) for every series `m` is a regular on (orphans of a removed schedule included)."""
        return [(k, c) for c in m.active() for k in c.rosters if ":" in k]

    @_member_write
    def regular_add(self, discord_id: int, key: str, by: str, character: str | None = None) -> tuple[Member, RegisteredCharacter, list[str]]:
        """Make a member a regular of a recurring series with one of their characters (default the main). One character
        per member per series: picking another replaces it. Returns (member, character, warnings) — D15: the same
        character as a regular on another series of the same raid is allowed with a warning (one lockout, one save)."""
        key = self._recurring_series(key)
        m = self.members.get(discord_id)
        if m is None or not m.active():
            raise RegistryError("they have no registered characters yet — they register first")
        if m.test:
            raise RegistryError(f"{m.display_name} is a test puppet: a test run treats every puppet as a regular already")
        c = next((c for c in m.active() if c.matches(character)), None) if character else m.main
        if not c:
            raise RegistryError(f"{m.display_name} has no {'character named ' + character if character else 'main'} yet")
        before = next((x for x in m.active() if key in x.rosters), None)
        if before is c:
            raise RegistryError(f"{c.label} is already a regular on {self.series_label(key)}")
        for other in m.active():
            if key in other.rosters:
                other.rosters.remove(key)
        c.rosters.append(key)
        rid = key.partition(":")[0]
        warn = [f"{c.label} is also a regular on {self.series_label(k)} — the same raid's lockout: one save covers both nights"
                for k in c.rosters if k != key and k.partition(":")[0] == rid]
        self.save(m, f"{by}: {m.display_name} ({c.label}) regular on {key}" + (f" (was {before.label})" if before else ""))
        return m, c, warn

    @_member_write
    def regular_remove(self, discord_id: int, key: str, by: str) -> Member:
        """Take a member off a series' regulars (an officer, or the member stepping down). Works on any series key they
        hold — a removed schedule's leftovers too (the Rosters page lists those as orphaned)."""
        m = self.members.get(discord_id)
        key = str(key or "").strip()
        hit = [c for c in (m.characters if m else []) if key in c.rosters]
        if not hit:
            raise RegistryError(f"{m.display_name if m else 'they'} isn't a regular on {self.series_label(key) if ':' in key else key or '?'}")
        for c in hit:
            c.rosters.remove(key)
        self.save(m, f"{by}: {m.display_name} off the regulars of {key}")
        return m

    # older names (Discord wizards before Phase 1, tests): the same verbs on series keys
    def roster_add(self, discord_id: int, roster: str, by: str, character: str | None = None, display_name: str | None = None) -> tuple[Member, RegisteredCharacter]:
        m, c, _warn = self.regular_add(discord_id, roster, by, character)
        return m, c

    def roster_remove(self, discord_id: int, roster: str, by: str) -> Member:
        return self.regular_remove(discord_id, roster, by)

    def series_coverage(self, rid: str | None = None) -> list[dict]:
        """Regulars coverage (no run needed): per recurring series (of `rid`, or every raid) its regulars and their role
        counts against the raid's tank/healer minimums times the schedule's rosters. ONE computation for the Rosters
        page, /roster overview, the analytics card, readiness (MCP / help) — computed in code, never by the model.
        [{key, raid, raid_name, id, name, label, active, size, rosters, seats, regulars: [{uid, display_name, character,
        cls, spec, role}], counts: {role: n}, need: {tank: n, healer: n}, short: {role: n}, level, no_spare: [role], cover,
        recruit}]"""
        out = []
        for r in ([rid] if rid else list(self.profile.raids)):
            if r not in self.profile.raids:
                continue
            rname = self.raid_def(r).get("name", r)
            for s in self.schedules(r):
                if schedule_class(s["kind"]) != "recurring":
                    continue
                key = series_key(r, s["id"])
                size = int(self.schedule_def(r, s["id"]).get("size") or DEFAULT_RAID_SIZE)
                n = int(s.get("rosters") or 1)
                bounds = self.role_bounds(r, size)
                regs = [{"uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec,
                         "role": self.profile.spec(c.cls, c.spec).role} for m, c in self.regulars(key)]
                counts = {role: sum(1 for x in regs if x["role"] == role) for role in ROLES}
                need = {role: int(bounds[role]["min"] or 0) * n for role in ("tank", "healer")}
                level, cover, recruit = self._coverage_level(key, need)
                no_spare = [role for role, k in need.items() if k and counts[role] == k] if regs else []
                out.append({"key": key, "raid": r, "raid_name": rname, "id": s["id"], "name": s.get("name") or s["id"],
                            "label": self.schedule_label(r, s), "active": bool(s.get("active", True)), "size": size, "rosters": n,
                            "seats": size * n, "regulars": regs, "counts": counts, "need": need,
                            "short": {role: k - counts[role] for role, k in need.items() if counts[role] < k},
                            "level": level, "no_spare": no_spare, "cover": cover, "recruit": recruit})
        return out

    def _coverage_level(self, key: str, need: dict[str, int]) -> tuple[str | None, list[str], list[str]]:
        """(level, cover, recruit) for a series: green = the regulars' own characters cover the tank/healer minimums with
        a spare in each (one absence still leaves the minimum); amber = covered with no spare (the design's worked
        example: tanks 2/2 is amber), or only with offspecs, declared flex roles or alts (`cover` names them); red = not
        even then (`recruit` = members who aren't regulars of it and play a short role, best rank first). None while it
        has no regulars."""
        regs = self.regulars(key)
        if not regs:
            return None, [], []
        seats = [r for r, k in need.items() for _ in range(k)]

        def role(c) -> str:
            return self.profile.spec(c.cls, c.spec).role

        own = [{role(c)} for _m, c in regs]
        spare = all(can_cover(own, seats + [r]) for r, k in need.items() if k)
        if can_cover(own, seats) and spare:
            return "green", [], []
        wide, cover = [], []
        for m, c in regs:
            extra = set(c.flex or [])
            if c.offspec:
                try:
                    extra.add(self.profile.spec(c.cls, c.offspec).role)
                except KeyError:
                    pass
            extra |= {role(a) for a in m.active() if a.label != c.label}
            extra.discard(role(c))
            if extra & set(need):
                cover.append(f"{m.display_name} ({'/'.join(sorted(extra & set(need)))})")
            wide.append({role(c)} | extra)
        if can_cover(wide, seats):
            return "amber", cover, []
        short = {r for r, k in need.items() if sum(1 for x in own if r in x) < k}
        ids = {m.discord_id for m, _ in regs}
        cands = sorted([m for m in self.members.values() if not m.test and m.discord_id not in ids and m.main and any(role(c) in short for c in m.active())],
                       key=lambda m: (RANK_ORDER.get(m.main.rank, 9), m.display_name.lower()))
        return "red", cover, [m.display_name for m in cands[:5]]

    def orphan_regulars(self) -> list[tuple[str, Member, RegisteredCharacter]]:
        """(key, member, character) for every regular of a series whose schedule no longer exists (or is ad hoc now):
        never stripped silently — the Rosters page lists them with a remove button."""
        known = dict(self.series("recurring"))
        return [(k, m, c) for m in self.members.values() for c in m.active() for k in c.rosters if ":" in k and k not in known]

    def migrate_rosters_to_series(self, by: str = "migrate", dry_run: bool = False) -> list[str]:
        """Roster redesign D11 (Phase 0): RegisteredCharacter.rosters keeps regular-series keys only
        ("<raid>:<schedule>", which always hold a ':'); every other entry is dropped — the phantom "main" left by the
        old teams migration and any run key ("bd-0917-1937") the lock used to write before seats moved to the run files.
        Retired characters too. Idempotent (a second run finds nothing and commits nothing); one commit for the lot
        ("migrate: rosters = regular series only"). `dry_run` changes nothing. Returns one line per member touched.
        Run it with scripts/migrate_rosters.py while the bot is stopped (the bot keeps members in memory)."""
        lines: list[str] = []
        with self.store.batch(f"{self.key}: migrate: rosters = regular series only ({by})"):
            for m in sorted(self.members.values(), key=lambda x: x.display_name.lower()):
                dropped = []
                for c in m.characters:
                    gone = [k for k in c.rosters if ":" not in k]
                    if gone:
                        dropped.append(f"{c.label} ({c.status}): {', '.join(gone)}")
                        if not dry_run:
                            c.rosters = [k for k in c.rosters if ":" in k]
                if dropped:
                    lines.append(f"{m.display_name}: dropped " + "; ".join(dropped))
                    if not dry_run:
                        self.save(m, f"migrate: {m.display_name} rosters = regular series only")
        return lines

    def roster_members(self, roster: str) -> list[tuple[Member, RegisteredCharacter]]:
        """The regulars of a series key (Registry.regulars, puppets included for old call sites)."""
        return [(m, c) for m in self.members.values() for c in m.active() if roster in c.rosters]

    # ---- placement confirmations (after a roster build is approved)
    @_member_write
    def add_placement_ask(self, discord_id: int, roster: str, character: str, by: str, channel: str = "dm") -> dict:
        """`channel`: dm (the Confirm / Can't DM) | web (DMs off: the ask waits on the Me page; unanswered still expires)."""
        m = self.member(discord_id)
        m.placement_asks = [a for a in m.placement_asks if not (a["roster"] == roster and a.get("answer") is None)]
        ask = {"roster": roster, "character": character, "asked_at": now(), "answer": None, "answered_at": None, "by": by, "channel": channel}
        m.placement_asks.append(ask)
        m.placement_asks = m.placement_asks[-PLACEMENT_ASK_HISTORY:]
        self.save(m, f"{m.display_name} asked to confirm {character} on {roster}")
        return ask

    def open_placement_asks(self, discord_id: int) -> list[dict]:
        m = self.members.get(discord_id)
        return [a for a in (m.placement_asks if m else []) if a.get("answer") is None]

    ANSWER_WORDS = {"yes": "yes", "no": "can't make it", "expired": "no answer in time", "cancelled": "the run was cancelled",
                    "withdrawn": "withdrawn by an officer", "released": "the seat was released"}

    def close_placement_ask(self, discord_id: int, roster: str, answer: str, by: str | None = None, *, save: bool = True) -> dict | None:
        """Close a member's OPEN placement ask on `roster` without an answer from them (`released`: their seat went —
        an officer, the board, an absence — so a later Confirm on the Me page or an old card is refused from one place,
        Registry.already_answered). Returns the ask closed, or None when none was open."""
        m = self.members.get(int(discord_id))
        ask = next((a for a in (m.placement_asks if m else []) if a["roster"] == roster and a.get("answer") is None), None)
        if ask is None:
            return None
        ask["answer"], ask["answered_at"], ask["answered_by"] = answer, now(), by
        if save:
            self.save(m, f"{m.display_name}'s confirmation for {roster} closed ({answer})")
        return ask

    # ---- the member's inbox (design §2.5, D26): notices instead of courtesy DMs
    def _fresh_notices(self, m: Member) -> list[dict]:
        """`m`'s notices inside NOTICE_DAYS, at most NOTICE_CAP, oldest first (the stored order)."""
        cut = (datetime.now(timezone.utc) - timedelta(days=NOTICE_DAYS)).isoformat(timespec="seconds")
        return [n for n in m.notices if str(n.get("at") or "") >= cut][-NOTICE_CAP:]

    def notify(self, discord_id: int | None, kind: str, text: str, *, run: str | None = None, save: bool = True) -> dict | None:
        """One notice on a member's inbox (the Me page and /me view show it): an OUTCOME they should know that used to be
        — or would have been — a courtesy DM (the DM rule, D26). Never a DM. Unknown members get nothing (None). The same
        unread text for the same run twice in a row is kept once. `save=False`: the caller saves the member (one commit).
        The save is quiet (save_quiet): the web's live topic hears it, the analytics change log does not."""
        m = self.members.get(int(discord_id)) if discord_id is not None else None
        if m is None:
            return None
        if kind not in NOTICE_KINDS:
            raise ValueError(f"unknown notice kind {kind}")
        text = str(text or "").replace("**", "").strip()[:NOTICE_TEXT_MAX]
        if not text:
            return None
        last = m.notices[-1] if m.notices else None
        if last and last.get("text") == text and last.get("run") == run and not last.get("read_at"):
            return last
        stamp = datetime.now(timezone.utc)
        base, taken, i = f"n{int(stamp.timestamp() * 1000):x}", {x.get("id") for x in m.notices}, 0
        while f"{base}{i:02d}" in taken:  # unique on the member file: two notices in one millisecond (at the cap too) differ
            i += 1
        n = {"id": f"{base}{i:02d}", "kind": kind, "text": text, "run": run,
             "at": stamp.isoformat(timespec="seconds"), "read_at": None}
        m.notices = (self._fresh_notices(m) + [n])[-NOTICE_CAP:]
        if save:
            self.save_quiet(m, f"notice for {m.display_name}: {kind}")
        return n

    def notices(self, discord_id: int, *, unread: bool = False) -> list[dict]:
        """A member's notices, newest first (inside NOTICE_DAYS, at most NOTICE_CAP); `unread` = only those not marked read."""
        m = self.members.get(int(discord_id))
        if m is None:
            return []
        out = [dict(n) for n in reversed(self._fresh_notices(m))]
        return [n for n in out if not n.get("read_at")] if unread else out

    def mark_read(self, discord_id: int, ids: list[str] | None = None) -> int:
        """Mark notices read (`ids` None = all). Returns how many changed; nothing is saved when none did."""
        m = self.members.get(int(discord_id))
        if m is None:
            return 0
        want = None if ids is None else {str(i) for i in ids}
        stamp, changed = now(), 0
        for n in m.notices:
            if not n.get("read_at") and (want is None or n.get("id") in want):
                n["read_at"] = stamp
                changed += 1
        if changed:
            m.notices = self._fresh_notices(m)
            self.save_quiet(m, f"{m.display_name} read {changed} notice(s)")
        return changed

    def save_quiet(self, m: Member, message: str) -> None:
        """Write and commit a member file without the change-log listeners (the analytics channel's log): for the inbox,
        which moves nothing anyone counts. The web's SAVE_LISTENERS still hear it, so the Me page reloads live."""
        m.updated_at = now()
        self.store.write_text(Path(self.key) / "members" / f"{m.discord_id}.json", m.model_dump_json(indent=1))
        self.members[m.discord_id] = m
        self.store.commit(f"{self.key}: {message}")
        self._snap[m.discord_id] = m.model_dump()
        for fn in list(SAVE_LISTENERS):
            try:
                fn(self, "member", [])
            except Exception:  # noqa: BLE001
                log.exception("registry listener failed (member, quiet)")

    def already_answered(self, ask: dict) -> str:
        """'Already answered (yes, by Kestrel at 5:12 PM).' — the one refusal every surface shows for a second answer."""
        what = self.ANSWER_WORDS.get(ask.get("answer"), str(ask.get("answer")))
        who = ask.get("answered_by")
        at = self.local12(ask["answered_at"], "%I:%M %p") if ask.get("answered_at") else None
        return f"Already answered ({what}" + (f", by {who}" if who else "") + (f" at {at}" if at else "") + ")."

    def claim_placement(self, discord_id: int, roster: str, yes: bool, by: str) -> tuple[Member, dict, str]:
        """The check-and-set half of answer_placement, IN MEMORY only (design B2): the caller runs it on the event loop —
        where the DM button and the scheduler's expiry run too — then saves (RaidMixin.record_placement pushes only the
        commit to a thread). A second answer is refused with what the first was (already_answered). Returns (member,
        ask, line)."""
        m = self.member(discord_id)
        ask = next((a for a in m.placement_asks if a["roster"] == roster and a.get("answer") is None), None)
        if not ask:
            last = next((a for a in reversed(m.placement_asks) if a["roster"] == roster), None)
            raise RegistryError(self.already_answered(last) if last and last.get("answer") else "Nothing to answer for that roster.")
        ask["answer"], ask["answered_at"], ask["answered_by"] = ("yes" if yes else "no"), now(), by
        line = f"{m.display_name} confirmed {ask['character']} on {roster}" if yes else f"{m.display_name} can't make {roster} — seat re-opened"
        return m, ask, line

    @_member_write
    def answer_placement(self, discord_id: int, roster: str, yes: bool, by: str) -> str:
        """Yes: keep the seat. No: note it (RaidMixin.after_placement_answer frees the seat in the run file).
        claim_placement + save, in the caller's thread (the Discord paths use RaidMixin.record_placement instead)."""
        m, _ask, line = self.claim_placement(discord_id, roster, yes, by)
        self.save(m, line)
        return line

    @staticmethod
    def absence_span(start: str, end: str | None) -> tuple[str, str]:
        """An absence's (start, end) as ISO days, validated the way add_absence stores it (the previews use it too)."""
        from datetime import date as _d

        try:
            s = _d.fromisoformat(str(start or "").strip())
            e = _d.fromisoformat(str(end).strip()) if end else s
        except ValueError:
            raise RegistryError("Dates are YYYY-MM-DD.")
        if e < s:
            raise RegistryError("End is before start.")
        if (e - s).days > MAX_ABSENCE_DAYS:
            raise RegistryError(f"Absences longer than {MAX_ABSENCE_DAYS} days: ask an officer to take you off the roster instead.")
        return s.isoformat(), e.isoformat()

    @staticmethod
    def absence_reason(reason: str | None) -> str | None:
        """An absence's reason as stored: stripped, None when empty; refused past ABSENCE_REASON_MAX characters (the
        cap lives here, so the Discord modal, the web and plain text all refuse it the same way)."""
        text = (reason or "").strip()
        if len(text) > ABSENCE_REASON_MAX:
            raise RegistryError(f"A reason is at most {ABSENCE_REASON_MAX} characters (this one has {len(text)}) — shorten it; only officers read it.")
        return text or None

    def absence_overlap(self, m: Member | None, start: str, end: str) -> Absence | None:
        """The absence `m` already has on any of those days (ISO, inclusive), or None."""
        return next((a for a in sorted(m.absences, key=lambda x: x.start) if a.start <= end and a.end >= start), None) if m else None

    def check_absence(self, m: Member | None, start: str, end: str | None, reason: str | None = None) -> tuple[str, str]:
        """Everything add_absence refuses, without writing (the previews call it): the span, the reason cap, an overlap
        with an absence already on file ("already away Tue 9 – Thu 11 Dec, recorded by Kessa")."""
        s, e = self.absence_span(start, end)
        self.absence_reason(reason)
        clash = self.absence_overlap(m, s, e)
        if clash is not None:
            who = m.display_name
            raise RegistryError(f"{who} is already away {self.span_label(clash.start, clash.end)}" + (f" (recorded by {clash.by})" if clash.by != who else "")
                                + " — clear that one first (I'm back), or pick days outside it.")
        return s, e

    def add_absence(self, discord_id: int, start: str, end: str | None, reason: str | None, by: str, display_name: str | None = None, *,
                    by_id: int | None = None) -> tuple[Member, Absence]:
        """Record an absence. Refused: a bad span, a reason over the cap, days that overlap an absence on file (one
        record per day — clearing one never leaves another half-covering it), another officer's test puppet."""
        with self.config_lock:  # people writes run in web worker threads and on the loop (design §2.4)
            m = self.member(discord_id, display_name, create=display_name is not None)
            why = self.puppet_refusal(m, by_id)
            if why:
                raise RegistryError(why)
            s, e = self.check_absence(m, start, end, reason)
            a = Absence(start=s, end=e, reason=self.absence_reason(reason), by=by)
            m.absences = [*m.absences, a]
            self.save(m, f"{m.display_name} absent {a.start}" + (f"→{a.end}" if a.end != a.start else "") + (f" (by {by})" if by != m.display_name else ""))
            return m, a

    def clear_absence(self, discord_id: int, start: str, by: str | None = None, *, by_id: int | None = None) -> Absence:
        """Remove the absence starting `start`; returns it so the caller can ripple the cleared span into the sheets
        (RaidMixin.after_absence_cleared). `by` defaults to the member (officers pass their own name). Refused: another
        officer's test puppet (`by_id`, Registry.puppet_refusal)."""
        from datetime import date as _d

        with self.config_lock:  # people writes run in web worker threads and on the loop (design §2.4)
            m = self.member(discord_id)
            why = self.puppet_refusal(m, by_id)
            if why:
                raise RegistryError(why)
            try:
                start = _d.fromisoformat(str(start).strip()).isoformat()
            except ValueError:
                parts = str(start).strip().split("-")
                try:
                    start = _d(*(int(x) for x in parts)).isoformat() if len(parts) == 3 else start
                except (TypeError, ValueError):
                    pass
            a = next((a for a in m.absences if a.start == start), None)
            if a is None:
                raise RegistryError(f"No absence starting {self.day_label(start)}.")
            m.absences = [x for x in m.absences if x.start != start]
            self.save(m, f"{m.display_name} cleared absence {start}" + (f" (by {by})" if by and by != m.display_name else ""))
            return a

    # ---- the character table (Me page and the Members page save the same shape)
    def save_character_table(self, discord_id: int, rows: list[dict], by: str, *, officer: bool = False, by_id: int | None = None,
                             rev: str | None = None) -> list[str]:
        """One save for a member's table. Each row: {label?, cls, spec, offspec?, name?, surname?, main?: bool (or slot: "main"),
        rank?, confirm?: bool, flex?: [role, …] (extra roles; [] clears, absent keeps), delete?: bool, retire?: bool}. A row whose
        label matches an active character updates it (spec/offspec, a name for a planned one); an unknown row adds a
        character (named) or a plan (unnamed); `main` on a row makes it the main — applied BEFORE rank, so one save that
        makes Jonny main and raider is not refused as "rank on an alt". `rank` (D25: per member) is accepted on the row
        that is the main after the save; on an alt only 'alt'. `rank` and `confirm` need `officer` (a member's own POST
        with a rank is refused — no self-promotion). `rev` = the characters_rev the editor loaded (stale → CharactersChanged).
        The whole table is applied to a COPY first, so a refusal on row N leaves nothing applied; then for real, one commit.
        Returns the change lines."""
        with self.config_lock:  # web saves run in worker threads while Discord writes on the loop (design §2.4)
            rows = self.check_character_table(discord_id, rows, by, officer=officer, by_id=by_id, rev=rev)
            m = self.members.get(discord_id)
            who = m.display_name if m else by
            with self.store.batch(f"{self.key}: {by} saved {who}'s characters"):
                return self._apply_table(discord_id, rows, by, by_id)

    def check_character_table(self, discord_id: int, rows: list[dict], by: str, *, officer: bool = False, by_id: int | None = None,
                              rev: str | None = None) -> list[dict]:
        """Everything save_character_table refuses, without writing: the rev, a puppet, a member's rank or confirm, and
        the whole table applied to a copy (the previews call it, so a {preview: true} that answers is a save that works).
        Returns the rows as they will be applied."""
        with self.config_lock:
            m = self.members.get(discord_id)
            if rev and m is not None and rev != self.characters_rev(m):
                raise CharactersChanged(m)
            if m is not None:
                why = self.puppet_refusal(m, by_id)
                if why:
                    raise RegistryError(why)
            rows = self._table_rows(m, rows, officer)
            self._table_shadow(discord_id)._apply_table(discord_id, rows, by, by_id)  # a refusal raises here, nothing written
            return rows

    @staticmethod
    def _table_rows(m: Member | None, rows, officer: bool) -> list[dict]:
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                raise RegistryError("rows must be objects")
            label = str(row.get("label") or "").strip()
            cur = next((x for x in (m.active() if m else []) if x.label == label), None) if label else None
            rank = str(row.get("rank") or "").strip().lower()
            if not officer and rank and (cur is None or rank != cur.rank):
                raise RegistryError("Only an officer sets a rank.")
            if not officer and row.get("confirm") and not (cur and cur.confirmed_by):
                raise RegistryError("Only an officer confirms a character.")
            out.append(row)
        return out

    def _table_shadow(self, discord_id: int) -> "Registry":
        """A throw-away copy whose `save` keeps the member in memory only: the table's verbs run on it unchanged, so a
        refusal is found before anything is committed (Registry.shadow copies the config; this copies one member)."""
        import copy

        sh = copy.copy(self)
        sh.members = dict(self.members)
        if discord_id in sh.members:
            sh.members[discord_id] = self.members[discord_id].model_copy(deep=True)
        sh.listeners, sh._snap, sh._shadow = [], {}, True

        def save(m: Member, message: str) -> None:
            sh.members[m.discord_id] = m

        sh.save = save  # type: ignore[method-assign]
        return sh

    def _apply_table(self, discord_id: int, rows: list[dict], by: str, by_id: int | None) -> list[str]:
        m = self.members.get(discord_id)
        who = m.display_name if m else by
        done: list[str] = []
        want_main = None
        ranks: list[tuple[str, str]] = []  # (final label, rank) — applied after the main moved
        confirms: list[str] = []
        for row in rows:
            label = str(row.get("label") or "").strip()
            cur = next((x for x in (m.active() if m else []) if x.label == label), None) if label else None
            if row.get("delete") or row.get("retire"):
                if cur is None:
                    continue
                if row.get("delete"):
                    self.delete_character(discord_id, cur.label)
                    done.append(f"deleted {cur.label}")
                else:
                    self.retire(discord_id, cur.label)
                    done.append(f"retired {cur.label}")
                m = self.members.get(discord_id)
                continue
            cls, spec, off = (row.get("cls") or "").strip(), (row.get("spec") or "").strip(), (row.get("offspec") or "").strip() or None
            name, surname = (row.get("name") or "").strip(), (row.get("surname") or "").strip() or None
            is_main = bool(row.get("main")) or row.get("slot") == "main"
            if cur is None and label and (any(c.label == label for c in (m.characters if m else [])) or not (cls and spec)):
                # a row naming a character that is gone (retired or deleted meanwhile) is refused, never silently dropped
                raise RegistryError(f"{label} is no longer one of {'your' if not m or by == m.display_name else m.display_name + chr(39) + 's'} active characters.")
            if cur is None:
                if not cls or not spec:
                    continue
                if name:
                    m, cur = self.add_character(discord_id, who, name, cls, spec, off, is_main, surname=surname)
                    done.append(f"added {cur.label}")
                else:
                    m, cur = self.set_plan(discord_id, who, cls, spec, off, "main" if is_main else "alt")
                    done.append(f"planned {cur.cls} {cur.spec}")
            else:
                if spec and (spec, off) != (cur.spec, cur.offspec):
                    cur = self.set_spec(discord_id, cur.label, spec, off)
                    done.append(f"{cur.label}: {spec}" + (f"/{off}" if off else ""))
                if not cur.name and name:
                    m, cur = self.name_character(discord_id, name, "main" if cur.is_main else "alt", surname=surname, label=cur.label)
                    done.append(f"named {cur.label}")
            rank = str(row.get("rank") or "").strip().lower()
            if rank:
                ranks.append((cur.label, rank))
            if row.get("confirm") and not cur.confirmed_by:
                confirms.append(cur.label)
            if row.get("flex") is not None:  # extra roles (set_flex drops the ones the spec/offspec already give)
                fl = row["flex"]
                if not isinstance(fl, list):
                    raise RegistryError("flex is a list of roles")
                before = list(cur.flex)
                cur = self.set_flex(discord_id, cur.label, [str(r).strip().lower() for r in fl if str(r).strip()])
                if cur.flex != before:
                    done.append(f"{cur.label}: flex {', '.join(cur.flex) or 'none'}")
            if is_main:
                want_main = cur.label
        m = self.members.get(discord_id) or m
        if m and want_main and not (m.main and m.main.label == want_main):
            self.set_main(discord_id, want_main)
            done.append(f"main is {want_main}")
        m = self.members.get(discord_id) or m
        for label, rank in ranks:  # main first, then rank (D25): rank is accepted on the main only
            c = next((x for x in (m.active() if m else []) if x.label == label), None)
            if c is None:
                continue
            if c is not m.main:
                if rank != "alt":
                    raise RegistryError(f"{label} is an alt — rank lives on the main ({m.main.label if m.main else 'none yet'}).")
                continue
            if rank != c.rank:
                self.set_rank(discord_id, rank, by, by_id=by_id)
                done.append(f"rank {rank}")  # the callers name the member ("Mira: rank core")
        for label in confirms:
            self.confirm(discord_id, label, by, by_id=by_id)
            done.append(f"confirmed {label}")
        return done

    def absences_between(self, start: str, end: str) -> list[tuple[Member, Absence]]:
        out = []
        for m in self.members.values():
            for a in m.absences:
                if a.start <= end and a.end >= start:
                    out.append((m, a))
        return sorted(out, key=lambda ma: ma[1].start)
