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


def bank_png(title: str, subtitle: str, rows: list[dict], footer: str = "") -> bytes:
    """Character bank table: one row per member.
    rows: [{member, role, main: {cls, spec, offspec, name, status, rank, rosters} | None, alts: [{cls, spec, name, status}]}]"""
    W, M, RH = 1100, 28, 34
    f_title, f_h, f_body, f_small = font(28, True), font(15, True), font(16), font(13)
    cols = [("Member", M), ("Main", M + 170), ("Spec", M + 380), ("Role", M + 585), ("Rank", M + 675), ("Rosters", M + 745), ("Alts", M + 845)]
    H = M + 60 + 30 + max(1, len(rows)) * RH + 20 + (30 if footer else 0)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((M, M), title, font=f_title, fill=INK)
    d.text((M, M + 34), subtitle, font=f_small, fill=MUTED)
    y = M + 64
    d.rounded_rectangle([M - 8, y, W - M + 8, y + 26 + max(1, len(rows)) * RH + 6], radius=8, fill=PANEL, outline=LINE)
    for name, x in cols:
        d.text((x + 4, y + 5), name.upper(), font=f_small, fill=MUTED)
    y += 26
    d.line([(M, y), (W - M, y)], fill=LINE)
    if not rows:
        d.text((M + 4, y + 8), "nobody has registered yet", font=f_body, fill=MUTED)
    for i, r in enumerate(rows):
        yy = y + i * RH
        if i % 2:
            d.rectangle([M - 4, yy + 1, W - M + 4, yy + RH - 1], fill="#1F2630")
        d.text((cols[0][1] + 4, yy + 8), r["member"][:17], font=f_body, fill=INK)
        mc = r.get("main")
        if mc:
            col = CLASS.get(mc["cls"], INK)
            d.rounded_rectangle([cols[1][1] + 4, yy + 8, cols[1][1] + 12, yy + 26], radius=2, fill=col)
            label = mc["name"] or f"{mc['cls']} (unnamed)"
            d.text((cols[1][1] + 20, yy + 8), label[:22], font=f_body, fill=col if mc["name"] else MUTED)
            spec = mc["spec"] + (f"/{mc['offspec']}" if mc.get("offspec") and mc["offspec"] != mc["spec"] else "")
            d.text((cols[2][1] + 4, yy + 8 + (2 if len(spec) > 16 else 0)), spec[:26], font=f_small if len(spec) > 16 else f_body, fill=INK)
            _role_glyph(d, cols[3][1] + 4, yy + 11, r.get("role") or "", 12)
            d.text((cols[3][1] + 22, yy + 8), (r.get("role") or "?")[:7], font=f_body, fill=ROLE_COLOUR.get(r.get("role") or "", MUTED))
            d.text((cols[4][1] + 4, yy + 8), mc.get("rank", "")[:8], font=f_body, fill=INK if mc.get("rank") not in ("trial", "alt") else MUTED)
            d.text((cols[5][1] + 4, yy + 8), (", ".join(mc.get("rosters") or []) or "—")[:12], font=f_body, fill=INK if mc.get("rosters") else MUTED)
        else:
            d.text((cols[1][1] + 4, yy + 8), "no main", font=f_body, fill=MUTED)
        x = cols[6][1] + 4
        alts = r.get("alts", [])
        fa = f_small if len(alts) > 1 else f_body
        shown = 0
        for a in alts:
            col = CLASS.get(a["cls"], INK)
            t = ((a["name"] or a["cls"]) + f" · {a['spec']}")[:20]
            w = 20 + int(d.textlength(t, font=fa))
            if x + w > W - M - 30 and shown:
                break
            d.rounded_rectangle([x, yy + 8, x + 8, yy + 26], radius=2, fill=col)
            d.text((x + 14, yy + 8 + (2 if fa is f_small else 0)), t, font=fa, fill=col if a["name"] else MUTED)
            x += w
            shown += 1
        if len(alts) > shown:
            d.text((x, yy + 10), f"+{len(alts) - shown}", font=f_small, fill=MUTED)
    if footer:
        d.text((M, H - 26), footer, font=f_small, fill=MUTED)
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def groups_png(profile: GameProfile, players: list[Player], result: RosterResult, cov, raid_buffs: list[dict], title: str, subtitle: str, assumptions: list[str], labels: list[str] | None = None) -> bytes:
    """Compact optimised-groups card: group panels (members + present/missing aura badges + totem picks),
    a raid-buff strip, and the scoping assumptions."""
    by = {p.signup_name: p for p in players}
    buffs = {b.id: b for b in profile.party_buffs()}
    n = len(result.groups)
    W, M, GAP = 1100, 28, 12
    cols = min(4, max(1, n))
    rows = -(-n // cols)
    pw = (W - 2 * M - GAP * (cols - 1)) // cols
    f_title, f_h, f_body, f_small, f_tiny = font(28, True), font(16, True), font(15), font(13), font(12)
    gsize = profile.comp_rules["group_size"]
    ph = 36 + gsize * 22 + 8 + 34 + 18 + 10
    rb_rows = -(-len(raid_buffs) // 3) if raid_buffs else 0
    H = M + 64 + rows * (ph + GAP) + (30 + rb_rows * 40 + 10 if raid_buffs else 0) + 18 * len(assumptions) + 24
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((M, M), title, font=f_title, fill=INK)
    d.text((M, M + 34), subtitle, font=f_small, fill=MUTED)
    top = M + 64
    for gi, names in enumerate(result.groups):
        x0 = M + (gi % cols) * (pw + GAP)
        y0 = top + (gi // cols) * (ph + GAP)
        g = cov.groups[gi]
        d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=8, fill=PANEL, outline=LINE)
        d.text((x0 + 12, y0 + 8), f"Group {gi + 1}", font=f_h, fill=INK)
        if labels and gi < len(labels):
            d.text((x0 + 12 + d.textlength(f"Group {gi + 1}", font=f_h) + 8, y0 + 11), labels[gi], font=f_tiny, fill=MUTED)
        val = result.group_reports[gi].value if gi < len(result.group_reports) else 0
        d.text((x0 + pw - 12 - d.textlength(f"+{val}", font=f_h), y0 + 8), f"+{val}", font=f_h, fill=OK if val else MUTED)
        y = y0 + 34
        for name in names:
            p = by[name]
            _role_glyph(d, x0 + 12, y + 5, p.role, 11)
            label = (p.character or p.signup_name)[:14]
            d.text((x0 + 30, y), label, font=f_body, fill=CLASS.get(p.cls, INK))
            d.text((x0 + 30 + d.textlength(label, font=f_body) + 6, y + 2), p.spec[:12], font=f_tiny, fill=MUTED)
            y += 22
        for _ in range(gsize - len(names)):
            d.rounded_rectangle([x0 + 12, y + 4, x0 + 23, y + 15], radius=3, outline=LINE, width=1)
            d.text((x0 + 30, y + 1), "open", font=f_tiny, fill=NA)
            y += 22
        y = y0 + 34 + gsize * 22 + 8
        # badges: present (coloured) then wanted-but-missing (grey, red outline); slot losers are skipped
        bx = x0 + 12
        bs = 24  # badge size; buffs worth < 2 to the group are noise and stay off the row
        present_ids = [bid for bid in g.present if g.wanted.get(bid, 0) >= 2]
        slot_taken = {buffs[bid].slot for bid in g.present if buffs[bid].slot}
        missing_ids = [bid for bid, w in sorted(g.wanted.items(), key=lambda kv: -kv[1]) if w >= 2 and bid not in g.present and not (buffs[bid].slot and buffs[bid].slot in slot_taken)]
        for bid in present_ids + missing_ids:
            b = buffs[bid]
            if bx + bs > x0 + pw - 12:
                d.text((bx, y + 4), "…", font=f_small, fill=MUTED)
                break
            missing = bid not in g.present
            _badge(d, bx, y, b.abbr, b.colour if not missing else NA, bs)
            if missing:
                d.rounded_rectangle([bx, y, bx + bs, y + bs], radius=6, outline="#E5484D", width=2)
            bx += bs + 3
        # totem picks (elements whose chosen totem matters to this group)
        if g.picks:
            picks = " · ".join(f"{slot.split('_')[1].title()} {buffs[bid].abbr}" for slot, bid in sorted(g.picks.items()) if not slot.endswith("_cd") and g.wanted.get(bid, 0) >= 2)
            d.text((x0 + 12, y + 34), ("totems: " + (picks or "nothing wanted here"))[:60], font=f_tiny, fill=MUTED)
        elif any(by[nm].cls == "Shaman" for nm in names):
            d.text((x0 + 12, y + 34), "totems: nothing wanted here", font=f_tiny, fill=MUTED)
    y = top + rows * (ph + GAP)
    if raid_buffs:
        d.text((M, y), "Raid-wide buffs", font=f_h, fill=INK)
        d.text((M + 150, y + 3), "cast on the whole raid · count = providers in the pool", font=f_small, fill=MUTED)
        y += 30
        cw = (W - 2 * M) // 3
        LEVEL = {"green": OK, "amber": WARN, "red": "#E5484D"}
        for i, rb in enumerate(raid_buffs):
            cx, cy = M + (i % 3) * cw, y + (i // 3) * 40
            _badge(d, cx, cy, rb["abbr"][:4], rb["colour"] if rb["providers"] else NA, 26)
            if not rb["providers"]:
                d.rounded_rectangle([cx, cy, cx + 26, cy + 26], radius=6, outline="#E5484D", width=2)
            d.text((cx + 34, cy + 1), f"{rb['name'][:22]} ×{len(rb['providers'])}", font=f_body, fill=LEVEL[rb["ok"]])
            d.text((cx + 34, cy + 19), rb["detail"][:48], font=f_tiny, fill=MUTED)
        y += rb_rows * 40 + 10
    for a in assumptions:
        d.text((M, y), ("assumes " + a)[:150], font=f_tiny, fill=MUTED)
        y += 18
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def comp_png(lines: list, title: str, subtitle: str, notes: list[str]) -> bytes:
    """Desired-comp table: target vs have per role/class/spec with the reason; officer targets marked."""
    W, M, RH = 1100, 28, 30
    f_title, f_h, f_body, f_small, f_tiny = font(28, True), font(15, True), font(16), font(13), font(12)
    LEVEL = {"green": OK, "amber": WARN, "red": "#E5484D"}
    H = M + 64 + 30 + max(1, len(lines)) * RH + 16 + 18 * len(notes) + 20
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((M, M), title, font=f_title, fill=INK)
    d.text((M, M + 34), subtitle, font=f_small, fill=MUTED)
    y = M + 64
    d.rounded_rectangle([M - 8, y, W - M + 8, y + 26 + max(1, len(lines)) * RH + 6], radius=8, fill=PANEL, outline=LINE)
    for name, x in (("Slot", M), ("Want", M + 190), ("Have (+offspec)", M + 260), ("Why", M + 460)):
        d.text((x + 4, y + 5), name.upper(), font=f_small, fill=MUTED)
    y += 26
    d.line([(M, y), (W - M, y)], fill=LINE)
    for i, l in enumerate(lines):
        yy = y + i * RH
        if i % 2:
            d.rectangle([M - 4, yy + 1, W - M + 4, yy + RH - 1], fill="#1F2630")
        key = l.key
        if key in ROLE_COLOUR:
            _role_glyph(d, M + 4, yy + 9, key, 12)
            d.text((M + 22, yy + 6), key.title(), font=f_body, fill=ROLE_COLOUR[key])
        else:
            cls = key.split(":")[0]
            d.rounded_rectangle([M + 4, yy + 7, M + 12, yy + 24], radius=2, fill=CLASS.get(cls, INK))
            d.text((M + 20, yy + 6), key.replace(":", " · ")[:20], font=f_body, fill=CLASS.get(cls, INK))
        want = f"{l.want}" + (f"–{l.max}" if l.max is not None and l.max != l.want else "")
        d.text((M + 194, yy + 6), want, font=f_body, fill=INK)
        d.text((M + 264, yy + 6), str(l.have), font=f_body, fill=LEVEL[l.level])
        if getattr(l, "flex", 0):
            d.text((M + 264 + d.textlength(str(l.have), font=f_body) + 5, yy + 9), f"+{l.flex} offspec", font=f_tiny, fill=MUTED)
        bw = 110
        d.rounded_rectangle([M + 334, yy + 12, M + 334 + bw, yy + 18], radius=3, fill=BARBG)
        frac = min(1.0, l.have / l.want) if l.want else 1.0
        if getattr(l, "flex", 0) and l.want:
            ff = min(1.0, (l.have + l.flex) / l.want)
            d.rounded_rectangle([M + 334 + int(bw * frac), yy + 12, M + 334 + int(bw * ff), yy + 18], radius=3, fill=WARN_SOFT, outline=WARN)
        d.rounded_rectangle([M + 334, yy + 12, M + 334 + int(bw * frac), yy + 18], radius=3, fill=LEVEL[l.level])
        why = ("officer: " if l.source == "officer" else "") + l.why
        d.text((M + 464, yy + 8), why[:96], font=f_tiny, fill=WARN if l.source == "officer" else MUTED)
    y += max(1, len(lines)) * RH + 16
    for a in notes:
        d.text((M, y), (a if a.startswith("offspec") else "assumes " + a)[:150], font=f_tiny, fill=MUTED)
        y += 18
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


RAID_ART = {
    # our own stylised emblems, no game art: (sky top, sky bottom, ridge colours far→near, accent, glyph)
    "barrow_deeps": ("#1B1E3A", "#3A2F5C", ["#4A4470", "#2E2A4C", "#1A1830"], "#9C8CFF", "cave"),
    "hyjal_summit_forever": ("#0F2A2A", "#1E5C48", ["#2F7A5A", "#1F5A44", "#12382C"], "#E8C36A", "tree"),
    "onyxias_lair": ("#2A0F0F", "#5C1E12", ["#7A2E1E", "#4A1A12", "#2A0E0A"], "#FF8A3D", "dragon"),
}


def raid_thumb_png(raid_id: str, name: str, size: int, lockout_days: int, w: int = 640, h: int = 300) -> bytes:
    """Generative emblem card for a raid: layered ridges under a gradient sky, a glyph, the size badge."""
    import colorsys
    import hashlib

    art = RAID_ART.get(raid_id)
    if not art:
        hue = int(hashlib.md5(raid_id.encode()).hexdigest()[:2], 16) / 255
        top = "#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.5, 0.25))
        bot = "#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.45))
        ridges = ["#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.4, v)) for v in (0.5, 0.35, 0.2)]
        art = (top, bot, ridges, "#E8C36A", "peak")
    top, bot, ridges, accent, glyph = art
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    t = tuple(int(top[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(bot[i:i + 2], 16) for i in (1, 3, 5))
    for y in range(h):
        f = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(t[i] + (b[i] - t[i]) * f) for i in range(3)))
    # stars / embers
    rnd = __import__("random").Random(raid_id)
    for _ in range(70):
        x, y = rnd.randint(0, w), rnd.randint(0, h // 2)
        d.point((x, y), fill=accent if rnd.random() < 0.3 else "#FFFFFF")
    # ridges
    for i, col in enumerate(ridges):
        base = int(h * (0.55 + 0.13 * i))
        pts = [(0, h)]
        x = 0
        while x <= w:
            amp = 40 + 25 * i
            y = base - int(abs(((x * (7 + 3 * i)) % 200) - 100) / 100 * amp) + rnd.randint(-6, 6)
            pts.append((x, y))
            x += 20
        pts.append((w, h))
        d.polygon(pts, fill=col)
    # glyph
    cx, cy = w // 2, int(h * 0.42)
    if glyph == "cave":
        d.ellipse([cx - 70, cy - 50, cx + 70, cy + 70], fill="#0B0A16")
        d.ellipse([cx - 54, cy - 34, cx + 54, cy + 60], fill="#161430")
        d.polygon([(cx - 8, cy + 8), (cx + 8, cy + 8), (cx, cy + 40)], fill=accent)
    elif glyph == "tree":
        d.rectangle([cx - 8, cy, cx + 8, cy + 90], fill="#5A3B1E")
        for k, r in enumerate((70, 55, 40)):
            d.polygon([(cx - r, cy + 20 - k * 28), (cx + r, cy + 20 - k * 28), (cx, cy - 60 - k * 28)], fill=("#2E9E6B", "#3FB27C", "#5CC896")[k])
        d.ellipse([cx - 6, cy - 110, cx + 6, cy - 98], fill=accent)
    elif glyph == "dragon":
        d.polygon([(cx - 120, cy + 10), (cx - 40, cy - 60), (cx - 10, cy - 10), (cx + 10, cy - 10), (cx + 40, cy - 60), (cx + 120, cy + 10), (cx + 30, cy), (cx, cy + 40), (cx - 30, cy)], fill="#1A0908")
        d.ellipse([cx - 12, cy - 16, cx - 4, cy - 8], fill=accent)
        d.ellipse([cx + 4, cy - 16, cx + 12, cy - 8], fill=accent)
    else:
        d.polygon([(cx - 60, cy + 40), (cx, cy - 60), (cx + 60, cy + 40)], fill="#DDE3EA")
    # size badge + text
    f_big, f_small = font(34, True), font(16)
    d.rounded_rectangle([w - 110, 18, w - 18, 70], radius=10, fill=accent)
    tw = d.textlength(str(size), font=f_big)
    d.text((w - 64 - tw / 2, 24), str(size), font=f_big, fill="#141414")
    d.text((22, h - 62), name, font=font(28, True), fill="#F4F1EA")
    d.text((22, h - 30), f"{size}-player · every {lockout_days} day{'s' if lockout_days != 1 else ''}", font=f_small, fill="#D9D5CB")
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
