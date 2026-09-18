"""Discord surface for the signup-driven cycle: sheets with persistent Join / Bench / No thanks buttons,
confirmation DMs after lock, /raid commands, /raid out, and the scheduler loop (open on cadence → health →
fill → lock → confirm → expire → close)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands

from . import raidcycle as rc
from .discord_registry import Guilds, is_officer
from .ops import Ops
from .registry import Registry, RegistryError

log = logging.getLogger(__name__)

from .constants import MARK, ROLES, TEAL  # noqa: E402
CLOCK12 = "%a %d %b %I:%M %p"  # plain-text stamps (thread names, autocomplete) where Discord can't render <t:…>

# How a released seat is explained to the member (drop_seated / expiry). Keys are the `why` values the callers pass.
RELEASE_WHY = {"declined": "you said you can't make it", "callout": "you called out", "callout (test)": "you called out", "absence": "you're marked away",
               "officer": "an officer took you off", "no-confirm": "no confirmation in time", "board": "the board was changed"}


def raid_name(reg: Registry, ev: rc.RaidEvent) -> str:
    return reg.raid_def(ev.instance).get("name", ev.instance or ev.team)


def run_label(reg: Registry, ev: rc.RaidEvent, style: str = "F") -> str:
    """How a run is named to members: the raid name plus a native timestamp (never the run key)."""
    return f"**{raid_name(reg, ev)}** <t:{int(ev.start.timestamp())}:{style}>"


def clock12(reg: Registry, t) -> str:
    """Guild-time 12-hour stamp for places Discord can't render timestamps (thread names, choice labels)."""
    return re.sub(r"\b0(\d:\d\d [AP]M)", r"\1", reg.local(t, CLOCK12))


def run_title(reg: Registry, ev: rc.RaidEvent) -> str:
    return f"{raid_name(reg, ev)} {clock12(reg, ev.start)}"


def sheet_state(ev: rc.RaidEvent) -> str:
    """The four states the sheet and scheduler know: open | locked | done | cancelled (legacy proposed/accepted read as locked)."""
    return ev.state if ev.state in ("open", "done", "cancelled") else "locked"


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


OUT_MARK = {"callout": " ⚑", "absence": " ✈"}  # a No thanks that wasn't a manual press: called out / away


def _member_rows(subs, outs) -> list[str]:
    """The Bench and No thanks member rows, identical on the open and locked sheet."""
    rows = []
    if subs:
        rows.append("**Bench** " + " · ".join(sg.display_name for sg in subs))
    if outs:
        rows.append("**No thanks** " + " · ".join(sg.display_name + OUT_MARK.get(sg.source, "") for sg in outs))
    return rows


def sheet_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico) -> discord.ui.LayoutView:
    """The sheet as a Discord layout message, one explicit layout per state:
    open — header with the raid emblem, class lines with real icons, Bench / No thanks member rows, timeline, Join / Bench / No thanks;
    locked — 🔒 header, the roster(s) group by group (the board's layout, one member per line), Not rostered, Bench / No thanks, Can't make it;
    done — "Finished" header over the roster; cancelled — "Cancelled" header, nothing else. Edited in place on every answer."""
    ui = discord.ui
    state = sheet_state(ev)
    unix = int(ev.start.timestamp())
    ins, subs, outs = (ev.by_status(s) for s in ("in", "sub", "out"))
    rd = reg.raid_def(ev.instance)
    name = rd.get("name", ev.instance or "raid")
    size = int(team.get("size") or rd.get("size") or 20)
    test = bool(team.get("test"))
    tag = "🧪 " if test else ""
    web = os.environ.get("OIBOT_WEB_URL", "")

    def header(text: str):
        return ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=f"{web}/img/raid/{ev.instance}.png")) if web.startswith("https") and ev.instance else ui.TextDisplay(text)

    parts: list = []
    if state == "cancelled":
        parts.append(header(f"## {tag}Cancelled · {name}\n<t:{unix}:F>"))
    elif state == "open":
        counts = {r: sum(1 for s in ins if s.role == r) for r in ROLES}
        runs = max(1, len(ins) // size) if size else 1
        head = f"## {tag}{name}\n<t:{unix}:F> · <t:{unix}:R>\n**{len(ins)}** / {size}" + (f" · {runs} runs" if runs > 1 else "") + "  " + "   ".join(f"{ico('role', r)} {n}" for r, n in counts.items())
        parts += [header(head), ui.Separator()]
        by_cls: dict[str, list] = {}
        for sg in ins:
            by_cls.setdefault(sg.cls, []).append(sg)
        if by_cls:
            lines = [f"{ico('class', cls)} **{cls}** {len(ss)} — " + " · ".join(f"{ico('spec', f'{sg.cls}:{sg.spec}')} {sg.character}" for sg in ss) for cls, ss in sorted(by_cls.items(), key=lambda kv: -len(kv[1]))]
            parts.append(ui.TextDisplay("\n".join(lines)[:3900]))
        else:
            parts.append(ui.TextDisplay("-# Nobody has joined yet."))
        rows = _member_rows(subs, outs)
        if rows:
            parts += [ui.Separator(), ui.TextDisplay("\n".join(rows))]
        _soft, hard, confirm = run_times(reg, ev, team)
        parts += [ui.Separator(), ui.TextDisplay(f"🔒 locks <t:{int(hard.timestamp())}:R> · ✓ confirm by <t:{int(confirm.timestamp())}:f>"),
                  ui.ActionRow(*[SignupButton(ev.key, st) for st in rc.STATUSES]),
                  ui.TextDisplay("-# " + ("test run · " if test else "") + "Join = I'm coming · Bench = call me if you need me")]
    else:  # locked | done: the roster(s) as the board shows them
        rosters = ev.all_rosters
        conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, ev)} if rosters else {}
        marks = {n: MARK.get(a) for n, a in conf.items()}
        tally = f"✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
        if state == "done":
            head = f"## {tag}Finished · {name}\n<t:{unix}:F>" + (f"\n**{len(ev.seated())}** rostered" if rosters else "")
        else:
            head = f"## {tag}🔒 {name}\n<t:{unix}:F> · <t:{unix}:R>\n" + (f"🔒 **{len(ev.seated())}** rostered" + (f" in {len(rosters)} rosters" if len(rosters) > 1 else "") + f" · {tally}" if rosters else "-# Building the roster…")
        parts += [header(head), ui.Separator()]
        for i, r in enumerate(rosters):
            if len(rosters) > 1:
                parts.append(ui.TextDisplay(f"### Roster {i + 1}"))
            summaries = _group_summaries(reg, r)
            for gi in range(len(r.groups)):
                if r.groups[gi]:
                    parts.append(ui.TextDisplay(_group_block(reg, ico, r, gi, marks, summaries, show_missing=False)))
        rows = []
        if rosters:
            joined = {sg.display_name for sg in ins}
            left_off = [p.signup_name for p in rosters[0].benched if p.signup_name in joined]  # joiners the solver left off; Bench / No thanks people sit in their own rows
            if left_off:
                rows.append("**Not rostered** " + " · ".join(left_off))
        rows += _member_rows(subs, outs)
        if rows:
            parts += [ui.Separator(), ui.TextDisplay("\n".join(rows))]
        if state == "locked":
            _soft, _hard, confirm = run_times(reg, ev, team)
            parts += [ui.Separator(), ui.TextDisplay(f"✓ confirm by <t:{int(confirm.timestamp())}:f>"),
                      ui.ActionRow(SignupButton(ev.key, "cant")),
                      ui.TextDisplay("-# " + ("test run · " if test else "") + "Rostered? answer your DM. Not rostered? nothing to do.")]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=0x8C97A8 if test else 0x98A3B5 if state in ("done", "cancelled") else TEAL))
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


