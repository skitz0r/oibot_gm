"""Discord surface for the signup-driven cycle: sheets with persistent Join / Bench / No thanks buttons,
confirmation DMs after lock, /raid commands, /raid out, and the scheduler loop (open on cadence → health →
fill → lock → confirm → expire → close)."""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from . import raidcycle as rc, render
from .discord_registry import Guilds, is_officer
from .ops import Ops
from .registry import Registry, RegistryError

TEAL = 0x2B7A78
LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}


class RaidContext:
    """Per-guild raid store + helpers, attached to the bot."""

    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, rc.RaidStore] = {k: rc.RaidStore(guilds.store, reg.key) for k, reg in ((r.key, r) for r in guilds.by_discord.values())}

    def store(self, reg: Registry) -> rc.RaidStore:
        return self.stores.setdefault(reg.key, rc.RaidStore(self.guilds.store, reg.key))


# ---------------------------------------------------------------- rendering

def run_times(reg: Registry, ev: rc.RaidEvent, team: dict) -> tuple[datetime, datetime, datetime]:
    """(health/nudge, lock, confirmation deadline) for a run."""
    start = ev.start
    soft = start - timedelta(hours=float(rc.team_setting(team, "cutoff_soft_hours")))
    hard = start - timedelta(hours=float(rc.team_setting(team, "cutoff_hard_hours")))
    confirm = datetime.fromisoformat(ev.confirm_by) if ev.confirm_by else start - timedelta(hours=float(team.get("confirm_hours", reg.raid_def(ev.instance)["confirm_hours_before"])))
    return soft, hard, confirm


def sheet_embed(reg: Registry, ev: rc.RaidEvent, team: dict, ico) -> discord.Embed:
    start = ev.start
    unix = int(start.timestamp())
    ins, subs, outs = (ev.by_status(s) for s in ("in", "sub", "out"))
    counts = {r: sum(1 for s in ins if s.role == r) for r in ("tank", "healer", "melee", "ranged")}
    rd = reg.raid_def(ev.instance)
    size = int(team.get("size") or rd.get("size") or 20)
    test = bool(team.get("test"))
    e = discord.Embed(title=f"{'🧪 ' if test else ''}{rd.get('name', ev.instance or 'raid')}", colour=0x8C97A8 if test else TEAL)
    roles = "  ".join(f"{ico('role', r)} {n}" for r, n in counts.items() if n)
    if ev.state == "open" or not ev.all_rosters:
        e.description = f"**<t:{unix}:F>** · <t:{unix}:R>\n**{len(ins)}** / {size} joined" + (f" · {len(subs)} bench" if subs else "") + (f"\n{roles}" if roles else "")
        by_cls: dict[str, list[str]] = {}
        for s in ins:
            by_cls.setdefault(s.cls, []).append(f"{ico('role', s.role)} {ico('spec', f'{s.cls}:{s.spec}') or ''} **{s.character}**".replace("  ", " "))
        for cls, lines in sorted(by_cls.items(), key=lambda kv: -len(kv[1])):
            e.add_field(name=f"{ico('class', cls)} {cls} · {len(lines)}", value="\n".join(lines)[:1000], inline=True)
        if subs:
            e.add_field(name=f"Bench · {len(subs)}", value=" · ".join(s.character for s in subs)[:1000], inline=False)
    else:
        conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, ev)}
        mark = {"yes": "✅", "no": "❌", "expired": "⌛", None: "⏳"}
        seated = ev.seated()
        e.description = f"**<t:{unix}:F>** · <t:{unix}:R>\n🔒 **{len(seated)}** seated" + (f" in {len(ev.all_rosters)} rosters" if len(ev.all_rosters) > 1 else "") + f" · ✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
        for i, r in enumerate(ev.all_rosters):
            for gi, g in enumerate(r.groups):
                members = [next((p for p in r.selected if p.signup_name == n), None) for n in g]
                lines = [f"{mark.get(conf.get(p.signup_name), '')} {ico('role', p.role)} **{p.character or p.signup_name}**" for p in members if p]
                if lines:
                    e.add_field(name=(f"R{i + 1} · " if len(ev.all_rosters) > 1 else "") + f"Group {gi + 1}", value="\n".join(lines)[:1000], inline=True)
        bench = ev.all_rosters[0].benched
        if bench:
            e.add_field(name=f"Bench · {len(bench)}", value=" · ".join(p.character or p.signup_name for p in bench)[:1000], inline=False)
        pending = [n for n, a in conf.items() if a is None]
        if pending:
            e.add_field(name="Waiting on", value=", ".join(pending[:15])[:1000], inline=False)
    if outs:
        e.add_field(name=f"No thanks · {len(outs)}", value=" · ".join(s.character + (" ⚑" if s.source == "callout" else "") for s in outs)[:1000], inline=False)
    _soft, hard, confirm = run_times(reg, ev, team)
    if ev.state == "open":
        e.add_field(name="\u200b", value=f"🔒 locks <t:{int(hard.timestamp())}:R> · ✓ confirm by <t:{int(confirm.timestamp())}:t>", inline=False)
    else:
        e.add_field(name="\u200b", value=f"✓ confirm by <t:{int(confirm.timestamp())}:t>", inline=False)
    e.set_footer(text=("test run · " if test else "") + "Join = I'm coming · Bench = call me if you need me · you pick the character after pressing")
    return e


def sheet_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico) -> discord.ui.LayoutView:
    """The sheet as a Discord layout message: header with the raid emblem, class lines with real icons, bench and
    no-thanks member rows, one timeline line, the buttons — edited in place on every answer."""
    ui = discord.ui
    start = ev.start
    unix = int(start.timestamp())
    ins, subs, outs = (ev.by_status(s) for s in ("in", "sub", "out"))
    rd = reg.raid_def(ev.instance)
    size = int(team.get("size") or rd.get("size") or 20)
    test = bool(team.get("test"))
    counts = {r: sum(1 for s in ins if s.role == r) for r in ("tank", "healer", "melee", "ranged")}
    locked = ev.state != "open" and bool(ev.all_rosters)
    runs = max(1, len(ev.all_rosters)) if locked else max(1, len(ins) // size) if size else 1
    head = f"## {'🧪 ' if test else ''}{rd.get('name', ev.instance or 'raid')}\n<t:{unix}:F> · <t:{unix}:R>\n"
    if locked:
        conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, ev)}
        head += f"🔒 **{len(ev.seated())}** seated" + (f" in {runs} rosters" if runs > 1 else "") + f" · ✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
    else:
        head += f"**{len(ins)}** / {size}" + (f" · {runs} runs" if runs > 1 else "") + "  " + "   ".join(f"{ico('role', r)} {n}" for r, n in counts.items())
    web = os.environ.get("OIBOT_WEB_URL", "")
    header: ui.Item = ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(media=f"{web}/img/raid/{ev.instance}.png")) if web.startswith("https") and ev.instance else ui.TextDisplay(head)
    parts: list = [header, ui.Separator()]
    if locked:
        mark = {"yes": "✅", "no": "❌", "expired": "⌛", None: "⏳"}
        for i, r in enumerate(ev.all_rosters):
            lines = []
            for gi, g in enumerate(r.groups):
                members = [next((p for p in r.selected if p.signup_name == n), None) for n in g]
                if any(members):
                    lines.append(f"**Group {gi + 1}** — " + " · ".join(f"{mark.get(conf.get(p.signup_name), '')}{ico('spec', f'{p.cls}:{p.spec}')} {p.character or p.signup_name}" for p in members if p))
            if lines:
                parts.append(ui.TextDisplay((f"### Roster {i + 1}\n" if len(ev.all_rosters) > 1 else "") + "\n".join(lines)))
        bench = ev.all_rosters[0].benched
        pending = [n for n, a in conf.items() if a is None]
        tail = []
        if bench:
            tail.append("**Bench** " + " · ".join(p.signup_name for p in bench))
        if pending:
            tail.append("**Waiting on** " + ", ".join(pending[:15]))
        if tail:
            parts += [ui.Separator(), ui.TextDisplay("\n".join(tail))]
    else:
        by_cls: dict[str, list] = {}
        for sg in ins:
            by_cls.setdefault(sg.cls, []).append(sg)
        if by_cls:
            lines = [f"{ico('class', cls)} **{cls}** {len(ss)} — " + " · ".join(f"{ico('spec', f'{sg.cls}:{sg.spec}')} {sg.character}" for sg in ss) for cls, ss in sorted(by_cls.items(), key=lambda kv: -len(kv[1]))]
            parts.append(ui.TextDisplay("\n".join(lines)[:3900]))
        else:
            parts.append(ui.TextDisplay("-# Nobody has joined yet."))
        rows = []
        if subs:
            rows.append("**Bench** " + " · ".join(sg.display_name for sg in subs))
        if outs:
            rows.append("**No thanks** " + " · ".join(sg.display_name + (" ⚑" if sg.source == "callout" else "") for sg in outs))
        if rows:
            parts += [ui.Separator(), ui.TextDisplay("\n".join(rows))]
    _soft, hard, confirm = run_times(reg, ev, team)
    parts.append(ui.Separator())
    parts.append(ui.TextDisplay((f"🔒 locks <t:{int(hard.timestamp())}:R> · " if ev.state == "open" else "") + f"✓ confirm by <t:{int(confirm.timestamp())}:t>"))
    if ev.state == "open":
        parts.append(ui.ActionRow(*[SignupButton(ev.key, st) for st in rc.STATUSES]))
    parts.append(ui.TextDisplay("-# " + ("test run · " if test else "") + ("Join = I'm coming · Bench = call me if you need me" if ev.state == "open" else "seated members confirm by DM · No thanks / `/raid out` frees a seat")))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=0x8C97A8 if test else TEAL))
    return view


