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
    name: Optional[str] = None  # None until the character exists (pre-launch "planned")
    cls: str
    spec: str
    offspec: Optional[str] = None
    is_main: bool = False
    rank: str = "trial"
    status: str = "active"  # planned | active | retired
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    note: Optional[str] = None  # officer-only

    @property
    def label(self) -> str:
        return self.name or f"{self.cls} ({self.spec})"


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
    signup_channel_id: Optional[int] = None  # where raid sheets are posted
    timezone: str = "America/Chicago"  # server time for schedules
    officer_roles: list[str] = Field(default_factory=list)
    # {key, name, size, schedule: "Tue 19:30", instance, cutoff_soft_hours, cutoff_hard_hours, open_days_before, reminders: dm|channel|none}
    raid_teams: list[dict] = Field(default_factory=list)

    def team_keys(self) -> list[str]:
        return [t["key"] for t in self.raid_teams] or ["main"]

    def team(self, key: str) -> Optional[dict]:
        return next((t for t in self.raid_teams if t["key"] == key), None)


class RegistryError(ValueError):
    pass


class Registry:
    def __init__(self, store: GitStore, guild_key: str, profile: GameProfile):
        self.store = store
        self.key = guild_key
        self.profile = profile
        self.members: dict[int, Member] = {}
        self.config = self.load_config()
        self.reload()

    # ---- persistence
    def load_config(self) -> GuildConfig:
        p = self.store.root / self.key / "guild.yaml"
        return GuildConfig(**yaml.safe_load(p.read_text()))

    def save_config(self, message: str) -> None:
        p = Path(self.key) / "guild.yaml"
        self.store.write_text(p, "# oibot_GM guild config — edit via /gm config or by PR.\n" + yaml.safe_dump(self.config.model_dump(), sort_keys=False))
        self.store.commit(f"{self.key}: {message}")

    def reload(self) -> None:
        d = self.store.root / self.key / "members"
        self.members = {}
        if d.exists():
            for f in d.glob("*.json"):
                m = Member.model_validate_json(f.read_text())
                self.members[m.discord_id] = m
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
                if c.name and c.name.lower() == n:
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
        m.characters = [c for c in m.characters if not (c.status == "planned" and c.is_main == is_main)]
        if is_main:
            for c in m.characters:
                c.is_main = False
        c = RegisteredCharacter(cls=cls, spec=spec, offspec=offspec, is_main=is_main, status="planned", rank="trial" if is_main else "alt")
        m.characters.append(c)
        self.save(m, f"{display_name} plans {slot}: {cls} {spec}{'/' + offspec if offspec else ''}")
        return m, c

    def set_roles(self, discord_id: int, display_name: str, primary: str, flex: list[str]) -> Member:
        roles = ("tank", "healer", "melee", "ranged")
        if primary not in roles or any(f not in roles for f in flex):
            raise RegistryError(f"Roles are {', '.join(roles)}.")
        m = self.member(discord_id, display_name, create=True)
        m.role_prefs = {"primary": primary, "flex": sorted({f for f in flex if f != primary})}
        self.save(m, f"{display_name} roles: {primary}" + (f" (+{', '.join(m.role_prefs['flex'])})" if m.role_prefs["flex"] else ""))
        return m

    def name_character(self, discord_id: int, name: str, slot: str = "main") -> tuple[Member, RegisteredCharacter]:
        """At launch: give a planned character its real name; it becomes active (pending confirmation)."""
        name = self.normalise_name(name)
        if self.find(name):
            raise RegistryError(f"{name} is already registered.")
        m = self.member(discord_id)
        c = next((c for c in m.planned() if c.is_main == (slot == "main")), None)
        if not c:
            raise RegistryError(f"You have no planned {slot} to name. Use /register instead.")
        c.name, c.status, c.updated_at = name, "active", now()
        self.save(m, f"{m.display_name} named planned {slot} → {name}")
        return m, c

    def plan_summary(self) -> dict:
        """Class/spec/role distribution across planned+active mains, plus flex and buff providers."""
        mains = [(m, m.main) for m in self.members.values() if m.main]
        by_cls: dict[str, list[str]] = {}
        by_role: dict[str, int] = {r: 0 for r in ("tank", "healer", "melee", "ranged")}
        flex: dict[str, list[str]] = {r: [] for r in by_role}
        for m, c in mains:
            by_cls.setdefault(c.cls, []).append(f"{m.display_name} · {c.spec}" + (f"/{c.offspec}" if c.offspec else ""))
            role = m.role_prefs.get("primary") or self.profile.spec(c.cls, c.spec).role
            by_role[role] = by_role.get(role, 0) + 1
            for f in m.role_prefs.get("flex", []):
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
    def add_character(self, discord_id: int, display_name: str, name: str, cls: str, spec: str, offspec: str | None, make_main: bool) -> tuple[Member, RegisteredCharacter]:
        name = self.normalise_name(name)
        if cls not in self.profile.classes:
            raise RegistryError(f"Unknown class {cls}.")
        spec = self.validate_spec(cls, spec)
        offspec = self.validate_spec(cls, offspec) if offspec else None
        hit = self.find(name)
        if hit:
            owner, _ = hit
            raise RegistryError(f"{name} is already registered" + (" to you." if owner.discord_id == discord_id else f" to {owner.display_name}. Ask an officer if that's wrong."))
        m = self.member(discord_id, display_name, create=True)
        c = RegisteredCharacter(name=name, cls=cls, spec=spec, offspec=offspec)
        if make_main or m.main is None:
            for other in m.characters:
                other.is_main = False
            c.is_main = True
            c.rank = "trial" if m.main is None else c.rank
        else:
            c.rank = "alt"
        m.characters.append(c)
        self.save(m, f"{display_name} registered {name} ({cls} {spec}{', main' if c.is_main else ', alt'})")
        return m, c

    def set_main(self, discord_id: int, name: str) -> tuple[Member, RegisteredCharacter, RegisteredCharacter | None]:
        m = self.member(discord_id)
        c = next((c for c in m.active() if (c.name or c.label).lower() == name.strip().lower()), None)
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
        c = next((c for c in m.active() if (c.name or c.label).lower() == name.strip().lower()), None)
        if not c:
            raise RegistryError(f"You have no active character named {name}.")
        c.spec = self.validate_spec(c.cls, spec)
        c.offspec = self.validate_spec(c.cls, offspec) if offspec else None
        c.updated_at = now()
        self.save(m, f"{m.display_name}: {c.label} spec {c.spec}{'/' + c.offspec if c.offspec else ''}")
        return c

    def retire(self, discord_id: int, name: str) -> RegisteredCharacter:
        m = self.member(discord_id)
        c = next((c for c in m.active() if (c.name or c.label).lower() == name.strip().lower()), None)
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

    # ---- team membership (the curated default roster per team)
    def team_add(self, discord_id: int, team: str, by: str, display_name: str | None = None) -> Member:
        if team not in self.config.team_keys():
            raise RegistryError(f"Unknown team {team}. Teams: {', '.join(self.config.team_keys())}.")
        m = self.member(discord_id, display_name, create=display_name is not None)
        if team not in m.teams:
            m.teams.append(team)
            self.save(m, f"{by} added {m.display_name} to team {team}")
        return m

    def team_remove(self, discord_id: int, team: str, by: str) -> Member:
        m = self.member(discord_id)
        if team in m.teams:
            m.teams.remove(team)
            self.save(m, f"{by} removed {m.display_name} from team {team}")
        return m

    def team_members(self, team: str) -> list[Member]:
        return [m for m in self.members.values() if team in m.teams]

    def team_pool(self, team: str) -> list[Member]:
        """Who a sheet expects: the team's members if any are set, else everyone with a main."""
        members = self.team_members(team)
        return members if members else [m for m in self.members.values() if m.main]

    # ---- availability & absences
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
        out: dict[str, dict[str, int]] = {t: {"in": 0, "out": 0, "sub": 0, "unset": 0} for t in self.config.team_keys()}
        for m in self.members.values():
            if not m.active():
                continue
            for t in out:
                out[t][m.availability.get(t, "unset")] += 1
        return out
