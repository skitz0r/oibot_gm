"""Parity B1, batch 8: the owner-only gate that no plain-text policy opens (HARD_OWNER_OPS), the `job` op (the Agents
page's Run now), the `proposal` op (the news card's Approve / Dismiss) and the `policy` op (replace / remove / compile,
with the bot's reading shown under Confirm / Discard; the persona is live as written). Every one reuses the verb the
site and the card use. No Discord, no LLM (a fake provider compiles)."""
from __future__ import annotations

import asyncio
import types

import pytest
from conftest import OWNER

from oibot_gm import agents, configops, discord_policy, news
from oibot_gm import policy as policy_mod
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.news import NewsStore, ProposalStore
from oibot_gm.policy import CompCompiled, LootCompiled, LootRule, PolicyStore
from oibot_gm.registry import PLAIN_WEB, RegistryError

URL = "https://forever.example/news/barrow-weekly"
OPS_CH = 900_001


@pytest.fixture(autouse=True)
def no_pending():
    policy_mod.PENDING.clear()
    yield
    policy_mod.PENDING.clear()


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, workload, system, user, schema):
        self.calls += 1
        if schema is LootCompiled:
            return LootCompiled(rules=[LootRule(id="R1", text="Mainspec before offspec.", kind="ordering"),
                                       LootRule(id="R2", text="Tanks first on shields.", kind="ordering")], ambiguities=["which shields?"], summary="Mainspec first.")
        return CompCompiled(constraints=[], summary="Nothing.")


class Feed:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None):
        self.lines.append((level, text))


class Bot:
    def __init__(self, provider=None):
        self.ctx = types.SimpleNamespace(provider=provider)
        self.ops = Feed()
        self.cards = []

    async def proposal_card_update(self, reg, p):
        self.cards.append((p.id, p.state))


# ---- the hard owner gate

def test_job_is_owner_only_even_when_setup_is_opened(reg):
    reg.set_plain_policy({"groups": {"setup": ["officers"]}}, "owner")
    officer = reg.test_members()[0].discord_id
    allowed, refused = configops.authorize(reg, [ConfigOp(op="job", value="review")], officer, officer=True, channel=PLAIN_WEB)
    assert not allowed and refused == ["Only the owner can start the bot's local jobs."]
    # the rest of the request is still judged on its own
    allowed, refused = configops.authorize(reg, [ConfigOp(op="job", value="review"), ConfigOp(op="set", path="about", value="hi")], officer, officer=True, channel=PLAIN_WEB)
    assert [op.op for op in allowed] == ["set"] and refused == ["Only the owner can start the bot's local jobs."]
    allowed, refused = configops.authorize(reg, [ConfigOp(op="job", value="review")], OWNER, officer=True, channel=PLAIN_WEB)
    assert allowed and not refused
    assert set(configops.HARD_OWNER_OPS) == {"plain", "job"}


# ---- job: agents.start_job, the checks the Agents page and MCP run_job hit

def test_job_starts_through_start_job(reg, monkeypatch):
    calls = []
    monkeypatch.setattr(agents, "read_status", lambda job, out=None: {"state": "idle"})

    def start(job, out=None):
        calls.append(job)
        return (True, f"{agents.JOBS[job]['name']} started") if job == "review" else (False, "Apply approved is already running")

    monkeypatch.setattr(agents, "start_job", start)
    assert configops.describe(reg, ConfigOp(op="job", value="news review")) == "start the News review job now (launchd runs it; the site's Agents page shows its steps)"
    assert asyncio.run(configops.apply_async(reg, ConfigOp(op="job", value="review"), "Owner", True)) == "News review started"
    with pytest.raises(RegistryError, match="already running"):
        asyncio.run(configops.apply_async(reg, ConfigOp(op="job", value="apply"), "Owner", True))
    assert calls == ["review", "apply"]
    assert "job is one of review, apply" in configops.describe(reg, ConfigOp(op="job", value="dance"))
    monkeypatch.setattr(agents, "read_status", lambda job, out=None: {"state": "running"})
    assert "already running" in configops.describe(reg, ConfigOp(op="job", value="review"))


# ---- proposal: ProposalStore.move + the card + an ops line

def a_proposal(reg, **kw):
    NewsStore(reg.store, reg.key).add({"url": URL, "title": "Barrow Deeps lockout is now 7 days on WoW: Forever realms", "description": "Weekly."},
                                      ["WoW: Forever", "Barrow Deeps"], news.now())
    data = {"title": "Barrow Deeps resets weekly", "news": [URL], "affects": "raid barrow_deeps lockout_days", "kind": "guild_setting",
            "change": "Barrow Deeps lockout 3 → 7 days", "edit": {"op": "raid_set", "target": "barrow_deeps", "field": "lockout_days", "value": "7"},
            "evidence": "Weekly.", "confidence": "high", **kw}
    return ProposalStore(reg.store, reg.key).create(reg, data)


