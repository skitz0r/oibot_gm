"""Officer roster wizards (flows_roster.py): confirm walk, rank, set-main, roster add/remove — driven end to end through
tests/wizard_harness.py, officer status monkeypatched."""
import asyncio
from types import SimpleNamespace

import pytest

from oibot_gm import discord_registry, flows_roster  # noqa: F401 — importing registers the flows
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness


class Ops:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None):
        self.lines.append(text)


def harness(reg, rs):
    h = Harness(reg, rs)
    h.client.ops = Ops()
    h.dms = []

    async def fetch_user(uid):
        async def send(text):
            h.dms.append((uid, text))
        return SimpleNamespace(id=uid, send=send)
    h.client.fetch_user = fetch_user
    return h


def user(m):
    return SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention=f"@{m.display_name}")


@pytest.fixture
def officer(monkeypatch):
    state = {"officer": True}
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: state["officer"])
    return state


@pytest.fixture
def rosters(reg):
    reg.config.rosters = [{"key": "alpha", "name": "Alpha team"}, {"key": "bravo", "name": "Bravo team"}]
    return reg


def run(coro):
    asyncio.run(coro)


# ---- /roster confirm

def test_confirm_walks_two_pending_in_a_row(reg, rs, officer):
    a, b = reg.test_members()[:2]
    reg.add_character(a.discord_id, a.display_name, "Altone", "Mage", "Fire", None, False)
    reg.add_character(b.discord_id, b.display_name, "Alttwo", "Priest", "Holy", None, False)
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_confirm"](h.command(), reg)
        assert "2 characters waiting" in h.text
        opt = h._items()[0].options
        assert any("Altone · Mage Fire · " + a.display_name == o.label for o in opt)
        await h.pick(h.selects()[0], "Altone")
        assert "Confirm **Altone**" in h.text and a.display_name in h.text
        await h.press("Confirm")
        assert "✅ Confirmed **Altone**" in h.text and "1 character waiting" in h.text
        await h.press("Confirm next")
        assert "Confirm **Alttwo**" in h.text
        await h.press("Confirm")
        assert "Nothing left to confirm" in h.text and h.screen["view"] is None
    run(go())
    assert reg.find("Altone")[1].confirmed_by == "Tester" and reg.find("Alttwo")[1].confirmed_by == "Tester"
    assert {uid for uid, _ in h.dms} == {a.discord_id, b.discord_id}
    assert len(h.client.ops.lines) == 2


def test_confirm_refuses_one_someone_else_confirmed(reg, rs, officer):
    a = reg.test_members()[0]
    reg.add_character(a.discord_id, a.display_name, "Raced", "Mage", "Fire", None, False)
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_confirm"](h.command(), reg)
        await h.pick(h.selects()[0], "Raced")
        reg.confirm("Raced", "Other Officer")  # meanwhile
        await h.press("Confirm")
        assert "already confirmed by Other Officer" in h.text and "Back to the list" in h.buttons()
        await h.press("Back to the list")
        assert "Nothing left to confirm" in h.text
    run(go())
    assert reg.find("Raced")[1].confirmed_by == "Other Officer" and not h.dms


def test_confirm_cancel_writes_nothing_and_list_button_opens_it(reg, rs, officer):
    a = reg.test_members()[0]
    reg.add_character(a.discord_id, a.display_name, "Waiting", "Mage", "Fire", None, False)
    h = harness(reg, rs)
    view = flows_roster.confirm_pending_view(reg, len(reg.pending()))
    assert view.children[0].label == "Confirm 1 character" and view.timeout is not None

    async def go():
        await view.children[0].callback(h.command())
        assert "Confirm characters" in h.text
        await h.pick(h.selects()[0], "Waiting")
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    run(go())
    assert not reg.find("Waiting")[1].confirmed_by
    assert flows_roster.confirm_pending_view(reg, 0) is None


def test_confirm_pages_past_25(reg, rs, officer):
    names = [f"Pend{chr(97 + i)}{chr(97 + j)}" for i in range(2) for j in range(15)]  # 30 unconfirmed
    for i, n in enumerate(names):
        m = reg.test_members()[i % len(reg.test_members())]
        reg.add_character(m.discord_id, m.display_name, n, "Mage", "Fire", None, False)
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_confirm"](h.command(), reg)
        assert len(h._items()[0].options) == 25 and "More →" in h.buttons()
        await h.press("More →")
        assert len(h._items()[0].options) == 5 and "← Previous" in h.buttons()
    run(go())


# ---- /roster rank

def test_rank_change_on_the_main(reg, rs, officer):
    m = reg.test_members()[3]  # a trial
    reg.add_character(m.discord_id, m.display_name, "Rankalt", "Mage", "Fire", None, False)
    main = m.main
    before = main.rank
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_rank"](h.command(), reg, member=user(m))
        assert h.selects() == ["Pick the character"]
        idx = next(o.value for o in h._items()[0].options if o.label == main.label)
        await h.pick("Pick the character", idx)
        assert "follows the main" in h.text
        assert [o.value for o in h._items()[0].options if o.default] == [before]
        await h.pick("Pick the rank", "core")
        assert f"**{before}** → **core**" in h.text
        await h.press("Confirm")
        assert "is now **core**" in h.text
    run(go())
    assert reg.members[m.discord_id].main.rank == "core"
    assert h.client.ops.lines


