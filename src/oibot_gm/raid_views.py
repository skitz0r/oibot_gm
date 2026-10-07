"""Pure rendering for the signup-driven cycle: the sheet and officer cards as Discord layout messages, the
confirmation / fill DMs, and the small text helpers (run names, stamps, gap lines) everything else formats with.
No I/O and no bot state: every function takes the registry, the event and `ico` (icon → app emoji) and returns text or
a `discord.ui.LayoutView`.

Import cycle: the layouts place the persistent buttons (`raid_buttons`), whose callbacks format their replies with the
helpers here. Both modules import each other at the *bottom* of the file — after every def — so either import order
works (the partially initialised module already has every name the other one binds)."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any, Protocol

import discord

from . import raidcycle as rc
from .constants import MARK, ROLES, TEAL
from .registry import Registry, RegistryError

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


class BotProto(Protocol):
    """What the raid mixins (`RaidMixin`, `RaidSchedulerMixin`) expect from the client they are mixed into — the
    attributes of `OibotGM` they touch. Documentation of the contract; the mixins' methods are not annotated with it
    (the audit's signature freeze), so keep this in step by hand when a mixin starts using something new."""

    ops: Any            # Ops: `emit(cfg, level, text[, exc])`
    raids: Any          # RaidContext: `store(reg)` → RaidStore
    registries: Any     # Guilds: `for_guild`, `only()`, `ordered()`, `by_key`, `for_interaction(interaction)` (never "the first guild")
    events: dict        # loot sessions (MockEvent) by thread id
    feed: Any           # FeedServer or None: companions + presence

    def ico(self, kind: str, key: str) -> str: ...
    def officiates(self, member: discord.Member, guild: discord.Guild) -> bool: ...
    async def cached_member(self, guild: discord.Guild, uid: int) -> discord.Member | None: ...
    def get_channel(self, id: int, /) -> Any: ...
    def get_guild(self, id: int, /) -> discord.Guild | None: ...
    async def fetch_user(self, id: int, /) -> discord.User: ...
    async def wait_until_ready(self) -> None: ...
    def is_closed(self) -> bool: ...
    # provided by RaidMixin itself, used by the buttons and the scheduler
    async def send_member_dm(self, reg: Registry, m: Any, text: str | None = None, view: Any = None, ev: Any = None) -> bool: ...
    async def is_officer_anywhere(self, reg: Registry, uid: int) -> bool: ...
    async def may_answer_for(self, interaction: discord.Interaction, reg: Registry, uid: int) -> bool: ...


# ---------------------------------------------------------------- rendering

def run_times(reg: Registry, ev: rc.RaidEvent, team: dict) -> tuple[datetime, datetime, datetime]:
    """(health/nudge, lock, confirmation deadline) for a run."""
    start = ev.start
    soft = start - timedelta(hours=float(rc.team_setting(team, "cutoff_soft_hours")))
    hard = start - timedelta(hours=float(rc.team_setting(team, "cutoff_hard_hours")))
    confirm = datetime.fromisoformat(ev.confirm_by) if ev.confirm_by else start - timedelta(hours=float(team.get("confirm_hours", reg.raid_def(ev.instance)["confirm_hours_before"])))
    return soft, hard, confirm


OUT_MARK = {"callout": " ⚑"}  # a No thanks that wasn't a manual press: called out after answering
AWAY_SHOWN = 15  # the Away row names this many, then "+N"


def split_away(reg: Registry, ev: rc.RaidEvent, outs: list) -> tuple[list, list]:
    """(away, no thanks): Away = a registered absence covers the run's day, whatever put them on No thanks; the rest
    pressed No thanks themselves (or called out). Someone away who pressed Join or Bench anyway is not Away."""
    day = ev.start.astimezone(reg.tz).date().isoformat()
    away = [sg for sg in outs if (m := reg.members.get(sg.discord_id)) and m.absent_on(day)]
    ids = {sg.discord_id for sg in away}
    return away, [sg for sg in outs if sg.discord_id not in ids]


def away_line(away: list) -> str:
    names = [sg.display_name for sg in away[:AWAY_SHOWN]]
    return " · ".join(names) + (f" · +{len(away) - AWAY_SHOWN}" if len(away) > AWAY_SHOWN else "")


EMBED_FIELD_MAX, EMBED_TOTAL_MAX = 1024, 5600  # Discord: 1024 per field value, 6000 per embed (kept under, with headroom)


def _column(lines: list[str]) -> str:
    """One embed column: one member per line, cut to the field limit (never mid-line)."""
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > EMBED_FIELD_MAX - 12:
            out.append(f"… +{len(lines) - len(out)}")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out) or "—"


