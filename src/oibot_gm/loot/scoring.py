"""Deterministic candidate building and base scoring for a drop.

base = upgrade_value × attendance_factor × rank_factor × (1 + wishlist_bonus) / loot_divisor
All factors are returned so the LLM (and the officer) can see the arithmetic."""
from __future__ import annotations

from datetime import date, timedelta

from ..models import Candidate, LootAward, Player
from ..profiles import GameProfile, Item, equippable


def candidates(
    profile: GameProfile,
    item: Item,
    roster: list[Player],
    awards: list[LootAward],
    wishlists: dict[str, list[dict]],
    raid_date: date,
) -> list[Candidate]:
    cfg = profile.loot
    window = timedelta(days=cfg["loot_divisor"]["window_days"])
    slot_cfg = cfg.get("slot_repeat", {"window_days": 7, "multiplier": 0.25})
    slot_window = timedelta(days=slot_cfg["window_days"])
    out: list[Candidate] = []
    for p in roster:
        spec = profile.spec(p.cls, p.spec)
        if not item.usable_by(spec):
            continue
        ok, _why = equippable(item, spec, profile)
        if not ok:  # tier data said yes, the game says no: never a candidate
            continue
        who = p.character or p.signup_name
        wl = next((w for w in wishlists.get(who, []) if w["item"] == item.id), None)
        tier = item.tier_for(spec)
        offspec = bool(wl and wl.get("offspec"))
        if tier is None and not offspec:
            continue  # usable but not an upgrade for this spec, and not asked for
        tier = tier or "B"
        hist = [a for a in awards if a.raider == who]
        already = any(a.item_id == item.id for a in hist)
        recent = [a for a in hist if raid_date - window <= a.received <= raid_date]
        recent_power = sum(a.total_weight for a in recent)

        upgrade = cfg["tier_weight"][tier] * profile.slot_weight(item, spec)
        if offspec:
            upgrade *= cfg["wishlist"]["offspec_multiplier"]
        att_factor = 0.5 + 0.5 * p.attendance
        rank_factor = cfg["rank_factor"].get("unknown" if p.unmapped else p.rank, cfg["rank_factor"]["unknown"])
        wl_bonus = cfg["wishlist"]["rank_bonus"].get(wl["rank"], 0.0) if wl else 0.0
        ld = cfg["loot_divisor"]
        eff = max(0.0, recent_power - ld["free_allowance_power"])
        divisor = 1 + ld["k"] * (eff ** ld["p"])
        # same slot again this week (incl. earlier tonight): strong discount unless nobody else wants it
        same_slot = [a for a in hist if raid_date - slot_window <= a.received <= raid_date and profile.items.get(a.item_id) and profile.items[a.item_id].slot == item.slot]
        slot_factor = slot_cfg["multiplier"] if same_slot else 1.0
        score = 0.0 if already else upgrade * att_factor * rank_factor * (1 + wl_bonus) * slot_factor / divisor
        out.append(
            Candidate(
                character=who,
                cls=p.cls,
                spec=p.spec,
                rank="unknown" if p.unmapped else p.rank,
                unmapped=p.unmapped,
                tier=tier,
                offspec=offspec,
                upgrade_value=round(upgrade, 3),
                attendance=round(p.attendance, 2),
                attendance_str=f"{p.attended}/{p.attendance_total}" if p.attendance_total else "n/a",
                recent_power=round(recent_power, 2),
                recent_items=[f"{profile.item_name(a.item_id)} ({a.tier}, {a.received.isoformat()})" for a in recent],
                wishlist_rank=wl["rank"] if wl else None,
                base_score=round(score, 3),
                factors={
                    "upgrade": round(upgrade, 3),
                    "attendance": round(att_factor, 3),
                    "rank": rank_factor,
                    "wishlist": round(1 + wl_bonus, 3),
                    "slot_repeat": slot_factor,
                    "divisor": round(divisor, 3),
                },
                already_has=already,
            )
        )
    out.sort(key=lambda c: -c.base_score)
    return out
