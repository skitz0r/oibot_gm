"""Raid-Helper signups → Players, resolved against the character registry.

Two sources are supported: the YAML fixture (transcribed) and, later, the
Raid-Helper API event JSON. Both end up in the same Player shape."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..models import Character, Player
from ..profiles import GameProfile
from .wcl import Attendance


def load_registry(path: Path) -> tuple[dict, dict[str, Character], dict[str, dict]]:
    doc = yaml.safe_load(path.read_text())
    chars = {c["name"]: Character(**c) for c in _drop_class_key(doc["characters"])}
    return doc["guild"], chars, doc.get("signup_map", {})


def _drop_class_key(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        r["cls"] = r.pop("class")
        out.append(r)
    return out


def load_signup(
    path: Path,
    profile: GameProfile,
    registry: dict[str, Character],
    signup_map: dict[str, dict],
    attendance: "Attendance",
) -> tuple[dict, list[Player]]:
    doc = yaml.safe_load(path.read_text())
    total = attendance.total
    # alt → main family for attendance roll-up
    family: dict[str, list[str]] = {}
    for c in registry.values():
        root = c.main or c.name
        family.setdefault(root, []).append(c.name)

    players: list[Player] = []
    for s in doc["signups"]:
        m = signup_map.get(s["name"], {})
        char_name = m.get("character")
        char = registry.get(char_name) if char_name else None
        spec = profile.spec_aliases.get(s["spec"], s["spec"])
        info = profile.spec(s["class"], spec)
        p = Player(
            signup_name=s["name"],
            pos=s["pos"],
            status=s["status"],
            cls=s["class"],
            spec=info.spec,
            role=info.role,
            character=char.name if char else None,
            map_confidence=m.get("confidence", "high" if char else "none"),
            spec_confidence=s.get("spec_confidence", "high"),
            unmapped=bool(m.get("unmapped", char is None)),
            note=m.get("note") or s.get("note"),
            rank=char.rank if char else "unknown",
        )
        if char:
            root = char.main or char.name
            members = family.get(root, [char.name])
            # a night counts once even if main and alt were both logged
            p.attended = attendance.count(members)
            p.attendance_total = total
            if len(members) > 1:
                p.attendance_family = root
            # can this person tank on a different character/offspec?
            main = registry.get(root)
            if main and main.name != char.name and profile.spec(main.cls, main.spec).role == "tank":
                p.tank_capable_main = f"{main.name} ({main.cls} {main.spec})"
            elif char.offspec and profile.spec(char.cls, char.offspec).role == "tank":
                p.tank_capable_main = f"{char.name} offspec {char.offspec}"
        players.append(p)
    return doc["event"], players