def sheet_message(reg: Registry, ev: rc.RaidEvent, team: dict, ico) -> tuple[discord.Embed, discord.ui.View | None]:
    """The sheet as an embed + buttons. An embed because its inline fields are the only columns Discord has (layout
    components run as one long text block): up to three columns per row, a class per column while the sheet is open,
    a group per column once it is locked, one member per line, spec icon + character. Header: raid emblem thumbnail,
    live timestamps, headcount and role counts (icon + number). One explicit layout per state:
    open — class columns, Bench / No thanks rows, Join / Bench / No thanks buttons;
    locked — group columns per roster with confirmation marks, Not rostered / Bench / No thanks, Can't make it;
    done — "Finished" over the roster; cancelled — "Cancelled", nothing else. Edited in place on every answer."""
    state = sheet_state(ev)
    unix = int(ev.start.timestamp())
    ins, subs, outs = (ev.by_status(s) for s in ("in", "sub", "out"))
    rd = reg.raid_def(ev.instance)
    name = rd.get("name", ev.instance or "raid")
    size = int(team.get("size") or rd.get("size") or 20)
    test = bool(team.get("test"))
    tag = "🧪 " if test else ""
    e = discord.Embed(colour=0x8C97A8 if test else 0x98A3B5 if state in ("done", "cancelled") else TEAL)
    thumb = raid_thumb_url(reg, ev.instance)
    if thumb:
        e.set_thumbnail(url=thumb)

    def member(character: str, cls: str, spec: str, mark: str | None = None, icons: bool = True) -> str:
        return ((mark + " ") if mark else "") + ((ico("spec", f"{cls}:{spec}") + " ") if icons else "") + character

    def rows_field() -> None:
        away, declined = split_away(reg, ev, outs)
        if subs:
            e.add_field(name=f"Bench ({len(subs)})", value=" · ".join(sg.display_name for sg in subs)[:EMBED_FIELD_MAX], inline=False)
        if away:  # a registered absence that day: not a choice, so not counted as No thanks
            e.add_field(name=f"Away ({len(away)})", value=away_line(away)[:EMBED_FIELD_MAX], inline=False)
        if declined:
            e.add_field(name=f"No thanks ({len(declined)})", value=" · ".join(sg.display_name + OUT_MARK.get(sg.source, "") for sg in declined)[:EMBED_FIELD_MAX], inline=False)

    if state == "cancelled":
        e.title, e.description = f"{tag}Cancelled · {name}", f"<t:{unix}:F>"
        return e, None
    if state == "open":
        counts = {r: sum(1 for s in ins if s.role == r) for r in ROLES}
        want = max(1, int(team.get("rosters") or 1))  # a schedule that declares N rosters advertises N × the seats
        runs = max(want, len(ins) // size) if size else want
        _soft, hard, confirm = run_times(reg, ev, team)
        confirms = rc.run_confirms(reg, ev)
        e.title = f"{tag}{name}"
        e.description = (f"<t:{unix}:F> · <t:{unix}:R>\n**{len(ins)}** / {size * want}" + (f" · {runs} runs" if runs > 1 else "") + "\u2003" + "\u2003".join(f"{ico('role', r)} {n}" for r, n in counts.items())
                         + f"\n🔒 locks <t:{int(hard.timestamp())}:R>" + (f" · ✓ confirm by <t:{int(confirm.timestamp())}:f>" if confirms else ""))
        by_cls: dict[str, list] = {}
        for sg in ins:
            by_cls.setdefault(sg.cls, []).append(sg)
        icons = sum(len(ico("spec", f"{sg.cls}:{sg.spec}")) + len(sg.character) + 2 for sg in ins) < EMBED_TOTAL_MAX - 600
        for cls, ss in sorted(by_cls.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            e.add_field(name=f"{ico('class', cls) or cls} ({len(ss)})", value=_column([member(sg.character, sg.cls, sg.spec, icons=icons) for sg in ss]), inline=True)
        if not by_cls:
            e.add_field(name="\u200b", value="-# Nobody has joined yet.", inline=False)
        rows_field()
        adhoc = rc.run_kind(reg, ev) == "adhoc"  # first come, first rostered; Join is the commitment when confirmations are off
        e.set_footer(text=("test run · " if test else "") + ("First come, first rostered · " if adhoc else "") + "Join = I'm coming · Bench = call me if you need me")
        return e, sheet_view(ev.key)
    # locked | done: the roster(s) as the board shows them, a group per column
    rosters = ev.all_rosters
    conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, ev)} if rosters and rc.run_confirms(reg, ev) else {}
    marks = {n: MARK.get(a) for n, a in conf.items()} if state == "locked" else {}
    tally = f"✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
    if state == "done":
        e.title, e.description = f"{tag}Finished · {name}", f"<t:{unix}:F>" + (f"\n**{len(ev.seated())}** rostered" if rosters else "")
    else:
        _soft, _hard, confirm = run_times(reg, ev, team)
        confirms = rc.run_confirms(reg, ev)
        e.title = f"{tag}🔒 {name}"
        e.description = (f"<t:{unix}:F> · <t:{unix}:R>\n" + (f"**{len(ev.seated())}** rostered" + (f" in {len(rosters)} rosters" if len(rosters) > 1 else "") + (f"\u2003{tally}" if confirms else "") if rosters else "-# Building the roster…")
                         + (f"\n✓ confirm by <t:{int(confirm.timestamp())}:f>" if confirms else ""))
    icons = sum(len(ico("spec", f"{p.cls}:{p.spec}")) + len(p.character or p.signup_name) + 4 for p in ev.seated()) < EMBED_TOTAL_MAX - 600
    for i, r in enumerate(rosters):
        if len(rosters) > 1:
            e.add_field(name=f"Roster {i + 1}", value=_role_counts(ico, r.selected) or "\u200b", inline=False)
        by = {p.signup_name: p for p in r.selected}
        for gi, g in enumerate(r.groups):
            ps = [by[n] for n in g if n in by]
            if ps:
                e.add_field(name=f"Group {gi + 1}", value=_column([member(p.character or p.signup_name, p.cls, p.spec, marks.get(p.signup_name), icons) for p in ps]), inline=True)
    if rosters:
        joined = {sg.display_name for sg in ins}
        left_off = [p.signup_name for p in rosters[0].benched if p.signup_name in joined]  # joiners the solver left off
        if left_off:
            e.add_field(name=f"Not rostered ({len(left_off)})", value=" · ".join(left_off)[:EMBED_FIELD_MAX], inline=False)
    rows_field()
    if state != "locked":
        return e, None
    e.set_footer(text=("test run · " if test else "") + ("Rostered? answer your DM. Not rostered? nothing to do." if rc.run_confirms(reg, ev)
                                                       else "Rostered? you're in — press Can't make it if that changes. Not rostered? nothing to do."))
    v = discord.ui.View(timeout=None)
    v.add_item(SignupButton(ev.key, "cant"))
    return e, v


