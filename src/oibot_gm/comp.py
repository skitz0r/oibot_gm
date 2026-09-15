"""Pool-level composition analytics (no sheet, no LLM):

- pool_players: every planned/active main as a solver Player
- optimize: run the group solver on the pool at a roster's size, slot-aware coverage per group
- raid_buff_status: raid-wide cast buffs (how many providers; blessings filled in priority order)
- ideal_comp: a deterministic "desired comp" for a raid size from the buff matrix and comp rules,
  overlaid with officer targets (roster config `comp_targets`), each line carrying its justification
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Player, RosterResult
from .profiles import GameProfile
from .registry import Member, Registry, RegisteredCharacter
from .roster import coverage as cov_mod, solver

ROLES = ("tank", "healer", "melee", "ranged")


# ---------------------------------------------------------------- pool → players → groups

def pool_players(reg: Registry) -> list[Player]:
    players = []
    for i, (m, c) in enumerate(sorted(((m, m.main) for m in reg.members.values() if m.main), key=lambda mc: mc[0].display_name.lower())):
        role = reg.roles_of(m)[0] or reg.profile.spec(c.cls, c.spec).role
        players.append(Player(signup_name=m.display_name, pos=i + 1, status="signed", cls=c.cls, spec=c.spec, role=role, offspec=c.offspec,
                              character=c.name or m.display_name, map_confidence="high", unmapped=False, rank=c.rank))
    return players


ARCHETYPES = ("tank/heal", "melee", "ranged", "casters")


def archetype_groups(n_groups: int) -> list[str]:
    """Label per group: the classic layout seeds one group per archetype, extra groups go to the
    archetypes that grow with raid size (melee, casters, ranged, then heal); fewer than four fold together."""
    if n_groups >= 4:
        labels = list(ARCHETYPES)
        for extra in ("melee", "casters", "ranged", "tank/heal", "melee", "casters", "ranged", "tank/heal"):
            if len(labels) >= n_groups:
                break
            labels.append(extra)
        return labels[:n_groups]
    if n_groups == 3:
        return ["tank/heal", "melee", "ranged+casters"]
    if n_groups == 2:
        return ["tank/heal+casters", "melee+ranged"]
    return ["everyone"]


def archetype_of(profile: GameProfile, p: Player) -> str:
    s = profile.spec(p.cls, p.spec)
    if p.role in ("tank", "healer"):
        return "tank/heal"
    if p.role == "melee":
        return "melee"
    return "ranged" if s.dmg == "physical" else "casters"


def seed_preferences(profile: GameProfile, players: list[Player], labels: list[str]) -> dict[str, int]:
    """Soft 1-based group preference per player: their archetype's groups, filled round-robin in pool order."""
    slots: dict[str, list[int]] = {}
    for gi, lab in enumerate(labels, 1):
        for a in ARCHETYPES:
            if a in lab:
                slots.setdefault(a, []).append(gi)
    counters: dict[str, int] = {}
    prefs = {}
    for p in players:
        a = archetype_of(profile, p)
        gs = slots.get(a) or [1]
        i = counters.get(a, 0)
        prefs[p.signup_name] = gs[i % len(gs)]
        counters[a] = i + 1
    return prefs


