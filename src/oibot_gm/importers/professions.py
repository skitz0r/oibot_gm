"""The professions import boundary (design §5.60): a paste from an addon export, or a wago.tools seed, becomes rows the
verb takes — {character, profession, skill, recipes: [{id?, name}]}. No format is known yet; the Me page shows no paste
box until FORMATS has one. Adding a format = one parser here and the box, nothing else."""
from __future__ import annotations

from typing import Callable

FORMATS: dict[str, Callable[[str], list[dict]]] = {}


class ImportError_(ValueError):
    """The refusal in words (never a traceback for a person)."""


def detect(text: str) -> str | None:
    for name, fn in FORMATS.items():
        try:
            fn(text)
            return name
        except ImportError_:
            continue
    return None


def parse(text: str) -> list[dict]:
    name = detect(text)
    if name is None:
        raise ImportError_("That isn't a professions export the bot can read yet — add professions on the Me page or with /me professions.")
    return FORMATS[name](text)
