"""Decoded RXP guide texts → the route dataset the explorer and the summaries read (the toolkit's rxp_to_json, with the
game's tables — zone ids, continent rects, dungeon tags — passed in from profiles/<version>/guides.yaml instead of read
from files beside the script)."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class GuideGame:
    """The version's tables (GameProfile.guides). `continents` = uiMapID → (index, width, height, left, top); the index is
    the explorer's continent slot."""
    zones: dict[int, str]
    continents: dict[int, dict]
    dungeons: dict[str, str]
    dungeon_aliases: dict[str, str] = field(default_factory=dict)
    addon_folder: str = "forever"
    addon_interface: str = "16001, 11509"

    @classmethod
    def from_profile(cls, doc: dict) -> "GuideGame":
        return cls(zones={int(k): str(v) for k, v in (doc.get("zones") or {}).items()},
                   continents={int(k): dict(v) for k, v in (doc.get("continents") or {}).items()},
                   dungeons={str(k): str(v) for k, v in (doc.get("dungeons") or {}).items()},
                   dungeon_aliases={str(k).upper(): str(v) for k, v in (doc.get("dungeon_aliases") or {}).items()},
                   addon_folder=str(doc.get("addon_folder") or "forever"),
                   addon_interface=str(doc.get("addon_interface") or "16001, 11509"))

    def __post_init__(self):
        self.name2map = {v: k for k, v in self.zones.items()}
        self.cont_index = {mid: int(c["index"]) for mid, c in self.continents.items()}  # uiMapID → explorer slot
        self.rects = {int(c["index"]): (float(c["width"]), float(c["height"]), float(c["left"]), float(c["top"])) for c in self.continents.values()}
        self.alias = {n.upper(): t for t, n in self.dungeons.items()} | self.dungeon_aliases

    def dtag(self, s: str) -> str | None:
        u = s.upper()
        return u if u in self.dungeons else self.alias.get(u)

    def world_to_pct(self, inst: int, wx: float, wy: float) -> tuple[int, float, float]:
        w, h, l, t = self.rects[inst]
        return inst, round((l - wx) / w, 4), round((t - wy) / h, 4)


def clean(s: str) -> str:
    s = re.sub(r"\|c[0-9a-fA-F]{8}", "", s)
    s = re.sub(r"\|cRXP_([A-Z]+)_", lambda m: "{" + m.group(1).lower() + "}", s)
    s = s.replace("|r", "{/}")
    s = re.sub(r"\|T.*?\|t", "", s)
    s = re.sub(r"\{(\w+)\}\s*\{/\}", "", s)
    return s.strip()


def split_cond(line: str) -> tuple[str, str]:
    m = re.search(r"\s*<<\s*(.+)$", line)
    if m:
        return line[:m.start()].strip(), m.group(1).strip()
    return line.strip(), ""


def _num(x: str) -> bool:
    return bool(re.match(r"^-?\d", x))