def raid_thumb_url(reg: Registry, rid: str | None) -> str | None:
    """The site's raid emblem for this guild (?g=: Discord fetches without cookies, and the URL — cached by browsers and
    Discord's media proxy — must differ per guild, whose size / lockout / name may differ). None off https."""
    web = os.environ.get("OIBOT_WEB_URL", "")
    return f"{web}/img/raid/{rid}.png?g={reg.key}" if web.startswith("https") and rid else None


def board_url(reg: Registry, ev: rc.RaidEvent) -> str | None:
    """The run on the site's Rosters page, in this guild (?g= selects it; two guilds can hold one run key)."""
    web = os.environ.get("OIBOT_WEB_URL", "")
    return f"{web}/app/rosters?g={reg.key}#run-{ev.key}" if web.startswith("http") else None


def _header(reg: Registry, ev: rc.RaidEvent, title: str, lines: list[str]):
    """Section with the raid emblem: title, run time with countdown, then `lines`."""
    ui = discord.ui
    unix = int(ev.start.timestamp())
    text = f"## {title}\n<t:{unix}:F> · <t:{unix}:R>" + ("\n" + "\n".join(lines) if lines else "")
    thumb = raid_thumb_url(reg, ev.instance)
    if thumb:
        return ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=thumb))
    return ui.TextDisplay(text)


def ask_line(reg: Registry, ico, ask) -> str:
    """One thread line per fill ask: who, what they'd play (spec icon + character), and what it covers."""
    c = reg.find(ask.character)
    what = (ico("spec", f"{c[1].cls}:{ask.spec}") + " " if c else "") + f"**{ask.character}**"
    when = " (before lock)" if ask.phase == "prelock" else ""
    if ask.kind in ("offspec", "flex"):
        return f"asked {ask.display_name} to swap to {ask.spec} on {what}{when}"
    if ask.kind == "alt":
        return f"asked {ask.display_name} to swap to {what}{when}"
    tail = f" ({ask.reason})" if ask.reason.startswith("covering") else ""
    return f"asked {ask.display_name} to come as {what}{tail}{when}"


KIND_WORD = {"sub": "bench", "regular": "regular, no answer", "unanswered": "no answer", "other": "not a regular", "flex": "flex", "offspec": "offspec", "alt": "alt"}
SUGGEST_LINES = 3  # a card names this many seats; Suggest… lists them all with their alternates


