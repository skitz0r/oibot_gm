"""A Raid-Helper event export → a guild's test roster (design §5.42).

    uv run python scripts/raidhelper_roster.py event.json > test_roster.yaml

Raid-Helper's "wowforever" template names SPECS, not classes (its className is Tank / Melee / Ranged / Healer /
Tentative / Absence); a spec name two classes share carries a 1 (Protection1 = the paladin's). The rows come out
as {name, character, cls, spec, rank} — the shape `Registry.test_roster` reads from `<guild>/test_roster.yaml` in
the PRIVATE data repo, so `/gm test seed` (or the Test bench's seed) rehearses against the guild's real signup
shape. A signup with no spec (an Absence) is skipped and named on stderr. Real names never belong in this repo:
the export and the roster file stay outside it."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oibot_gm.profiles import GameProfile  # noqa: E402

SPEC = {"Protection": ("Warrior", "Protection"), "Protection1": ("Paladin", "Protection"), "Guardian": ("Druid", "Guardian"),
        "Arms": ("Warrior", "Arms"), "Fury": ("Warrior", "Fury"), "Retribution": ("Paladin", "Retribution"), "Feral": ("Druid", "Feral"),
        "Assassination": ("Rogue", "Assassination"), "Combat": ("Rogue", "Combat"), "Subtlety": ("Rogue", "Subtlety"),
        "Enhancement": ("Shaman", "Enhancement"), "Elemental": ("Shaman", "Elemental"), "Restoration": ("Druid", "Restoration"),
        "Restoration1": ("Shaman", "Restoration"), "Holy": ("Priest", "Holy"), "Holy1": ("Paladin", "Holy"), "Discipline": ("Priest", "Discipline"),
        "Shadow": ("Priest", "Shadow"), "Smite": ("Priest", "Discipline"), "Arcane": ("Mage", "Arcane"), "Fire": ("Mage", "Fire"), "Frost": ("Mage", "Frost"),
        "Affliction": ("Warlock", "Affliction"), "Demonology": ("Warlock", "Demonology"), "Destruction": ("Warlock", "Destruction"),
        "Beastmastery": ("Hunter", "BeastMastery"), "Marksmanship": ("Hunter", "Marksmanship"), "Survival": ("Hunter", "Survival"),
        "Balance": ("Druid", "Balance")}


def rows_from(signups: list[dict], profile: GameProfile, rank: str = "raider") -> tuple[list[dict], list[str]]:
    """The roster rows and the signups skipped (no spec: an Absence, a bare Late)."""
    rows, skipped = [], []
    for s in signups:
        name, spec = str(s.get("name") or ""), s.get("specName")
        if not name or not spec or spec not in SPEC:
            skipped.append(f"{name or '?'} ({s.get('className') or '?'}: {'no spec' if not spec else 'unknown spec ' + str(spec)})")
            continue
        cls, sp = SPEC[spec]
        if sp not in profile.classes.get(cls, {}):
            skipped.append(f"{name} ({cls} {sp} isn't in this game version)")
            continue
        m = re.match(r"^(.*?)\((.*?)\)$", name)  # "Nick(Character)" — Raid-Helper's use_nicknames; "Nick/Alt" keeps the first
        character = re.sub(r"[^A-Za-z]", "", m.group(2) if m else name.split("/")[0])[:12].capitalize()
        rows.append({"name": name, "character": character or None, "cls": cls, "spec": sp, "rank": rank})
    return rows, skipped


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    doc = json.loads(Path(argv[1]).read_text())
    signups = doc.get("signUps", doc) if isinstance(doc, dict) else doc
    profile = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / (argv[2] if len(argv) > 2 else "forever"))
    rows, skipped = rows_from(signups, profile)
    for r in rows:
        if not r["character"]:
            del r["character"]
    sys.stdout.write(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True))
    print(f"{len(rows)} rows" + (f"; skipped: {', '.join(skipped)}" if skipped else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