def board_url(ev: rc.RaidEvent) -> str | None:
    web = os.environ.get("OIBOT_WEB_URL", "")
    return f"{web}/app/rosters#run-{ev.key}" if web.startswith("http") else None


def _header(reg: Registry, ev: rc.RaidEvent, title: str, lines: list[str]):
    """Section with the raid emblem: title, run time with countdown, then `lines`."""
    ui = discord.ui
    unix = int(ev.start.timestamp())
    text = f"## {title}\n<t:{unix}:F> · <t:{unix}:R>" + ("\n" + "\n".join(lines) if lines else "")
    web = os.environ.get("OIBOT_WEB_URL", "")
    if web.startswith("https") and ev.instance:
        return ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=f"{web}/img/raid/{ev.instance}.png"))
    return ui.TextDisplay(text)


def _role_counts(ico, players) -> str:
    counts = {r: sum(1 for p in players if p.role == r) for r in ("tank", "healer", "melee", "ranged")}
    return "   ".join(f"{ico('role', r)} {n}" for r, n in counts.items())


def _group_summaries(reg: Registry, r) -> list[dict]:
    """Per-group present/missing auras for a locked roster (same coverage code as the site)."""
    from . import comp as comp_mod
    from .roster import coverage as cov_mod

    try:
        cov = cov_mod.compute(reg.profile, r.selected, r)
        return comp_mod.groups_summary(reg, r.selected, r, cov)["groups"]
    except Exception:  # noqa: BLE001
        return []


def _aura_line(ico, g: dict | None) -> str:
    if not g:
        return ""
    present = " ".join(ico("buff", a["id"]) for a in g["present"] if ico("buff", a["id"]))
    missing = " ".join(ico("buff", a["id"]) for a in g["missing"][:6] if ico("buff", a["id"]))
    return (present + (f"  ⚠ {missing}" if missing else "")).strip()


def _group_block(reg: Registry, ico, r, gi: int, marks: dict | None = None, summaries: list[dict] | None = None, title: str | None = None) -> str:
    """One group as the web board shows it: heading, one member per line (spec icon + name), its auras under it."""
    members = [next((p for p in r.selected if p.signup_name == n), None) for n in r.groups[gi]]
    lines = [f"**{title or f'Group {gi + 1}'}**"]
    for p in members:
        if p:
            mark = (marks or {}).get(p.signup_name)
            lines.append(f"{(mark + ' ') if mark else ''}{ico('spec', f'{p.cls}:{p.spec}')} {p.character or p.signup_name}")
    aura = _aura_line(ico, summaries[gi] if summaries and gi < len(summaries) else None)
    if aura:
        lines.append("-# " + aura)
    return "\n".join(lines)


MARK = {"yes": "✅", "no": "❌", "expired": "⌛", None: "⏳"}


def run_actions_row(ev: rc.RaidEvent, locked: bool):
    ui = discord.ui
    items = []
    url = board_url(ev)
    if url:
        items.append(ui.Button(label="Open the board", style=discord.ButtonStyle.link, url=url))
    items.append(RunButton(ev.key, "fill"))
    items.append(RunButton(ev.key, "cancel" if locked else "lock"))
    return ui.ActionRow(*items)


def health_layout(reg: Registry, rs, ev: rc.RaidEvent, team: dict, ico) -> discord.ui.LayoutView:
    """The health check as a card: joined vs size, role have/need with the short role marked, who could cover,
    nobody-brings icons, no-answer count, double-booked; officer buttons."""
    ui = discord.ui
    h = rc.health_data(reg, ev, team)
    n, size, _t, subs = h["headcount"]
    players = rc.players_for(reg, ev)
    runs = rc.how_many_rosters(reg, players, size, reg.role_bounds(ev.instance, size))
    roles = []
    for r in h["roles"]:
        have, need = r["have"], r["need"]
        roles.append(f"{ico('role', r['role'])} " + (f"**{have}**/{need * max(1, runs)}" if need and have < need * max(1, runs) else f"{have}" + (f"/{need * max(1, runs)}" if need else "")))
    head = _header(reg, ev, f"Roster health · {reg.raid_def(ev.instance).get('name', ev.instance)}", [f"**{n}** / {size}" + (f" · {runs} runs" if runs > 1 else "") + (f" · {subs} bench" if subs else "") + "   " + "   ".join(roles)])
    body = []
    short = [f"{r['need'] * max(1, runs) - r['have']} {r['role']}" for r in h["roles"] if r["need"] and r["have"] < r["need"] * max(1, runs)]
    if short:
        hints = [r["hint"] for r in h["roles"] if r["need"] and r["have"] < r["need"] * max(1, runs) and r["hint"]]
        body.append(f"**Short** {', '.join(short)}" + (f" · -# {' · '.join(hints)}" if hints else ""))
    missing = [b for b in h["buffs"] if not b["providers"]]
    if missing:
        body.append("**Nobody brings** " + " ".join(ico("buff", b["id"]) for b in missing if ico("buff", b["id"])))
    if h["unresponsive"]:
        body.append(f"**No answer** {len(h['unresponsive'])}" + (" · nudged" if ev.nudged else ""))
    busy = rc.conflicts(rs, ev) if rs is not None else {}
    double = [reg.members[u].display_name for u in busy if u in reg.members and str(u) in ev.signups and ev.signups[str(u)].status == "in"]
    if double:
        body.append("**Double-booked** " + ", ".join(double))
    _soft, hard, confirm = run_times(reg, ev, team)
    parts = [head, ui.Separator()] + ([ui.TextDisplay("\n".join(body)), ui.Separator()] if body else [])
    parts.append(ui.TextDisplay(f"🔒 locks <t:{int(hard.timestamp())}:R> · ✓ confirm by <t:{int(confirm.timestamp())}:t>"))
    parts.append(run_actions_row(ev, locked=False))
    worst = "red" if short else "amber" if missing or h["unresponsive"] else "green"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour={"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]))
    return view


def lock_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, i: int) -> discord.ui.LayoutView:
    """One locked roster as a card: groups with confirmation marks and auras, raid-wide row, bench, waiting-on, buttons."""
    ui = discord.ui
    r = ev.all_rosters[i]
    conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, ev) if c["roster"] == i + 1}
    marks = {n: MARK.get(a) for n, a in conf.items()}
    tally = f"✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
    rd = reg.raid_def(ev.instance)
    head = _header(reg, ev, f"🔒 {rd.get('name', ev.instance)}" + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else ""),
                   [f"**{len(r.selected)}** seated" + (f" · synergy {r.synergy_value}" if r.synergy_value else "") + "   " + _role_counts(ico, r.selected), tally])
    summaries = _group_summaries(reg, r)
    parts = [head, ui.Separator()]
    for gi in range(len(r.groups)):
        if r.groups[gi]:
            parts.append(ui.TextDisplay(_group_block(reg, ico, r, gi, marks, summaries)))
    from . import comp as comp_mod

    rb = comp_mod.raid_buff_status(reg.profile, r.selected)
    raidwide = " ".join(ico("buff", b["id"]) for b in rb if b["providers"] and ico("buff", b["id"]))
    raidmiss = " ".join(ico("buff", b["id"]) for b in rb if not b["providers"] and ico("buff", b["id"]))
    tail = [f"**Raid-wide** {raidwide}" + (f"  ⚠ {raidmiss}" if raidmiss else "")]
    if i == 0 and r.benched:
        tail.append("**Bench** " + " · ".join(p.signup_name for p in r.benched))
    pending = [n for n, a in conf.items() if a is None]
    if pending:
        tail.append("**Waiting on** " + ", ".join(pending[:15]))
    _soft, _hard, confirm = run_times(reg, ev, team)
    tail.append(f"-# ✓ confirm by <t:{int(confirm.timestamp())}:t> · unanswered then counts as out")
    parts += [ui.Separator(), ui.TextDisplay("\n".join(tail)), run_actions_row(ev, locked=True)]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=TEAL))
    return view