def plan_ask_md(reg: Registry, ico, a) -> str:
    """One suggested ask on a card: the spec icon they'd play + the character, then how (flex / offspec / alt / bench …)."""
    c = reg.find(a.character)
    return (ico("spec", f"{c[1].cls}:{a.spec}") + " " if c else "") + f"**{a.character}** · {KIND_WORD.get(a.kind, a.kind)}"


def still_short_md(ico, plan) -> str:
    sa = plan.short_after
    bits = ([f"{sa['headcount']} seat{'s' if sa['headcount'] != 1 else ''}"] if sa.get("headcount") else []) + [f"{ico('role', r)} {k}" for r, k in sa.get("roles", {}).items()]
    return ("   ".join(bits) + " → recruit") if bits else "nothing"


def suggested_block(reg: Registry, ico, plan) -> str:
    """The Suggested block (the health card before lock; the lock card when a role swap waits for an officer): what the
    planner would ask, cheapest first, each with the code-written reason and its alternates, then what stays short if
    everyone says yes. Nothing here is sent until an officer presses Ask. "" when nothing is short."""
    seats = [st for st in plan.seats if st.options]
    if not plan.seats:  # before lock the asks still out cover their seats: say who the card is waiting on
        return f"**Suggested** · asked, waiting on {', '.join(plan.waiting)}" if plan.waiting and plan.phase == "prelock" else ""
    head = "**Suggested**"
    if plan.phase == "prelock":
        head += " · expected " + "   ".join(f"{ico('role', r)} {plan.have.get(r, 0)}/{plan.need[r]}" for r in ("tank", "healer") if plan.need.get(r))
        if plan.expected:
            head += f" (joined + {plan.expected} regular{'s' if plan.expected != 1 else ''} yet to answer)"
    if plan.full and any(a.swap for st in seats for a in st.options[0]):
        head += " · every seat taken: swaps first"
    if plan.waiting and plan.phase == "prelock":
        head += f" · waiting on {', '.join(plan.waiting)}"
    lines = [head]
    for st in seats[:SUGGEST_LINES]:
        main = st.options[0]
        # the spec icon alone says role + spec on a member line (the iconography rule: role icons are for counts only)
        lines.append(" + ".join(plan_ask_md(reg, ico, a) for a in main))
        why = (main[0].why or "").split(" · ", 1)[-1] if st.role else (main[0].why or "")
        alts = " · ".join(" + ".join(a.display_name for a in opt) for opt in st.options[1:])
        lines.append("-# " + why + (f" · alternates: {alts}" if alts else ""))
    if len(seats) > SUGGEST_LINES:
        lines.append(f"-# +{len(seats) - SUGGEST_LINES} more — Ask sends only the {SUGGEST_LINES} above; Suggest… lists them all")
    lines.append(f"Still short if all say yes: {still_short_md(ico, plan)}")
    return "\n".join(lines)


def suggest_row(ev: rc.RaidEvent, plan):
    """Ask (sends what the card shows — the first SUGGEST_LINES seats, after lock within the cap; refused and refreshed
    if the plan changed since: the rev rides in its id) and Suggest… (every seat with its alternates, pick and send)."""
    try:
        n = len(rc.send_batch(plan, limit=SUGGEST_LINES)[0])
    except RegistryError:  # after lock with the cap full: nothing can go out now
        n = 0
    return discord.ui.ActionRow(RunButton(ev.key, "ask", rev=plan.rev, label=f"Ask these {n}" if n > 1 else "Ask this one" if n else "Ask", disabled=not n),
                                RunButton(ev.key, "suggest"))


def _role_counts(ico, players) -> str:
    counts = {r: sum(1 for p in players if p.role == r) for r in ROLES}
    return "   ".join(f"{ico('role', r)} {n}" for r, n in counts.items())


def gaps_text(ico, nd: dict) -> str:
    """What a run is short, from rc.needs(): seats as a number, role shortfalls as role icon + number (never "2 tank")."""
    bits = [f"{nd['headcount']} seat{'s' if nd['headcount'] != 1 else ''}"] if nd["headcount"] else []
    bits += [f"{ico('role', r)} {n}" for r, n in nd["roles"].items()]
    return "   ".join(bits) if bits else "nothing"


LEVEL_DOT = {"green": "🟢", "amber": "🟠", "red": "🔴"}


