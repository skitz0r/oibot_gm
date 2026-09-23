"""Plain-text permissions (design §5.25): every op in one capability group, the self-service tier decided in code
(configops.bind_self), the one check (Registry.may_plain) across channels × people × groups, the owner's policy on
the web (officers read, the owner writes) and MCP, confirm_for through the buttons' own verb, and the router in
discord_bot (intent first, one model call for a plain question). No LLM and no Discord anywhere."""
from __future__ import annotations

import asyncio
import contextlib
import time
import types

import pytest
from conftest import OFFICER_ROLE, OWNER, join, lock_with_board, open_test_run

from oibot_gm import configops
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.discord_raid import RaidMixin
from oibot_gm.registry import PLAIN_GROUPS, PLAIN_WEB, RegistryError

OPS_CH, ANALYTICS_CH, GENERAL_CH, RAIDERS_CH = 900_001, 900_002, 900_003, 900_004
RAID_LEAD = 300_000_000_000_000_007  # a Discord role the owner can grant groups to


@pytest.fixture
def g(reg):
    """The fixture guild with an ops and an analytics channel; two puppets: `me` (a registered member) and `other`."""
    reg.config.ops_channel_id, reg.config.analytics_channel_id = OPS_CH, ANALYTICS_CH
    reg.save_config("channels (test)")
    me, other = reg.test_members()[:2]
    return types.SimpleNamespace(reg=reg, me=me, other=other)


def check(reg, ops, author, *, officer=False, roles=(), channel=GENERAL_CH):
    return configops.authorize(reg, ops, author, officer=officer, role_ids=roles, channel=channel)


def one(reg, op: ConfigOp, author, **kw) -> str | None:
    """None when the op is allowed, else the refusal line."""
    allowed, refused = check(reg, [op], author, **kw)
    return None if allowed else refused[0]


# ---- every op is in exactly one group

def test_every_op_is_mapped_to_one_group():
    listed = set(ConfigOp.model_fields["op"].description.split(": ", 1)[1].split(", "))
    assert listed == set(configops.OP_GROUP), f"unmapped: {listed - set(configops.OP_GROUP)}, stale: {set(configops.OP_GROUP) - listed}"
    assert set(configops.OP_GROUP.values()) <= set(PLAIN_GROUPS) and "self" not in configops.OP_GROUP.values()
    assert configops.SELF_OPS <= set(configops.OP_GROUP)
    assert set(PLAIN_GROUPS) == {"self", "members", "runs", "comp", "raids", "setup", "test"}
    assert all(example for _, _, example in PLAIN_GROUPS.values())


# ---- the routing matrix with the defaults

SAMPLE = {  # one op per group (the self one has an empty member: the requester)
    "self": lambda g: ConfigOp(op="absence", start="2099-11-01", end="2099-11-03"),
    "members": lambda g: ConfigOp(op="absence", member=g.other.display_name, start="2099-11-01"),
    "runs": lambda g: ConfigOp(op="run_lock", target="barrow_deeps"),
    "comp": lambda g: ConfigOp(op="comp_target", target="barrow_deeps", field="healer", value="3-4"),
    "raids": lambda g: ConfigOp(op="raid_set", target="barrow_deeps", field="notes", value="x"),
    "setup": lambda g: ConfigOp(op="set", path="timezone", value="Europe/London"),
    "test": lambda g: ConfigOp(op="test", value="seed 3"),
}


def who_id(g, who):
    return {"owner": OWNER, "officer": g.me.discord_id, "member": g.me.discord_id, "stranger": 424242}[who]


# expected allowed groups per (who, channel) under the default policy
DEFAULTS = {
    ("owner", "ops"): set(SAMPLE), ("owner", "general"): set(SAMPLE), ("owner", "dm"): set(SAMPLE), ("owner", "web"): set(SAMPLE),
    ("officer", "ops"): {"self", "members", "runs", "comp", "test"}, ("officer", "analytics"): {"self", "members", "runs", "comp", "test"},
    ("officer", "general"): {"self"}, ("officer", "dm"): {"self"}, ("officer", "web"): {"self", "members", "runs", "comp", "test"},
    ("member", "ops"): {"self"}, ("member", "general"): {"self"}, ("member", "dm"): {"self"},
    ("stranger", "ops"): set(), ("stranger", "general"): set(), ("stranger", "dm"): set(),
}
CHANNEL = {"ops": OPS_CH, "analytics": ANALYTICS_CH, "general": GENERAL_CH, "dm": None, "web": PLAIN_WEB}


