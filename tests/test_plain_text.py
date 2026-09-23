"""The plain-text control surface: every action the site and the slash commands expose is a ConfigOp. describe() says
what will happen; apply() does the registry/store part; apply_async() runs the Discord side effects through the bot's
verbs (here: a fake bot that records the calls). No LLM anywhere."""
from __future__ import annotations

import asyncio
import types

import pytest

from conftest import join, lock_with_board, open_test_run

from oibot_gm import configops, help as help_mod, raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError


class FakeBot:
    """Just the verbs configops.apply_async calls, each recording its call and doing the minimum to the store."""

    def __init__(self, rs):
        self.calls: list[tuple] = []
        self.raids = types.SimpleNamespace(store=lambda reg: rs)
        self.ico = lambda kind, key: ""

    async def set_answer(self, reg, rs, ev, m, status, character, by):
        self.calls.append(("set_answer", ev.key, m.display_name, status, character, by))
        s = rc.set_signup(reg, rs, ev, m, character, status, source="officer")
        return f"✅ {m.display_name}: **{s.character}** {rc.LABELS[status]}"

    async def lock_run(self, reg, rs, ev, by):
        self.calls.append(("lock_run", ev.key, by))
        ev.state, ev.locked_at = "locked", rc.now()
        rs.save(ev, "locked (fake)")
        return f"{ev.key}: locked by {by}"

    async def cancel_run(self, reg, rs, ev, by, reason):
        self.calls.append(("cancel_run", ev.key, by, reason))
        ev.state = "cancelled"
        rs.save(ev, "cancelled (fake)")
        return f"Cancelled {ev.key}" + (f": {reason}" if reason else "")

    async def run_fill(self, reg, rs, ev, team, by):
        self.calls.append(("run_fill", ev.key, by))
        return [], rc.needs(reg, ev, team)

    async def post_run_update(self, reg, ev, text):
        self.calls.append(("post_run_update", ev.key, text))

    async def after_board_change(self, reg, rs, ev, team, added, removed, by):
        self.calls.append(("after_board_change", ev.key, [s.display_name for s in added], list(removed), by))

    async def absence_cleared(self, reg, m, a, by):
        self.calls.append(("absence_cleared", m.display_name, a.start, by))
        return ["sheet re-opened"]

    async def open_run_and_post(self, reg, rs, instance, start, by):
        self.calls.append(("open_run_and_post", instance, start.isoformat(), by))
        return rc.open_run(reg, rs, instance, start, by=by)

    async def test_bench_clear(self, reg, by):
        self.calls.append(("test_bench_clear", by))
        gone = reg.clear_test_members(by)
        return f"{gone} test member(s) removed, 0 test run(s) cancelled"

    async def post_sheet(self, reg, rs, ev, channel):
        self.calls.append(("post_sheet", ev.key))

    def get_channel(self, cid):
        return None


def run(coro):
    return asyncio.run(coro)


def names(bot: FakeBot) -> list[str]:
    return [c[0] for c in bot.calls]


# ---------------------------------------------------------------- schema

def test_schema_stays_small():
    assert len(ConfigOp.model_fields) <= 13, "the structured-output compiler rejects schemas past ~14 fields"
    for op in ("run_open", "run_answer", "run_lock", "run_cancel", "run_fill", "run_strategy", "run_autofill", "run_board", "character", "absence_clear", "dm", "test"):
        assert op in ConfigOp.model_fields["op"].description
        assert op + ":" in configops.SCHEMA_TEXT or op + " " in configops.SCHEMA_TEXT
    assert "tonight" in configops.SCHEMA_TEXT and "several ops" in configops.SCHEMA_TEXT


def test_member_by_character_name(reg):
    m = reg.test_members()[0]
    assert configops._member(reg, m.main.name) is m
    assert configops._member(reg, m.display_name.upper()) is m
    assert configops._member(reg, f"<@{m.discord_id}>") is m
    assert configops._member(reg, "Nobody") is None


def test_live_event_by_raid_id(reg, rs):
    ev = open_test_run(reg, rs)
    assert configops._live_event(reg, "barrow_deeps")[1].key == ev.key
    assert configops._live_event(reg, reg.raid_def("barrow_deeps")["name"])[1].key == ev.key
    open_test_run(reg, rs, days=10)
    with pytest.raises(RegistryError, match="2 live runs"):
        configops._live_event(reg, "barrow_deeps")
    with pytest.raises(RegistryError, match="no live run"):
        configops._live_event(reg, "hyjal_summit_forever")


