"""Image rendering for Discord: the roster/coverage card (same visual language
as the HTML report) and small class/role badge emojis. Pillow only; no game art."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Player, RosterResult
from .profiles import GameProfile
from .roster import coverage as cov_mod

# dark-theme tokens from the HTML report
BG, PANEL, INK, MUTED, LINE = "#14181F", "#1C222B", "#E8ECF2", "#98A3B5", "#2C3441"
OK, OK_SOFT, WARN, WARN_SOFT, NA, BARBG = "#3FC2B1", "#183C39", "#E5B04A", "#3E2F12", "#3A4351", "#2A323D"
CLASS = {"Warrior": "#C79C6E", "Paladin": "#F58CBA", "Hunter": "#ABD473", "Rogue": "#FFF569", "Priest": "#E6E6E6", "Shaman": "#5C9BFF", "Mage": "#69CCF0", "Warlock": "#9482C9", "Druid": "#FF7D0A"}
ROLE_COLOUR = {"tank": "#6FA8DC", "healer": "#7CCB8F", "melee": "#E0A448", "ranged": "#C9A4FF"}
CLASS_ABBR = {"Warrior": "W", "Paladin": "Pa", "Hunter": "H", "Rogue": "Ro", "Priest": "Pr", "Shaman": "S", "Mage": "M", "Warlock": "Wl", "Druid": "D"}

_FONT_DIRS = [Path("/System/Library/Fonts/Supplemental"), Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/truetype/liberation")]
_FONT_FILES = {False: ["Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"], True: ["Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for name in _FONT_FILES[bold]:
            p = d / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def _role_glyph(d: ImageDraw.ImageDraw, x: int, y: int, role: str, s: int = 12) -> None:
    c = ROLE_COLOUR.get(role, MUTED)
    if role == "tank":
        d.polygon([(x, y), (x + s, y), (x + s, y + s * 0.6), (x + s / 2, y + s), (x, y + s * 0.6)], fill=c)
    elif role == "healer":
        t = s // 3
        d.rectangle([x + t, y, x + s - t, y + s], fill=c)
        d.rectangle([x, y + t, x + s, y + s - t], fill=c)
    elif role == "melee":
        d.line([(x, y + s), (x + s, y)], fill=c, width=3)
        d.line([(x, y + s * 0.55), (x + s * 0.45, y + s)], fill=c, width=3)
    else:  # ranged
        d.polygon([(x, y + s), (x + s, y + s / 2), (x, y)], fill=c)


def roster_png(profile: GameProfile, players: list[Player], roster: RosterResult, title: str, subtitle: str = "") -> bytes:
    by = {p.signup_name: p for p in players}
    cov = cov_mod.compute(profile, players, roster)
    n_groups = len(roster.groups)
    W, M, GAP = 1500, 30, 16
    card_w = (W - 2 * M - GAP * (n_groups - 1)) // n_groups
    f_title, f_h, f_name, f_small, f_tiny = font(30, True), font(20, True), font(19, True), font(16), font(14)
    row_h, cell, name_w, bar_w = 26, 32, 300, 140
    buff_lines = max(len(g.buffs) for g in roster.group_reports) if roster.group_reports else 0
    card_h = 44 + 5 * 30 + min(buff_lines, 6) * 20 + 18
    matrix_top = M + 60 + card_h + 30
    header_h = 150
    matrix_h = header_h + sum(30 + row_h * len(g.players) + 8 for g in cov.groups) + 10
    adv = [a for a in roster.advisories][:6]
    adv_h = (30 + 26 * len(adv) + 10) if adv else 0
    H = matrix_top + matrix_h + M + (30 if cov.unmet_raidwide else 0) + adv_h
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((M, M), title, font=f_title, fill=INK)
    if subtitle:
        d.text((M, M + 38), subtitle, font=f_small, fill=MUTED)

    # ---- group cards
    top = M + 60
    for gi, gr in enumerate(roster.group_reports):
        x0 = M + gi * (card_w + GAP)
        d.rounded_rectangle([x0, top, x0 + card_w, top + card_h], radius=8, fill=PANEL, outline=LINE)
        d.text((x0 + 14, top + 10), f"Group {gr.index}", font=f_h, fill=INK)
        d.text((x0 + card_w - 14 - d.textlength(f"+{gr.value}", font=f_h), top + 10), f"+{gr.value}", font=f_h, fill=OK)
        y = top + 44
        for name in roster.groups[gi]:
            p = by[name]
            _role_glyph(d, x0 + 14, y + 6, p.role)
            label = p.character or f"{p.signup_name}?"
            d.text((x0 + 34, y), label, font=f_name, fill=CLASS.get(p.cls, INK))
            d.text((x0 + 34 + d.textlength(label, font=f_name) + 8, y + 3), p.spec, font=f_small, fill=MUTED)
            y += 30
        y += 4
        for b in gr.buffs[:6]:
            name, _, rest = b.partition(" [")
            val = b.rsplit("+", 1)[-1]
            d.text((x0 + 14, y), f"{name.split(' (')[0]}", font=f_tiny, fill=MUTED)
            d.text((x0 + card_w - 14 - d.textlength(f"+{val}", font=f_tiny), y), f"+{val}", font=f_tiny, fill=OK)
            y += 20

    # ---- coverage matrix
    d.text((M, matrix_top), "Buff coverage", font=f_h, fill=INK)
    d.text((M + 170, matrix_top + 4), "■ benefits, covered   ▢ benefits, missing in group   · not relevant", font=f_small, fill=MUTED)
    mx = M + name_w
    hy = matrix_top + 36
    short = [cov_mod._short(b) for b in cov.buffs]
    for ci, s in enumerate(short):
        tw = int(d.textlength(s, font=f_small)) + 6
        tmp = Image.new("RGBA", (tw, 22), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((2, 2), s, font=f_small, fill=MUTED)
        rot = tmp.rotate(90, expand=True)
        img.paste(rot, (mx + ci * cell + (cell - rot.width) // 2, hy + header_h - 20 - rot.height), rot)
    d.text((mx + len(short) * cell + 12, hy + header_h - 40), "coverage", font=f_small, fill=MUTED)
    y = hy + header_h
    for g in cov.groups:
        d.rectangle([M, y, W - M, y + 26], fill=PANEL)
        d.text((M + 10, y + 3), f"Group {g.index}", font=f_h, fill=INK)
        miss = "  ·  ".join(m.split(" (")[0] for m in g.missing_summary[:3])
        if miss:
            d.text((M + 130, y + 6), "missing: " + miss, font=f_tiny, fill=WARN)
        y += 30
        for pc in g.players:
            _role_glyph(d, M + 10, y + 7, pc.role, 11)
            d.text((M + 30, y + 2), pc.name, font=f_name, fill=CLASS.get(pc.cls, INK))
            d.text((M + 30 + d.textlength(pc.name, font=f_name) + 8, y + 5), pc.spec, font=f_tiny, fill=MUTED)
            for ci, c in enumerate(pc.cells):
                cx = mx + ci * cell + (cell - 18) // 2
                cy = y + 4
                if c.value <= 0:
                    d.ellipse([cx + 6, cy + 6, cx + 11, cy + 11], fill=NA)
                elif c.present:
                    d.rounded_rectangle([cx, cy, cx + 18, cy + 18], radius=3, fill=OK)
                else:
                    d.rounded_rectangle([cx, cy, cx + 18, cy + 18], radius=3, fill=WARN_SOFT, outline=WARN, width=2)
            bx = mx + len(short) * cell + 12
            d.rounded_rectangle([bx, y + 9, bx + bar_w, y + 15], radius=3, fill=BARBG)
            d.rounded_rectangle([bx, y + 9, bx + int(bar_w * pc.pct), y + 15], radius=3, fill=OK)
            d.text((bx + bar_w + 8, y + 3), f"{pc.pct:.0%}", font=f_tiny, fill=MUTED)
            d.line([(M, y + row_h - 1), (W - M, y + row_h - 1)], fill=LINE)
            y += row_h
        y += 8
    if cov.unmet_raidwide:
        d.text((M, y), "Nobody on the roster provides: " + ", ".join(cov.unmet_raidwide), font=f_small, fill=WARN)
        y += 30
    if adv:
        y += 6
        d.text((M, y), "Advisories", font=f_h, fill=INK)
        y += 30
        for a in adv:
            lvl, _, rest = a.partition(" ")
            col = {"🔴": "#E5484D", "🟡": WARN, "🟢": OK}.get(lvl, MUTED)
            d.ellipse([M + 4, y + 7, M + 16, y + 19], fill=col)
            d.text((M + 28, y + 2), (rest if lvl in ("🔴", "🟡", "🟢") else a)[:140], font=f_small, fill=INK)
            y += 26

    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def buff_badge_png(abbr: str, colour: str, size: int = 96) -> bytes:
    """Rounded square with the buff's abbreviation, for emojis and cards."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=size // 5, fill=colour)
    f = font(int(size * (0.42 if len(abbr) <= 2 else 0.3)), True)
    tw, th = d.textbbox((0, 0), abbr, font=f)[2:]
    d.text(((size - tw) / 2, (size - th) / 2 - size * 0.06), abbr, font=f, fill="#14181F")
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _badge(d: ImageDraw.ImageDraw, x: int, y: int, abbr: str, colour: str, s: int = 26) -> None:
    d.rounded_rectangle([x, y, x + s, y + s], radius=6, fill=colour)
    f = font(int(s * (0.48 if len(abbr) <= 2 else 0.34)), True)
    tw, th = d.textbbox((0, 0), abbr, font=f)[2:]
    d.text((x + (s - tw) / 2, y + (s - th) / 2 - 2), abbr, font=f, fill="#14181F")


