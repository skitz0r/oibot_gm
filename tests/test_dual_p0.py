"""Dual interfaces, phase 0 (design §2.2, §2.4, §2.6; out/dual_interface_design.md §6 Phase 0, part A):
ONE identity predicate the five old checks alias; seats, callouts and free_seat keyed by member id (a nick change
mid-run keeps the seat and the callout); one audit helper; the per-registry config lock and thread-local batches;
section revs with ConfigChanged carried through the configops revs dict under "config:"."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import discord
import pytest
from conftest import GUILD, OFFICER_ROLE, OWNER, by_role, join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_mcp import TOKEN, FakeBot

from oibot_gm import configops, guild_routing, ops as ops_mod, raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_bot import OibotGM
from oibot_gm.discord_help import asker_identity
from oibot_gm.discord_raid import RaidMixin
from oibot_gm.discord_registry import is_officer
from oibot_gm.registry import ConfigChanged, RegistryError
from oibot_gm.web import app as web

SERVER = 1
OFFICER_UID, MEMBER_UID, OUTSIDER_UID, DM_MEMBER_UID = 500_000_000_000_000_001, 500_000_000_000_000_002, 500_000_000_000_000_003, 500_000_000_000_000_004
SU = 424_242_424_242_424_242
SECRET = "s" * 40


def run(coro):
    return asyncio.run(coro)


# ---- doubles: a Discord server with real discord.Member instances (subclass trick, as test_multiguild_core's Ch), so
# the REAL predicates run — OibotGM.officiates reads guild_permissions and roles, guild_routing sees a Member of the server

class Role:
    def __init__(self, rid):
        self.id, self.name, self.position, self.managed = rid, f"role{rid}", 1, False

    def is_default(self):
        return False


class FakeGuild:
    def __init__(self, gid, people):
        self.id, self.name, self.icon, self.owner_id = gid, "Demo", None, None
        self.people = people  # uid -> Member (the gateway cache)
        self.roles = [Role(OFFICER_ROLE)]

    def get_member(self, uid):
        return self.people.get(uid)

    async def fetch_member(self, uid):
        raise RuntimeError("not in the gateway cache and the REST lookup is not simulated")  # a blip, never a 404


class Member(discord.Member):
    """A real discord.Member type with plain attributes: the parent's properties and slots are shadowed by class
    attributes here, so a subclass instance (which has a __dict__) can be filled by hand."""

    id = display_name = roles = guild_permissions = guild = bot = None

    def __repr__(self):
        return f"<Member {self.display_name} {self.id}>"

    def __hash__(self):
        return hash(self.id)

    def __init__(self, uid, name, guild, roles=(), manage=False):
        self.id, self.display_name, self.guild = uid, name, guild
        self.roles = [Role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(manage_guild=manage)
        self.bot = False


class User:
    """A bare discord.User as a DM hands it over: an id, no roles, no guild."""

    def __init__(self, uid, name):
        self.id, self.display_name, self.bot = uid, name, False


class IdBot(FakeBot):
    """test_mcp's web-capable FakeBot plus the REAL identity code of the client, bound as methods: officiates, cached_member,
    saw_member, role_of, identity, plain_identity (OibotGM) and is_officer_anywhere (RaidMixin)."""

    officiates = OibotGM.officiates
    cached_member = OibotGM.cached_member
    saw_member = OibotGM.saw_member
    role_of = OibotGM.role_of
    identity = OibotGM.identity
    plain_identity = OibotGM.plain_identity
    is_officer_anywhere = RaidMixin.is_officer_anywhere

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.guild = FakeGuild(SERVER, {})
        self.guild.people = {
            OFFICER_UID: Member(OFFICER_UID, "Offi", self.guild, roles=[OFFICER_ROLE]),
            MEMBER_UID: Member(MEMBER_UID, "Mem", self.guild),
            DM_MEMBER_UID: Member(DM_MEMBER_UID, "Dee", self.guild),
            OWNER: Member(OWNER, "Own", self.guild),
        }

    def get_guild(self, gid):
        return self.guild if gid == SERVER else None

    def get_user(self, uid):
        return None


def interaction(bot, user, in_server=True):
    return SimpleNamespace(client=bot, user=user, guild_id=SERVER if in_server else None,
                           guild=bot.guild if in_server else None, extras={})


@pytest.fixture
def idbot(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(SU))
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    reg.config.owner_discord_id = OWNER
    bot = IdBot(reg, rs)
    for uid, name in ((OFFICER_UID, "Offi"), (MEMBER_UID, "Mem"), (DM_MEMBER_UID, "Dee"), (OUTSIDER_UID, "Outy"), (OWNER, "Own"), (SU, "Host")):
        reg.set_plan(uid, name, "Priest", "Holy", None, "main")  # registered, so the web's member rows carry a privilege
    return bot


def web_privilege(bot, reg, uid) -> str:
    """app.py's `privilege`, read back through GET /api/members (the officer's view) for one member."""
    c = TestClient(web.create_app(bot), follow_redirects=False)
    c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(OFFICER_UID), "name": "x", "iat": time.time()}))
    r = c.get("/api/members", headers={"X-Requested-With": "oibot"})
    assert r.status_code == 200, r.text
    return next(row["privilege"] for row in r.json()["rows"] if row["uid"] == str(uid))


def all_five(bot, reg, who, in_server: bool) -> dict:
    """Every old predicate, as its callers see it: is_officer (sync, commands), is_officer_anywhere (buttons),
    plain_identity (chat), asker_identity (/help), the web's privilege — plus role_of/identity themselves."""
    itx = interaction(bot, who, in_server)
    role = run(bot.role_of(reg, who))
    ident = run(bot.identity(reg, who))
    officer = role in ("owner", "officer")
    return {
        "role": role, "identity": (ident.role, ident.officer), "is_officer": is_officer(itx, reg),
        "anywhere": run(bot.is_officer_anywhere(reg, who.id)), "plain": run(bot.plain_identity(reg, who))[0],
        "asker": run(asker_identity(itx, reg))[0], "web": web_privilege(bot, reg, who.id), "officer": officer,
    }


