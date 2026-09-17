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
# label keywords → player archetype tokens; a label may combine several ("tank/heal", "melee+ranged")
TOKENS = {"tank": ("tank",), "heal": ("heal", "healer", "healers"), "melee": ("melee",), "ranged": ("ranged", "hunter", "hunters", "physical"), "caster": ("caster", "casters", "spell", "dps")}
FALLBACK = {"tank": ("heal", "melee"), "heal": ("tank", "caster"), "melee": ("ranged", "tank"), "ranged": ("caster", "melee"), "caster": ("ranged", "heal")}


def archetype_groups(n_groups: int, custom: list[str] | None = None) -> list[str]:
    """Label per group. Officers can set their own layout per roster (`comp_groups`); otherwise the classic
    layout seeds one group per archetype and extra groups go to the archetypes that grow with raid size
    (melee, casters, ranged, then heal); fewer than four fold together."""
    if custom:
        labels = [str(c).strip() for c in custom if str(c).strip()][:n_groups]
        pad = ("melee", "casters", "ranged", "tank/heal")
        while len(labels) < n_groups:
            labels.append(pad[(len(labels) - len(custom)) % len(pad)])
        return labels
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


def token_of(profile: GameProfile, p: Player) -> str:
    if p.role == "tank":
        return "tank"
    if p.role == "healer":
        return "heal"
    if p.role == "melee":
        return "melee"
    return "ranged" if profile.spec(p.cls, p.spec).dmg == "physical" else "caster"


def archetype_of(profile: GameProfile, p: Player) -> str:
    t = token_of(profile, p)
    return {"tank": "tank/heal", "heal": "tank/heal", "melee": "melee", "ranged": "ranged", "caster": "casters"}[t]


def label_tokens(label: str) -> set[str]:
    words = [w for w in label.lower().replace("/", " ").replace("+", " ").replace(",", " ").replace("-", " ").split() if w]
    return {tok for tok, kws in TOKENS.items() if any(w in kws for w in words)}


def seed_preferences(profile: GameProfile, players: list[Player], labels: list[str]) -> dict[str, int]:
    """Soft 1-based group preference per player: the groups whose label names their archetype, filled
    round-robin in pool order; archetypes with no group of their own fall back to the nearest one."""
    slots: dict[str, list[int]] = {}
    for gi, lab in enumerate(labels, 1):
        for tok in label_tokens(lab):
            slots.setdefault(tok, []).append(gi)
    counters: dict[str, int] = {}
    prefs = {}
    for p in players:
        tok = token_of(profile, p)
        gs = slots.get(tok)
        for fb in FALLBACK[tok]:
            if gs:
                break
            gs = slots.get(fb)
        gs = gs or list(range(1, len(labels) + 1))
        i = counters.get(tok, 0)
        prefs[p.signup_name] = gs[i % len(gs)]
        counters[tok] = i + 1
    return prefs


def players_from_seats(reg: Registry, seats: list[dict]) -> list[Player]:
    """Proposal seats ({discord_id, display_name, character, cls, spec, role}) → solver Players (offspec from the registry)."""
    out = []
    for i, s in enumerate(seats):
        m = reg.members.get(int(s["discord_id"]))
        c = next((c for c in (m.active() if m else []) if c.label == s["character"]), None)
        out.append(Player(signup_name=s["display_name"], pos=i + 1, status="signed", cls=s["cls"], spec=s["spec"], role=s["role"], offspec=(c.offspec if c else None),
                          character=s["character"], map_confidence="high", unmapped=False, rank=(c.rank if c else "trial")))
    return out