def ask_line(reg: Registry, ico, ask) -> str:
    """One thread line per fill ask: who, what they'd play (spec icon + character), and what it covers."""
    c = reg.find(ask.character)
    what = (ico("spec", f"{c[1].cls}:{ask.spec}") + " " if c else "") + f"**{ask.character}**"
    if ask.kind == "offspec":
        return f"asked {ask.display_name} to swap to {ask.spec} on {what}"
    if ask.kind == "alt":
        return f"asked {ask.display_name} to swap to {what}"
    tail = f" ({ask.reason})" if ask.reason.startswith("covering") else ""
    return f"asked {ask.display_name} to come as {what}{tail}"


def _role_counts(ico, players) -> str:
    counts = {r: sum(1 for p in players if p.role == r) for r in ROLES}
    return "   ".join(f"{ico('role', r)} {n}" for r, n in counts.items())


def gaps_text(ico, nd: dict) -> str:
    """What a run is short, from rc.needs(): seats as a number, role shortfalls as role icon + number (never "2 tank")."""
    bits = [f"{nd['headcount']} seat{'s' if nd['headcount'] != 1 else ''}"] if nd["headcount"] else []
    bits += [f"{ico('role', r)} {n}" for r, n in nd["roles"].items()]
    return "   ".join(bits) if bits else "nothing"


def _group_summaries(reg: Registry, r) -> list[dict]:
    """Per-group present/missing auras for a locked roster (same coverage code as the site)."""
    from . import comp as comp_mod
    from .roster import coverage as cov_mod

    try:
        cov = cov_mod.compute(reg.profile, r.selected, r)
        return comp_mod.groups_summary(reg, r.selected, r, cov)["groups"]
    except Exception:  # noqa: BLE001
        return []


def _aura_line(ico, g: dict | None, show_missing: bool = True) -> str:
    """Group buffs as icons only: the present set, then (when shown) the missing set behind a single ⛔ marker.
    Discord can't grey an emoji, so the marker is the tint; the tooltips carry the names."""
    if not g:
        return ""
    present = " ".join(ico("buff", a["id"]) for a in g["present"] if ico("buff", a["id"]))
    missing = " ".join(ico("buff", a["id"]) for a in g["missing"][:6] if ico("buff", a["id"])) if show_missing else ""
    out = present
    if missing:
        out += ("  ·  " if out else "") + f"⛔ {missing}"
    return out


def _group_block(reg: Registry, ico, r, gi: int, marks: dict | None = None, summaries: list[dict] | None = None, title: str | None = None, me: str | None = None, show_missing: bool = True) -> str:
    """One group as the web board shows it: heading, one member per line (spec icon + name), its auras under it.
    `me` = the reader's signup name: their line is bold with a pointer."""
    members = [next((p for p in r.selected if p.signup_name == n), None) for n in r.groups[gi]]
    lines = [f"**{title or f'Group {gi + 1}'}**"]
    for p in members:
        if p:
            mark = (marks or {}).get(p.signup_name)
            name = p.character or p.signup_name
            lines.append(f"{(mark + ' ') if mark else ''}{ico('spec', f'{p.cls}:{p.spec}')} " + (f"**{name}** ◀" if p.signup_name == me else name))
    aura = _aura_line(ico, summaries[gi] if summaries and gi < len(summaries) else None, show_missing)
    if aura:
        lines.append("-# " + aura)
    return "\n".join(lines)


def _raidwide_line(reg: Registry, ico, r, show_missing: bool = True) -> str:
    from . import comp as comp_mod

    rb = comp_mod.raid_buff_status(reg.profile, r.selected)
    have = " ".join(ico("buff", b["id"]) for b in rb if b["providers"] and ico("buff", b["id"]))
    miss = " ".join(ico("buff", b["id"]) for b in rb if not b["providers"] and ico("buff", b["id"])) if show_missing else ""
    out = have
    if miss:
        out += ("  ·  " if out else "") + f"⛔ {miss}"
    return out




def run_actions_row(ev: rc.RaidEvent, locked: bool):
    ui = discord.ui
    items = []
    url = board_url(ev)
    if url:
        items.append(ui.Button(label="Open the board", style=discord.ButtonStyle.link, url=url))
    if locked:  # the fill engine only runs on a locked roster
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
    short = [r for r in h["roles"] if r["need"] and r["have"] < r["need"] * max(1, runs)]
    if short:
        body.append("**Short** " + "   ".join(f"{ico('role', r['role'])} {r['need'] * max(1, runs) - r['have']}" for r in short))
        hints = [r["hint"] for r in short if r["hint"]]
        if hints:
            body.append("-# " + " · ".join(hints))  # `-#` only renders at the start of a line
    missing = [b for b in h["buffs"] if not b["providers"]]
    if missing:
        body.append("⛔ " + " ".join(ico("buff", b["id"]) for b in missing if ico("buff", b["id"])))
    if h["unresponsive"]:
        body.append(f"**No answer** {len(h['unresponsive'])}" + (" · nudged" if ev.nudged else ""))
    busy = rc.conflicts(rs, ev) if rs is not None else {}
    double = [reg.members[u].display_name for u in busy if u in reg.members and str(u) in ev.signups and ev.signups[str(u)].status == "in"]
    if double:
        body.append("**Double-booked** " + ", ".join(double))
    _soft, hard, confirm = run_times(reg, ev, team)
    parts = [head, ui.Separator()] + ([ui.TextDisplay("\n".join(body)), ui.Separator()] if body else [])
    parts.append(ui.TextDisplay(f"🔒 locks <t:{int(hard.timestamp())}:R> · ✓ confirm by <t:{int(confirm.timestamp())}:f>"))
    parts.append(run_actions_row(ev, locked=False))
    worst = "red" if short else "amber" if missing or h["unresponsive"] else "green"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour={"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]))
    return view


def lock_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, i: int) -> discord.ui.LayoutView:
    """One locked roster as a card: groups with confirmation marks and auras, raid-wide row, bench, waiting-on, buttons."""
    ui = discord.ui
    r = ev.all_rosters[i]
    rows = [c for c in rc.confirmations(reg, ev) if c["roster"] == i + 1]
    conf = {c["display_name"]: c["answer"] for c in rows}
    via_site = {c["display_name"] for c in rows if c["channel"] == "web"}  # DMs off: the ask waits on the Me page
    marks = {n: MARK.get(a) for n, a in conf.items()}
    tally = f"✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
    rd = reg.raid_def(ev.instance)
    head = _header(reg, ev, f"🔒 {rd.get('name', ev.instance)}" + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else ""),
                   [f"**{len(r.selected)}** rostered" + (f" · synergy {r.synergy_value}" if r.synergy_value else "") + "   " + _role_counts(ico, r.selected), tally])
    summaries = _group_summaries(reg, r)
    parts = [head, ui.Separator()]
    for gi in range(len(r.groups)):
        if r.groups[gi]:
            parts.append(ui.TextDisplay(_group_block(reg, ico, r, gi, marks, summaries)))
    tail = ["-# " + _raidwide_line(reg, ico, r)]
    if i == 0 and r.benched:
        tail.append("**Bench** " + " · ".join(p.signup_name for p in r.benched))
    pending = [n + (" (site)" if n in via_site else "") for n, a in conf.items() if a is None]
    if pending:
        tail.append("**Waiting on** " + ", ".join(pending[:15]))
    _soft, _hard, confirm = run_times(reg, ev, team)
    tail.append(f"-# ✓ confirm by <t:{int(confirm.timestamp())}:f> · unanswered then counts as out")
    parts += [ui.Separator(), ui.TextDisplay("\n".join(tail)), run_actions_row(ev, locked=True)]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=TEAL))
    return view


