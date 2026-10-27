"""Shared literal values: one place for the role tuple, answer marks, level colours and the brand colour.
Values are copied from where they used to live (raidcycle / discord_raid / discord_pool / comp); don't change them
here without checking every site that renders them."""
from __future__ import annotations

ROLES = ("tank", "healer", "melee", "ranged")
RANK_ORDER = {"core": 0, "raider": 1, "trial": 2, "social": 3, "alt": 4}  # lower = seated first

# confirmation / fill-ask answer marks (None = still waiting)
MARK = {"yes": "✅", "no": "❌", "expired": "⌛", None: "⏳"}
# what each mark means, in the glossary's words — the site's tooltips and the confirmation tally (design §5.56 phase 2);
# /api/meta carries both maps ("waiting" = the None key), so the site draws the Discord card's marks, never its own
MARK_WORDS = {"yes": "confirmed", "no": "can't make it", "expired": "no answer in time", None: "waiting"}

# a raid's (or a schedule's regulars') coverage level as a word about the guild, never a colour alone: the Readiness and
# Rosters chips (meta.level_words)
LEVEL_WORDS = {"green": "ready", "amber": "thin", "red": "short"}

# health levels → embed / container accent colours and the dot used in text
LEVEL_COLOUR = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}
LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}

TEAL = 0x2B7A78  # the bot's embed colour

# the one human-facing clock (the clock rule): 12-hour, no leading zero. Registry.local12 converts to the guild zone and
# formats with this; `fmt12` is the same formatting for a datetime already in the zone it should read in.
CLOCK12 = "%a %d %b %I:%M %p"


def fmt12(dt, fmt: str = CLOCK12) -> str:
    """'Tue 22 Sep 7:30 PM' / 'Wed 9 Dec 3:00 PM' for a datetime in its own zone: the hour's AND the day's leading zero
    stripped (a person says "the 9th", never "oh-nine"). The single implementation behind wizard.fmt12,
    raid_views.clock12, flows_config.fmt12, agents.clock12 and Registry.day_label; never `%H:%M` where a person reads."""
    import re

    return re.sub(r"\b0(\d) ([A-Z][a-z]{2})\b", r"\1 \2", re.sub(r"\b0(\d:\d\d [AP]M)", r"\1", dt.strftime(fmt)))
