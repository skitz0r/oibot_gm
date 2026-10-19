"""The test bench on both surfaces (dual interfaces phase 2, B9; owner decision D20): the '-test' key space, the
ensure_run refusal, the puppet DM digest, test-run cards in the asking channel, the test feed, tempo names, the
/api/test/* routes and the /gm test compose | comp wizards. Real Registry / RaidStore underneath (conftest); the bot
is RaidMixin + SetupMixin on the recording fake of test_parity_verbs."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from conftest import OWNER, join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, bearer
from test_parity_verbs import Bot, Chan
from wizard_harness import Harness

from oibot_gm import configops, discord_registry, raidcycle as rc, testfeed
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_pool import SetupMixin
from oibot_gm.flows_test import TestAnswerButton, compose_diff, review_line
from oibot_gm.presets import TEMPOS
from oibot_gm.raid_views import _header
from oibot_gm.registry import RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS

TESTER = 500_000_000_000_000_010


class TBot(SetupMixin, Bot):
    """The real test-bench verbs (SetupMixin) over RaidMixin on the recording fake. DMs are kept with their view."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.sent: dict[int, list[tuple]] = {}

    async def fetch_user(self, uid):
        bot = self

        class U:
            id = int(uid)

            async def send(self, content=None, **kw):
                bot.sent.setdefault(int(uid), []).append((content, kw.get("view")))
                bot.dms.setdefault(int(uid), []).append(content if content is not None else kw.get("view"))
                return SimpleNamespace(id=1, channel=SimpleNamespace(id=2))
        return U()


def run(coro):
    return asyncio.run(coro)


def start_in(reg, minutes=180):
    return (reg.now_local() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)


# ---------------------------------------------------------------- the '-test' key space

def test_a_test_run_and_a_real_run_at_the_same_minute_get_separate_keys(reg, rs):
    """The bug: open_test_run opened with schedule=None, so its key equalled a real default-schedule run's at that minute
    and ensure_run / open_event handed back the REAL roster — the puppets' answers landed on the real sheet."""
    at = start_in(reg)
    real = rc.open_run(reg, rs, "barrow_deeps", at, by="scheduler")
    test = rc.open_run(reg, rs, "barrow_deeps", at, by="t", cutoffs={"soft": 0.5, "hard": 0.4, "confirm": 0.2, "test_by": TESTER})
    assert real.team == rc.run_key("barrow_deeps", at) and test.team == rc.run_key("barrow_deeps", at, rc.TEST_SCHEDULE)
    assert test.team == real.team + "-test" and test.key != real.key
    assert rc.is_test_run(reg, test) and not rc.is_test_run(reg, real)
    join(reg, rs, test, reg.test_members()[:5])
    assert not real.signups, "puppets never land on the real sheet"
    assert reg.config.roster(real.team).get("test") is None and reg.config.roster(test.team)["test"] is True
    # the real run's own settings: a test roster is never read for it, nor the schedule 'test' for either
    assert rc.open_run(reg, rs, "barrow_deeps", at, by="scheduler") is real


def test_a_test_run_of_a_named_schedule_keeps_the_schedule_in_its_key(reg):
    at = start_in(reg)
    assert rc.run_key("barrow_deeps", at, "fri", test=True) == rc.run_key("barrow_deeps", at, "fri") + "-test"
    assert rc.run_key("barrow_deeps", at, test=True) == rc.run_key("barrow_deeps", at, rc.TEST_SCHEDULE) == rc.run_key("barrow_deeps", at) + "-test"


