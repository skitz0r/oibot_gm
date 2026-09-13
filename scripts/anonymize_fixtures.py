"""Produce an anonymized copy of a guild fixture directory.

Every distinct player/character/Discord name is mapped to a generated fantasy
name, consistently across all files (YAML, JSON, CSV, Markdown), so the data
keeps its structure (classes, specs, alts, loot history, attendance) while no
real name survives. Guild name, realm and WCL guild id are replaced too.

    uv run python scripts/anonymize_fixtures.py fixtures/25bg fixtures/demo
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

SYL_A = ["ka", "ra", "vo", "ther", "mor", "el", "syl", "dra", "gor", "ith", "bal", "nym", "ash", "tor", "vel", "ori", "zan", "kel", "fen", "lua", "mir", "sor", "tal", "ver", "wyn"]
SYL_B = ["dan", "wen", "rik", "mund", "lith", "gar", "nis", "vex", "ros", "tam", "dur", "ael", "brin", "kos", "mael", "ryn", "thas", "ulf", "zor", "quil", "sha", "dil", "orn", "ex", "ian"]


def fake_name(real: str, taken: set[str]) -> str:
    h = int(hashlib.sha256(real.lower().encode()).hexdigest(), 16)
    for salt in range(50):
        x = h + salt * 7919
        name = (SYL_A[x % len(SYL_A)] + SYL_B[(x // 97) % len(SYL_B)] + (SYL_A[(x // 9973) % len(SYL_A)] if x % 3 == 0 else "")).capitalize()
        if name not in taken:
            taken.add(name)
            return name
    raise RuntimeError("name space exhausted")


def collect_names(src: Path) -> set[str]:
    names: set[str] = set()
    chars = yaml.safe_load((src / "characters.yaml").read_text())
    for c in chars["characters"]:
        names.add(c["name"])
        if c.get("main"):
            names.add(c["main"])
    for k, v in chars.get("signup_map", {}).items():
        names.add(k)
        if v.get("character"):
            names.add(v["character"])
    for f in src.glob("signup_*.yaml"):
        doc = yaml.safe_load(f.read_text())
        names.add(doc["event"].get("leader", ""))
        for s in doc["signups"]:
            names.add(s["name"])
    for f in src.glob("wcl_attendance_*.json"):
        for r in json.loads(f.read_text()):
            for p in r["players"]:
                names.add(p["name"])
    for f in src.glob("wcl_roster_*.json"):
        for r in json.loads(f.read_text()):
            names.add(r["name"])
    for f in src.glob("biscouncil_loot_*.csv"):
        for line in f.read_text().splitlines():
            if line.startswith("#") or line.startswith("Playername") or line.startswith("raider_name"):
                continue
            first = line.split(",")[0].strip()
            if first:
                names.add(first)
    wl = yaml.safe_load((src / "wishlists.yaml").read_text())
    names.update(wl.get("wishlists", {}).keys())
    names.discard("")
    # composite Discord names like "Middle(Alleeriah)": map the parts too
    for n in list(names):
        for part in re.split(r"[()\s]+", n):
            if part and part != n:
                names.add(part)
    return names


def build_map(names: set[str]) -> dict[str, str]:
    taken: set[str] = set()
    m: dict[str, str] = {}
    # map case-insensitively: "diza" and "Diza" get the same fake, keep the original casing style
    for n in sorted(names, key=lambda s: (-len(s), s)):
        key = n.lower()
        if key in m:
            continue
        base = fake_name(key, taken)
        m[key] = base
    return m


def replace_text(text: str, m: dict[str, str]) -> str:
    keys = sorted(m, key=len, reverse=True)
    pattern = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k in keys) + r")(?![A-Za-z])", re.IGNORECASE)

    def sub(match: re.Match) -> str:
        orig = match.group(0)
        fake = m[orig.lower()]
        if orig.islower():
            return fake.lower()
        if orig.isupper():
            return fake.upper()
        return fake

    return pattern.sub(sub, text)


def main(src: Path, dst: Path) -> None:
    names = collect_names(src)
    m = build_map(names)
    m.update({"25 big guys": "Demo Guild", "25bg": "demo", "nightslayer": "Demorealm"})
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_dir():
            continue
        text = f.read_text()
        out = replace_text(text, m)
        out = out.replace("wcl_guild_id: 813187", "wcl_guild_id: 0")
        (dst / f.name).write_text(out)
    (dst / "README.md").write_text("# demo fixtures\n\nAnonymized copy of a real TBC guild's shadow-mode data: names are generated, structure (classes, specs, alts, loot history, attendance) is real. Tiers, wishlists, policy and ranks are mock — see provenance.md.\n")
    print(f"{len(m)} names mapped → {dst}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
