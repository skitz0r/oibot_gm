"""Deterministic candidate building and base scoring for a drop.

base = upgrade_value × attendance_factor × rank_factor × (1 + wishlist_bonus) / loot_divisor
All factors are returned so the LLM (and the officer) can see the arithmetic.

An item without tier data (a drop the tables don't know yet — every drop on WoW: Forever's first night, design §3.1
decision 4) is still scored: everyone rostered who can equip it is a candidate at `untiered_weight` (default 0.5),
ranked by attendance and recent loot only. Attendance nobody has tracked yet is neutral (factor 1.0), never a penalty.

Attendance (design §3.5 L35, D49) comes from Registry.attendance — done runs, rostered (+ confirmed on a recurring run),
every character of a member rolled up by Discord id — through `attendance_lookup`; when it is unknown (the window holds
fewer than ATTENDANCE_MIN_RUNS real runs, or the member had none) the factor stays 1.0 and the candidate says
`attendance_known: False`."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Optional

from ..models import Candidate, LootAward, Player
from ..profiles import GameProfile, Item, equippable

UNTIERED_WEIGHT = 0.5


def same_item(a: LootAward, item: Item) -> bool:
    """Is award `a` of this item? By id when the item has one; an item known only by name (id 0) matches by name, so
    two unknown items never collide on id 0."""
    if item.id:
        return a.item_id == item.id
    return not a.item_id and (a.item_name or "").strip().lower() == item.name.strip().lower()


def award_name(a: LootAward, profile: GameProfile, items: dict | None = None) -> str:
    if a.item_name:
        return a.item_name
    it = (items or {}).get(str(a.item_id)) if items else None
    return it.name if it else profile.item_name(a.item_id)


def _award_item(a: LootAward, profile: GameProfile, items: dict | None) -> Item | None:
    if items is not None:
        hit = items.get(str(a.item_id)) if a.item_id else None
        if hit is None and a.item_name:
            n = a.item_name.lower()
            hit = next((it for it in items.values() if it.name.lower() == n), None)
        return hit
    return profile.items.get(a.item_id)


def is_tiered(item: Item) -> bool:
    return bool(item.tiers)


def weight_for(profile: GameProfile, item: Item, spec, cfg: dict) -> tuple[str, float]:
    """(tier, upgrade weight) of `item` for a recipient's spec: the tier × slot weight; an untiered item scores
    `untiered_weight` (tier '?'); a tiered item with no tier for this spec counts as B (a known item awarded to a
    non-candidate, design §3.1 decision 4)."""
    if not is_tiered(item):
        return "?", float(cfg.get("untiered_weight", UNTIERED_WEIGHT))
    tier = (item.tier_for(spec) if spec is not None else None) or "B"
    sw = profile.slot_weight(item, spec) if spec is not None else 1.0
    return tier, round(cfg["tier_weight"].get(tier, cfg["tier_weight"].get("B", 0.05)) * sw, 3)


def attendance_factor(attended: int, total: int, cfg: dict) -> float:
    """The attendance factor: 1 - weight × 0.5 × (1 - ratio) — with the default weight 1.0 that is 0.5 + 0.5 × ratio
    (never zero, never above 1). `total` 0 = unknown → 1.0 (neutral)."""
    if not total:
        return 1.0
    w = float((cfg.get("attendance") or {}).get("weight", 1.0))
    return max(0.0, min(1.0, 1 - w * 0.5 * (1 - attended / total)))


def attendance_lookup(reg) -> Callable[[Player], Optional[tuple[int, int]]]:
    """Player → (attended, total) from Registry.attendance_known over the loot policy's window, or None when unknown.
    By the Player's Discord id; a sub kept by name (no id) is found by character name. Memoised per call site: one
    candidates() call reads each member once."""
    memo: dict = {}

    def look(p: Player) -> Optional[tuple[int, int]]:
        uid = p.discord_id
        if uid is None and p.character:
            try:
                hit = reg.find(p.character)
            except Exception:  # noqa: BLE001 — an ambiguous name is simply unknown
                hit = None
            uid = hit[0].discord_id if hit else None
        if uid is None:
            return None
        if uid not in memo:
            memo[uid] = reg.attendance_known(uid)
        return memo[uid]
    return look


def candidates(
    profile: GameProfile,
    item: Item,
    roster: list[Player],
    awards: list[LootAward],
    wishlists: dict[str, list[dict]],
    raid_date: date,
    *,
    cfg: dict | None = None,
    items: dict | None = None,
    reg=None,
    attendance: Callable[[Player], Optional[tuple[int, int]]] | None = None,
) -> list[Candidate]:
    """The candidate table for one drop. `cfg`: the guild's loot settings (default Registry.loot_cfg() with `reg`, else
    the profile's); `items`: the item lookup for past awards' slots (default Registry.items() with `reg`, else the
    profile's tables); `attendance`: Player → (attended, total) | None (default attendance_lookup(reg) with `reg`) —
    used for a Player that carries no attendance of its own (an imported roster's does)."""
    if reg is not None:
        cfg = cfg or reg.loot_cfg()
        items = items if items is not None else reg.items()
        attendance = attendance or attendance_lookup(reg)
    cfg = cfg or {**profile.loot, "untiered_weight": profile.loot.get("untiered_weight", UNTIERED_WEIGHT)}
    tiered = is_tiered(item)
    window = timedelta(days=cfg["loot_divisor"]["window_days"])
    slot_cfg = cfg.get("slot_repeat", {"window_days": 7, "multiplier": 0.25})
    slot_window = timedelta(days=slot_cfg["window_days"])
    out: list[Candidate] = []
    live = [a for a in awards if not a.void]
    for p in roster:
        try:
            spec = profile.spec(p.cls, p.spec)
        except KeyError:
            continue
        if tiered and not item.usable_by(spec):
            continue
        ok, _why = equippable(item, spec, profile)
        if not ok:  # tier data said yes (or there is none), the game says no: never a candidate
            continue
        who = p.character or p.signup_name
        wl = next((w for w in wishlists.get(who, []) if w.get("item") == item.id or (not item.id and str(w.get("item")).lower() == item.name.lower())), None)
        offspec = bool(wl and wl.get("offspec"))
        if tiered:
            tier = item.tier_for(spec)
            if tier is None and not offspec:
                continue  # usable but not an upgrade for this spec, and not asked for
            tier = tier or "B"
            upgrade = cfg["tier_weight"][tier] * profile.slot_weight(item, spec)
        else:
            tier = "?"
            upgrade = float(cfg.get("untiered_weight", UNTIERED_WEIGHT))
        hist = [a for a in live if a.raider == who]
        already = any(same_item(a, item) for a in hist)
        recent = [a for a in hist if raid_date - window <= a.received <= raid_date]
        recent_power = sum(a.total_weight for a in recent)

        if offspec:
            upgrade *= cfg["wishlist"]["offspec_multiplier"]
        if not p.attendance_total and attendance is not None:
            seen = attendance(p)
            if seen:
                p = p.model_copy(update={"attended": seen[0], "attendance_total": seen[1]})
        known_att = bool(p.attendance_total)
        att_factor = attendance_factor(p.attended, p.attendance_total, cfg)  # unknown attendance is neutral (decision 10)
        rank_factor = cfg["rank_factor"].get("unknown" if p.unmapped else p.rank, cfg["rank_factor"]["unknown"])
        wl_bonus = cfg["wishlist"]["rank_bonus"].get(wl["rank"], 0.0) if wl else 0.0
        ld = cfg["loot_divisor"]
        eff = max(0.0, recent_power - ld["free_allowance_power"])
        divisor = 1 + ld["k"] * (eff ** ld["p"])
        # same slot again this week (incl. earlier tonight): strong discount unless nobody else wants it; an item whose
        # slot nobody knows (untiered, learned by name) never counts as "the same slot"
        same_slot = []
        if item.slot and item.slot != "unknown":
            for a in hist:
                if raid_date - slot_window <= a.received <= raid_date:
                    ai = _award_item(a, profile, items)
                    if ai is not None and ai.slot == item.slot:
                        same_slot.append(a)
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
                attendance_str=f"{p.attended}/{p.attendance_total}" if known_att else "n/a",
                attendance_known=known_att,
                tiered=tiered,
                recent_power=round(recent_power, 2),
                recent_items=[f"{award_name(a, profile, items)} ({a.tier}, {a.received.isoformat()})" for a in recent],
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