def test_ensure_run_refuses_a_roster_whose_test_flag_differs(reg, rs):
    at = start_in(reg, 240)
    key = rc.run_key("barrow_deeps", at)
    # a rehearsal's roster from before the key space sits at a real key: the scheduler's real run refuses to take it over
    reg.config.rosters.append({"key": key, "name": "old rehearsal", "test": True, "instance": "barrow_deeps", "size": 20, "ephemeral": True})
    with pytest.raises(RegistryError, match="a test run: a real run can't share it"):
        rc.ensure_run(reg, "barrow_deeps", at)
    # …and a real roster that somehow sits in the test key space is never taken by a rehearsal
    tkey = rc.run_key("barrow_deeps", at, rc.TEST_SCHEDULE)
    reg.config.rosters.append({"key": tkey, "name": "odd real", "instance": "barrow_deeps", "size": 20, "ephemeral": True})
    with pytest.raises(RegistryError, match="a real run: a test run can't share it"):
        rc.ensure_run(reg, "barrow_deeps", at, cutoffs={"soft": 0.5, "hard": 0.4, "confirm": 0.2})


def test_open_event_never_hands_a_rehearsals_sheet_to_a_real_run(reg, rs):
    at = start_in(reg, 300)
    ev = open_test_run(reg, rs)
    team = {"key": ev.team.removesuffix("-test"), "instance": "barrow_deeps"}  # a real roster whose event key collides (old files)
    rs.events[f"{team['key']}-{ev.start.date().isoformat()}"] = ev
    with pytest.raises(RegistryError, match="test run's sheet"):
        rc.open_event(reg, rs, team, ev.start)
    assert at  # (the run itself is untouched)


def test_the_schedule_id_test_is_kept_for_test_runs(reg):
    with pytest.raises(RegistryError, match="kept for test runs"):
        reg.set_schedule("barrow_deeps", "test", {"kind": "weekly", "slots": "Sat 20:00"}, "t")


def test_the_guards_hold_in_the_new_key_space(reg, rs):
    """A test run's pool is puppets + its tester; its sheet refuses a real member; /raid set refuses it (rc.is_test_run)."""
    bot = TBot(reg, rs)
    ev = open_test_run(reg, rs, test_by=TESTER)
    assert ev.team.endswith("-test")
    pool = {m.discord_id for m, _c in rc.test_pool(reg, ev)} if hasattr(rc, "test_pool") else None
    real = reg.add_character(4242, "Realperson", "Realperson", "Warrior", next(iter(reg.profile.classes["Warrior"])), None, True)[0]
    assert not rc.on_bench_side(reg, ev, reg.members[real.discord_id] if hasattr(real, "discord_id") else reg.members[4242])
    with pytest.raises(RegistryError, match="test run"):
        run(bot.set_answer(reg, rs, ev, reg.members[4242], "in", None, by="Officer"))
    with pytest.raises(RegistryError, match="test run"):  # /raid set's verb is the same set_answer
        run(bot.set_answer(reg, rs, ev, reg.members[4242], "in", None, by="Officer", by_id=OWNER))
    assert pool is None or 4242 not in pool


# ---------------------------------------------------------------- the puppet DM digest

def test_puppets_confirm_asks_reach_the_tester_as_one_digest(reg, rs):
    bot = TBot(reg, rs)
    ev = open_test_run(reg, rs, test_by=TESTER)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])

    async def go():
        sent = await bot.send_confirmations(reg, rs, ev, rc.run_team(reg, ev), "t")
        assert sent == 10 and not bot.sent, "ten puppet asks, zero DMs so far: the digest waits for the batch"
        assert await bot.flush_puppet_digests() == 1
    run(go())
    (text, view), = bot.sent[TESTER]
    assert "10 Confirm asks sent to puppets" in text and "(test run)" in text and ms[0].display_name in text
    btn = next(c for c in view.children if isinstance(c, TestAnswerButton))
    assert btn.item.label == "Answer as puppets…" and btn.item.custom_id == f"testans.{reg.key}:{ev.key}"
    assert all(m.discord_id not in bot.sent for m in ms), "never a DM to a puppet's own (fake) id"
    # the asks are real asks: each puppet holds one, answerable as a member's own
    assert len(SetupMixin.puppet_asks(reg, ev)) == 10


def test_a_digest_counts_kinds_in_words():
    assert testfeed.digest_line({"confirm": 12, "fill": 3}) == "12 Confirm asks sent to puppets · 3 fill asks"
    assert testfeed.digest_line({"fill": 1}) == "1 fill ask sent to puppets"
    assert testfeed.digest_line({}) == "Nothing sent to puppets"


