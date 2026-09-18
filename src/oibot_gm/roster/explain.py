"""Deterministic justification for a roster: bench what-ifs, tank/healer
advisories, unmapped signups. Everything here is computed, not generated.

Advisories are one-liners prefixed with a level dot (🔴/🟡/🟢) so cards and
tables can render them as rows; the long explanations go to `details`."""
from __future__ import annotations

from ..constants import RANK_ORDER
from ..models import Player, RosterResult
from ..profiles import GameProfile
from .solver import SolveOptions, solve

# bench what-ifs re-solve the whole model once per benched player: keep the total bounded
WHATIF_MAX_BENCH = 3  # only the highest-ranked / earliest-signed benched players get a number
WHATIF_TIME_LIMIT_S = 2.0  # per re-solve
WHATIF_MAX_ROSTERS = 3  # skip entirely for bigger splits …
WHATIF_MAX_PLAYERS = 30  # … or bigger sheets


def whatif_allowed(n_rosters: int, n_players: int) -> bool:
    return n_rosters <= WHATIF_MAX_ROSTERS and n_players <= WHATIF_MAX_PLAYERS


def annotate(profile: GameProfile, players: list[Player], raid_id: str, result: RosterResult, whatif: bool = True,
             whatif_limit: int = WHATIF_MAX_BENCH, whatif_time_s: float = WHATIF_TIME_LIMIT_S) -> RosterResult:
    raid = profile.raids[raid_id]
    adv: list[str] = []
    details: list[str] = []

    need = raid.get("tank_needs", {}).get("count")
    have = result.role_counts.get("tank", 0)
    if need and have < need:
        options = [p for p in players if p.tank_capable_main and p.role != "tank"]
        fix = ("; ".join(f"{p.signup_name} → {p.tank_capable_main}" for p in options[:2])
               or "no tank-capable main/offspec on the sheet")
        adv.append(f"{'🔴' if have < need - 1 else '🟡'} Tanks {have}/{need} · {fix}")
        if raid["tank_needs"].get("note"):
            details.append(f"Tanks: {raid['tank_needs']['note'].strip()}")

    unk = [p for p in result.selected if p.unmapped]
    if unk:
        adv.append(f"🟡 Unregistered ×{len(unk)} · " + ", ".join(p.signup_name for p in unk[:5])
                   + ("…" if len(unk) > 5 else ""))
        details.append("Unregistered signups are treated as trials: no attendance or loot history until an officer "
                       "confirms them.")
    low = [p for p in result.selected if not p.unmapped and p.map_confidence == "low"]
    if low:
        adv.append("🟡 Verify name match · " + ", ".join(f"{p.signup_name}→{p.character}" for p in low[:4]))
    alts = [p for p in result.selected if p.attendance_family and p.character != p.attendance_family]
    if alts:
        adv.append("🟢 On an alt · "
                   + ", ".join(f"{p.signup_name} ({p.character}, main {p.attendance_family})" for p in alts[:4]))
    if result.spec_switches:
        adv.append("🟡 Offspec to fill roles · "
                   + ", ".join(f"{n} → {s}" for n, s in result.spec_switches.items()))
        details.append("Offspec switches satisfy role minimums the signed specs couldn't; each costs a small objective "
                       "penalty so they're only used when needed.")

    whatif_map: dict[str, int] = {}
    candidates = (sorted(result.benched, key=lambda p: (RANK_ORDER.get(p.rank, 9), p.pos))[:max(0, whatif_limit)]
                  if whatif else [])
    for p in candidates:
        try:
            alt = solve(profile, players, raid_id, SolveOptions(force_in=(p.signup_name,), time_limit_s=whatif_time_s))
            whatif_map[p.signup_name] = alt.objective - result.objective
        except RuntimeError:
            whatif_map[p.signup_name] = -9999
    promoted = [p for p in result.selected if p.status == "bench"]
    if promoted:
        adv.append("🟢 Promoted from bench · "
                   + ", ".join(f"{p.signup_name} ({p.spec}, {p.attended}/{p.attendance_total})" for p in promoted[:3]))
    if whatif_map:
        ranked = sorted(whatif_map.items(), key=lambda kv: -kv[1])[:4]
        adv.append("🟢 Bench instead · " + ", ".join(f"{n} {d:+d}" for n, d in ranked))
        details.append("Bench numbers are the objective change if that player were forced into the raid "
                       "(negative = worse).")

    result.advisories = adv
    result.details = details
    result.bench_whatif = whatif_map
    return result
