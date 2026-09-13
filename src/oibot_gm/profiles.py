"""GameProfile: everything version-specific, loaded from YAML. No game logic
lives in code that knows about TBC or Forever by name."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import SpecInfo


@dataclass
class Buff:
    id: str
    name: str
    providers: list[str]  # "Class:Spec" or "Class:*"
    scope: str
    kind: str
    stacking: str
    value: dict[str, float]
    note: str = ""

    def provided_by(self, spec: SpecInfo) -> bool:
        return any(p == spec.key or p == f"{spec.cls}:*" for p in self.providers)

    def benefit(self, spec: SpecInfo) -> float:
        """Highest matching value key; spec override wins even if lower."""
        override = self.value.get(f"spec:{spec.spec}")
        if override is not None:
            return float(override)
        best = 0.0
        for key, v in self.value.items():
            if key.startswith("spec:"):
                continue
            hit = (
                key == "all"
                or key == spec.role
                or (key == "physical" and spec.dmg == "physical")
                or (key == "spell" and spec.dmg == "spell")
                or (key == "mana" and spec.mana)
            )
            if hit:
                best = max(best, float(v))
        return best


@dataclass
class Item:
    id: int
    name: str
    slot: str
    type: str
    boss: str
    tiers: dict[str, str]
    usable: list[str] = field(default_factory=list)
    token_group: str | None = None
    note: str = ""

    def usable_by(self, spec: SpecInfo) -> bool:
        if not self.usable:  # derive from tiers when not listed
            return self.tier_for(spec) is not None
        return any(u == spec.key or u == f"{spec.cls}:*" or u == f"role:{spec.role}" for u in self.usable)

    def tier_for(self, spec: SpecInfo) -> str | None:
        """Most specific tier key wins: Class:Spec, then Class:*, then role:<role>."""
        for key in (spec.key, f"{spec.cls}:*", f"role:{spec.role}"):
            if key in self.tiers:
                return self.tiers[key]
        return None


ARMOR_RANK = {"cloth": 0, "leather": 1, "mail": 2, "plate": 3}


def equippable(item: "Item", spec: SpecInfo, profile: "GameProfile") -> tuple[bool, str]:
    """Structural guard independent of tier data: armour class and token group."""
    if item.type in ARMOR_RANK:
        cls_max = profile.armor.get(spec.cls)
        if cls_max and ARMOR_RANK[item.type] > ARMOR_RANK[cls_max]:
            return False, f"{spec.cls} cannot equip {item.type}"
    if item.token_group and spec.cls not in profile.token_groups.get(item.token_group, []):
        return False, f"{item.token_group} token is not for {spec.cls}"
    return True, ""


@dataclass
class GameProfile:
    name: str
    root: Path
    classes: dict[str, dict[str, dict[str, Any]]]
    spec_aliases: dict[str, str]
    token_groups: dict[str, list[str]]
    armor: dict[str, str]
    biscouncil: dict[str, Any]
    buffs: list[Buff]
    comp_rules: dict[str, Any]
    raids: dict[str, dict[str, Any]]
    items: dict[int, Item]
    known_items: dict[int, str]
    loot: dict[str, Any]
    _spec_cache: dict[str, SpecInfo] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> "GameProfile":
        def y(name: str) -> Any:
            return yaml.safe_load((root / name).read_text())

        c = y("classes.yaml")
        buffs = [Buff(**b) for b in y("buffs.yaml")["buffs"]]
        raids = {r["id"]: r for r in y("raids.yaml")["raids"]}
        items: dict[int, Item] = {}
        known: dict[int, str] = {}
        for f in sorted((root / "items").glob("*.yaml")):
            doc = yaml.safe_load(f.read_text())
            for it in doc.get("items", []):
                items[int(it["id"])] = Item(**it)
            known.update({int(k): v for k, v in doc.get("known_items", {}).items()})
        for it in items.values():
            known.setdefault(it.id, it.name)
        return cls(
            name=c["profile"],
            root=root,
            classes=c["classes"],
            spec_aliases=c.get("spec_aliases", {}),
            token_groups=c.get("token_groups", {}),
            armor=c.get("armor", {}),
            biscouncil=c.get("biscouncil", {}),
            buffs=buffs,
            comp_rules=y("comp_rules.yaml"),
            raids=raids,
            items=items,
            known_items=known,
            loot=y("loot_defaults.yaml"),
        )

    def spec(self, cls: str, spec: str) -> SpecInfo:
        spec = self.spec_aliases.get(spec, spec)
        key = f"{cls}:{spec}"
        if key not in self._spec_cache:
            try:
                attrs = self.classes[cls][spec]
            except KeyError as e:
                raise KeyError(f"unknown spec {key}") from e
            self._spec_cache[key] = SpecInfo(cls=cls, spec=spec, **attrs)
        return self._spec_cache[key]

    def party_buffs(self) -> list[Buff]:
        return [b for b in self.buffs if b.scope == "party" and b.kind == "aura" and b.providers]

    def item_name(self, item_id: int) -> str:
        return self.known_items.get(item_id, f"item {item_id}")

    def slot_weight(self, item: Item, spec: SpecInfo) -> float:
        sw = self.loot["slot_weight"]
        # Hunters use melee weapons as stat sticks; everyone else's ranged slot is stats too.
        if item.slot in ("two_hand", "one_hand") and spec.cls == "Hunter":
            return sw["ranged_weapon_stat"]
        return sw.get(item.slot, 1.0)