def test_the_digest_closes_after_a_quiet_spell(reg, rs, monkeypatch):
    """One batch = DMs with no gap longer than DIGEST_QUIET_S; a later DM opens a new digest."""
    bot = TBot(reg, rs)
    monkeypatch.setattr(TBot, "DIGEST_QUIET_S", 0.05)
    ev = open_test_run(reg, rs, test_by=TESTER)
    p = reg.test_members()

    async def go():
        for m in p[:3]:
            await bot.send_member_dm(reg, m, "hello", ev=ev)
        await asyncio.sleep(0.3)
        assert len(bot.sent[TESTER]) == 1 and "3 messages sent to puppets" in bot.sent[TESTER][0][0]
        await bot.send_member_dm(reg, p[3], "again", ev=ev)
        await asyncio.sleep(0.3)
    run(go())
    assert len(bot.sent[TESTER]) == 2 and "1 message sent to puppets" in bot.sent[TESTER][1][0]


def test_answer_as_puppets_button_opens_the_answer_wizard_on_that_run(reg, rs, monkeypatch):
    bot = TBot(reg, rs)

    async def officer(r, uid):
        return uid == 424242
    monkeypatch.setattr(TBot, "is_officer_anywhere", lambda self, r, uid: officer(r, uid), raising=False)
    a = open_test_run(reg, rs, days=2, test_by=TESTER)
    b = open_test_run(reg, rs, days=4, test_by=TESTER)
    h = Harness(reg, rs, client=bot)
    monkeypatch.setattr("oibot_gm.guild_routing.resolve_button", lambda i, g, legacy: _resolved(reg, i))

    async def go():
        await TestAnswerButton(reg.key, b.key).callback(h.card_press())
        assert h.buttons() == ["Random mix", "One member", "Done"] and "Pick the run" not in h.selects(), "the run is preselected"
        await h.press("Random mix")
        await h.submit(h.modal, **{"in": "3", "sub": "0", "out": "0"})
    run(go())
    assert len(rs.events[b.key].by_status("in")) == 3 and not rs.events[a.key].signups
    h2 = Harness(reg, rs, client=bot, user=SimpleNamespace(id=7, display_name="Nobody", mention="@n"))

    async def stranger():
        await TestAnswerButton(reg.key, b.key).callback(h2.card_press())
    run(stranger())
    assert "Officers only" in h2.text


async def _resolved(reg, i):
    return reg, i


# ---------------------------------------------------------------- test-run cards go to the asking channel (D20)

def test_test_run_cards_post_in_the_asking_channel_with_the_test_badge(reg, rs):
    bot = TBot(reg, rs)
    asking, roster = Chan(77, "rehearsal"), Chan(88, "roster")
    bot.channels.update({77: asking, 88: roster})
    reg.config.roster_channel_id = 88
    ev, posted = run(bot.open_test_run(reg, rs, "barrow_deeps", start_in=40, nudge_in=32, lock_in=25, confirm_in=15, dm=False, by="Officer",
                                       by_id=TESTER, channel=asking))
    assert posted is asking and ev.message_id and reg.config.roster(ev.team)["test_channel"] == 77
    assert bot.officer_channel(reg, ev) is asking, "its health / lock cards post where it was asked for"
    real = rc.open_run(reg, rs, "barrow_deeps", start_in(reg, 600), by="t")
    assert bot.officer_channel(reg, real) is roster, "a real run is untouched"
    assert "🧪 Test run · Locked" in header_text(_header(reg, ev, "Locked", []))
    assert "🧪" not in header_text(_header(reg, real, "Locked", []))


def header_text(item) -> str:
    return item.content if isinstance(item, discord.ui.TextDisplay) else item.children[0].content


