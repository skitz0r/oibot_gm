"""Member/character registry backed by the git store.

One JSON file per Discord member under <guild>/members/<discord_id>.json.
Characters belong to exactly one member; one of them is the main. Officers
confirm characters and set ranks. Every change is a store commit."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from .profiles import GameProfile
from .store import GitStore

RANKS = ("trial", "raider", "core", "alt", "social")
NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ]{2,12}$")


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


AVAILABILITY = ("in", "out", "sub")


class Member(BaseModel):
    discord_id: int
    display_name: str
    characters: list[RegisteredCharacter] = Field(default_factory=list)
    availability: dict[str, str] = Field(default_factory=dict)  # team -> in | out | sub
    absences: list[Absence] = Field(default_factory=list)
    role_prefs: dict = Field(default_factory=dict)  # {"primary": "healer", "flex": ["ranged"]}
    slot_prefs: dict[str, str] = Field(default_factory=dict)  # 'Tue 19:30' -> yes | maybe | no (legacy; the grid wins when set)
    placement_asks: list[dict] = Field(default_factory=list)  # {roster, character, asked_at, answer yes|no|None, answered_at, by}
    week: list[dict] = Field(default_factory=list)  # weekly availability grid: {day 0-6 (Mon=0), start, end (minutes), level preferred|available}; times not covered = unavailable
    teams: list[str] = Field(default_factory=list)  # officer-curated team membership (the default weekly roster)
    dm_opt_out: bool = False
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
    officer_roles: list[str] = Field(default_factory=list)
    # rosters: {key, name, size, schedule: "Tue 19:30", instance, cutoff_soft_hours, cutoff_hard_hours, open_days_before, reminders, open_dm}
    rosters: list[dict] = Field(default_factory=list)
    # raids: guild overrides per instance id over profiles/<game>/raids.yaml: {lockout_days, duration_hours, comp: {tank/healer/dps: {min,max}}, notes}
    raids: dict[str, dict] = Field(default_factory=dict)
    # auras: what the guild has learned about Forever's buffs, over profiles/<game>/buffs.yaml
    buffs: dict[str, dict] = Field(default_factory=dict)  # buff id -> {scope, family, strength, status, note}
    families: dict[str, dict] = Field(default_factory=dict)  # family id -> {name, value: {key: points}, status, note}

    def __init__(self, **data):
        if "raid_teams" in data and not data.get("rosters"):
            data["rosters"] = data.pop("raid_teams")  # pre-roster config files
        data.pop("raid_teams", None)
        super().__init__(**data)

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


RAID_WEIGHT_DEFAULTS = {"rank": 3, "main": 2, "sat_out": 2, "signup_order": 1}
RAID_HOURS_FIELDS = ("signup_lead_hours", "lock_hours_before", "confirm_hours_before")
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
    for team in sorted(set(old.get("availability", {})) | set(new.get("availability", {}))):
        a, b = old.get("availability", {}).get(team), new.get("availability", {}).get(team)
        if a != b:
            lines.append(f"{who}: availability {team} {a or '—'} → **{b or '—'}**")
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
    if old.get("week") != new.get("week"):
        hours = sum((r["end"] - r["start"]) for r in new.get("week", [])) / 60
        lines.append(f"{who}: availability grid → {len(new.get('week', []))} block(s), {hours:.0f}h/week")
    if old.get("slot_prefs") != new.get("slot_prefs"):
        lines.append(f"{who}: slots " + (", ".join(f"{k} {v}" for k, v in new.get("slot_prefs", {}).items()) or "cleared"))
    if old.get("dm_opt_out") != new.get("dm_opt_out"):
        lines.append(f"{who}: DMs {'off' if new['dm_opt_out'] else 'on'}")
    return lines


def bank_rows(reg: "Registry") -> list[dict]:
    """Character bank: one row per member (main first by class, then those without a main), for render.bank_png."""
    rows = []
    for m in reg.members.values():
        main = m.main
        alts = [c for c in m.active() if not c.is_main]
        if not main and not alts:
            continue
        role, _ = reg.roles_of(m)
        rows.append({"member": m.display_name, "role": role,
                     "main": {"cls": main.cls, "spec": main.spec, "offspec": main.offspec, "name": main.name, "status": main.status, "rank": main.rank, "rosters": list(main.rosters)} if main else None,
                     "alts": [{"cls": a.cls, "spec": a.spec, "name": a.name, "status": a.status} for a in alts]})
    order = {"tank": 0, "healer": 1, "melee": 2, "ranged": 3}
    return sorted(rows, key=lambda r: (r["main"] is None, order.get(r["role"], 9), r["main"]["cls"] if r["main"] else "", r["member"].lower()))


def pool_health_data(reg: "Registry", roster: dict) -> dict:
    """Readiness of the *potential* pool (every planned/active main) against a roster's size, in the same
    shape as raidcycle.health_data so it renders through render.health_png. No sheet involved."""
    profile = reg.profile
    size = int(roster.get("size") or reg.raid_def(roster.get("instance")).get("size") or 20)
    key = roster.get("key", "main")
    keys = reg.run_keys(roster.get("instance")) | {key}  # placed on this roster, or on any run of the same raid
    mains = [(m, m.main) for m in reg.members.values() if m.main]
    on_roster = [(m, c) for m, c in mains if keys & set(c.rosters)]
    alts = [(m, c) for m in reg.members.values() for c in m.active() if not c.is_main]
    bounds = reg.role_bounds(roster.get("instance"), size)

    def role_of(m: Member, c: RegisteredCharacter) -> str:
        return reg.roles_of(m)[0] or profile.spec(c.cls, c.spec).role

    counts = {r: sum(1 for m, c in mains if role_of(m, c) == r) for r in ("tank", "healer", "melee", "ranged")}
    need = {"tank": bounds["tank"]["min"], "healer": bounds["healer"]["min"], "melee": 0, "ranged": 0}
    roles = []
    for r in ("tank", "healer", "melee", "ranged"):
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
        self.config = self.load_config()
        self.refresh_profile()
        self.reload()

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
        for fn in self.listeners:
            try:
                fn(self, kind, lines)
            except Exception as e:  # noqa: BLE001
                print(f"registry listener failed: {e}")

    def reload(self) -> None:
        d = self.store.root / self.key / "members"
        self.members = {}
        self._snap = {}
        if d.exists():
            for f in d.glob("*.json"):
                m = Member.model_validate_json(f.read_text())
                if m.role_prefs.get("flex") and m.main:  # flex used to live on the member; it belongs to the character
                    for r in m.role_prefs["flex"]:
                        if r not in m.main.flex:
                            m.main.flex.append(r)
                    m.role_prefs = {}
                    self.store.write_text(Path(self.key) / "members" / f"{m.discord_id}.json", m.model_dump_json(indent=1))
                if m.teams and m.main:  # pre-roster files kept membership on the member
                    for k in m.teams:
                        if k not in m.main.rosters:
                            m.main.rosters.append(k)
                    m.teams = []
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
        valid = ("tank", "healer", "melee", "ranged")
        if any(r not in valid for r in roles):
            raise RegistryError(f"Roles are {', '.join(valid)}.")
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
        out.setdefault("lockout_days", 7)
        out.setdefault("duration_hours", 3)
        out.setdefault("slots", [])
        out.setdefault("signup_lead_hours", 120)
        out.setdefault("lock_hours_before", 24)
        out.setdefault("confirm_hours_before", 6)
        out["weights"] = {**RAID_WEIGHT_DEFAULTS, **(out.get("weights") or {})}
        return out

    def raid_shell(self, instance: str) -> dict:
        """A roster-shaped dict for a raid definition, so pool/comp/group analytics can run per raid."""
        rd = self.raid_def(instance)
        over = self.config.raids.get(instance, {})
        return {"key": instance, "name": rd.get("name", instance), "size": int(rd.get("size") or 20), "instance": instance,
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
        """Owner override for a raid: lockout_days | duration_hours | first_open | notes | auto | tank_min/max | healer_min/max | dps_min/max
        | slots | signup_lead_hours | lock_hours_before | confirm_hours_before | weight_rank/main/sat_out/signup_order."""
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
            eff = self.raid_def(instance)
            if eff["lock_hours_before"] < eff["confirm_hours_before"]:
                raise RegistryError("lock must come before the confirmation deadline (lock_hours_before ≥ confirm_hours_before)")
            if eff["signup_lead_hours"] <= eff["lock_hours_before"]:
                raise RegistryError("signups must open before they lock (signup_lead_hours > lock_hours_before)")
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
            over.setdefault("comp", {}).setdefault(role, {})[bound] = n
            eff = self.raid_def(instance)["comp"][role]
            if eff.get("min", 0) > eff.get("max", 999):
                raise RegistryError(f"{role}: min {eff['min']} above max {eff['max']}")
        else:
            raise RegistryError(f"unknown raid field {field}")
        self.save_config(f"raid {instance} {field} → {value} (by {by})")
        return f"{instance}: {field} = {value}"

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
                n = float(value) if value not in (None, "") else 0.0
            except (TypeError, ValueError):
                raise RegistryError(f"{key}: {value!r} isn't a number")
            if n > 0:
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
        by_role: dict[str, int] = {r: 0 for r in ("tank", "healer", "melee", "ranged")}
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
        m = self.member(discord_id)
        changed = False
        for c in m.characters:
            if roster in c.rosters:
                c.rosters.remove(roster)
                changed = True
        if changed:
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
        """Who a sheet expects, with the character they raid on: the roster if curated, else every main."""
        members = self.roster_members(roster)
        return members if members else [(m, m.main) for m in self.members.values() if m.main]

    def on_roster(self, m: Member, roster: str) -> bool:
        return any(roster in c.rosters for c in m.active())

    # older call sites
    def team_add(self, discord_id: int, team: str, by: str, display_name: str | None = None) -> Member:
        return self.roster_add(discord_id, team, by, None, display_name)[0]

    def team_remove(self, discord_id: int, team: str, by: str) -> Member:
        return self.roster_remove(discord_id, team, by)

    def team_members(self, team: str) -> list[Member]:
        return [m for m, _ in self.roster_members(team)]

    def team_pool(self, team: str) -> list[Member]:
        return [m for m, _ in self.roster_pool(team)]

    # ---- availability & absences
    def set_slot_prefs(self, discord_id: int, prefs: dict[str, str], display_name: str | None = None) -> Member:
        """Rate the guild's candidate raid times: yes | maybe | no (unknown slots and other values are dropped)."""
        m = self.member(discord_id, display_name, create=display_name is not None)
        clean = {k: v for k, v in prefs.items() if k in self.config.slots and v in ("yes", "maybe", "no")}
        if clean == m.slot_prefs:
            return m
        m.slot_prefs = clean
        self.save(m, f"{m.display_name} slots: " + ", ".join(f"{k}={v}" for k, v in clean.items()))
        return m

    # ---- placement confirmations (after a roster build is approved)
    def add_placement_ask(self, discord_id: int, roster: str, character: str, by: str) -> dict:
        m = self.member(discord_id)
        m.placement_asks = [a for a in m.placement_asks if not (a["roster"] == roster and a.get("answer") is None)]
        ask = {"roster": roster, "character": character, "asked_at": now(), "answer": None, "answered_at": None, "by": by}
        m.placement_asks.append(ask)
        m.placement_asks = m.placement_asks[-12:]
        self.save(m, f"{m.display_name} asked to confirm {character} on {roster}")
        return ask

    def open_placement_asks(self, discord_id: int) -> list[dict]:
        m = self.members.get(discord_id)
        return [a for a in (m.placement_asks if m else []) if a.get("answer") is None]

    def answer_placement(self, discord_id: int, roster: str, yes: bool, by: str) -> str:
        """Yes: keep the seat and default them In for that roster. No: give the seat back (roster_remove) and note it."""
        m = self.member(discord_id)
        ask = next((a for a in m.placement_asks if a["roster"] == roster and a.get("answer") is None), None)
        if not ask:
            raise RegistryError("Nothing to answer for that roster.")
        ask["answer"], ask["answered_at"] = ("yes" if yes else "no"), now()
        if yes:
            if m.availability.get(roster) != "in":
                m.availability[roster] = "in"
            self.save(m, f"{m.display_name} confirmed {ask['character']} on {roster}")
            return f"{m.display_name} confirmed {ask['character']} on {roster}"
        self.save(m, f"{m.display_name} declined {roster}")
        if self.on_roster(m, roster):
            self.roster_remove(discord_id, roster, by)
        return f"{m.display_name} can't make {roster} — seat re-opened"

    def set_week(self, discord_id: int, ranges: list[dict], display_name: str | None = None) -> Member:
        """Replace the weekly availability grid. Ranges are merged per day/level; anything outside them is unavailable."""
        clean: list[dict] = []
        for r in ranges:
            try:
                day, start, end, level = int(r["day"]), int(r["start"]), int(r["end"]), str(r["level"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= day <= 6 and 0 <= start < end <= 1440 and level in ("preferred", "available")):
                continue
            clean.append({"day": day, "start": start, "end": end, "level": level})
        merged: list[dict] = []
        for day in range(7):
            for level in ("preferred", "available"):
                spans = sorted((r["start"], r["end"]) for r in clean if r["day"] == day and r["level"] == level)
                cur: list[int] | None = None
                for a, b in spans:
                    if cur and a <= cur[1]:
                        cur[1] = max(cur[1], b)
                    else:
                        if cur:
                            merged.append({"day": day, "start": cur[0], "end": cur[1], "level": level})
                        cur = [a, b]
                if cur:
                    merged.append({"day": day, "start": cur[0], "end": cur[1], "level": level})
        m = self.member(discord_id, display_name, create=display_name is not None)
        if merged == m.week:
            return m
        m.week = merged
        hours = sum((r["end"] - r["start"]) for r in merged) / 60
        self.save(m, f"{m.display_name} availability grid: {len(merged)} block(s), {hours:.0f}h/week")
        return m

    def week_level(self, m: Member, start, hours: float) -> str | None:
        """'preferred' if the whole raid window sits inside preferred blocks, 'available' if inside preferred+available,
        None if uncovered; the window is in the guild timezone, Monday=0. Members without a grid return None."""
        if not m.week:
            return None
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        z = ZoneInfo(self.config.timezone)
        t0 = start.astimezone(z)
        need = [(t0 + timedelta(minutes=i)) for i in range(0, int(hours * 60), 30)]

        def covered(levels: tuple[str, ...]) -> bool:
            for t in need:
                mins = t.hour * 60 + t.minute
                if not any(r["day"] == t.weekday() and r["start"] <= mins < r["end"] and r["level"] in levels for r in m.week):
                    return False
            return True

        if covered(("preferred",)):
            return "preferred"
        if covered(("preferred", "available")):
            return "available"
        return None

    def slot_pref(self, m: Member, start, hours: float, slot: str) -> str | None:
        """Effective yes/maybe/no for a raid window: the grid when the member has one, else the legacy slot rating."""
        if m.week:
            return {"preferred": "yes", "available": "maybe", None: "no"}[self.week_level(m, start, hours)]
        return m.slot_prefs.get(slot)

    def week_heat(self) -> list[list[tuple[int, int]]]:
        """[day][half-hour] -> (preferred count, available count) over mains, for the officer heat-map."""
        heat = [[(0, 0) for _ in range(48)] for _ in range(7)]
        for m in self.members.values():
            if not m.main or not m.week:
                continue
            for r in m.week:
                for i in range(r["start"] // 30, min(48, -(-r["end"] // 30))):
                    p, a = heat[r["day"]][i]
                    heat[r["day"]][i] = (p + 1, a) if r["level"] == "preferred" else (p, a + 1)
        return heat

    def slot_summary(self) -> list[dict]:
        """Per candidate slot: who said yes/maybe/no, with role counts among the yes+maybe mains."""
        out = []
        from .raidcycle import next_raid_time

        for slot in self.config.slots:
            yes, maybe, no, unset = [], [], [], []
            roles = {r: 0 for r in ("tank", "healer", "melee", "ranged")}
            try:
                start = next_raid_time(slot, self.config.timezone)
            except ValueError:
                start = None
            for m in self.members.values():
                if not m.main:
                    continue
                v = self.slot_pref(m, start, 3.0, slot) if start else m.slot_prefs.get(slot)
                {"yes": yes, "maybe": maybe, "no": no}.get(v, unset).append(m.display_name)
                if v in ("yes", "maybe"):
                    r = self.roles_of(m)[0]
                    if r in roles:
                        roles[r] += 1
            out.append({"slot": slot, "yes": yes, "maybe": maybe, "no": no, "unset": unset, "roles": roles})
        return out

    def set_availability(self, discord_id: int, team: str, value: str) -> Member:
        if value not in AVAILABILITY:
            raise RegistryError(f"Availability must be one of {', '.join(AVAILABILITY)}.")
        if team not in self.config.team_keys():
            raise RegistryError(f"Unknown team {team}. Teams: {', '.join(self.config.team_keys())}.")
        m = self.member(discord_id)
        m.availability[team] = value
        self.save(m, f"{m.display_name} availability {team}={value}")
        return m

    def add_absence(self, discord_id: int, start: str, end: str | None, reason: str | None, by: str, display_name: str | None = None) -> tuple[Member, Absence]:
        from datetime import date as _d

        try:
            s = _d.fromisoformat(start)
            e = _d.fromisoformat(end) if end else s
        except ValueError:
            raise RegistryError("Dates are YYYY-MM-DD.")
        if e < s:
            raise RegistryError("End is before start.")
        if (e - s).days > 120:
            raise RegistryError("Absences longer than 120 days: set availability to out instead.")
        m = self.member(discord_id, display_name, create=display_name is not None)
        a = Absence(start=s.isoformat(), end=e.isoformat(), reason=(reason or "").strip() or None, by=by)
        m.absences = [x for x in m.absences if not (x.start == a.start and x.end == a.end)] + [a]
        self.save(m, f"{m.display_name} absent {a.start}" + (f"→{a.end}" if a.end != a.start else "") + (f" (by {by})" if by != m.display_name else ""))
        return m, a

    def clear_absence(self, discord_id: int, start: str) -> Member:
        m = self.member(discord_id)
        before = len(m.absences)
        m.absences = [a for a in m.absences if a.start != start]
        if len(m.absences) == before:
            raise RegistryError(f"No absence starting {start}.")
        self.save(m, f"{m.display_name} cleared absence {start}")
        return m

    def absences_between(self, start: str, end: str) -> list[tuple[Member, Absence]]:
        out = []
        for m in self.members.values():
            for a in m.absences:
                if a.start <= end and a.end >= start:
                    out.append((m, a))
        return sorted(out, key=lambda ma: ma[1].start)

    def availability_summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {t: {"in": 0, "out": 0, "sub": 0, "unset": 0} for t in self.config.roster_keys()}
        for m in self.members.values():
            if not m.active():
                continue
            for t in out:
                out[t][m.availability.get(t, "unset")] += 1
        return out