def test_proposal_approve_then_dismiss(reg):
    p = a_proposal(reg)
    op = ConfigOp(op="proposal", target=p.id, value="approve", reason="checked the patch notes")
    assert configops.describe(reg, op) == (f"news proposal {p.id} “Barrow Deeps resets weekly” (guild_setting, high): proposed → approved — the apply job "
                                           "carries it out within 15 minutes; note: checked the patch notes")
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, op, "Officer", is_owner=False)
    bot = Bot()
    line = asyncio.run(configops.apply_async(reg, op, "Officer", False, bot=bot, by_id=42))
    assert line == f"news proposal {p.id} approved: Barrow Deeps resets weekly"
    got = ProposalStore(reg.store, reg.key).get(p.id)
    assert (got.state, got.decided_by, got.decided_by_id, got.note) == ("approved", "Officer", 42, "checked the patch notes")
    assert bot.cards == [(p.id, "approved")] and bot.ops.lines[-1][1] == f"news proposal {p.id} approved by Officer: Barrow Deeps resets weekly"
    asyncio.run(configops.apply_async(reg, ConfigOp(op="proposal", target=p.id, value="dismiss"), "Officer", False, bot=bot))
    assert ProposalStore(reg.store, reg.key).get(p.id).state == "dismissed"
    assert "dismissed: it can't become dismissed" in configops.describe(reg, ConfigOp(op="proposal", target=p.id, value="dismiss"))


def test_needs_developer_can_only_be_dismissed(reg):
    p = a_proposal(reg, kind="needs_developer", edit={})
    assert "needs a developer" in configops.describe(reg, ConfigOp(op="proposal", target=p.id, value="approve"))
    with pytest.raises(RegistryError, match="needs a developer"):
        asyncio.run(configops.apply_async(reg, ConfigOp(op="proposal", target=p.id, value="approve"), "Officer", False, bot=Bot()))
    assert "no proposal" in configops.describe(reg, ConfigOp(op="proposal", target="260101-ffff", value="approve"))
    assert f"- {p.id}: Barrow Deeps resets weekly (needs_developer, proposed)" in configops.current_config_text(reg, officer=True)


def test_proposals_are_officers_by_default(reg):
    p = a_proposal(reg)
    member = reg.test_members()[0].discord_id
    allowed, refused = configops.authorize(reg, [ConfigOp(op="proposal", target=p.id, value="approve")], member, officer=False, channel=PLAIN_WEB)
    assert not allowed and refused


# ---- policy: replace / remove / compile, and policy_append, through policy.begin_edit + compile_pending

def test_policy_describe(reg):
    ps = PolicyStore(reg.store, reg.key)
    assert "loot policy: replace the whole text" in configops.describe(reg, ConfigOp(op="policy", doc="loot", field="replace", text="# Loot\n\n1. Tanks first."))
    assert configops.describe(reg, ConfigOp(op="policy", doc="loot", field="remove", text="bigger upgrades first")).startswith("loot policy: remove “2. Bigger upgrades first.”")
    assert "no line like" in configops.describe(reg, ConfigOp(op="policy", doc="loot", field="remove", text="free pizza"))
    assert "compile the current text" in configops.describe(reg, ConfigOp(op="policy", doc="comp", field="compile"))
    assert "no compiled form" in configops.describe(reg, ConfigOp(op="policy", doc="persona", field="compile"))
    assert "live at once" in configops.describe(reg, ConfigOp(op="policy", doc="persona", field="replace", text="Be terse."))
    assert "doc is one of loot, comp" in configops.describe(reg, ConfigOp(op="policy_append", doc="persona", text="x"))
    assert ps.read("loot") == policy_mod.DEFAULTS["loot"], "describe writes nothing"


def test_policy_remove_offline_is_a_draft_and_compile_needs_the_bot(reg):
    ps = PolicyStore(reg.store, reg.key)
    before = ps.read("loot")
    assert configops.apply(reg, ConfigOp(op="policy", doc="loot", field="remove", text="Bigger upgrades first."), "Officer", False, ps) == "loot policy saved as a draft (compile pending)"
    assert "Bigger upgrades" not in ps.read("loot") and policy_mod.pending(ps, "loot")["previous"] == before
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="policy", doc="loot", field="compile"), "Officer", False, ps)