@pytest.mark.parametrize("who,where", list(DEFAULTS))
def test_default_matrix(g, who, where):
    got = {grp for grp, make in SAMPLE.items() if one(g.reg, make(g), who_id(g, who), officer=who in ("owner", "officer"), channel=CHANNEL[where]) is None}
    assert got == DEFAULTS[(who, where)]


def test_refusals_say_who_and_where(g):
    member = g.me.discord_id
    assert one(g.reg, SAMPLE["members"](g), member, channel=OPS_CH) == "Only officers can change someone else's absence."
    assert one(g.reg, SAMPLE["runs"](g), member, officer=True, channel=GENERAL_CH) == f"Plain text can't change runs in <#{GENERAL_CH}> — use <#{OPS_CH}> or <#{ANALYTICS_CH}>."
    assert one(g.reg, SAMPLE["raids"](g), member, officer=True, channel=OPS_CH) == "Only the owner can change raids & auras."
    assert "in DMs" in one(g.reg, SAMPLE["runs"](g), member, officer=True, channel=None)
    assert one(g.reg, SAMPLE["self"](g), 424242, channel=GENERAL_CH) == "Only registered members can change your absences — /register first."


def test_custom_policy_roles_and_channels(g):
    reg, member = g.reg, g.me.discord_id
    reg.set_plain_policy({"groups": {"runs": ["officers", f"role:{RAID_LEAD}"], "self": ["officers"]},
                          "channels": {str(RAIDERS_CH): "act", str(GENERAL_CH): "answer", str(OPS_CH): "ignore"}, "dm": "answer"}, "owner")
    # a Raid Lead acts on runs in #raiders, not in #general (answer only), not in DMs
    assert one(reg, SAMPLE["runs"](g), member, roles=[RAID_LEAD], channel=RAIDERS_CH) is None
    assert one(reg, SAMPLE["runs"](g), member, roles=[], channel=RAIDERS_CH) == f"Only officers or @role {RAID_LEAD} can change runs."
    assert "only answers questions" in one(reg, SAMPLE["runs"](g), member, roles=[RAID_LEAD], channel=GENERAL_CH)
    assert "only answers questions" in one(reg, SAMPLE["self"](g), member, officer=True, channel=None)
    # the ops channel was switched off: even officers are refused there; the analytics channel still acts by default
    assert "only answers questions" in one(reg, SAMPLE["runs"](g), member, officer=True, channel=OPS_CH)
    assert one(reg, SAMPLE["runs"](g), member, officer=True, channel=ANALYTICS_CH) is None
    assert reg.plain_act_channels() == [str(RAIDERS_CH), str(ANALYTICS_CH)]
    # self closed to members: a member can't touch their own absence; an officer still can (members group covers it)
    assert one(reg, SAMPLE["self"](g), member, channel=RAIDERS_CH) == "Only officers can change your absences."
    assert one(reg, SAMPLE["self"](g), member, officer=True, channel=RAIDERS_CH) is None
    # the owner can always do everything, even where plain text only answers
    assert one(reg, SAMPLE["setup"](g), OWNER, channel=GENERAL_CH) is None
    # "everyone" opens a group to unregistered people too; [] = the owner only
    reg.set_plain_policy({"groups": {"test": ["everyone"], "comp": []}, "channels": {}}, "owner")
    assert one(reg, SAMPLE["test"](g), 424242, channel=OPS_CH) is None
    assert one(reg, SAMPLE["comp"](g), member, officer=True, channel=OPS_CH) == "Only the owner can change comp & policy."


def test_policy_is_validated_and_stored(g):
    reg = g.reg
    for bad in ({"groups": {"nope": ["officers"]}}, {"groups": {"runs": ["admins"]}}, {"dm": "loud"}, {"channels": {"general": "act"}}, {"channels": {"1": "sometimes"}}):
        with pytest.raises((RegistryError, ValueError)):
            reg.set_plain_policy(bad, "owner")
    line = reg.set_plain_policy({"groups": {"runs": ["officers", f"role:{RAID_LEAD}"], "members": ["officers"]}, "dm": "act"}, "owner")
    assert "Runs →" in line and "DMs self → act" in line and "Other people's" not in line  # members left at its default isn't a change
    assert reg.config.plain.groups == {"runs": ["officers", f"role:{RAID_LEAD}"]}  # defaults aren't stored
    fresh = type(reg)(reg.store, reg.key, reg.profile)
    assert fresh.plain_who("runs") == ["officers", f"role:{RAID_LEAD}"] and fresh.plain_mode(None) == "act"


