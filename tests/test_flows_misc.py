"""The test bench wizards (/gm test run|answer|seed) and /gm rule, driven end to end through fake interactions."""
import asyncio
from types import SimpleNamespace

import pytest

from oibot_gm import discord_policy, discord_registry
from oibot_gm import flows_misc as fm
from oibot_gm import policy as policy_mod
from oibot_gm.wizard import FLOWS
from conftest import lock_with_board, open_test_run
from wizard_harness import Harness


class FakeOps:
    def __init__(self):
        self.lines = []

    async def emit(self, config, level, line, **kw):
        self.lines.append((level, line))


def make_client(reg, rs, ps=None):
    calls = {"posted": [], "refreshed": [], "dropped": [], "compiled": []}

    async def post_sheet(reg_, rs_, ev, channel):
        calls["posted"].append(ev.key)

    async def refresh_sheet(reg_, ev):
        calls["refreshed"].append(ev.key)

    async def drop_seated(reg_, rs_, ev, team, m, why, note, announce=True):
        calls["dropped"].append((m.display_name, why))

    client = SimpleNamespace(raids=SimpleNamespace(store=lambda _r: rs), post_sheet=post_sheet, refresh_sheet=refresh_sheet, drop_seated=drop_seated,
                             get_channel=lambda _id: None, ops=FakeOps(), policies=SimpleNamespace(store=lambda _r: ps),
                             ctx=SimpleNamespace(provider="llm"), ico=lambda kind, key: "")
    return client, calls


@pytest.fixture
def officer(monkeypatch):
    state = {"officer": True}
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: state["officer"])
    return state


def only_run(rs):
    live = rs.live()
    assert len(live) == 1
    return live[0]


def cutoff_minutes(reg, ev):
    t = reg.config.roster(ev.team)
    return tuple(round(t[k] * 60) for k in ("cutoff_soft_hours", "cutoff_hard_hours", "confirm_hours"))


# ---- /gm test run

def test_run_default_press_opens_todays_standard_run(reg, rs, officer):
    client, calls = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_run"](h.command(), reg)
        assert "Standard" in h.text and "Open the test run" in h.buttons()
        before = reg.now_local()
        await h.press("Open the test run")
        assert "🧪 Test run" in h.text and h.screen["view"] is None
        ev = only_run(rs)
        start_in = (ev.start - before).total_seconds() / 60
        assert 39 <= start_in <= 40
    asyncio.run(go())
    ev = only_run(rs)
    assert cutoff_minutes(reg, ev) == (32, 25, 15)
    t = reg.config.roster(ev.team)
    assert t["test"] and t["test_by"] == h.user.id and t["open_dm"] is False
    assert calls["posted"] == [ev.key] and any("opened test run" in l for _, l in client.ops.lines)


def test_run_quick_tempo_and_dm_toggle(reg, rs, officer):
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)
    raids = list(reg.profile.raids)

    async def go():
        await FLOWS["test_run"](h.command(), reg)
        if len(raids) > 1:
            await h.pick("Which raid", raids[-1])
        await h.pick("Tempo", "quick")
        await h.press("DM everyone when it opens: off")
        assert "DM everyone when it opens: on" in h.buttons()
        await h.press("Open the test run")
    asyncio.run(go())
    ev = only_run(rs)
    soft, hard, confirm = cutoff_minutes(reg, ev)
    assert (soft, hard, confirm) == (16, 12, 8)
    assert reg.config.roster(ev.team)["open_dm"] is True and reg.config.roster(ev.team)["instance"] == raids[-1]


def test_every_tempo_holds_the_ordering():
    for _, start, nudge, lock, confirm in fm.TEMPOS.values():
        assert start > nudge > lock > confirm >= 0


