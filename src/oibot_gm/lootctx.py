"""Loot contexts: everything the loot pipeline reads and the few things it writes, one interface for both kinds.

`RegistryLootContext` — a registered guild's ledger / precedents / wishlists / compiled policy / items (profile + the
guild's learned `<guild>/items/*.yaml`) / loot settings (`Registry.loot_cfg`), one per guild (`bot.loot_ctx(session)`
caches it; `reload()` after an import). `FixtureLootContext` — the same interface over the /mock flow's shadow
fixtures (`discord_bot.GuildContext`). A real guild's session NEVER reads the fixtures (design §3.1).

The ledger and precedents are append-only jsonl: a correction appends a `kind: void` row naming the award's
`import_id`; a replaced reason appends a new precedent with the same `import_id` (the last one is the active one)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from .llm.provider import Provider
from .models import LootAward
from .policy import PolicyStore
from .profiles import GameProfile, Item
from .registry import Registry
from .store import GitStore


def fold_ledger(rows: list[dict]) -> tuple[list[LootAward], set[str]]:
    """Ledger rows → (live awards, every import_id seen). A `kind: void` row voids the award with its import_id."""
    voided = {r.get("import_id") for r in rows if r.get("kind") == "void" and r.get("import_id")}
    awards, ids = [], set()
    for r in rows:
        if r.get("kind"):
            continue
        a = LootAward(**{k: v for k, v in r.items() if k in LootAward.model_fields})
        if a.import_id:
            ids.add(a.import_id)
            if a.import_id in voided:
                a.void = True
        awards.append(a)
    return awards, ids


def fold_precedents(rows: list[dict]) -> list[dict]:
    """Precedent rows with status applied: a later precedent with the same import_id supersedes the earlier one; a
    `kind: status` row sets the status of the precedent it names (import_id) — retire / restore."""
    out: list[dict] = []
    last: dict[str, dict] = {}
    for r in rows:
        if r.get("kind") == "status":
            tgt = last.get(r.get("import_id") or "")
            if tgt is not None:
                tgt["status"] = r.get("status", tgt.get("status"))
            continue
        r = dict(r)
        iid = r.get("import_id")
        if iid and iid in last:
            last[iid]["status"] = "superseded"
        if iid:
            last[iid] = r
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
        self.wishlists: dict[str, list[dict]] = (yaml.safe_load(wl.read_text()) or {}).get("wishlists", {}) if wl.exists() else {}

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

    def void(self, import_id: str, reason: str, by: str, at: str) -> None:
        self.store.append_jsonl(Path(self.guild_key) / "ledger.jsonl", {"kind": "void", "import_id": import_id, "reason": reason, "by": by, "at": at})
        self.ledger = [a for a in self.ledger if a.import_id != import_id]

    def append_precedent(self, row: dict) -> None:
        self.store.append_jsonl(Path(self.guild_key) / "precedents.jsonl", row)
        iid = row.get("import_id")
        for p in self.precedents:
            if iid and p.get("import_id") == iid and p.get("status", "active") == "active":
                p["status"] = "superseded"
        self.precedents.append(dict(row))

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

    @property
    def provenance(self) -> str:
        n_wl = len(self.wishlists)
        return (
            "- Roster, classes, specs, ranks: REAL, from the guild registry (members registered themselves; officers confirmed).\n"
            "- Loot history / 14d loot power: REAL, awards recorded through the bot or the in-game feed.\n"
            "- Attendance: from the weekly sheets once raids have run; may be empty early on (then it counts as neutral).\n"
            "- Item tiers per spec: from the game profile's item table and the guild's own tiers (curated, unverified against the game); "
            "an item without tier data is scored the same for everyone. "
            f"Wishlists: {'guild file with ' + str(n_wl) + ' characters' if n_wl else 'none recorded'}.\n"
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


class FixtureLootContext:
    """The /mock flow's context (shadow fixtures: the BisCouncil export + the bot's native rows under the fixtures'
    guild key) behind the same interface as RegistryLootContext. Learned items are never written for the fixtures."""

    def __init__(self, ctx, store: GitStore | None):
        self.ctx = ctx
        self.store = store
        self.guild_key = ctx.guild_key
        self.guild = ctx.guild
        self.profile: GameProfile = ctx.profile
        self.provider = ctx.provider
        self.ledger = ctx.ledger  # shared list: GuildContext's own readers see what the council records
        self.precedents = ctx.precedents
        self.wishlists = ctx.wishlists
        rows = store.read_jsonl(Path(self.guild_key) / "ledger.jsonl") if store else []
        self.import_ids = {r["import_id"] for r in rows if r.get("import_id")}

    @property
    def loot_cfg(self) -> dict:
        return {**self.profile.loot, "untiered_weight": self.profile.loot.get("untiered_weight", 0.5)}

    @property
    def items(self) -> dict:
        return {str(i): it for i, it in self.profile.items.items()}

    def item(self, ref=None, name: str | None = None):
        if ref not in (None, "", 0, "0"):
            hit = self.profile.items.get(int(ref))
            if hit is not None:
                return hit
        if name:
            n = name.strip().lower()
            return next((it for it in self.profile.items.values() if it.name.lower() == n), None)
        return None

    def learn(self, item_id, name, *, boss=None, instance=None, run=None, source="companion"):
        return self.item(item_id, name) or Item(id=int(item_id or 0), name=str(name or f"item {item_id}"), slot="unknown", type="unknown", boss=boss or "?", tiers={})

    def append_ledger(self, row: dict, award: LootAward | None = None) -> None:
        if self.store is not None:
            self.store.append_jsonl(Path(self.guild_key) / "ledger.jsonl", row)
        if row.get("import_id"):
            self.import_ids.add(row["import_id"])
        if award is not None and not row.get("kind"):
            self.ledger.append(award)

    def void(self, import_id: str, reason: str, by: str, at: str) -> None:
        if self.store is not None:
            self.store.append_jsonl(Path(self.guild_key) / "ledger.jsonl", {"kind": "void", "import_id": import_id, "reason": reason, "by": by, "at": at})
        self.ledger[:] = [a for a in self.ledger if a.import_id != import_id]

    def append_precedent(self, row: dict) -> None:
        if self.store is not None:
            self.store.append_jsonl(Path(self.guild_key) / "precedents.jsonl", row)
        self.precedents.append(dict(row))

    def precedent_for(self, import_id: str) -> dict | None:
        return next((p for p in reversed(self.precedents) if p.get("import_id") == import_id and p.get("status", "active") == "active"), None)

    def policy_version(self) -> str:
        return hashlib.sha1(self.ctx.policy.encode()).hexdigest()[:10]

    def policy_for_llm(self) -> str:
        return self.ctx.policy_for_llm()

    def reload(self) -> None:  # the fixtures are read once at start
        return None