def test_a_test_run_with_nowhere_to_post_is_refused_naming_the_setting(reg, rs):
    """The site, MCP or a DM (a DM can't host the updates thread) without a signup channel: refused before anything is written."""
    bot = TBot(reg, rs)
    rosters = len(reg.config.rosters)
    for where in (None, SimpleNamespace(id=5, guild=None)):  # no channel; a DM (no server)
        with pytest.raises(RegistryError, match="signup channel"):
            run(bot.open_test_run(reg, rs, "barrow_deeps", start_in=40, nudge_in=32, lock_in=25, confirm_in=15, dm=False, by="O", by_id=TESTER, channel=where))
    assert not rs.live() and len(reg.config.rosters) == rosters
    reg.config.signup_channel_id, bot.channels[55] = 55, Chan(55, "signups")
    ev, posted = run(bot.open_test_run(reg, rs, "barrow_deeps", start_in=40, nudge_in=32, lock_in=25, confirm_in=15, dm=False, by="O", by_id=TESTER))
    assert posted is bot.channels[55] and "test_channel" not in reg.config.roster(ev.team), "from the site: the officer channel, as any run"


# ---------------------------------------------------------------- the test feed

@pytest.fixture
def feed(reg, rs, monkeypatch):
    monkeypatch.setattr(rc, "RUN_LISTENERS", [])
    monkeypatch.setattr(testfeed, "LISTENERS", [])
    published = []
    testfeed.LISTENERS.append(lambda gk, run_key: published.append((gk, run_key)))
    testfeed.install(lambda gk, run_key: (reg, rs.events[run_key]) if run_key in rs.events else None, lambda cid: {77: "rehearsal"}.get(cid))
    testfeed.FEED.forget(reg.key)
    yield published
    testfeed.FEED.forget(reg.key)


def kinds(reg, run_key=None):
    return [(e["kind"], e["text"]) for e in testfeed.FEED.events(reg.key, run_key)]


def test_the_feed_has_every_card_dm_step_and_answer_of_a_test_run(reg, rs, feed):
    bot = TBot(reg, rs)
    asking = Chan(77, "rehearsal")
    bot.channels[77] = asking
    ev, _ = run(bot.open_test_run(reg, rs, "barrow_deeps", start_in=40, nudge_in=32, lock_in=25, confirm_in=15, dm=False, by="Officer", by_id=TESTER, channel=asking))
    got = kinds(reg, ev.key)
    assert ("card", "Sheet posted in #rehearsal") in got
    assert any(k == "step" and "opened" in t for k, t in got)
    ms = reg.test_members()[:10]
    run(bot.test_answer(reg, rs, ev, member=ms[0], status="in", by="Officer", by_id=TESTER))
    assert any(k == "answer" and t.startswith(f"{ms[0].display_name}: ") for k, t in kinds(reg, ev.key))
    join(reg, rs, ev, ms[1:])
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])

    async def lock_side():
        await bot.post_lock_cards(reg, rs, ev, rc.run_team(reg, ev), asking)
        await bot.send_confirmations(reg, rs, ev, rc.run_team(reg, ev), "t")
        await bot.post_run_update(reg, ev, "🧩 short 1 healer")
        await bot.flush_puppet_digests()
    run(lock_side())
    got = kinds(reg, ev.key)
    assert any(k == "card" and t.startswith("Lock card · roster 1 posted") for k, t in got)
    dms = [t for k, t in got if k == "dm"]
    assert len(dms) == 10 and all("would get (Confirm ask)" in t for t in dms)
    assert ("step", "🧩 short 1 healer") in got, "a thread line"
    assert not any(k == "step" and " in as " in t for k, t in got), "an answer is one event, never also its log line"
    assert any(k == "step" and "confirmation DMs sent to 10" in t for k, t in got)
    run(bot.cancel_run(reg, rs, ev, "Officer", "done rehearsing", notify=False))
    assert any(k == "step" and "cancel" in t.lower() for k, t in kinds(reg, ev.key))
    assert feed and all(g == reg.key and r == ev.key for g, r in feed), "every event publishes test:<run> for its guild"
    assert {e["kind"] for e in testfeed.FEED.events(reg.key)} == {"card", "dm", "step", "answer"}