def test_rank_cancel_writes_nothing(reg, rs, officer):
    m = reg.test_members()[3]
    before = m.main.rank
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_rank"](h.command(), reg, member=user(m))  # one character: straight to the rank
        await h.pick("Pick the rank", "core")
        await h.press("Cancel")
    run(go())
    assert reg.members[m.discord_id].main.rank == before


# ---- /roster set-main

def test_set_main(reg, rs, officer):
    m = reg.test_members()[0]  # core
    old = m.main.label
    reg.add_character(m.discord_id, m.display_name, "Newmain", "Mage", "Fire", None, False)
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_set_main"](h.command(), reg, member=user(m))  # one other character: straight to confirm
        assert "Make **Newmain**" in h.text and "becomes **core**" in h.text
        await h.press("Confirm")
        assert "main is now **Newmain**" in h.text
    run(go())
    mm = reg.members[m.discord_id]
    assert mm.main.label == "Newmain" and mm.main.rank == "core"
    assert next(c for c in mm.characters if c.label == old).rank == "alt"


def test_set_main_refuses_a_retired_pick(reg, rs, officer):
    m = reg.test_members()[1]
    reg.add_character(m.discord_id, m.display_name, "Goner", "Mage", "Fire", None, False)
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_set_main"](h.command(), reg, member=user(m))
        reg.retire(m.discord_id, "Goner")  # meanwhile
        await h.press("Confirm")
        assert "retired meanwhile" in h.text
    run(go())
    assert reg.members[m.discord_id].main.label != "Goner"


def test_set_main_with_no_other_character_says_so(reg, rs, officer):
    m = reg.test_members()[2]
    h = harness(reg, rs)
    run(FLOWS["roster_set_main"](h.command(), reg, member=user(m)))
    assert "no other character" in h.text and h.screen["view"] is None


# ---- /roster add + remove

def test_add_then_remove_round_trip(reg, rs, officer, rosters):
    m = reg.test_members()[0]
    reg.add_character(m.discord_id, m.display_name, "Rosteralt", "Mage", "Fire", None, False)
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_add"](h.command(), reg, member=user(m))
        assert [o.label for o in h._items()[0].options] == ["Alpha team", "Bravo team"]
        await h.pick("Pick the roster", "bravo")
        assert any(b.startswith("Their main") for b in h.buttons())
        await h.press(next(b for b in h.buttons() if b.startswith("Their main")))
        assert "to **Bravo team**" in h.text
        await h.press("Confirm")
        assert "→ **Bravo team**" in h.text
    run(go())
    assert "bravo" in reg.members[m.discord_id].main.rosters

    h2 = harness(reg, rs)

    async def back():
        await FLOWS["roster_remove"](h2.command(), reg, member=user(m))  # on one roster: straight to confirm
        assert "from **Bravo team**" in h2.text
        await h2.press("Confirm")
        assert "removed from **Bravo team**" in h2.text
    run(back())
    assert not any("bravo" in c.rosters for c in reg.members[m.discord_id].characters)


def test_remove_when_on_no_roster_says_so(reg, rs, officer, rosters):
    m = reg.test_members()[5]
    h = harness(reg, rs)
    run(FLOWS["roster_remove"](h.command(), reg, member=user(m)))
    assert "isn't on any roster" in h.text and h.screen["view"] is None


def test_remove_from_a_roster_they_left_meanwhile_refuses(reg, rs, officer, rosters):
    m = reg.test_members()[6]
    reg.roster_add(m.discord_id, "alpha", "t")
    reg.roster_add(m.discord_id, "bravo", "t")
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_remove"](h.command(), reg, member=user(m))
        await h.pick("Pick the roster", "alpha")
        reg.roster_remove(m.discord_id, "alpha", "Other Officer")  # meanwhile
        await h.press("Confirm")
        assert "❌" in h.text and "isn't on alpha" in h.text
    run(go())
    assert "bravo" in reg.members[m.discord_id].main.rosters


def test_add_cancel_writes_nothing(reg, rs, officer, rosters):
    m = reg.test_members()[7]
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_add"](h.command(), reg, member=user(m))
        await h.pick("Pick the roster", "alpha")  # one character: straight to confirm
        await h.press("Cancel")
    run(go())
    assert not any(c.rosters for c in reg.members[m.discord_id].characters)


# ---- officers only

@pytest.mark.parametrize("name", ["roster_confirm", "roster_rank", "roster_set_main", "roster_add", "roster_remove"])
def test_non_officer_is_stopped_at_entry(reg, rs, officer, name):
    officer["officer"] = False
    h = harness(reg, rs)
    seed = {} if name == "roster_confirm" else {"member": user(reg.test_members()[0])}
    run(FLOWS[name](h.command(), reg, **seed))
    assert "Officers only" in h.text and h.screen["view"] is None


def test_non_officer_is_stopped_at_a_later_press(reg, rs, officer, rosters):
    m = reg.test_members()[0]
    h = harness(reg, rs)

    async def go():
        await FLOWS["roster_add"](h.command(), reg, member=user(m))
        officer["officer"] = False
        assert not await h.screen["view"].interaction_check(h._on_screen())
        assert "Officers only" in h.text
    run(go())
    assert not any(c.rosters for c in reg.members[m.discord_id].characters)