# ---------------------------------------------------------------- describe

def test_describe_run_ops(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:6])
    d = lambda **kw: configops.describe(reg, ConfigOp(**kw))  # noqa: E731
    assert d(op="run_lock", target=ev.key).startswith(f"lock ") and ev.key in d(op="run_lock", target=ev.key) and "confirmation DMs" in d(op="run_lock", target=ev.key)
    assert "6 joined" in d(op="run_lock", target=ev.key)
    assert d(op="run_answer", target=ev.team, member=ms[0].display_name, value="bench").endswith(f"{ms[0].display_name} → Bench (now Join as {ms[0].main.label})")
    assert "hasn't answered" in d(op="run_answer", target=ev.key, member=ms[9].display_name, value="join")
    assert "join | bench | no thanks" in d(op="run_answer", target=ev.key, member=ms[0].display_name, value="maybe")
    assert d(op="run_cancel", target=ev.key, reason="server down").startswith("cancel ") and "server down" in d(op="run_cancel", target=ev.key, reason="server down")
    assert "balanced → rotation" in d(op="run_strategy", target=ev.key, value="rotation")
    assert "strategy is one of" in d(op="run_strategy", target=ev.key, value="random")
    assert "auto-fill the board" in d(op="run_autofill", target=ev.key)
    assert "works after lock" in d(op="run_fill", target=ev.key, value="preview")
    board = d(op="run_board", target=ev.key, value=f"{ms[0].display_name}, {ms[1].display_name} | {ms[2].display_name}")
    assert "board →" in board and "layout the lock will use" in board
    assert "not joined on this sheet: Nobody" in d(op="run_board", target=ev.key, value="Nobody")
    assert "no live run bd-0101-0000" in d(op="run_lock", target="bd-0101-0000")
    o = d(op="run_open", target="barrow_deeps", value="2030-01-08 19:30")
    assert o.startswith("open Barrow Deeps") and "bd-0108-1930-2030-01-08" in o and "no signup channel" in o
    assert "unknown raid" in d(op="run_open", target="nowhere")
    assert "time looks like" in d(op="run_open", target="barrow_deeps", value="tonight")


def test_describe_member_and_test_ops(reg, rs):
    m = reg.test_members()[0]
    d = lambda **kw: configops.describe(reg, ConfigOp(**kw))  # noqa: E731
    assert d(op="character", member=m.display_name, character="Jonny", field="add", value="Mage Frost") == f"{m.display_name}: add Jonny (Mage Frost, alt)"
    assert d(op="character", member=m.display_name, character=m.main.label, field="spec", value="Arms") == f"{m.main.label}: spec {m.main.spec} → Arms"
    assert d(op="character", member=m.display_name, character=m.main.label, field="retire") == f"{m.display_name}: retire {m.main.label} (their main; the next character becomes main)"
    assert "class is fixed" in d(op="character", member=m.display_name, character=m.main.label, field="cls", value="Mage")
    assert "has no active character named Ghost" in d(op="character", member=m.display_name, character="Ghost", field="spec", value="Arms")
    assert "unknown member Nobody" in d(op="character", member="Nobody", character="X", field="retire")
    assert d(op="dm", member=m.display_name, value="off").startswith(f"{m.display_name}: DMs on → off")
    assert "dm is on | off" in d(op="dm", member=m.display_name, value="maybe")
    reg.add_absence(m.discord_id, "2030-03-01", "2030-03-03", None, "t")
    assert d(op="absence_clear", member=m.display_name) == f"{m.display_name}: clear absence 2030-03-01 → 2030-03-03 — open sheets it had pre-filled re-open for them (a seat freed on a locked run is not handed back)"
    assert "no absence starting 2030-04-01" in d(op="absence_clear", member=m.display_name, start="2030-04-01")
    assert d(op="test", value="seed 5").startswith("test bench: seed 5 puppet member(s) (now 16)")
    assert "compressed Barrow Deeps run" in d(op="test", value="run barrow_deeps")
    assert "unknown raid" in d(op="test", value="run nowhere")
    assert d(op="test", value="clear").startswith("test bench: clear — cancel 0 test run(s) and delete 16 puppet")
    assert "'seed N'" in d(op="test", value="explode")


