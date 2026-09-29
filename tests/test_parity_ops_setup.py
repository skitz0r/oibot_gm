"""Parity B2, batch 7 and the readiness read: the owner's setup ops over plain text — plain-text permissions (the plain
op, owner only whatever the policy says), the recruitment extras (member roles, editing and reordering questions,
creating the invite), clearing a channel through the Config page's verb, raid/schedule settings saved together
(coalesce) with the cadence presets and run previews in the diff, the timezone preview, and the test bench's tempos,
answer mixes and 'seed all'; then GET /api/readiness and MCP raid_readiness. No LLM, no Discord (a recording bot)."""
from __future__ import annotations

import asyncio
import types

import httpx
import pytest
from conftest import OWNER, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, bearer
from test_parity_verbs import Bot

from oibot_gm import configops, discord_policy, presets
from oibot_gm import mcp_server as ms
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.registry import PLAIN_WEB, RegistryError
from oibot_gm.web import app as web

OPS_CH, GENERAL_CH = 900_001, 900_003
RAID_LEAD = 300_000_000_000_000_007


def run(coro):
    return asyncio.run(coro)


class SetupBot(Bot):
    """Bot plus the SetupMixin verbs the setup ops reach (recorded, no Discord)."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.set_channels, self.invites = [], []

    async def plain_identity(self, reg, user):
        return user.id == OWNER, []

    async def set_channel(self, reg, kind, channel_id, by):
        self.set_channels.append((kind, channel_id))
        setattr(reg.config, f"{kind}_channel_id", channel_id)
        reg.save_config(f"{kind} channel (by {by})")
        return f"{kind} channel " + ("cleared" if channel_id is None else f"→ #{channel_id}")

    async def create_invite(self, reg, channel_id):
        self.invites.append(channel_id)
        return "https://discord.gg/abc123"


def owner_apply(reg, op, bot=None, **kw):
    return run(configops.apply_async(reg, op, "Owner", True, bot=bot, by_id=OWNER, **kw))


# ---------------------------------------------------------------- plain: the plain-text permissions

def test_plain_op_edits_groups_channels_dm_and_default(reg):
    reg.role_names[RAID_LEAD] = "Raid Lead"
    op = ConfigOp(op="plain", target="runs", value="officers, @Raid Lead")
    assert configops.describe(reg, op) == "plain text · Runs: officers → officers or @Raid Lead"
    owner_apply(reg, op)
    assert reg.plain_who("runs") == ["officers", f"role:{RAID_LEAD}"]
    owner_apply(reg, ConfigOp(op="plain", target="Runs", value="default"))
    assert reg.plain_who("runs") == ["officers"] and "runs" not in reg.config.plain.groups, "back to the default: nothing stored"
    assert "don't know “@Nobody”" in configops.describe(reg, ConfigOp(op="plain", target="runs", value="@Nobody"))
    owner_apply(reg, ConfigOp(op="plain", target=f"<#{GENERAL_CH}>", value="answer"))
    assert reg.plain_mode(GENERAL_CH) == "answer"
    assert configops.describe(reg, ConfigOp(op="plain", target=f"<#{GENERAL_CH}>", value="clear")) == f"plain text in <#{GENERAL_CH}>: answer → self (back to the default)"
    owner_apply(reg, ConfigOp(op="plain", target=f"<#{GENERAL_CH}>", value="clear"))
    assert str(GENERAL_CH) not in reg.config.plain.channels
    owner_apply(reg, ConfigOp(op="plain", target="dm", value="answer"))
    owner_apply(reg, ConfigOp(op="plain", target="default", value="ignore"))
    assert (reg.config.plain.dm, reg.config.plain.default) == ("answer", "ignore")
    with pytest.raises(RegistryError, match="a mode is one of"):
        configops.apply(reg, ConfigOp(op="plain", target="dm", value="loud"), "Owner", True)
    with pytest.raises(RegistryError, match="needs the owner"):
        configops.apply(reg, ConfigOp(op="plain", target="dm", value="self"), "Officer", False)
    assert "plain" in configops.OWNER_OPS and configops.OP_GROUP["plain"] == "setup"


def test_plain_describe_warns_when_it_takes_act_from_this_channel(reg):
    reg.config.ops_channel_id = OPS_CH
    stop = ConfigOp(op="plain", target=f"<#{OPS_CH}>", value="answer")
    assert "plain text would stop acting in this channel" in configops.describe(reg, stop, OPS_CH)
    assert "⚠" not in configops.describe(reg, stop, GENERAL_CH), "elsewhere it's no warning"
    assert "⚠" not in configops.describe(reg, stop), "unknown channel: no guess"
    mute = ConfigOp(op="plain", target="dm", value="ignore")
    assert "ignore @mentions, yours too" in configops.describe(reg, mute, None)


def test_plain_is_owner_only_even_when_setup_is_opened(reg):
    reg.set_plain_policy(reg.merge_plain_policy({"groups": {"setup": ["officers"]}}), "t")
    officer = reg.test_members()[0].discord_id
    allowed, refused = configops.authorize(reg, [ConfigOp(op="plain", target="runs", value="everyone"), ConfigOp(op="set", path="about", value="hi")],
                                           officer, officer=True, channel=PLAIN_WEB)
    assert [o.op for o in allowed] == ["set"] and refused == [configops.HARD_OWNER_OPS["plain"]]


def test_parser_context_shows_the_policy_to_officers_only(reg):
    text = configops.current_config_text(reg, officer=True)
    assert "## Plain-text permissions" in text and "- runs (Runs): officers" in text and "- DMs: self" in text
    assert "## Plain-text permissions" not in configops.current_config_text(reg, officer=False)


# ---------------------------------------------------------------- recruit: member roles, questions, the invite

def test_recruit_member_roles(reg, rs):
    reg.role_names[RAID_LEAD] = "Raid Lead"
    op = ConfigOp(op="recruit", field="member_role_add", value="@Raid Lead")
    assert configops.describe(reg, op) == "member roles (register without applying): none → @Raid Lead"
    owner_apply(reg, op, SetupBot(reg, rs))
    assert reg.config.member_role_ids == [RAID_LEAD]
    assert "is already a member role" in configops.describe(reg, op)
    owner_apply(reg, ConfigOp(op="recruit", field="member_role_remove", value=f"<@&{RAID_LEAD}>"))
    assert reg.config.member_role_ids == []
    assert "no role “Ghosts”" in configops.describe(reg, ConfigOp(op="recruit", field="member_role_add", value="Ghosts"))


def test_recruit_question_edit_and_move_keep_ids(reg):
    for label in ("Your main?", "Logs link", "Why us?"):
        owner_apply(reg, ConfigOp(op="recruit", field="question_add", value=label))
    ids = [q.id for q in reg.config.recruitment.questions]
    edit = ConfigOp(op="recruit", field="question_edit", target="2", value="Warcraft Logs link", reason="a URL", text="required")
    assert configops.describe(reg, edit) == "question 2: “Logs link” → “Warcraft Logs link”, hint — → a URL, required"
    owner_apply(reg, edit)
    q = reg.config.recruitment.questions[1]
    assert (q.id, q.label, q.hint, q.required) == (ids[1], "Warcraft Logs link", "a URL", True)
    owner_apply(reg, ConfigOp(op="recruit", field="question_edit", target="Warcraft Logs link", reason="none"))
    assert reg.config.recruitment.questions[1].hint is None and reg.config.recruitment.questions[1].label == "Warcraft Logs link"
    move = ConfigOp(op="recruit", field="question_move", target="Why us?", value="top")
    assert configops.describe(reg, move) == "move question “Why us?”: 3 → 1"
    owner_apply(reg, move)
    assert [q.id for q in reg.config.recruitment.questions] == [ids[2], ids[0], ids[1]]
    owner_apply(reg, ConfigOp(op="recruit", field="question_move", target="1", value="down"))
    assert [q.id for q in reg.config.recruitment.questions] == [ids[0], ids[2], ids[1]]
    assert "no question “9”" in configops.describe(reg, ConfigOp(op="recruit", field="question_move", target="9", value="up"))
    assert "already question 1" in configops.describe(reg, ConfigOp(op="recruit", field="question_move", target="1", value="up"))


def test_recruit_invite_create_goes_through_the_bot(reg, rs):
    bot = SetupBot(reg, rs)
    op = ConfigOp(op="recruit", field="invite_create", value="<#555>")
    assert "a new permanent invite to <#555>" in configops.describe(reg, op)
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, op, "Owner", True)
    out = owner_apply(reg, op, bot)
    assert out.endswith("https://discord.gg/abc123") and bot.invites == [555]
    assert reg.config.invite_url == "https://discord.gg/abc123" and bot.card_refreshes == 1


# ---------------------------------------------------------------- channels: clear, through the Config page's verb

def test_channel_clear(reg, rs):
    reg.config.absences_channel_id = 42
    op = ConfigOp(op="set", path="absences_channel", value="none")
    assert configops.describe(reg, op) == "absences_channel: <#42> → not set"
    bot = SetupBot(reg, rs)
    assert owner_apply(reg, op, bot) == "absences channel cleared" and bot.set_channels == [("absences", None)]
    owner_apply(reg, ConfigOp(op="set", path="signup_channel", value="<#77>"), bot)
    assert bot.set_channels[-1] == ("signup", 77), "every channel kind goes through set_channel"
    reg.config.news_channel_id = 9
    assert configops.apply(reg, ConfigOp(op="set", path="news_channel", value="clear"), "Owner", True) == "news_channel cleared"
    assert reg.config.news_channel_id is None


# ---------------------------------------------------------------- raids: presets, previews, coalesce

def test_cadence_preset(reg):
    op = ConfigOp(op="raid_set", target="barrow_deeps", field="cadence", value="short")
    d = configops.describe(reg, op)
    assert d.startswith("raid barrow_deeps cadence: opens 48h") and d.endswith("→ short (opens 48h, nudge 36h, lock 24h, confirm by 12h before)")
    configops.apply(reg, op, "Owner", True)
    rd = reg.raid_def("barrow_deeps")
    assert {f: float(rd[f]) for f in presets.CADENCE_FIELDS} == {f: float(v) for f, v in presets.preset_fields("short").items()}
    assert "cadence is one of" in configops.describe(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="cadence", value="lazy"))


def test_coalesce_saves_settings_that_only_work_together(reg):
    ops = [ConfigOp(op="raid_set", target="barrow_deeps", field="lock_hours_before", value="60"),
           ConfigOp(op="raid_set", target="barrow_deeps", field="signup_lead_hours", value="100"),
           ConfigOp(op="dm", value="off"),
           ConfigOp(op="raid_set", target="barrow_deeps", field="notes", value="bring pots")]
    with pytest.raises(RegistryError, match="signups must open before they lock"):
        configops.apply(reg, ops[0], "Owner", True)  # alone, the first can't apply
    merged = configops.coalesce(ops)
    assert [type(o).__name__ for o in merged] == ["MergedOp", "ConfigOp", "ConfigOp"], "only consecutive ops on one raid merge"
    assert configops.describe(reg, merged[0]).endswith("(saved together)")
    configops.apply(reg, merged[0], "Owner", True)
    rd = reg.raid_def("barrow_deeps")
    assert (rd["lock_hours_before"], rd["signup_lead_hours"]) == (60, 100)
    sched = configops.coalesce([ConfigOp(op="schedule_set", target="barrow_deeps", field="alt.name", value="Alt run"),
                                ConfigOp(op="schedule_set", target="barrow_deeps", field="alt.slots", value="Sat 20:00")])
    assert len(sched) == 1
    configops.apply(reg, sched[0], "Owner", True)  # a new schedule's name before its run times: fine as one write
    assert next(s for s in reg.schedules("barrow_deeps") if s["id"] == "alt")["name"] == "Alt run"


def test_confirm_view_coalesces(reg, rs):
    bot = SetupBot(reg, rs)
    ops = [ConfigOp(op="raid_set", target="barrow_deeps", field="lock_hours_before", value="60"),
           ConfigOp(op="raid_set", target="barrow_deeps", field="signup_lead_hours", value="100")]
    log = []

    async def edit(content=None, **kw):
        log.append(content)

    async def nothing(*a, **kw):
        pass

    it = types.SimpleNamespace(user=types.SimpleNamespace(id=OWNER, display_name="Owner"), client=bot, response=types.SimpleNamespace(defer=nothing),
                               edit_original_response=edit, followup=types.SimpleNamespace(send=nothing))
    view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=ops), None, bot.ops, OWNER, "Owner", OPS_CH)
    run(view.apply.callback(it))
    assert log[0].startswith("✅") and "⛔" not in log[0], log[0]
    assert reg.raid_def("barrow_deeps")["lock_hours_before"] == 60


def test_run_time_changes_preview_the_next_runs(reg):
    reg.set_raid_override("barrow_deeps", "first_open", "2026-01-01T00:00", "t")
    d = configops.describe(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="slots", value="Thu 20:00"))
    assert "→ Thu 8:00 PM — after it: Barrow Deeps: next Thu " in d and "20:00" not in d
    bad = configops.describe(reg, ConfigOp(op="schedule_set", target="barrow_deeps", field="x.lockout", value="9 at 20:00"))
    assert "refused" in bad or "day" in bad
    reg.set_raid_override("barrow_deeps", "slots", "Tue 19:30", "t")
    tz = configops.describe(reg, ConfigOp(op="set", path="timezone", value="America/New_York"))
    assert tz.startswith("timezone: America/Los_Angeles → America/New_York") and "on today's clock" in tz
    assert "refused" in configops.describe(reg, ConfigOp(op="set", path="timezone", value="Mars/Olympus"))


# ---------------------------------------------------------------- test bench: tempo, answer mix, seed all

def test_test_run_tempo_and_dm(reg, rs):
    bot = SetupBot(reg, rs)
    d = configops.describe(reg, ConfigOp(op="test", value="run barrow_deeps quick dm"))
    assert "starts in 20 min, nudge 16, lock 12, confirm 8 min before" in d and "every puppet is DMed" in d
    out = owner_apply(reg, ConfigOp(op="test", value="run barrow_deeps quick dm"), bot)
    assert "(quick)" in out and "every puppet DMed" in out
    ev = next(e for e in rs.live() if (reg.config.roster(e.team) or {}).get("test"))
    t = reg.config.roster(ev.team)
    assert t["test_by"] == OWNER and t.get("open_dm") is True
    minutes = (ev.start - reg.now_local()).total_seconds() / 60
    assert 18 <= minutes <= 21
    assert "test run is 'run <raid>" in configops.describe(reg, ConfigOp(op="test", value="run barrow_deeps turbo"))


def test_test_answer_mix_and_seed_all(reg, rs):
    bot = SetupBot(reg, rs)
    ev = open_test_run(reg, rs, test_by=OWNER)
    d = configops.describe(reg, ConfigOp(op="test", value="answer join 3 bench 2 out 1"))
    assert d.startswith(f"test bench: on {ev.key}, 3 Join, 2 Bench, 1 No thanks from the 16 puppet(s)")
    out = owner_apply(reg, ConfigOp(op="test", value=f"answer {ev.key} join 3 bench 2 out 1"), bot)
    assert out.startswith(f"test bench on {ev.key}: ") and out.count(";") == 5
    assert (len(ev.by_status("in")), len(ev.by_status("sub")), len(ev.by_status("out"))) == (3, 2, 1)
    assert "say how many" in configops.describe(reg, ConfigOp(op="test", value="answer"))
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="test", value="answer join 1"), "Officer", False)
    everyone = len(reg.test_roster())
    assert f"seed {everyone} puppet member(s) (now 16 of 80" in configops.describe(reg, ConfigOp(op="test", value="seed all"))
    owner_apply(reg, ConfigOp(op="test", value="seed all"), bot)
    assert len(reg.test_members()) == everyone


# ---------------------------------------------------------------- readiness: the route and the MCP tool

@pytest.fixture
def c(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(Bot(reg, rs)))


def test_readiness_route(c, reg):
    r = c.get("/api/readiness", headers=bearer())
    assert r.status_code == 200, r.text
    rows = r.json()["raids"]
    assert [x["raid"] for x in rows] == list(reg.profile.raids)
    one = c.get("/api/readiness", params={"raid": "barrow_deeps"}, headers=bearer()).json()["raids"]
    assert len(one) == 1 and one[0]["raid"] == "barrow_deeps" and {"headcount", "roles", "comp", "short", "over", "missing_buffs", "plan"} <= set(one[0])
    assert c.get("/api/readiness", params={"raid": "nowhere"}, headers=bearer()).status_code == 404
    assert c.get("/api/readiness").status_code == 401


def test_readiness_route_is_officers_only(reg, rs, monkeypatch):
    """A signed-in member gets 403 (the pool plan is officer data); a signed-in officer gets the rows."""
    from test_recruitment_web import MEMBER_UID, OFFICER_UID, SECRET, RecruitBot, as_user
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    client = TestClient(web.create_app(RecruitBot(reg, rs)), follow_redirects=False)
    as_user(client, MEMBER_UID)
    r = client.get("/api/readiness")
    assert r.status_code == 403, r.text
    as_user(client, OFFICER_UID)
    r = client.get("/api/readiness", params={"raid": "barrow_deeps"})
    assert r.status_code == 200 and [x["raid"] for x in r.json()["raids"]] == ["barrow_deeps"]


def test_raid_readiness_tool_hits_the_route(c):
    def handler(request: httpx.Request) -> httpx.Response:
        r = c.request(request.method, request.url.path, params=dict(request.url.params), headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        out = ms.raid_readiness("barrow_deeps")
        assert [x["raid"] for x in out["raids"]] == ["barrow_deeps"]
        assert len(ms.raid_readiness()["raids"]) >= 1
    finally:
        ms.use(None)


def test_new_ops_are_documented():
    text = configops.SCHEMA_TEXT
    for needle in ("plain:", "question_edit", "question_move", "member_role_add", "invite_create", "cadence", "seed all", "answer [<run key>]", '"none" clears it'):
        assert needle in text, needle
    assert "handing the guild to a new owner (slash only, by design" in text and "the loot session itself" in text
    assert len(ConfigOp.model_fields) <= 13