def test_the_five_identity_checks_agree_on_a_member_a_dm_user_the_owner_and_a_superuser(idbot, reg):
    g = idbot.guild
    # an officer by role, pressing in the server
    a = all_five(idbot, reg, g.people[OFFICER_UID], True)
    assert a["role"] == "officer" and a["identity"] == ("officer", True) and a["web"] == "officer"
    assert a["is_officer"] and a["anywhere"] and a["plain"] and a["asker"]
    # a plain member in the server
    b = all_five(idbot, reg, g.people[MEMBER_UID], True)
    assert b["role"] == "member" and b["web"] == "member" and not (b["is_officer"] or b["anywhere"] or b["plain"] or b["asker"])
    # a bare User in a DM: looked up as the guild member (no roles on the User itself)
    c = all_five(idbot, reg, User(DM_MEMBER_UID, "Dee"), False)
    assert c["role"] == "member" and c["web"] == "member" and not (c["anywhere"] or c["plain"] or c["asker"]) and not c["is_officer"]
    # a DM from someone who is not in the server at all
    d = all_five(idbot, reg, User(OUTSIDER_UID, "Outy"), False)
    assert d["role"] == "outside" and d["web"] == "outside" and not (d["anywhere"] or d["plain"] or d["asker"] or d["is_officer"])
    # the owner, from a DM (a bare User) and in the server
    e = all_five(idbot, reg, User(OWNER, "Own"), False)
    f = all_five(idbot, reg, g.people[OWNER], True)
    for x in (e, f):
        assert x["role"] == "owner" and x["web"] == "owner" and x["is_officer"] and x["anywhere"] and x["plain"] and x["asker"]
    # a host superuser who is not even in the server: owner everywhere (Registry.is_owner_id)
    h = all_five(idbot, reg, User(SU, "Host"), False)
    assert h["role"] == "owner" and h["web"] == "owner" and h["is_officer"] and h["anywhere"] and h["plain"] and h["asker"]


def test_a_member_decides_live_and_refreshes_the_cache_a_uid_reads_the_cache_with_a_stale_flag(idbot, reg):
    g = idbot.guild
    m = g.people[MEMBER_UID]
    idbot.__dict__.setdefault("_member_gone", set()).add((SERVER, MEMBER_UID))  # a cached 404 from earlier
    r = run(guild_routing.resolve_role(idbot, reg, m))
    assert r.role == "member" and not r.stale and (SERVER, MEMBER_UID) not in idbot._member_gone, "evidence beats the cache"
    # a Member of ANOTHER server decides nothing here: looked up by id like a User
    other = Member(MEMBER_UID, "Mem", FakeGuild(99, {}), roles=[OFFICER_ROLE], manage=True)
    assert run(guild_routing.role_of(idbot, reg, other)) == "member", "roles in another server grant nothing in this one"
    # by id, answered from the process's own cache (gateway miss): marked stale
    del g.people[DM_MEMBER_UID]
    idbot._member_cache = {(SERVER, DM_MEMBER_UID): (time.time(), Member(DM_MEMBER_UID, "Dee", g))}
    r = run(guild_routing.resolve_role(idbot, reg, DM_MEMBER_UID, max_age=guild_routing.PRIV_WEB))
    assert r.role == "member" and r.stale
    r = run(guild_routing.resolve_role(idbot, reg, DM_MEMBER_UID, max_age=guild_routing.PRIV_LIVE))
    assert r.role == "outside", "a write re-resolves live: the cache is not an answer"
    assert guild_routing.PRIV_LIVE == 0 and guild_routing.PRIV_ROUTING == 10 and guild_routing.PRIV_WEB == 60


# ---- seats by id: a nick change mid-run

class SeatBot:
    """RaidMixin's own verbs over a real registry and store; Discord calls are recorded, DMs counted."""

    set_answer = RaidMixin.set_answer
    call_out = RaidMixin.call_out
    drop_seated = RaidMixin.drop_seated
    release_asks_of = RaidMixin.release_asks_of
    refill_after_release = RaidMixin.refill_after_release
    refresh_cards = RaidMixin.refresh_cards

    def __init__(self, reg, rs):
        self.raids = SimpleNamespace(store=lambda _reg: rs)
        self.calls, self.lines = [], []
        self.ops = SimpleNamespace(emit=self._emit)
        self.ico = lambda kind, key: ""

    async def _emit(self, cfg, level, text, *a, **k):
        self.lines.append((level, text))

    async def refresh_sheet(self, reg, ev):
        self.calls.append("refresh_sheet")

    async def post_run_update(self, reg, ev, text):
        self.calls.append(("update", text))

    async def dm_seat_released(self, reg, ev, m, why):
        self.calls.append(("dm_released", m.discord_id, why))
        return True

    async def run_fill(self, reg, rs, ev, team, by=None, **kw):
        self.calls.append(("run_fill", by))
        return [], None

    async def confirm_one(self, reg, ev, sg, i, gi, by):
        return False