def coverage_line(ico, cov: dict) -> str:
    """One recurring series' regulars coverage (Registry.series_coverage) for a Discord card: the level dot (green:
    regular mains cover the minimums with a spare; amber: no spare, or only with offspecs or alts; red: can't), role icon + count over the
    minimum where there is one, ⚠ when short (the iconography rule: never "2 tank"); then regulars / seats."""
    if not cov["regulars"]:
        return f"no regulars yet · every main is expected · {cov['seats']} seats"
    bits = [f"{ico('role', r)} {n}" + (f"/{cov['need'][r]}" if cov["need"].get(r) else "") + (" ⚠" if r in cov["short"] else "")
            for r, n in cov["counts"].items() if n or cov["need"].get(r)]
    dot = LEVEL_DOT.get(cov.get("level") or "", "")
    return (dot + " " if dot else "") + "   ".join(bits) + f" · {len(cov['regulars'])}/{cov['seats']}" + (f" · recruit: {', '.join(cov['recruit'][:3])}" if cov.get("level") == "red" and cov.get("recruit") else "")


def forecast_lines(ico, cov: dict, limit: int = 4) -> list[str]:
    """The series' 4-week forecast (rc.series_forecast) for a Discord card: one line per upcoming start that the
    regulars minus recorded absences leave short — day, role icon + number, who is away. Empty when all are covered."""
    out = []
    for f in (cov.get("forecast") or [])[:limit]:
        if f["short"]:
            out.append(f"-# {f['day']}: short " + "   ".join(f"{ico('role', r)} {k}" for r, k in f["short"].items()) + (f" ({', '.join(f['away'][:4])} away)" if f["away"] else ""))
    return out


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


def run_actions_row(reg: Registry, ev: rc.RaidEvent, locked: bool):
    ui = discord.ui
    items = []
    url = board_url(reg, ev)
    if url:
        items.append(ui.Button(label="Open the board", style=discord.ButtonStyle.link, url=url))
    # one order in every state — Open · Lock · Fill · Cancel — so nothing moves when the run locks; what does not
    # apply is disabled in place (fill only runs on a locked roster)
    items.append(RunButton(ev.key, "lock", disabled=locked, label="Locked" if locked else None))
    items.append(RunButton(ev.key, "fill", disabled=not locked))
    items.append(RunButton(ev.key, "cancel"))
    return ui.ActionRow(*items)


def health_layout(reg: Registry, rs, ev: rc.RaidEvent, team: dict, ico, officer: bool = True) -> discord.ui.LayoutView:
    """The health check as a card: joined vs size, role have/need with the short role marked, who could cover,
    nobody-brings icons, no-answer count, double-booked; officer buttons. `officer=False` is the card a member sees
    (/raid health is open to members): the counts, what is short and the missing buffs only — never the officer-facing
    parts (owner decision D1 and Phase 2): who could cover, the regulars who haven't answered, who is double-booked,
    the Suggested block and the officer buttons."""
    ui = discord.ui
    h = rc.health_data(reg, ev, team)  # role have/need = rc.gaps, the same numbers needs() and the fill engine read
    n, seats, _t, subs = h["headcount"]  # seats = what the run advertises (size × the rosters its schedule expects)
    per, want = rc.run_size(reg, ev), h.get("rosters", 1)
    players = rc.players_for(reg, ev)
    runs = max(want, rc.how_many_rosters(reg, players, per, reg.role_bounds(ev.instance, per), want=want))
    roles = []
    for r in h["roles"]:
        have, need = r["have"], r["need"]
        roles.append(f"{ico('role', r['role'])} " + (f"**{have}**/{need}" if need and have < need else f"{have}" + (f"/{need}" if need else "")))
    head = _header(reg, ev, f"Roster health · {reg.raid_def(ev.instance).get('name', ev.instance)}", [f"**{n}** / {seats}" + (f" · {runs} runs" if runs > 1 else "") + (f" · {subs} bench" if subs else "") + "   " + "   ".join(roles)])
    body = []
    reason = rc.split_reason(reg, players, per, reg.role_bounds(ev.instance, per))  # per run, as rc.run_split_reason
    if reason:  # the headcount allows more runs than the tanks/healers do (or not even one meets its minimums)
        body.append(rc.split_reason_text(reason, lambda role, k: f"{ico('role', role)} {k}"))
    short = [r for r in h["roles"] if r["need"] and r["have"] < r["need"]]
    if short:
        body.append("**Short** " + "   ".join(f"{ico('role', r['role'])} {r['need'] - r['have']}" for r in short))
        hints = [r["hint"] for r in short if r["hint"]] if officer else []  # names of who could cover: officers only
        if hints:
            body.append("-# " + " · ".join(hints))  # `-#` only renders at the start of a line
    missing = [b for b in h["buffs"] if not b["providers"]]
    if missing:
        body.append("⛔ " + " ".join(ico("buff", b["id"]) for b in missing if ico("buff", b["id"])))
    waiting = rc.not_answered(reg, ev) if officer else []  # the regulars who haven't answered (every main while the series has none); ad hoc: nobody
    if waiting:
        whose = {"regulars": "regulars", "everyone": "no regulars set: everyone", "test": "test bench"}.get(h.get("expected_from"), "")
        lines = [f"**No answer** {len(waiting)}" + (f" · {whose}" if whose else "") + (" · nudged" if ev.nudged else "")]
        lines += [f"{ico('spec', f'{c.cls}:{c.spec}')} {c.label}" for _m, c in waiting[:NO_ANSWER_LINES]]  # one member per line
        if len(waiting) > NO_ANSWER_LINES:
            lines.append(f"-# +{len(waiting) - NO_ANSWER_LINES} more on the board")
        body.append("\n".join(lines))
    busy = rc.conflicts(rs, ev) if rs is not None else {}
    double = [reg.members[u].display_name for u in busy if u in reg.members and str(u) in ev.signups and ev.signups[str(u)].status == "in"] if officer else []
    if double:
        body.append("**Double-booked** " + ", ".join(double))
    _soft, hard, confirm = run_times(reg, ev, team)
    parts = [head, ui.Separator()] + ([ui.TextDisplay("\n".join(body)), ui.Separator()] if body else [])
    plan = rc.role_plan(reg, rs, ev) if officer else None  # nothing is sent until an officer presses Ask
    sug = suggested_block(reg, ico, plan) if plan else ""
    if sug:
        parts += [ui.TextDisplay(sug), suggest_row(ev, plan), ui.Separator()]
    parts.append(ui.TextDisplay(f"🔒 locks <t:{int(hard.timestamp())}:R>" + (f" · ✓ confirm by <t:{int(confirm.timestamp())}:f>" if rc.run_confirms(reg, ev) else " · first come, no confirmations")))
    if officer:
        parts.append(run_actions_row(reg, ev, locked=False))
    worst = "red" if short or (plan and plan.short["roles"]) else "amber" if missing or waiting else "green"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour={"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]))
    return view


NO_ANSWER_LINES = 12  # the health card names this many regulars who haven't answered, then "+N"
LEFTOVER_LINES = 15  # a card's text is capped (4000 chars across its displays); the board lists the rest


def health_locked_layout(reg: Registry, ev: rc.RaidEvent, ico) -> discord.ui.LayoutView:
    """What the open sheet's health card becomes once the run locks: the header and a pointer to the lock card — no
    Suggested block, no Ask (its suggestions were for the open sheet; the lock card has the current ones)."""
    ui = discord.ui
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(_header(reg, ev, f"Roster health · {raid_name(reg, ev)}", ["🔒 Locked — the lock card below has the roster and what is still short."]),
                               accent_colour=0x98A3B5))
    return view


