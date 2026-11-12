"""Blizzard Game Data API: item facts (name, quality, slot, armour class, class
restrictions, ilvl) and the game's recipe list (recipe_rows → profiles/<version>/recipes.yaml).
Client-credentials OAuth; namespace per game profile. Free tier, non-commercial, attribute
Blizzard; no loot sources for Classic."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from oibot_gm import battlenet
from oibot_gm.battlenet import MAX_PER_SECOND  # noqa: F401  (every network request of the process, whichever client sends it)

RETRIES = 3  # a 5xx or 429 is tried again after 1, 2, 4 s


def _pace() -> None:
    """At most MAX_PER_SECOND requests a second to Blizzard across the process (battlenet.LIMITER, shared with the armory)."""
    battlenet.LIMITER.acquire()

INVENTORY_TO_SLOT = {
    "HEAD": "head", "NECK": "neck", "SHOULDER": "shoulders", "CHEST": "chest", "ROBE": "chest", "WAIST": "waist", "LEGS": "legs",
    "FEET": "feet", "WRIST": "wrist", "HAND": "hands", "FINGER": "finger", "TRINKET": "trinket", "CLOAK": "back", "SHIELD": "shield",
    "WEAPON": "one_hand", "TWOHWEAPON": "two_hand", "WEAPONMAINHAND": "one_hand", "WEAPONOFFHAND": "one_hand", "HOLDABLE": "off_hand",
    "RANGED": "ranged_weapon_primary", "RANGEDRIGHT": "ranged_weapon_primary", "THROWN": "ranged_weapon_stat", "RELIC": "ranged_weapon_stat",
    "NON_EQUIP": "token",
}
ARMOR_SUBCLASS = {"Cloth": "cloth", "Leather": "leather", "Mail": "mail", "Plate": "plate", "Shields": "shield"}


@dataclass
class BlizzItem:
    id: int
    name: str
    quality: str
    slot: str
    type: str  # cloth|leather|mail|plate|shield|weapon subclass|token|misc
    ilvl: int
    classes: list[str]  # allowable classes (tokens), [] = any
    raw_inventory: str
    raw_class: str
    raw_subclass: str


class Blizzard:
    def __init__(self, region: str = "us", namespace: str = "static-classic-us", locale: str = "en_US", cache_dir: Path | None = None,
                 fetch: Callable[[str], dict] | None = None):
        """`fetch(url) -> dict` stands in for the network in get() (tests: recorded responses); then no credentials are needed."""
        self.cid = os.environ.get("BLIZZARD_CLIENT_ID")
        self.secret = os.environ.get("BLIZZARD_CLIENT_SECRET")
        self.fetch = fetch
        if fetch is None and (not self.cid or not self.secret):
            raise RuntimeError("BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET missing from .env (create a client at develop.battle.net/access/clients)")
        self.region, self.namespace, self.locale = region, namespace, locale
        self.cache_dir = cache_dir

    def token(self) -> str:
        """The process's one token (battlenet.TOKEN, shared with the armory)."""
        return battlenet.TOKEN.get()

    def get(self, path: str, namespace: str | None = None) -> dict:
        ns = namespace or self.namespace
        cache = self.cache_dir / f"{ns}{path.replace('/', '_')}.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text())
        url = f"https://{self.region}.api.blizzard.com{path}?namespace={ns}&locale={self.locale}"
        if self.fetch is not None:
            d = self.fetch(url)
        else:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token()}"})
            for attempt in range(RETRIES + 1):
                _pace()
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        d = json.load(r)
                    break
                except urllib.error.HTTPError as e:  # a passing 5xx / 429 is retried; anything else, or the last try, stops
                    if attempt == RETRIES or not (e.code == 429 or e.code >= 500):
                        raise
                    time.sleep(2 ** attempt)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(d))
        return d

    def item(self, item_id: int, namespace: str | None = None) -> BlizzItem:
        d = self.get(f"/data/wow/item/{item_id}", namespace)
        inv = (d.get("inventory_type") or {}).get("type", "")
        cls = (d.get("item_class") or {}).get("name", "")
        sub = (d.get("item_subclass") or {}).get("name", "")
        slot = INVENTORY_TO_SLOT.get(inv, "misc")
        if cls == "Armor":
            typ = ARMOR_SUBCLASS.get(sub, sub.lower() or "misc")
            if sub == "Miscellaneous":
                typ = {"neck": "neck", "finger": "ring", "trinket": "trinket", "back": "cloak", "off_hand": "offhand"}.get(slot, "misc")
        elif cls == "Weapon":
            typ = sub.lower().replace("one-handed ", "").replace("two-handed ", "").replace("s", "", 1) if sub.endswith("s") else sub.lower()
        else:
            typ = "token" if slot == "token" else cls.lower()
        classes = [c["name"] for c in (d.get("preview_item", {}).get("requirements", {}).get("playable_classes", {}).get("links") or [])]
        return BlizzItem(id=item_id, name=d.get("name", ""), quality=(d.get("quality") or {}).get("name", ""), slot=slot, type=typ, ilvl=int(d.get("level", 0)), classes=classes, raw_inventory=inv, raw_class=cls, raw_subclass=sub)