def confirm_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, sg, i: int, gi: int, uid: int) -> discord.ui.LayoutView:
    """Confirmation DM: your seat, the group you'd be in (the board's layout), the deadline, Confirm / Can't make it."""
    ui = discord.ui
    r = ev.all_rosters[i]
    rd = reg.raid_def(ev.instance)
    head = _header(reg, ev, f"You're seated · {rd.get('name', ev.instance)}", [f"{ico('spec', f'{sg.cls}:{sg.spec}')} **{sg.character}**" + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else "") + f" · Group {gi + 1}"])
    _s, _h, confirm = run_times(reg, ev, team)
    parts = [head, ui.Separator(), ui.TextDisplay(_group_block(reg, ico, r, gi, None, _group_summaries(reg, r), title=f"Group {gi + 1}")), ui.Separator(),
             ui.TextDisplay(f"-# Confirm keeps the seat. Can't make it frees it for someone on the bench. Unanswered by <t:{int(confirm.timestamp())}:t> counts as out."),
             ui.ActionRow(PlaceButton(ev.team, uid, "yes"), PlaceButton(ev.team, uid, "no"))]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=TEAL))
    return view


def fill_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, ask) -> discord.ui.LayoutView:
    """Fill DM: what opened, the seat offered (character + spec), the group they'd join, Yes / Can't."""
    ui = discord.ui
    rd = reg.raid_def(ev.instance)
    what = {"sub": "you're on the bench", "pool": "you haven't answered the sheet", "other_roster": "you're free that night", "offspec": f"you could play **{ask.spec}** instead of your main spec", "alt": f"you could bring your alt"}[ask.kind]
    c = reg.find(ask.character)
    cls = c[1].cls if c else ""
    head = _header(reg, ev, f"A seat opened · {rd.get('name', ev.instance)}", [f"{ask.reason} — {what}", f"Come as {ico('spec', f'{cls}:{ask.spec}') if cls else ''} **{ask.character}** ({ask.spec})"])
    parts = [head]
    if ev.all_rosters:
        for i, r in enumerate(ev.all_rosters):
            gi = next((k for k, g in enumerate(r.groups) if len(g) < int(reg.profile.comp_rules["group_size"])), None)
            if gi is not None:
                parts += [ui.Separator(), ui.TextDisplay(_group_block(reg, ico, r, gi, None, _group_summaries(reg, r), title=f"You'd join Group {gi + 1}" + (f" of Roster {i + 1}" if len(ev.all_rosters) > 1 else "")))]
                break
    parts += [ui.Separator(), ui.TextDisplay("-# Yes puts you straight in the seat. No answer and the next person is asked."), ui.ActionRow(FillButton(ev.key, ask.discord_id, "yes"), FillButton(ev.key, ask.discord_id, "no"))]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=0xE0A448))
    return view


class RunButton(discord.ui.DynamicItem[discord.ui.Button], template=r"runact:(?P<key>[A-Za-z0-9_\-]+):(?P<action>fill|lock|cancel)"):
    """Officer buttons on the health and lock cards: Fill seats (with a preview + confirm), Lock now, Cancel run."""
    LABELS = {"fill": ("Fill seats", discord.ButtonStyle.secondary), "lock": ("Lock now", discord.ButtonStyle.secondary), "cancel": ("Cancel run", discord.ButtonStyle.danger)}

    def __init__(self, key: str, action: str):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"runact:{key}:{action}"))
        self.key, self.action = key, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg or not await bot.is_officer_anywhere(reg, interaction.user.id):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That run is closed.", ephemeral=True)
            return
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        by = interaction.user.display_name
        if self.action == "fill":
            nd = rc.needs(reg, ev, team)
            if not nd["headcount"] and not nd["roles"]:
                await interaction.response.send_message("Nothing to fill — every seat is taken.", ephemeral=True)
                return
            batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
            outstanding = [a for a in ev.fill_asks if a.open]
            gaps = (f"{nd['headcount']} seat{'s' if nd['headcount'] != 1 else ''}" if nd["headcount"] else "") + "".join(f", {n} {r}" for r, n in nd["roles"].items())
            lines = [f"**Short:** {gaps.strip(', ')}"]
            if outstanding:
                lines.append(f"**Already asked, waiting:** {', '.join(a.display_name for a in outstanding)}")
            if batch:
                lines.append("**Will DM now:** " + "; ".join(f"{a.display_name} — {a.kind.replace('_', ' ')}, as {a.character} ({a.role})" for a in batch))
                lines.append("-# Each gets Yes / Can't this time. A yes takes the seat at once; a no asks the next person. Never more than 3 questions out at a time. Answers post in this run's thread.")
            else:
                lines.append("**Nobody to ask right now** — either the 3 outstanding questions already cover it, or everyone eligible has been asked.")
            view = discord.ui.View(timeout=120)
            go = discord.ui.Button(label=f"Send {len(batch)} ask{'s' if len(batch) != 1 else ''}", style=discord.ButtonStyle.success, disabled=not batch)
            no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

            async def send(i: discord.Interaction):
                await i.response.edit_message(content="Sending…", view=None)
                sent, nd2 = await bot.run_fill(reg, rs, ev, team, by=by)
                await i.edit_original_response(content=("Asked " + ", ".join(a.display_name for a in sent)) if sent else "Nobody could be asked.")

            async def cancel(i: discord.Interaction):
                await i.response.edit_message(content="Cancelled — nothing sent.", view=None)

            go.callback, no.callback = send, cancel
            view.add_item(go); view.add_item(no)
            await interaction.response.send_message("\n".join(lines), view=view, ephemeral=True)
            return
        if self.action == "lock":
            if ev.state != "open":
                await interaction.response.send_message("Already locked.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            line = await bot.lock_run(reg, rs, ev, by=by)
            await interaction.followup.send(line, ephemeral=True)
            await bot.ops.emit(reg.config, "info", line)
            return
        if self.action == "cancel":
            view = discord.ui.View(timeout=60)
            yes = discord.ui.Button(label="Cancel the run", style=discord.ButtonStyle.danger)

            async def do(i: discord.Interaction):
                ev.state = "cancelled"
                ev.log.append(f"cancelled by {by}")
                rs.save(ev, "cancelled")
                await bot.refresh_sheet(reg, ev)
                await bot.post_run_update(reg, ev, f"🛑 run cancelled by {by}")
                await i.response.edit_message(content=f"Cancelled {ev.key}.", view=None)
                await bot.ops.emit(reg.config, "warn", f"{by} cancelled {ev.key}")

            yes.callback = do
            view.add_item(yes)
            await interaction.response.send_message(f"Cancel **{team.get('name', ev.team)}**? Seated members are not told automatically.", view=view, ephemeral=True)


def health_card(reg: Registry, ev: rc.RaidEvent, team: dict, ico, rs=None) -> tuple[discord.Embed, discord.File]:
    """Image card + a one-line embed. Numbers are in the image; the embed carries the level and the next timers."""
    h = rc.health_data(reg, ev, team)
    levels = [h["headcount_level"]] + [r["level"] for r in h["roles"] if r["need"]]
    worst = "red" if "red" in levels else ("amber" if "amber" in levels else "green")
    colour = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]
    start = ev.start.astimezone(reg.tz)  # card text is guild time; the embed's <t:> stamps render per viewer
    soft, hard, _ = run_times(reg, ev, team)
    png = render.health_png(f"Roster health · {team.get('name', ev.team)}", f"{start.strftime('%a %b %d %H:%M %Z')} · locks {hard.astimezone(reg.tz).strftime('%a %H:%M')}", h["headcount"], h["roles"], h["buffs"], h["unresponsive"], footer="tiles: have / need · amber = bench could cover · badges: party buffs from joined players")
    file = discord.File(BytesIO(png), filename="health.png")
    n, size, _tent, subs = h["headcount"]
    e = discord.Embed(colour=colour, description=f"{LEVEL_DOT[worst]} **{n}/{size}** joined · {subs} bench · nudge <t:{int(soft.timestamp())}:R> · lock <t:{int(hard.timestamp())}:R>")
    e.set_image(url="attachment://health.png")
    missing = [b for b in h["buffs"] if not b["providers"]]
    if missing:
        e.add_field(name="Nobody brings", value=" ".join(f"{ico('buff', b['id'])}" for b in missing)[:900], inline=False)
    if rs is not None:
        busy = rc.conflicts(rs, ev)
        double = [f"{reg.members[u].display_name} ({k})" for u, k in busy.items() if u in reg.members and str(u) in ev.signups and ev.signups[str(u)].status == "in"]
        if double:
            e.add_field(name="Double-booked", value=", ".join(double)[:900], inline=False)
        open_asks = [a for a in ev.fill_asks if a.open]
        answered = [a for a in ev.fill_asks if a.answer == "yes"]
        if open_asks or answered:
            e.add_field(name="Fill", value=(f"asked: {', '.join(a.display_name for a in open_asks)}" if open_asks else "") + (f"\nfilled: {', '.join(a.display_name for a in answered)}" if answered else ""), inline=False)
    return e, file