def test_run_cancel_writes_nothing_and_non_officer_is_refused(reg, rs, officer):
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)
    rosters_before = len(reg.config.rosters)

    async def go():
        await FLOWS["test_run"](h.command(), reg)
        await h.press("Cancel")
        assert "nothing was changed" in h.text
        officer["officer"] = False
        await FLOWS["test_run"](h.command(), reg)
        assert "Officers only" in h.text and h.screen["view"] is None
    asyncio.run(go())
    assert rs.live() == [] and len(reg.config.rosters) == rosters_before


def test_run_guard_rechecks_on_press(reg, rs, officer):
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_run"](h.command(), reg)
        officer["officer"] = False
        assert not await h.screen["view"].interaction_check(h._on_screen())
    asyncio.run(go())
    assert rs.live() == []


# ---- /gm test answer

def test_answer_random_mix(reg, rs, officer):
    ev = open_test_run(reg, rs)
    client, calls = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_answer"](h.command(), reg)
        assert h.buttons() == ["Random mix", "One member", "Done"]  # one live run: no run picker
        await h.press("Random mix")
        assert set(h.modal._items) == {"in", "sub", "out"}
        await h.submit(h.modal, **{"in": "5", "sub": "2", "out": "1"})
        assert "🧪" in h.text and "Random mix" in h.buttons()  # back on the menu, ready to go again
    asyncio.run(go())
    ev = rs.for_team(ev.team)
    assert (len(ev.by_status("in")), len(ev.by_status("sub")), len(ev.by_status("out"))) == (5, 2, 1)
    assert calls["refreshed"] and any("8 answer(s)" in l for _, l in client.ops.lines)


def test_answer_picks_the_run_when_several_are_live(reg, rs, officer):
    a = open_test_run(reg, rs, days=3)
    b = open_test_run(reg, rs, days=4)
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)
    m = reg.test_members()[0]

    async def go():
        await FLOWS["test_answer"](h.command(), reg)
        assert h.selects() == ["Pick the run"]
        await h.pick("Pick the run", b.team)
        await h.press("One member")
        await h.pick("Pick a test member", str(m.discord_id))
        assert h.buttons()[:3] == ["Join", "Bench", "No thanks"]
        await h.press("Bench")
    asyncio.run(go())
    assert rs.for_team(b.team).signups[str(m.discord_id)].status == "sub"
    assert str(m.discord_id) not in rs.for_team(a.team).signups


def test_answer_one_member_on_a_locked_run_calls_out(reg, rs, officer):
    ev = open_test_run(reg, rs)
    names = [m.display_name for m in reg.test_members()[:10]]
    for m in reg.test_members()[:10]:
        from oibot_gm import raidcycle as rc

        rc.set_signup(reg, rs, ev, m, None, "in", source="test")
    ev = lock_with_board(reg, rs, rs.for_team(ev.team), names)
    target = reg.test_members()[0]
    client, calls = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_answer"](h.command(), reg)
        await h.press("One member")
        await h.pick("Pick a test member", str(target.discord_id))
        await h.press("No thanks")
        assert "called out" in h.text
    asyncio.run(go())
    assert calls["dropped"] == [(target.display_name, "callout (test)")]


def test_answer_member_list_pages_past_25(reg, rs, officer):
    reg.seed_test_members(len(reg.test_roster()), "t")
    if len(reg.test_members()) <= 25:
        pytest.skip("the built-in bench is 25 or fewer")
    open_test_run(reg, rs)
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_answer"](h.command(), reg)
        await h.press("One member")
        assert "More →" in h.buttons() and len(h._items()[0].options) == 25
        await h.press("More →")
        assert h._items()[0].options[0].value == str(reg.test_members()[25].discord_id)
    asyncio.run(go())


def test_answer_cancel_writes_nothing_and_no_run_says_so(reg, rs, officer):
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_answer"](h.command(), reg)
        assert "No live run" in h.text
    asyncio.run(go())
    ev = open_test_run(reg, rs)

    async def go2():
        await FLOWS["test_answer"](h.command(), reg)
        await h.press("One member")
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    asyncio.run(go2())
    assert rs.for_team(ev.team).signups == {}