# ---------------------------------------------------------------- apply / apply_async

def test_run_answer(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="run_answer", target=ev.key, member=ms[0].display_name, value="join"), "officer", is_owner=False, raids=rs)
    out = run(configops.apply_async(reg, ConfigOp(op="run_answer", target=ev.key, member=ms[0].display_name, value="no thanks"), "Officer", False, bot=bot))
    assert bot.calls == [("set_answer", ev.key, ms[0].display_name, "out", None, "Officer")]
    assert out == f"{ms[0].display_name}: {ms[0].main.label} No thanks"
    assert rs.events[ev.key].signups[str(ms[0].discord_id)].status == "out"
    # by character name, by raid id, with a character
    run(configops.apply_async(reg, ConfigOp(op="run_answer", target="barrow_deeps", member=ms[1].main.name, value="Bench", character=ms[1].main.label), "Officer", False, bot=bot))
    assert bot.calls[-1] == ("set_answer", ev.key, ms[1].display_name, "sub", ms[1].main.label, "Officer")
    with pytest.raises(RegistryError, match="unknown member"):
        run(configops.apply_async(reg, ConfigOp(op="run_answer", target=ev.key, member="Nobody", value="join"), "Officer", False, bot=bot))


def test_run_lock_and_cancel(reg, rs):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:10])
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="run_lock", target=ev.key), "officer", is_owner=False, raids=rs)
    assert run(configops.apply_async(reg, ConfigOp(op="run_lock", target=ev.team), "Officer", False, bot=bot)) == f"{ev.key}: locked by Officer"
    assert bot.calls == [("lock_run", ev.key, "Officer")]
    with pytest.raises(RegistryError, match="already locked"):
        run(configops.apply_async(reg, ConfigOp(op="run_lock", target=ev.key), "Officer", False, bot=bot))
    assert "already locked" in configops.describe(reg, ConfigOp(op="run_lock", target=ev.key))
    assert run(configops.apply_async(reg, ConfigOp(op="run_cancel", target=ev.key, reason="server down"), "Officer", False, bot=bot)) == f"Cancelled {ev.key}: server down"
    assert bot.calls[-1] == ("cancel_run", ev.key, "Officer", "server down")
    with pytest.raises(RegistryError, match="no live run"):
        run(configops.apply_async(reg, ConfigOp(op="run_cancel", target=ev.key), "Officer", False, bot=bot))


def test_run_open(reg, rs):
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="run_open", target="barrow_deeps", value="2030-01-08 19:30"), "officer", is_owner=False)
    out = run(configops.apply_async(reg, ConfigOp(op="run_open", target="barrow_deeps", value="2030-01-08 19:30"), "Officer", False, bot=bot))
    assert out.startswith("opened bd-0108-1930-2030-01-08") and "no signup channel" in out
    assert bot.calls[0][:2] == ("open_run_and_post", "barrow_deeps") and bot.calls[0][2].startswith("2030-01-08T19:30")
    assert rs.for_team("bd-0108-1930").state == "open"
    with pytest.raises(RegistryError, match="no slots yet"):
        run(configops.apply_async(reg, ConfigOp(op="run_open", target="barrow_deeps"), "Officer", False, bot=bot))


def test_run_strategy_and_board_open(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:5])
    assert configops.apply(reg, ConfigOp(op="run_strategy", target=ev.key, value="first"), "officer", is_owner=False, raids=rs) == f"{ev.key}: split strategy first"
    assert rc.RaidStore(reg.store, reg.key).events[ev.key].split_strategy == "first"
    with pytest.raises(RegistryError, match="strategy is one of"):
        configops.apply(reg, ConfigOp(op="run_strategy", target=ev.key, value="chaos"), "officer", is_owner=False, raids=rs)
    layout = f"{ms[0].display_name}, {ms[1].display_name} | {ms[2].display_name}"
    assert configops.apply(reg, ConfigOp(op="run_board", target=ev.key, value=layout), "officer", is_owner=False, raids=rs) == f"{ev.key}: board = {layout}"
    assert rs.events[ev.key].layout == [[ms[0].display_name, ms[1].display_name], [ms[2].display_name]]
    with pytest.raises(RegistryError, match="not joined on this sheet: Nobody"):
        configops.apply(reg, ConfigOp(op="run_board", target=ev.key, value="Nobody"), "officer", is_owner=False, raids=rs)
    bot = FakeBot(rs)  # before lock the board is store-only: the bot isn't involved
    assert run(configops.apply_async(reg, ConfigOp(op="run_board", target=ev.key, value=""), "Officer", False, bot=bot)) == f"{ev.key}: board = cleared"
    assert bot.calls == [] and rs.events[ev.key].layout is None


