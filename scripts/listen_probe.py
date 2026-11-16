"""Probe the listening question offline (design §5.59 / §5.61): each sentence below is judged by Jev against the live
LISTEN_QUESTION with an empty `memory`, exactly as the gate would see it in a general channel. Prints the percentage per
sentence and fails (exit 1) when a bait sentence scores under 0.8, a banter sentence over 0.15, or a NEWS sentence
(what someone has, learned or got — the bot reads professions from the armory, the owner 2026-11-16) reaches 0.5,
the line where the bot would add its 👀 — the retune guard.

    uv run python scripts/listen_probe.py            # the sets below
    uv run python scripts/listen_probe.py "a sentence"  # one sentence

Needs TYPESAFE_API_KEY in .env. Nothing is written anywhere."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from oibot_gm import discord_bot as db  # noqa: E402
from oibot_gm.llm import decisions  # noqa: E402

BAIT = ["does anyone have the pattern for devilsaur gauntlets?", "anyone able to enchant crusader on my sword?",
        "who can make a mooncloth bag", "when does Barrow Deeps run this week?", "what class is Rhoz playing?",
        "who is our jewelcrafter?", "i can't make thursday", "what day do the horde runs happen?"]
BANTER = ["lol nice pull", "gg everyone, great raid", "that boss is so annoying", "anyone watching the game tonight?",
          "brb food", "we wiped at 2% again, classic", "haha same", "did you see the new patch notes"]
NEWS = ["i just got the leatherworking pattern for hide of the wild", "i have every merchant favor enchant",
        "just hit 300 leatherworking on my hunter", "i can make devilsaur gauntlets now", "learned crusader today"]


def state(text: str) -> dict:
    return {"bot": db.LISTEN_BOT_WORDS, "guild": {"raids": ["Barrow Deeps"], "words": db.LISTEN_GLOSSARY},
            "channel": {"name": "#general", "purpose": "", "topic": "General chat about the game", "category": "Guild"},
            "recent": [], "memory": [], "message": {"author": "someone", "text": text, "reply_to_bot": False}}


async def main(argv: list[str]) -> int:
    dec = decisions.DecisionProvider(timeout=8.0)
    if not dec.available:
        print("TYPESAFE_API_KEY is not set", file=sys.stderr)
        return 2
    sets = [("bait", argv, None)] if argv else [("bait", BAIT, 0.8), ("banter", BANTER, 0.15), ("news", NEWS, 0.4999)]
    bad = 0
    for name, texts, line in sets:
        for t in texts:
            p = await dec.noul(state(t), db.LISTEN_QUESTION, db.LISTEN_CRITERIA, workload="listen")
            ok = line is None or (p is not None and (p >= line if name == "bait" else p <= line))
            bad += not ok
            print(f"{'' if p is None else f'{p:.0%}':>5}  {'ok ' if ok else 'BAD'}  [{name}] {t}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