def leftover_block(reg: Registry, ev: rc.RaidEvent, ico) -> str:
    """Joiners the locked roster(s) left out, as the board shows them: "Leftovers (n)", one member per line with the
    spec icon, then what another run is short and whether leftovers + bench + pool could make one. "" when none."""
    left = rc.leftovers(reg, ev, ev.all_rosters)
    if not left:
        return ""
    lines = [f"**Leftovers ({len(left)})**"] + [f"{ico('spec', f'{p.cls}:{p.spec}')} {p.character or p.signup_name}" for p in left[:LEFTOVER_LINES]]
    if len(left) > LEFTOVER_LINES:
        lines.append(f"-# +{len(left) - LEFTOVER_LINES} more on the board")
    reason = rc.run_split_reason(reg, ev)
    if reason:
        lines.append("-# " + rc.split_reason_text(reason, lambda role, k: f"{ico('role', role)} {k}"))
    hint = rc.another_run_hint(reg, ev, ev.all_rosters, reason)
    if hint:
        lines.append("-# " + hint)
    return "\n".join(lines)


def lock_notes(reg: Registry, ev: rc.RaidEvent, ico, i: int) -> str:
    """What the lock decided that officers should see on the card: "Locked short" (the solver caps a minimum at what
    the sheet can supply — said out loud instead of locking quietly), the spec switches the solver made (one member
    per line, the spec icon they now play), and on a recurring run the regulars who joined but weren't rostered (who
    gets priority next time). Roster `i`'s switches; the other two on roster 1's card."""
    r = ev.all_rosters[i]
    lines = []
    if i == 0:
        short = rc.needs(reg, ev)["roles"]
        if short:  # "Locked short" when the lock itself came out short (lock_run logs it); "Short" when a seat freed since
            locked_short = any(ln.startswith("locked short") for ln in ev.log)
            lines.append(f"**{'Locked short' if locked_short else 'Short'}** " + "   ".join(f"{ico('role', role)} {k}" for role, k in short.items()))
    if r.spec_switches:
        by = {p.signup_name: p for p in r.selected}
        lines.append("**Playing an offspec**")
        lines += [f"{ico('spec', f'{p.cls}:{p.spec}')} {p.character or p.signup_name}" for n in r.spec_switches if (p := by.get(n))]
    if i == 0 and rc.run_kind(reg, ev) == "recurring" and not rc.is_test_run(reg, ev):
        seated = {rc.player_uid(ev, p) for p in ev.seated()}
        benched = [(m, c) for m, c in rc.run_regulars(reg, ev) if (sg := ev.signups.get(str(m.discord_id))) and sg.status in ("in", "sub") and m.discord_id not in seated]
        if benched:
            lines.append("**Regulars not rostered**")
            lines += [f"{ico('spec', f'{c.cls}:{c.spec}')} {c.label}" for _m, c in benched[:LEFTOVER_LINES]]
    return "\n".join(lines)