def test_run_board_locked(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:6])
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:5]])
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="run_board", target=ev.key, value=ms[0].display_name), "officer", is_owner=False, raids=rs)
    d = configops.describe(reg, ConfigOp(op="run_board", target=ev.key, value=", ".join(m.display_name for m in ms[1:6])))
    assert f"asked to confirm: {ms[5].display_name}" in d and f"freed: {ms[0].display_name}" in d
    out = run(configops.apply_async(reg, ConfigOp(op="run_board", target=ev.key, value=", ".join(m.display_name for m in ms[1:6])), "Officer", False, bot=bot))
    assert out == f"{ev.key}: asked {ms[5].display_name} to confirm; freed {ms[0].display_name}"
    assert bot.calls == [("after_board_change", ev.key, [ms[5].display_name], [ms[0].display_name], "Officer")]
    seated = {p.signup_name for p in rs.events[ev.key].seated()}
    assert ms[5].display_name in seated and ms[0].display_name not in seated


def test_run_fill(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:8])
    join(reg, rs, ev, ms[8:12], "sub")
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="works after lock"):
        configops.apply(reg, ConfigOp(op="run_fill", target=ev.key), "officer", is_owner=False, raids=rs)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:6]])
    preview = configops.apply(reg, ConfigOp(op="run_fill", target=ev.key, value="preview"), "officer", is_owner=False, raids=rs)
    short = rc.run_size(reg, ev) - 6
    assert preview.startswith(f"{ev.key}: short {short} seat(s)") and "; would ask next: " in preview and " (sub: " in preview
    assert not rs.events[ev.key].fill_asks, "preview sends nothing"
    assert run(configops.apply_async(reg, ConfigOp(op="run_fill", target=ev.key, value="preview"), "Officer", False, bot=bot)) == preview
    assert bot.calls == []
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="run_fill", target=ev.key, value="send"), "officer", is_owner=False, raids=rs)
    assert run(configops.apply_async(reg, ConfigOp(op="run_fill", target=ev.key, value="send"), "Officer", False, bot=bot)) == f"{ev.key}: nobody left to ask (or the asks outstanding already cover it)"
    assert bot.calls == [("run_fill", ev.key, "Officer")]


def test_character_ops(reg):
    m = reg.test_members()[0]
    main = m.main.label
    assert configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Jonny", field="add", value="mage frost arcane"), "Officer", is_owner=False) == f"{m.display_name}: added Jonny"
    c = next(c for c in m.active() if c.label == "Jonny")
    assert (c.cls, c.spec, c.offspec, c.is_main, c.rank) == ("Mage", "Frost", "Arcane", False, "alt")
    assert configops.apply(reg, ConfigOp(op="character", member=m.main.name, character="Jonny", field="spec", value="Fire"), "Officer", is_owner=False) == f"{m.display_name}: Jonny: Fire/Arcane"
    assert (c.spec, c.offspec) == ("Fire", "Arcane")
    assert configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Jonny", field="offspec", value="none"), "Officer", is_owner=False) == f"{m.display_name}: Jonny: Fire"
    assert c.offspec is None
    with pytest.raises(RegistryError, match="not a Mage spec"):
        configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Jonny", field="spec", value="Arms"), "Officer", is_owner=False)
    assert configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Jonny", field="rank", value="raider"), "Officer", is_owner=False) == f"{m.display_name}: Jonny: rank raider"
    assert configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Jonny", field="main"), "Officer", is_owner=False) == f"{m.display_name}: main is Jonny"
    assert m.main.label == "Jonny"
    assert configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Jonny", field="retire"), "Officer", is_owner=False) == f"{m.display_name}: retired Jonny"
    assert c.status == "retired" and m.main.label == main, "the old main comes back"
    with pytest.raises(RegistryError, match="nothing changed"):
        configops.apply(reg, ConfigOp(op="character", member=m.display_name, character=main, field="spec", value=m.main.spec), "Officer", is_owner=False)
    with pytest.raises(RegistryError, match="class is fixed"):
        configops.apply(reg, ConfigOp(op="character", member=m.display_name, character=main, field="cls", value="Mage"), "Officer", is_owner=False)
    with pytest.raises(RegistryError, match="already has a name"):
        configops.apply(reg, ConfigOp(op="character", member=m.display_name, character=main, field="name", value="Other"), "Officer", is_owner=False)
    with pytest.raises(RegistryError, match="add needs"):
        configops.apply(reg, ConfigOp(op="character", member=m.display_name, character="Solo", field="add", value="Mage"), "Officer", is_owner=False)