# ---- the self-service safety property

def test_empty_member_is_the_requester_never_anyone_else(g):
    reg, me = g.reg, g.me
    for op in (ConfigOp(op="absence", start="2099-11-01"), ConfigOp(op="dm", value="off"), ConfigOp(op="absence_clear"),
               ConfigOp(op="character", character=me.main.name, field="spec", value=me.main.spec), ConfigOp(op="set_main", character=me.main.name)):
        allowed, refused = check(reg, [op], me.discord_id)
        assert allowed == [op] and not refused, (op, refused)
        assert op.member == f"<@{me.discord_id}>"
    # an unregistered person's empty member is still themselves: refused as self, not reaching anyone else
    op = ConfigOp(op="dm", value="off")
    assert one(reg, op, 424242) and op.member == "<@424242>"


def test_naming_yourself_any_way_is_self(g):
    reg, me = g.reg, g.me
    for ref in (me.display_name, me.display_name.upper(), f"<@{me.discord_id}>", f"<@!{me.discord_id}>", me.main.name):
        op = ConfigOp(op="absence", member=ref, start="2099-11-01")
        assert one(reg, op, me.discord_id) is None, ref
        assert op.member == f"<@{me.discord_id}>"


def test_someone_else_is_refused_for_a_member(g):
    reg, me, other = g.reg, g.me, g.other
    for ref in (other.display_name, f"<@{other.discord_id}>", other.main.name, "<@&123>", "<@999999>"):
        op = ConfigOp(op="absence", member=ref, start="2099-11-01")
        assert one(reg, op, me.discord_id) == "Only officers can change someone else's absence.", ref
        assert op.member == ref  # never rewritten to anyone
    # an officer may
    assert one(reg, ConfigOp(op="absence", member=other.display_name, start="2099-11-01"), me.discord_id, officer=True, channel=OPS_CH) is None


def test_display_name_collisions_are_not_self(g):
    reg, me, other = g.reg, g.me, g.other
    other.display_name = me.display_name  # two members called the same
    reg.save(other, "rename (test)")
    op = ConfigOp(op="dm", member=me.display_name, value="off")
    assert one(reg, op, me.discord_id) == "Only officers can change someone else's DM setting."
    # my display name is someone else's character name: ambiguous, so not self either
    reg.members[other.discord_id].display_name = "Otherperson"
    other.main.name = me.display_name
    reg.save(other, "char named like me (test)")
    assert one(reg, ConfigOp(op="dm", member=me.display_name, value="off"), me.discord_id) is not None
    # a mention stays unambiguous
    assert one(reg, ConfigOp(op="dm", member=f"<@{me.discord_id}>", value="off"), me.discord_id) is None


def test_character_of_someone_else_is_refused(g):
    reg, me, other = g.reg, g.me, g.other
    theirs = other.main.name
    for op in (ConfigOp(op="character", character=theirs, field="spec", value="Holy"), ConfigOp(op="set_main", character=theirs),
               ConfigOp(op="character", character=theirs, field="retire"), ConfigOp(op="run_answer", target="x", value="join", character=theirs)):
        assert one(reg, op, me.discord_id) == f"{theirs} isn't one of your characters", op
    # a new character of my own may take any unused name
    assert one(reg, ConfigOp(op="character", character="Brandnew", field="add", value="Priest Holy"), me.discord_id) is None
    # rank is never self-service, not even on my own character
    assert one(reg, ConfigOp(op="character", character=me.main.name, field="rank", value="core"), me.discord_id) == "Only officers can change ranks."
    assert one(reg, ConfigOp(op="rank", character=me.main.name, rank="core"), me.discord_id) == "Only officers can change ranks."