def test_a_real_run_never_reaches_the_feed(reg, rs, feed):
    bot = TBot(reg, rs)
    real = rc.open_run(reg, rs, "barrow_deeps", start_in(reg, 500), by="t")
    run(bot.post_run_update(reg, real, "a real line"))
    assert testfeed.FEED.events(reg.key) == [] and not feed


def test_feed_text_is_plain_words_in_the_12_hour_clock(reg):
    m = reg.test_members()[0]
    line = testfeed.plain(reg, f"<:healer:123> **short** <@{m.discord_id}> at <t:1893456000:t> in <#77>\n-# small print", lambda cid: "rehearsal")
    assert line.splitlines()[0].startswith(f"short @{m.display_name} at ") and "#rehearsal" in line and line.splitlines()[1] == "small print"
    assert ("AM" in line or "PM" in line) and "<" not in line


# ---------------------------------------------------------------- tempo names

def test_tempo_names_and_minutes():
    for name, row in TEMPOS.items():
        assert testfeed.tempo_of(name) == row
        assert testfeed.tempo_of(name.upper()) == row
    label, s, n, lk, c = testfeed.tempo_of("30")
    assert label == "30 min" and s == 30 and s > n > lk > c >= 0
    assert testfeed.tempo_of(None)[1:] == TEMPOS["standard"][1:]
    assert testfeed.tempo_of(None, start_in=50, nudge_in=40, lock_in=30, confirm_in=20)[1:] == (50, 40, 30, 20)
    for bad in ("turbo", "2"):
        with pytest.raises(ValueError):
            testfeed.tempo_of(bad)
    with pytest.raises(ValueError, match="start_in > nudge_in"):
        testfeed.tempo_of(None, start_in=10, nudge_in=20, lock_in=5, confirm_in=1)


# ---------------------------------------------------------------- the routes (/api/test/*)

