"""Loot contexts: everything the loot pipeline reads and the few things it writes, one interface for both kinds.

`RegistryLootContext` — a registered guild's ledger / precedents / wishlists / compiled policy / items (profile + the
guild's learned `<guild>/items/*.yaml`) / loot settings (`Registry.loot_cfg`), one per guild (`bot.loot_ctx(session)`
caches it; `reload()` after an import). The /mock flow's fixtures context is retired (D11): no session reads the
demo fixtures (design §3.1).

The ledger and precedents are append-only jsonl: a correction appends a `kind: void` row naming the award's
`import_id`; a replaced reason appends a new precedent with the same `import_id` (the last one is the active one)."""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import yaml

from .llm.provider import Provider
from .models import LootAward
from .policy import PolicyStore
from .profiles import GameProfile
from .registry import Registry, RegistryError
from .store import GitStore


log = logging.getLogger(__name__)
# fn(guild_key, award, row, was) after every NEW award row in a guild's ledger (Confirm, a companion award, a correction,
# the crash repair): the public loot log (discord_pool.LootLogMixin) posts one line per award. `was` = the recipient a
# correction replaced (else None). Called on the writer's thread, inside its batch — a listener only schedules work.
LEDGER_LISTENERS: list = []


def award_ref(a) -> str:
    """The key a later ledger row names an award by: its import_id, else (an imported row without one)
    'raider|item|received'."""
    if getattr(a, "import_id", None):
        return a.import_id
    return f"{a.raider}|{a.item_id or (a.item_name or '').lower()}|{a.received.isoformat()}"


def fold_ledger(rows: list[dict]) -> tuple[list[LootAward], set[str]]:
    """Ledger rows → (live awards, every import_id seen). A `kind: void` row voids the award with its import_id; a
    `kind: reweight` row (Loot › Items' "re-weight past awards", D12) sets the tier / weight of the award it names
    (`ref` = award_ref) — the record of who got what never changes, only what it weighs in recent-loot scores."""
    voided = {r.get("import_id") for r in rows if r.get("kind") == "void" and r.get("import_id")}
    weights = {r.get("ref"): r for r in rows if r.get("kind") == "reweight" and r.get("ref")}
    awards, ids = [], set()
    for r in rows:
        if r.get("kind"):
            continue
        a = LootAward(**{k: v for k, v in r.items() if k in LootAward.model_fields})
        if a.import_id:
            ids.add(a.import_id)
            if a.import_id in voided:
                a.void = True
        w = weights.get(award_ref(a))
        if w is not None:
            a.tier, a.total_weight, a.tiered = str(w.get("tier", a.tier)), float(w.get("total_weight", a.total_weight)), True
        awards.append(a)
    return awards, ids


def precedent_ref(r: dict, n: int) -> str:
    """How a precedent is named for Retire / Restore: its import_id, else 'row-<n>' (its place among the precedents —
    the file is append-only, so the place never changes)."""
    return r.get("import_id") or f"row-{n}"


def find_precedent(precedents: list[dict], ref: str) -> dict | None:
    """The precedent a ref names: after a replaced reason two rows share one import_id ref — the newest one that is not
    superseded is the live one (the one a status row folds onto), so Retire / Restore reach it; the superseded row is
    only returned when nothing else carries the ref."""
    rows = [p for p in precedents if p.get("ref") == ref]
    return next((p for p in reversed(rows) if p.get("status", "active") != "superseded"), rows[-1] if rows else None)


def fold_precedents(rows: list[dict]) -> list[dict]:
    """Precedent rows with status applied: a later precedent with the same import_id supersedes the earlier one; a
    `kind: status` row sets the status of the precedent it names (`import_id` = precedent_ref) — retire / restore,
    with who and why kept on the precedent (`status_by`, `status_reason`). Each row carries its `ref`."""
    out: list[dict] = []
    last: dict[str, dict] = {}
    refs: dict[str, dict] = {}
    for r in rows:
        if r.get("kind") == "status":
            tgt = refs.get(r.get("import_id") or "")
            if tgt is not None:
                tgt["status"] = r.get("status", tgt.get("status"))
                tgt["status_by"], tgt["status_reason"], tgt["status_at"] = r.get("by"), r.get("reason"), r.get("at")
            continue
        r = dict(r)
        r["ref"] = precedent_ref(r, len(out))
        iid = r.get("import_id")
        if iid and iid in last:
            last[iid]["status"] = "superseded"
        if iid:
            last[iid] = r
        refs[r["ref"]] = r
        out.append(r)
    return out