def lock_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, i: int, rs=None) -> discord.ui.LayoutView:
    """One locked roster as a card: groups with confirmation marks and auras, raid-wide row, bench, waiting-on, buttons."""
    ui = discord.ui
    r = ev.all_rosters[i]
    confirms = rc.run_confirms(reg, ev)
    rows = [c for c in rc.confirmations(reg, ev) if c["roster"] == i + 1] if confirms else []
    conf = {c["display_name"]: c["answer"] for c in rows}
    via_site = {c["display_name"] for c in rows if c["channel"] == "web"}  # DMs off: the ask waits on the Me page
    marks = {n: MARK.get(a) for n, a in conf.items()}
    tally = f"✅ {sum(1 for a in conf.values() if a == 'yes')} · ⏳ {sum(1 for a in conf.values() if a is None)} · ❌ {sum(1 for a in conf.values() if a in ('no', 'expired'))}"
    rd = reg.raid_def(ev.instance)
    head = _header(reg, ev, f"🔒 {rd.get('name', ev.instance)}" + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else ""),
                   [f"**{len(r.selected)}** rostered" + (f" · synergy {r.synergy_value}" if r.synergy_value else "") + "   " + _role_counts(ico, r.selected), tally if confirms else "-# first come — no confirmations: Join is the commitment"])
    summaries = _group_summaries(reg, r)
    parts = [head, ui.Separator()]
    for gi in range(len(r.groups)):
        if r.groups[gi]:
            parts.append(ui.TextDisplay(_group_block(reg, ico, r, gi, marks, summaries)))
    left = leftover_block(reg, ev, ico) if i == 0 else ""
    if left:
        parts.append(ui.TextDisplay(left))
    extra = lock_notes(reg, ev, ico, i)
    if extra:
        parts.append(ui.TextDisplay(extra))
    if i == 0 and ev.state == "locked" and rc.run_kind(reg, ev) == "recurring":
        plan = rc.role_plan(reg, rs, ev)
        if any(a.swap for a in plan.asks):  # D3: after lock a role swap waits for an officer's Ask
            parts += [ui.Separator(), ui.TextDisplay(suggested_block(reg, ico, plan)), suggest_row(ev, plan)]
    tail = ["-# " + _raidwide_line(reg, ico, r)]
    lefts = {p.signup_name for p in rc.leftovers(reg, ev, ev.all_rosters)} if i == 0 else set()
    bench = [p for p in r.benched if p.signup_name not in lefts] if i == 0 else []
    if bench:
        tail.append("**Bench** " + " · ".join(p.signup_name for p in bench))
    pending = [n + (" (site)" if n in via_site else "") for n, a in conf.items() if a is None]
    if pending:
        tail.append("**Waiting on** " + ", ".join(pending[:15]))
    _soft, _hard, confirm = run_times(reg, ev, team)
    if confirms:
        tail.append(f"-# ✓ confirm by <t:{int(confirm.timestamp())}:f> · unanswered then counts as out")
    parts += [ui.Separator(), ui.TextDisplay("\n".join(tail)), run_actions_row(reg, ev, locked=True)]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=TEAL))
    return view


