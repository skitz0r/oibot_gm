"""Decoded RXP guide texts → readable Markdown, one document per guide group (the toolkit's rxp_to_md, as functions)."""
from __future__ import annotations

import re
from collections import defaultdict


def clean(s: str) -> str:
    s = re.sub(r"\|c[0-9a-fA-F]{8}", "", s)
    s = re.sub(r"\|cRXP_[A-Z]+_", "**", s)
    s = s.replace("|r", "**")
    s = re.sub(r"\*\*\s*\*\*", "", s)
    s = re.sub(r"\|c[0-9a-fA-F]{8}(.*?)\|r", r"\1", s)
    s = re.sub(r"\|T.*?\|t", "", s)
    s = s.replace("|r", "")
    return s.strip()


def parse_guide(text: str) -> tuple[dict, list[dict]]:
    meta: dict = {}
    steps: list[dict] = []
    cur = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if cur is None:
            m = re.match(r"^#(\w+)\s*(.*)", line)
            if m and line.lower() != "step":
                k, v = m.groups()
                if k in ("name", "next", "group", "subgroup", "displayname", "defaultfor", "title"):
                    meta[k] = v.strip()
                continue
            if line.startswith("<<"):
                meta["cond"] = line[2:].strip()
                continue
        if re.match(r"^step\b", line, re.I):
            cond = line[4:].strip()
            cond = cond[2:].strip() if cond.startswith("<<") else ""
            cur = {"tags": [], "cond": cond, "lines": []}
            steps.append(cur)
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
    return meta, steps


DEFAULTS = {
    "accept": lambda a: f"Accept quest #{a}",
    "turnin": lambda a: f"Turn in quest #{a}",
    "complete": lambda a: f"Complete quest #{a.split(',')[0]}" + (f" (objective {a.split(',')[1]})" if "," in a else ""),
    "xp": lambda a: f"Grind until level {a}",
    "train": lambda a: "Train new spells/abilities",
    "fly": lambda a: f"Fly to {a}",
    "fp": lambda a: f"Get the {a} flight path",
    "hs": lambda a: f"Hearthstone to {a}" if a else "Use your Hearthstone",
    "home": lambda a: f"Set your hearth at {a}" if a else "Set your hearth",
    "collect": lambda a: f"Collect {a.split(',')[1] if ',' in a else 1}x item #{a.split(',')[0]}",
    "use": lambda a: f"Use item #{a}",
    "vendor": lambda a: "Visit a vendor",
    "trainer": lambda a: "Visit the trainer",
    "abandon": lambda a: f"Abandon quest #{a}",
    "deathskip": lambda a: "Die and resurrect at the spirit healer to skip travel",
}
# commands that carry no text of their own: addon flags and conditions, not a line a person reads
SILENT = {"zoneskip", "subzoneskip", "target", "mob", "unitscan", "is", "zone", "subzone", "itemcount", "maxlevel", "link", "line",
          "waypoint", "timer", "cooldown", "skipgossip", "disablecheckbox", "aura", "cast", "group", "solo", "money", "item", "skill",
          "bindlocation", "dungeon", "stable", "destroy", "equip", "gossipoption", "bankwithdraw", "bankdeposit", "addquestitem",
          "reputation", "xpto", "bronzetube", "blasted", "usespell", "engrave", "dailyturnin", "daily"}
STEP_TAGS = {"completewith", "label", "requires", "optional", "sticky", "loop", "era", "som", "tbc", "season", "phase", "hardcore",
             "softcore", "ssf", "xprate", "ah", "hardcoreserver", "softcoreserver", "forever"}


def render_step(idx: int, step: dict) -> str:
    tags: list[str] = []
    body: list[tuple[str, str]] = []
    coords: list[str] = []
    for line in step["lines"]:
        if line.startswith("#"):
            m = re.match(r"^#(\w+)\s*(.*)", line)
            if m and m.group(1) in STEP_TAGS:
                tags.append(f"{m.group(1)} {m.group(2)}".strip())
            continue
        lcond = ""
        if "<<" in line and not line.startswith(">>"):
            line, lcond = line.rsplit("<<", 1)
            line, lcond = line.strip(), lcond.strip()
        if line.startswith(">>"):
            body.append((clean(line[2:]), lcond))
            continue
        m = re.match(r"^\.(\w+)\s*(.*)", line)
        if not m:
            body.append((clean(line), lcond))
            continue
        cmd, rest = m.groups()
        text = ""
        if ">>" in rest:
            args, text = rest.split(">>", 1)
            args, text = args.strip(), clean(text)
        else:
            args = rest.strip()
        if cmd == "goto":
            parts = args.split(",")
            if len(parts) >= 3:
                coords.append(f"{parts[1]}, {parts[2]}")
            if text:
                body.append((text, lcond))
            continue
        if text:
            body.append((text, lcond))
            continue
        if cmd in SILENT:
            continue
        d = DEFAULTS.get(cmd, lambda a, _c=cmd: f"{_c} {a}")
        body.append((d(args), lcond))
    out = [f"**{idx}.**"]
    if step["cond"]:
        out[0] += f" *(only if: {step['cond']})*"
    for t, lc in body:
        out.append(f"   - {t}" + (f" *(if {lc})*" if lc else ""))
    if coords:
        out.append("   - 📍 " + " → ".join(dict.fromkeys(coords)))
    if tags:
        out.append(f"   - <sub>{'; '.join(tags)}</sub>")
    return "\n".join(out)


def _order_key(n: str):
    m = re.match(r"(\d+)-(\d+)", n)
    return (int(m.group(1)), int(m.group(2)), n) if m else (999, 999, n)


def render(texts: list[str]) -> dict[str, str]:
    """{file name: markdown} — one document per guide group, its guides in level order with a contents list."""
    guides: dict[str, tuple[dict, list[dict]]] = {}
    for i, t in enumerate(texts, 1):
        meta, steps = parse_guide(t)
        guides[meta.get("name") or f"guide{i}"] = (meta, steps)
    groups: dict[str, list[str]] = defaultdict(list)
    for n, (m, _) in guides.items():
        groups[m.get("group", "?")].append(n)
    out: dict[str, str] = {}
    for g, names in groups.items():
        order = sorted(names, key=_order_key)
        md = [f"# {g}\n",
              "Legend: 📍 = in-game world coordinates (x, y) used by the addon's arrow · *(only if …)* = step applies only to that "
              "faction/class/race · <sub>tags</sub> = addon flags (optional, completewith, etc.)\n",
              "## Contents\n"]
        for n in order:
            md.append(f"- [{n}](#{re.sub(r'[^a-z0-9]+', '-', n.lower()).strip('-')})")
        md.append("")
        for n in order:
            meta, steps = guides[n]
            md.append(f"\n---\n\n## {n}\n")
            info = [f"For: {meta['cond']}" if "cond" in meta else "", f"Next: {meta['next']}" if "next" in meta else ""]
            md.append(" · ".join(i for i in info if i) + "\n")
            for i, st in enumerate(steps, 1):
                md.append(render_step(i, st) + "\n")
        out[re.sub(r"[^A-Za-z0-9 ()-]+", "_", g) + ".md"] = "\n".join(md)
    return out