def _locked(reg, rs):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    for m in seated:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    return ev, seated


def test_a_nick_change_mid_run_keeps_the_seat_and_the_callout(reg, rs):
    ev, seated = _locked(reg, rs)
    m = seated[-1]
    old = m.display_name
    reg.rename(m.discord_id, "Freshnick", "t")
    assert reg.members[m.discord_id].display_name == "Freshnick" and ev.seat_of(m) and ev.seat_of(m.discord_id)
    assert ev.seat_of(old), "the seat still answers to the name it was taken under (the run's own signup)"
    bot = SeatBot(reg, rs)
    line = run(bot.set_answer(reg, rs, ev, m, "out", None, by="Freshnick", by_id=m.discord_id))  # their own Can't make it
    assert "seat freed" in line and "you are" in line, line
    assert not ev.seat_of(m) and not ev.seat_of(m.discord_id) and all("Freshnick" not in g and old not in g for g in ev.roster.groups)
    co = [c for c in ev.callouts if c.discord_id == m.discord_id]
    assert len(co) == 1 and co[0].display_name == "Freshnick", "the callout is theirs, keyed by id"
    assert ev.signups[str(m.discord_id)].status == "out"
    assert ("dm_released", m.discord_id, "callout") in bot.calls


def test_member_never_renames_inside_a_verb(reg):
    m = reg.test_members()[0]
    name = m.display_name
    reg.set_plan(m.discord_id, "Sneakyrename", "Priest", "Holy", None, "alt")
    reg.add_absence(m.discord_id, reg.now_local().date().isoformat(), None, None, "t", display_name="Anotherrename")
    assert reg.members[m.discord_id].display_name == name
    with pytest.raises(RegistryError):
        reg.rename(m.discord_id, "  ", "t")
    reg.rename(m.discord_id, "Chosen", "t")
    assert reg.members[m.discord_id].display_name == "Chosen" and reg.member(m.discord_id, "Ignored").display_name == "Chosen"


def test_by_id_decides_whose_answer_it_is_never_the_name(reg, rs):
    ev, seated = _locked(reg, rs)
    bot = SeatBot(reg, rs)
    m, officer = seated[0], seated[1]
    reg.rename(officer.discord_id, m.display_name, "t")  # an officer whose nick now equals the member's: the old string compare was fooled
    line = run(bot.call_out(reg, rs, ev, m, None, by=officer.display_name, by_id=officer.discord_id))
    assert line.startswith(f"{m.display_name} is out of"), "the officer's by_id makes it an officer's callout, whatever the names"
    assert ev.callouts[-1].discord_id == m.discord_id
    line = run(bot.set_answer(reg, rs, ev, seated[2], "sub", None, by=seated[2].display_name))  # no by_id: an officer acting
    assert line.startswith(f"✅ {seated[2].display_name}: ")


def test_free_seat_expiry_and_the_board_find_a_renamed_seat(reg, rs):
    ev, seated = _locked(reg, rs)
    a, b, c = seated[0], seated[1], seated[2]
    old_c = c.display_name
    for m in (a, b, c):
        reg.rename(m.discord_id, f"New{m.display_name}", "t")
    # free by id, by Member, by the name the run knows (the one they were seated under); the registry's new name is not
    # the run's business — callers pass the member or the id
    assert rc.free_seat(reg, rs, ev, a.discord_id, "declined") is not None and not ev.seat_of(a)
    assert rc.free_seat(reg, rs, ev, b, "declined") is not None and not ev.seat_of(b)
    assert rc.free_seat(reg, rs, ev, old_c, "declined") is not None and not ev.seat_of(c)
    assert rc.free_seat(reg, rs, ev, "Nobodyhere", "declined") is None
    # the board keeps a renamed seat: the layout still says the OLD name (what the groups carry), nobody is freed or re-added
    d = seated[3]
    old_d = d.display_name
    reg.rename(d.discord_id, "Renamedd", "t")
    layout = [list(g) for g in ev.roster.groups]
    assert any(old_d in g for g in layout)
    added, removed = rc.apply_layout_locked(reg, rs, ev, layout)
    assert added == [] and removed == [] and ev.seat_of(d) and ev.seat_of(d)[1].signup_name == old_d, "no churn: the seat is the same person by id"
    assert any(old_d in g for g in ev.roster.groups) and ev.signups[str(d.discord_id)].status == "in"
    # expiry frees the remaining unanswered seats by id, renamed or not
    e = seated[4]
    old_e = e.display_name
    reg.rename(e.discord_id, "Expiree", "t")
    ev.confirm_by = (rc.datetime.now(ev.start.tzinfo) - rc.timedelta(minutes=1)).isoformat()
    gone = rc.expire_confirmations(reg, rs, ev)
    assert old_e in gone
    assert not ev.seat_of(e) and ev.signups[str(e.discord_id)].source == "no-confirm"


def test_seat_of_falls_back_to_the_name_for_old_run_files(reg, rs):
    ev, seated = _locked(reg, rs)
    p = ev.roster.selected[0]
    p.discord_id = None  # a file from before ids were stored
    m = next(m for m in seated if m.display_name == p.signup_name)
    assert ev.seat_of(m)[1] is p and ev.seat_of(p.signup_name)[1] is p and ev.seat_of(m.discord_id)[1] is p, "the run's own signup supplies the id"
    ev.signups.pop(str(m.discord_id))  # nothing left to resolve the name: the bare name still finds the seat
    assert ev.seat_of(p.signup_name)[1] is p and ev.seat_of(m.discord_id) is None


