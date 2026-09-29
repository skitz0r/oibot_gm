"""Stage C D1 fixes (out/parity/stageC_report.md H1, M1–M8): who can read a reply decides how much it carries; a
member can't roster themselves on a locked run by chat; a policy reading goes live only against the text it read;
Apply is single-flight and keeps its board revs honest; lock_run and the web autofill share the board lock; MCP
plain_change applies exactly what it previewed; the raid_set Apply line reads in 12-hour words; team_member is gone.
No Discord, no LLM."""
from __future__ import annotations

import asyncio
import contextlib
import types
from datetime import timedelta

import discord
import pytest
from conftest import OFFICER_ROLE, OWNER

from oibot_gm import configops, discord_policy, help as help_mod
from oibot_gm.configops import ConfigRequest
from oibot_gm.discord_help import HelpMixin, officer_only_channel

OPS_CH, GENERAL_CH, ANALYTICS_CH, ROSTER_CH, APPS_CH = 910_001, 910_002, 910_003, 910_004, 910_005


# ---------------------------------------------------------------- H1: the reply's readers decide the snapshot

@pytest.fixture
def secret(reg):
    """Another member's absence with a reason (officer-only) inside the next week."""
    m = reg.test_members()[3]
    reg.add_absence(m.discord_id, (reg.now_local().date() + timedelta(days=2)).isoformat(), None, "hospital appointment", "t")
    return m


def test_public_officer_snapshot_is_the_member_view(reg, rs, secret):
    q = "who's away next week and why?"
    private = help_mod.guild_state(reg, rs, OWNER, True, q)
    assert "hospital appointment" in private and "officer index:" in private and "### Absences" in private
    public = help_mod.guild_state(reg, rs, OWNER, True, q, public=True)
    for leak in ("hospital appointment", "officer index:", "### Absences", "(officers"):
        assert leak not in public, leak
    assert "## Asker: <@%d> (officer)" % OWNER in public and "## Reply audience: a channel members can read" in public
    # a member's snapshot is unchanged by the flag
    uid = reg.test_members()[0].discord_id
    assert help_mod.guild_state(reg, rs, uid, False, q, public=True) == help_mod.guild_state(reg, rs, uid, False, q)


class HelpBot(HelpMixin):
    def __init__(self, reg, rs):
        self.ctx = types.SimpleNamespace(provider=types.SimpleNamespace(summary=lambda: "LLM spend $0"))
        self.tree = None
        self.raids = types.SimpleNamespace(store=lambda r: rs)
        self.ops = types.SimpleNamespace(recent=[("9:00 PM", "info", "ops secret line")])


def test_help_answer_in_public_uses_the_member_view_and_says_where_the_officer_view_is(reg, rs, secret, monkeypatch):
    reg.config.ops_channel_id = OPS_CH
    seen = []

    def fake_answer(provider, tree, reg_, rs_, uid, officer, question, extras=None, public=False):
        seen.append(help_mod.guild_state(reg_, rs_, uid, officer, question, extras, public=public) + f"\nextras ops: {extras.ops if extras else None}")
        return help_mod.HelpAnswer(answer="Nobody you need to know about.")

    monkeypatch.setattr(help_mod, "answer", fake_answer)
    bot = HelpBot(reg, rs)
    q = "who's away next week and why? any ops errors?"
    text = asyncio.run(bot.help_answer(reg, OWNER, True, q, public=True))
    assert "hospital appointment" not in seen[0] and "ops secret line" not in seen[0]
    assert text.startswith("Nobody you need to know about.") and f"officers can ask in <#{OPS_CH}> or by DM for the officer view" in text
    text = asyncio.run(bot.help_answer(reg, OWNER, True, q))
    assert "hospital appointment" in seen[1] and "ops secret line" in seen[1] and "officer view" not in text


# officer_only_channel / reply_private on stand-in Discord objects

class Perms:
    def __init__(self, view=False, admin=False, manage=False):
        self.view_channel, self.administrator, self.manage_guild = view, admin, manage