def groups_for(reg: Registry, players: list[Player], roster: dict, time_limit_s: float = 6.0) -> tuple[RosterResult | None, cov_mod.Coverage | None, list[str]]:
    """Groups for a fixed set of players at the roster's size (everyone selected), seeded by archetype."""
    size = int(roster.get("size") or reg.raid_def(roster.get("instance")).get("size") or reg.profile.comp_rules["raid_size"])
    n_groups = max(1, -(-size // reg.profile.comp_rules["group_size"]))
    labels = archetype_groups(n_groups, roster.get("comp_groups"))
    if not players:
        return None, None, labels
    raid_id = roster.get("instance") if roster.get("instance") in reg.profile.raids else next(iter(reg.profile.raids))
    bounds = reg.role_bounds(roster.get("instance"), size)
    have = {r: sum(1 for p in players if p.role == r or (p.offspec and reg.profile.spec(p.cls, p.offspec).role == r)) for r in ROLES}
    role_min = {r: min(bounds[r]["min"], have[r]) for r in ("tank", "healer")}
    opts = solver.SolveOptions(raid_size=size, role_min=role_min, prefer_group=seed_preferences(reg.profile, players, labels), prefer_weight=12,
                               force_in=tuple(p.signup_name for p in players), time_limit_s=time_limit_s, workers=4)
    try:
        result = solver.solve(reg.profile, players, raid_id, opts)
    except Exception:  # noqa: BLE001
        return None, None, labels
    return result, cov_mod.compute(reg.profile, players, result), labels


def groups_summary(reg: Registry, players: list[Player], result: RosterResult | None, cov) -> dict:
    """Everything the web needs to draw a run's groups and aura coverage without an image:
    roles, raid-wide buffs (level + detail), per group: members (class/spec/role/name), present badges,
    wanted-but-missing badges, totem picks; unmet buffs; scoping assumptions."""
    profile = reg.profile
    buffs = {b.id: b for b in profile.party_buffs()}
    roles = {r: sum(1 for p in players if p.role == r) for r in ROLES}
    rb = raid_buff_status(profile, players)
    out = {"roles": roles, "raid": [{"abbr": r["abbr"], "colour": r["colour"], "art": r.get("art"), "name": r["name"], "ok": r["ok"], "n": len(r["providers"]), "detail": r["detail"], "status": r["status"]} for r in rb],
           "groups": [], "unmet": [], "assumptions": profile.buff_assumptions(), "synergy": None}
    if result is None or cov is None:
        return out
    by = {p.signup_name: p for p in players}
    out["unmet"] = cov.unmet_raidwide
    out["synergy"] = result.synergy_value
    seated = [profile.spec(p.cls, p.spec) for p in result.selected]
    in_run = {bid for bid, b in buffs.items() if any(b.provided_by(sp) for sp in seated)}
    for gi, names in enumerate(result.groups):
        g = cov.groups[gi]
        present = [bid for bid in g.present if g.wanted.get(bid, 0) > 0]
        slot_taken = {buffs[bid].slot for bid in g.present if buffs[bid].slot}
        missing = [bid for bid, w in sorted(g.wanted.items(), key=lambda kv: -kv[1]) if w > 0 and bid not in g.present and not (buffs[bid].slot and buffs[bid].slot in slot_taken)]
        out["groups"].append({
            "n": gi + 1,
            "members": [{"name": by[n].character or n, "member": n, "cls": by[n].cls, "spec": by[n].spec, "role": by[n].role} for n in names],
            "present": [{"abbr": buffs[b].abbr, "colour": buffs[b].colour, "art": buffs[b].art, "name": buffs[b].short, "who": ", ".join(g.present[b][:2])} for b in present],
            "missing": [{"abbr": buffs[b].abbr, "colour": buffs[b].colour, "art": buffs[b].art, "name": buffs[b].short, "in_run": b in in_run} for b in missing],
            "picks": [f"{slot.split('_')[1].title()} {buffs[bid].abbr}" for slot, bid in sorted(g.picks.items()) if not slot.endswith("_cd") and g.wanted.get(bid, 0) > 0],
            "value": result.group_reports[gi].value if gi < len(result.group_reports) else 0,
        })
    return out


def profile_buff_abbr(profile: GameProfile, bid: str) -> str:
    return next((b.abbr for b in profile.party_buffs() if b.id == bid), bid)


def optimize(reg: Registry, roster: dict, time_limit_s: float = 6.0) -> tuple[list[Player], RosterResult | None, cov_mod.Coverage | None, list[str]]:
    """Groups for the pool at the roster's *full* size (empty slots stay visible), seeded by archetype:
    tanks and healers together, melee with the Enhancement shaman, hunters, casters. Seeds are soft;
    buff synergy can still move someone. Returns (players, result, coverage, group labels)."""
    players = pool_players(reg)
    size = int(roster.get("size") or reg.raid_def(roster.get("instance")).get("size") or reg.profile.comp_rules["raid_size"])
    n_groups = max(1, -(-size // reg.profile.comp_rules["group_size"]))
    labels = archetype_groups(n_groups, roster.get("comp_groups"))
    if not players:
        return players, None, None, labels
    raid_id = roster.get("instance") if roster.get("instance") in reg.profile.raids else next(iter(reg.profile.raids))
    bounds = reg.role_bounds(roster.get("instance"), size)
    # the pool is not a sheet: role minimums are advisory, so relax them to what the pool can satisfy
    have = {r: sum(1 for p in players if p.role == r or (p.offspec and reg.profile.spec(p.cls, p.offspec).role == r)) for r in ROLES}
    role_min = {r: min(bounds[r]["min"], have[r]) for r in ("tank", "healer")}
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
        out.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "art": b.art, "name": b.short, "status": b.status, "providers": provs, "ok": ok, "detail": detail, "choices": b.choices})
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
    if key == "dps":
        return sum(1 for p in players if p.role in ("melee", "ranged"))
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
        if key == "dps":
            if p.role not in ("melee", "ranged") and os_.role in ("melee", "ranged"):
                out.append(f"{p.signup_name} ({p.offspec})")
        elif key in ROLES:
            if p.role != key and os_.role == key:
                out.append(f"{p.signup_name} ({p.offspec})")
        elif ":" in key:
            cls, spec = key.split(":", 1)
            if p.cls == cls and p.spec != spec and p.offspec == spec:
                out.append(f"{p.signup_name} ({p.offspec})")
    return out


def ideal_comp(profile: GameProfile, size: int, players: list[Player], targets: dict | None = None, instance: str | None = None, reg=None) -> IdealComp:
    """Derived targets: role bounds (scaled), one provider per group for party auras that matter to most
    groups, enough providers for raid buffs (blessings = one per choice). Officer targets override."""
    rules = profile.comp_rules
    groups = -(-size // rules["group_size"])
    rd = reg.raid_def(instance) if reg else dict(profile.raids.get(instance or "", {}) or {})
    from_raid = bool(rd.get("comp"))
    bounds = reg.role_bounds(instance, size) if reg else solver.scaled_role_bounds(rules, size)
    ic = IdealComp(size=size, groups=groups)
    src = f"{rd.get('name', instance)} desired comp" if from_raid else "comp rule"
    note = (rd.get("tank_needs") or {}).get("note")
    ic.lines.append(CompLine("tank", bounds["tank"]["min"], 0, f"{src}: {bounds['tank']['min']}–{bounds['tank']['max']} for a {size}-man" + (f" ({note})" if note else ""), max=bounds["tank"]["max"]))
    ic.lines.append(CompLine("healer", bounds["healer"]["min"], 0, f"{src}: {bounds['healer']['min']}–{bounds['healer']['max']} for a {size}-man", max=bounds["healer"]["max"]))
    if from_raid and bounds.get("dps", {}).get("min"):
        ic.lines.append(CompLine("dps", bounds["dps"]["min"], 0, f"{src}: {bounds['dps']['min']}–{bounds['dps']['max']} damage dealers", max=bounds["dps"]["max"]))
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
    order = {r: i for i, r in enumerate(ROLES + ("dps",))}
    ic.lines.sort(key=lambda l: (0 if l.key in order else 1, order.get(l.key, 0), -l.want, l.key))
    ic.notes = ([f"offspec flexibility ({len(swaps)}): " + ", ".join(swaps[:8]) + ("…" if len(swaps) > 8 else "")] if swaps else ["offspec flexibility: nobody has registered an offspec in another role"]) + profile.buff_assumptions()
    return ic


