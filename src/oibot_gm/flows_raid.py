"""The /raid wizards: `/raid open` (a shortlist of the raid's next runs, or a day · hour · minutes picker — never a typed
time), `/raid set` (the character is picked, not spelled), and the run-scoped commands (sheet, health, lock, loot,
cancel, out, fill), which keep working with no arguments: the run picker appears only when more than one run is live.
`/raid out` and `/raid cancel` take their note / reason in a small modal instead of a one-line slash option.

Every body here is the old slash-command body (raid_commands.py) with the same bot calls, replies and officer checks;
only the way it answers changed, so it works from the slash command and from a select on the wizard message alike."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

import discord

from . import raidcycle as rc
from . import wizard_opts as wo
from .constants import TEAL
from .raid_views import ask_line, gaps_text, health_layout, raid_name, run_label, run_title, sheet_message, sheet_state
from .registry import Registry
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, both_clocks, combine_when, flow, week_opts, when_fields, when_note

LIVE = ("open", "locked")
OTHER_STARTS = 22  # the shortlist's select: the other upcoming runs (the first one is the primary button)


# ---------------------------------------------------------------- shared

def officer_guard(reg: Registry) -> Callable[[discord.Interaction], Awaitable[str | None]]:
    async def still_officer(i: discord.Interaction) -> str | None:
        from .discord_registry import is_officer

        return None if is_officer(i, reg) else "Officers only."
    return still_officer


async def officer_only(interaction: discord.Interaction, reg: Registry) -> bool:
    """The entry check every officer flow makes before it shows anything (the guard re-checks at every press)."""
    from .discord_registry import is_officer

    if is_officer(interaction, reg):
        return True
    await interaction.response.send_message("Officers only.", ephemeral=True)
    return False


def run_team(reg: Registry, ev: rc.RaidEvent) -> dict:
    """What the old `current_event` handed the bodies: the run's roster dict, or a bare stand-in."""
    return reg.config.roster(ev.team) or {"key": ev.team, "size": 20}


def is_command(i: discord.Interaction) -> bool:
    """The slash command itself, not yet answered — the bodies that used to answer with a public or layout message
    keep doing exactly that there."""
    return i.type == discord.InteractionType.application_command and not i.response.is_done()


def when_relative(reg: Registry, dt: datetime) -> str:
    days = (dt.astimezone(reg.tz).date() - reg.now_local().date()).days
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


Body = Callable[[discord.Interaction, Wizard, rc.RaidStore, "rc.RaidEvent | None"], Awaitable[None]]


async def with_run(interaction: discord.Interaction, reg: Registry, *, title: str, then: Body, states: tuple[str, ...] = LIVE,
                   officer: bool = False, prefer: Callable[[rc.RaidEvent], bool] | None = None) -> None:
    """Resolve the run a run-scoped command is about, then call `then(interaction, wizard, rs, ev)`.

    One candidate (live, in `states`, narrowed by `prefer` when that leaves any) → straight to the body: the command
    stays zero-argument on a one-run night. Several → one select of them. None in `states` → the body gets the first
    live run (or None) so it says what the old command said ("No open sheet to lock.", "Lock the roster first")."""
    rs = interaction.client.raids.store(reg)
    wiz = Wizard(reg, interaction.user.id, title=title, guard=officer_guard(reg) if officer else None)
    live = rs.live()
    cands = [ev for ev in live if sheet_state(ev) in states]
    if prefer:
        cands = [ev for ev in cands if prefer(ev)] or cands
    if len(cands) <= 1:
        await then(interaction, wiz, rs, cands[0] if cands else (live[0] if live else None))
        return
    teams = {ev.team for ev in cands}
    opts = [o for o in wo.live_runs(reg, rs, states=states) if o.value in teams]

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        await then(i, wiz, rs, rs.for_team(values[0]))  # re-read: the run may have closed since the list was drawn

    await wiz.show(interaction, "Which run?", wiz.select("Pick the run", opts, picked), wiz.button("Cancel", wiz.cancel))