# ---- one audit helper

class Lines:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, *a, **k):
        self.lines.append((getattr(cfg, "key", None), level, text))


def test_audit_line_prefix_from_via_and_host_suffix_for_a_superuser(reg, monkeypatch):
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(SU))
    reg.config.owner_discord_id = OWNER
    assert ops_mod.audit_line(reg, "Jon", "signup channel → #raids") == "Jon: signup channel → #raids"
    assert ops_mod.audit_line(reg, "Jon", "x", via="web") == "[web] Jon: x"
    assert ops_mod.audit_line(reg, "Jon", "x", via="mcp") == "[mcp] Jon: x"
    assert ops_mod.audit_line(reg, "Jon", "x", via="chat") == "[chat] Jon: x"
    assert ops_mod.audit_line(reg, "Host", "x", via="web", by_id=SU) == "[web] Host (host): x", "a superuser in a guild they don't own"
    assert ops_mod.audit_line(reg, "Own", "x", via="web", by_id=OWNER) == "[web] Own: x"
    reg.config.owner_discord_id = SU
    assert ops_mod.audit_line(reg, "Host", "x", by_id=SU) == "Host: x", "their own guild: no suffix"
    out = Lines()
    assert run(ops_mod.audited(out, reg, "Jon", "did it", via="mcp", by_id=OWNER)) == "[mcp] Jon: did it"
    assert out.lines == [(GUILD, "info", "[mcp] Jon: did it")]
    real = ops_mod.Ops(SimpleNamespace(get_channel=lambda cid: None, registries=None))
    reg.config.ops_channel_id = None
    assert run(real.audited(reg, "Jon", "x", via="web", level="warn")) == "[web] Jon: x" and real.recent_for(GUILD)[-1][1:] == ("warn", "[web] Jon: x")


def test_set_channel_writes_the_one_audit_line_and_apply_async_does_not_double_it(duo):
    from oibot_gm.discord_pool import SetupMixin

    class Ch(discord.TextChannel):
        def __init__(self, cid, gid):
            self.id, self.name, self.guild = cid, "general", SimpleNamespace(id=gid)

    chans = {50: Ch(50, 1)}

    class Bot(SetupMixin):
        set_channel = SetupMixin.set_channel

        def __init__(self):
            self.ops = Lines()

        def get_channel(self, cid):
            return chans.get(cid)

    bot = Bot()
    assert run(bot.set_channel(duo.a, "signup", 50, "Jon", via="web", by_id=7)) == "signup channel → #general"
    assert bot.ops.lines == [(GUILD, "info", "[web] Jon: signup channel → #general")]
    # through apply_async with via: the channel op is audited once (by set_channel), a plain setting once (by apply_async)
    bot.ops.lines.clear()
    run(configops.apply_async(duo.a, ConfigOp(op="set", path="roster_channel", value="<#50>"), "Jon", True, bot=bot, by_id=7, via="mcp"))
    assert bot.ops.lines == [(GUILD, "info", "[mcp] Jon: roster channel → #general")]
    bot.ops.lines.clear()
    run(configops.apply_async(duo.a, ConfigOp(op="set", path="timezone", value="Europe/London"), "Jon", True, bot=bot, by_id=7, via="web"))
    assert bot.ops.lines == [(GUILD, "info", "[web] Jon: timezone = Europe/London")]
    bot.ops.lines.clear()
    run(configops.apply_async(duo.a, ConfigOp(op="set", path="about", value="hi"), "Jon", True, bot=bot, by_id=7))
    assert bot.ops.lines == [], "no via: the caller writes its own summary (Discord's Apply)"


def test_mcp_viewer_is_named_after_the_owner_never_mcp(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    reg.config.owner_discord_id = OWNER
    bot = FakeBot(reg, rs)
    assert web.owner_name(bot, reg) == "the owner"
    bot.get_user = lambda uid: SimpleNamespace(display_name="Owny") if uid == OWNER else None
    assert web.owner_name(bot, reg) == "Owny"
    reg.set_plan(OWNER, "Ownrecord", "Priest", "Holy", None, "main")
    assert web.owner_name(bot, reg) == "Ownrecord", "the member record first"
    req = SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}", "x-oibot-guild": GUILD}, client=SimpleNamespace(host="127.0.0.1"), query_params={})
    v = web.mcp_viewer(req, bot, TOKEN)
    assert v is not None and v.name == "Ownrecord" and v.via == "mcp" and v.uid == OWNER and v.owner


# ---- the config lock, thread-local batches, preview on a shadow