# ---- /gm test seed

def test_seed_offers_the_whole_bench_and_seeds_it(reg, rs, officer):
    n = len(reg.test_roster())
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_seed"](h.command(), reg)
        opts = h._items()[0].options
        assert opts[-1].value == str(n) and opts[-1].label == f"All {n}"
        assert all(int(o.value) <= n for o in opts)
        await h.pick("How many", str(n))
        assert "in total" in h.text
    asyncio.run(go())
    assert len(reg.test_members()) == n


def test_seed_takes_its_ceiling_from_a_private_roster(reg, rs, officer):
    import yaml

    reg.clear_test_members("t")
    rows = [{"name": f"Bench{c}", "cls": "Mage", "spec": "Fire"} for c in "abcdefg"]
    (reg.store.root / reg.key / "test_roster.yaml").write_text(yaml.safe_dump(rows))
    opts = fm.seed_opts(reg)
    assert [o.value for o in opts] == ["5", "7"]
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_seed"](h.command(), reg)
        await h.pick("How many", "5")
    asyncio.run(go())
    assert len(reg.test_members()) == 5


def test_seed_cancel_and_refusal(reg, rs, officer):
    have = len(reg.test_members())
    client, _ = make_client(reg, rs)
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["test_seed"](h.command(), reg)
        await h.press("Cancel")
        officer["officer"] = False
        await FLOWS["test_seed"](h.command(), reg)
        assert "Officers only" in h.text
    asyncio.run(go())
    assert len(reg.test_members()) == have


# ---- /gm rule loot|comp

@pytest.fixture
def ps(reg):
    return policy_mod.PolicyStore(reg.store, reg.key)


def test_rule_shows_the_tail_and_takes_the_compile_path(reg, rs, ps, officer, monkeypatch):
    client, calls = make_client(reg, rs, ps)
    got = {}

    async def fake_compile(interaction, reg_, ps_, doc, previous, provider, ops, followup=False):
        got.update(doc=doc, previous=previous, provider=provider, followup=followup, draft=ps_.read(doc))
    monkeypatch.setattr(discord_policy, "compile_and_confirm", fake_compile)
    before = ps.read("loot")
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["rule"](h.command(), reg, doc="loot")
        assert "```md" in h.text and h.buttons() == ["Write the rule", "Cancel"]
        await h.press("Write the rule")
        await h.submit(h.modal, text="Tier tokens go to mains first\nunless nobody needs them")
        assert "Added to the loot policy draft" in h.text
    asyncio.run(go())
    assert got["doc"] == "loot" and got["previous"] == before and got["provider"] == "llm" and got["followup"]
    assert got["draft"].endswith("- Tier tokens go to mains first\n  unless nobody needs them\n")


def test_rule_cancel_writes_nothing_and_non_officer_is_refused(reg, rs, ps, officer, monkeypatch):
    client, _ = make_client(reg, rs, ps)
    monkeypatch.setattr(discord_policy, "compile_and_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no compile")))
    before = ps.read("comp")
    h = Harness(reg, rs, client=client)

    async def go():
        await FLOWS["rule"](h.command(), reg, doc="comp")
        await h.press("Write the rule")
        assert h.modal is not None
        await h.press("Cancel")  # the modal was dismissed; cancel from the screen
        assert "nothing was changed" in h.text
        officer["officer"] = False
        await FLOWS["rule"](h.command(), reg, doc="comp")
        assert "Officers only" in h.text
    asyncio.run(go())
    assert ps.read("comp") == before


def test_policy_tail_cuts_at_a_line():
    text = "\n".join(f"- rule {i}" for i in range(500))
    tail = fm.policy_tail(text)
    assert tail.startswith("…\n- rule") and tail.endswith("- rule 499") and len(tail) <= fm.TAIL_CHARS + 2