class Role:
    def __init__(self, rid, name, *, admin=False, manage=False):
        self.id, self.name, self.permissions = rid, name, Perms(admin=admin, manage=manage)


class Chan:
    def __init__(self, cid, guild, visible_to=(), parent=None, overwrites=None):
        self.id, self.guild, self.parent, self.parent_id = cid, guild, parent, getattr(parent, "id", None)
        self.visible_to, self.overwrites = set(visible_to), overwrites or {}

    def permissions_for(self, role):
        return Perms(view=role.id in self.visible_to)


def guild_with_roles():
    everyone, raider, officer, admin = Role(1, "@everyone"), Role(2, "Raider"), Role(OFFICER_ROLE, "Officer"), Role(4, "Admin", admin=True)
    return types.SimpleNamespace(roles=[everyone, raider, officer, admin]), everyone, raider, officer


def test_reply_private_follows_the_channels_readers(reg):
    cfg = reg.config
    cfg.ops_channel_id, cfg.roster_channel_id, cfg.applications_channel_id, cfg.analytics_channel_id = OPS_CH, ROSTER_CH, APPS_CH, ANALYTICS_CH
    guild, everyone, raider, officer = guild_with_roles()
    bot = HelpBot(reg, None)
    assert bot.reply_private(reg, None), "a DM"
    for cid in (OPS_CH, ROSTER_CH, APPS_CH):
        assert bot.reply_private(reg, Chan(cid, guild, visible_to={1, 2})), cid
    assert bot.reply_private(reg, Chan(99, guild, visible_to={1}, parent=Chan(OPS_CH, guild))), "a thread in #ops"
    assert not bot.reply_private(reg, Chan(GENERAL_CH, guild, visible_to={1, 2, OFFICER_ROLE}))
    assert not bot.reply_private(reg, Chan(ANALYTICS_CH, guild, visible_to={2, OFFICER_ROLE})), "raiders can read analytics"
    assert bot.reply_private(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE})), "analytics made officer-only"
    assert not officer_only_channel(reg, types.SimpleNamespace(guild=None)), "anything unreadable counts as public"


# handle_change on a real Message type: the parser context and the to_help snapshot follow the channel

class Msg(discord.Message):
    def __init__(self, **kw):  # a stand-in: only what handle_change reads
        for k, v in kw.items():
            setattr(self, k, v)


class CapturingProvider:
    def __init__(self, reply):
        self.seen, self.reply = [], reply

    def complete(self, workload, system, user, schema):
        self.seen.append(user)
        return self.reply


def test_handle_change_in_a_public_channel_parses_and_answers_with_the_member_view(reg):
    reg.apply(7_070_000_001, "nyxie", "Nyx", "Priest", "Holy", None, None, None, None, surname="Starfall")
    replies, helped = [], []

    async def plain_identity(reg_, user):
        return True, []

    async def help_answer(reg_, uid, officer, text, may_act=False, public=False):
        helped.append((officer, public))
        return None  # fall through to the parser's own reply

    async def reply(content=None, **kw):
        replies.append(content)

    private = {"yes": False}
    bot = types.SimpleNamespace(plain_identity=plain_identity, help_answer=help_answer, raids=None, reply_private=lambda r, ch: private["yes"])
    prov = CapturingProvider(ConfigRequest(kind="question", reply="Which applicant?", questions=[]))

    def ask():
        msg = Msg(author=types.SimpleNamespace(id=OWNER, display_name="Owner"), guild=object(), channel=object(), reply=reply)
        asyncio.run(discord_policy.handle_change(msg, reg, None, prov, None, "who applied?", bot=bot, channel=GENERAL_CH, to_help=True))

    ask()
    assert "nyxie" not in prov.seen[-1] and "Open applications" not in prov.seen[-1] and "Plain-text permissions" not in prov.seen[-1]
    assert helped[-1] == (True, True)
    private["yes"] = True
    ask()
    assert "nyxie" in prov.seen[-1] and helped[-1] == (True, False)