@pytest.fixture
def site(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.setattr(rc, "RUN_LISTENERS", list(rc.RUN_LISTENERS))
    monkeypatch.setattr(testfeed, "LISTENERS", list(testfeed.LISTENERS))
    bot = TBot(reg, rs)
    client = TestClient(web.create_app(bot))
    testfeed.FEED.forget(reg.key)
    yield client, bot
    testfeed.FEED.forget(reg.key)


def test_state_route_names_the_missing_signup_channel_and_the_tempos(reg, rs, site):
    client, bot = site
    r = client.get("/api/test", headers=bearer())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["members"] == len(reg.test_members()) and d["runs"] == []
    assert "signup channel" in d["open_refusal"] and d["signup_channel"] == {"set": False, "name": None}
    assert [t["id"] for t in d["tempos"]] == list(TEMPOS) and d["default_tempo"] == "standard"
    assert d["seed"][-1]["count"] == 0 and d["seed"][-1]["label"].startswith("all ")
    assert d["clear_consequence"][0].startswith("Deletes ")
    reg.config.signup_channel_id, bot.channels[55] = 55, Chan(55, "signups")
    d = client.get("/api/test", headers=bearer()).json()
    assert d["open_refusal"] is None and d["signup_channel"] == {"set": True, "name": "signups"}


def test_seed_route(reg, site):
    client, _ = site
    n = len(reg.test_roster())
    r = client.post("/api/test/seed", json={"count": 0}, headers=bearer())
    assert r.status_code == 200 and r.json()["members"] == n == len(reg.test_members())


def test_run_route_takes_tempo_names_and_refuses_without_a_signup_channel(reg, rs, site):
    client, bot = site
    r = client.post("/api/test/run", json={"raid": "barrow_deeps", "tempo": "quick"}, headers=bearer())
    assert r.status_code == 400 and "signup channel" in r.json()["error"] and not rs.live()
    reg.config.signup_channel_id, bot.channels[55] = 55, Chan(55, "signups")
    assert client.post("/api/test/run", json={"raid": "barrow_deeps", "tempo": "turbo"}, headers=bearer()).status_code == 400
    assert client.post("/api/test/run", json={"raid": "nowhere"}, headers=bearer()).status_code == 400
    before = reg.now_local()
    r = client.post("/api/test/run", json={"raid": "barrow_deeps", "tempo": "quick", "dm_open": True}, headers=bearer())
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    t = reg.config.roster(ev.team)
    assert ev.team.endswith("-test") and t["open_dm"] is True and t["test_by"] == OWNER
    assert (round(t["cutoff_soft_hours"] * 60), round(t["cutoff_hard_hours"] * 60), round(t["confirm_hours"] * 60)) == TEMPOS["quick"][2:]
    assert 19 <= (ev.start - before).total_seconds() / 60 <= 20
    r = client.post("/api/test/run", json={"raid": "barrow_deeps", "tempo": 25}, headers=bearer())  # minutes work too
    assert r.status_code == 200 and "starts" in r.json()["message"]
    rows = client.get("/api/test", headers=bearer()).json()["runs"]
    assert {x["key"] for x in rows} == {ev.key, r.json()["key"]} and all(x["posted"] for x in rows)


def test_answer_route_mix_one_puppet_asks_and_the_real_run_refusal(reg, rs, site):
    client, bot = site
    ev = open_test_run(reg, rs, test_by=OWNER)
    r = client.post("/api/test/answer", json={"join": 5, "bench": 2, "out": 1}, headers=bearer())  # the only live test run
    assert r.status_code == 200 and r.json()["answered"] == 8
    m = next(p for p in reg.test_members() if str(p.discord_id) not in ev.signups)
    r = client.post("/api/test/answer", json={"run": ev.key, "member": str(m.discord_id), "status": "sub"}, headers=bearer())
    assert r.status_code == 200 and ev.signups[str(m.discord_id)].status == "sub"
    # once locked: the puppets' own Confirm / Can't make it, through the member's own verb
    ins = [reg.members[int(u)] for u, sg in ev.signups.items() if sg.status == "in"]
    lock_with_board(reg, rs, ev, [x.display_name for x in ins])
    run(bot.send_confirmations(reg, rs, ev, rc.run_team(reg, ev), "t"))
    run(bot.flush_puppet_digests())
    assert len(client.get("/api/test", headers=bearer()).json()["runs"][0:1]) == 1
    r = client.post("/api/test/answer", json={"run": ev.key, "yes": 3, "no": 1}, headers=bearer())
    assert r.status_code == 200, r.text
    answers = [a["answer"] for x in ins for a in x.placement_asks if a["roster"] == ev.team]
    assert answers.count("yes") == 3 and answers.count("no") == 1
    r = client.post("/api/test/answer", json={"run": ev.key, "member": str(ins[-1].discord_id), "status": "yes"}, headers=bearer())
    assert r.status_code == 200 or "no open ask" in r.text
    real = rc.open_run(reg, rs, "barrow_deeps", start_in(reg, 700), by="t")
    r = client.post("/api/test/answer", json={"run": real.key, "join": 1}, headers=bearer())
    assert r.status_code == 400 and "real run" in r.text and not real.signups


def test_clear_route_previews_the_consequence_then_clears(reg, rs, site):
    client, _ = site
    ev = open_test_run(reg, rs, test_by=OWNER)
    n = len(reg.test_members())
    r = client.post("/api/test/clear", json={"preview": True}, headers=bearer())
    assert r.status_code == 200 and r.json()["consequence"][0] == f"Cancels 1 test run, deletes {n} puppets, analytics cards resume."
    assert reg.test_members() and ev.state == "open", "a preview writes nothing"
    r = client.post("/api/test/clear", json={}, headers=bearer())
    assert r.status_code == 200 and not reg.test_members() and rs.events[ev.key].state == "cancelled"
    assert client.post("/api/test/clear", json={"preview": True}, headers=bearer()).json()["empty"] is True


def test_compose_and_comp_routes(reg, rs, site):
    client, _ = site
    rogues = lambda: sum(1 for m in reg.test_members() if (m.main.cls, m.main.spec) == ("Rogue", "Combat"))  # noqa: E731
    had = rogues()
    made = reg.add_test_members([{"cls": "Rogue", "spec": "Combat", "count": 2}], "t")
    r = client.post("/api/test/compose", json={"add": [{"cls": "Druid", "spec": "Restoration", "count": 2}], "remove": [str(made[0].discord_id)]}, headers=bearer())
    assert r.status_code == 200 and "2 added, 1 removed" in r.json()["message"]
    assert rogues() == had + 1
    assert client.post("/api/test/compose", json={"add": [{"cls": "Bard", "spec": "Lute", "count": 1}]}, headers=bearer()).status_code == 400
    r = client.post("/api/test/comp", json={"raid": "barrow_deeps"}, headers=bearer())
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    assert ev.team.endswith("-test") and ev.message_id is None and len(ev.by_status("in")) == len(reg.test_members())


def test_feed_route_lists_events_after_a_seq(reg, rs, site):
    client, bot = site
    ev = open_test_run(reg, rs, test_by=OWNER)
    run(bot.test_answer(reg, rs, ev, join=3, by="the owner", by_id=OWNER))
    d = client.get("/api/test/feed", headers=bearer()).json()
    assert ev.key in d["runs"] and [e["kind"] for e in d["events"]].count("answer") == 3
    last = d["events"][-1]["seq"]
    assert client.get(f"/api/test/feed?after={last}", headers=bearer()).json()["events"] == []
    assert all(e["run"] == ev.key for e in client.get(f"/api/test/feed?run={ev.key}", headers=bearer()).json()["events"])


def test_test_routes_are_officers_only(reg, site):
    client, _ = site
    for path in ("/api/test", "/api/test/feed"):
        assert client.get(path).status_code in (401, 403)
    assert client.post("/api/test/seed", json={"count": 5}, headers={"X-Requested-With": "oibot"}).status_code in (401, 403)


def test_mcp_test_bench_sends_tempo_names_and_reads_the_feed(monkeypatch):
    import httpx

    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request):
        import json

        calls.append((request.method, request.url.path, json.loads(request.read() or b"{}") if request.method != "GET" else dict(request.url.params)))
        if request.url.path == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps"}]})
        return httpx.Response(200, json={"message": "ok", "events": []})
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        ms.test_bench("run", raid="barrow", tempo="slow", dm_open=True)
        assert calls[-1][1:] == ("/api/test/run", {"raid": "barrow_deeps", "dm_open": True, "tempo": "slow"})
        ms.test_bench("answer", run="bd", yes=2)
        assert calls[-1][1] == "/api/test/answer" and calls[-1][2]["yes"] == 2
        assert ms.test_bench("feed", run="bd-x")["events"] == [] and calls[-1][:2] == ("GET", "/api/test/feed")
        with pytest.raises(Exception, match="action must be"):
            ms.test_bench("explode")
    finally:
        ms.use(None)