def health_png(title: str, subtitle: str, headcount: tuple[int, int, int, int], roles: list[dict], buffs: list[dict], unresponsive: list[str], footer: str = "",
               headcount_text: str | None = None, unresponsive_label: str = "No response", buff_hint: str = "badge = buff · name = provider · red outline = nobody signed brings it") -> bytes:
    """Roster health card (a sheet's health, or the pool's readiness with the labels overridden).
    headcount: (in, size, tentative, sub); roles: [{role, have, need, level, hint}];
    buffs: [{abbr, colour, name, providers: [names]}] (empty providers = missing)."""
    W, M = 1100, 28
    f_title, f_h, f_big, f_body, f_small = font(28, True), font(18, True), font(34, True), font(16), font(14)
    LEVEL = {"green": OK, "amber": WARN, "red": "#E5484D"}
    rows_buffs = -(-len(buffs) // 4)
    H = M + 60 + 70 + 20 + 120 + 20 + 30 + rows_buffs * 40 + 20 + (40 if unresponsive else 0) + 40
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((M, M), title, font=f_title, fill=INK)
    d.text((M, M + 34), subtitle, font=f_small, fill=MUTED)
    y = M + 64
    # headcount bar
    n_in, size, tent, sub = headcount
    d.rounded_rectangle([M, y, W - M, y + 62], radius=8, fill=PANEL, outline=LINE)
    d.text((M + 14, y + 8), "Headcount", font=f_h, fill=INK)
    bar_x, bar_w = M + 160, W - 2 * M - 160 - 240
    d.rounded_rectangle([bar_x, y + 24, bar_x + bar_w, y + 38], radius=7, fill=BARBG)
    frac = min(1.0, n_in / size) if size else 0
    d.rounded_rectangle([bar_x, y + 24, bar_x + int(bar_w * frac), y + 38], radius=7, fill=OK if n_in >= size else WARN)
    if tent:
        tf = min(1.0, (n_in + tent) / size) if size else 0
        d.rounded_rectangle([bar_x + int(bar_w * frac), y + 24, bar_x + int(bar_w * tf), y + 38], radius=7, fill=WARN_SOFT, outline=WARN)
    d.text((bar_x + bar_w + 16, y + 14), headcount_text or f"{n_in}/{size} in · {tent} tentative · {sub} sub", font=f_body, fill=INK)
    y += 82
    # role tiles
    tile_w = (W - 2 * M - 3 * 14) // 4
    for i, r in enumerate(roles):
        x0 = M + i * (tile_w + 14)
        col = LEVEL[r["level"]]
        d.rounded_rectangle([x0, y, x0 + tile_w, y + 138], radius=8, fill=PANEL, outline=col, width=2)
        _role_glyph(d, x0 + 16, y + 16, r["role"], 18)
        d.text((x0 + 44, y + 12), r["role"].title(), font=f_h, fill=INK)
        d.text((x0 + 16, y + 42), f"{r['have']}" + (f" / {r['need']}" if r.get("need") else ""), font=f_big, fill=col)
        hint = r.get("hint") or ""
        for j, part in enumerate([h.strip() for h in hint.split(" · ") if h.strip()][:2]):
            d.text((x0 + 16, y + 90 + j * 18), part[:44], font=f_small, fill=MUTED)
    y += 158
    # buffs
    d.text((M, y), "Buff coverage", font=f_h, fill=INK)
    d.text((M + 150, y + 3), buff_hint, font=f_small, fill=MUTED)
    y += 30
    col_w = (W - 2 * M) // 4
    for i, b in enumerate(buffs):
        cx, cy = M + (i % 4) * col_w, y + (i // 4) * 40
        missing = not b["providers"]
        _badge(d, cx, cy, b["abbr"], b["colour"] if not missing else NA)
        if missing:
            d.rounded_rectangle([cx, cy, cx + 26, cy + 26], radius=6, outline="#E5484D", width=2)
        d.text((cx + 34, cy + 1), b["name"][:22], font=f_body, fill=INK if not missing else "#E5484D")
        d.text((cx + 34, cy + 19), (", ".join(b["providers"][:2]) + ("…" if len(b["providers"]) > 2 else "")) if not missing else "missing", font=f_small, fill=MUTED)
    y += rows_buffs * 40 + 16
    if unresponsive:
        lbl = f"{unresponsive_label} ({len(unresponsive)})"
        d.text((M, y), lbl, font=f_h, fill=WARN)
        d.text((M + 20 + int(d.textlength(lbl, font=f_h)), y + 3), ", ".join(unresponsive[:14]) + ("…" if len(unresponsive) > 14 else ""), font=f_body, fill=MUTED)
        y += 40
    if footer:
        d.text((M, H - 30), footer, font=f_small, fill=MUTED)
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def class_badge_png(cls: str, size: int = 96) -> bytes:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 2, size - 2], fill=CLASS.get(cls, MUTED))
    txt = CLASS_ABBR.get(cls, cls[:2])
    f = font(int(size * 0.5), True)
    tw, th = d.textbbox((0, 0), txt, font=f)[2:]
    d.text(((size - tw) / 2, (size - th) / 2 - size * 0.06), txt, font=f, fill="#14181F")
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def role_badge_png(role: str, size: int = 96) -> bytes:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 2, size - 2], fill="#1C222B", outline=ROLE_COLOUR.get(role, MUTED), width=6)
    _role_glyph(d, size // 4, size // 4, role, size // 2)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