def verify_profile(profile_dir: Path, namespace: str, region: str = "us", cache_dir: Path | None = None) -> list[dict]:
    """Compare every item row in <profile>/items/*.yaml against Blizzard. Returns mismatches."""
    import yaml

    bz = Blizzard(region=region, namespace=namespace, cache_dir=cache_dir)
    out = []
    for f in sorted((profile_dir / "items").glob("*.yaml")):
        doc = yaml.safe_load(f.read_text()) or {}
        for row in doc.get("items", []):
            try:
                b = bz.item(int(row["id"]))
            except Exception as e:  # noqa: BLE001
                out.append({"id": row["id"], "name": row.get("name"), "problem": f"lookup failed: {str(e)[:80]}"})
                continue
            issues = []
            if b.name and b.name.lower() != str(row.get("name", "")).lower().split(" (")[0]:
                issues.append(f"name: {row.get('name')} → {b.name}")
            # profile conventions: tier tokens carry the reward slot; wands live in the ranged slot;
            # cloaks are 'cloak' here but 'Cloth' armour class in the game data
            is_token = bool(row.get("token_group")) or b.slot == "token"
            slot_ok = is_token or row.get("slot") == b.slot or (row.get("slot") == "wand" and b.slot == "ranged_weapon_primary") or (row.get("slot") in ("one_hand", "two_hand") and b.slot in ("one_hand", "two_hand") and row.get("slot") == b.slot)
            if b.slot != "misc" and not slot_ok:
                issues.append(f"slot: {row.get('slot')} → {b.slot}")
            armour_ok = row.get("type") == b.type or (row.get("type") == "cloak" and b.slot == "back") or is_token
            if b.type in ("cloth", "leather", "mail", "plate") and not armour_ok:
                issues.append(f"armour: {row.get('type')} → {b.type}")
            if issues:
                out.append({"id": row["id"], "name": row.get("name"), "problem": "; ".join(issues), "blizzard": b.__dict__})
    return out


def row_for(b: BlizzItem, boss: str) -> dict:
    """A profile item row with empty tiers (the guild tiers it later)."""
    return {"id": b.id, "name": b.name, "slot": b.slot, "type": b.type, "boss": boss, "ilvl": b.ilvl, "tiers": {}, "classes": b.classes or None}


# ---------------------------------------------------------------- the recipe list (armory link, spec §4.1)

TEST_PROFESSION = "Test Profession [DNT]"  # in the index of the build; never a profile's


def _shape(ok: bool, path: str, what: str) -> None:
    if not ok:
        raise ValueError(f"unexpected shape at {path}: {what}")


def _ref(d: object, path: str, what: str) -> dict:
    """A {key, name, id} reference as {id, name}; anything else stops the import."""
    _shape(isinstance(d, dict) and isinstance(d.get("id"), int) and isinstance(d.get("name"), str), path, what)
    return {"id": d["id"], "name": d["name"]}


def _quantity(v: object, path: str) -> int:
    """A whole, positive count (Blizzard sends 1.0); a fraction, zero or anything else stops the import."""
    _shape(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and float(v).is_integer(), path, "quantity")
    return int(v)