def test_the_rest_of_a_request_still_applies(g, rs):
    reg, me, other = g.reg, g.me, g.other
    ops = [ConfigOp(op="absence", start="2099-11-01", end="2099-11-03"), ConfigOp(op="absence", member=other.display_name, start="2099-11-01"),
           ConfigOp(op="raid_set", target="barrow_deeps", field="notes", value="x")]
    allowed, refused = check(reg, ops, me.discord_id)
    assert allowed == [ops[0]]
    assert refused == ["Only officers can change someone else's absence.", "Only the owner can change raids & auras."]
    line = configops.apply(reg, allowed[0], me.display_name, True)
    assert me.display_name in line and reg.members[me.discord_id].absences[-1].start == "2099-11-01"
    assert not any(a.start == "2099-11-01" for a in reg.members[other.discord_id].absences)


def test_own_answer_on_a_sheet_is_the_members_own(g, rs):
    """run_answer for yourself goes through set_answer with by = your registry name, so it counts as your own press."""
    from test_plain_text import FakeBot

    reg, me = g.reg, g.me
    ev = open_test_run(reg, rs)
    op = ConfigOp(op="run_answer", target=ev.key, value="bench")
    assert one(reg, op, me.discord_id) is None
    bot = FakeBot(rs)
    asyncio.run(configops.apply_async(reg, op, me.display_name, True, bot=bot, by_id=me.discord_id))
    assert bot.calls[0][:5] == ("set_answer", ev.key, me.display_name, "sub", None) and bot.calls[0][5] == me.display_name
    assert ev.signups[str(me.discord_id)].status == "sub"


# ---- confirm_for: the Confirm / Can't make it buttons' verb

class PlacementBot:
    """RaidMixin.answer_placement_for itself (the shared verb), with the Discord ripple recorded."""

    answer_placement_for = RaidMixin.answer_placement_for

    def __init__(self, rs):
        self.calls = []
        self.raids = types.SimpleNamespace(store=lambda reg: rs)

    async def after_placement_answer(self, reg, uid, roster, yes, line):
        self.calls.append(("after_placement_answer", uid, roster, yes, line))


def locked_with_asks(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:5]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    for m in ms:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    return ev, ms


def test_confirm_for_describe_and_apply(g, rs):
    reg = g.reg
    ev, ms = locked_with_asks(reg, rs)
    yes = ConfigOp(op="confirm_for", target=ev.key, member=ms[1].display_name, value="yes")
    assert configops.describe(reg, yes).endswith(f"{ms[1].display_name} → confirmed")
    no = ConfigOp(op="confirm_for", target=ev.key, member=ms[2].display_name, value="no")
    assert "can't make it — seat freed" in configops.describe(reg, no)
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, yes, "Officer", True)
    bot = PlacementBot(rs)
    line = asyncio.run(configops.apply_async(reg, yes, "Officer", True, bot=bot))
    assert line == f"{ms[1].display_name} confirmed {ms[1].main.label} on {ev.team}"
    assert reg.members[ms[1].discord_id].placement_asks[-1]["answer"] == "yes"
    line = asyncio.run(configops.apply_async(reg, no, "Officer", True, bot=bot))
    assert "can't make" in line and reg.members[ms[2].discord_id].placement_asks[-1]["answer"] == "no"
    assert [c[3] for c in bot.calls] == [True, False] and bot.calls[1][1] == ms[2].discord_id
    # answered already, not locked, bad value: refused before Apply
    assert "no confirmation waiting" in configops.describe(reg, yes)
    other = open_test_run(reg, rs, days=6)
    assert "isn't locked yet" in configops.describe(reg, ConfigOp(op="confirm_for", target=other.key, member=ms[3].display_name))
    assert "yes | no" in configops.describe(reg, ConfigOp(op="confirm_for", target=ev.key, member=ms[3].display_name, value="perhaps"))


def test_confirm_for_groups(g, rs):
    reg = g.reg
    ev, ms = locked_with_asks(reg, rs)
    mine = ConfigOp(op="confirm_for", target=ev.key, value="yes")  # "I confirm for tonight"
    assert one(reg, mine, ms[0].discord_id) is None and mine.member == f"<@{ms[0].discord_id}>"
    theirs = ConfigOp(op="confirm_for", target=ev.key, member=ms[1].display_name, value="yes")
    assert one(reg, theirs, ms[0].discord_id) == "Only officers can change someone else's confirmation."
    assert one(reg, theirs, ms[0].discord_id, officer=True, channel=OPS_CH) is None


# ---- the web: officers read, the owner writes, others 403; MCP is the owner

class Guild:
    def __init__(self):
        self.id = 1
        self.roles = [types.SimpleNamespace(id=RAID_LEAD, name="Raid Lead", position=2, is_default=lambda: False, managed=False)]