def test_config_verbs_and_save_config_hold_the_registry_lock(reg, monkeypatch):
    seen = []
    orig = reg.save_config

    def spy(message, notify=True):
        seen.append(reg.config_lock._is_owned())
        return orig(message, notify)

    monkeypatch.setattr(reg, "save_config", spy)
    reg.set_raid_override("barrow_deeps", "lockout_days", 5, "t")
    reg.set_schedule("barrow_deeps", "fri", {"kind": "weekly", "slots": "Fri 20:00", "name": "Fridays"}, "t")
    reg.set_buff_override(reg.profile.buffs[0].id, "note", "x", "t")
    reg.set_recruitment("t", note="hi")
    reg.set_plain_policy({"dm": "answer"}, "t")
    reg.set_officer_role("<@&123>", True, "t")
    configops.apply(reg, ConfigOp(op="set", path="timezone", value="Europe/London"), "t", is_owner=True)
    assert seen and all(seen), seen
    assert isinstance(reg.config_lock, type(threading.RLock()))
    # a worker thread's verb waits for the loop thread's lock
    reg.config_lock.acquire()
    done = threading.Event()
    threading.Thread(target=lambda: (reg.set_raid_override("barrow_deeps", "lockout_days", 6, "t"), done.set()), daemon=True).start()
    assert not done.wait(0.3), "blocked while the lock is held"
    reg.config_lock.release()
    assert done.wait(5) and reg.raid_def("barrow_deeps")["lockout_days"] == 6


def test_git_batches_are_per_thread(store):
    head0 = store.head()
    other_landed = threading.Event()

    def other():
        store.write_text(f"{GUILD}/other.txt", "from another thread\n")
        store.commit("other thread's commit")
        other_landed.set()

    with store.batch("the loop's batch"):
        t = threading.Thread(target=other)
        t.start()
        t.join(10)
        assert other_landed.is_set() and store.head() != head0, "another thread's commit lands at once: it is not inside this thread's batch"
        head1 = store.head()
        store.write_text(f"{GUILD}/mine.txt", "deferred\n")
        store.commit("deferred inside the batch")
        assert store.head() == head1, "the batching thread's own commit waits for the block to end"
    assert store.head() != head1, "the batch commits on exit"
    log = store._git("log", "-2", "--format=%s%n%b").stdout
    assert "deferred inside the batch" in log and "other thread's commit" in log and log.index("other thread") > log.index("deferred")


def test_preview_runs_on_a_shadow_and_never_touches_the_live_config(reg, store):
    head = store.head()
    before = reg.config.model_dump()
    rev = reg.config_rev("raids:barrow_deeps")

    def on(sh):
        sh.set_raid_overrides("barrow_deeps", {"lockout_days": 5, "duration_hours": 2}, "preview")
        return sh.raid_def("barrow_deeps")

    rd = reg.preview(on)
    assert rd["lockout_days"] == 5 and reg.raid_def("barrow_deeps")["lockout_days"] != 5
    assert reg.config.model_dump() == before and store.head() == head and reg.config_rev("raids:barrow_deeps") == rev
    with pytest.raises(RegistryError):
        reg.preview(lambda sh: sh.set_raid_overrides("barrow_deeps", {"lockout_days": "soon"}, "preview"))
    assert reg.config.model_dump() == before


def test_raid_wizard_preview_uses_the_shadow(reg):
    from oibot_gm.flows_config import RaidConfigWizard

    w = RaidConfigWizard.__new__(RaidConfigWizard)
    w.reg, w.raid, w.draft = reg, "barrow_deeps", {"lockout_days": 5}
    before = reg.config.model_dump()
    rd, err = RaidConfigWizard.preview(w)
    assert err is None and rd["lockout_days"] == 5 and reg.config.model_dump() == before
    w.draft = {"lockout_days": "never"}
    rd, err = RaidConfigWizard.preview(w)
    assert rd is None and err and reg.config.model_dump() == before


# ---- section revs and ConfigChanged

def test_config_rev_per_section_ignores_bookkeeping(reg):
    revs = {s: reg.config_rev(s) for s in ("settings", "raids:barrow_deeps", "buffs", "recruitment", "plain")}
    assert len(set(revs.values())) == 5 and all(len(r) == 12 for r in revs.values())
    # the scheduler's bookkeeping moves no rev
    reg.config.registration_message_id = 12345
    reg.config.analytics_message_ids = {"bd": 1}
    reg.config.rosters.append({"key": "bd-x", "name": "x", "size": 10})
    reg.save_config("bookkeeping", notify=False)
    assert {s: reg.config_rev(s) for s in revs} == revs
    # each verb moves its own section only
    reg.config.timezone = "Europe/London"
    assert reg.config_rev("settings") != revs["settings"] and reg.config_rev("raids:barrow_deeps") == revs["raids:barrow_deeps"]
    reg.set_raid_override("barrow_deeps", "lockout_days", 5, "t")
    assert reg.config_rev("raids:barrow_deeps") != revs["raids:barrow_deeps"] and reg.config_rev("buffs") == revs["buffs"]
    reg.set_buff_override(reg.profile.buffs[0].id, "note", "seen", "t")
    assert reg.config_rev("buffs") != revs["buffs"] and reg.config_rev("recruitment") == revs["recruitment"]
    reg.set_recruitment("t", note="healers wanted")
    assert reg.config_rev("recruitment") != revs["recruitment"] and reg.config_rev("plain") == revs["plain"]
    reg.set_plain_policy({"dm": "answer"}, "t")
    assert reg.config_rev("plain") != revs["plain"]
    with pytest.raises(RegistryError):
        reg.config_rev("nope")