def parse_guide(text: str, game: GuideGame) -> tuple[dict, list[dict]]:
    meta: dict = {"tags": []}
    steps: list[dict] = []
    cur = None
    last_zone = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        if re.match(r"^step\b", line, re.I):
            _, cond = split_cond(line)
            cur = {"cond": cond, "flags": {}, "lines": [], "pts": []}
            steps.append(cur)
            continue
        if cur is None:
            if line.startswith("<<"):
                meta["cond"] = line[2:].strip()
                continue
            m = re.match(r"^#(\w+)\s*(.*)", line)
            if m:
                k, v = m.group(1), m.group(2).strip()
                if k == "next":
                    meta["next"] = (meta.get("next", "") + ";" + v) if meta.get("next") else v
                elif k in ("name", "group", "subgroup", "defaultfor", "title", "version"):
                    meta[k] = split_cond(v)[0] if k == "group" else v
                elif k == "displayname":
                    dn, dc = split_cond(v)
                    if not dc or "disp" not in meta:
                        meta["disp"] = dn
                else:
                    meta["tags"].append(k)
            continue
        if line.startswith("#"):
            m = re.match(r"^#(\w+)\s*(.*)", line)
            if m:
                cur["flags"][m.group(1)] = m.group(2).strip() or True
            continue
        body, lcond = split_cond(line) if not line.startswith(">>") else (line, "")
        text_part = ""
        if ">>" in body:
            body, text_part = body.split(">>", 1)
            body, text_part = body.strip(), clean(text_part)
        if body.startswith(">>"):
            text_part, body = clean(body[2:]), ""
        el: dict = {"cmd": "", "args": "", "text": text_part}
        if lcond:
            el["cond"] = lcond
        if body.startswith("."):
            m = re.match(r"^\.(\w+)\s*(.*)", body)
            if m:
                el["cmd"], el["args"] = m.group(1), m.group(2).strip()
        elif body.startswith("+"):
            el["cmd"], el["text"] = "obj", clean(body[1:])
        elif body.startswith("*"):
            el["cmd"], el["text"] = "note", clean(body[1:])
        elif body:
            el["text"] = (clean(body) + " " + text_part).strip()
        cmd = el["cmd"]
        if cmd == "goto":
            a = [x.strip() for x in el["args"].split(",")]
            zone = a[0] if a and (not _num(a[0]) or "/" in a[0]) else None
            if zone is None and len(a) >= 2 and re.match(r"^\d{4}$", a[0]):
                zone = a[0]
            if zone:
                last_zone = zone
            else:
                zone = last_zone
            try:
                if zone and "/" in zone:
                    mid_s, inst_s = zone.split("/")
                    mid, inst = int(mid_s), int(inst_s)
                    nums = [x for x in a[1:] if _num(x)]
                    c, px, py = game.world_to_pct(inst, float(nums[0]), float(nums[1]))
                    pt = {"c": c, "x": px, "y": py, "m": mid}
                    if lcond:
                        pt["cond"] = lcond
                    if len(nums) > 2 and nums[2].lstrip("-").replace(".", "").isdigit() and float(nums[2]) < 0:
                        pt["dyn"] = 1
                    cur["pts"].append(pt)
                elif zone and zone.isdigit() and int(zone) in game.cont_index:
                    nums = [x for x in a if _num(x)]
                    px, py = float(nums[-2]) / 100, float(nums[-1]) / 100
                    if len(nums) >= 3 and a[0] == zone:
                        px, py = float(nums[1]) / 100, float(nums[2]) / 100
                    pt = {"c": game.cont_index[int(zone)], "x": round(px, 4), "y": round(py, 4), "m": int(zone)}
                    if lcond:
                        pt["cond"] = lcond
                    cur["pts"].append(pt)
                elif zone and (zone in game.name2map or (zone.isdigit() and int(zone) in game.zones)):
                    nums = [x for x in a[1:] if _num(x)]
                    pt = {"zone": game.name2map[zone] if zone in game.name2map else int(zone), "zx": float(nums[0]) / 100, "zy": float(nums[1]) / 100, "approx": 1}
                    if lcond:
                        pt["cond"] = lcond
                    cur["pts"].append(pt)
            except (ValueError, IndexError, KeyError):
                pass  # a coordinate the guide wrote in a shape the map can't place: the step keeps its text
        elif cmd == "dungeon":
            a = el["args"].split()[0] if el["args"] else ""
            skip = a.startswith("!")
            t = game.dtag(a.lstrip("!"))
            if t:
                if lcond:
                    el["dcond"] = lcond
                cur.setdefault("dskip" if skip else "dungeon", []).append({"t": t, "cond": lcond})
        elif cmd == "group":
            cur["group"] = True
        elif cmd == "solo":
            cur["solo"] = True
        elif cmd == "xp":
            m = re.match(r"(\d+)", el["args"])
            if m:
                cur["xp"] = int(m.group(1))
        if cmd == "goto":
            el["args"] = ""
        if not el["text"] and not el["cmd"]:
            continue
        cur["lines"].append(el)
    return meta, steps