@pytest.fixture
def web_client(g, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from itsdangerous import URLSafeSerializer
    from test_mcp import TOKEN, FakeBot

    from oibot_gm.web import app as web

    officer_id = g.other.discord_id
    guild = Guild()

    class Bot(FakeBot):
        def get_guild(self, gid):
            return guild

        async def cached_member(self, guild, uid, max_age=300):
            return types.SimpleNamespace(id=uid, display_name=f"u{uid}", roles=[types.SimpleNamespace(id=OFFICER_ROLE)] if uid == officer_id else [])

        def officiates(self, member, guild):
            return member.id in (officer_id, OWNER)

        def guild_channels(self, reg):
            return [{"id": str(OPS_CH), "name": "ops", "category": None}, {"id": str(GENERAL_CH), "name": "general", "category": None}]

    monkeypatch.setenv("OIBOT_WEB_SECRET", "s" * 40)
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    sign = URLSafeSerializer("s" * 40, salt="session")
    bot = Bot(g.reg, rs)
    c = TestClient(web.create_app(bot))

    def as_(uid):
        c.cookies.set(web.COOKIE, sign.dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
        return {"X-Requested-With": "oibot"}

    return types.SimpleNamespace(c=c, as_=as_, officer=officer_id, member=g.me.discord_id, bot=bot, token=TOKEN)


def test_api_officers_read_owner_writes(web_client, g):
    w = web_client
    r = w.c.get("/api/plain-permissions", headers=w.as_(w.officer))
    assert r.status_code == 200, r.text
    d = r.json()
    assert not d["owner"] and [x["id"] for x in d["groups"]] == list(PLAIN_GROUPS) and d["dm"] == "self" and d["default"] == "self"
    assert {x["id"]: x["mode"] for x in d["channels"]} == {str(OPS_CH): "act", str(GENERAL_CH): "self"}
    assert d["guild_roles"] == [{"id": str(RAID_LEAD), "name": "Raid Lead"}]
    assert "confirm_for" in next(x for x in d["groups"] if x["id"] == "runs")["ops"]
    assert w.c.get("/api/plain-permissions", headers=w.as_(w.member)).status_code == 403
    body = {"groups": {"runs": ["officers", f"role:{RAID_LEAD}"]}, "channels": {str(GENERAL_CH): "answer"}, "dm": "act"}
    assert w.c.post("/api/admin/plain-permissions", json=body, headers=w.as_(w.member)).status_code == 403
    assert w.c.post("/api/admin/plain-permissions", json=body, headers=w.as_(w.officer)).status_code == 403
    assert w.c.post("/api/admin/plain-permissions", json=body, headers={"Authorization": f"Bearer {w.token}"}).status_code == 403  # CSRF marker
    r = w.c.post("/api/admin/plain-permissions", json=body, headers={"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"})
    assert r.status_code == 200, r.text
    assert g.reg.plain_who("runs") == ["officers", f"role:{RAID_LEAD}"] and g.reg.plain_mode(GENERAL_CH) == "answer" and g.reg.plain_mode(None) == "act"
    assert "Runs → officers or @Raid Lead" in r.json()["message"]
    assert any("plain-text permissions" in t for _, t in w.bot.ops.lines)
    bad = w.c.post("/api/admin/plain-permissions", json={"groups": {"runs": ["admins"]}}, headers={"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"})
    assert bad.status_code == 400 and "who is" in bad.json()["error"]


def test_web_plain_change_uses_the_same_check(web_client, g, monkeypatch):
    w = web_client
    w.bot.ctx = types.SimpleNamespace(provider=object())
    req = ConfigRequest(kind="change", reply="ok", ops=[ConfigOp(op="raid_set", target="barrow_deeps", field="notes", value="bring flasks"),
                                                         ConfigOp(op="dm", member=g.me.display_name, value="off")])
    monkeypatch.setattr(configops, "parse", lambda provider, reg, text, by=None: req.model_copy(deep=True))
    r = w.c.post("/api/ops/change", json={"text": "x", "apply": True}, headers=w.as_(w.officer))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["refused"] == ["Only the owner can change raids & auras."] and [o["op"] for o in d["ops"]] == ["dm"]
    assert g.reg.members[g.me.discord_id].dm_opt_out and "notes" not in g.reg.config.raids.get("barrow_deeps", {})
    assert w.c.post("/api/ops/change", json={"text": "x"}, headers=w.as_(w.member)).status_code == 403
    # MCP is the owner: everything
    d = w.c.post("/api/ops/change", json={"text": "x", "apply": True}, headers={"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"}).json()
    assert d["refused"] == [] and g.reg.raid_def("barrow_deeps")["notes"] == "bring flasks"


def test_web_placement_routes_share_the_verb(web_client, g, rs):
    w = web_client
    ev, ms = locked_with_asks(g.reg, rs)
    seen = []

    async def after(reg, uid, roster, yes, line):
        seen.append((uid, yes))

    w.bot.answer_placement_for = types.MethodType(RaidMixin.answer_placement_for, w.bot)
    w.bot.after_placement_answer = after
    r = w.c.post("/api/admin/placement", json={"member": ms[0].display_name, "run": ev.key, "answer": "yes"}, headers={"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"})
    assert r.status_code == 200, r.text
    assert seen == [(ms[0].discord_id, True)] and g.reg.members[ms[0].discord_id].placement_asks[-1]["answer"] == "yes"


def test_mcp_tools_call_the_routes():
    import httpx

    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.read()))
        if request.method == "GET":
            return httpx.Response(200, json={"groups": [{"id": "runs", "label": "Runs", "who": ["officers"], "who_text": "officers", "ops": ["run_lock"], "example": "lock it", "default": ["officers"]}],
                                             "channels": [{"id": "1", "name": "ops", "mode": "act"}], "listed": {}, "dm": "self", "default": "self", "guild_roles": []})
        return httpx.Response(200, json={"message": "plain-text permissions: Runs → officers or @Raid Lead"})

    ms.use(ms.Api(url="http://bot", token="a" * 64, transport=httpx.MockTransport(handler)))
    try:
        assert ms.plain_permissions()["groups"][0]["ops"] == ["run_lock"]
        assert ms.set_plain_permissions(groups={"runs": ["officers", "role:5"]}, dm="act").startswith("plain-text permissions")
    finally:
        ms.use(None)
    assert calls[0][:2] == ("GET", "/api/plain-permissions") and calls[1][:2] == ("POST", "/api/admin/plain-permissions")
    assert b'"dm":"act"' in calls[1][2].replace(b" ", b"")