class Typing:
    @contextlib.asynccontextmanager
    async def typing(self):
        yield


def test_plain_text_mention_by_an_officer_in_a_public_channel_is_answered_publicly(reg):
    from oibot_gm import discord_bot as db

    helped, replies = [], []

    async def help_answer(reg_, uid, officer, text, may_act=False, public=False):
        helped.append((officer, public))
        return "an answer"

    async def plain_identity(reg_, user):
        return True, []

    async def reply(content=None, **kw):
        replies.append(content)

    private = {"yes": False}
    stub = types.SimpleNamespace(ctx=types.SimpleNamespace(provider=object()), policies=types.SimpleNamespace(store=lambda r: None), ops=None,
                                 help_answer=help_answer, plain_identity=plain_identity, reply_private=lambda r, ch: private["yes"] or ch is None)

    def send(guild):
        msg = types.SimpleNamespace(author=types.SimpleNamespace(id=OWNER, display_name="Owner"), channel=Typing(), reply=reply, guild=guild)
        asyncio.run(db.OibotGM.plain_text(stub, msg, reg, "who's away and why?", None if guild is None else GENERAL_CH))

    send(object())
    send(None)  # a DM
    private["yes"] = True
    send(object())  # an officer-only channel
    assert helped == [(True, True), (True, False), (True, False)] and replies == ["an answer"] * 3


# ---------------------------------------------------------------- M1: no self-rostering on a locked run by chat

def test_member_join_after_lock_is_refused_with_the_sheets_words(reg, rs):
    from conftest import join, lock_with_board, open_test_run

    from oibot_gm import raidcycle as rc
    from oibot_gm.configops import ConfigOp

    ms = reg.test_members()
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, ms[:10])
    me = ms[12]

    def check(value, officer=False):
        return configops.authorize(reg, [ConfigOp(op="run_answer", target=ev.key, value=value)], me.discord_id, officer=officer, channel=None, raids=rs)

    assert check("join")[0] and check("bench")[0], "open: a member answers their own sheet"
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:10]])
    for value in ("join", "bench"):
        allowed, refused = check(value)
        assert not allowed and refused == [rc.LOCKED_JOIN], value
    assert check("no thanks")[0], "saying no stays self-service after lock"
    assert check("join", officer=True)[0], "an officer may still seat themselves"
    assert "Can't make it" in rc.LOCKED_JOIN


# ---------------------------------------------------------------- M2: a reading goes live only against the text it read

from oibot_gm import policy as policy_mod  # noqa: E402
from oibot_gm.policy import LootCompiled, LootRule, PolicyPendingError, PolicyStore  # noqa: E402


class Reader:
    """A compile that reads the text it was handed; `during` runs mid-compile (another officer's edit)."""

    def __init__(self, during=None):
        self.during, self.seen = during, []

    def complete(self, workload, system, user, schema):
        self.seen.append(user)
        if self.during:
            self.during()
            self.during = None
        rule = user.split("\n1. ", 1)[1].split("\n")[0] if "\n1. " in user else "?"
        return LootCompiled(rules=[LootRule(id="R1", text=rule, kind="ordering")], summary=rule)


def test_confirm_refuses_a_reading_of_other_text(reg):
    ps = PolicyStore(reg.store, reg.key)
    policy_mod.begin_edit(ps, "loot", "# Loot\n\n1. A rule", "Officer A")
    policy_mod.compile_pending(ps, "loot", Reader())
    ps.store.write_text(ps.path("loot"), "# Loot\n\n1. B rule\n")  # a PR edit: under the pending reading's feet
    with pytest.raises(PolicyPendingError, match="changed since that reading was compiled"):
        policy_mod.confirm_pending(ps, "loot", "Officer A")
    assert ps.compiled("loot") is None
    # the stage C repro: a draft written the Discord way (/gm policy edit) drops the held reading
    policy_mod.begin_edit(ps, "loot", "# Loot\n\n1. A rule", "Officer A")
    policy_mod.compile_pending(ps, "loot", Reader())
    ps.write_draft("loot", "# Loot\n\n1. B rule", "Officer B")
    with pytest.raises(PolicyPendingError, match="compile it first"):
        policy_mod.confirm_pending(ps, "loot", "Officer A")
    policy_mod.compile_pending(ps, "loot", Reader())
    assert policy_mod.confirm_pending(ps, "loot", "Officer A").startswith("loot policy is live")
    assert ps.compiled("loot")["summary"] == "B rule", "the live reading is of the live text"