def test_config_verbs_refuse_a_stale_rev_and_write_nothing(reg, store):
    rev = reg.config_rev("raids:barrow_deeps")
    reg.set_raid_override("barrow_deeps", "lockout_days", 5, "t", rev=rev)  # the rev it saw: fine
    assert reg.raid_def("barrow_deeps")["lockout_days"] == 5
    head = store.head()
    with pytest.raises(ConfigChanged) as e:
        reg.set_raid_overrides("barrow_deeps", {"lockout_days": 6}, "t", rev=rev)  # someone changed it since
    assert isinstance(e.value, RegistryError) and e.value.section == "raids:barrow_deeps" and "changed since you looked" in str(e.value)
    assert reg.raid_def("barrow_deeps")["lockout_days"] == 5 and store.head() == head
    with pytest.raises(ConfigChanged):
        reg.set_schedule("barrow_deeps", "fri", {"kind": "weekly", "slots": "Fri 20:00"}, "t", rev=rev)
    with pytest.raises(ConfigChanged):
        reg.set_buff_override(reg.profile.buffs[0].id, "note", "x", "t", rev="000000000000")
    with pytest.raises(ConfigChanged):
        reg.set_recruitment("t", open=True, rev="000000000000")
    with pytest.raises(ConfigChanged):
        reg.set_plain_policy({"dm": "answer"}, "t", rev="000000000000")
    with pytest.raises(ConfigChanged):
        reg.set_officer_role("<@&5>", True, "t", rev="000000000000")
    with pytest.raises(ConfigChanged):
        configops.apply(reg, ConfigOp(op="set", path="timezone", value="Europe/London"), "t", is_owner=True, rev="000000000000")
    assert reg.config.timezone != "Europe/London" and store.head() == head
    reg.set_raid_override("barrow_deeps", "lockout_days", 7, "t", rev=None)  # no rev: no check, as every caller today


def test_configops_revs_carry_config_sections_under_the_config_prefix(reg, rs):
    ops = [ConfigOp(op="raid_set", target="barrow_deeps", field="lockout_days", value="5"),
           ConfigOp(op="aura_set", target=reg.profile.buffs[0].id, field="note", value="x"),
           ConfigOp(op="set", path="timezone", value="Europe/London"),
           ConfigOp(op="recruit", field="open", value="true"),
           ConfigOp(op="dm", value="off", member="x")]
    revs = configops.board_revs(reg, ops, rs)
    assert set(revs) == {"config:raids:barrow_deeps", "config:buffs", "config:settings", "config:recruitment"}
    assert revs["config:raids:barrow_deeps"] == reg.config_rev("raids:barrow_deeps")
    assert configops.op_rev(ops[0], None, revs) == revs["config:raids:barrow_deeps"] and configops.op_rev(ops[4], None, revs) is None
    assert configops.config_section(ops[2]) == "settings" and configops.config_section(ops[4]) is None
    # Apply's loop: an op applied with its rev refreshes it (this request's own write); someone else's change in
    # between marks the section stale and the op is refused with nothing written
    before = configops.snapshot_revs(reg, revs, rs)
    assert before == revs
    configops.apply(reg, ops[0], "t", is_owner=True, rev=configops.op_rev(ops[0], None, revs))
    stale: set = set()
    configops.refresh_revs(reg, revs, rs, stale, before)
    assert revs["config:raids:barrow_deeps"] == reg.config_rev("raids:barrow_deeps") and not stale
    reg.set_buff_override(reg.profile.buffs[0].id, "status", "confirmed", "someone else")  # an edit on the site meanwhile
    before = configops.snapshot_revs(reg, revs, rs)
    with pytest.raises(ConfigChanged):
        configops.apply(reg, ops[1], "t", is_owner=True, rev=configops.op_rev(ops[1], None, revs))
    configops.refresh_revs(reg, revs, rs, stale, before)
    assert "config:buffs" in stale and revs["config:buffs"] != reg.config_rev("buffs"), "the held rev stays: later ops can't absorb that change"
    assert reg.config.buffs[reg.profile.buffs[0].id].get("note") is None


def test_characters_rev_follows_the_character_list_not_updated_at(reg):
    m = reg.test_members()[0]
    r0 = reg.characters_rev(m)
    m.dm_opt_out = not m.dm_opt_out
    reg.save(m, "dm toggle")
    reg.add_placement_ask(m.discord_id, "bd-x", m.main.label, "lock")
    assert reg.characters_rev(reg.members[m.discord_id]) == r0, "bookkeeping on the member moves nothing"
    primary, flex = reg.roles_of(m)
    extra = next(r for r in ("tank", "healer", "melee", "ranged") if r != primary and r not in flex)
    reg.set_flex(m.discord_id, m.main.label, [extra])
    assert reg.characters_rev(reg.members[m.discord_id]) != r0


# ---- after review: one name per seat, every caller asks by member, the nick follows, writes decide live, one audit line

def _src_hits(needle: str, skip: tuple[str, ...] = ()) -> list[str]:
    """Lines of the package's own source carrying `needle` (a structural guard: a pattern that must not come back)."""
    from pathlib import Path

    root = Path(rc.__file__).parent
    return [f"{p.relative_to(root)}:{i}" for p in sorted(root.rglob("*.py")) if p.name not in skip
            for i, l in enumerate(p.read_text().splitlines(), 1) if needle in l]


