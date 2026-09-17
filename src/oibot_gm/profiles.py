"""GameProfile: everything version-specific, loaded from YAML. No game logic
lives in code that knows about TBC or Forever by name."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import SpecInfo


@dataclass
class Family:
    """A stacking family: buffs in the same family don't stack on a target — the strongest present one counts.
    The family also says who benefits (value keys: all | physical | spell | mana | melee | ranged | healer | tank | spec:<Name>)."""

    id: str
    name: str
    value: dict[str, float] = field(default_factory=dict)
    status: str = "assumed"
    note: str = ""

    def benefit(self, spec: SpecInfo) -> float:
        return _benefit(self.value, spec)


def _benefit(value: dict[str, float], spec: SpecInfo) -> float:
    """Highest matching value key; spec override wins even if lower."""
    override = value.get(f"spec:{spec.spec}")
    if override is not None:
        return float(override)
    best = 0.0
    for key, v in value.items():
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
class Buff:
    id: str
    name: str
    providers: list[str]  # "Class:Spec" or "Class:*"
    scope: str
    kind: str
    stacking: str
    value: dict[str, float] = field(default_factory=dict)  # own beneficiary map; empty = use the family's
    family: str | None = None  # stacking family id (defaults to the buff's own id: a family of one)
    strength: float = 1.0  # multiplier on the family's benefit (ranks, weaker variants)
    _family: "Family | None" = None  # resolved by the profile
    note: str = ""
    icon: dict[str, str] = field(default_factory=dict)  # {abbr, colour} for badges/emojis
    slot: str | None = None  # buffs sharing a slot are mutually exclusive per provider (totem elements)
    status: str = "assumed"  # confirmed | reported | assumed — how sure we are this is how the game version behaves
    choices: list[str] = field(default_factory=list)  # raid scope: each provider fills one of these, in priority order (blessings)
    wanted: int | None = None  # raid scope: providers for full coverage (default: 1, or len(choices))

    @property
    def max_benefit(self) -> float:
        vals = self.value or (self._family.value if self._family else {})
        return max([float(v) * float(self.strength) for v in vals.values()] or [0.0])

    @property
    def short(self) -> str:
        return self.name.split(" (")[0]

    @property
    def abbr(self) -> str:
        return self.icon.get("abbr") or "".join(w[0] for w in self.short.split() if w[0].isalpha())[:4]

    @property
    def colour(self) -> str:
        return self.icon.get("colour", "#98A3B5")

    @property
    def art(self) -> str | None:
        return self.icon.get("art")

    def provided_by(self, spec: SpecInfo) -> bool:
        return any(p == spec.key or p == f"{spec.cls}:*" for p in self.providers)

    @property
    def family_id(self) -> str:
        return self.family or self.id

    def benefit(self, spec: SpecInfo) -> float:
        """The buff's own beneficiary map when it has one, else the family's map × the buff's strength."""
        if self.value:
            return _benefit(self.value, spec) * float(self.strength)
        if self._family is not None:
            return self._family.benefit(spec) * float(self.strength)
        return 0.0


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
    icons: dict[str, dict[str, str]] = field(default_factory=dict)  # {classes, roles, specs('Class:Spec')} -> CDN icon names
    families: dict[str, Family] = field(default_factory=dict)  # stacking families; buffs without one get a family of their own
    _spec_cache: dict[str, SpecInfo] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> "GameProfile":
        def y(name: str) -> Any:
            return yaml.safe_load((root / name).read_text())

        c = y("classes.yaml")
        bdoc = y("buffs.yaml")
        buffs = [Buff(**b) for b in bdoc["buffs"]]
        families = {f["id"]: Family(**f) for f in bdoc.get("families", [])}
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
            icons=c.get("icons", {}),
            token_groups=c.get("token_groups", {}),
            armor=c.get("armor", {}),
            biscouncil=c.get("biscouncil", {}),
            buffs=buffs,
            families=families,
            comp_rules=y("comp_rules.yaml"),
            raids=raids,
            items=items,
            known_items=known,
            loot=y("loot_defaults.yaml"),
        )

    def __post_init__(self):
        self.link_families()

    def link_families(self) -> None:
        """Every buff resolves to a Family object: a declared one, or an implicit family of one built from its own value map."""
        for b in self.buffs:
            fid = b.family_id
            if fid not in self.families:
                self.families[fid] = Family(id=fid, name=b.short, value=dict(b.value), status=b.status, note="")
            b._family = self.families[fid]

    def with_overrides(self, buffs: dict[str, dict], families: dict[str, dict]) -> "GameProfile":
        """A copy with guild-level aura facts applied: per buff scope/family/strength/status/note; per family
        name/value/status/note (a family id that doesn't exist yet is created)."""
        import copy

        out = copy.copy(self)
        out.families = {k: copy.deepcopy(f) for k, f in self.families.items()}
        out.buffs = [copy.copy(b) for b in self.buffs]
        for fid, over in (families or {}).items():
            f = out.families.setdefault(fid, Family(id=fid, name=over.get("name") or fid.replace("_", " ").title()))
            for k in ("name", "status", "note"):
                if k in over and over[k] is not None:
                    setattr(f, k, over[k])
            if "value" in over and over["value"] is not None:
                f.value = {k: float(v) for k, v in over["value"].items()}
        for bid, over in (buffs or {}).items():
            b = next((x for x in out.buffs if x.id == bid), None)
            if not b:
                continue
            for k in ("scope", "family", "status", "note"):
                if k in over and over[k] is not None:
                    setattr(b, k, over[k])
            if over.get("strength") is not None:
                b.strength = float(over["strength"])
            if over.get("family"):
                b.value = {}  # moved into a family: the family's beneficiaries apply
        out._spec_cache = dict(self._spec_cache)
        out.link_families()
        return out

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

    def raid_buffs(self) -> list[Buff]:
        return [b for b in self.buffs if b.scope == "raid" and b.kind == "aura" and b.providers]

    def buff_assumptions(self) -> list[str]:
        """One line per scoping assumption the cards should disclose."""
        out = []
        slots = {b.slot for b in self.party_buffs() if b.slot}
        if slots:
            st = {b.status for b in self.party_buffs() if b.slot}
            out.append(f"totems: one per element per shaman, party-wide, static radius ({'/'.join(sorted(st))})")
        others = [b for b in self.party_buffs() if not b.slot]
        if others:
            out.append("auras/shouts: party-wide (" + ", ".join(f"{b.abbr} {b.status}" for b in others) + ")")
        rb = self.raid_buffs()
        if rb:
            out.append("cast buffs: raid-wide (" + ", ".join(f"{b.abbr} {b.status}" for b in rb) + ")")
        return out

    def item_name(self, item_id: int) -> str:
        return self.known_items.get(item_id, f"item {item_id}")

    def slot_weight(self, item: Item, spec: SpecInfo) -> float:
        sw = self.loot["slot_weight"]
        # Hunters use melee weapons as stat sticks; everyone else's ranged slot is stats too.
        if item.slot in ("two_hand", "one_hand") and spec.cls == "Hunter":
            return sw["ranged_weapon_stat"]
        return sw.get(item.slot, 1.0)