def test_a_slow_compile_never_lands_on_a_newer_edit(reg):
    ps = PolicyStore(reg.store, reg.key)
    original = ps.read("loot")
    policy_mod.begin_edit(ps, "loot", "# Loot\n\n1. A rule", "Officer A")
    slow = Reader(during=lambda: policy_mod.begin_edit(ps, "loot", "# Loot\n\n1. B rule", "Officer B"))
    with pytest.raises(PolicyPendingError, match="changed while I was reading it"):
        policy_mod.compile_pending(ps, "loot", slow)
    held = policy_mod.pending(ps, "loot")
    assert held["compiled"] is None and held["previous"] == original and ps.read("loot") == "# Loot\n\n1. B rule\n"


class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kw):
        self.log.append(("send", content, kw))

    async def edit_message(self, content=None, **kw):
        self.log.append(("edit_message", content, kw))

    async def defer(self, **kw):
        self.log.append(("defer", None, kw))

    def is_done(self):
        return any(k == "defer" for k, _, _ in self.log)


class Feed:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None):
        self.lines.append((level, text))


def press(log, name="Officer"):
    async def followup(content=None, **kw):
        log.append(("followup", content, kw))

    return types.SimpleNamespace(user=types.SimpleNamespace(id=OWNER, display_name=name), response=Resp(log), followup=types.SimpleNamespace(send=followup))


def test_discord_policy_edit_is_the_one_pending_edit_and_a_stale_view_does_nothing(reg):
    ps, log, feed = PolicyStore(reg.store, reg.key), [], Feed()
    original = ps.read("loot")

    async def go():
        modal = discord_policy.PolicyEditModal(reg, ps, "loot", Reader(), feed)
        modal.text._value = "# Loot\n\n1. A rule"
        await modal.on_submit(press(log, "Officer A"))
        view_a = log[-1][2]["view"]
        assert isinstance(view_a, discord_policy.CompileConfirmView) and policy_mod.pending(ps, "loot")["compiled"] is view_a.compiled
        # officer B edits on the site / MCP / chat meanwhile: A's reading is now of old text
        policy_mod.begin_edit(ps, "loot", "# Loot\n\n1. B rule", "Officer B")
        await view_a.confirm.callback(press(log, "Officer A"))
        assert "changed since this reading" in log[-1][1] and ps.compiled("loot") is None
        await view_a.discard.callback(press(log, "Officer A"))
        assert ps.read("loot") == "# Loot\n\n1. B rule\n", "a stale Discard doesn't wipe B's newer draft"
        assert policy_mod.pending(ps, "loot")["previous"] == original, "B's pending edit keeps the text to go back to"
        # B compiles through Discord reload and confirms: live, and nothing left pending
        await discord_policy.compile_and_confirm(press(log, "Officer B"), reg, ps, "loot", Reader(), feed, followup=True)
        view_b = log[-1][2]["view"]
        await view_b.confirm.callback(press(log, "Officer B"))
        assert ps.compiled("loot")["summary"] == "B rule" and policy_mod.pending(ps, "loot") is None

    asyncio.run(go())


# ---------------------------------------------------------------- M3: Apply is single-flight