def test_a_renamed_member_who_re_answers_leaves_one_name_per_seat_and_no_ghost(reg, rs):
    """Rename → Join again → the site saves the board it shows → the seat is freed: the groups never carry two names for
    one person, and the name leaves with the seat (a ghost 'Freshnick' stood in a group with no seat behind it)."""
    ev, seated = _locked(reg, rs)
    m = seated[-1]
    old = m.display_name
    reg.rename(m.discord_id, "Freshnick", "t")
    bot = SeatBot(reg, rs)
    line = run(bot.set_answer(reg, rs, ev, m, "in", None, by="Freshnick", by_id=m.discord_id))  # their own Join, same character
    assert "stays rostered" in line, line
    seat = ev.seat_of(m)
    assert seat and seat[1].signup_name == "Freshnick" and ev.seat_of("Freshnick")[1] is seat[1] and ev.seat_of(old) is None
    names = [n for g in ev.roster.groups for n in g]
    assert names.count("Freshnick") == 1 and old not in names, "the seat and its group entry follow the current name"
    added, removed = rc.apply_layout_locked(reg, rs, ev, [list(g) for g in ev.roster.groups])  # the board, saved as shown
    assert added == [] and removed == [] and ev.seat_of(m)[1].signup_name == "Freshnick"
    assert rc.free_seat(reg, rs, ev, m, "declined") is not None
    assert not ev.seat_of(m) and all("Freshnick" not in g and old not in g for g in ev.roster.groups)
    assert len([n for g in ev.roster.groups for n in g]) == len(ev.roster.selected), "every group entry has a seat behind it"


def test_the_board_relabels_a_kept_seat_and_free_seat_drops_the_group_entry_by_id(reg, rs):
    """The probe as found: the signup carries the new nick, the seat's Player still the old one. A board write must not
    leave the Player under one name and the groups under another, and free_seat must find the entry whatever name it bears."""
    ev, seated = _locked(reg, rs)
    m = seated[-1]
    old = m.display_name
    reg.rename(m.discord_id, "Freshnick", "t")
    rc.set_signup(reg, rs, ev, m, None, "in")  # a re-answer that did not go through _reseat (an older run file)
    assert ev.seat_of(m)[1].signup_name == old and ev.signups[str(m.discord_id)].display_name == "Freshnick"
    added, removed = rc.apply_layout_locked(reg, rs, ev, [list(g) for g in ev.roster.groups])
    assert added == [] and removed == []
    assert ev.seat_of(m)[1].signup_name == "Freshnick", "the kept seat takes the name the groups were written with"
    assert sum(n in ("Freshnick", old) for g in ev.roster.groups for n in g) == 1
    ev.seat_of(m)[1].signup_name = old  # and even with the Player still under the old name …
    assert rc.free_seat(reg, rs, ev, m, "declined") is not None
    assert all("Freshnick" not in g and old not in g for g in ev.roster.groups), "… the group entry goes by id"


def test_every_caller_asks_seat_of_with_the_member_never_the_display_name(reg, rs):
    """After a rename the run's signup and seat still carry the old nick, so `seat_of(m.display_name)` found nothing
    (the sheet's Can't make it said 'not rostered', the Me page showed no seat, describe() promised no freed seat)."""
    assert _src_hits("seat_of(m.display_name)") == [] and _src_hits("seat_of(me.display_name)") == []
    ev, seated = _locked(reg, rs)
    m = seated[0]
    reg.rename(m.discord_id, "Freshnick", "t")
    assert ev.seat_of(m) and ev.seat_of(m.display_name) is None, "the run knows the old name only"
    text = configops.describe(reg, ConfigOp(op="run_answer", target=ev.key, member="Freshnick", value="out"))
    assert "seat freed" in text, text
    text = configops.describe(reg, ConfigOp(op="run_answer", target=ev.key, member="Freshnick", value="sub"))
    assert "keeps the seat" in text, text


def test_a_nick_seen_in_the_server_renames_the_record_and_nothing_else(idbot, reg, rs):
    """Registry.rename has ONE trigger: a discord.Member of the guild's server whose display name differs from the record
    — a command or a press (guild_routing.need / resolve_button → _tag) or on_member_update. Runs keep the name the person
    answered under; the seat is by id."""
    g = idbot.guild
    m = g.people[MEMBER_UID]
    m.display_name = "Memnew"
    reg_, _ = run(guild_routing.need(interaction(idbot, m)))
    assert reg_ is reg and reg.members[MEMBER_UID].display_name == "Memnew"
    assert not guild_routing.sync_name(reg, m), "unchanged: a no-op, no commit"
    run(guild_routing.need(interaction(idbot, User(DM_MEMBER_UID, "Deenew"), False)))  # a DM: a bare User has no nick to read
    assert reg.members[DM_MEMBER_UID].display_name == "Dee"
    assert not guild_routing.sync_name(reg, Member(DM_MEMBER_UID, "Elsewhere", FakeGuild(99, {})) ), "another server's nick is not this guild's"
    assert not guild_routing.sync_name(reg, Member(777, "Nobody", g)), "unregistered: nothing to rename"
    assert reg.members[DM_MEMBER_UID].display_name == "Dee" and 777 not in reg.members
    # on_member_update (the gateway event, when the members intent is on): the seat survives, the run keeps its name
    ev, seated = _locked(reg, rs)
    p = seated[0]
    old = p.display_name
    run(OibotGM.on_member_update(idbot, Member(p.discord_id, old, g), Member(p.discord_id, "Nicky", g)))
    assert reg.members[p.discord_id].display_name == "Nicky"
    assert ev.seat_of(p) and ev.seat_of(p.discord_id) and ev.signups[str(p.discord_id)].display_name == old and ev.seat_of(p)[1].signup_name == old
    run(OibotGM.on_member_update(idbot, Member(5, "x", FakeGuild(99, {})), Member(5, "y", FakeGuild(99, {}))))  # an unconfigured server: ignored
    _, itx = run(guild_routing.resolve_button(interaction(idbot, Member(p.discord_id, "Nickier", g)), reg.key))
    assert reg.members[p.discord_id].display_name == "Nickier", "a persistent button's press is a sighting too"


