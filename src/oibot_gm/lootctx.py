"""Loot context for a registered guild (the real counterpart of the mock
GuildContext): everything the loot pipeline reads, sourced from the data repo."""
from __future__ import annotations

from pathlib import Path

import yaml

from .llm.provider import Provider
from .models import LootAward
from .policy import PolicyStore
from .profiles import GameProfile
from .registry import Registry
from .store import GitStore


class RegistryLootContext:
    def __init__(self, reg: Registry, store: GitStore, provider: Provider | None):
        self.reg = reg
        self.guild_key = reg.key
        self.guild = {"name": reg.config.name, "game_profile": reg.config.game_profile}
        self.profile: GameProfile = reg.profile
        self.store = store
        self.policy_store = PolicyStore(store, reg.key)
        self.provider = provider
        self.reload()

    def reload(self) -> None:
        rows = self.store.read_jsonl(Path(self.guild_key) / "ledger.jsonl")
        self.ledger: list[LootAward] = [LootAward(**{k: v for k, v in r.items() if k in LootAward.model_fields}) for r in rows]
        self.precedents: list[dict] = self.store.read_jsonl(Path(self.guild_key) / "precedents.jsonl")
        wl = self.store.root / self.guild_key / "wishlists.yaml"
        self.wishlists: dict[str, list[dict]] = (yaml.safe_load(wl.read_text()) or {}).get("wishlists", {}) if wl.exists() else {}

    @property
    def policy(self) -> str:
        return self.policy_store.read("loot")

    @property
    def persona(self) -> str:
        return self.policy_store.read("persona")

    @property
    def provenance(self) -> str:
        n_wl = len(self.wishlists)
        return (
            "- Roster, classes, specs, ranks: REAL, from the guild registry (members registered themselves; officers confirmed).\n"
            "- Loot history / 14d loot power: REAL, awards recorded through the bot or the in-game feed.\n"
            "- Attendance: from the weekly sheets once raids have run; may be empty early on.\n"
            f"- Item tiers per spec: from the game profile's item table (curated, unverified against the game); wishlists: {'guild file with ' + str(n_wl) + ' characters' if n_wl else 'none recorded'}.\n"
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