def test_a_second_apply_or_a_cancel_mid_apply_does_nothing(reg, monkeypatch):
    from oibot_gm.configops import ConfigOp

    applied, log = [], []

    async def slow_apply(reg_, op, by, is_owner, ps=None, bot=None, by_id=None, **_kw):
        applied.append(op.op)
        await asyncio.sleep(0.05)  # a lock's solve
        return "locked"

    monkeypatch.setattr(configops, "apply_async", slow_apply)

    async def plain_identity(reg_, user):
        return True, []

    def presser():
        async def edit_original_response(content=None, **kw):
            log.append(("edit", content))

        async def send_message(content=None, **kw):
            log.append(("send", content))

        async def edit_message(content=None, **kw):
            log.append(("ack", content, kw.get("view", "kept")))

        return types.SimpleNamespace(user=types.SimpleNamespace(id=OWNER, display_name="Owner"), client=types.SimpleNamespace(plain_identity=plain_identity, raids=None),
                                     response=types.SimpleNamespace(edit_message=edit_message, send_message=send_message), edit_original_response=edit_original_response,
                                     followup=types.SimpleNamespace(send=send_message))

    async def go():
        view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", value="off")]), None, Feed(), OWNER, "Owner", GENERAL_CH)
        await asyncio.gather(view.apply.callback(presser()), view.apply.callback(presser()), view.cancel.callback(presser()))

    asyncio.run(go())
    assert applied == ["dm"], "applied once"
    assert ("ack", "⏳ Applying…", None) in log, "the buttons go at once"
    sends = [c for k, c, *_ in log if k == "send"]
    assert any("Already applying" in c for c in sends) and any("Too late to cancel" in c for c in sends)
    assert not any(k == "ack" and c == "Cancelled." for k, c, *_ in log)


# ---------------------------------------------------------------- M5: board revs follow this request's own writes only

def test_a_pin_before_a_board_op_is_this_requests_own_change(reg, rs):
    from test_parity_ops_board import PressBot, board_run, press

    from oibot_gm import raidcycle as rc
    from oibot_gm.configops import ConfigOp

    bot = PressBot(reg, rs)
    ev, names = board_run(reg, rs)
    ops = [ConfigOp(op="pin", target=ev.key, member=names[0], value="in"),
           ConfigOp(op="run_board", target=ev.key, value=f"{names[0]}, {names[1]} | {names[2]}")]
    log = []
    view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=ops), None, bot.ops, OWNER, "Owner", GENERAL_CH,
                                            revs=configops.board_revs(reg, ops, rs))
    asyncio.run(view.apply.callback(press(bot, OWNER, log)))
    assert "⛔" not in log[0], log[0]
    assert ev.layout[:2] == [[names[0], names[1]], [names[2]]] and list(ev.pins.values()) == ["in"]
    assert view.revs[ev.key] == rc.board_rev(reg, ev)


def test_after_a_stale_board_op_the_rest_of_the_request_cannot_overwrite(reg, rs):
    from test_parity_ops_board import PressBot, board_run, press

    from oibot_gm.configops import ConfigOp

    bot = PressBot(reg, rs)
    ev, names = board_run(reg, rs)
    ops = [ConfigOp(op="run_move", target=ev.key, member=names[0], value="g2"),
           ConfigOp(op="dm", member=names[5], value="off"),
           ConfigOp(op="run_board", target=ev.key, value=f"{names[0]}, {names[1]}")]
    revs = configops.board_revs(reg, ops, rs)
    asyncio.run(bot.board_move(reg, rs, ev, names[3], (0, 0), by="someone else"))  # between the preview and Apply
    theirs = [list(g) for g in ev.layout]
    log = []
    view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=ops), None, bot.ops, OWNER, "Owner", GENERAL_CH, revs=revs)
    asyncio.run(view.apply.callback(press(bot, OWNER, log)))
    assert log[0].count("changed since you looked") == 2, log[0]
    assert "DMs" in log[0] or "dm" in log[0].lower(), "the unrelated op still applies"
    assert ev.layout == theirs, "the other officer's move survives"


# ---------------------------------------------------------------- M4: one lock per run, and the board lock around it

