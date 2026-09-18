"""Shared literal values: one place for the role tuple, answer marks, level colours and the brand colour.
Values are copied from where they used to live (raidcycle / discord_raid / discord_pool / comp); don't change them
here without checking every site that renders them."""
from __future__ import annotations

ROLES = ("tank", "healer", "melee", "ranged")
RANK_ORDER = {"core": 0, "raider": 1, "trial": 2, "social": 3, "alt": 4}  # lower = seated first

# confirmation / fill-ask answer marks (None = still waiting)
MARK = {"yes": "✅", "no": "❌", "expired": "⌛", None: "⏳"}

# health levels → embed / container accent colours and the dot used in text
LEVEL_COLOUR = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}
LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}

TEAL = 0x2B7A78  # the bot's embed colour
