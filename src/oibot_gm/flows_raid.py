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

from . import configops
from . import raidcycle as rc
from . import wizard_opts as wo
from .raid_views import health_layout, health_locked_brief, raid_name, run_label, run_title, sheet_state
from .registry import ADHOC_SCHEDULE, ADHOC_SCHEDULE_NAME, Registry, RegistryError, schedule_class
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
    """Raid → Next run · Another upcoming run… (a select of the other upcoming runs of every recurring schedule) ·
    Ad hoc run… (a pickup template, or a one-off — then day · hour · minute selects; roster redesign §5) → confirm with
    both clocks → open and post. A one-off is ADHOC_SCHEDULE: ad hoc defaults, never a run of the raid's own times."""

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
        if sid == ADHOC_SCHEDULE:
            return ADHOC_SCHEDULE_NAME
        s = next((x for x in self.reg.schedules(self.rid) if x["id"] == (sid or "default")), None)
        return (s or {}).get("name") or ""

    def template_buttons(self) -> list[discord.ui.Item]:
        """Up to three pickup templates as buttons; more than three become ONE select (design B4: a screen holds five
        rows, and a button per template would push One-off / Back off it)."""
        tpls = self.templates()
        if len(tpls) > 3:
            async def picked(interaction: discord.Interaction, values: list[str]) -> None:
                await self._template(values[0])(interaction)
            return [self.select("Pick a template", [Opt(s["id"], s["name"], "pick a day and time next") for s in tpls], picked)]
        return [self.button(f"{s['name']} — pick a time", self._template(s["id"])) for s in tpls]

    def is_adhoc(self, sid: str | None) -> bool:
        if sid == ADHOC_SCHEDULE:
            return True
        s = next((x for x in self.reg.schedules(self.rid) if x["id"] == (sid or "default")), None)
        return bool(s) and schedule_class(s["kind"]) == "adhoc"

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

    # ---- layer 1: Next run · Another upcoming run… · Ad hoc run…
    async def shortlist(self, interaction: discord.Interaction) -> None:
        ups = self.upcoming()
        self.draft["day0"] = self.reg.now_local().date()
        self.draft["schedule"], self.draft["matched"] = None, None
        many = self.many()
        if not ups:
            await self.show(interaction, f"**{self.name()}** has no upcoming run to open: {self.no_slot_reason()}\n"
                            "-# Set its run times, or open an ad hoc run at a day and time you pick.",
                            self.button("Set this raid's run times →", self.to_run_times, discord.ButtonStyle.primary),
                            self.button("Ad hoc run…", self.adhoc),
                            self.button("Cancel", self.cancel))
            return
        s0, t0 = ups[0]
        items: list[discord.ui.Item] = [self.button(f"Next run — {self.reg.local12(t0)}" + (f" · {s0['name']}" if many else ""), self.next_picked, discord.ButtonStyle.primary)]
        if len(ups) > 1:
            items.append(self.button("Another upcoming run…", self.another))
        items += [self.button("Ad hoc run…", self.adhoc), self.button("Cancel", self.cancel)]
        await self.show(interaction, f"**{self.name()}** — which run?\n-# Ad hoc = a pickup or a one-off at a time you pick: first come, no regulars, no reminder.", *items)

    async def another(self, interaction: discord.Interaction) -> None:
        """The other upcoming runs of every recurring schedule (the value names the schedule unless it is the raid's own).
        More than OTHER_STARTS of them page BY WEEK (design B4): one week per screen with ← Earlier week / Later week →,
        so a busy raid's runs past the first 22 are reachable without typing a date."""
        ups = self.upcoming()
        others = ups[1:]
        if not others:
            await self.shortlist(interaction)
            return
        many = self.many()
        page_btns: list[discord.ui.Item] = []
        head = f"**{self.name()}** — which upcoming run?"
        if len(others) > OTHER_STARTS:
            tz = self.reg.tz
            weeks = sorted({(t.astimezone(tz).date() - timedelta(days=t.astimezone(tz).weekday())) for _s, t in others})
            page = max(0, min(int(self.draft.get("week_page") or 0), len(weeks) - 1))
            self.draft["week_page"] = page
            monday = weeks[page]
            others = [(s, t) for s, t in others if monday <= t.astimezone(tz).date() < monday + timedelta(days=7)][:OTHER_STARTS]
            head += f"\n-# Week of {monday.strftime('%a %d %b')} ({page + 1} of {len(weeks)})"
            page_btns = [self.button("← Earlier week", self._week_page(page - 1), disabled=page == 0),
                         self.button("Later week →", self._week_page(page + 1), disabled=page >= len(weeks) - 1)]
        else:
            others = others[:OTHER_STARTS]
        opts = [Opt(t.isoformat() + ("" if s["id"] == "default" else f"|{s['id']}"), self.reg.local12(t), (f"{s['name']} · " if many else "") + when_relative(self.reg, t)) for s, t in others]
        await self.show(interaction, head, self.select("Pick the run", opts, self.start_picked), *page_btns,
                        self.button("Back", self.shortlist), self.button("Cancel", self.cancel))

    def _week_page(self, page: int):
        async def go(interaction: discord.Interaction) -> None:
            self.draft["week_page"] = page
            await self.another(interaction)
        return go

    async def adhoc(self, interaction: discord.Interaction) -> None:
        """An ad hoc run: one of the raid's pickup templates (its own settings) or a one-off; both then pick a day and time."""
        if not self.templates():
            await self.open_when(interaction)
            return
        await self.show(interaction, f"**{self.name()}** · an ad hoc run — first come, no regulars, no reminder. From a template, or a one-off?",
                        *self.template_buttons(), self.button("One-off — pick a day and time", self.open_when),
                        self.button("Back", self.shortlist), self.button("Cancel", self.cancel))

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
        title = f"Open {self.sched_name(tpl) or self.name()}" if tpl and tpl != ADHOC_SCHEDULE else f"Open {self.name()}"
        return Form(title, when_fields(self.reg, day0=self.draft["day0"], default=usual, day_note=self._day_note), self.when_submitted)

    def _day_note(self, day: date) -> str | None:
        hits = [t for t in self.starts() if t.astimezone(self.reg.tz).date() == day]
        return f"{self.name()} run time {self.reg.local12(hits[0], '%I:%M %p')}" if hits else None

    async def open_when(self, interaction: discord.Interaction) -> None:
        self.draft["schedule"] = ADHOC_SCHEDULE  # a one-off at a picked time: ad hoc (its own defaults, never the raid's run times)
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
        self.draft["start"], self.draft["note"], self.draft["matched"] = start, note, None
        if self.draft.get("schedule") == ADHOC_SCHEDULE:
            # a one-off at exactly an upcoming recurring run's minute IS that run (rc.which_run — the site's preview says
            # the same): opened as scheduled, with its regulars and confirmations, never a second "ad hoc" run beside it
            w = rc.which_run(self.reg, self.rid, start, None)
            if w["kind"] == "scheduled":
                self.draft["schedule"], self.draft["matched"] = w["schedule"] or "default", w["schedule_name"]
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
        if self.draft.get("matched"):
            lines.append(f"-# That time is the {self.draft['matched']} run — it opens as scheduled: its regulars are expected"
                         + (" and the rostered are asked to confirm." if self.reg.schedule_def(self.rid, sid).get("confirm", True) else "."))
        if self.is_adhoc(sid):
            sd = self.reg.schedule_def(self.rid, sid)
            lines.append("-# Ad hoc: first come, first rostered; no regulars, no reminder" + ("" if sd.get("confirm") else ", no confirmations (Join is the commitment)")
                         + f"; locks {sd['lock_hours_before']:g} h before.")
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
        closed = rc.closed_run_refusal(self.rs, rc.event_key(self.rid, start, self.draft.get("schedule")))
        if closed:  # open_run refuses it: the screen says so instead of offering an Open that would fail
            lines.append(f"-# {closed}")
            await self.show(interaction, "\n".join(lines), self.button("Change", self.shortlist), self.button("Cancel", self.cancel))
            return
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
            seated = ev.seat_of(m)
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
            line = await interaction.client.set_answer(self.reg, self.rs, ev, m, self.draft["status"], want, by=interaction.user.display_name, by_id=interaction.user.id)
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
        try:
            line = await i.client.repost_sheet(reg, rs, ev, i.channel)  # the verb the site, MCP and plain text use
        except RegistryError as e:  # a channel of another server
            line = str(e)
        await wiz.finish(i, line)
    await with_run(interaction, reg, title="Re-post a sheet", then=body)