# ---------------------------------------------------------------- /raid open

class OpenWizard(Wizard):
    """Raid → the shortlist (next run as one button, the other upcoming runs of every schedule in a select, a pickup
    template's own button, or 'Another day and time…') → confirm with both clocks → open and post."""

    def __init__(self, reg: Registry, rs, owner_id: int):
        super().__init__(reg, owner_id, title="Open a run", guard=officer_guard(reg))
        self.rs = rs
        self.draft = {"raid": None, "start": None, "note": "", "day0": reg.now_local().date(), "schedule": None}

    @property
    def rid(self) -> str:
        return self.draft["raid"]

    def name(self) -> str:
        return self.reg.raid_def(self.rid).get("name", self.rid)

    def upcoming(self) -> list[tuple[dict, datetime]]:
        """(schedule, start) for every active schedule, soonest first."""
        return rc.upcoming(self.reg, self.rid, self.reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)

    def starts(self) -> list[datetime]:
        return [t for _s, t in self.upcoming()]

    def templates(self) -> list[dict]:
        return [s for s in self.reg.schedules(self.rid) if s["kind"] == "pickup" and s.get("active", True)]

    def many(self) -> bool:
        """More than one schedule to tell apart: runs are then labelled with their schedule's name."""
        return len([s for s in self.reg.schedules(self.rid) if s["kind"] != "pickup"]) > 1

    def sched_name(self, sid: str | None) -> str:
        s = next((x for x in self.reg.schedules(self.rid) if x["id"] == (sid or "default")), None)
        return (s or {}).get("name") or ""

    def template_buttons(self) -> list[discord.ui.Item]:
        return [self.button(f"Open {s['name']} — pick a time", self._template(s["id"])) for s in self.templates()[:3]]

    def _template(self, sid: str):
        async def go(interaction: discord.Interaction) -> None:
            self.draft["schedule"] = sid
            await self.open_form(interaction, self.when_form())
        return go

    async def start(self, interaction: discord.Interaction) -> None:
        opts = wo.raids(self.reg)
        if len(opts) == 1:
            self.draft["raid"] = opts[0].value
            await self.shortlist(interaction)
            return
        await self.show(interaction, "Which raid?", self.select("Pick the raid", opts, self.raid_picked), self.button("Cancel", self.cancel))

    async def raid_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["raid"] = values[0]
        await self.shortlist(interaction)

    # ---- layer 1: the shortlist
    async def shortlist(self, interaction: discord.Interaction) -> None:
        ups = self.upcoming()
        self.draft["day0"] = self.reg.now_local().date()
        self.draft["schedule"] = None
        many = self.many()
        if not ups:
            await self.show(interaction, f"**{self.name()}** has no upcoming run to open: {self.no_slot_reason()}\n"
                            "-# Set its run times, or open a one-off run at a day and time you pick.",
                            self.button("Set this raid's run times →", self.to_run_times, discord.ButtonStyle.primary),
                            *self.template_buttons(),
                            self.button("Another day and time…", self.open_when),
                            self.button("Cancel", self.cancel))
            return
        items: list[discord.ui.Item] = []
        others = ups[1:1 + OTHER_STARTS]
        if others:  # the value names the schedule only when it isn't the raid's own run times (old values stay ISO)
            items.append(self.select("Or another upcoming run", [Opt(t.isoformat() + ("" if s["id"] == "default" else f"|{s['id']}"), self.reg.local12(t),
                                                                     (f"{s['name']} · " if many else "") + when_relative(self.reg, t)) for s, t in others], self.start_picked))
        s0, t0 = ups[0]
        items += [self.button(f"Open the next slot — {self.reg.local12(t0)}" + (f" · {s0['name']}" if many else ""), self.next_picked, discord.ButtonStyle.primary),
                  *self.template_buttons(),
                  self.button("Another day and time…", self.open_when),
                  self.button("Cancel", self.cancel)]
        await self.show(interaction, f"**{self.name()}** — which run?", *items)

    def no_slot_reason(self) -> str:
        if not [s for s in self.reg.schedules(self.rid) if s["kind"] != "pickup" and s.get("active", True)]:
            return "it has no run times yet." + (" (its pickup templates open when you pick a time)" if self.templates() else "")
        fo = self.reg.first_open(self.rid)
        if fo is not None:
            return f"every run time in the next {rc.OPEN_HORIZON_DAYS} days falls before its first lockout opens ({self.reg.local12(fo)})."
        return f"none of its run times falls in the next {rc.OPEN_HORIZON_DAYS} days."

    async def to_run_times(self, interaction: discord.Interaction) -> None:
        from .wizard import FLOWS

        nxt = FLOWS.get("raid_config")
        if nxt is None:
            await self.finish(interaction, "Run times are set on the Raids page.")
            return
        self._closed = True
        self.stop()
        await nxt(interaction, self.reg, raid=self.rid, section="times")

    async def next_picked(self, interaction: discord.Interaction) -> None:
        ups = self.upcoming()
        if not ups:
            await self.shortlist(interaction)
            return
        self.draft["start"], self.draft["note"], self.draft["schedule"] = ups[0][1], "", ups[0][0]["id"]
        await self.confirm(interaction)

    async def start_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        iso, _, sid = values[0].partition("|")
        self.draft["start"], self.draft["note"], self.draft["schedule"] = datetime.fromisoformat(iso), "", sid or None
        await self.confirm(interaction)

    # ---- layer 2: day · hour · minutes
    def when_form(self) -> Form:
        starts = self.starts()
        usual = starts[0] if starts else None
        tpl = self.draft.get("schedule")
        title = f"Open {self.sched_name(tpl) or self.name()}" if tpl else f"Open {self.name()}"
        return Form(title, when_fields(self.reg, day0=self.draft["day0"], default=usual, day_note=self._day_note), self.when_submitted)

    def _day_note(self, day: date) -> str | None:
        hits = [t for t in self.starts() if t.astimezone(self.reg.tz).date() == day]
        return f"{self.name()} run time {self.reg.local12(hits[0], '%I:%M %p')}" if hits else None

    async def open_when(self, interaction: discord.Interaction) -> None:
        self.draft["schedule"] = None  # a one-off at a picked time: the raid's own cadence
        await self.open_form(interaction, self.when_form())

    async def when_submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        if v.get("day") == "later":
            await self.show(interaction, "Which week?",
                            self.select("Pick the week", week_opts(self.reg, self.draft["day0"] + timedelta(days=MAX_OPTIONS - 1)), self.week_picked),
                            self.button("Cancel", self.cancel))
            return
        start, note = combine_when(self.reg, v)
        if start <= self.reg.now_local():
            await self.refuse(interaction, f"{self.reg.local12(start)} has already passed — pick a later time.", self.open_when, "Pick again")
            return
        self.draft["start"], self.draft["note"] = start, note
        await self.confirm(interaction)

    async def week_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["day0"] = date.fromisoformat(values[0])
        await self.open_form(interaction, self.when_form())

    # ---- confirm and save
    def existing(self, start: datetime) -> rc.RaidEvent | None:
        """The run open_run would hand back for this start (same roster key and day), if it is still live."""
        ev = self.rs.events.get(rc.event_key(self.rid, start, self.draft.get("schedule")))
        return ev if ev and ev.state not in ("done", "cancelled") else None

    async def confirm(self, interaction: discord.Interaction) -> None:
        start = self.draft["start"]
        sid = self.draft.get("schedule")
        label = f"**{self.name()}**" + (f" · {self.sched_name(sid)}" if sid and (sid != "default" or self.many()) else "")
        lines = [f"Open {label} at {both_clocks(self.reg, start)}?"]
        if sid:
            sd = self.reg.schedule_def(self.rid, sid)
            n = int(sd["schedule"].get("rosters") or 1)
            if n > 1:
                lines.append(f"-# {n} rosters of {sd.get('size', '?')}: the sheet advertises {n * int(sd.get('size') or 0)} seats.")
        if self.draft["note"]:
            lines.append(when_note(self.draft["note"], start))
        ev = self.existing(start)
        if ev:
            lines.append(f"-# A run for that time already exists ({sheet_state(ev)}); Open points you at it instead of making a second.")
        fo = self.reg.first_open(self.rid)
        if fo is not None and start < fo:
            lines.append(f"-# This is before the raid's first lockout opens ({self.reg.local12(fo)}).")
        if not self.reg.config.signup_channel_id:
            lines.append("-# No signup channel is set, so the sheet will be posted in this channel.")
        await self.show(interaction, "\n".join(lines),
                        self.button("Open", self.save, discord.ButtonStyle.success),
                        self.button("Change", self.shortlist),
                        self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        start = self.draft["start"]
        if start <= self.reg.now_local():
            await self.refuse(interaction, f"{self.reg.local12(start)} has passed while this was open — pick another time.", self.shortlist, "Pick again")
            return
        await self.working(interaction, "Opening…")
        bot, rs = interaction.client, self.rs
        ev = await bot.open_run_and_post(self.reg, rs, self.rid, start, by=interaction.user.display_name, **rc.schedule_kw(self.draft.get("schedule")))
        extra = ""
        if not ev.message_id:  # no signup channel configured: the sheet goes where the officer asked
            if interaction.channel is not None:
                await bot.post_sheet(self.reg, rs, ev, interaction.channel)
                extra = "\n-# No signup channel is set, so the sheet was posted in this channel."
            else:
                extra = "\n-# No signup channel is set and this channel can't take the sheet — run /raid sheet where it belongs."
        where = f" in <#{ev.channel_id}>" if ev.channel_id else ""
        await self.finish(interaction, f"✅ Opened {run_label(self.reg, ev)}{where}{extra}")


@flow("raid_open")
async def open_raid_open(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return
    await OpenWizard(reg, interaction.client.raids.store(reg), interaction.user.id).start(interaction)


# ---------------------------------------------------------------- /raid set

class SetWizard(Wizard):
    """Run (only if several) → the member's character (only if several) → confirm → bot.set_answer."""

    def __init__(self, reg: Registry, rs, owner_id: int, member_id: int, status: str):
        super().__init__(reg, owner_id, title="Set an answer", guard=officer_guard(reg))
        self.rs = rs
        self.draft = {"member": member_id, "status": status, "team": None, "character": None}

    def member(self):
        return self.reg.members.get(self.draft["member"])

    async def start(self, interaction: discord.Interaction) -> None:
        m = self.member()
        live = [ev for ev in self.rs.live() if sheet_state(ev) in LIVE]
        if not live or not m:
            await self.finish(interaction, "No live sheet, or that member isn't registered.")
            return
        self.title = f"Set an answer · {m.display_name}"
        if len(live) == 1:
            self.draft["team"] = live[0].team
            await self.character_step(interaction)
            return
        await self.show(interaction, "Which run?", self.select("Pick the run", wo.live_runs(self.reg, self.rs), self.run_picked), self.button("Cancel", self.cancel))

    async def run_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["team"] = values[0]
        await self.character_step(interaction)

    async def character_step(self, interaction: discord.Interaction) -> None:
        m = self.member()
        ev = self.rs.for_team(self.draft["team"])
        s = ev.signups.get(str(m.discord_id)) if ev and m else None
        opts = wo.characters_of(self.reg, m, getattr(interaction.client, "ico", None), current=s.character if s else None)
        if len(opts) <= 1:
            self.draft["character"] = opts[0].value if opts else None
            await self.confirm(interaction)
            return
        await self.show(interaction, f"Which of **{m.display_name}**'s characters?", self.select("Pick the character", opts, self.character_picked), self.button("Cancel", self.cancel))

    async def character_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["character"] = values[0]
        await self.confirm(interaction)

    def char_label(self) -> str:
        m, want = self.member(), (self.draft["character"] or "")
        c = next((c for c in m.active() if want in (c.name, c.label)), None) if m else None
        return c.label if c else (m.main.label if m and m.main else want or "their main")

    async def confirm(self, interaction: discord.Interaction) -> None:
        m, st = self.member(), self.draft["status"]
        ev = self.rs.for_team(self.draft["team"])
        if not ev or not m:
            await self.finish(interaction, "That run closed, or the member isn't registered any more — nothing was changed.")
            return
        lines = [f"Set **{m.display_name}** ({self.char_label()}) to **{rc.LABELS[st]}** on {run_label(self.reg, ev)}?"]
        if sheet_state(ev) == "locked":
            seated = ev.seat_of(m.display_name)
            if st == "in":
                lines.append("-# They keep their seat." if seated else "-# The roster is locked: they get a seat if one is free, and a DM asking them to confirm.")
            elif st == "out":
                lines.append("-# Their seat is freed, they're told, and the bench is asked." if seated else "-# They weren't rostered; only the sheet changes.")
            elif seated:
                lines.append("-# They keep their seat until set to No thanks.")
        await self.show(interaction, "\n".join(lines),
                        self.button("Confirm", self.save, discord.ButtonStyle.success),
                        self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        m = self.member()
        ev = self.rs.for_team(self.draft["team"])  # re-read: never trust the screen
        if not ev or sheet_state(ev) not in LIVE:
            await self.finish(interaction, "That run closed — nothing was changed.")
            return
        want = self.draft["character"]
        if not m or (want and not any(want in (c.name, c.label) for c in m.active())):
            await self.finish(interaction, "That character isn't active any more — nothing was changed.")
            return
        await self.working(interaction, "Saving…")
        try:
            line = await interaction.client.set_answer(self.reg, self.rs, ev, m, self.draft["status"], want, by=interaction.user.display_name)
        except ValueError as e:
            line = f"❌ {e}"
        await self.finish(interaction, line)


@flow("raid_set")
async def open_raid_set(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User, status: str, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    await SetWizard(reg, rs, interaction.user.id, member.id, status).start(interaction)


# ---------------------------------------------------------------- run-scoped commands

@flow("raid_sheet")
async def open_raid_sheet(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live sheet.")
            return
        if i.channel is None:
            await wiz.finish(i, "I can't post the sheet here — run /raid sheet in a server channel.")
            return
        embed, view = sheet_message(reg, ev, run_team(reg, ev), i.client.ico)
        msg = await i.channel.send(embed=embed, **({"view": view} if view else {}))
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")
        await wiz.finish(i, f"Re-posted the sheet for {run_label(reg, ev)} in this channel.")
    await with_run(interaction, reg, title="Re-post a sheet", then=body)


@flow("raid_health")
async def open_raid_health(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    from .discord_registry import is_officer

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live sheet.")
            return
        if sheet_state(ev) != "open":
            await wiz.finish(i, f"{run_label(reg, ev)} is {sheet_state(ev)} — the health check is for an open sheet; see its lock cards in the roster channel.")
            return
        layout, eph = health_layout(reg, rs, ev, run_team(reg, ev), i.client.ico), not is_officer(i, reg)
        if is_command(i):
            await i.response.send_message(view=layout, ephemeral=eph)
            return
        await wiz.finish(i, f"Health check for {run_label(reg, ev)} below.")
        await i.followup.send(view=layout, ephemeral=eph)
    await with_run(interaction, reg, title="Roster health", then=body, states=("open",))


@flow("raid_lock")
async def open_raid_lock(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev or ev.state != "open":
            await wiz.finish(i, "No open sheet to lock.")
            return
        await wiz.working(i, "Locking…")
        bot = i.client
        line = await bot.lock_run(reg, rs, ev, by=i.user.display_name)
        await wiz.finish(i, line)
        await bot.ops.emit(reg.config, "info", line)
    await with_run(interaction, reg, title="Lock a sheet", then=body, states=("open",), officer=True)


@flow("raid_loot")
async def open_raid_loot(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev or not ev.roster or ev.state == "open":
            await wiz.finish(i, "Lock the roster first (/raid lock).")
            return
        bot = i.client
        existing = next((s for s in bot.events.values() if s.id == ev.key and s.state == "raid"), None)
        if existing:
            await wiz.finish(i, f"Loot thread already open: <#{existing.raid_thread_id}>")
            return
        if i.channel is None:
            await wiz.finish(i, "I can't open the loot thread here — run /raid loot in a server channel.")
            return
        from .discord_bot import MockEvent as LootSession

        await wiz.working(i, "Opening the loot thread…")
        instance = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
        players = rc.players_for(reg, ev)
        e = discord.Embed(title=f"⚔ {run_title(reg, ev)}", colour=TEAL, description=f"<t:{int(ev.start.timestamp())}:F>\nRoster ({len(ev.roster.selected)}): " + ", ".join(p.character or p.signup_name for p in ev.roster.selected)[:3800])
        e.set_footer(text="loot council: tick drops (or let the companion feed do it) → Distribute → chat to adjust → Confirm · /raid end for the summary")
        msg = await i.channel.send(embed=e)
        thread = await msg.create_thread(name=f"loot · {run_title(reg, ev)}"[:100])
        session = LootSession(id=ev.key, channel_id=thread.id, instance=instance, date=ev.start.date().isoformat(), guild=reg.key, origin="raid", signups=players, roster=ev.roster, state="raid", raid_thread_id=thread.id)
        bot.events[thread.id] = session
        ev.thread_id = thread.id
        rs.save(ev, "loot thread opened")
        session.save(f"{session.id}: loot session opened")
        await wiz.finish(i, f"Loot thread open: <#{thread.id}>")
        await bot.post_boss_tables(session, thread)
        await bot.ops.emit(reg.config, "info", f"{i.user.display_name} opened loot council for {ev.key}")
    await with_run(interaction, reg, title="Loot council", then=body, states=("locked",), officer=True)


@flow("raid_cancel")
async def open_raid_cancel(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live sheet.")
            return
        key = ev.key

        async def submitted(si: discord.Interaction, v: dict[str, str]) -> None:
            why = await wiz.guard(si) if wiz.guard else None
            if why:
                await wiz.finish(si, why)
                return
            cur = rs.events.get(key)  # re-read: it may have closed while the box was open
            if not cur or cur.state in ("done", "cancelled"):
                await wiz.finish(si, "That run already closed — nothing was changed.")
                return
            await wiz.working(si, "Cancelling…")
            line = await si.client.cancel_run(reg, rs, cur, by=si.user.display_name, reason=v.get("reason") or None)
            if si.channel is not None:  # the old command said it in the channel, for everyone
                await si.channel.send(line)
            await wiz.finish(si, line)

        form = Form(f"Cancel {raid_name(reg, ev)} {reg.local12(ev.start, '%a %d %b')}", [
            Field("reason", "Why? (optional)", required=False, paragraph=True, max_length=300,
                  description="Submitting cancels the run. Close this box to keep it.", placeholder="shown in the channel and the run's thread"),
        ], submitted)
        await wiz.open_form(i, form)
    await with_run(interaction, reg, title="Cancel a run", then=body, officer=True)


@flow("raid_out")
async def open_raid_out(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    uid = interaction.user.id
    if not reg.members.get(uid):  # before any run picker: nothing to pick for someone who isn't registered
        await interaction.response.send_message("No live run, or you're not registered.", ephemeral=True)
        return

    def mine(ev: rc.RaidEvent) -> bool:  # the runs they answered Join/Bench on, or hold a seat in
        m = reg.members.get(uid)
        s = ev.signups.get(str(uid))
        return bool((s and s.status in ("in", "sub")) or (m and ev.seat_of(m.display_name)))

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev or not reg.members.get(uid):
            await wiz.finish(i, "No live run, or you're not registered.")
            return
        key = ev.key

        async def submitted(si: discord.Interaction, v: dict[str, str]) -> None:
            cur, m = rs.events.get(key), reg.members.get(uid)  # re-read
            if not cur or cur.state in ("done", "cancelled") or not m:
                await wiz.finish(si, "That run closed — nothing to call out of.")
                return
            note = v.get("note") or None
            t = run_team(reg, cur)
            bot = si.client
            co = rc.callout(reg, rs, cur, m, t, note)
            await wiz.finish(si, f"Noted: out for {run_label(reg, cur)} ({co.hours_before:.0f}h before{', after lock' if co.late else ''}). Thanks for saying so.")
            if sheet_state(cur) == "locked" and cur.seat_of(m.display_name):
                await bot.drop_seated(reg, rs, cur, t, m, "callout", note)
                return
            await bot.refresh_sheet(reg, cur)
            # the note is for the officers only: nothing in the public signup channel, one line in the run's updates thread
            await bot.post_run_update(reg, cur, f"⚑ {m.display_name} ({co.character}) called out, {co.hours_before:.0f}h before")
            await bot.ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {cur.key} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else ""))

        form = Form(f"Out · {raid_name(reg, ev)} {reg.local12(ev.start, '%a %d %b')}", [
            Field("note", "Note for the officers (optional)", required=False, paragraph=True, max_length=300,
                  description="Submitting marks you out (and frees your seat if you're rostered).", placeholder="only officers see this"),
        ], submitted)
        await wiz.open_form(i, form)
    await with_run(interaction, reg, title="Can't make it", then=body, prefer=mine)


@flow("raid_fill")
async def open_raid_fill(interaction: discord.Interaction, reg: Registry, *, preview: bool = False, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return
    import asyncio

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live run.")
            return
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            await wiz.finish(i, "Fill works after lock — the sheet is still open.")
            return
        await wiz.working(i, "Looking for people…")
        bot, t = i.client, run_team(reg, ev)
        nd = rc.needs(reg, ev, t)
        gaps = f"short {gaps_text(bot.ico, nd)}" if nd["headcount"] or nd["roles"] else "nothing short"
        busy = rc.conflicts(rs, ev)
        if preview:
            cands = await asyncio.to_thread(rc.fill_candidates, reg, rs, ev, t)
            lines = [f"{n + 1}. " + ask_line(reg, bot.ico, a) + (" — tied to " + next((b.display_name for b in cands if b.discord_id == a.pair), "?") if a.pair else "") for n, a in enumerate(cands[:15])]
            open_asks = [a for a in ev.fill_asks if a.open]
            await wiz.finish(i, f"{run_label(reg, ev)} · {gaps}" + (f" · double-booked: {', '.join(reg.members[u].display_name for u in busy if u in reg.members)}" if busy else "") + f"\nOutstanding asks: {', '.join(a.display_name for a in open_asks) or 'none'}\nWould ask next:\n" + ("\n".join(lines) or "nobody left"))
            return
        sent, nd = await bot.run_fill(reg, rs, ev, t, by=i.user.display_name)
        if sent:
            await bot.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, bot.ico, a) for a in sent))
        await wiz.finish(i, f"{run_label(reg, ev)} · {gaps}\n" + ("Asked: " + ", ".join(ask_line(reg, bot.ico, a) for a in sent) if sent else ("Nothing to fill." if not (nd["headcount"] or nd["roles"]) else "Nobody left to ask (or the asks outstanding already cover it).")))
        await bot.ops.emit(reg.config, "info", f"{i.user.display_name} ran fill for {ev.key}: asked {len(sent)}")
    await with_run(interaction, reg, title="Fill seats", then=body, states=("locked",), officer=True)


__all__ = ["with_run", "OpenWizard", "SetWizard", "officer_guard", "officer_only", "run_team"]