# ---------------------------------------------------------------- persistent buttons

class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid:(?P<key>[A-Za-z0-9_\-]+):(?P<status>in|tentative|out|sub)"):
    STYLES = {"in": discord.ButtonStyle.success, "sub": discord.ButtonStyle.primary, "out": discord.ButtonStyle.secondary}

    def __init__(self, key: str, status: str):
        status = "in" if status == "tentative" else status
        super().__init__(discord.ui.Button(label=rc.LABELS[status], style=self.STYLES[status], custom_id=f"raid:{key}:{status}"))
        self.key, self.status = key, status

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], match["status"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("This sheet is closed.", ephemeral=True)
            return
        m = reg.members.get(interaction.user.id)
        if not m or not m.active():
            await interaction.response.send_message("Register a character first: `/register`.", ephemeral=True)
            return
        if ev.state != "open" and self.status != "out":
            await interaction.response.send_message("The roster is locked. If you were seated you'll have a confirmation DM; otherwise press **No thanks** or `/raid out` to be taken off.", ephemeral=True)
            return
        chars = m.active()
        if len(chars) > 1 and self.status == "in":
            # one press per character: no default to second-guess
            view = discord.ui.View(timeout=120)
            for c in chars[:5]:
                btn = discord.ui.Button(label=f"{c.label} · {c.cls} {c.spec}"[:80], style=discord.ButtonStyle.success if c.is_main else discord.ButtonStyle.secondary, emoji=bot.ico("class", c.cls) or None)

                async def pick(i: discord.Interaction, label=c.label):
                    await bot.apply_signup(i, reg, rs, ev, m, label, self.status)

                btn.callback = pick
                view.add_item(btn)
            await interaction.response.send_message(f"**{rc.LABELS[self.status]}** as which character?", view=view, ephemeral=True)
            return
        await bot.apply_signup(interaction, reg, rs, ev, m, None, self.status)


class FillButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fill:(?P<key>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Yes/No on a fill DM. Survives restarts; only the person asked can answer."""

    def __init__(self, key: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Yes, count me in" if answer == "yes" else "Can't this time", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"fill:{key}:{uid}:{answer}"))
        self.key, self.uid, self.answer = key, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], int(match["uid"]), match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not await bot.may_answer_for(interaction, reg, self.uid):
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        rs = bot.raids.store(reg) if reg else None
        ev = rs.events.get(self.key) if rs else None
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That raid is closed — thanks anyway.", ephemeral=True)
            return
        ask = next((a for a in ev.fill_asks if a.discord_id == self.uid and a.open), None)
        if not ask:
            await interaction.response.send_message("Already answered (or the gap was filled).", ephemeral=True)
            return
        line = rc.apply_fill_answer(reg, rs, ev, ask, self.answer == "yes")
        try:
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(line, ephemeral=True)
        await bot.after_fill_answer(reg, rs, ev, ask, line)


class PlaceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"place:(?P<roster>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Confirm / can't make it on a locked roster (DM after lock). Persistent; only the person asked can answer."""

    def __init__(self, roster: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Confirm" if answer == "yes" else "Can't make it", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"place:{roster}:{uid}:{answer}"))
        self.roster, self.uid, self.answer = roster, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["roster"], int(match["uid"]), match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if not await bot.may_answer_for(interaction, reg, self.uid):
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        try:
            line = reg.answer_placement(self.uid, self.roster, self.answer == "yes", interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"{e}", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(line, ephemeral=True)
        await bot.after_placement_answer(reg, self.uid, self.roster, self.answer == "yes", line)


def place_view(roster: str, uid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(PlaceButton(roster, uid, "yes"))
    v.add_item(PlaceButton(roster, uid, "no"))
    return v


def fill_view(key: str, uid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(FillButton(key, uid, "yes"))
    v.add_item(FillButton(key, uid, "no"))
    return v


def sheet_view(key: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for s in rc.STATUSES:
        v.add_item(SignupButton(key, s))
    return v


# ---------------------------------------------------------------- bot mixin

class RaidMixin:
    """Methods the bot needs; mixed into OibotGM."""

    async def may_answer_for(self, interaction: discord.Interaction, reg, uid: int) -> bool:
        """The person asked — or an officer answering for a test member (the test bench's puppets)."""
        if interaction.user.id == uid:
            return True
        m = reg.members.get(uid) if reg else None
        return bool(m and getattr(m, "test", False) and await self.is_officer_anywhere(reg, interaction.user.id))

    async def send_member_dm(self, reg, m, text: str | None = None, view=None) -> bool:
        """DM a member — text + buttons, or a layout card (`view` a LayoutView, `text` None). For a test member the same
        goes to whoever is running the test, with the puppet's name in front. Returns True when delivered."""
        target, prefix = m.discord_id, None
        if getattr(m, "test", False):
            tester = next((t.get("test_by") for t in reg.config.rosters if t.get("test") and t.get("test_by")), None) or reg.config.owner_discord_id
            if not tester:
                return False
            target, prefix = int(tester), f"🧪 **{m.display_name}** would get:"
        try:
            user = await self.fetch_user(target)
            if isinstance(view, discord.ui.LayoutView):
                if prefix:
                    await user.send(prefix)
                await user.send(view=view)
            else:
                await user.send((prefix + "\n" + (text or "")) if prefix else (text or ""), view=view)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- officer cards + the run's updates thread
    async def post_run_update(self, reg, ev, text: str) -> None:
        """One line in the run's thread (under its health or lock card; created on first use); falls back to the officer channel."""
        ch = self.officer_channel(reg, ev)
        thread = self.get_channel(ev.updates_thread_id) if ev.updates_thread_id else None
        if thread is None and ev.cards and ch:
            mid = ev.cards.get("lock:0") or ev.cards.get("health")
            try:
                msg = await ch.fetch_message(mid)
                thread = await msg.create_thread(name=f"{(reg.config.team(ev.team) or {}).get('name', ev.team)} · updates"[:100])
                ev.updates_thread_id = thread.id
                rs = self.raids.store(reg)
                rs.save(ev, "updates thread")
            except Exception:  # noqa: BLE001
                thread = None
        try:
            await (thread or ch).send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:  # noqa: BLE001
            pass

    async def refresh_cards(self, reg, ev) -> None:
        """Re-render the run's officer cards in place after anything that changes seats or confirmations."""
        if not ev.cards or not ev.cards_channel_id:
            return
        ch = self.get_channel(ev.cards_channel_id)
        if not ch:
            return
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        rs = self.raids.store(reg)
        for key, mid in list(ev.cards.items()):
            try:
                msg = await ch.fetch_message(mid)
                if key == "health":
                    if ev.state == "open":
                        await msg.edit(view=health_layout(reg, rs, ev, team, self.ico))
                elif key.startswith("lock:"):
                    i = int(key[5:])
                    if i < len(ev.all_rosters):
                        await msg.edit(view=lock_layout(reg, ev, team, self.ico, i))
            except Exception as e:  # noqa: BLE001
                print(f"card refresh failed ({key}): {e}")

    async def apply_signup(self, interaction: discord.Interaction, reg, rs, ev, m, character, status):
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        if ev.state != "open" and status == "out" and ev.seat_of(m.display_name):
            await self.drop_seated(reg, rs, ev, team, m, "callout", None)
            await interaction.response.send_message(f"Noted — you're off the {ev.key} roster; the bot is looking for a replacement.", ephemeral=True)
            return
        try:
            s = rc.set_signup(reg, rs, ev, m, character, status)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        msg = f"✅ {s.character}: **{rc.LABELS.get(s.status, s.status)}** for {ev.key}" + (f" — {s.note}" if s.note and s.status != status else "")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        await self.refresh_sheet(reg, ev)

    async def refresh_sheet(self, reg, ev) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        if ev.channel_id and ev.message_id:
            ch = self.get_channel(ev.channel_id)
            try:
                msg = await ch.fetch_message(ev.message_id)
                await msg.edit(view=sheet_layout(reg, ev, team, self.ico))
            except Exception as e:  # noqa: BLE001
                print(f"sheet refresh failed: {e}")

    async def post_sheet(self, reg, rs, ev, channel) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        msg = await channel.send(view=sheet_layout(reg, ev, team, self.ico))
        ev.channel_id, ev.message_id = channel.id, msg.id
        rs.save(ev, "sheet posted")
        if rc.team_setting(team, "open_dm"):
            await self.dm_on_open(reg, rs, ev, team)

    async def dm_on_open(self, reg, rs, ev, team) -> int:
        """Optional per-run: DM every registered raider when the sheet opens, with the same buttons."""
        unix = int(ev.start.timestamp())
        sent = 0
        for m in reg.team_pool(team["key"]):
            if not m.main or m.dm_opt_out:
                continue
            s = ev.signups.get(str(m.discord_id))
            status = f"You're pre-filled as **{rc.LABELS.get(s.status, s.status)}** on {s.character}" if s else "You haven't answered yet"
            if await self.send_member_dm(reg, m, f"**{reg.config.name} · {team.get('name', ev.team)}** <t:{unix}:F> (<t:{unix}:R>). {status}. Join / Bench / No thanks:" + (f" (sheet: <#{ev.channel_id}>)" if ev.channel_id else ""), sheet_view(ev.key)):
                sent += 1
        ev.log.append(f"open DMs sent to {sent}")
        rs.save(ev, f"open DMs {sent}")
        return sent

    async def post_health(self, reg, rs, ev, channel, nudge: bool) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        if channel is not None:
            try:
                msg = await channel.send(view=health_layout(reg, rs, ev, team, self.ico))
                ev.cards["health"], ev.cards_channel_id = msg.id, channel.id
            except Exception as e:  # noqa: BLE001
                print(f"health card failed: {e}")
        if nudge and rc.team_setting(team, "reminders") != "none":
            targets = [m for m in reg.team_pool(team["key"]) if m.main and str(m.discord_id) not in ev.signups and m.discord_id not in ev.nudged and not m.dm_opt_out]
            unix = int(ev.start.timestamp())
            for m in targets:
                if await self.send_member_dm(reg, m, f"{reg.config.name}: the **{team.get('name', ev.team)}** sheet for <t:{unix}:F> is still waiting for you — Join, Bench or No thanks in <#{ev.channel_id}>."):
                    ev.nudged.append(m.discord_id)
            if targets:
                rs.save(ev, f"nudged {len(targets)}")
        ev.health_posted = True
        rs.save(ev, "health posted")

    # ---- filling gaps by DM
    async def run_fill(self, reg, rs, ev, team, by: str = "scheduler") -> tuple[list, dict]:
        """Send the next batch of fill DMs. Returns (asks sent, needs)."""
        nd = rc.needs(reg, ev, team)
        if not nd["headcount"] and not nd["roles"]:
            if ev.fill_state == "asking":
                ev.fill_state = "filled"
                rs.save(ev, "fill: complete")
            return [], nd
        batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
        unix = int(ev.start.timestamp())
        name = f"{reg.config.name} · {team.get('name', ev.team)}"
        sent = []
        for ask in batch:
            what = {
                "sub": f"you're on the bench — can you come as **{ask.character}** ({ask.spec})?",
                "pool": f"you haven't answered the sheet — can you come as **{ask.character}** ({ask.spec})?",
                "other_roster": f"could you help out on **{ask.character}** ({ask.spec})?",
                "offspec": f"would you play **{ask.spec}** on {ask.character} instead of your main spec?",
                "alt": f"could you bring your alt **{ask.character}** ({ask.spec}) instead?",
            }[ask.kind]
            m = reg.members.get(ask.discord_id)
            if m and await self.send_member_dm(reg, m, None, fill_layout(reg, ev, team, self.ico, ask)):
                ev.fill_asks.append(ask)
                sent.append(ask)
            else:
                ask.answer, ask.answered_at = "expired", rc.now()
                ev.fill_asks.append(ask)
        if sent:
            ev.fill_state = "asking"
            ev.log.append(f"fill ({by}): asked {', '.join(a.display_name for a in sent)}")
            rs.save(ev, f"fill asked {len(sent)}")
        elif not [a for a in ev.fill_asks if a.open]:
            ev.fill_state = "exhausted"
            rs.save(ev, "fill: nobody left to ask")
        return sent, nd

    async def after_fill_answer(self, reg, rs, ev, ask, line: str) -> None:
        await self.refresh_sheet(reg, ev)
        cfg = reg.config
        team = cfg.team(ev.team) or {"key": ev.team, "size": 20}
        officer_ch = self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else (self.get_channel(ev.channel_id) if ev.channel_id else None)
        nd = rc.needs(reg, ev, team)
        still = (f"still short {nd['headcount']}" if nd["headcount"] else "") + ("".join(f", {n} {r}" for r, n in nd["roles"].items()))
        await self.post_run_update(reg, ev, f"🧩 {line}" + (f" · {still.strip(', ')}" if still else " · **gaps filled**"))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info", f"fill {ev.key}: {line}")
        if ask.answer == "yes" and ev.state != "open":
            m = reg.members.get(ask.discord_id)
            if m:
                try:
                    reg.roster_add(m.discord_id, ev.team, "fill", ask.character if ask.kind == "alt" else None)
                except Exception:  # noqa: BLE001
                    pass
                a = reg.add_placement_ask(m.discord_id, ev.team, ask.character, "fill")
                a["answer"], a["answered_at"] = "yes", rc.now()
                reg.save(m, f"{m.display_name} seated on {ev.team} via fill")
        if ask.answer == "no":
            await self.run_fill(reg, rs, ev, team, by="answer")

    async def is_officer_anywhere(self, reg, uid: int) -> bool:
        if reg.config.owner_discord_id == uid:
            return True
        guild = self.get_guild(reg.config.discord_guild_id)
        if not guild:
            return False
        m = guild.get_member(uid)
        if m is None:
            try:
                m = await guild.fetch_member(uid)
            except Exception:  # noqa: BLE001
                return False
        return self.officiates(m, guild)

    async def officer_ids(self, reg) -> list[int]:
        guild = self.get_guild(reg.config.discord_guild_id)
        ids = set()
        if reg.config.owner_discord_id:
            ids.add(reg.config.owner_discord_id)
        if guild:
            for m in guild.members:
                if not m.bot and self.officiates(m, guild):
                    ids.add(m.id)
        return sorted(ids)

    async def cleanup_ephemeral(self, reg, rs) -> None:
        """Drop run rosters whose sheet is done/cancelled, and their memberships."""
        cfg = reg.config
        gone = []
        for t in list(cfg.rosters):
            if not t.get("ephemeral"):
                continue
            evs = [e for e in rs.events.values() if e.team == t["key"]]
            if evs and all(e.state in ("done", "cancelled") for e in evs):
                for m in list(reg.members.values()):
                    if reg.on_roster(m, t["key"]):
                        reg.roster_remove(m.discord_id, t["key"], "cleanup")
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
        if gone:
            reg.save_config(f"runs closed: {gone}", notify=False)

    # ---- lock → roster(s) → confirmations
    def officer_channel(self, reg, ev=None):
        """Where officer-facing posts go: the roster channel, else the ops channel, else nowhere (never the public sheet channel)."""
        cfg = reg.config
        return (self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else None) or (self.get_channel(cfg.ops_channel_id) if cfg.ops_channel_id else None)

    async def lock_run(self, reg, rs, ev, by: str = "scheduler") -> str:
        """Lock the sheet, build the roster(s) from the signups (pins honoured), show them on the sheet and in the
        officer channel, and DM every seated member for confirmation."""
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        ev.state = "locked"
        ev.locked_at = rc.now()
        _, _, confirm = run_times(reg, ev, team)
        ev.confirm_by = confirm.isoformat()
        rs.save(ev, f"locked by {by}")
        players, result = await asyncio.to_thread(rc.propose, reg, rs, ev)
        ev.state = "locked"
        rs.save(ev, "roster set")
        await self.refresh_sheet(reg, ev)
        ch = self.officer_channel(reg, ev)
        if ch:
            for i, r in enumerate(ev.all_rosters):
                try:
                    msg = await ch.send(view=lock_layout(reg, ev, team, self.ico, i))
                    ev.cards[f"lock:{i}"], ev.cards_channel_id = msg.id, ch.id
                except Exception as e:  # noqa: BLE001
                    print(f"lock card failed: {e}")
            rs.save(ev, "lock cards")
        sent = await self.send_confirmations(reg, rs, ev, team, by)
        sig = self.get_channel(ev.channel_id) if ev.channel_id else None
        if sig and sig is not ch:
            await sig.send(f"🔒 **{team.get('name', ev.team)}** is locked: {sum(len(r.selected) for r in ev.all_rosters)} seated in {len(ev.all_rosters)} roster(s). Seated members: confirm in your DMs.")
        return f"{ev.key}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)"

    async def confirm_one(self, reg, ev, sg, roster_i: int, group_i: int, by: str) -> bool:
        """Seat one signup on the run's roster and ask them to confirm by DM (auto-confirmed when DMs are off)."""
        m = reg.members.get(sg.discord_id)
        if not m:
            return False
        try:
            reg.roster_add(m.discord_id, ev.team, by, sg.character)
        except Exception:  # noqa: BLE001
            pass
        ask = reg.add_placement_ask(m.discord_id, ev.team, sg.character, by)
        if m.dm_opt_out:
            ask["answer"], ask["answered_at"] = "yes", rc.now()
            reg.save(m, f"{m.display_name} auto-confirmed {ev.team} (DMs off)")
            return False
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        return await self.send_member_dm(reg, m, None, confirm_layout(reg, ev, team, self.ico, sg, roster_i, group_i, m.discord_id))

    async def send_confirmations(self, reg, rs, ev, team, by: str) -> int:
        sent = 0
        for i, r in enumerate(ev.all_rosters):
            for gi, g in enumerate(r.groups):
                for n in g:
                    sg = next((s for s in ev.signups.values() if s.display_name == n), None)
                    if sg and await self.confirm_one(reg, ev, sg, i, gi, by):
                        sent += 1
        ev.log.append(f"confirmation DMs sent to {sent}")
        rs.save(ev, f"confirmations {sent}")
        return sent

    async def after_board_change(self, reg, rs, ev, team, added, removed: list[str], by: str) -> None:
        """Officer edits on a locked board: substitutions get a confirmation DM, removals free the seat and ask the bench."""
        for sg in added:
            seat = ev.seat_of(sg.display_name)
            gi = next((k for k, g in enumerate(ev.all_rosters[seat[0]].groups) if sg.display_name in g), 0) if seat else 0
            await self.confirm_one(reg, ev, sg, seat[0] if seat else 0, gi, by)
        for name in removed:
            m = next((mm for mm in reg.members.values() if mm.display_name == name), None)
            if m and reg.on_roster(m, ev.team):
                reg.roster_remove(m.discord_id, ev.team, by)
        if added or removed:
            await self.post_run_update(reg, ev, f"✏️ board by {by}: " + (f"in {', '.join(s.display_name for s in added)} (asked to confirm)" if added else "") + (" · " if added and removed else "") + (f"out {', '.join(removed)}" if removed else ""))
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if removed and rc.team_setting(team, "autofill"):
            sent, nd = await self.run_fill(reg, rs, ev, team, by=by)
            if sent:
                await self.post_run_update(reg, ev, "🧩 asked " + ", ".join(f"{a.display_name} ({a.kind})" for a in sent))
        await self.ops.emit(reg.config, "info", f"[web] {by} edited the {ev.key} board" + (f": +{len(added)}" if added else "") + (f" -{len(removed)}" if removed else ""))

    async def after_placement_answer(self, reg, uid: int, roster: str, yes: bool, line: str) -> None:
        cfg = reg.config
        rs = self.raids.store(reg)
        ev = next((e for e in rs.live() if e.team == roster), None)
        m = reg.members.get(uid)
        if ev and m and not yes:
            team = cfg.team(ev.team) or {"key": ev.team, "size": 20}
            await self.drop_seated(reg, rs, ev, team, m, "declined", None, announce=False)
        if ev:
            await self.post_run_update(reg, ev, f"{'✅' if yes else '↩️'} {line}")
            await self.refresh_sheet(reg, ev)
            await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info" if yes else "warn", line)

    async def drop_seated(self, reg, rs, ev, team, m, why: str, note: str | None, announce: bool = True) -> None:
        """A seated member is out after lock: free the seat, post it, ask the bench to fill."""
        rc.free_seat(reg, rs, ev, m.display_name, why)
        if reg.on_roster(m, ev.team):
            reg.roster_remove(m.discord_id, ev.team, why)
        if announce:
            await self.post_run_update(reg, ev, f"↩️ {m.display_name} is out ({why}{f': {note}' if note else ''}) — seat freed")
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if rc.team_setting(team, "autofill"):
            sent, nd = await self.run_fill(reg, rs, ev, team, by=why)
            if sent:
                await self.post_run_update(reg, ev, f"🧩 replacement for {m.display_name} — asked " + ", ".join(f"{a.display_name} ({a.kind})" for a in sent))

    # ---- absences ripple into sheets
    async def after_absence(self, reg, m, start: str, end: str, by: str) -> list[str]:
        """Mark every live sheet inside the absence: open → out; locked and seated → seat freed + fill."""
        rs = self.raids.store(reg)
        touched = []
        for ev in rs.live():
            day = ev.start.astimezone(reg.tz).date().isoformat()
            if not (start <= day <= end):
                continue
            team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
            sg = ev.signups.get(str(m.discord_id))
            if ev.state != "open" and ev.seat_of(m.display_name):
                await self.drop_seated(reg, rs, ev, team, m, "absence", None)
            elif not sg or sg.status != "out":
                if m.main:
                    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
                    await self.refresh_sheet(reg, ev)
            touched.append(ev.key)
        return touched

    # ---- scheduler
    async def scheduler(self) -> None:
        """Every minute: open sheets on cadence, post health/nudges, fill, lock, expire confirmations, close."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.scheduler_tick()
            except Exception as e:  # noqa: BLE001
                print(f"scheduler error: {e}")
            await asyncio.sleep(60)

    async def scheduler_tick(self) -> None:
        feed = getattr(self, "feed", None)
        raid = next((e for e in self.events.values() if e.state == "raid"), None)
        if feed and raid:
            for c in list(feed.companions.values()):
                if c.silent_for > 300 and not getattr(c, "warned", False):
                    c.warned = True
                    cfg = next(iter(self.registries.by_discord.values())).config if self.registries.by_discord else None
                    if cfg:
                        await self.ops.emit(cfg, "warn", f"companion {c.character} silent for {int(c.silent_for // 60)} min during {raid.id} — is /chatlog on?")
                elif c.silent_for <= 300:
                    c.warned = False
        for reg in self.registries.by_discord.values():
            cfg = reg.config
            rs = self.raids.store(reg)
            channel = self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None
            await self.cleanup_ephemeral(reg, rs)
            now = reg.now_local()
            # open sheets on cadence: every slot occurrence within its raid's signup lead
            if channel:
                for inst in reg.profile.raids:
                    rd = reg.raid_def(inst)
                    for _slot, start in rc.slot_starts(reg, inst, now, float(rd["signup_lead_hours"])):
                        key = f"{rc.run_key(inst, start)}-{start.date().isoformat()}"
                        if key in rs.events:
                            continue
                        ev = rc.open_run(reg, rs, inst, start)
                        await self.post_sheet(reg, rs, ev, channel)
                        await self.ops.emit(cfg, "info", f"opened {ev.key} ({len(ev.signups)} pre-filled out from absences)")
            for ev in rs.live():
                team = cfg.team(ev.team) or {"key": ev.team, "size": 20}
                ch = self.get_channel(ev.channel_id) if ev.channel_id else channel
                if not ch:
                    continue
                officer_ch = self.officer_channel(reg, ev)
                soft, hard, confirm = run_times(reg, ev, team)
                if ev.state == "open" and not ev.health_posted and now >= soft:
                    await self.post_health(reg, rs, ev, officer_ch, nudge=True)
                    await self.ops.emit(cfg, "info", f"{ev.key}: health check posted, nudged {len(ev.nudged)}")
                if ev.state == "open" and now >= hard:
                    line = await self.lock_run(reg, rs, ev)
                    await self.ops.emit(cfg, "info", line)
                    continue
                if ev.state != "open" and ev.all_rosters:
                    gone = rc.expire_confirmations(reg, rs, ev)
                    if gone:
                        await self.post_run_update(reg, ev, f"⌛ no confirmation from {', '.join(gone)} — seats freed")
                        await self.refresh_sheet(reg, ev)
                        await self.refresh_cards(reg, ev)
                        await self.ops.emit(cfg, "warn", f"{ev.key}: confirmations expired for {len(gone)}")
                if rc.team_setting(team, "autofill") and ev.fill_state in ("idle", "asking") and (ev.health_posted or ev.state != "open"):
                    sent, nd = await self.run_fill(reg, rs, ev, team)
                    if sent:
                        await self.post_run_update(reg, ev, f"🧩 short {nd['headcount']}" + "".join(f", {n} {r}" for r, n in nd["roles"].items()) + " — asked " + ", ".join(f"{a.display_name} ({a.kind})" for a in sent))
                    elif ev.fill_state == "exhausted" and "fill exhausted" not in ev.log:
                        ev.log.append("fill exhausted")
                        rs.save(ev, "fill exhausted")
                        await self.post_run_update(reg, ev, f"🧩 nobody left to ask — short {nd['headcount']}" + "".join(f", {n} {r}" for r, n in nd["roles"].items()))
                if ev.state in ("locked", "proposed", "accepted") and now >= ev.start + timedelta(hours=6):
                    ev.state = "done"
                    rs.save(ev, "done")
                    await self.refresh_sheet(reg, ev)
                    await self.ops.emit(cfg, "info", f"{ev.key}: closed")


# ---------------------------------------------------------------- commands

def register_raid_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot) -> None:
    async def need(interaction: discord.Interaction) -> Registry | None:
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
        return reg

    async def officer(interaction: discord.Interaction) -> Registry | None:
        reg = await need(interaction)
        if reg and not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return None
        return reg

    async def run_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        live = bot.raids.store(reg).live() if reg else []
        return [app_commands.Choice(name=f"{e.team} · {e.start.astimezone(reg.tz).strftime('%a %d %b %H:%M')} · {e.state}"[:100], value=e.team) for e in live if current.lower() in e.team.lower()][:25]

    async def raid_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=reg.raid_def(r).get("name", r), value=r) for r in (reg.profile.raids if reg else []) if current.lower() in r.lower() or current.lower() in reg.raid_def(r).get("name", "").lower()][:25]

    def current_event(reg: Registry, run: str | None):
        rs = bot.raids.store(reg)
        ev = rs.for_team(run) if run else next(iter(rs.live()), None)
        return rs, ev, (reg.config.roster(ev.team) if ev else None) or {"key": run or "?", "size": 20}

    raid = app_commands.Group(name="raid", description="Raid sheets and rosters")

    @raid.command(name="open", description="Officer: open a sheet now — the raid's next slot, or a one-off 'YYYY-MM-DD HH:MM'")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_open(interaction: discord.Interaction, raid: str, when: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        if raid not in reg.profile.raids:
            await interaction.response.send_message(f"Unknown raid. Options: {', '.join(reg.profile.raids)}", ephemeral=True)
            return
        now = reg.now_local()
        try:
            if when:
                start = datetime.fromisoformat(when.strip().replace(" ", "T", 1)).replace(tzinfo=reg.tz)
            else:
                nxt = rc.slot_starts(reg, raid, now, 24 * 14)
                if not nxt:
                    await interaction.response.send_message(f"{raid} has no slots yet (or none before it opens). Set them: `/gm config raid raid:{raid} slots:'Tue 19:30'` — or pass a date/time.", ephemeral=True)
                    return
                start = nxt[0][1]
        except ValueError:
            await interaction.response.send_message("Time looks like `2026-12-10 19:30` (guild time).", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rc.open_run(reg, rs, raid, start, by=interaction.user.display_name)
        channel = bot.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else interaction.channel
        await interaction.response.send_message(f"Opened {ev.key} in {channel.mention}", ephemeral=True)
        if not ev.message_id:
            await bot.post_sheet(reg, rs, ev, channel)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} opened {ev.key}")

    @raid.command(name="sheet", description="Re-post a live sheet")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_sheet(interaction: discord.Interaction, run: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live sheet.", ephemeral=True)
            return
        await interaction.response.send_message(view=sheet_layout(reg, ev, t, bot.ico))
        msg = await interaction.original_response()
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")

    @raid.command(name="health", description="Roster health for a live sheet")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_health(interaction: discord.Interaction, run: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live sheet.", ephemeral=True)
            return
        embed, file = await asyncio.to_thread(health_card, reg, ev, t, bot.ico, rs)
        await interaction.response.send_message(embed=embed, file=file, ephemeral=not is_officer(interaction, reg))

    @raid.command(name="lock", description="Officer: lock a sheet now — roster from the signups, confirmation DMs to everyone seated")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_lock(interaction: discord.Interaction, run: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev or ev.state != "open":
            await interaction.response.send_message("No open sheet to lock.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        line = await bot.lock_run(reg, rs, ev, by=interaction.user.display_name)
        await interaction.followup.send(line, ephemeral=True)
        await ops.emit(reg.config, "info", line)

    @raid.command(name="loot", description="Officer: open the loot council thread for the locked roster")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_loot(interaction: discord.Interaction, run: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev or not ev.roster or ev.state == "open":
            await interaction.response.send_message("Lock the roster first (/raid lock).", ephemeral=True)
            return
        from .discord_bot import MockEvent as LootSession

        existing = next((s for s in bot.events.values() if s.id == ev.key and s.state == "raid"), None)
        if existing:
            await interaction.response.send_message(f"Loot thread already open: <#{existing.raid_thread_id}>", ephemeral=True)
            return
        instance = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
        players = rc.players_for(reg, ev)
        e = discord.Embed(title=f"⚔ {reg.profile.raids[instance]['name']} · {ev.key}", colour=TEAL, description=f"Roster ({len(ev.roster.selected)}): " + ", ".join(p.character or p.signup_name for p in ev.roster.selected)[:3800])
        e.set_footer(text="loot council: tick drops (or let the companion feed do it) → Distribute → chat to adjust → Confirm · /raid end for the summary")
        await interaction.response.send_message(embed=e)
        msg = await interaction.original_response()
        thread = await msg.create_thread(name=f"loot · {ev.key}"[:100])
        session = LootSession(id=ev.key, channel_id=thread.id, instance=instance, date=ev.start.date().isoformat(), guild=reg.key, origin="raid", signups=players, roster=ev.roster, state="raid", raid_thread_id=thread.id)
        bot.events[thread.id] = session
        ev.thread_id = thread.id
        rs.save(ev, "loot thread opened")
        session.save(f"{session.id}: loot session opened")
        await bot.post_boss_tables(session, thread)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} opened loot council for {ev.key}")

    @raid.command(name="end", description="Officer: close the loot council and summarise awards")
    async def raid_end(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        session = bot.event_for(interaction.channel_id)
        if not session or session.origin != "raid":
            await interaction.response.send_message("Run this in the raid's loot thread.", ephemeral=True)
            return
        await bot.end_session(session, interaction)
        rs = bot.raids.store(reg)
        ev = rs.events.get(session.id)
        if ev and ev.state != "done":
            ev.state = "done"
            rs.save(ev, "done (loot closed)")
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} closed loot council {session.id}: {len(session.awards)} awards, {len(session.overrides)} overrides")

    from .discord_registry import register_commands as _rc

    @raid.command(name="set", description="Officer: set someone's answer on a sheet (join / bench / out)")
    @app_commands.choices(status=[app_commands.Choice(name=rc.LABELS[s], value=s) for s in rc.STATUSES])
    @app_commands.autocomplete(run=run_autocomplete, character=_rc.member_char_autocomplete)
    async def raid_set(interaction: discord.Interaction, member: discord.User, status: app_commands.Choice[str], character: str | None = None, run: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        m = reg.members.get(member.id)
        if not ev or not m:
            await interaction.response.send_message("No live sheet, or that member isn't registered.", ephemeral=True)
            return
        if ev.state != "open" and status.value == "out" and ev.seat_of(m.display_name):
            await interaction.response.send_message(f"✅ {m.display_name} taken off the {ev.key} roster; filling the seat.", ephemeral=True)
            await bot.drop_seated(reg, rs, ev, t, m, "officer", None)
            return
        try:
            s = rc.set_signup(reg, rs, ev, m, character, status.value, source="officer")
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {m.display_name}: {s.character} {rc.LABELS[status.value]}", ephemeral=True)
        await bot.refresh_sheet(reg, ev)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} set {m.display_name} {status.value} on {ev.key}")

    @raid.command(name="cancel", description="Officer: cancel a run")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_cancel(interaction: discord.Interaction, run: str | None = None, reason: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live sheet.", ephemeral=True)
            return
        ev.state = "cancelled"
        ev.log.append(f"cancelled by {interaction.user.display_name}: {reason or ''}")
        rs.save(ev, "cancelled")
        await bot.refresh_sheet(reg, ev)
        await interaction.response.send_message(f"Cancelled {ev.key}" + (f": {reason}" if reason else ""))
        await ops.emit(reg.config, "warn", f"{interaction.user.display_name} cancelled {ev.key}" + (f": {reason}" if reason else ""))

    @raid.command(name="list", description="Live runs")
    async def raid_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        rs = bot.raids.store(reg)
        live = rs.live()
        await interaction.response.send_message("\n".join(f"• {e.key} · {e.state} · <t:{int(e.start.timestamp())}:F> · {len(e.by_status('in'))} joined" for e in live) or "No live runs.", ephemeral=True)

    @raid.command(name="out", description="Can't make a run you joined (frees your seat if the roster is locked)")
    @app_commands.autocomplete(run=run_autocomplete)
    async def callout_cmd(interaction: discord.Interaction, note: str | None = None, run: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        m = reg.members.get(interaction.user.id)
        if not ev or not m:
            await interaction.response.send_message("No live run, or you're not registered.", ephemeral=True)
            return
        co = rc.callout(reg, rs, ev, m, t, note)
        await interaction.response.send_message(f"Noted: out for {ev.key} ({co.hours_before:.0f}h before{', after lock' if co.late else ''}). Thanks for saying so.", ephemeral=True)
        if ev.state != "open" and ev.seat_of(m.display_name):
            await bot.drop_seated(reg, rs, ev, t, m, "callout", note)
            return
        await bot.refresh_sheet(reg, ev)
        ch = bot.get_channel(ev.channel_id) if ev.channel_id else None
        if ch:
            await ch.send(f"⚑ {m.display_name} ({co.character}) called out for {ev.key}, {co.hours_before:.0f}h before" + (f": {note}" if note else ""))
        await ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {ev.key} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else ""))

    @raid.command(name="fill", description="Officer: ask the next best people to cover a sheet's gaps (bench, pool, offspec/alt)")
    @app_commands.autocomplete(run=run_autocomplete)
    @app_commands.describe(preview="only show who would be asked")
    async def raid_fill(interaction: discord.Interaction, run: str | None = None, preview: bool = False):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live run.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        nd = rc.needs(reg, ev, t)
        gaps = (f"short {nd['headcount']}" if nd["headcount"] else "headcount ok") + "".join(f" · {n} {r} short" for r, n in nd["roles"].items())
        busy = rc.conflicts(rs, ev)
        if preview:
            cands = await asyncio.to_thread(rc.fill_candidates, reg, rs, ev, t)
            lines = [f"{i + 1}. {a.display_name} — {a.kind}: {a.character} ({a.spec}, {a.role}) for {a.reason}" for i, a in enumerate(cands[:15])]
            open_asks = [a for a in ev.fill_asks if a.open]
            await interaction.followup.send(f"**{ev.key}** · {gaps}" + (f" · double-booked: {', '.join(reg.members[u].display_name for u in busy if u in reg.members)}" if busy else "") + f"\nOutstanding asks: {', '.join(a.display_name for a in open_asks) or 'none'}\nWould ask next:\n" + ("\n".join(lines) or "nobody left"), ephemeral=True)
            return
        sent, nd = await bot.run_fill(reg, rs, ev, t, by=interaction.user.display_name)
        await interaction.followup.send(f"**{ev.key}** · {gaps}\n" + ("Asked: " + ", ".join(f"{a.display_name} ({a.kind}: {a.character})" for a in sent) if sent else ("Nothing to fill." if not (nd["headcount"] or nd["roles"]) else "Nobody left to ask (or the asks outstanding already cover it).")), ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} ran fill for {ev.key}: asked {len(sent)}")

    tree.add_command(raid)
