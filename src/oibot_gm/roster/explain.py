"""Deterministic justification for a roster: bench what-ifs, tank/healer
advisories, unmapped signups. Everything here is computed, not generated.

Advisories are one-liners prefixed with a level dot (🔴/🟡/🟢) so cards and
tables can render them as rows; the long explanations go to `details`."""
from __future__ import annotations

from ..models import Player, RosterResult
from ..profiles import GameProfile
from .solver import SolveOptions, solve


def annotate(profile: GameProfile, players: list[Player], raid_id: str, result: RosterResult, whatif: bool = True) -> RosterResult:
    raid = profile.raids[raid_id]
    adv: list[str] = []
    details: list[str] = []

    need = raid.get("tank_needs", {}).get("count")
    have = result.role_counts.get("tank", 0)
    if need and have < need:
        options = [p for p in players if p.tank_capable_main and p.role != "tank"]
        fix = "; ".join(f"{p.signup_name} → {p.tank_capable_main}" for p in options[:2]) or "no tank-capable main/offspec on the sheet"
        adv.append(f"{'🔴' if have < need - 1 else '🟡'} Tanks {have}/{need} · {fix}")
        if raid["tank_needs"].get("note"):
            details.append(f"Tanks: {raid['tank_needs']['note'].strip()}")

    unk = [p for p in result.selected if p.unmapped]
    if unk:
        adv.append(f"🟡 Unregistered ×{len(unk)} · " + ", ".join(p.signup_name for p in unk[:5]) + ("…" if len(unk) > 5 else ""))
        details.append("Unregistered signups are treated as trials: no attendance or loot history until an officer confirms them.")
    low = [p for p in result.selected if not p.unmapped and p.map_confidence == "low"]
    if low:
        adv.append("🟡 Verify name match · " + ", ".join(f"{p.signup_name}→{p.character}" for p in low[:4]))
    alts = [p for p in result.selected if p.attendance_family and p.character != p.attendance_family]
    if alts:
        adv.append("🟢 On an alt · " + ", ".join(f"{p.signup_name} ({p.character}, main {p.attendance_family})" for p in alts[:4]))
    tent = [p for p in result.selected if p.note == "tentative"]
    if tent:
        adv.append(f"🟡 Tentative in roster ×{len(tent)} · " + ", ".join(p.signup_name for p in tent[:5]))

    whatif_map: dict[str, int] = {}
    for p in (result.benched if whatif else []):
        try:
            alt = solve(profile, players, raid_id, SolveOptions(force_in=(p.signup_name,), time_limit_s=8))
            whatif_map[p.signup_name] = alt.objective - result.objective
        except RuntimeError:
            whatif_map[p.signup_name] = -9999
    promoted = [p for p in result.selected if p.status == "bench"]
    if promoted:
        adv.append("🟢 Promoted from bench · " + ", ".join(f"{p.signup_name} ({p.spec}, {p.attended}/{p.attendance_total})" for p in promoted[:3]))
    if whatif_map:
        adv.append("🟢 Bench instead · " + ", ".join(f"{n} {d:+d}" for n, d in sorted(whatif_map.items(), key=lambda kv: -kv[1])[:4]))
        details.append("Bench numbers are the objective change if that player were forced into the raid (negative = worse).")

    result.advisories = adv
    result.details = details
    result.bench_whatif = whatif_map
    return result