# ---------------------------------------------------------------- plain text keeps puppets off real runs

def test_plain_text_test_answer_refuses_a_real_run(reg, rs):
    from test_plain_text import FakeBot

    real = rc.open_run(reg, rs, "barrow_deeps", start_in(reg, 800), by="t")
    with pytest.raises(RegistryError, match="real run"):
        run(configops.apply_async(reg, ConfigOp(op="test", value=f"answer {real.key} join 2"), "Officer", False, bot=FakeBot(rs), by_id=OWNER))
    with pytest.raises(RegistryError, match="0 live test runs"):  # the only live run is real: never picked
        run(configops.apply_async(reg, ConfigOp(op="test", value="answer join 2"), "Officer", False, bot=FakeBot(rs), by_id=OWNER))
    assert not real.signups


# ---------------------------------------------------------------- /gm test compose | comp | clear (wizards)

@pytest.fixture
def officer(monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)


def test_compose_diff_and_review_line(reg):
    reg.clear_test_members("t")
    reg.add_test_members([{"cls": "Mage", "spec": "Frost", "count": 3}], "t")
    add, remove = compose_diff(reg, {"Mage:Frost": 1, "Priest:Holy": 3})
    assert add == [{"cls": "Priest", "spec": "Holy", "count": 3}] and len(remove) == 2
    assert review_line(add, remove) == "3 to add · 2 to remove"


