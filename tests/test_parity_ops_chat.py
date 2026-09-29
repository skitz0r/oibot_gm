"""Parity B1, chat side (batches 1 and 2): Apply defers before slow ops and still edits the diff (the ops line is written
even when the edit fails), the run_open preview carries Discord's own <t:…:f> stamp in Discord only, absences read
in words and name the sheets they touched, a plain question in an act channel is answered by the /ask snapshot, the
parser sees member roles by name, and a newcomer refused with APPLY_FIRST gets the Apply button. No Discord, no LLM."""
from __future__ import annotations

import asyncio
import types

import discord
import pytest
from conftest import OWNER, open_test_run

from oibot_gm import configops, discord_policy
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.raid_views import run_title

OPS_CH, GENERAL_CH = 900_001, 900_003


class Feed:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None):
        self.lines.append((level, text))


class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kw):
        self.log.append(("send", content, kw))

    async def edit_message(self, content=None, **kw):
        self.log.append(("edit_message", content, kw))

    async def defer(self, **kw):
        self.log.append(("defer", None, kw))


def interaction(uid, log, *, officer=False, edit_fails=False, **client_kw):
    async def plain_identity(reg, user):
        return officer or uid == OWNER, []

    async def followup(content=None, **kw):
        log.append(("followup", content, kw))

    async def edit_original_response(content=None, **kw):
        if edit_fails:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "Unknown interaction")
        log.append(("edit", content, kw))

    client = types.SimpleNamespace(plain_identity=plain_identity, raids=None, **client_kw)
    return types.SimpleNamespace(user=types.SimpleNamespace(id=uid, display_name=f"user{uid}"), client=client, response=Resp(log),
                                 followup=types.SimpleNamespace(send=followup), edit_original_response=edit_original_response)


# ---- batch 1: Apply defers, then edits; the ops line is written either way

def test_apply_defers_before_a_slow_op_and_edits_the_original(reg, monkeypatch):
    feed, log = Feed(), []

    async def slow_apply(reg_, op, by, is_owner, ps=None, bot=None, by_id=None, **_kw):
        log.append(("applying", op.op, {}))
        await asyncio.sleep(0.05)  # stands in for a lock that takes longer than Discord's 3 seconds
        return "locked bd-x"

    monkeypatch.setattr(configops, "apply_async", slow_apply)

    async def go():
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", value="off")])
        me = reg.test_members()[0]
        view = discord_policy.ConfigConfirmView(reg, req, None, feed, me.discord_id, me.display_name, OPS_CH)
        await view.apply.callback(interaction(me.discord_id, log))
        kinds = [k for k, _, _ in log]
        assert kinds == ["defer", "applying", "edit"], kinds
        assert log[-1][1] == "✅ locked bd-x" and log[-1][2]["view"] is None
        assert feed.lines and "plain-text change" in feed.lines[-1][1] and "locked bd-x" in feed.lines[-1][1]
        # the edit fails (the interaction expired): the ops line still records the change
        log.clear()
        feed.lines.clear()
        view = discord_policy.ConfigConfirmView(reg, req, None, feed, me.discord_id, me.display_name, OPS_CH)
        await view.apply.callback(interaction(me.discord_id, log, edit_fails=True))
        assert [k for k, _, _ in log] == ["defer", "applying"] and "locked bd-x" in feed.lines[-1][1]

    asyncio.run(go())


def test_a_refused_presser_is_answered_without_deferring(reg):
    feed, log = Feed(), []

    async def go():
        me, other = reg.test_members()[:2]
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", value="off")])
        view = discord_policy.ConfigConfirmView(reg, req, None, feed, me.discord_id, me.display_name, OPS_CH)
        await view.apply.callback(interaction(other.discord_id, log))
        assert log == [("send", f"Only {me.display_name} or an officer can apply this.", {"ephemeral": True})]

    asyncio.run(go())


# ---- batch 1: the run_open preview's <t:…:f> stamp is Discord-only

def test_run_open_preview_has_a_discord_stamp_but_describe_does_not(reg):
    op = ConfigOp(op="run_open", target="barrow_deeps", value="2030-12-10 19:30")
    plain = configops.describe(reg, op)
    line = discord_policy.preview_line(reg, op)
    start, _ = configops._open_time(reg, "barrow_deeps", op.value)
    assert "<t:" not in plain and "7:30 PM" in plain
    assert line == plain + f" · <t:{int(start.timestamp())}:f>"
    bad = ConfigOp(op="run_open", target="nowhere")
    assert discord_policy.preview_line(reg, bad) == configops.describe(reg, bad), "an op that can't apply gets no stamp"


# ---- batch 1: absences in words, and the sheets they touched

def test_absence_describe_uses_words_not_iso(reg):
    m = reg.test_members()[0]
    line = configops.describe(reg, ConfigOp(op="absence", member=m.display_name, start="2030-03-01", end="2030-03-03", reason="holiday"))
    assert line == f"{m.display_name}: absent {reg.span_label('2030-03-01', '2030-03-03')} (holiday)"
    assert "2030-03-01" not in line and "Mar" in line


