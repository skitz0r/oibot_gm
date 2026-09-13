"""Deterministic justification for a roster: bench what-ifs, tank/healer
advisories, unmapped signups. Everything here is computed, not generated."""
from __future__ import annotations

from ..models import Player, RosterResult
from ..profiles import GameProfile
from .solver import SolveOptions, solve


def annotate(profile: GameProfile, players: list[Player], raid_id: str, result: RosterResult, whatif: bool = True) -> RosterResult:
    raid = profile.raids[raid_id]
    adv: list[str] = []

    # Tank need for this instance vs what we have
    need = raid.get("tank_needs", {}).get("count")
    have = result.role_counts.get("tank", 0)
    if need and have < need:
        options = [p for p in players if p.tank_capable_main and p.role != "tank"]
        opt_txt = "; ".join(f"{p.signup_name} could bring {p.tank_capable_main}" for p in options) or "no signup has a known tank main/offspec"
        adv.append(f"{raid['name']} wants {need} tanks, roster has {have}. {raid['tank_needs'].get('note','').strip()} Options: {opt_txt}.")

    # Unmapped / low-confidence signups
    unk = [p for p in result.selected if p.unmapped]
    if unk:
        adv.append("Unregistered signups treated as trials (no attendance or loot history): " + ", ".join(f"{p.signup_name} ({p.cls} {p.spec})" for p in unk) + ".")
    low = [p for p in result.selected if not p.unmapped and p.map_confidence == "low"]
    if low:
        adv.append("Low-confidence name matches, verify: " + ", ".join(f"{p.signup_name}→{p.character}" for p in low) + ".")
    alts = [p for p in result.selected if p.attendance_family and p.character != p.attendance_family]
    if alts:
        adv.append("On an alt tonight (attendance rolled up to main): " + ", ".join(f"{p.signup_name} on {p.character} (main {p.attendance_family})" for p in alts) + ".")

    # Bench what-ifs: objective if each excluded player were forced in
    whatif: dict[str, int] = {}
    for p in (result.benched if whatif else []):
        try:
            alt = solve(profile, players, raid_id, SolveOptions(force_in=(p.signup_name,), time_limit_s=8))
            whatif[p.signup_name] = alt.objective - result.objective
        except RuntimeError:
            whatif[p.signup_name] = -9999
    promoted = [p for p in result.selected if p.status == "bench"]
    if promoted or whatif:
        parts = [f"promoted {p.signup_name} ({p.cls} {p.spec}, attendance {p.attended}/{p.attendance_total})" for p in promoted]
        alt_parts = [f"{n}: {d:+d}" for n, d in sorted(whatif.items(), key=lambda kv: -kv[1])]
        adv.append("Bench decision: " + ("; ".join(parts) if parts else "nobody promoted") + ". Objective delta if forced in instead — " + ", ".join(alt_parts) + ".")

    result.advisories = adv
    result.bench_whatif = whatif
    return result