def dataset(texts: list[tuple[str, str]], game: GuideGame) -> dict:
    """[(source 'starter' | 'paid', text)] → {guides, zones, dungeons, mapnames}: every guide with its steps, each step
    with a level estimate (interpolated between the guide's xp markers) and its map points; zone boxes from the points."""
    guides: list[dict] = []
    seen: set[str] = set()
    for src, t in texts:
        meta, steps = parse_guide(t, game)
        name = meta.get("name") or f"guide{len(guides) + 1}"
        if name in seen:
            continue
        seen.add(name)
        m = re.match(r"(\d+)-(\d+)", meta.get("disp", name))
        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        n = len(steps)
        marks = [(0, lo)] + [(i, s["xp"]) for i, s in enumerate(steps) if "xp" in s] + [(max(0, n - 1), max(hi, lo))]
        for i, s in enumerate(steps):
            for (i0, l0), (i1, l1) in zip(marks, marks[1:]):
                if i0 <= i <= i1:
                    s["lvl"] = round(l0 + (l1 - l0) * ((i - i0) / max(1, i1 - i0)), 2)
                    break
        nxt = []
        for opt in meta.get("next", "").split(";"):
            opt = opt.strip()
            if not opt:
                continue
            nm, cond = split_cond(opt)
            nxt.append({"name": nm.split("\\")[-1], "cond": cond})
        guides.append({"name": name, "disp": meta.get("disp", name), "group": meta.get("group", ""), "sub": meta.get("subgroup", ""),
                       "cond": meta.get("cond", ""), "src": src, "defaultfor": meta.get("defaultfor", ""), "next": nxt, "lo": lo, "hi": hi, "steps": steps})
    zb: dict[int, list] = defaultdict(lambda: [1, 1, 0, 0, None])
    for g in guides:
        for s in g["steps"]:
            for p in s["pts"]:
                if "m" in p and p["m"] not in game.cont_index:
                    b = zb[p["m"]]
                    b[0], b[1], b[2], b[3], b[4] = min(b[0], p["x"]), min(b[1], p["y"]), max(b[2], p["x"]), max(b[3], p["y"]), p["c"]
    zones = {}
    pad = 0.012
    for mid, b in zb.items():
        zones[mid] = {"name": game.zones.get(mid, str(mid)), "c": b[4], "x0": round(max(0, b[0] - pad), 4), "y0": round(max(0, b[1] - pad), 4),
                      "x1": round(min(1, b[2] + pad), 4), "y1": round(min(1, b[3] + pad), 4)}
    for g in guides:
        for s in g["steps"]:
            for p in s["pts"]:
                if "zone" in p and p["zone"] in zones:
                    z = zones[p["zone"]]
                    p["c"], p["m"] = z["c"], p["zone"]
                    p["x"] = round(z["x0"] + (z["x1"] - z["x0"]) * p["zx"], 4)
                    p["y"] = round(z["y0"] + (z["y1"] - z["y0"]) * p["zy"], 4)
                    del p["zone"], p["zx"], p["zy"]
            s["pts"] = [p for p in s["pts"] if "x" in p]
    return {"guides": guides, "zones": zones, "dungeons": dict(game.dungeons), "mapnames": {str(k): v for k, v in game.zones.items()}}


def summaries(data: dict, game: GuideGame) -> tuple[dict[str, str], list[dict]]:
    """({file name: markdown table}, rows) per guide group: guides, steps, zones visited in order, dungeons, walking km."""
    zones = data["zones"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for g in data["guides"]:
        groups[g["group"]].append(g)
    files: dict[str, str] = {}
    rows: list[dict] = []
    for grp, gs in groups.items():
        gs.sort(key=lambda g: (g["lo"], g["hi"], g["name"]))
        lines = [f"# {grp} — route summary\n", "| Guide | Steps | Zones visited (in order) | Dungeons | Walking |", "|---|---|---|---|---|"]
        grp_zones: list[str] = []
        grp_dungeons: set[str] = set()
        grp_walk = 0.0
        grp_steps = 0
        for g in gs:
            order: list[str] = []
            dungeons: set[str] = set()
            walk = 0.0
            last = None
            for st in g["steps"]:
                for d in st.get("dungeon", []):
                    dungeons.add(d["t"])
                for p in st["pts"]:
                    zn = zones.get(p["m"], {}).get("name") or zones.get(str(p["m"]), {}).get("name") or game.zones.get(p["m"]) or str(p["m"])
                    if not order or order[-1] != zn:
                        order.append(zn)
                    if last and last["c"] == p["c"] and p["c"] in game.rects:
                        w, h = game.rects[p["c"]][:2]
                        d = (((p["x"] - last["x"]) * w) ** 2 + ((p["y"] - last["y"]) * h) ** 2) ** 0.5
                        if d < 3000:
                            walk += d
                    last = p
            seen: dict[str, int] = {}
            disp: list[str] = []
            for zn in order:
                if zn in seen:
                    seen[zn] += 1
                else:
                    seen[zn] = 1
                    disp.append(zn)
            zs = " → ".join(z + (f" (×{seen[z]})" if seen[z] > 1 else "") for z in disp)
            km = walk * 0.9144 / 1000
            lines.append(f"| {g['disp']} | {len(g['steps'])} | {zs} | {', '.join(game.dungeons.get(d, d) for d in sorted(dungeons)) or '—'} | {km:.1f} km |")
            for z in disp:
                if z not in grp_zones:
                    grp_zones.append(z)
            grp_dungeons |= dungeons
            grp_walk += walk
            grp_steps += len(g["steps"])
        files[re.sub(r"[^A-Za-z0-9 ()-]+", "_", grp) + " - summary.md"] = "\n".join(lines) + "\n"
        rows.append({"group": grp, "guides": len(gs), "steps": grp_steps, "lo": min(g["lo"] for g in gs), "hi": max(g["hi"] for g in gs),
                     "zones": grp_zones, "dungeons": [game.dungeons.get(d, d) for d in sorted(grp_dungeons)], "km": round(grp_walk * 0.9144 / 1000, 1),
                     "starter": all(g["src"] == "starter" for g in gs)})
    rows.sort(key=lambda r: (r["lo"], r["hi"], r["group"]))
    return files, rows