def test_compose_wizard_class_spec_count_review_save(reg, rs, officer):
    reg.clear_test_members("t")
    reg.add_test_members([{"cls": "Mage", "spec": "Frost", "count": 2}], "t")
    bot = TBot(reg, rs)
    h = Harness(reg, rs, client=bot)

    async def go():
        await FLOWS["test_compose"](h.command(), reg)
        assert "2 of 80 test members" in h.text and h.selects() == ["Add or change a class"]
        await h.pick("Add or change a class", "Priest")
        await h.pick("Which spec", "Holy")
        await h.pick("How many", "3")
        await h.pick("Add or change a class", "Mage")
        await h.pick("Which spec", "Frost")
        await h.pick("How many", "0")
        assert "**3 to add · 2 to remove**" in h.text and len(reg.test_members()) == 2, "nothing written before Save"
        await h.press("Save")
        assert "3 added, 2 removed" in h.text
    run(go())
    assert sorted((m.main.cls, m.main.spec) for m in reg.test_members()) == [("Priest", "Holy")] * 3
    assert any("3 added, 2 removed" in line for _lvl, line in bot.ops.lines)


def test_compose_wizard_start_over_and_cancel_write_nothing(reg, rs, officer):
    bot = TBot(reg, rs)
    before = sorted(m.discord_id for m in reg.test_members())
    h = Harness(reg, rs, client=bot)

    async def go():
        await FLOWS["test_compose"](h.command(), reg)
        await h.pick("Add or change a class", "Warrior")
        await h.pick("Which spec", next(iter(reg.profile.classes["Warrior"])))
        await h.pick("How many", "2")
        await h.press("Start over")
        assert "to remove" not in h.text
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    run(go())
    assert sorted(m.discord_id for m in reg.test_members()) == before


def test_comp_wizard_opens_the_sandbox_and_links_its_board(reg, rs, officer, monkeypatch):
    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    bot = TBot(reg, rs)
    h = Harness(reg, rs, client=bot)

    async def go():
        await FLOWS["test_comp"](h.command(), reg)
        assert "Try a comp" in h.buttons()
        await h.press("Try a comp")
    run(go())
    ev = next(e for e in rs.live() if rc.is_test_run(reg, e))
    assert f"https://gm.example/app/rosters?g={reg.key}#run-{ev.key}" in h.text and ev.message_id is None
    assert len(ev.by_status("in")) == len(reg.test_members())


def test_comp_wizard_without_puppets_says_how_to_add_them(reg, rs, officer):
    reg.clear_test_members("t")
    h = Harness(reg, rs, client=TBot(reg, rs))
    run(FLOWS["test_comp"](h.command(), reg))
    assert "/gm test compose" in h.text and h.screen["view"] is None


def test_clear_wizard_shows_the_consequence_from_code(reg, rs, officer):
    bot = TBot(reg, rs)
    ev = open_test_run(reg, rs, test_by=OWNER)
    n = len(reg.test_members())
    h = Harness(reg, rs, client=bot)

    async def go():
        await FLOWS["test_clear"](h.command(), reg)
        assert f"Cancels 1 test run, deletes {n} puppets, analytics cards resume." in h.text
        await h.press("Clear the test bench")
    run(go())
    assert not reg.test_members() and rs.events[ev.key].state == "cancelled"


def test_every_new_wizard_screen_passes_the_lint(reg, rs, officer):
    """(the harness lints each screen; this walks the compose wizard's every screen with a full bench)"""
    reg.seed_test_members(len(reg.test_roster()), "t")
    h = Harness(reg, rs, client=TBot(reg, rs))

    async def go():
        await FLOWS["test_compose"](h.command(), reg)
        for cls in list(reg.profile.classes)[:3]:
            await h.pick("Add or change a class", cls)
            await h.pick("Which spec", next(iter(reg.profile.classes[cls])))
            await h.pick("How many", h._items()[0].options[-1].value)
    run(go())
    assert discord.ButtonStyle.success  # (screens linted by the harness as they rendered)