def optimize(reg: Registry, roster: dict, time_limit_s: float = 6.0) -> tuple[list[Player], RosterResult | None, cov_mod.Coverage | None, list[str]]:
    """Groups for the pool at the roster's *full* size (empty slots stay visible), seeded by archetype:
    tanks and healers together, melee with the Enhancement shaman, hunters, casters. Seeds are soft;
    buff synergy can still move someone. Returns (players, result, coverage, group labels)."""
    players = pool_players(reg)
    size = int(roster.get("size") or reg.profile.comp_rules["raid_size"])
    n_groups = max(1, -(-size // reg.profile.comp_rules["group_size"]))
    labels = archetype_groups(n_groups)
    if not players:
        return players, None, None, labels
    raid_id = roster.get("instance") if roster.get("instance") in reg.profile.raids else next(iter(reg.profile.raids))
    bounds = solver.scaled_role_bounds(reg.profile.comp_rules, size)
    # the pool is not a sheet: role minimums are advisory, so relax them to what the pool can satisfy
    have = {r: sum(1 for p in players if p.role == r or (p.offspec and reg.profile.spec(p.cls, p.offspec).role == r)) for r in ROLES}
    role_min = {r: min(bounds[r]["min"], have[r]) for r in bounds}
    opts = solver.SolveOptions(raid_size=size, role_min=role_min, prefer_group=seed_preferences(reg.profile, players, labels), prefer_weight=12,
                               time_limit_s=time_limit_s, workers=4)
    try:
        result = solver.solve(reg.profile, players, raid_id, opts)
    except Exception:  # noqa: BLE001 — infeasible pools fall back to "no card"
        result = None
    if result is None:
        return players, None, None, labels
    return players, result, cov_mod.compute(reg.profile, players, result), labels


# ---------------------------------------------------------------- raid-wide buffs

def raid_buff_status(profile: GameProfile, players: list[Player]) -> list[dict]:
    """[{id, abbr, colour, name, status, providers, ok, detail}] for raid-scope buffs; blessings expand to choices."""
    out = []
    for b in profile.raid_buffs():
        provs = [p.character or p.signup_name for p in players if b.provided_by(profile.spec(p.cls, p.spec))]
        if b.choices:
            want = b.wanted or len(b.choices)
            filled = b.choices[: len(provs)]
            missing = b.choices[len(provs):want]
            detail = ", ".join(filled) + (f" · missing {', '.join(missing[:3])}" if missing else "")
            ok = "green" if not missing else ("amber" if len(filled) >= want // 2 else "red")
        else:
            detail = ", ".join(provs[:3]) + ("…" if len(provs) > 3 else "") if provs else "nobody"
            ok = "green" if len(provs) >= 2 else ("amber" if provs else "red")
        out.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "name": b.short, "status": b.status, "providers": provs, "ok": ok, "detail": detail, "choices": b.choices})
    return out


# ---------------------------------------------------------------- desired comp

@dataclass
class CompLine:
    key: str  # role name, "Class" or "Class:Spec"
    want: int
    have: int
    why: str
    max: int | None = None
    source: str = "derived"  # derived | officer
    level: str = "green"
    flex: int = 0  # mains whose offspec could fill this slot instead
    flex_who: list[str] = field(default_factory=list)


@dataclass
class IdealComp:
    size: int
    groups: int
    lines: list[CompLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _count(players: list[Player], key: str, profile: GameProfile) -> int:
    if key in ROLES:
        return sum(1 for p in players if p.role == key)
    if ":" in key:
        cls, spec = key.split(":", 1)
        return sum(1 for p in players if p.cls == cls and p.spec == spec)
    return sum(1 for p in players if p.cls == key)


def _flex(players: list[Player], key: str, profile: GameProfile) -> list[str]:
    """Who could fill the slot by switching to their offspec (not counted in have)."""
    out = []
    for p in players:
        if not p.offspec or p.offspec == p.spec:
            continue
        try:
            os_ = profile.spec(p.cls, p.offspec)
        except KeyError:
            continue
        if key in ROLES:
            if p.role != key and os_.role == key:
                out.append(f"{p.signup_name} ({p.offspec})")
        elif ":" in key:
            cls, spec = key.split(":", 1)
            if p.cls == cls and p.spec != spec and p.offspec == spec:
                out.append(f"{p.signup_name} ({p.offspec})")
    return out


def ideal_comp(profile: GameProfile, size: int, players: list[Player], targets: dict | None = None, instance: str | None = None) -> IdealComp:
    """Derived targets: role bounds (scaled), one provider per group for party auras that matter to most
    groups, enough providers for raid buffs (blessings = one per choice). Officer targets override."""
    rules = profile.comp_rules
    groups = -(-size // rules["group_size"])
    bounds = solver.scaled_role_bounds(rules, size)
    ic = IdealComp(size=size, groups=groups)
    tank_note = (profile.raids.get(instance or "", {}).get("tank_needs") or {}).get("note")
    tanks = (profile.raids.get(instance or "", {}).get("tank_needs") or {}).get("count") or bounds["tank"]["min"]
    ic.lines.append(CompLine("tank", tanks, 0, f"{'instance needs ' + str(tanks) if tank_note else 'comp rule min'} for a {size}-man" + (f" ({tank_note})" if tank_note else ""), max=bounds["tank"]["max"]))
    ic.lines.append(CompLine("healer", bounds["healer"]["min"], 0, f"comp rule: {rules['roles']['healer']['min']}/{rules['raid_size']} scaled to {size}", max=bounds["healer"]["max"]))
    # class wants from the buff matrix: party auras worth having (max benefit ≥ 3) want one provider per group
    # that has beneficiaries; the pool tells us how many such groups there are (else assume by role share)
    wants: dict[str, tuple[int, str]] = {}
    gsize = rules["group_size"]
    all_specs = [profile.spec(c, s) for c, specs in profile.classes.items() for s in specs]
    for b in profile.party_buffs():
        if not b.providers or b.kind != "aura" or b.max_benefit < 3:
            continue
        prov = b.providers[0]
        cls = prov.split(":")[0]
        if b.slot and cls == "Shaman":
            continue  # handled below as "one shaman per group"
        aud_roles = {s.role for s in all_specs if b.benefit(s) > 0}
        if players:
            bens = sum(1 for p in players if b.benefit(profile.spec(p.cls, p.spec)) > 0)
        else:
            share = sum(bounds[r]["min"] if r in bounds else size // 4 for r in aud_roles)
            bens = min(size, share)
        n = max(1, min(groups, -(-bens // gsize)))
        dmgs = {s.dmg for s in all_specs if b.benefit(s) > 0}
        aud = "caster" if dmgs <= {"spell"} else "physical" if dmgs <= {"physical"} else ("melee" if aud_roles <= {"tank", "melee"} else "ranged" if aud_roles <= {"ranged"} else "")
        key = prov if ":" in prov and not prov.endswith(":*") else cls
        cur = wants.get(key, (0, ""))
        if n > cur[0]:
            wants[key] = (n, f"{b.short} is party-wide → one per {aud + ' ' if aud else ''}group; {bens} in the pool benefit → {n} [{b.status}]")
    if any(b.slot for b in profile.party_buffs()):
        wants["Shaman"] = (groups, f"totems are party-wide → one shaman per group ({groups}); Enhancement with the melee for Windfury, Restoration with healers/casters [assumed]")
    for b in profile.raid_buffs():
        cls = b.providers[0].split(":")[0]
        n = b.wanted or (len(b.choices) if b.choices else 1)
        why = (f"{b.short}: one Greater Blessing per paladin → {', '.join(b.choices[:n])} = {n}" if b.choices else f"{b.short} is raid-wide → {n} provider{'s' if n > 1 else ''} (+1 for safety)") + f" [{b.status}]"
        cur = wants.get(cls, (0, ""))
        if n > cur[0]:
            wants[cls] = (n, why)
    for key, (n, why) in wants.items():
        ic.lines.append(CompLine(key, n, 0, why))
    # officer overrides
    for key, t in (targets or {}).items():
        line = next((l for l in ic.lines if l.key == key), None)
        want = int(t.get("min", line.want if line else 0))
        mx = t.get("max")
        why = t.get("note") or "officer target"
        if line:
            line.want, line.max, line.why, line.source = want, (int(mx) if mx is not None else line.max), why, "officer"
        else:
            ic.lines.append(CompLine(key, want, 0, why, max=int(mx) if mx is not None else None, source="officer"))
    # fill have + level
    for l in ic.lines:
        l.have = _count(players, l.key, profile)
        l.flex_who = _flex(players, l.key, profile)
        l.flex = len(l.flex_who)
        if l.max is not None and l.have > l.max:
            l.level = "amber"
        elif l.have >= l.want:
            l.level = "green"
        elif l.have + l.flex >= l.want or l.have >= max(1, l.want - 1):
            l.level = "amber"  # reachable with offspec switches, or one short
        else:
            l.level = "red"
    swaps = [f"{p.signup_name} {p.spec}→{p.offspec} ({profile.spec(p.cls, p.offspec).role})" for p in players if p.offspec and p.offspec != p.spec and p.cls in profile.classes and p.offspec in profile.classes[p.cls] and profile.spec(p.cls, p.offspec).role != p.role]
    order = {r: i for i, r in enumerate(ROLES)}
    ic.lines.sort(key=lambda l: (0 if l.key in ROLES else 1, order.get(l.key, 0), -l.want, l.key))
    ic.notes = ([f"offspec flexibility ({len(swaps)}): " + ", ".join(swaps[:8]) + ("…" if len(swaps) > 8 else "")] if swaps else ["offspec flexibility: nobody has registered an offspec in another role"]) + profile.buff_assumptions()
    return ic