def confirm_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, sg, i: int, gi: int, uid: int) -> discord.ui.LayoutView:
    """Confirmation DM: your seat, the group you'd be in (the board's layout), the deadline, Confirm / Can't make it."""
    ui = discord.ui
    r = ev.all_rosters[i]
    rd = reg.raid_def(ev.instance)
    seat = next((p for p in r.selected if p.signup_name == sg.display_name), None)
    spec = seat.spec if seat else sg.spec  # the spec the roster has them on (the solver may have switched an offspec)
    lines = [f"{ico('spec', f'{sg.cls}:{spec}')} **{sg.character}**" + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else "") + f" · Group {gi + 1}"]
    found = reg.find(sg.character)
    own = found[1] if found else None  # the character as registered: its main spec and offspec
    if own is not None and spec != own.spec:  # an offspec the solver switched them to, or one they agreed to before lock
        lines.append(f"You're rostered as **{spec}**" + (", your offspec" if spec == own.offspec else "")
                     + (" — as you agreed" if sg.spec == spec else f" — you signed up as {sg.spec}") + ".")
    head = _header(reg, ev, f"You're rostered · {rd.get('name', ev.instance)}", lines)
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
    """Fill DM: what opened, the seat offered (character + spec), the group they'd join, Confirm / Can't make it."""
    ui = discord.ui
    rd = reg.raid_def(ev.instance)
    c = reg.find(ask.character)
    cls = c[1].cls if c else ""
    if ask.phase == "prelock" and ev.state == "open":
        return prelock_layout(reg, ev, team, ico, ask, cls)
    # only what the bot knows: "other" = not one of the run's regulars and no answer on the sheet — never a claim that
    # they're free that night (they never said so)
    what = {"sub": "you're on the bench", "regular": "you haven't answered the sheet", "unanswered": "you haven't answered the sheet",
            "other": "you haven't answered the sheet (you're not one of its regulars)", "offspec": f"you could play **{ask.spec}** instead of your main spec",
            "flex": f"you said you can play {ask.role}", "alt": "you could bring your alt"}.get(ask.kind, "")
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
    first = ("-# Confirm counts once you both say yes." if partner else "-# Confirm makes the swap at once." if ask.swap else "-# Confirm puts you straight in the seat.")
    parts += [ui.Separator(), ui.TextDisplay(first + " A no asks the next person." + deadline), ui.ActionRow(FillButton(ev.key, ask.discord_id, "yes"), FillButton(ev.key, ask.discord_id, "no"))]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=0xE0A448))
    return view


def prelock_layout(reg: Registry, ev: rc.RaidEvent, team: dict, ico, ask, cls: str) -> discord.ui.LayoutView:
    """A pre-lock ask (an officer pressed Ask on a suggestion): the run is short a role, could you come as X? A yes
    puts them down as Join with that spec / character; the roster is still built at lock, when an unanswered ask
    closes. Yes, as <spec> / No."""
    ui = discord.ui
    rd = reg.raid_def(ev.instance)
    icon = ico("spec", f"{cls}:{ask.spec}") + " " if cls else ""
    partner = rc.partner_of(ev, ask)
    if ask.reason.startswith("covering") and partner:
        lines = [f"{run_label(reg, ev)}: {partner.display_name} could switch to {partner.role} if someone covers their {ask.role} seat.",
                 f"Could you come as {icon}**{ask.character}** ({ask.spec})? Both of you say yes, or neither changes."]
    else:
        role = (ask.role if ask.role not in ("melee", "ranged") else "DPS")
        lines = [f"{run_label(reg, ev)} is short a {role}."]
        if ask.kind in ("offspec", "flex"):
            lines.append(f"Could you come as {icon}**{ask.spec}** on **{ask.character}**?")
        elif ask.kind == "alt":
            lines.append(f"Could you bring {icon}**{ask.character}** ({ask.spec})?")
        else:
            lines.append(f"Could you come as {icon}**{ask.character}** ({ask.spec})?")
        if partner:
            lines.append(f"{partner.display_name} is being asked to cover your {ask.vacates} seat; it only happens if you both say yes.")
    _soft, hard, _c = run_times(reg, ev, team)
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(_header(reg, ev, f"Could you help? · {rd.get('name', ev.instance)}", lines), ui.Separator(),
                               ui.TextDisplay(f"-# Yes puts you down as Join with that; the roster is still built at lock (<t:{int(hard.timestamp())}:R>). No answer by then counts as no."),
                               ui.ActionRow(FillButton(ev.key, ask.discord_id, "yes", label=f"Yes, as {ask.spec}"[:80]), FillButton(ev.key, ask.discord_id, "no", label="No")),
                               accent_colour=0xE0A448))
    return view


def closed_layout(reg: Registry, ev: rc.RaidEvent, ico) -> discord.ui.LayoutView:
    """What a run's officer cards become once it is cancelled or finished: the header and the last log line, no buttons."""
    ui = discord.ui
    title = ("Cancelled" if ev.state == "cancelled" else "Finished") + f" · {raid_name(reg, ev)}"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(_header(reg, ev, title, [f"-# {ev.log[-1]}"] if ev.log else []), accent_colour=0x98A3B5))
    return view


# the buttons the layouts place; imported last so `raid_buttons` (which imports this module's helpers at its own
# bottom) always finds every name above bound, whichever module is imported first
from .raid_buttons import FillButton, PlaceButton, RunButton, SignupButton, sheet_view  # noqa: E402