class RegistryLootContext:
    def __init__(self, reg: Registry, store: GitStore, provider: Provider | None):
        self.reg = reg
        self.guild_key = reg.key
        self.guild = {"name": reg.config.name, "game_profile": reg.config.game_profile}
        self.store = store
        self.policy_store = PolicyStore(store, reg.key)
        self.provider = provider
        self.reload()

    @property
    def profile(self) -> GameProfile:
        return self.reg.profile

    def reload(self) -> None:
        rows = self.store.read_jsonl(Path(self.guild_key) / "ledger.jsonl")
        all_awards, self.import_ids = fold_ledger(rows)
        self.ledger: list[LootAward] = [a for a in all_awards if not a.void]
        self.precedents: list[dict] = fold_precedents(self.store.read_jsonl(Path(self.guild_key) / "precedents.jsonl"))
        wl = self.store.root / self.guild_key / "wishlists.yaml"
        self._legacy_wishlists: dict[str, list[dict]] = (yaml.safe_load(wl.read_text()) or {}).get("wishlists", {}) if wl.exists() else {}

    @property
    def wishlists(self) -> dict[str, list[dict]]:
        """The map scoring expects ({character: [{item, rank, offspec}]}), derived live from the members' native
        wishlists (Registry.wishlists, D5); a legacy <guild>/wishlists.yaml fills in characters with no native list."""
        return {**self._legacy_wishlists, **self.reg.wishlists()}

    # ---- the after-the-fact writes (Loot › Precedents, Loot › Items)
    def set_precedent_status(self, ref: str, status: str, reason: str, by: str) -> dict:
        """Retire (status `retired`) or restore (`active`) a precedent: an appended `kind: status` row (append-only
        stays append-only); the bot cites active precedents only. Returns the precedent as it now reads."""
        from datetime import datetime, timezone

        if status not in ("retired", "active"):
            raise RegistryError("status is retired or active")
        hit = find_precedent(self.precedents, ref)
        if hit is None:
            raise RegistryError(f"No precedent {ref}.")
        if hit.get("status") == "superseded":
            raise RegistryError("That precedent was replaced by a later reason — retire the newer one.")
        if hit.get("status", "active") == status:
            raise RegistryError(f"That precedent is already {status}.")
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.append_jsonl(Path(self.guild_key) / "precedents.jsonl", {"kind": "status", "import_id": ref, "status": status, "by": by, "reason": reason, "at": at})
        hit["status"], hit["status_by"], hit["status_reason"], hit["status_at"] = status, by, reason, at
        return hit

    def awards_of_item(self, item) -> list[LootAward]:
        """Live ledger awards of one item (by id, or by name for an item known only by name)."""
        n = (item.name or "").lower()
        return [a for a in self.ledger if (item.id and a.item_id == item.id) or (not a.item_id and (a.item_name or "").lower() == n)]

    def reweight(self, item, by: str) -> int:
        """D12 "re-weight past awards": every live award of `item` gets the weight its tiers give now (tier for the
        recipient's spec × slot weight) through appended `kind: reweight` rows — who got what never changes. Returns
        the rows written."""
        from datetime import datetime, timezone

        from .loot import scoring

        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n = 0
        for a in self.awards_of_item(item):
            spec = None
            m = self.reg.members.get(a.discord_id) if a.discord_id else None
            hit = None
            if m is None:
                try:
                    hit = self.reg.find(a.raider)
                except RegistryError:
                    hit = None
                m = hit[0] if hit else None
            c = next((c for c in (m.characters if m else []) if c.label.lower() == a.raider.lower()), None)
            if c is not None:
                try:
                    spec = self.profile.spec(c.cls, c.spec)
                except KeyError:
                    spec = None
            tier, weight = scoring.weight_for(self.profile, item, spec, self.loot_cfg)
            if (tier, round(weight, 3)) == (a.tier, round(a.total_weight, 3)):
                continue
            self.store.append_jsonl(Path(self.guild_key) / "ledger.jsonl", {"kind": "reweight", "ref": award_ref(a), "tier": tier, "total_weight": weight, "by": by, "at": at,
                                                                            "was": {"tier": a.tier, "total_weight": a.total_weight}})
            a.tier, a.total_weight, a.tiered = tier, weight, True
            n += 1
        return n

    # ---- items and settings (the registry's, live)
    @property
    def loot_cfg(self) -> dict:
        return self.reg.loot_cfg()

    @property
    def items(self) -> dict:
        return self.reg.items()

    def item(self, ref=None, name: str | None = None):
        return self.reg.item(ref, name)

    def learn(self, item_id, name, *, boss=None, instance=None, run=None, source="companion"):
        return self.reg.learn_item(item_id, name, boss=boss, instance=instance, run=run, source=source)

    # ---- writes (append-only; the caller holds the session lock and a GitStore.batch)
    def append_ledger(self, row: dict, award: LootAward | None = None) -> None:
        self.store.append_jsonl(Path(self.guild_key) / "ledger.jsonl", row)
        if row.get("import_id"):
            self.import_ids.add(row["import_id"])
        if award is not None and not row.get("kind"):
            self.ledger.append(award)
            base = re.sub(r"-c\d+$", "", award.import_id or "")
            was = self.__dict__.setdefault("_voided", {}).pop(base, None) if award.source == "correct" else None
            for fn in list(LEDGER_LISTENERS):
                try:
                    fn(self.guild_key, award, row, was)
                except Exception:  # noqa: BLE001 — the award is recorded; a listener (the public log) never undoes it
                    log.exception("ledger listener failed")

    def void(self, import_id: str, reason: str, by: str, at: str) -> None:
        self.store.append_jsonl(Path(self.guild_key) / "ledger.jsonl", {"kind": "void", "import_id": import_id, "reason": reason, "by": by, "at": at})
        gone = next((a for a in self.ledger if a.import_id == import_id), None)
        if gone is not None:  # the correction appended next names who it replaced (the public log's "was X, now Y")
            self.__dict__.setdefault("_voided", {})[re.sub(r"-c\d+$", "", import_id)] = gone.raider
        self.ledger = [a for a in self.ledger if a.import_id != import_id]

    def append_precedent(self, row: dict) -> None:
        self.store.append_jsonl(Path(self.guild_key) / "precedents.jsonl", row)
        iid = row.get("import_id")
        for p in self.precedents:
            if iid and p.get("import_id") == iid and p.get("status", "active") == "active":
                p["status"] = "superseded"
        row = dict(row)
        # the same name fold_precedents gives it at load: its place among the precedent rows (status rows never land here)
        row["ref"] = precedent_ref(row, len(self.precedents))
        self.precedents.append(row)

    def precedent_for(self, import_id: str) -> dict | None:
        return next((p for p in reversed(self.precedents) if p.get("import_id") == import_id and p.get("status", "active") == "active"), None)

    # ---- policy
    @property
    def policy(self) -> str:
        return self.policy_store.read("loot")

    @property
    def persona(self) -> str:
        return self.policy_store.read("persona")

    def policy_version(self) -> str:
        """The loot policy an award was judged under: a short hash of its text (stamped on ledger and precedent rows)."""
        return hashlib.sha1(self.policy.encode()).hexdigest()[:10]

    def wishlist_provenance(self) -> str:
        """Where the wishlists the model reads come from, counted from the data (D5): entered through the bot (site or
        Discord — by the raider, or an officer for them), imported from ThatsMyBIS, or the older guild file."""
        native = tmb = 0
        labels: set[str] = set()
        for m in self.reg.members.values():
            for c in m.characters:
                if c.status == "retired" or not c.wishlist:
                    continue
                labels.add(c.label)
                native += any(e.source != "tmb" for e in c.wishlist)
                tmb += any(e.source == "tmb" for e in c.wishlist)
        legacy = sum(1 for k in self._legacy_wishlists if k not in labels)
        parts = []
        if native:
            parts.append(f"{native} character(s) with entries made through the bot (site or Discord, by the raider or an officer for them)")
        if tmb:
            parts.append(f"{tmb} with entries imported from a ThatsMyBIS export")
        if legacy:
            parts.append(f"{legacy} from an older guild wishlist file")
        return "; ".join(parts) if parts else "none recorded"

    @property
    def provenance(self) -> str:
        return (
            "- Roster, classes, specs, ranks: REAL, from the guild registry (members registered themselves; officers confirmed).\n"
            "- Loot history / 14d loot power: REAL, awards recorded through the bot or the in-game feed.\n"
            "- Attendance: from the weekly sheets once raids have run; may be empty early on (then it counts as neutral).\n"
            "- Item tiers per spec: from the game profile's item table and the guild's own tiers (curated, unverified against the game); "
            "an item without tier data is scored the same for everyone. "
            f"Wishlists: {self.wishlist_provenance()}.\n"
            "- Loot policy: the guild's own document, compiled and confirmed by an officer.\n"
            "Never describe tiers as something a raider submitted unless wishlists say so."
        )

    def policy_for_llm(self) -> str:
        text = self.policy
        compiled = self.policy_store.compiled("loot")
        if compiled and compiled.get("rules"):
            text += "\n\n## Compiled rules (cite these ids)\n" + "\n".join(f"[{r['id']}] {r['text']}" for r in compiled["rules"])
        text += "\n\n## Voice\n" + self.persona + "\n\n## Data provenance\n" + self.provenance
        recent = [p for p in self.precedents if p.get("status", "active") == "active"][-20:]
        if recent:
            text += "\n\n## Precedents from earlier raids (council overrides, with reasons; cite when relevant)\n" + "\n".join(f"- {p.get('date','?')} {p['item']}: bot picked {p['bot']}, council awarded {p['human']} — “{p['reason']}”" for p in recent)
        return text