def test_writes_decide_the_rights_live_on_discord_apply_and_the_web_apply(idbot, reg, rs, monkeypatch):
    """PRIV_LIVE is used: ConfigConfirmView's Apply and the site's plain-change apply re-resolve the presser NOW
    (max_age 0), while a preview and a press keep the routing window."""
    from oibot_gm.configops import ConfigRequest
    from oibot_gm.discord_policy import ConfigConfirmView

    seen = []
    real = guild_routing.resolve_role

    async def spy(bot, reg_, who, *, max_age=guild_routing.PRIV_ROUTING):
        seen.append(max_age)
        return await real(bot, reg_, who, max_age=max_age)

    monkeypatch.setattr(guild_routing, "resolve_role", spy)
    g = idbot.guild
    view = SimpleNamespace(reg=reg, requester_id=MEMBER_UID, requester="Mem")
    assert run(ConfigConfirmView._may_press(view, interaction(idbot, g.people[OFFICER_UID]))) == (True, [OFFICER_ROLE])
    assert seen[-1] == guild_routing.PRIV_LIVE
    assert run(idbot.plain_identity(reg, g.people[OFFICER_UID])) == (True, [OFFICER_ROLE]) and seen[-1] == guild_routing.PRIV_ROUTING
    # the web: preview as an officer, lose the role, apply → decided live, refused; the lookup was the live window
    idbot.ctx = SimpleNamespace(provider=object())
    puppet = reg.test_members()[0]
    monkeypatch.setattr(configops, "parse", lambda *a, **k: ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", member=puppet.display_name, value="off")]))
    c = TestClient(web.create_app(idbot), follow_redirects=False)
    c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(OFFICER_UID), "name": "x", "iat": time.time()}))
    H = {"X-Requested-With": "oibot"}
    d = c.post("/api/ops/change", json={"text": "dm off"}, headers=H).json()
    assert d.get("token"), d
    # between the preview and the Apply the gateway loses the officer and the process's cache still says "officer": the
    # request-time check (PRIV_WEB-style, from the cache) lets the request in, the apply re-resolves live and finds no answer
    officer = g.people.pop(OFFICER_UID)
    idbot._member_cache = {(SERVER, OFFICER_UID): (time.time(), officer)}
    seen.clear()
    r = c.post("/api/ops/change", json={"token": d["token"], "apply": True}, headers=H)
    assert r.status_code == 200, r.text
    assert guild_routing.PRIV_LIVE in seen, seen
    d2 = r.json()
    assert d2["applied"] == [] and d2["refused"] and not reg.members[puppet.discord_id].dm_opt_out, d2
    # the same route with the officer back in the server: applied, and the ops line carries the web prefix through ops.audited
    g.people[OFFICER_UID] = officer
    d = c.post("/api/ops/change", json={"text": "dm off"}, headers=H).json()
    d2 = c.post("/api/ops/change", json={"token": d["token"], "apply": True}, headers=H).json()
    assert d2["applied"] and reg.members[puppet.discord_id].dm_opt_out
    assert idbot.ops.lines[-1][1].startswith("[web] Offi: "), idbot.ops.lines[-1]


def test_no_surface_prefix_is_written_by_hand_and_the_board_line_carries_via(reg, rs, monkeypatch):
    """§2.6: the surface is a prefix from `via`, never baked into a line — so an MCP edit through a web route reads [mcp]."""
    assert _src_hits('"[web]', skip=("ops.py",)) == [] and _src_hits('"[mcp]', skip=("ops.py",)) == [], "no literal surface tag in a line"
    ev, seated = _locked(reg, rs)

    class Bot(SeatBot):
        after_board_change = RaidMixin.after_board_change

    bot = Bot(reg, rs)
    team = rc.run_team(reg, ev)
    run(bot.after_board_change(reg, rs, ev, team, [], [seated[0].display_name], "Jon", via="mcp"))
    assert bot.lines[-1] == ("info", f"[mcp] Jon: edited the {ev.key} board -1")
    run(bot.after_board_change(reg, rs, ev, team, [], [seated[1].display_name], "Jon"))
    assert bot.lines[-1] == ("info", f"[web] Jon: edited the {ev.key} board -1"), "the board is the web's by default"
    run(bot.after_board_change(reg, rs, ev, team, [], [seated[2].display_name], "Jon", via="discord"))
    assert bot.lines[-1] == ("info", f"Jon: edited the {ev.key} board -1")
    # a web route reached with the MCP bearer: the pin line is the owner's, prefixed [mcp]
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    fb = FakeBot(reg, rs)
    c = TestClient(web.create_app(fb))
    ev2 = open_test_run(reg, rs, instance="barrow_deeps", days=5)
    pool = reg.test_members()
    join(reg, rs, ev2, pool[:3])
    r = c.post(f"/api/run/{ev2.key}/pin", json={"member": pool[0].display_name, "pin": "in"}, headers={"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"})
    assert r.status_code == 200, r.text
    assert fb.ops.lines[-1] == ("info", f"[mcp] the owner: {ev2.key} {pool[0].display_name} pinned in"), fb.ops.lines