def test_policy_replace_compiles_and_the_view_confirms(reg):
    ps, prov = PolicyStore(reg.store, reg.key), FakeProvider()
    bot = Bot(prov)
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="policy", doc="loot", field="replace", text="# Loot\n\n1. Mainspec first.\n2. Tanks first on shields."), "Officer", False, ps, bot=bot))
    assert line == "loot policy saved as a draft — my reading: 2 item(s), 1 question(s), waiting for Confirm or Discard"
    assert prov.calls == 1 and policy_mod.pending(ps, "loot")["compiled"] is not None
    assert ps.compiled("loot") is None, "nothing is live before Confirm"
    confirmed = asyncio.run(asyncio.to_thread(policy_mod.confirm_pending, ps, "loot", "Officer"))
    assert confirmed == "loot policy is live with this reading (2 item(s))" and ps.compiled("loot")["summary"] == "Mainspec first."


def test_policy_persona_is_live_without_compiling(reg):
    ps, prov = PolicyStore(reg.store, reg.key), FakeProvider()
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="policy", doc="persona", field="replace", text="Be terse."), "Officer", False, ps, bot=Bot(prov)))
    assert line == "persona saved — live as written" and prov.calls == 0 and ps.read("persona") == "Be terse.\n"
    assert policy_mod.pending(ps, "persona") is None


def test_policy_append_without_an_llm_still_saves(reg):
    ps = PolicyStore(reg.store, reg.key)
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="policy_append", doc="comp", text="keep Ash with Bright"), "Officer", False, ps, bot=Bot(None)))
    assert line.startswith("comp policy saved as a draft") and "no LLM to compile" in line
    assert ps.read("comp").rstrip().endswith("- keep Ash with Bright")


# ---- the Discord side: Apply shows the reading with Confirm / Discard; Discard puts the old text back

class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kw):
        self.log.append(("send", content, kw))

    async def edit_message(self, content=None, **kw):
        self.log.append(("edit_message", content, kw))

    async def defer(self, **kw):
        self.log.append(("defer", None, kw))


def owner_interaction(log, bot):
    async def plain_identity(reg, user):
        return True, []

    async def followup(content=None, **kw):
        log.append(("followup", content, kw))

    async def edit_original_response(content=None, **kw):
        log.append(("edit", content, kw))

    bot.plain_identity, bot.raids = plain_identity, None
    return types.SimpleNamespace(user=types.SimpleNamespace(id=OWNER, display_name="Owner"), client=bot, response=Resp(log),
                                 followup=types.SimpleNamespace(send=followup), edit_original_response=edit_original_response)


def test_apply_then_discard_restores_the_previous_text(reg):
    ps, feed, log = PolicyStore(reg.store, reg.key), Feed(), []
    before = ps.read("loot")
    bot = Bot(FakeProvider())

    async def go():
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="policy_append", doc="loot", text="Tanks first on shields.")])
        view = discord_policy.ConfigConfirmView(reg, req, ps, feed, OWNER, "Owner", OPS_CH)
        await view.apply.callback(owner_interaction(log, bot))
        assert [k for k, _, _ in log] == ["edit_message", "edit", "followup"]
        assert "my reading: 2 item(s)" in log[1][1]
        kw = log[2][2]
        assert kw["ephemeral"] and kw["embed"].title == "My reading of the loot policy" and "R1" in kw["embed"].description
        compile_view = kw["view"]
        assert isinstance(compile_view, discord_policy.CompileConfirmView) and compile_view.previous == before
        await compile_view.discard.callback(owner_interaction(log, bot))
        assert ps.read("loot") == before and policy_mod.pending(ps, "loot") is None and ps.compiled("loot") is None
        assert log[-1][0] == "edit_message" and "discarded" in log[-1][1]

    asyncio.run(go())


def test_apply_then_confirm_makes_the_reading_live(reg):
    ps, feed, log = PolicyStore(reg.store, reg.key), Feed(), []
    bot = Bot(FakeProvider())

    async def go():
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="policy", doc="loot", field="compile")])
        view = discord_policy.ConfigConfirmView(reg, req, ps, feed, OWNER, "Owner", OPS_CH)
        await view.apply.callback(owner_interaction(log, bot))
        assert log[1][1].startswith("✅ loot policy: compiling the current text — my reading: 2 item(s)")
        await log[2][2]["view"].confirm.callback(owner_interaction(log, bot))
        assert ps.compiled("loot")["summary"] == "Mainspec first." and policy_mod.pending(ps, "loot") is None
        assert "confirmed the compiled loot policy (2 items)" in feed.lines[-1][1]

    asyncio.run(go())
