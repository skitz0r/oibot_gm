"""A Raid-Helper event export → the guild's roster, two ways.

    uv run python scripts/raidhelper_roster.py event.json [profile] > test_roster.yaml      # the test bench's shape (§5.42)
    uv run python scripts/raidhelper_roster.py --members event.json [profile] > rows.json  # the real roster (§5.53)

Raid-Helper's "wowforever" template names SPECS, not classes (its className is Tank / Melee / Ranged / Healer /
Tentative / Absence); a spec name two classes share carries a 1 (Protection1 = the paladin's). Without a flag the rows
come out as {name, character, cls, spec, rank} — the shape `Registry.test_roster` reads from `<guild>/test_roster.yaml`
in the PRIVATE data repo, so `/gm test seed` rehearses against the guild's real signup shape. With `--members` they come
out as JSON rows {discord_id, name, character, cls, spec} for `import_members` (MCP) / POST /api/members/import: the
signup's `userId` is the person's Discord id, `name` the display name (the part before a "(Character)" or "/Alt"), and
no rank (a rank is a person's decision, D8; `--rank raider` adds one). A signup with no spec (an Absence) — and, for
members, one with no Discord id — is skipped and named on stderr. Real names never belong in this repo: the export and
the files it writes stay outside it."""
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


def split_name(name: str) -> tuple[str, str]:
    """A signup's name → (display name, character name): "Nick(Character)" (Raid-Helper's use_nicknames) names the
    character in the parentheses; "Nick/Alt" keeps the first; a character name is letters only, at most 12."""
    m = re.match(r"^(.*?)\((.*?)\)$", name.strip())
    display = (m.group(1) if m else name.split("/")[0]).strip() or name.strip()
    character = re.sub(r"[^A-Za-z]", "", m.group(2) if m else name.split("/")[0])[:12].capitalize()
    return display, character


def rows_from(signups: list[dict], profile: GameProfile, rank: str = "raider") -> tuple[list[dict], list[str]]:
    """The test roster rows and the signups skipped (no spec: an Absence, a bare Late)."""
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
        _display, character = split_name(name)
        rows.append({"name": name, "character": character or None, "cls": cls, "spec": sp, "rank": rank})
    return rows, skipped


def member_rows(signups: list[dict], profile: GameProfile, rank: str | None = None) -> tuple[list[dict], list[str]]:
    """The import rows for `Registry.import_members`: {discord_id, name, character, cls, spec} (+ rank when given); a
    signup without a Discord id is skipped and named."""
    rows, skipped = [], []
    for s in signups:
        uid = str(s.get("userId") or "").strip()
        got, bad = rows_from([s], profile)
        if bad:
            skipped += bad
            continue
        if not uid.isdigit():
            skipped.append(f"{s.get('name') or '?'} (no Discord id in the export)")
            continue
        display, character = split_name(str(s["name"]))
        row = {"discord_id": uid, "name": display, "character": character or display, "cls": got[0]["cls"], "spec": got[0]["spec"]}
        if rank:
            row["rank"] = rank
        rows.append(row)
    return rows, skipped


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = [a for a in argv[1:] if a.startswith("--")]
    members = "--members" in flags
    rank = next((f.split("=", 1)[1] for f in flags if f.startswith("--rank=")), None)
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    doc = json.loads(Path(args[0]).read_text())
    signups = doc.get("signUps", doc) if isinstance(doc, dict) else doc
    profile = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / (args[1] if len(args) > 1 else "forever"))
    if members:
        rows, skipped = member_rows(signups, profile, rank)
        sys.stdout.write(json.dumps(rows, indent=1) + "\n")
    else:
        rows, skipped = rows_from(signups, profile, rank or "raider")
        for r in rows:
            if not r["character"]:
                del r["character"]
        sys.stdout.write(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True))
    print(f"{len(rows)} rows" + (f"; skipped: {', '.join(skipped)}" if skipped else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