# ---- the router: intent first, one model call for a plain question

class Channel:
    @contextlib.asynccontextmanager
    async def typing(self):
        yield


def router(reg, monkeypatch, help_says, officer=False):
    """OibotGM.plain_text on a stub client: records help calls and change-flow calls."""
    from oibot_gm import discord_bot as db

    seen = {"help": [], "change": [], "replies": []}

    async def fake_change(message, reg_, ps, provider, ops, text, *, bot, channel, req=None):
        seen["change"].append((text, channel, req))

    monkeypatch.setattr(db, "handle_change", fake_change)

    async def help_answer(reg_, uid, off, text, may_act=False):
        seen["help"].append((text, may_act))
        return db.HELP_ACT if (may_act and help_says == "act") else "an answer"

    async def plain_identity(reg_, user):
        return officer or user.id == OWNER, []

    stub = types.SimpleNamespace(ctx=types.SimpleNamespace(provider=object()), policies=types.SimpleNamespace(store=lambda r: None), ops=None,
                                 help_answer=help_answer, plain_identity=plain_identity)

    async def reply(content=None, **kw):
        seen["replies"].append(content)

    def send(author_id, channel, text="hi"):
        msg = types.SimpleNamespace(author=types.SimpleNamespace(id=author_id, display_name="x"), channel=Channel(), reply=reply)
        asyncio.run(db.OibotGM.plain_text(stub, msg, reg, text, channel))

    return seen, send


def test_router_question_costs_one_call(g, monkeypatch):
    seen, send = router(g.reg, monkeypatch, "answer")
    send(g.me.discord_id, GENERAL_CH, "how do I sign up?")
    assert seen["help"] == [("how do I sign up?", True)] and not seen["change"] and seen["replies"] == ["an answer"]


def test_router_change_in_a_self_channel_and_dm(g, monkeypatch):
    seen, send = router(g.reg, monkeypatch, "act")
    send(g.me.discord_id, GENERAL_CH, "I'll be away Nov 1-3")
    send(g.me.discord_id, None, "I'll be away Nov 1-3")
    assert len(seen["help"]) == 2 and [c[1] for c in seen["change"]] == [GENERAL_CH, None] and not seen["replies"]