def _makes_quantity(cq: object, path: str) -> dict:
    """crafted_quantity → {quantity} for {value}, or {quantity: minimum, quantity_max: maximum} for a range that
    really varies ({minimum, maximum}, e.g. a bomb that makes 1–4). Any other shape, or none, stops the import."""
    _shape(isinstance(cq, dict), path, "crafted_quantity")
    if "value" in cq:
        return {"quantity": _quantity(cq["value"], path)}
    _shape("minimum" in cq and "maximum" in cq, path, "crafted_quantity")
    lo, hi = _quantity(cq["minimum"], path), _quantity(cq["maximum"], path)
    _shape(hi >= lo, path, "crafted_quantity range")
    return {"quantity": lo, "quantity_max": hi} if hi > lo else {"quantity": lo}


def build_of(doc: dict, namespace: str) -> str:
    """The game build a response came from: the part Blizzard puts into the namespace of its own links
    ('static-1.60.1_70399-forever-us' for 'static-forever-us' → '1.60.1_70399'); '' when the link has none."""
    href = ((doc.get("_links") or {}).get("self") or {}).get("href") or ""
    ns = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("namespace", [""])[0]
    head, _, tail = namespace.partition("-")
    m = re.fullmatch(rf"{re.escape(head)}-(.+)-{re.escape(tail)}", ns)
    return m.group(1) if m else ""


def recipe_row(d: dict, profession: str, category: str) -> dict:
    """One recipe response as a recipes.yaml row: {id, name, profession, category, makes {id, name, quantity
    [, quantity_max]} | None, reagents [{id, name, quantity}]}. An enchant makes no item: makes is None. quantity is the
    least made; quantity_max is there only when the game makes a varying count."""
    path = f"/data/wow/recipe/{d.get('id')}"
    _shape(isinstance(d.get("id"), int) and isinstance(d.get("name"), str) and bool(d["name"].strip()), path, "id / name")
    makes = None
    if d.get("crafted_item") is not None:
        makes = {**_ref(d["crafted_item"], path, "crafted_item"), **_makes_quantity(d.get("crafted_quantity"), path)}
    reagents = d.get("reagents") or []
    _shape(isinstance(reagents, list), path, "reagents")
    rows = []
    for r in reagents:
        _shape(isinstance(r, dict), path, "reagent")
        rows.append({**_ref(r.get("reagent"), path, "reagent"), "quantity": _quantity(r.get("quantity"), path)})
    return {"id": d["id"], "name": d["name"].strip(), "profession": profession, "category": category, "makes": makes, "reagents": rows}


def recipe_rows(bz: Blizzard, professions: dict[int, str]) -> tuple[list[dict], str]:
    """Every recipe of the profile's professions (`professions` = Blizzard id → profile profession id), walked
    profession index → profession → skill tier → recipe, as recipes.yaml rows sorted by profession, category, name;
    and the build string. The test profession and any profession not in `professions` are dropped. Every response is
    validated: an unexpected shape raises ValueError naming its path. Nothing is written here."""
    path = "/data/wow/profession/index"
    idx = bz.get(path)
    _shape(isinstance(idx.get("professions"), list), path, "professions")
    build = build_of(idx, bz.namespace)
    rows: dict[int, dict] = {}
    for p in idx["professions"]:
        ref = _ref(p, path, "profession")
        if ref["name"] == TEST_PROFESSION or ref["id"] not in professions:
            continue
        ppath = f"/data/wow/profession/{ref['id']}"
        tiers = bz.get(ppath).get("skill_tiers") or []
        _shape(isinstance(tiers, list), ppath, "skill_tiers")
        for t in tiers:
            tpath = f"{ppath}/skill-tier/{_ref(t, ppath, 'skill tier')['id']}"
            tier = bz.get(tpath)
            _shape(isinstance(tier.get("categories"), list), tpath, "categories")
            for c in tier["categories"]:
                _shape(isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("recipes"), list), tpath, "category")
                for r in c["recipes"]:
                    rid = _ref(r, tpath, "recipe")["id"]
                    if rid not in rows:  # a recipe listed twice keeps its first category
                        rows[rid] = recipe_row(bz.get(f"/data/wow/recipe/{rid}"), professions[ref["id"]], c["name"].strip())
    return sorted(rows.values(), key=lambda r: (r["profession"], r["category"], r["name"], r["id"])), build
