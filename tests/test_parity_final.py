"""Stage C final fixer: officer_only_channel doesn't count the bot's own role or overwrite as a member (an officer-only
channel the bot was let into stays officer-only), and lock_run solves again when an answer or pin changes during the
solve (a No thanks pressed mid-lock is never rostered). No Discord, no LLM."""
from __future__ import annotations

import asyncio
import time
import types

import discord
from conftest import OFFICER_ROLE, join, open_test_run
from test_parity_d1 import ANALYTICS_CH, Chan, Mem, Perms, Role
from test_parity_verbs import Bot

from oibot_gm import raidcycle as rc
from oibot_gm.discord_help import officer_only_channel

BOT_UID = 777_000_000_000_000_001


# ---------------------------------------------------------------- the bot's own role / overwrite is not a member

class BotRole(Role):
    """The managed role Discord gives the bot on invite (Role.is_bot_managed() is True for it)."""

    def is_bot_managed(self):
        return True


class Booster(Role):
    """Server Booster: managed too, but held by people."""
    managed = True

    def is_bot_managed(self):
        return False


class BotMem(Mem):
    bot = property(lambda s: True)


def bot_guild():
    everyone, raider, officer = Role(1, "@everyone"), Role(2, "Raider"), Role(OFFICER_ROLE, "Officer")
    oibot, boost = BotRole(5, "oibot"), Booster(6, "Server Booster")
    me = types.SimpleNamespace(id=BOT_UID)
    return types.SimpleNamespace(roles=[everyone, raider, officer, oibot, boost], me=me)


def test_the_bots_own_role_or_overwrite_keeps_an_officer_channel_officer_only(reg):
    """Analytics: @everyone deny, Officer allow, the bot's role allow → officer-only (the manual's §10a promise)."""
    guild = bot_guild()
    allow = discord.PermissionOverwrite(view_channel=True)
    assert officer_only_channel(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE, 5})), "the bot's managed role"
    assert officer_only_channel(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE},
                                          overwrites={discord.Object(id=BOT_UID, type=discord.User): allow})), "guild.me's overwrite"
    assert officer_only_channel(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE}, overwrites={BotMem(BOT_UID + 1, []): allow})), "a cached bot Member"
    # people still count: a human-held managed role, a raider, or a raider Member overwrite
    assert not officer_only_channel(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE, 5, 6})), "boosters can read it"
    assert not officer_only_channel(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE, 5, 2})), "raiders can read it"
    assert not officer_only_channel(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE}, overwrites={Mem(424_245, []): allow}))
    assert Perms  # the d1 stand-ins, reused


# ---------------------------------------------------------------- lock_run: answers and pins written during the solve

def counting_propose(delay=0.1, calls=None):
    """The solve in its worker thread: seats every 'in' joiner not pinned out (at most 10), after `delay`."""
    def propose(reg, rs, ev, save=True):
        if calls is not None:
            calls.append(sorted(s.display_name for s in ev.signups.values() if s.status == "in"))
        time.sleep(delay)
        out = {u for u, p in ev.pins.items() if p == "out"}
        names = [s.display_name for k, s in ev.signups.items() if s.status == "in" and k not in out][:10]
        ev.rosters = rc.board_rosters(reg, ev, [names[:5], names[5:10]], rc.run_size(reg, ev))
        ev.log.append("solver: proposed")
        return [], None
    return propose


def seated(ev):
    return {p.signup_name for r in ev.all_rosters for p in r.selected}


def test_a_no_thanks_pressed_during_the_lock_solve_is_not_rostered(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "propose", counting_propose(calls=calls))
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    quitter = ms[3]

    async def decline_later():
        await asyncio.sleep(0.03)  # the lock is solving now; the sheet is still open
        return await bot.set_answer(reg, rs, ev, quitter, "out", None, by=quitter.display_name)

    async def go():
        return await asyncio.gather(bot.lock_run(reg, rs, ev, by="scheduler"), decline_later())

    locked, _ = asyncio.run(go())
    assert "locked by scheduler" in locked, locked
    assert ev.signups[str(quitter.discord_id)].status == "out"
    assert quitter.display_name not in seated(ev), "rostered from an answer they had already changed"
    assert len(calls) == 2 and quitter.display_name not in calls[1], "solved again on the live answers"
    assert any("changed during the solve — solving again" in line for line in ev.log)
    asked = {m.discord_id for m in ms if any(a["roster"] == ev.team for a in reg.members[m.discord_id].placement_asks)}
    assert quitter.discord_id not in asked and len(asked) == 9, "no Confirm ask for a seat they declined"


def test_a_pin_out_set_during_the_lock_solve_is_honoured(reg, rs, monkeypatch):
    monkeypatch.setattr(rc, "propose", counting_propose())
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)

    async def pin_later():
        await asyncio.sleep(0.03)
        rc.set_pin(ev, ms[0].display_name, "out")  # the site's pin: no lock while the sheet is open

    async def go():
        return await asyncio.gather(bot.lock_run(reg, rs, ev, by="chat"), pin_later())

    locked, _ = asyncio.run(go())
    assert "locked by chat" in locked and ms[0].display_name not in seated(ev)


def test_answers_that_keep_changing_fail_the_lock_and_leave_the_sheet_open(reg, rs, monkeypatch):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    solve = counting_propose(delay=0)
    calls = []

    def propose(reg_, rs_, trial, save=True):  # every solve, someone changes their answer on the live sheet meanwhile
        ev.signups[str(ms[len(calls)].discord_id)].status = "sub"
        calls.append(1)
        return solve(reg_, rs_, trial, save)

    monkeypatch.setattr(rc, "propose", propose)
    line = asyncio.run(Bot(reg, rs).lock_run(reg, rs, ev, by="chat"))
    assert "lock failed" in line and "kept changing" in line, line
    assert len(calls) == rc.LOCK_SOLVES and ev.state == "open" and not ev.rosters and ev.lock_error


def test_a_members_own_reason_stays_out_of_a_public_reply(reg, rs):
    """Asking where others read the reply: the snapshot leaves out even the asker's own absence reason (in a DM or a
    private channel it is theirs to see)."""
    from datetime import timedelta

    from oibot_gm import help as help_mod
    m = reg.test_members()[0]
    day = (reg.now_local().date() + timedelta(days=5)).isoformat()
    reg.add_absence(m.discord_id, day, day, "dentist appointment", "t")
    private = help_mod.guild_state(reg, rs, m.discord_id, False, "when am I away?")
    public = help_mod.guild_state(reg, rs, m.discord_id, False, "when am I away?", public=True)
    assert "dentist" in private and "dentist" not in public and "left out on purpose" in public


def test_cancel_claims_the_diff_so_a_pending_apply_refuses():
    from types import SimpleNamespace

    from oibot_gm import discord_policy as dp
    view = dp.ConfigConfirmView.__new__(dp.ConfigConfirmView)
    view.busy = False
    assert not view.busy
    # the Cancel handler sets busy before its first await on the edit: Apply's own check then refuses
    import inspect
    src = inspect.getsource(dp.ConfigConfirmView.cancel)
    assert src.index("self.busy = True") < src.index("await interaction.response.edit_message")
    _ = SimpleNamespace