def slow_propose(delay=0.05):
    import time as _time

    from oibot_gm import raidcycle as rc

    def propose(reg, rs, ev, save=True):
        _time.sleep(delay)  # the solve, in its worker thread
        names = [s.display_name for s in ev.signups.values() if s.status == "in"]
        ev.rosters = rc.board_rosters(reg, ev, [names[:5], names[5:10]], rc.run_size(reg, ev))
        ev.log.append("solver: proposed")
        return [], None
    return propose


def test_two_locks_at_once_lock_once(reg, rs, monkeypatch):
    from conftest import join, open_test_run
    from test_parity_verbs import Bot

    from oibot_gm import raidcycle as rc

    monkeypatch.setattr(rc, "propose", slow_propose())
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:10])

    async def go():
        return await asyncio.gather(bot.lock_run(reg, rs, ev, by="chat"), bot.lock_run(reg, rs, ev, by="scheduler"), return_exceptions=True)

    first, second = asyncio.run(go())
    assert isinstance(first, str) and "locked by chat" in first, first
    assert isinstance(second, rc.SolverBusy), second
    assert ev.state == "locked" and ev.log.count("locked by chat") == 1 and "locked by scheduler" not in ev.log
    with pytest.raises(configops.RegistryError, match="already locked"):
        asyncio.run(bot.lock_run(reg, rs, ev, by="card"))


def test_a_move_during_the_lock_solve_is_refused_not_lost_and_log_lines_survive(reg, rs, monkeypatch):
    from conftest import join, open_test_run
    from test_parity_verbs import Bot

    from oibot_gm import raidcycle as rc

    monkeypatch.setattr(rc, "propose", slow_propose(0.1))
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)

    async def move_later():
        await asyncio.sleep(0.02)  # the lock is solving now
        ev.log.append("someone: a line written during the solve")
        return await bot.board_move(reg, rs, ev, ms[0].display_name, (1, 0), by="officer B")

    async def go():
        return await asyncio.gather(bot.lock_run(reg, rs, ev, by="chat"), move_later(), return_exceptions=True)

    locked, moved = asyncio.run(go())
    assert "locked by chat" in locked
    assert isinstance(moved, rc.BoardChanged), "queued behind the lock, the move is refused as stale — not applied to the roster"
    assert "someone: a line written during the solve" in ev.log and "solver: proposed" in ev.log


def test_web_autofill_saves_through_save_board_with_its_rev(reg, rs, monkeypatch):
    """A drag while the site's Auto-fill solves wins: the fill is refused (409) instead of overwriting it."""
    from conftest import join, open_test_run
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, bearer
    from test_parity_verbs import Bot

    from oibot_gm import raidcycle as rc
    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = Bot(reg, rs)
    c = TestClient(web.create_app(bot))
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]
    drag = {"on": True}

    def autofill(reg_, rs_, ev_):
        if drag["on"]:
            ev.layout = [[], [names[9]]]  # officer B's drag lands while the solver runs
        return [names[:5], names[5:9]]

    monkeypatch.setattr(rc, "autofill", autofill)
    r = c.post(f"/api/run/{ev.key}/autofill", json={}, headers=bearer())
    assert r.status_code == 409, r.text
    assert ev.layout == [[], [names[9]]], "officer B's drag survives"
    assert not any("auto-filled" in line for line in ev.log)
    drag["on"] = False
    r = c.post(f"/api/run/{ev.key}/autofill", json={}, headers=bearer())
    assert r.status_code == 200, r.text
    assert ev.layout[:2] == [names[:5], names[5:9]] and any("auto-filled" in line for line in ev.log)


# ---------------------------------------------------------------- M6: MCP plain_change applies exactly what it previewed