def test_router_act_channel_parses_first(g, monkeypatch):
    seen, send = router(g.reg, monkeypatch, "answer", officer=True)
    send(g.me.discord_id, OPS_CH, "lock tonight's run")
    assert not seen["help"] and seen["change"] == [("lock tonight's run", OPS_CH, None)]


def test_router_answer_and_ignore_channels(g, monkeypatch):
    g.reg.set_plain_policy({"channels": {str(GENERAL_CH): "answer", str(RAIDERS_CH): "ignore"}}, "owner")
    seen, send = router(g.reg, monkeypatch, "act")
    send(g.me.discord_id, GENERAL_CH, "I'll be away")
    assert seen["help"] == [("I'll be away", False)] and not seen["change"] and seen["replies"] == ["an answer"]
    send(g.me.discord_id, RAIDERS_CH, "anything")
    assert len(seen["help"]) == 1 and len(seen["replies"]) == 1  # ignored: no call, no reply
    send(OWNER, GENERAL_CH, "I'll be away")  # the owner may act anywhere the bot listens
    assert seen["help"][-1] == ("I'll be away", True) and seen["change"][-1][1] == GENERAL_CH


def test_router_unregistered_person_in_a_self_channel_only_gets_answers(g, monkeypatch):
    seen, send = router(g.reg, monkeypatch, "act")
    send(424242, GENERAL_CH, "I'll be away")
    assert seen["help"] == [("I'll be away", False)] and not seen["change"]


# ---- the Discord change flow: refused ops listed, the rest offered; Apply re-checks the presser

class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kw):
        self.log.append(("send", content, kw))

    async def edit_message(self, content=None, **kw):
        self.log.append(("edit", content, kw))


def interaction(uid, name, officer, log):
    async def plain_identity(reg, user):
        return officer or uid == OWNER, []

    async def send(content=None, **kw):
        log.append(("followup", content, kw))

    client = types.SimpleNamespace(plain_identity=plain_identity, raids=None)
    return types.SimpleNamespace(user=types.SimpleNamespace(id=uid, display_name=name), client=client, response=Resp(log), followup=types.SimpleNamespace(send=send))


def test_handle_change_and_apply(g):
    from oibot_gm.discord_policy import handle_change

    reg, me, other = g.reg, g.me, g.other
    ops_feed = []

    class Feed:
        async def emit(self, cfg, level, text, exc=None):
            ops_feed.append(text)

    async def go():
        log = []
        req = ConfigRequest(kind="change", reply="Got it.", ops=[ConfigOp(op="dm", value="off"), ConfigOp(op="dm", member=other.display_name, value="off")])
        it = interaction(me.discord_id, me.display_name, False, log)
        await handle_change(it, reg, None, None, Feed(), "turn my DMs and theirs off", bot=it.client, channel=GENERAL_CH, req=req)
        kind, content, kw = log[-1]
        desc = kw["embed"].description
        assert "DMs on → off" in desc and "⛔ Only officers can change someone else's DM setting." in desc
        view = kw["view"]
        assert [op.member for op in view.req.ops] == [f"<@{me.discord_id}>"]
        # someone else (not an officer) can't press it
        await view.apply.callback(interaction(other.discord_id, other.display_name, False, log))
        assert log[-1][0] == "send" and "Only" in log[-1][1] and not reg.members[me.discord_id].dm_opt_out
        # the requester applies it: their own DMs only
        await view.apply.callback(interaction(me.discord_id, me.display_name, False, log))
        assert log[-1][0] == "edit" and "DMs off" in log[-1][1]
        assert reg.members[me.discord_id].dm_opt_out and not reg.members[other.discord_id].dm_opt_out
        assert ops_feed and ops_feed[-1].startswith(f"{me.display_name} plain-text change in <#{GENERAL_CH}>")
        # everything refused: a refusal line, no Apply button
        log.clear()
        req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="raid_set", target="barrow_deeps", field="notes", value="x")])
        await handle_change(it, reg, None, None, Feed(), "x", bot=it.client, channel=GENERAL_CH, req=req)
        assert log == [("followup", "⛔ Only the owner can change raids & auras.", {"ephemeral": True})]

    asyncio.run(go())