def confirm_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, sg, i: int, gi: int, uid: int) -> discord.ui.LayoutView:
    """Confirmation DM: your seat, the group you'd be in (the board's layout), the deadline, Confirm / Can't make it."""
    ui = discord.ui
    r = ev.all_rosters[i]
    rd = reg.raid_def(ev.instance)
    head = _header(reg, ev, f"You're rostered · {rd.get('name', ev.instance)}", [f"{ico('spec', f'{sg.cls}:{sg.spec}')} **{sg.character}**" + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else "") + f" · Group {gi + 1}"])
    _s, _h, confirm = run_times(reg, ev, team)
    summaries = _group_summaries(reg, r)
    parts = [head, ui.Separator()]
    for k in range(len(r.groups)):  # the whole roster, group by group; the reader's own line is bold; buffs the group has
        if r.groups[k]:
            parts.append(ui.TextDisplay(_group_block(reg, ico, r, k, None, summaries, me=sg.display_name, show_missing=False)))
    rw = _raidwide_line(reg, ico, r, show_missing=False)
    if rw:
        parts.append(ui.TextDisplay("-# " + rw))
    parts += [ui.Separator(),
             ui.TextDisplay(f"-# Confirm keeps the seat. Can't make it frees it for someone on the bench. Unanswered by <t:{int(confirm.timestamp())}:f> counts as out."),
             ui.ActionRow(PlaceButton(ev.team, uid, "yes"), PlaceButton(ev.team, uid, "no"))]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=TEAL))
    return view


def fill_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, ask) -> discord.ui.LayoutView:
    """Fill DM: what opened, the seat offered (character + spec), the group they'd join, Yes / Can't."""
    ui = discord.ui
    rd = reg.raid_def(ev.instance)
    what = {"sub": "you're on the bench", "pool": "you haven't answered the sheet", "other_roster": "you're free that night", "offspec": f"you could play **{ask.spec}** instead of your main spec", "alt": "you could bring your alt"}[ask.kind]
    c = reg.find(ask.character)
    cls = c[1].cls if c else ""
    lines = [f"{ask.reason} — {what}", f"{'Swap to' if ask.swap else 'Come as'} {ico('spec', f'{cls}:{ask.spec}') if cls else ''} **{ask.character}** ({ask.spec})"]
    partner = rc.partner_of(ev, ask)
    if partner and ask.swap:
        lines.append(f"{partner.display_name} is being asked to cover your {ask.vacates} seat; the swap only happens if you both say yes.")
    elif partner:
        lines.append(f"{partner.display_name} would swap to {partner.role} to make room; this only happens if you both say yes.")
    head = _header(reg, ev, f"{'A swap request' if ask.swap else 'A seat opened'} · {rd.get('name', ev.instance)}", lines)
    parts = [head]
    if ev.all_rosters:
        for i, r in enumerate(ev.all_rosters):
            gi = next((k for k, g in enumerate(r.groups) if len(g) < int(reg.profile.comp_rules["group_size"])), None)
            if gi is not None:
                parts += [ui.Separator(), ui.TextDisplay(_group_block(reg, ico, r, gi, None, _group_summaries(reg, r), title=f"You'd join Group {gi + 1}" + (f" of Roster {i + 1}" if len(ev.all_rosters) > 1 else ""), show_missing=False))]
                break
    deadline = f" No answer by <t:{int(datetime.fromisoformat(ask.expires_at).timestamp())}:f> counts as no." if ask.expires_at else ""
    parts += [ui.Separator(), ui.TextDisplay(("-# Yes makes the swap at once." if ask.swap else "-# Yes puts you straight in the seat.") + " A no asks the next person." + deadline), ui.ActionRow(FillButton(ev.key, ask.discord_id, "yes"), FillButton(ev.key, ask.discord_id, "no"))]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=0xE0A448))
    return view