def test_apply_async_absence_names_the_sheets_it_touched(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    seen = []

    async def announce_absence(reg_, m_, a, by):
        seen.append((m_.display_name, a.start, by))
        return [ev.key]

    bot = types.SimpleNamespace(announce_absence=announce_absence, raids=types.SimpleNamespace(store=lambda r: rs))
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="absence", member=m.display_name, start="2030-03-01", end="2030-03-02"), "Officer", False, bot=bot))
    assert line == f"{m.display_name} absent {reg.span_label('2030-03-01', '2030-03-02')} · off 1 sheet: {run_title(reg, ev)}"
    assert seen == [(m.display_name, "2030-03-01", "Officer")]

    async def none(reg_, m_, a, by):
        return []

    bot.announce_absence = none
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="absence", member=m.display_name, start="2030-04-01"), "Officer", False, bot=bot))
    assert "off" not in line and line.endswith(reg.span_label("2030-04-01", None))


# ---- batch 2: a question in an act channel is answered by the /ask snapshot

def test_question_in_an_act_channel_goes_to_help(reg):
    log, asked = [], []

    async def help_answer(reg_, uid, officer, text, may_act=False):
        asked.append((uid, officer, text, may_act))
        return "the full answer"

    async def go():
        it = interaction(OWNER, log, help_answer=help_answer)
        req = ConfigRequest(kind="question", reply="parser's short line", ops=[])
        await discord_policy.handle_change(it, reg, None, None, Feed(), "who is rostered tonight?", bot=it.client, channel=OPS_CH, req=req)
        assert asked == [(OWNER, True, "who is rostered tonight?", False)]
        assert log[-1] == ("followup", "the full answer", {"ephemeral": True})
        # outside the ask audience help says None: the parser's reply stands
        async def outside(*a, **k):
            return None
        it.client.help_answer = outside
        await discord_policy.handle_change(it, reg, None, None, Feed(), "x?", bot=it.client, channel=OPS_CH, req=req)
        assert log[-1] == ("followup", "parser's short line", {"ephemeral": True})
        # a clarifying question (the parser needs to know which run) stays the parser's
        it.client.help_answer = help_answer
        asked.clear()
        clar = ConfigRequest(kind="question", reply="Which run?", ops=[], questions=["bd-1 or bd-2?"])
        await discord_policy.handle_change(it, reg, None, None, Feed(), "lock the run", bot=it.client, channel=OPS_CH, req=clar)
        assert not asked and log[-1][1] == "Which run?\n• bd-1 or bd-2?"
        # help already answered this message elsewhere (the router's ACT path): no second help call
        await discord_policy.handle_change(it, reg, None, None, Feed(), "x?", bot=it.client, channel=GENERAL_CH, req=req, to_help=False)
        assert not asked and log[-1][1] == "parser's short line"

    asyncio.run(go())


# ---- batch 2: the parser's context names member roles, open applications and waiting proposals

def test_current_config_text_names_member_roles(reg):
    reg.role_names = {777: "Raider", 778: "Guest"}
    reg.set_recruitment("t", member_role_ids=[777, 999])
    text = configops.current_config_text(reg, officer=True)
    assert "member_roles:\n- Raider\n- role:999" in text and "member_role_ids" not in text
    assert "## Open applications" in text and "## News proposals waiting" in text


# ---- batch 4 (chat side): a newcomer refused with APPLY_FIRST gets the Apply to join button

def test_apply_first_refusal_carries_the_apply_button(reg):
    log = []
    newcomer = 4_242_000_001

    async def go():
        it = interaction(newcomer, log)
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="character", field="add", character="Nyx Starfall", value="Priest Holy", text="main")])
        await discord_policy.handle_change(it, reg, None, None, Feed(), "add my main Nyx", bot=it.client, channel=GENERAL_CH, req=req)
        kind, content, kw = log[-1]
        assert content == "⛔ " + configops.APPLY_FIRST
        assert [getattr(c, "custom_id", None) for c in kw["view"].children] == ["reg:apply"]
        # applications closed: the refusal alone, no button
        reg.set_recruitment("t", open=False)
        await discord_policy.handle_change(it, reg, None, None, Feed(), "add my main Nyx", bot=it.client, channel=GENERAL_CH, req=req)
        assert log[-1] == ("followup", "⛔ " + configops.APPLY_FIRST, {"ephemeral": True})

    asyncio.run(go())


def test_apply_first_alongside_allowed_ops_adds_the_button_to_the_diff(reg):
    log = []
    newcomer = 4_242_000_002

    async def go():
        it = interaction(newcomer, log)
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="character", field="add", value="Priest Holy", text="main"),
                                                          ConfigOp(op="dm", value="off")])
        reg.add_absence(newcomer, "2030-01-01", None, None, "t", display_name="Newbie")  # a record, but nothing registered: still must apply
        reg.set_plain_policy({"groups": {"self": ["everyone"]}}, "owner")  # so the DM op is allowed for someone unregistered
        await discord_policy.handle_change(it, reg, None, None, Feed(), "plan my main and turn DMs off", bot=it.client, channel=GENERAL_CH, req=req)
        kind, content, kw = log[-1]
        assert isinstance(kw["view"], discord_policy.ConfigConfirmView)
        assert "reg:apply" in [getattr(c, "custom_id", None) for c in kw["view"].children]
        assert "⛔ " + configops.APPLY_FIRST in kw["embed"].description

    asyncio.run(go())


@pytest.mark.parametrize("value", ["2030-12-10 19:30"])
def test_run_open_stamp_in_the_proposed_changes_embed(reg, value):
    log = []

    async def go():
        it = interaction(OWNER, log)
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="run_open", target="barrow_deeps", value=value)])
        await discord_policy.handle_change(it, reg, None, None, Feed(), "open bd", bot=it.client, channel=OPS_CH, req=req)
        assert "<t:" in log[-1][2]["embed"].description

    asyncio.run(go())