def test_absence_clear(reg, rs):
    m = reg.test_members()[0]
    reg.add_absence(m.discord_id, "2030-03-01", "2030-03-03", "holiday", "t")
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="(?i)no absence starting Mon 01 Apr"):
        run(configops.apply_async(reg, ConfigOp(op="absence_clear", member=m.display_name, start="2030-04-01"), "Officer", False, bot=bot))
    out = run(configops.apply_async(reg, ConfigOp(op="absence_clear", member=m.display_name), "Officer", False, bot=bot))
    assert out == f"{m.display_name}: cleared absence 2030-03-01 → 2030-03-03 — sheet re-opened"
    assert bot.calls == [("absence_cleared", m.display_name, "2030-03-01", "Officer")]
    assert not reg.members[m.discord_id].absences
    reg.add_absence(m.discord_id, "2030-05-01", None, None, "t")
    reg.add_absence(m.discord_id, "2030-06-01", None, None, "t")
    with pytest.raises(RegistryError, match="2 upcoming absences"):
        configops.apply(reg, ConfigOp(op="absence_clear", member=m.display_name), "Officer", is_owner=False)
    assert configops.apply(reg, ConfigOp(op="absence_clear", member=m.display_name, start="2030-05-01"), "Officer", is_owner=False) == f"{m.display_name}: cleared absence 2030-05-01"
    assert [a.start for a in reg.members[m.discord_id].absences] == ["2030-06-01"]


def test_dm(reg):
    m = reg.test_members()[0]
    assert configops.apply(reg, ConfigOp(op="dm", member=m.display_name, value="off"), "Officer", is_owner=False) == f"{m.display_name}: DMs off"
    assert reg.members[m.discord_id].dm_opt_out is True
    assert configops.apply(reg, ConfigOp(op="dm", member=f"<@{m.discord_id}>", value="ON"), "Officer", is_owner=False) == f"{m.display_name}: DMs on"
    assert reg.members[m.discord_id].dm_opt_out is False
    with pytest.raises(RegistryError, match="dm is on \\| off"):
        configops.apply(reg, ConfigOp(op="dm", member=m.display_name, value="sometimes"), "Officer", is_owner=False)


def test_test_bench(reg, rs):
    bot = FakeBot(rs)
    n = len(reg.test_members())
    assert configops.apply(reg, ConfigOp(op="test", value="seed 18"), "Officer", is_owner=False) == f"test bench: 2 puppet(s) created, 18 in total"
    assert len(reg.test_members()) == n + 2
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="test", value="run barrow_deeps"), "Officer", is_owner=False)
    out = run(configops.apply_async(reg, ConfigOp(op="test", value="run barrow_deeps"), "Officer", False, bot=bot, by_id=42))
    assert out.startswith("test run bd-") and "lock 25 min before" in out and "no signup channel" in out
    ev = rs.live()[0]
    t = reg.config.roster(ev.team)
    assert t["test"] is True and t["test_by"] == 42 and t["cutoff_hard_hours"] == 25 / 60 and t["confirm_hours"] == 15 / 60
    assert bot.calls == [], "no signup channel: nothing posted"
    with pytest.raises(RegistryError, match="unknown raid"):
        run(configops.apply_async(reg, ConfigOp(op="test", value="run nowhere"), "Officer", False, bot=bot))
    assert run(configops.apply_async(reg, ConfigOp(op="test", value="clear"), "Officer", False, bot=bot)) == "test bench: 18 test member(s) removed, 0 test run(s) cancelled"
    assert bot.calls == [("test_bench_clear", "Officer")] and not reg.test_members()


