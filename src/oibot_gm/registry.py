"""Member/character registry backed by the git store.

One JSON file per Discord member under <guild>/members/<discord_id>.json.
Characters belong to exactly one member; one of them is the main. Officers
confirm characters and set ranks. Every change is a store commit."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .constants import ROLES
from .profiles import GameProfile
from .store import GitStore

log = logging.getLogger(__name__)

RANKS = ("trial", "raider", "core", "alt", "social")
NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ]{2,12}$")
PLACEMENT_ASK_HISTORY = 12  # placement asks kept per member
MAX_ABSENCE_DAYS = 120
DEFAULT_RAID_SIZE = 20  # when neither the roster nor the raid says


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RegisteredCharacter(BaseModel):
    name: Optional[str] = None  # first name; None until the character exists (pre-launch "planned")
    surname: Optional[str] = None  # WoW: Forever names are first + last
    cls: str
    spec: str
    offspec: Optional[str] = None
    is_main: bool = False
    rank: str = "trial"
    status: str = "active"  # planned | active | retired
    rosters: list[str] = Field(default_factory=list)  # officer-curated roster membership, per character
    flex: list[str] = Field(default_factory=list)  # extra roles this character can play besides its spec/offspec
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    note: Optional[str] = None  # officer-only

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
    dm_opt_out: bool = False
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


class Applicant(BaseModel):
    discord_id: int
    display_name: str
    name: str
    cls: str
    spec: str
    offspec: Optional[str] = None
    logs_url: Optional[str] = None
    availability: Optional[str] = None
    about: Optional[str] = None
    status: str = "open"  # open | accepted | declined | withdrawn
    created_at: str = Field(default_factory=now)
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decision_note: Optional[str] = None
    message_id: Optional[int] = None  # review card in the applications channel


# ---- plain-text permissions (design §5.25): what a sentence to the bot may DO, by capability group, by who, by where.
# Every configops op belongs to exactly one group (configops.OP_GROUP); an op a member aims at their own record is
# "self" instead of its usual group (configops.bind_self decides that in code, never the model).
# id -> (label, default who, what a person could type)
PLAIN_GROUPS: dict[str, tuple[str, list[str], str]] = {
    "self": ("Own record", ["registered"], "I'll be away Nov 1–3 · make Xanthe my main · I can't make tonight"),
    "members": ("Other people's records", ["officers"], "rank Jonny raider · Mira is away next week · turn Kessa's DMs off"),
    "runs": ("Runs", ["officers"], "lock tonight's Barrow Deeps · Xanthe confirmed for tonight · fill the empty seats"),
    "comp": ("Comp & policy", ["officers"], "we want 3–4 healers in Barrow Deeps · pin Jonny in tonight"),
    "raids": ("Raids & auras", ["owner"], "Barrow Deeps runs Tue and Thu 7:30 pm · fortitude is party-wide"),
    "setup": ("Guild setup", ["owner"], "post raid sheets in #signups · add @Raid Lead as an officer role"),
    "test": ("Test bench", ["officers"], "seed 20 puppets · clear the test bench"),
}
PLAIN_WHO = ("everyone", "registered", "officers", "owner")  # plus "role:<discord role id>"
# act = anything the person's groups allow · self = own record only (other requests are refused with where to go)
# answer = questions only · ignore = the bot doesn't answer @mentions there at all
PLAIN_MODES = ("act", "self", "answer", "ignore")
PLAIN_WEB = "web"  # the site's plain-text box and the MCP server: not a Discord channel, always "act"


class PlainPolicy(BaseModel):
    """Owner-set (web Ops page, MCP). Missing groups use their PLAIN_GROUPS default; unlisted channels use `default`,
    except the ops and analytics channels, which act unless listed."""
    groups: dict[str, list[str]] = Field(default_factory=dict)
    channels: dict[str, str] = Field(default_factory=dict)  # channel id (as a string) -> mode
    dm: str = "self"
    default: str = "self"


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
    timezone: str = "America/Los_Angeles"  # guild time: every schedule, window and displayed clock uses it
    auto_propose_hour: int = 12  # guild-local hour when the planner runs for raids with auto-propose on
    slots: list[str] = Field(default_factory=list)  # legacy candidate-slot poll (unused since the signup-driven cycle)
    ask_audience: str = "registered"  # who may ask the LLM free-form questions: officers | confirmed | registered | everyone
    about: Optional[str] = None  # short public blurb for the static guide (owner-set)
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
        return [t["key"] for t in self.rosters] or ["main"]

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


SAVE_LISTENERS: list = []  # process-wide fn(reg, kind: "member"|"config", lines) after every commit (the web's event stream)
RAID_WEIGHT_DEFAULTS = {"rank": 3, "main": 2, "sat_out": 2, "signup_order": 1}
RAID_HOURS_FIELDS = ("signup_lead_hours", "lock_hours_before", "confirm_hours_before", "nudge_hours_before", "fill_ask_hours")
RAID_BOOL_FIELDS = ("nudge", "autofill", "open_dm")  # true/false raid settings (command + API layers parse them the same way)
# effective defaults under profiles/<game>/raids.yaml and guild overrides (nudge_hours_before is derived: halfway between open and lock)
RAID_DEFAULTS = {"lockout_days": 7, "duration_hours": 3, "signup_lead_hours": 120, "lock_hours_before": 24, "confirm_hours_before": 6,
                 "split_policy": "balanced", "nudge": True, "fill_ask_hours": 4, "autofill": True, "open_dm": False}
SPLIT_POLICIES = ("balanced", "first", "rotation")  # how a slot with more joiners than one run seats is split at the scheduled lock
CLOCK12 = "%a %d %b %I:%M %p"  # the one human-facing clock format; %H:%M is for machines
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


def _clabel(c: dict) -> str:
    if c.get("name"):
        return f"{c['name']} {c['surname']}" if c.get("surname") else c["name"]
    return f"{c['cls']} ({c['spec']})"


def _roles_text(rp: dict) -> str:
    if not rp or not rp.get("primary"):
        return "—"
    return rp["primary"] + (f" (+{', '.join(rp['flex'])})" if rp.get("flex") else "")


def diff_member(old: dict | None, new: dict) -> list[str]:
    """Human lines describing what changed between two saved states of a member (for the change log)."""
    who = new["display_name"]
    if old is None:
        lines = [f"➕ **{who}** joined the pool"]
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
            ch.append("flex " + (", ".join(c.get("flex", [])) or "none"))
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


def pool_health_data(reg: "Registry", roster: dict) -> dict:
    """Readiness of the *potential* pool (every planned/active main) against a roster's size, in the same
    shape as raidcycle.health_data (the analytics cards read it). No sheet involved."""
    profile = reg.profile
    size = int(roster.get("size") or reg.raid_def(roster.get("instance")).get("size") or DEFAULT_RAID_SIZE)
    key = roster.get("key", "main")
    keys = reg.run_keys(roster.get("instance")) | {key}  # placed on this roster, or on any run of the same raid
    mains = [(m, m.main) for m in reg.members.values() if m.main]
    on_roster = [(m, c) for m, c in mains if keys & set(c.rosters)]
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
    not_on_roster = [m.display_name for m, c in mains if not (keys & set(c.rosters))]
    unnamed = sum(1 for m, c in mains if not c.name)
    hc_level = "green" if len(mains) >= size else ("amber" if len(mains) + len(alts) >= size else "red")
    return {"headcount": (len(mains), size, len(alts), len(on_roster)), "headcount_level": hc_level, "roles": roles, "buffs": buffs,
            "unresponsive": not_on_roster, "unnamed": unnamed, "mains": len(mains), "on_roster": len(on_roster), "alts": len(alts)}


class Registry:
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

    def day_label(self, iso_day: str, with_year: bool = False) -> str:
        """'2026-12-24' -> 'Thu 24 Dec' (a date a person reads; ISO stays for the data repo and the API)."""
        from datetime import date as _d

        try:
            d = _d.fromisoformat(str(iso_day))
        except ValueError:
            return str(iso_day)
        return d.strftime("%a %d %b %Y" if with_year else "%a %d %b")  # same day form as CLOCK12 ('Wed 09 Dec')

    def span_label(self, start: str, end: str | None) -> str:
        return self.day_label(start) + (f" → {self.day_label(end)}" if end and end != start else "")

    def local12(self, value, fmt: str = CLOCK12) -> str:
        """Guild-local time the way every surface a PERSON reads shows it: 12-hour, no leading zero. ISO and 24-hour
        stay for machines only (datetime-local inputs, comparisons, the data repo). See CLAUDE.md: no military time."""
        return re.sub(r"\b0(\d:\d\d [AP]M)", r"\1", self.local(value, fmt))

    def load_config(self) -> GuildConfig:
        p = self.store.root / self.key / "guild.yaml"
        return GuildConfig(**yaml.safe_load(p.read_text()))

    def save_config(self, message: str, notify: bool = True) -> None:
        self.refresh_profile()
        p = Path(self.key) / "guild.yaml"
        self.store.write_text(p, "# oibot_GM guild config — edit via /gm config or by PR.\n" + yaml.safe_dump(self.config.model_dump(), sort_keys=False))
        self.store.commit(f"{self.key}: {message}")
        if notify:
            self._notify("config", [message])

    def _notify(self, kind: str, lines: list[str]) -> None:
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
                    for k in legacy_teams:
                        if k not in m.main.rosters:
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
    def apply(self, discord_id: int, display_name: str, name: str, cls: str, spec: str, offspec: str | None, logs_url: str | None, availability: str | None, about: str | None) -> Applicant:
        if discord_id in self.members and self.members[discord_id].active():
            raise RegistryError("You're already a member; use /register or /char add for more characters.")
        name = self.normalise_name(name)
        if cls not in self.profile.classes:
            raise RegistryError(f"Unknown class {cls}.")
        spec = self.validate_spec(cls, spec)
        offspec = self.validate_spec(cls, offspec) if offspec else None
        if self.find(name):
            raise RegistryError(f"{name} is already registered to a member.")
        clash = next((x for x in self.applicants.values() if x.status == "open" and x.name.lower() == name.lower() and x.discord_id != discord_id), None)
        if clash:
            raise RegistryError(f"There is already an open application for {name}. If that character is yours, ask an officer.")
        existing = self.applicants.get(discord_id)
        if existing and existing.status == "open":
            raise RegistryError(f"You already have an open application for {existing.name}. An officer will get to it.")
        a = Applicant(discord_id=discord_id, display_name=display_name, name=name, cls=cls, spec=spec, offspec=offspec, logs_url=(logs_url or "").strip() or None, availability=(availability or "").strip() or None, about=(about or "").strip() or None)
        self.save_applicant(a, f"application from {display_name}: {name} ({cls} {spec})")
        return a

    def open_applicants(self) -> list[Applicant]:
        return sorted([a for a in self.applicants.values() if a.status == "open"], key=lambda a: a.created_at)

    def decide(self, discord_id: int, accept: bool, by: str, note: str | None = None) -> tuple[Applicant, Optional[RegisteredCharacter]]:
        a = self.applicants.get(discord_id)
        if not a or a.status != "open":
            raise RegistryError("No open application for that member.")
        a.status = "accepted" if accept else "declined"
        a.decided_by, a.decided_at, a.decision_note = by, now(), (note or "").strip() or None
        char = None
        if accept:
            m, char = self.add_character(a.discord_id, a.display_name, a.name, a.cls, a.spec, a.offspec, True)
            char.confirmed_by, char.confirmed_at = by, now()
            self.save(m, f"{by} accepted {a.display_name} as {a.name}")
        self.save_applicant(a, f"{by} {a.status} application from {a.display_name} ({a.name})")
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
        m = self.members.get(discord_id)
        if m is None:
            if not create:
                raise RegistryError("You have no registered characters yet. Use /register first.")
            m = Member(discord_id=discord_id, display_name=display_name or str(discord_id))
        elif display_name and m.display_name != display_name:
            m.display_name = display_name
        return m

    def find(self, name: str) -> tuple[Member, RegisteredCharacter] | None:
        """By character name; planned (unnamed) characters match by their label, e.g. 'Shaman (Enhancement)'."""
        n = name.strip().lower()
        for m in self.members.values():
            for c in m.characters:
                if c.name and (c.label.lower() == n or c.name.lower() == n):
                    return m, c
        for m in self.members.values():
            for c in m.characters:
                if not c.name and c.status == "planned" and c.label.lower() == n:
                    return m, c
        return None

    # ---- pre-launch plans (no character names yet)
    def set_plan(self, discord_id: int, display_name: str, cls: str, spec: str, offspec: str | None, slot: str) -> tuple[Member, RegisteredCharacter]:
        """slot: main | alt. Replaces any existing *planned* character in that slot."""
        if cls not in self.profile.classes:
            raise RegistryError(f"Unknown class {cls}.")
        spec = self.validate_spec(cls, spec)
        offspec = self.validate_spec(cls, offspec) if offspec else None
        m = self.member(discord_id, display_name, create=True)
        is_main = slot == "main"
        prev = next((c for c in m.characters if c.status == "planned" and c.is_main == is_main), None)
        m.characters = [c for c in m.characters if c is not prev]
        if is_main:
            for c in m.characters:
                c.is_main = False
        c = RegisteredCharacter(cls=cls, spec=spec, offspec=offspec, is_main=is_main, status="planned", rank="trial" if is_main else "alt")
        if prev:  # a re-plan keeps roster placement, rank and identity (the change log shows it as a spec change)
            c.rosters, c.rank, c.created_at, c.note = list(prev.rosters), prev.rank, prev.created_at, prev.note
        m.characters.append(c)
        self.save(m, f"{display_name} plans {slot}: {cls} {spec}{'/' + offspec if offspec else ''}")
        return m, c

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

    def set_roles(self, discord_id: int, display_name: str, primary: str, flex: list[str]) -> Member:
        """Legacy /me plan roles: the primary is ignored (it follows the spec); flex goes on the main."""
        m = self.member(discord_id, display_name, create=True)
        if not m.main:
            raise RegistryError("Plan or register a main first.")
        self.set_flex(discord_id, m.main.label, [f for f in flex if f != primary] + ([primary] if primary else []))
        return m

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

    def run_keys(self, instance: str | None) -> set[str]:
        """Roster keys (standing or dated runs) of an instance."""
        return {t["key"] for t in self.config.rosters if t.get("instance") == instance}

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

    def set_raid_overrides(self, instance: str, fields: dict, by: str) -> list[str]:
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
        self.save_config(f"raid {instance}: {', '.join(fields)} (by {by})")
        return lines

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

    def _apply_raid_override(self, instance: str, field: str, value, by: str, commit: bool = True) -> str:
        """Owner override for a raid: lockout_days | duration_hours | first_open | notes | auto | tank_min/max | healer_min/max | dps_min/max
        | slots | signup_lead_hours | lock_hours_before | confirm_hours_before | nudge_hours_before | fill_ask_hours | split_policy
        | nudge / autofill / open_dm (true|false) | weight_rank/main/sat_out/signup_order."""
        if instance not in self.profile.raids:
            raise RegistryError(f"unknown raid {instance}; known: {', '.join(self.profile.raids)}")
        over = self.config.raids.setdefault(instance, {})
        if field == "slots":
            over["slots"] = parse_slots(value)
            value = ", ".join(over["slots"]) or "none"
        elif field in RAID_HOURS_FIELDS:
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise RegistryError(f"{field} must be a number of hours")
            if num < 0:
                raise RegistryError(f"{field} can't be negative")
            over[field] = int(num) if num == int(num) else num
        elif field in RAID_BOOL_FIELDS:
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
                    raise RegistryError("first_open looks like 2026-12-09T15:00 (guild timezone) or 2026-12-09T15:00-08:00")
                if t.tzinfo is None:
                    t = t.replace(tzinfo=_Z(self.config.timezone))
                over["first_open"] = t.isoformat()
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

    def set_buff_override(self, bid: str, field: str, value, by: str) -> str:
        """scope | family (a family id, another buff's id, or 'own' to stand alone) | strength | status | note."""
        self.buff(bid)
        over = self.config.buffs.setdefault(bid, {})
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
            raise RegistryError(f"unknown buff field {field}")
        self.save_config(f"aura {bid} {field} → {value} (by {by})")
        return f"{bid}: {field} = {value}"

    def set_family_override(self, fid: str, field: str, value, by: str) -> str:
        """name | status | note | value (whole map as 'all: 3, mana: 2') | value:<key> (one entry; 0 or blank removes it)."""
        fid = str(fid or "").strip().lower().replace(" ", "_")
        if not fid:
            raise RegistryError("which family?")
        over = self.config.families.setdefault(fid, {})
        if fid not in self.profile.families and "name" not in over and field != "name":
            over["name"] = fid.replace("_", " ").title()
        if field == "name":
            over["name"] = str(value or "").strip() or fid
        elif field == "status":
            if value not in self.STATUSES:
                raise RegistryError(f"status must be one of {', '.join(self.STATUSES)}")
            over["status"] = value
        elif field == "note":
            over["note"] = str(value or "").strip()
        elif field == "value":
            cur = dict(self.profile.families[fid].value) if fid in self.profile.families else {}
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
            _ = cur
        elif field.startswith("value:"):
            key = self._value_key(field[6:])
            cur = dict(over.get("value") or (self.profile.families[fid].value if fid in self.profile.families else {}))
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
            raise RegistryError(f"unknown family field {field}")
        self.save_config(f"aura family {fid} {field} → {value} (by {by})")
        return f"family {fid}: {field} = {value}"

    def clear_aura_overrides(self, by: str, bid: str | None = None) -> str:
        if bid:
            self.config.buffs.pop(bid, None)
            self.config.families.pop(bid, None)
            self.save_config(f"aura {bid} overrides cleared (by {by})")
            return f"{bid}: back to the game defaults"
        self.config.buffs, self.config.families = {}, {}
        self.save_config(f"aura overrides cleared (by {by})")
        return "auras: back to the game defaults"

    def clear_raid_override(self, instance: str, by: str) -> str:
        self.config.raids.pop(instance, None)
        self.save_config(f"raid {instance} overrides cleared (by {by})")
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

    def plain_allows(self, group: str, author_id: int, officer: bool, role_ids=()) -> bool:
        if self.config.owner_discord_id is not None and author_id == self.config.owner_discord_id:
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
        """THE plain-text permission check, used by every plain-text path (Discord @mention and DMs, /gm change, the
        site's box, MCP). `group`: the op's capability group ("self" when it acts on the author's own record, see
        configops.bind_self); `fallback`: the group that would allow it on anyone (an officer's own absence passes as
        "members" even if "self" were closed to them); `channel`: channel id, None = DMs, PLAIN_WEB. Returns None when
        allowed, otherwise the refusal line: who may, and where."""
        what = what or PLAIN_GROUPS[group][0].lower()
        mode = self.plain_mode(channel)
        owner = self.config.owner_discord_id is not None and author_id == self.config.owner_discord_id
        where = "in DMs" if channel is None else f"in <#{channel}>"
        act = self.plain_act_channels()
        go = ("use " + " or ".join(f"<#{c}>" for c in act)) if act else "use the slash commands or the site"
        if not (self.plain_allows(group, author_id, officer, role_ids) or (fallback and self.plain_allows(fallback, author_id, officer, role_ids))):
            hint = " — /register first" if group == "self" and "registered" in self.plain_who(group) and self.verification(author_id) == "unregistered" else ""
            return f"Only {self.plain_who_text(group)} can change {what}{hint}."  # who first: it holds in every channel
        if mode == "ignore" or (mode == "answer" and not owner):
            return f"Plain text only answers questions {where} — {go}."
        if mode == "self" and group != "self" and not owner:
            return f"Plain text can't change {what} {where} — {go}."
        return None

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
        # a group left at its default is not stored, so a changed default later reaches it
        p.groups = {g: w for g, w in p.groups.items() if w != PLAIN_GROUPS[g][1]}
        before = self.config.plain
        self.config.plain = p
        changed = [g for g in PLAIN_GROUPS if self.plain_who(g) != (list(before.groups[g]) if g in before.groups else PLAIN_GROUPS[g][1])]
        chans = sorted(set(p.channels.items()) ^ set(before.channels.items()))
        line = "plain-text permissions: " + ("; ".join(f"{PLAIN_GROUPS[g][0]} → {self.plain_who_text(g)}" for g in changed) or "groups unchanged") \
            + (f"; channels {len({c for c, _ in chans})} changed" if chans else "") + (f"; DMs {before.dm} → {p.dm}" if before.dm != p.dm else "") \
            + (f"; other channels {before.default} → {p.default}" if before.default != p.default else "")
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
            for other in m.characters:
                other.is_main = False
            c.is_main = True
            c.rank = "trial" if m.main is None else c.rank
        else:
            c.rank = "alt"
        m.characters.append(c)
        self.save(m, f"{display_name} registered {c.label} ({cls} {spec}{', main' if c.is_main else ', alt'})")
        return m, c

    def set_main(self, discord_id: int, name: str) -> tuple[Member, RegisteredCharacter, RegisteredCharacter | None]:
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(name)), None)
        if not c:
            raise RegistryError(f"You have no active character named {name}.")
        old = m.main
        if old is c:
            raise RegistryError(f"{c.label} is already your main.")
        for other in m.characters:
            other.is_main = False
        c.is_main = True
        if old and old.rank in ("trial", "raider", "core"):
            c.rank, old.rank = old.rank, "alt"  # rank follows the player
        c.updated_at = now()
        self.save(m, f"{m.display_name} main {old.label if old else '-'} → {c.label}")
        return m, c, old

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

    def delete_character(self, discord_id: int, label: str) -> RegisteredCharacter:
        """Remove a character outright (roster placements go with it); the next character becomes main if needed."""
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(label)), None)
        if not c:
            raise RegistryError(f"You have no active character named {label}.")
        m.characters = [x for x in m.characters if x is not c]
        if c.is_main:
            nxt = next((x for x in m.characters if x.status in ("active", "planned")), None)
            if nxt:
                nxt.is_main = True
                if nxt.rank == "alt" and c.rank in ("trial", "raider", "core"):
                    nxt.rank = c.rank
        self.save(m, f"{m.display_name} deleted {c.label}")
        return c

    def retire(self, discord_id: int, name: str) -> RegisteredCharacter:
        m = self.member(discord_id)
        c = next((c for c in m.active() if c.matches(name)), None)
        if not c:
            raise RegistryError(f"You have no active character named {name}.")
        c.status = "retired"
        c.updated_at = now()
        if c.is_main:
            c.is_main = False
            nxt = next((x for x in m.active()), None)
            if nxt:
                nxt.is_main = True
                if nxt.rank == "alt" and c.rank in ("trial", "raider", "core"):
                    nxt.rank = c.rank  # the player's standing follows them to the new main
        self.save(m, f"{m.display_name} retired {c.label}")
        return c

    # ---- officer actions
    def confirm(self, name: str, by: str) -> tuple[Member, RegisteredCharacter]:
        hit = self.find(name)
        if not hit:
            raise RegistryError(f"No character named {name}.")
        m, c = hit
        if c.confirmed_by:  # re-confirming used to overwrite who confirmed it, silently
            raise RegistryError(f"{c.label} was already confirmed by {c.confirmed_by}.")
        c.confirmed_by, c.confirmed_at, c.updated_at = by, now(), now()
        self.save(m, f"{by} confirmed {c.label}")
        return m, c

    def set_rank(self, name: str, rank: str, by: str) -> tuple[Member, RegisteredCharacter]:
        if rank not in RANKS:
            raise RegistryError(f"Rank must be one of {', '.join(RANKS)}.")
        hit = self.find(name)
        if not hit:
            raise RegistryError(f"No character named {name}.")
        m, c = hit
        c.rank, c.updated_at = rank, now()
        self.save(m, f"{by} set {c.label} rank {rank}")
        return m, c

    def officer_set_main(self, discord_id: int, name: str, by: str) -> tuple[Member, RegisteredCharacter, RegisteredCharacter | None]:
        m, c, old = self.set_main(discord_id, name)
        return m, c, old

    # ---- roster membership (curated, per character)
    def roster_add(self, discord_id: int, roster: str, by: str, character: str | None = None, display_name: str | None = None) -> tuple[Member, RegisteredCharacter]:
        if roster not in self.config.roster_keys():
            raise RegistryError(f"Unknown roster {roster}. Rosters: {', '.join(self.config.roster_keys())}.")
        m = self.member(discord_id, display_name, create=display_name is not None)
        c = next((c for c in m.active() if c.matches(character)), None) if character else m.main
        if not c:
            raise RegistryError(f"{m.display_name} has no {'character named ' + character if character else 'main'} yet.")
        for other in m.active():  # one character per member per roster
            if other is not c and roster in other.rosters:
                other.rosters.remove(roster)
        if roster not in c.rosters:
            c.rosters.append(roster)
            self.save(m, f"{by} added {m.display_name} ({c.label}) to roster {roster}")
        return m, c

    def roster_remove(self, discord_id: int, roster: str, by: str) -> Member:
        if roster not in self.config.roster_keys():
            raise RegistryError(f"No roster called {roster}.")
        m = self.member(discord_id)
        changed = False
        for c in m.characters:
            if roster in c.rosters:
                c.rosters.remove(roster)
                changed = True
        if not changed:
            raise RegistryError(f"{m.display_name} isn't on {roster}.")
        self.save(m, f"{by} removed {m.display_name} from roster {roster}")
        return m

    def roster_members(self, roster: str) -> list[tuple[Member, RegisteredCharacter]]:
        out = []
        for m in self.members.values():
            for c in m.active():
                if roster in c.rosters:
                    out.append((m, c))
        return out

    def roster_pool(self, roster: str) -> list[tuple[Member, RegisteredCharacter]]:
        """Who a sheet expects, with the character they raid on: the roster if curated, else every main.
        A test run's pool is the puppets plus the officer who opened it — real members are never involved."""
        t = self.config.roster(roster) or {}
        if t.get("test"):
            tester = t.get("test_by")
            return [(m, m.main) for m in self.members.values() if m.main and (m.test or m.discord_id == tester)]
        members = self.roster_members(roster)
        return members if members else [(m, m.main) for m in self.members.values() if m.main]

    def on_roster(self, m: Member, roster: str) -> bool:
        return any(roster in c.rosters for c in m.active())

    def team_pool(self, team: str) -> list[Member]:
        """Members of roster_pool (older call sites in discord_raid.py / raidcycle.py)."""
        return [m for m, _ in self.roster_pool(team)]

    # ---- placement confirmations (after a roster build is approved)
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

    def answer_placement(self, discord_id: int, roster: str, yes: bool, by: str) -> str:
        """Yes: keep the seat. No: give the seat back (roster_remove) and note it."""
        m = self.member(discord_id)
        ask = next((a for a in m.placement_asks if a["roster"] == roster and a.get("answer") is None), None)
        if not ask:
            raise RegistryError("Nothing to answer for that roster.")
        ask["answer"], ask["answered_at"] = ("yes" if yes else "no"), now()
        if yes:
            self.save(m, f"{m.display_name} confirmed {ask['character']} on {roster}")
            return f"{m.display_name} confirmed {ask['character']} on {roster}"
        self.save(m, f"{m.display_name} declined {roster}")
        if self.on_roster(m, roster):
            self.roster_remove(discord_id, roster, by)
        return f"{m.display_name} can't make {roster} — seat re-opened"

    def add_absence(self, discord_id: int, start: str, end: str | None, reason: str | None, by: str, display_name: str | None = None) -> tuple[Member, Absence]:
        from datetime import date as _d

        try:
            s = _d.fromisoformat(start)
            e = _d.fromisoformat(end) if end else s
        except ValueError:
            raise RegistryError("Dates are YYYY-MM-DD.")
        if e < s:
            raise RegistryError("End is before start.")
        if (e - s).days > MAX_ABSENCE_DAYS:
            raise RegistryError(f"Absences longer than {MAX_ABSENCE_DAYS} days: ask an officer to take you off the roster instead.")
        m = self.member(discord_id, display_name, create=display_name is not None)
        a = Absence(start=s.isoformat(), end=e.isoformat(), reason=(reason or "").strip() or None, by=by)
        m.absences = [x for x in m.absences if not (x.start == a.start and x.end == a.end)] + [a]
        self.save(m, f"{m.display_name} absent {a.start}" + (f"→{a.end}" if a.end != a.start else "") + (f" (by {by})" if by != m.display_name else ""))
        return m, a

    def clear_absence(self, discord_id: int, start: str, by: str | None = None) -> Absence:
        """Remove the absence starting `start`; returns it so the caller can ripple the cleared span into the sheets
        (RaidMixin.after_absence_cleared). `by` defaults to the member (officers pass their own name)."""
        from datetime import date as _d

        m = self.member(discord_id)
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
    def save_character_table(self, discord_id: int, rows: list[dict], by: str) -> list[str]:
        """One save for a member's table. Each row: {label?, cls, spec, offspec?, name?, surname?, main?: bool (or slot: "main"),
        rank?, delete?: bool, retire?: bool}. A row whose label matches an active character updates it (spec/offspec,
        a name for a planned one, rank when given); an unknown row adds a character (named) or a plan (unnamed);
        `main` on a row makes it the main. One commit; returns the change lines."""
        m = self.members.get(discord_id)
        who = m.display_name if m else by
        done: list[str] = []
        want_main = None
        with self.store.batch(f"{self.key}: {by} saved {who}'s characters"):
            for row in rows or []:
                if not isinstance(row, dict):
                    raise RegistryError("rows must be objects")
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
                rank = str(row.get("rank") or "").strip()
                if rank and rank != cur.rank:
                    self.set_rank(cur.label, rank, by)
                    done.append(f"{cur.label}: rank {rank}")
                if is_main:
                    want_main = cur.label
            m = self.members.get(discord_id) or m
            if m and want_main and not (m.main and m.main.label == want_main):
                self.set_main(discord_id, want_main)
                done.append(f"main is {want_main}")
        return done

    def absences_between(self, start: str, end: str) -> list[tuple[Member, Absence]]:
        out = []
        for m in self.members.values():
            for a in m.absences:
                if a.start <= end and a.end >= start:
                    out.append((m, a))
        return sorted(out, key=lambda ma: ma[1].start)