def closed_layout(reg: Registry, ev: rc.RaidEvent, ico) -> discord.ui.LayoutView:
    """What a run's officer cards become once it is cancelled or finished: the header and the last log line, no buttons."""
    ui = discord.ui
    title = ("Cancelled" if ev.state == "cancelled" else "Finished") + f" · {raid_name(reg, ev)}"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(_header(reg, ev, title, [f"-# {ev.log[-1]}"] if ev.log else []), accent_colour=0x98A3B5))
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
        team = rc.run_team(reg, ev)
        by = interaction.user.display_name
        if self.action == "fill":
            if sheet_state(ev) != "locked" or not ev.all_rosters:
                await interaction.response.send_message("Fill works after lock — the sheet is still open.", ephemeral=True)
                return
            nd = rc.needs(reg, ev, team)
            if not nd["headcount"] and not nd["roles"]:
                await interaction.response.send_message("Nothing to fill — every seat is taken.", ephemeral=True)
                return
            batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
            outstanding = [a for a in ev.fill_asks if a.open]
            lines = [f"**Short** {gaps_text(bot.ico, nd)}"]
            if outstanding:
                lines.append(f"**Already asked, waiting:** {', '.join(a.display_name for a in outstanding)}")
            if batch:
                lines.append("**Will DM now:**\n" + "\n".join("• " + ask_line(reg, bot.ico, a) + (" — tied to " + next((b.display_name for b in batch if b.discord_id == a.pair), "?") if a.pair else "") for a in batch))
                hrs = reg.raid_def(ev.instance).get("fill_ask_hours")
                lines.append(f"-# Each gets Yes / Can't this time. A yes takes the seat at once; a no asks the next person; no answer within {hrs:g} h counts as no. Tied asks go out together and a no from either withdraws the other. Never more than 3 questions out at a time. Answers post in this run's thread.")
            else:
                lines.append("**Nobody to ask right now** — either the 3 outstanding questions already cover it, or everyone eligible has been asked.")
            view = discord.ui.View(timeout=120)
            go = discord.ui.Button(label=f"Send {len(batch)} ask{'s' if len(batch) != 1 else ''}", style=discord.ButtonStyle.success, disabled=not batch)
            no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

            async def send(i: discord.Interaction):
                await i.response.edit_message(content="Sending…", view=None)
                sent, nd2 = await bot.run_fill(reg, rs, ev, team, by=by)
                await i.edit_original_response(content=("Asked " + ", ".join(a.display_name for a in sent)) if sent else "Nobody could be asked.")
                if sent:
                    await bot.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, bot.ico, a) for a in sent))

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
            try:
                line = await bot.lock_run(reg, rs, ev, by=by)
            except Exception as e:  # noqa: BLE001
                line = f"lock failed: {e}"
            await interaction.followup.send(line, ephemeral=True)
            if not ev.lock_error:
                await bot.ops.emit(reg.config, "info", line)
            return
        if self.action == "cancel":
            view = discord.ui.View(timeout=60)
            yes = discord.ui.Button(label="Cancel the run", style=discord.ButtonStyle.danger)

            async def do(i: discord.Interaction):
                await i.response.edit_message(content="Cancelling…", view=None)
                line = await bot.cancel_run(reg, rs, ev, by=by, reason=None)
                await i.edit_original_response(content=line)

            yes.callback = do
            view.add_item(yes)
            await interaction.response.send_message(f"Cancel {run_label(reg, ev)}? Rostered members are not told automatically.", view=view, ephemeral=True)


# ---------------------------------------------------------------- persistent buttons