def test_owner_gating_unchanged(reg, rs):
    """The new ops are officer ops (like /raid, /gm test and the Members page); the owner set is untouched."""
    assert configops.OWNER_OPS == {"set", "role_add", "role_remove", "raid_set", "raid_reset", "aura_set", "family_set", "aura_reset"}
    assert not (configops.RUN_OPS | {"character", "absence_clear", "dm", "test"}) & configops.OWNER_OPS
    bot = FakeBot(rs)
    with pytest.raises(RegistryError, match="needs the owner"):
        run(configops.apply_async(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="lock_hours_before", value="12"), "Officer", False, bot=bot))
    m = reg.test_members()[0]
    assert run(configops.apply_async(reg, ConfigOp(op="dm", member=m.display_name, value="off"), "Officer", False, bot=bot)).endswith("DMs off")


def test_current_config_lists_live_runs(reg, rs):
    ev = open_test_run(reg, rs)
    text = configops.current_config_text(reg)
    assert "## Live runs" in text and f"- {ev.key}: Barrow Deeps" in text and "open" in text and "(test run)" in text
    assert "## Test bench: 16 puppet" in text and "rosters:" not in text.split("## Effective")[0]


# ---------------------------------------------------------------- reads: what /ask sees

def test_guild_state_reads(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:6])
    join(reg, rs, ev, ms[6:8], "sub")
    rc.set_pin(ev, ms[0].display_name, "in")
    rs.save(ev, "pin")
    reg.add_absence(ms[9].discord_id, ev.start.date().isoformat(), None, "wedding", "t")
    s = help_mod.guild_state(reg, rs, ms[0].discord_id, False)
    assert f"live run {ev.key}" in s and "TEST RUN" in s and "state open" in s and "nudge " in s and "lock " in s
    assert "answers: 6 joined / 2 bench / 0 no thanks / 8 not answered" in s and f"pinned in: {ms[0].display_name}" in s
    assert f"{ms[0].display_name} ({ms[0].main.label} {ms[0].main.spec})" in s
    assert f"absent that day: {ms[9].display_name}" in s and "wedding" not in s, "reasons are officer-only"
    assert "## Asker (member)" in s and f"on {ev.key}: in as {ms[0].main.label}" in s and "pinned in" in s
    assert "aura overrides: none" in s and "test bench: 16 puppet member(s)" in s and f"test runs {ev.key}" in s
    assert "absences <#" not in s and "absences not set" in s
    # an officer asking about someone by character name gets their record; a member doesn't
    q = f"is {ms[3].main.name} coming tonight?"
    officer = help_mod.guild_state(reg, rs, ms[0].discord_id, True, q)
    assert f"## Member named in the question: {ms[3].display_name}" in officer and f"on {ev.key}: in as {ms[3].main.label}" in officer
    assert "## Member named in the question" not in help_mod.guild_state(reg, rs, ms[0].discord_id, False, q)
    # locked: groups with specs, confirmations, fill asks
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:5]])
    reg.add_placement_ask(ms[0].discord_id, ev.team, ms[0].main.label, "t")
    reg.answer_placement(ms[0].discord_id, ev.team, True, "t")
    ev.fill_asks.append(rc.FillAsk(discord_id=ms[7].discord_id, display_name=ms[7].display_name, kind="sub", role="melee", character=ms[7].main.label, spec=ms[7].main.spec, reason="short 15", expires_at=ev.start.isoformat()))
    rs.save(ev, "ask")
    s = help_mod.guild_state(reg, rs, ms[0].discord_id, True)
    assert "roster 1 (5 seated): g1:" in s and f"{ms[0].display_name} ({ms[0].main.label} {ms[0].main.spec})" in s
    assert f"confirmations: confirmed {ms[0].display_name}" in s and ms[1].display_name in s.split("waiting")[1].split("declined")[0]
    assert f"fill asks outstanding: {ms[7].display_name} (sub: {ms[7].main.label} {ms[7].main.spec} for melee, short 15, deadline" in s
    assert f"bench after lock: {ms[5].display_name} · short: {rc.run_size(reg, ev) - 5} seat(s)" in s
    assert len(s) <= help_mod.MAX_STATE_CHARS