def test_plain_change_apply_uses_the_previewed_ops_and_revs_never_a_new_parse(reg, rs, monkeypatch):
    from conftest import join, open_test_run
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, bearer
    from test_parity_verbs import Bot

    from oibot_gm.configops import ConfigOp
    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = Bot(reg, rs)
    bot.ctx = types.SimpleNamespace(provider=object())
    c = TestClient(web.create_app(bot))
    ms = reg.test_members()
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, ms[:6])
    parses = []

    def parse(provider, reg_, text, by=None, officer=False):  # a nondeterministic parser: another member each call
        parses.append(text)
        who = ms[0] if len(parses) == 1 else ms[1]
        return ConfigRequest(kind="change", reply="ok", ops=[ConfigOp(op="dm", member=who.display_name, value="off")])

    monkeypatch.setattr(configops, "parse", parse)
    H = bearer()
    d = c.post("/api/ops/change", json={"text": "turn Kestrel's DMs off"}, headers=H).json()
    assert d["token"] and d["expires_in"] > 0 and not d["applied"]
    r = c.post("/api/ops/change", json={"text": "turn Kestrel's DMs off", "apply": True}, headers=H)
    assert r.status_code == 400 and "token" in r.json()["error"], "apply never re-parses the text"
    d2 = c.post("/api/ops/change", json={"token": d["token"], "apply": True}, headers=H).json()
    assert len(parses) == 1 and d2["applied"] and reg.members[ms[0].discord_id].dm_opt_out and not reg.members[ms[1].discord_id].dm_opt_out
    assert c.post("/api/ops/change", json={"token": d["token"], "apply": True}, headers=H).status_code == 400, "single use"
    # a board op carries the rev it was previewed on
    names = [m.display_name for m in ms[:6]]
    monkeypatch.setattr(configops, "parse", lambda *a, **k: ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="run_move", target=ev.key, member=names[0], value="g1")]))
    token = c.post("/api/ops/change", json={"text": "move"}, headers=H).json()["token"]
    asyncio.run(bot.board_move(reg, rs, ev, names[1], (1, 0), by="someone else"))
    d3 = c.post("/api/ops/change", json={"token": token, "apply": True}, headers=H).json()
    assert "changed since you looked" in d3.get("error", ""), d3
    assert names[0] not in (ev.layout or [[]])[0]


def test_mcp_plain_change_sends_the_token_on_apply(monkeypatch):
    from oibot_gm import mcp_server as msrv

    sent = []
    monkeypatch.setattr(msrv, "api", lambda: types.SimpleNamespace(post=lambda path, body: sent.append((path, body)) or {"ok": True}))
    msrv.plain_change("make Kessa core")
    msrv.plain_change(apply=True, token="abc")
    assert sent == [("/api/ops/change", {"text": "make Kessa core"}), ("/api/ops/change", {"token": "abc", "apply": True})]
    with pytest.raises(Exception, match="token"):
        msrv.plain_change("make Kessa core", apply=True)


# ---------------------------------------------------------------- M7: the raid_set Apply line reads in 12-hour words

def test_raid_set_apply_line_and_change_log_read_in_12_hour_words(reg):
    import re

    from oibot_gm.configops import ConfigOp
    from oibot_gm.registry import recent_changes

    def military(text):
        return re.search(r"\b([01]\d|2[0-3]):[0-5]\d\b(?! ?[AP]M)|\d{4}-\d\d-\d\dT", text)

    line = configops.apply(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="slots", value="Tue 19:30, Thu 20:00"), "Owner", True)
    assert line == "barrow_deeps: slots = Tue 7:30 PM, Thu 8:00 PM", line
    line = configops.apply(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="first_open", value="2026-12-09 15:00"), "Owner", True)
    assert "3:00 PM" in line and not military(line), line
    merged = configops.coalesce([ConfigOp(op="raid_set", target="barrow_deeps", field="slots", value="Wed 21:15"),
                                 ConfigOp(op="raid_set", target="barrow_deeps", field="first_open", value="2026-12-10 18:30")])
    assert len(merged) == 1
    line = configops.apply(reg, merged[0], "Owner", True)
    assert "Wed 9:15 PM" in line and "6:30 PM" in line and not military(line), line
    assert reg.config.raids["barrow_deeps"]["slots"] == ["Wed 21:15"], "the stored form stays 24-hour"
    for row in recent_changes(reg.key, 10):
        assert not military(row["text"]), row["text"]