class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid:(?P<key>[A-Za-z0-9_\-]+):(?P<status>in|tentative|out|sub|cant)"):
    """The sheet's member buttons: Join / Bench / No thanks while open; a single Can't make it once locked, which
    releases a rostered member's seat (anyone else: nothing to do)."""
    STYLES = {"in": discord.ButtonStyle.success, "sub": discord.ButtonStyle.primary, "out": discord.ButtonStyle.secondary, "cant": discord.ButtonStyle.danger}

    def __init__(self, key: str, status: str):
        status = "in" if status == "tentative" else status
        super().__init__(discord.ui.Button(label="Can't make it" if status == "cant" else rc.LABELS[status], style=self.STYLES[status], custom_id=f"raid:{key}:{status}"))
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
        if not ev or sheet_state(ev) in ("done", "cancelled"):
            await interaction.response.send_message("This sheet is closed.", ephemeral=True)
            return
        team = rc.run_team(reg, ev)
        m = reg.members.get(interaction.user.id)
        if team.get("test") and not (m and getattr(m, "test", False)) and str(interaction.user.id) != str(team.get("test_by") or ""):
            await interaction.response.send_message("This is a rehearsal sheet.", ephemeral=True)
            return
        if not m or not m.active():
            where = f"in <#{reg.config.registration_channel_id}>" if reg.config.registration_channel_id else "with `/register`"
            await interaction.response.send_message(f"You're not registered yet — register a character {where} first, then press again.", ephemeral=True)
            return
        if self.status == "cant":
            if sheet_state(ev) == "open":
                await interaction.response.send_message("The sheet is still open — press **No thanks** instead.", ephemeral=True)
                return
            if not ev.seat_of(m.display_name):
                await interaction.response.send_message("You're not rostered for this run — nothing to do.", ephemeral=True)
                return
            await bot.apply_signup(interaction, reg, rs, ev, m, None, "out")
            return
        if sheet_state(ev) != "open":
            await interaction.response.send_message("The roster is locked — answer your confirmation DM, or press Can't make it on the sheet.", ephemeral=True)
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
                thread = await msg.create_thread(name=f"{run_title(reg, ev)} · updates"[:100])
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
        team = rc.run_team(reg, ev)
        rs = self.raids.store(reg)
        for key, mid in list(ev.cards.items()):
            try:
                msg = await ch.fetch_message(mid)
                if ev.state in ("done", "cancelled"):
                    await msg.edit(view=closed_layout(reg, ev, self.ico))
                elif key == "health":
                    if ev.state == "open":
                        await msg.edit(view=health_layout(reg, rs, ev, team, self.ico))
                elif key.startswith("lock:"):
                    i = int(key[5:])
                    if i < len(ev.all_rosters):
                        await msg.edit(view=lock_layout(reg, ev, team, self.ico, i))
            except Exception as e:  # noqa: BLE001
                log.warning("card refresh failed (%s %s): %s", ev.key, key, e)

    async def apply_signup(self, interaction: discord.Interaction, reg, rs, ev, m, character, status):
        """A member's own press (or an officer pressing for a test puppet): set_answer, the line back ephemerally."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            line = await self.set_answer(reg, rs, ev, m, status, character, by=m.display_name)
        except ValueError as e:
            line = f"❌ {e}"
        await interaction.followup.send(line, ephemeral=True)

    async def set_answer(self, reg, rs, ev, m, status: str, character: str | None, by: str) -> str:
        """The one path for setting someone's answer on a sheet — the member (by == their name) or an officer.
        Open sheet: the signup + sheet refresh. Locked: Join seats them (if not already) and asks them to confirm;
        No thanks frees a rostered seat (drop_seated: DM, thread line, fill); Bench is the signup only. Returns a line."""
        if status not in rc.STATUSES:
            raise ValueError(f"status must be one of {rc.STATUSES}")
        mine = by == m.display_name
        source = "member" if mine else "officer"
        team = rc.run_team(reg, ev)
        label = run_label(reg, ev)
        who = "" if mine else f"{m.display_name}: "
        if sheet_state(ev) == "open":
            s = rc.set_signup(reg, rs, ev, m, character, status, source=source)
            await self.refresh_sheet(reg, ev)
            line = f"✅ {who}{s.character} **{rc.LABELS[s.status]}** for {label}" + (f" — {s.note}" if s.note and s.status != status else "")
        elif status == "out":
            if ev.seat_of(m.display_name):
                await self.drop_seated(reg, rs, ev, team, m, "callout" if mine else "officer", None)
                line = f"✅ {who or 'you are '}off the {label} roster — seat freed" + ("; the bot is looking for a replacement" if rc.team_setting(team, "autofill") else "")
            else:
                rc.set_signup(reg, rs, ev, m, character, "out", source=source)
                await self.refresh_sheet(reg, ev)
                line = f"✅ {who}**No thanks** for {label} (wasn't rostered)"
        elif status == "in":
            s = rc.set_signup(reg, rs, ev, m, character, "in", source=source)
            seat = ev.seat_of(m.display_name)
            if seat:
                rc._reseat(ev, s)  # a character swap keeps the seat
                rs.save(ev, f"{m.display_name} seat updated")
                line = f"✅ {who}{s.character} stays rostered for {label}"
            else:
                i = rc.seat_player(reg, ev, s)
                if i is None:
                    rs.save(ev, f"{m.display_name} in, no seat")
                    line = f"✅ {who}{s.character} **Join** for {label} — every seat is taken, so not rostered (move them in on the board)"
                else:
                    rs.save(ev, f"{m.display_name} rostered by {by}")
                    gi = next((k for k, g in enumerate(ev.all_rosters[i].groups) if m.display_name in g), 0)
                    sent = await self.confirm_one(reg, ev, s, i, gi, by)
                    await self.post_run_update(reg, ev, f"✏️ {by} rostered {m.display_name} ({s.character})" + (" — asked to confirm" if sent else " — DMs off, the ask waits on the site" if m.dm_opt_out else ""))
                    line = f"✅ {who}{s.character} rostered for {label}" + (" — asked to confirm by DM" if sent else " — DMs off: the confirmation waits on the site" if m.dm_opt_out else "")
            await self.refresh_sheet(reg, ev)
            await self.refresh_cards(reg, ev)
        else:  # sub after lock: the signup only; a rostered member keeps the seat until set out
            s = rc.set_signup(reg, rs, ev, m, character, "sub", source=source)
            await self.refresh_sheet(reg, ev)
            line = f"✅ {who}{s.character} **Bench** for {label}" + (" — still rostered; set No thanks to free the seat" if ev.seat_of(m.display_name) else "")
        if not mine:
            await self.ops.emit(reg.config, "info", f"{by} set {m.display_name} {status} on {ev.key}")
        return line

    async def refresh_sheet(self, reg, ev) -> None:
        team = rc.run_team(reg, ev)
        if ev.channel_id and ev.message_id:
            ch = self.get_channel(ev.channel_id)
            try:
                msg = await ch.fetch_message(ev.message_id)
                await msg.edit(view=sheet_layout(reg, ev, team, self.ico))
            except Exception as e:  # noqa: BLE001
                log.warning("sheet refresh failed (%s): %s", ev.key, e)

    async def post_sheet(self, reg, rs, ev, channel) -> None:
        team = rc.run_team(reg, ev)
        msg = await channel.send(view=sheet_layout(reg, ev, team, self.ico))
        ev.channel_id, ev.message_id = channel.id, msg.id
        rs.save(ev, "sheet posted")
        if rc.team_setting(team, "open_dm"):
            await self.dm_on_open(reg, rs, ev, team)

    async def dm_on_open(self, reg, rs, ev, team) -> int:
        """Optional per-run: DM every registered raider when the sheet opens, with the same buttons."""
        unix = int(ev.start.timestamp())
        _soft, hard, _confirm = run_times(reg, ev, team)
        sent = 0
        for m in reg.team_pool(team["key"]):
            if not m.main or m.dm_opt_out:
                continue
            s = ev.signups.get(str(m.discord_id))
            status = f"You're pre-filled as **{rc.LABELS.get(s.status, s.status)}** on {s.character}" if s else "You haven't answered yet"
            if await self.send_member_dm(reg, m, f"{reg.config.name} · {run_label(reg, ev)} (<t:{unix}:R>). {status}. Join / Bench / No thanks:" + (f" (sheet: <#{ev.channel_id}>)" if ev.channel_id else "") + f"\n-# The roster locks <t:{int(hard.timestamp())}:f>; no answer by then means you're not on it.", sheet_view(ev.key)):
                sent += 1
        ev.log.append(f"open DMs sent to {sent}")
        rs.save(ev, f"open DMs {sent}")
        return sent

    async def post_health(self, reg, rs, ev, channel, nudge: bool) -> None:
        team = rc.run_team(reg, ev)
        if channel is not None:
            try:
                msg = await channel.send(view=health_layout(reg, rs, ev, team, self.ico))
                ev.cards["health"], ev.cards_channel_id = msg.id, channel.id
            except Exception as e:  # noqa: BLE001
                log.warning("health card failed (%s): %s", ev.key, e)
        if nudge and rc.team_setting(team, "reminders") != "none":
            targets = [m for m in reg.team_pool(team["key"]) if m.main and str(m.discord_id) not in ev.signups and m.discord_id not in ev.nudged and not m.dm_opt_out]
            _soft, hard, _confirm = run_times(reg, ev, team)
            for m in targets:  # same buttons as the sheet (and the open DM), so they can answer right here
                if await self.send_member_dm(reg, m, f"{reg.config.name}: the {run_label(reg, ev)} sheet is still waiting for you — Join, Bench or No thanks here or in <#{ev.channel_id}>.\n-# The roster locks <t:{int(hard.timestamp())}:f>; no answer by then means you're not on it.", sheet_view(ev.key)):
                    ev.nudged.append(m.discord_id)
            if targets:
                rs.save(ev, f"nudged {len(targets)}")
        ev.health_posted = True
        rs.save(ev, "health posted")

    # ---- filling gaps by DM
    async def run_fill(self, reg, rs, ev, team, by: str = "scheduler") -> tuple[list, dict]:
        """Send the next batch of fill DMs. Returns (asks sent, needs). The fill engine only runs on a locked roster:
        before lock nothing is sent, whoever calls (scheduler, card button, /raid fill, the web board)."""
        nd = rc.needs(reg, ev, team)
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            return [], nd
        if not nd["headcount"] and not nd["roles"]:
            if ev.fill_state == "asking":
                ev.fill_state = "filled"
                rs.save(ev, "fill: complete")
            return [], nd
        batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
        sent = []
        deadline = rc.ask_deadline(reg, ev).isoformat()
        for ask in batch:  # tied pairs sit next to each other in the batch; both are on the event before either DM goes out
            ask.expires_at = deadline
            ev.fill_asks.append(ask)
        for ask in batch:
            m = reg.members.get(ask.discord_id)
            if m and await self.send_member_dm(reg, m, None, fill_layout(reg, ev, team, self.ico, ask)):
                sent.append(ask)
            else:
                ask.answer, ask.answered_at = "expired", rc.now()
                rc.release_partner(reg, rs, ev, ask)
        if sent:
            ev.fill_state = "asking"
            ev.log.append(f"fill ({by}): asked {', '.join(a.display_name for a in sent)}")
            rs.save(ev, f"fill asked {len(sent)}")
        elif not [a for a in ev.fill_asks if a.open]:
            ev.fill_state = "exhausted"
            rs.save(ev, "fill: nobody left to ask")
        return sent, nd

    async def withdraw_ask(self, reg, ev, ask, because: str) -> None:
        """Tell the other half of a tied pair the question is off."""
        m = reg.members.get(ask.discord_id)
        if m:
            await self.send_member_dm(reg, m, f"Never mind the {'swap' if ask.swap else 'seat'} question for **{reg.raid_def(ev.instance).get('name', ev.instance)}** <t:{int(ev.start.timestamp())}:F> — {because}. Nothing to do.")
        await self.post_run_update(reg, ev, f"↩️ withdrew the ask to {ask.display_name} ({because})")

    async def after_fill_answer(self, reg, rs, ev, ask, line: str) -> None:
        await self.refresh_sheet(reg, ev)
        cfg = reg.config
        team = rc.run_team(reg, ev)
        if ask.answer != "yes":
            other = rc.release_partner(reg, rs, ev, ask)
            if other:
                await self.withdraw_ask(reg, ev, other, f"{ask.display_name} said no")
        nd = rc.needs(reg, ev, team)
        await self.post_run_update(reg, ev, f"🧩 {line}" + (f" · still short {gaps_text(self.ico, nd)}" if nd["headcount"] or nd["roles"] else " · **gaps filled**"))
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
                reg.save(m, f"{m.display_name} rostered on {ev.team} via fill")
        if ask.answer == "no":
            await self.run_fill(reg, rs, ev, team, by="answer")

    async def is_officer_anywhere(self, reg, uid: int) -> bool:
        if reg.config.owner_discord_id == uid:
            return True
        guild = self.get_guild(reg.config.discord_guild_id)
        if not guild:
            return False
        m = await self.cached_member(guild, uid)  # TTL cache on the client: button presses don't hit the API each time
        return bool(m) and self.officiates(m, guild)

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
        officer channel, and DM every rostered member for confirmation."""
        team = rc.run_team(reg, ev)
        # solve on a copy first: the run only becomes locked once a roster exists, so a failed solve (or a crash
        # mid-way) leaves the sheet open rather than a locked run with no roster
        trial = ev.model_copy(deep=True)
        ev.lock_tried_at = rc.now()
        try:
            await asyncio.to_thread(rc.propose, reg, rs, trial, False)
        except Exception as e:  # noqa: BLE001 — solver infeasible / timed out
            why = rc.solver_error_text(e)
            first = ev.lock_error != why
            ev.lock_error = why
            ev.log.append(f"lock ({by}) failed: {why}")
            rs.save(ev, "lock failed")
            if first:
                await self.post_run_update(reg, ev, f"⚠️ lock by {by} failed — {why}. The sheet stays open; fix the board or the raid rules and lock again.")
            await self.ops.emit(reg.config, "warn", f"{ev.key}: lock failed — {why}")
            return f"{ev.key}: lock failed — {why}"
        _, _, confirm = run_times(reg, ev, team)
        ev.rosters, ev.log = trial.rosters, trial.log
        ev.state, ev.locked_at, ev.confirm_by, ev.lock_error = "locked", rc.now(), confirm.isoformat(), None
        ev.log.append(f"locked by {by}")
        rs.save(ev, f"locked by {by}")
        await self.refresh_sheet(reg, ev)
        ch = self.officer_channel(reg, ev)
        if ch:
            for i, r in enumerate(ev.all_rosters):
                try:
                    msg = await ch.send(view=lock_layout(reg, ev, team, self.ico, i))
                    ev.cards[f"lock:{i}"], ev.cards_channel_id = msg.id, ch.id
                except Exception as e:  # noqa: BLE001
                    log.warning("lock card failed (%s roster %d): %s", ev.key, i + 1, e)
            rs.save(ev, "lock cards")
        else:
            await self.ops.emit(reg.config, "warn", f"{ev.key}: no roster/ops channel set — lock cards not posted")
        sent = await self.send_confirmations(reg, rs, ev, team, by)
        sig = self.get_channel(ev.channel_id) if ev.channel_id else None
        if sig and sig is not ch:
            try:
                await sig.send(f"🔒 {run_label(reg, ev)} is locked: {sum(len(r.selected) for r in ev.all_rosters)} rostered" + (f" in {len(ev.all_rosters)} rosters" if len(ev.all_rosters) > 1 else "") + ". Rostered? confirm in your DMs. Not rostered? nothing to do.", allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:  # noqa: BLE001
                log.warning("lock line failed (%s): %s", ev.key, e)
        return f"{ev.key}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)"

    async def confirm_one(self, reg, ev, sg, roster_i: int, group_i: int, by: str) -> bool:
        """Seat one signup on the run's roster and ask them to confirm: by DM, or — DMs off — as a pending ask on the
        Me page (channel "web"). Never auto-confirmed: the tally counts them as waiting until they answer, and an
        unanswered ask expires like any other. Returns True when a DM went out."""
        m = reg.members.get(sg.discord_id)
        if not m:
            return False
        try:
            reg.roster_add(m.discord_id, ev.team, by, sg.character)
        except Exception:  # noqa: BLE001
            pass
        reg.add_placement_ask(m.discord_id, ev.team, sg.character, by, channel="web" if m.dm_opt_out else "dm")
        if m.dm_opt_out:
            return False
        team = rc.run_team(reg, ev)
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
        if removed and rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=by)
            if sent:
                await self.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        await self.ops.emit(reg.config, "info", f"[web] {by} edited the {ev.key} board" + (f": +{len(added)}" if added else "") + (f" -{len(removed)}" if removed else ""))

    async def after_placement_answer(self, reg, uid: int, roster: str, yes: bool, line: str) -> None:
        cfg = reg.config
        rs = self.raids.store(reg)
        ev = next((e for e in rs.live() if e.team == roster), None)
        m = reg.members.get(uid)
        if ev and m and not yes:
            team = rc.run_team(reg, ev)
            await self.drop_seated(reg, rs, ev, team, m, "declined", None, announce=False)
        if ev:
            await self.post_run_update(reg, ev, f"{'✅' if yes else '↩️'} {line}")
            await self.refresh_sheet(reg, ev)
            await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info" if yes else "warn", line)

    async def dm_seat_released(self, reg, ev, m, why: str) -> bool:
        """One short DM whenever a member's seat on a locked roster is released, whoever released it."""
        reason = RELEASE_WHY.get(why, why)
        return await self.send_member_dm(reg, m, f"Your seat on {run_label(reg, ev)} was released ({reason}). Nothing else to do — if that's wrong, tell an officer.")

    async def drop_seated(self, reg, rs, ev, team, m, why: str, note: str | None, announce: bool = True) -> None:
        """A rostered member is out after lock: free the seat, tell them, post it, ask the bench to fill."""
        rc.free_seat(reg, rs, ev, m.display_name, why)
        if reg.on_roster(m, ev.team):
            reg.roster_remove(m.discord_id, ev.team, why)
        await self.dm_seat_released(reg, ev, m, why)
        if announce:
            await self.post_run_update(reg, ev, f"↩️ {m.display_name} is out ({why}{f': {note}' if note else ''}) — seat freed")
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=why)
            if sent:
                await self.post_run_update(reg, ev, f"🧩 replacement for {m.display_name}:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))

    # ---- opening and cancelling runs (commands, the web and plain-text ops share these)
    async def open_run_and_post(self, reg, rs, instance: str, start: datetime, by: str) -> rc.RaidEvent:
        """Open (or find) the run for `instance` at `start`, post its sheet in the signup channel when one is set
        (else the event stays unposted: channel_id None, `/raid sheet` can place it), and say so on the ops feed."""
        ev = rc.open_run(reg, rs, instance, start, by=by)
        channel = self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None
        if channel and not ev.message_id:
            await self.post_sheet(reg, rs, ev, channel)
        await self.ops.emit(reg.config, "info", f"{by} opened {ev.key} ({len(ev.signups)} pre-filled out from absences)" + ("" if ev.message_id else " — no signup channel: sheet not posted"))
        return ev

    async def cancel_run(self, reg, rs, ev, by: str, reason: str | None) -> str:
        """Cancel a run: state, sheet + officer cards re-rendered, open confirmations withdrawn, thread line, ops warn."""
        ev.state = "cancelled"
        ev.log.append(f"cancelled by {by}" + (f": {reason}" if reason else ""))
        rs.save(ev, "cancelled")
        waiting = rc.withdraw_confirmations(reg, ev, "cancelled") if ev.all_rosters else []
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"🛑 run cancelled by {by}" + (f": {reason}" if reason else "") + (f" · confirmations withdrawn for {len(waiting)}" if waiting else ""))
        await self.ops.emit(reg.config, "warn", f"{by} cancelled {ev.key}" + (f": {reason}" if reason else ""))
        return f"Cancelled {run_label(reg, ev)}" + (f": {reason}" if reason else "") + " — rostered members are not told automatically; say so in the channel."

    # ---- absences ripple into sheets
    async def after_absence_cleared(self, reg, m, a) -> list[str]:
        """The cleared absence's span: on every live open sheet the absence-sourced No thanks is dropped (they can
        answer again); on a locked run whose seat was handed back for it, say so — nobody is re-seated. Returns lines."""
        rs = self.raids.store(reg)
        lines = []
        for ev in rs.live():
            day = ev.start.astimezone(reg.tz).date().isoformat()
            if not (a.start <= day <= a.end) or m.absent_on(day):  # another absence still covers the day
                continue
            sg = ev.signups.get(str(m.discord_id))
            if not sg or sg.status != "out" or sg.source != "absence":
                continue
            label = run_label(reg, ev)
            if sheet_state(ev) == "open":
                del ev.signups[str(m.discord_id)]
                ev.log.append(f"{m.display_name}'s absence cleared: answer reset")
                rs.save(ev, f"{m.display_name} absence cleared")
                await self.refresh_sheet(reg, ev)
                lines.append(f"{label}: you can answer the sheet again")
            else:
                lines.append(f"{label}: the roster is locked and your seat was handed back when the absence was recorded — ask an officer if you want back in")
        return lines

    async def after_absence(self, reg, m, start: str, end: str, by: str) -> list[str]:
        """Mark every live sheet inside the absence: open → out; locked and rostered → seat freed + fill."""
        rs = self.raids.store(reg)
        touched = []
        for ev in rs.live():
            day = ev.start.astimezone(reg.tz).date().isoformat()
            if not (start <= day <= end):
                continue
            team = rc.run_team(reg, ev)
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
        """Every minute: open sheets on cadence, post health/nudges, lock, fill, expire confirmations, close."""
        await self.wait_until_ready()
        self._last_tick_error: str | None = None
        while not self.is_closed():
            try:
                await self.scheduler_tick()
                self._last_tick_error = None
            except Exception as e:  # noqa: BLE001
                text = f"scheduler tick failed: {type(e).__name__}: {e}"
                log.exception("scheduler tick failed")
                if text != self._last_tick_error:  # the same failure every minute is reported once
                    self._last_tick_error = text
                    cfg = next(iter(self.registries.by_discord.values())).config if self.registries.by_discord else None
                    if cfg:
                        try:
                            await self.ops.emit(cfg, "error", text, e)
                        except Exception:  # noqa: BLE001
                            log.warning("ops emit failed for the scheduler error")
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
        exhausted_posted: set[str] = self.__dict__.setdefault("_fill_exhausted_posted", set())  # run keys whose "nobody left to ask" line went out
        for reg in self.registries.by_discord.values():
            cfg = reg.config
            rs = self.raids.store(reg)
            channel = self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None
            await self.cleanup_ephemeral(reg, rs)
            now = reg.now_local()
            # open sheets on cadence: every slot occurrence within its raid's signup lead (needs the signup channel)
            if channel:
                for inst in reg.profile.raids:
                    rd = reg.raid_def(inst)
                    for _slot, start in rc.slot_starts(reg, inst, now, float(rd["signup_lead_hours"])):
                        key = f"{rc.run_key(inst, start)}-{start.date().isoformat()}"
                        if key in rs.events:
                            continue
                        await self.open_run_and_post(reg, rs, inst, start, by="scheduler")
            # every live run is processed even when its sheet channel can't be resolved: the steps that post to a
            # channel (refresh_sheet, post_run_update, cards) fail soft on their own
            for ev in rs.live():
                team = rc.run_team(reg, ev)
                officer_ch = self.officer_channel(reg, ev)
                soft, hard, confirm = run_times(reg, ev, team)
                state = sheet_state(ev)
                if state == "open" and not ev.health_posted and now >= soft:
                    await self.post_health(reg, rs, ev, officer_ch, nudge=True)
                    await self.ops.emit(cfg, "info", f"{ev.key}: health check posted, nudged {len(ev.nudged)}")
                if state == "locked" and not ev.all_rosters:  # a lock that never finished (old code path / crash): back to open, retry below
                    ev.state, ev.locked_at, ev.confirm_by = "open", None, None
                    rs.save(ev, "lock recovered")
                    state = "open"
                    await self.ops.emit(cfg, "warn", f"{ev.key}: locked without a roster — reopened, retrying the lock")
                if state == "open" and now >= hard:
                    tried = datetime.fromisoformat(ev.lock_tried_at) if ev.lock_tried_at else None
                    if ev.lock_error and tried and now - tried < timedelta(minutes=15):
                        continue  # failed recently; give the officers time to fix the board before trying again
                    line = await self.lock_run(reg, rs, ev)
                    if not ev.lock_error:
                        await self.ops.emit(cfg, "info", line)
                    continue
                if state != "locked":
                    continue  # the fill engine, confirmations and closing only apply to a locked roster
                gone = rc.expire_confirmations(reg, rs, ev)
                if gone:
                    for name in gone:
                        m = next((mm for mm in reg.members.values() if mm.display_name == name), None)
                        if m:
                            await self.dm_seat_released(reg, ev, m, "no-confirm")
                    await self.post_run_update(reg, ev, f"⌛ no confirmation from {', '.join(gone)} — seats freed")
                    await self.refresh_sheet(reg, ev)
                    await self.refresh_cards(reg, ev)
                    await self.ops.emit(cfg, "warn", f"{ev.key}: confirmations expired for {len(gone)}")
                expired, released = rc.expire_fill_asks(reg, rs, ev)
                if expired:
                    await self.post_run_update(reg, ev, f"⌛ no answer from {', '.join(a.display_name for a in expired)} — counts as no")
                    for a in released:
                        await self.withdraw_ask(reg, ev, a, "the other half of the question timed out")
                    await self.refresh_cards(reg, ev)
                if rc.team_setting(team, "autofill") and ev.fill_state in ("idle", "asking"):
                    sent, nd = await self.run_fill(reg, rs, ev, team)
                    if sent:
                        exhausted_posted.discard(ev.key)
                        await self.post_run_update(reg, ev, f"🧩 short {gaps_text(self.ico, nd)}:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
                    elif ev.fill_state == "exhausted" and ev.key not in exhausted_posted:  # just transitioned: say so once
                        exhausted_posted.add(ev.key)
                        await self.post_run_update(reg, ev, f"🧩 nobody left to ask — short {gaps_text(self.ico, nd)}")
                elif ev.fill_state != "exhausted":
                    exhausted_posted.discard(ev.key)
                if now >= rc.run_close_at(reg, ev):
                    ev.state = "done"
                    rs.save(ev, "done")
                    exhausted_posted.discard(ev.key)
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
        return [app_commands.Choice(name=f"{run_title(reg, e)} · {sheet_state(e)}"[:100], value=e.team) for e in live if current.lower() in e.team.lower() or current.lower() in raid_name(reg, e).lower()][:25]

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
                nxt = rc.slot_starts(reg, raid, now, 24 * rc.OPEN_HORIZON_DAYS)
                if not nxt:
                    await interaction.response.send_message(f"{raid} has no slots yet (or none before it opens). Set them: `/gm config raid raid:{raid} slots:'Tue 19:30'` — or pass a date/time.", ephemeral=True)
                    return
                start = nxt[0][1]
        except ValueError:
            await interaction.response.send_message("Time looks like `2026-12-10 19:30` (guild time).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rs = bot.raids.store(reg)
        ev = await bot.open_run_and_post(reg, rs, raid, start, by=interaction.user.display_name)
        if not ev.message_id:  # no signup channel configured: the sheet goes where the officer asked
            await bot.post_sheet(reg, rs, ev, interaction.channel)
        await interaction.followup.send(f"Opened {run_label(reg, ev)} in <#{ev.channel_id}>", ephemeral=True)

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
        if sheet_state(ev) != "open":
            await interaction.response.send_message(f"{run_label(reg, ev)} is {sheet_state(ev)} — the health check is for an open sheet; see its lock cards in the roster channel.", ephemeral=True)
            return
        await interaction.response.send_message(view=health_layout(reg, rs, ev, t, bot.ico), ephemeral=not is_officer(interaction, reg))

    @raid.command(name="lock", description="Officer: lock a sheet now — roster from the signups, confirmation DMs to everyone rostered")
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
        e = discord.Embed(title=f"⚔ {run_title(reg, ev)}", colour=TEAL, description=f"<t:{int(ev.start.timestamp())}:F>\nRoster ({len(ev.roster.selected)}): " + ", ".join(p.character or p.signup_name for p in ev.roster.selected)[:3800])
        e.set_footer(text="loot council: tick drops (or let the companion feed do it) → Distribute → chat to adjust → Confirm · /raid end for the summary")
        await interaction.response.send_message(embed=e)
        msg = await interaction.original_response()
        thread = await msg.create_thread(name=f"loot · {run_title(reg, ev)}"[:100])
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            line = await bot.set_answer(reg, rs, ev, m, status.value, character, by=interaction.user.display_name)
        except ValueError as e:
            line = f"❌ {e}"
        await interaction.followup.send(line, ephemeral=True)

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
        await interaction.response.defer(thinking=True)
        line = await bot.cancel_run(reg, rs, ev, by=interaction.user.display_name, reason=reason)
        await interaction.followup.send(line)

    @raid.command(name="list", description="Live runs")
    async def raid_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        rs = bot.raids.store(reg)
        live = rs.live()
        await interaction.response.send_message("\n".join(f"• {run_label(reg, e)} · {sheet_state(e)} · {len(e.seated()) if sheet_state(e) == 'locked' else len(e.by_status('in'))} {'rostered' if sheet_state(e) == 'locked' else 'joined'}" for e in live) or "No live runs.", ephemeral=True)

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
        await interaction.response.send_message(f"Noted: out for {run_label(reg, ev)} ({co.hours_before:.0f}h before{', after lock' if co.late else ''}). Thanks for saying so.", ephemeral=True)
        if sheet_state(ev) == "locked" and ev.seat_of(m.display_name):
            await bot.drop_seated(reg, rs, ev, t, m, "callout", note)
            return
        await bot.refresh_sheet(reg, ev)
        # the note is for the officers only: nothing in the public signup channel, one line in the run's updates thread
        await bot.post_run_update(reg, ev, f"⚑ {m.display_name} ({co.character}) called out, {co.hours_before:.0f}h before")
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
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            await interaction.response.send_message("Fill works after lock — the sheet is still open.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        nd = rc.needs(reg, ev, t)
        gaps = f"short {gaps_text(bot.ico, nd)}" if nd["headcount"] or nd["roles"] else "nothing short"
        busy = rc.conflicts(rs, ev)
        if preview:
            cands = await asyncio.to_thread(rc.fill_candidates, reg, rs, ev, t)
            lines = [f"{i + 1}. " + ask_line(reg, bot.ico, a) + (" — tied to " + next((b.display_name for b in cands if b.discord_id == a.pair), "?") if a.pair else "") for i, a in enumerate(cands[:15])]
            open_asks = [a for a in ev.fill_asks if a.open]
            await interaction.followup.send(f"{run_label(reg, ev)} · {gaps}" + (f" · double-booked: {', '.join(reg.members[u].display_name for u in busy if u in reg.members)}" if busy else "") + f"\nOutstanding asks: {', '.join(a.display_name for a in open_asks) or 'none'}\nWould ask next:\n" + ("\n".join(lines) or "nobody left"), ephemeral=True)
            return
        sent, nd = await bot.run_fill(reg, rs, ev, t, by=interaction.user.display_name)
        if sent:
            await bot.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, bot.ico, a) for a in sent))
        await interaction.followup.send(f"{run_label(reg, ev)} · {gaps}\n" + ("Asked: " + ", ".join(ask_line(reg, bot.ico, a) for a in sent) if sent else ("Nothing to fill." if not (nd["headcount"] or nd["roles"]) else "Nobody left to ask (or the asks outstanding already cover it).")), ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} ran fill for {ev.key}: asked {len(sent)}")

    tree.add_command(raid)