@flow("raid_health")
async def open_raid_health(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    from .discord_registry import is_officer

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live sheet.")
            return
        # any time (design B4: the officer's "where are we"): an open sheet's health card, or a locked run's brief —
        # an officer gets the full card (Suggested block / who hasn't answered or confirmed, the buttons), shown to
        # everyone only in the run's officer channel; a member gets the redacted card, always just to them
        officer = is_officer(i, reg)
        oc = i.client.officer_channel(reg, ev) if officer else None
        eph = not (officer and oc is not None and i.channel is not None and i.channel.id == oc.id)
        if sheet_state(ev) == "open":
            layout = health_layout(reg, rs, ev, run_team(reg, ev), i.client.ico, officer=officer)
        else:
            layout = health_locked_brief(reg, ev, i.client.ico, officer=officer)
        if is_command(i):
            await i.response.send_message(view=layout, ephemeral=eph)
            return
        await wiz.finish(i, f"Health check for {run_label(reg, ev)} below.")
        await i.followup.send(view=layout, ephemeral=eph)
    await with_run(interaction, reg, title="Roster health", then=body)


@flow("raid_lock")
async def open_raid_lock(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """/raid lock — the run picker when several are open, then THE lock confirm screen (raid_buttons.lock_decide: the
    consequence from code, Lock now · Preview lock · Back) — the card's Lock now shows the same screen."""
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev or ev.state != "open":
            await wiz.finish(i, "No open sheet to lock.")
            return
        from .raid_buttons import lock_decide

        wiz.title = f"Lock {raid_name(reg, ev)}"
        await lock_decide(i, i.client, reg, rs, ev, wiz=wiz)
    await with_run(interaction, reg, title="Lock a sheet", then=body, states=("open",), officer=True)


@flow("raid_loot")
async def open_raid_loot(interaction: discord.Interaction, reg: Registry, *, key: str | None = None, **_seed) -> None:
    """/raid loot (the run picker when several are locked) or the lock card's Loot button (`key`: that card's run,
    never a picker — two locked runs each open their own council from their own card)."""
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev or not ev.roster or ev.state == "open":
            await wiz.finish(i, "Lock the roster first (/raid lock).")
            return
        bot = i.client
        await wiz.working(i, "Opening the loot council…")
        # LootMixin.open_council: one session per roster of a split run ("loot · Roster 2"), else one for the run —
        # keyed (guild, session), its thread in the officer channel; idempotent (two officers at once get the same
        # council). An open council: its pending thread is tried again and what still waits is re-posted with buttons.
        had_thread = {s.id for s in bot.events.values() if s.guild == reg.key and s.run_key == ev.key and s.raid_thread_id}
        try:
            opened = await bot.open_council(reg, rs, ev, by=i.user.display_name, by_id=i.user.id)
        except RegistryError as e:
            await wiz.finish(i, configops.user_error(e))
            return
        lines = []
        for session, created in opened:
            label = f"{session.label}: " if session.roster_index is not None and session.label else ""
            if not session.raid_thread_id:
                lines.append(f"{label}the council is open, but I couldn't reach the officer channel — its thread is pending (I try again on reconnect, or run /raid loot again).")
                continue
            lines.append(f"{label}" + ("Loot thread open: " if created else "Loot council is open — card re-posted in ") + f"<#{session.raid_thread_id}>")
        await wiz.finish(i, "\n".join(lines))
        # a new council's session card is its thread's first message (LootSurfaceMixin.loot_open_thread: Tick drops… is
        # the one tick path); an open one gets a fresh card and its pending picks at the bottom of the thread
        for session, created in opened:
            if not created and session.id in had_thread:  # a thread made just now (it was pending) got its re-post from loot_ensure_thread
                await bot.loot_repost(reg, session)
    if key:
        rs = interaction.client.raids.store(reg)
        wiz = Wizard(reg, interaction.user.id, title="Loot council", guard=officer_guard(reg))
        await body(interaction, wiz, rs, rs.events.get(key))
        return
    await with_run(interaction, reg, title="Loot council", then=body, states=("locked",), officer=True)


@flow("raid_cancel")
async def open_raid_cancel(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """/raid cancel — the run picker when several are live, then THE cancel screen (raid_buttons.cancel_decide: the
    consequence, then one Form with the optional reason and "Tell the channel?"; nobody is DMed). The card's Cancel run
    shows the same screen."""
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live sheet.")
            return
        from .raid_buttons import cancel_decide

        wiz.title = f"Cancel {raid_name(reg, ev)}"
        await cancel_decide(i, i.client, reg, rs, ev, wiz=wiz)
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
        return bool((s and s.status in ("in", "sub")) or (m and ev.seat_of(m)))

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
            await wiz.working(si, "Noting it…")
            # the shared verb (the site, MCP and plain text call it too): callout, seat freed after lock, the note for
            # the officers only (the run's updates thread and the ops feed, never the public signup channel)
            try:
                line = await si.client.call_out(reg, rs, cur, m, v.get("note") or None, by=m.display_name, by_id=m.discord_id)
            except ValueError as e:
                line = f"❌ {e}"
            await wiz.finish(si, line)

        form = Form(f"Out · {raid_name(reg, ev)} {reg.local12(ev.start, '%a %d %b')}", [
            Field("note", "Note for the officers (optional)", required=False, paragraph=True, max_length=300,
                  description="Submitting marks you out (and frees your seat if you're rostered).", placeholder="only officers see this"),
        ], submitted)
        await wiz.open_form(i, form)
    await with_run(interaction, reg, title="Can't make it", then=body, prefer=mine)


# ---------------------------------------------------------------- suggestions: the card's Suggest…, /raid fill on an open run

MAX_PICK_SELECTS = 4  # a screen holds five rows: up to four seats get an alternates select, the buttons take the last


def _opt_label(opt: list) -> str:
    return " + ".join(rc._ask_words(a) for a in opt)[:100]


async def show_suggestions(i: discord.Interaction, wiz: Wizard, rs, key: str, note: str = "", show_only: bool = False) -> None:
    """The planner's suggestions for one run on the wizard's screen — before lock (the cards' Suggest…, /raid fill on
    an open run) and after it (/raid fill, the lock card's Fill seats): each seat's suggestion with the code-written
    reason, a select per seat that has alternates (pick someone else — no names typed), then the exact batch Send DMs
    (rc.batch_preview: one ask per line — who, as which character and spec, for which short role, SWAP marked, the tied
    backfill named — what waits under the cap and what stays short), then Send (RaidMixin.send_plan with the plan
    fingerprint this screen was drawn with: when the plan changed meanwhile — board or answers — nothing is sent and
    the screen is redrawn) and Cancel. Nothing is sent before Send. `show_only`: the same screen without Send."""
    reg = wiz.reg
    ev = rs.events.get(key)
    if not ev or ev.state not in LIVE:
        await wiz.finish(i, "That run is closed.")
        return
    plan = rc.role_plan(reg, rs, ev)
    if wiz.draft.get("rev") != plan.rev:
        wiz.draft["picks"] = {}  # a new plan: earlier picks pointed at its predecessor's seats
    wiz.draft["rev"], wiz.draft["key"] = plan.rev, key
    picks = wiz.draft.setdefault("picks", {})
    when = "before lock — a yes changes their signup, nobody is seated until the lock" if plan.phase == "prelock" else "after lock — a yes takes the seat (role swaps need your Ask)"
    lines = ([note] if note else []) + [f"{run_title(reg, ev)} · {when}"]
    withdraw = withdraw_select(wiz, rs, key, ev)  # an ask already out can be closed early from here (D15)
    if not plan.seats:
        tail = (" — the minimums are covered by who joined and the regulars yet to answer." if plan.phase == "prelock"
                else f" beyond the asks still out (waiting on {', '.join(plan.waiting)})." if plan.waiting else ".")
        if withdraw is None or show_only:
            await wiz.finish(i, "\n".join(lines + ["Nothing is short" + tail]))
            return
        await wiz.show(i, "\n".join(lines + ["Nothing is short" + tail]), withdraw, wiz.button("Close", wiz.cancel))
        return
    if plan.phase == "prelock":
        lines.append("Expected: " + " · ".join(f"{r} {plan.have.get(r, 0)}/{plan.need[r]}" for r in ("tank", "healer") if plan.need.get(r))
                     + (f" (joined + {plan.expected} regulars yet to answer)" if plan.expected else "") + (" · every seat taken, so swaps first" if plan.full else ""))
    try:
        chosen = rc.pick_asks(plan, picks) if plan.asks else []
    except RegistryError as e:  # one person picked for two seats: say so, Send stays off until a pick changes
        chosen = []
        lines.append(f"⚠️ {e}")
    if plan.waiting:
        lines.append(f"Asked, waiting on {', '.join(plan.waiting)} — their seats count as covered until they answer.")
    seats = []  # (the seat's line, the code-written reason)
    for n, st in enumerate(plan.seats):
        what = st.role or "seat"
        if not st.options:
            seats.append((f"{n + 1}. {what}: nobody can cover it → recruit", ""))
            continue
        k = int(picks.get(n, 0))
        opt = st.options[k] if k < len(st.options) else st.options[0]
        seats.append((f"{n + 1}. {what}: " + " + ".join(rc._ask_words(a) for a in opt) + (" (alternate)" if k else ""), opt[0].why or ""))
    batch = ["", "**A Send would DM:**" if show_only else "**Send DMs exactly these:**"] + (rc.batch_preview(plan, picks) if chosen else [f"Still short if everyone says yes: {rc.still_short_words(plan)}"])
    text, whole = fit_suggestions(lines, seats, batch)
    limit = None  # Send's seat limit: None = the whole batch shown
    if not whole and chosen:
        # the whole batch doesn't fit a screen: send what fits — the first seats whose asks the screen lists — and say the
        # rest are on the site (design B7: "Send these N (the rest on the site)" instead of a disabled Send)
        limit, text = fit_partial(lines, plan, picks)
    if show_only:
        await wiz.finish(i, text)
        return
    items = []
    selects = MAX_PICK_SELECTS - (1 if withdraw is not None else 0)
    for n, st in enumerate(plan.seats):
        if len(st.options) > 1 and len(items) < selects:
            async def picked(si: discord.Interaction, values: list[str], n=n) -> None:
                wiz.draft["picks"][n] = int(values[0])
                await show_suggestions(si, wiz, rs, key)
            opts = [Opt(str(k), _opt_label(opt), "the suggestion" if k == 0 else f"alternate {k}", default=int(picks.get(n, 0)) == k) for k, opt in enumerate(st.options)]
            items.append(wiz.select(f"Seat {n + 1} ({st.role or 'seat'}): who to ask", opts, picked))
    if withdraw is not None:
        items.append(withdraw)

    async def send(si: discord.Interaction) -> None:
        cur = rs.events.get(key)
        if not cur or cur.state not in LIVE:
            await wiz.finish(si, "That run closed — nothing sent.")
            return
        await wiz.working(si, "Sending…")
        try:
            extra = {"limit": limit} if limit else {}
            sent, sent_plan = await si.client.send_plan(reg, rs, cur, rev=wiz.draft["rev"], by=si.user.display_name, picks=dict(wiz.draft["picks"]), **extra)
        except rc.PlanChanged as e:
            await show_suggestions(si, wiz, rs, key, note=f"⚠️ {e}")
            return
        except RegistryError as e:
            await wiz.refuse(si, str(e), retry=lambda ri: show_suggestions(ri, wiz, rs, key))
            return
        from .raid_buttons import sent_words

        await wiz.finish(si, sent_words(sent) + (" A yes changes their signup; the lock still builds the roster." if plan.phase == "prelock" and sent else "")
                         + rc.held_back_words(sent_plan) + (" The rest of the plan is on the site's Rosters page (Fill)." if limit else ""))

    try:
        n_send = len(rc.send_batch(plan, picks, limit)[0]) if chosen else 0
    except RegistryError:  # the cap is full: Send stays off (the batch block says why)
        n_send = 0
    if not whole and not limit:  # not even one seat's asks fit on the screen: Send never DMs anyone the screen didn't show
        n_send = 0
    label = f"Send these {n_send} (the rest on the site)" if limit else f"Send {n_send} ask{'s' if n_send != 1 else ''}"
    items.append(wiz.button(label, send, discord.ButtonStyle.success, disabled=not n_send))
    items.append(wiz.button("Cancel", wiz.cancel))
    await wiz.show(i, text, *items)


def withdraw_select(wiz: Wizard, rs, key: str, ev: rc.RaidEvent):
    """'Withdraw an ask…' — one option per ask still out on the run (who, before or after lock, as what): the pick calls
    RaidMixin.withdraw_sent_ask (their DM card is edited to "Never mind", the tied half released) and redraws the
    screen. None when nothing is out."""
    out = [a for a in ev.fill_asks if a.pending]
    if not out:
        return None
    opts = [Opt(str(a.discord_id), rc._ask_words(a)[:100], ("before lock" if a.phase == "prelock" else "after lock") + (" · waits on the site" if a.channel == "web" else ""))
            for a in out][:MAX_OPTIONS]

    async def picked(si: discord.Interaction, values: list[str]) -> None:
        cur = rs.events.get(key)
        if not cur or cur.state not in LIVE:
            await wiz.finish(si, "That run closed — nothing was changed.")
            return
        try:
            line = await si.client.withdraw_sent_ask(wiz.reg, rs, cur, int(values[0]), by=si.user.display_name, by_id=si.user.id)
        except RegistryError as e:
            line = f"⚠️ {e}"
        await show_suggestions(si, wiz, rs, key, note=line)
    return wiz.select("Withdraw an ask…", opts, picked)


def fit_partial(lines: list[str], plan: rc.Plan, picks: dict, limit: int | None = None) -> tuple[int | None, str]:
    """The most seats whose asks fit on one screen, and that screen's text: the batch of the first k seats
    (rc.send_batch with limit=k — exactly what Send then DMs) one ask per line, then "the rest on the site"."""
    limit = limit or SUGGEST_TEXT_MAX
    n_seats = sum(1 for st in plan.seats if st.options)
    for k in range(n_seats, 0, -1):
        try:
            asks, held = rc.send_batch(plan, picks, k)
        except RegistryError:
            continue
        if not asks:
            continue
        body = [f"{n + 1}. {ln}" for n, ln in enumerate(rc.batch_lines(asks))]
        rest = n_seats - k
        text = "\n".join(lines + ["", f"**Send DMs exactly these {len(asks)}:**"] + body
                         + ([f"-# {rest} more seat{'s' if rest != 1 else ''} wait — the site's Rosters page (Fill) lists them all"] if rest else [])
                         + ([f"-# {held} more wait — at most {rc.FILL_MAX_OPEN} asks out at once"] if held else []))
        if len(text) <= limit:
            return k, text
    return None, "\n".join(lines) + "\n⚠️ Too many asks to show here, so Send stays off — send them from the site's Rosters page (Fill)."


SUGGEST_TEXT_MAX = 1900  # what a wizard screen holds (Discord's 2000 less room for the wizard's own lines)


def fit_suggestions(lines: list[str], seats: list[tuple[str, str]], batch: list[str], limit: int = SUGGEST_TEXT_MAX) -> tuple[str, bool]:
    """The suggestions screen's text within `limit`, the batch Send DMs kept whole: first the seats' reasons go, then
    seat lines from the end ("+N more seats"). Returns (text, whole): `whole` False = even the batch alone doesn't fit,
    so the screen says so and Send stays off — a Send never DMs anyone the screen didn't show."""
    full = "\n".join(lines + [ln + (f"\n   -# {why}" if why else "") for ln, why in seats] + batch)
    if len(full) <= limit:
        return full, True
    bare = [ln for ln, _why in seats]
    for keep in range(len(bare), -1, -1):
        more = [f"-# +{len(bare) - keep} more seat{'s' if len(bare) - keep != 1 else ''} — the selects below and the site list them all"] if keep < len(bare) else []
        text = "\n".join(lines + bare[:keep] + more + batch)
        if len(text) <= limit:
            return text, True
    warn = "⚠️ Too many asks to show here, so Send stays off — send them from the site's Raids page (Suggest)."
    out = "\n".join(lines)
    for ln in batch:
        if len(out) + len(ln) + len(warn) + 2 > limit:
            break
        out += "\n" + ln
    return (out + "\n" + warn)[:limit], False


@flow("raid_suggest")
async def open_raid_suggest(interaction: discord.Interaction, reg: Registry, *, key: str | None = None, **_seed) -> None:
    """The card's Suggest… (and any caller with a run key): the suggestions screen for that run; without a key, the run
    picker first."""
    if not await officer_only(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    if key:
        wiz = Wizard(reg, interaction.user.id, title="Suggestions", guard=officer_guard(reg))
        await show_suggestions(interaction, wiz, rs, key)
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live run.")
            return
        await show_suggestions(i, wiz, rs, ev.key)
    await with_run(interaction, reg, title="Suggestions", then=body, officer=True)


@flow("raid_fill")
async def open_raid_fill(interaction: discord.Interaction, reg: Registry, *, preview: bool = False, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if not ev:
            await wiz.finish(i, "No live run.")
            return
        if sheet_state(ev) != "open" and not ev.all_rosters:
            await wiz.finish(i, "The roster is still being built — try again in a moment.")
            return
        # before and after lock: the plan, the exact batch Send DMs, then Send / Cancel (RaidMixin.send_plan with the
        # rev this screen was drawn with — a stale plan is refused and re-shown); nothing goes out before Send.
        # `preview`: the same screen without Send.
        await show_suggestions(i, wiz, rs, ev.key, show_only=preview)
    await with_run(interaction, reg, title="Fill seats", then=body, officer=True)


__all__ = ["with_run", "OpenWizard", "SetWizard", "officer_guard", "officer_only", "run_team"]
