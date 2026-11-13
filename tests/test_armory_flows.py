"""The armory link on Discord (armory link §5.4 "Discord."): the character wizard (`/me char`, `/roster char`) gains Link to
the armory… / Unlink… / Keep professions by hand / Follow the armory again / Sync now, officers Move to another member…
(every version, R11), `/me professions` shows a synced character's list with why it isn't edited there, and officers get
`/roster armory` (the counts and Link the sure matches). Driven through tests/wizard_harness.py, every screen linted. The
armory is the fake of tests/test_armory_link.py (never Blizzard); the guild and the people are invented."""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest
from conftest import OWNER
from test_armory_link import ORLA, TESSA, WREN, FakeOps, char, clock, commits, facts, fake, link_guild, people  # noqa: F401 — fixtures
from wizard_harness import Harness

from oibot_gm import armory, armory_seed, discord_armory, discord_registry, pending_views
from oibot_gm import consequences as cq
from oibot_gm import wizard_flows  # noqa: F401 — registers every flow
from oibot_gm.armory import ArmoryDown
from oibot_gm.profiles import GameProfile
from oibot_gm.web.api_armory import NO_GUILD_YET, NOT_READ_YET
from oibot_gm.wizard import FLOWS

ROOT = Path(__file__).resolve().parents[1]
ARMORY_BUTTONS = {"Link to the armory…", "Unlink…", "Keep professions by hand", "Follow the armory again", "Sync now"}


def run(coro):
    return asyncio.run(coro)


class Client:
    """The bot as the wizards and the verbs see it: the bot's RaidStore, an ops feed that records, icons, sheet redraws."""

    def __init__(self, rs):
        self.raids = types.SimpleNamespace(store=lambda _reg: rs)
        self.ops = FakeOps()
        self.ico = lambda kind, key: ""
        self.redrawn: list = []

    async def refresh_sheet(self, reg, ev):
        self.redrawn.append(ev.key)

    async def send_member_dm(self, *a, **k):
        raise AssertionError("the armory never DMs")


def harness(reg, rs, uid, name=None):
    name = name or (reg.members[uid].display_name if uid in reg.members else "Owner")
    return Harness(reg, rs, user=types.SimpleNamespace(id=uid, display_name=name, mention=f"@{name}"), client=Client(rs))


@pytest.fixture
def officer(monkeypatch):
    state = {"officer": True}
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: state["officer"] or r.is_owner_id(i.user.id))

    async def live(bot, reg, by_id):  # the verbs' live check (PRIV_LIVE) follows the same switch
        return by_id is not None and (state["officer"] or reg.is_owner_id(int(by_id)))
    monkeypatch.setattr(discord_armory, "_officer", live)
    return state


IVY = 555_000_000_000_000_555  # an officer who is not the owner


def link_bram(reg, rs):
    bot = Client(rs)
    found = run(discord_armory.find_for_link(bot, reg, "Bram", "Kettle"))
    return run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Tessa", by_id=TESSA))


def tessa(reg):
    return reg.members[TESSA]


# ---------------------------------------------------------------- /me char: link

def test_a_member_links_their_character_through_the_lookup_and_the_decide_screen(reg, rs, fake, clock):
    people(reg)
    h = harness(reg, rs, TESSA)

    async def go():
        await FLOWS["member_char"](h.command(), reg)
        assert "Not linked to the armory yet." in h.text
        assert "Link to the armory…" in h.buttons() and not ({"Unlink…", "Sync now", "Move to another member…"} & set(h.buttons()))
        await h.press("Link to the armory…")  # one named character: straight to the box
        assert h.modal is not None and [f.label for f in h.modal._fields] == ["First name", "Surname"]
        assert h.modal._fields[0].default == "Bram"
        before = commits(reg)
        await h.submit(h.modal, name="Bram", surname="Kettle")
        assert "Looking in the armory" in h.screens[-2]["text"], "the press is answered before the lookup"
        found = await discord_armory.find_for_link(h.client, reg, "Bram", "Kettle")
        assert h.text.endswith("\n".join(cq.armory_link_consequence(reg, tessa(reg), char(reg, TESSA, "Bram"), found, TESSA)))
        assert h.buttons() == ["Link it", "Back"] and commits(reg) == before, "nothing written before the press"
        await h.press("Link it")
        assert "✅ Bram Kettle is linked to the armory." in h.text and h.screen["view"] is None
    run(go())
    c = char(reg, TESSA, "Bram Kettle")
    assert c.armory.id == 700101 and c.profession_sync
    assert len([t for lv, t in h.client.ops.lines if lv == "info"]) == 1


def test_the_hub_line_and_the_actions_of_a_linked_character(reg, rs, fake, clock):
    people(reg)
    link_guild(reg)
    link_bram(reg, rs)
    h = harness(reg, rs, TESSA)
    run(FLOWS["member_char"](h.command(), reg))
    assert "Linked to the armory · Updated <t:" in h.text and ":R>" in h.text
    assert "in the guild" not in h.text, "not known before the roster is read: nothing said"
    assert {"Unlink…", "Keep professions by hand", "Sync now"} <= set(h.buttons())
    assert "Link to the armory…" not in h.buttons() and "Follow the armory again" not in h.buttons()
    run(discord_armory.roster_refresh(Client(rs), reg))
    h2 = harness(reg, rs, TESSA)
    run(FLOWS["member_char"](h2.command(), reg))
    assert "· in the guild" in h2.text


def test_not_found_and_the_armory_down_are_one_sentence_with_a_way_back(reg, rs, fake, clock):
    people(reg)
    h = harness(reg, rs, TESSA)
    before = commits(reg)

    async def go():
        await FLOWS["member_char"](h.command(), reg)
        await h.press("Link to the armory…")
        await h.submit(h.modal, name="Bram", surname="Nobody")
        assert discord_armory.NOT_FOUND in h.text and h.buttons() == ["Try again", "Cancel"]
        await h.press("Try again")
        assert h.modal is not None
        fake.fail = ArmoryDown()
        await h.submit(h.modal, name="Bram", surname="Kettle")
        assert armory.DOWN_WORDS in h.text and "Try again" in h.buttons()
    run(go())
    assert commits(reg) == before and char(reg, TESSA, "Bram").armory is None


# ---------------------------------------------------------------- unlink, the switch, Sync now

def test_keep_by_hand_follow_again_sync_now_and_unlink(reg, rs, fake, clock):
    people(reg)
    link_bram(reg, rs)
    h = harness(reg, rs, TESSA)

    async def go():
        await FLOWS["member_char"](h.command(), reg)
        await h.press("Keep professions by hand")
        assert h.text.endswith("\n".join(cq.profession_sync_consequence(False, "Bram Kettle", current=True)))
        await h.press("Keep them by hand")
        assert "✅ Bram Kettle's professions are kept by hand." in h.text
        assert char(reg, TESSA, "Bram Kettle").profession_sync is False

        await FLOWS["member_char"](h.command(), reg)
        assert "professions kept by hand" in h.text and "Follow the armory again" in h.buttons()
        await h.press("Follow the armory again")
        assert h.text.endswith("\n".join(cq.profession_sync_consequence(True, "Bram Kettle", current=False)))
        await h.press("Follow the armory")
        assert "✅ Bram Kettle's professions follow the armory." in h.text
        assert char(reg, TESSA, "Bram Kettle").profession_sync is True

        await FLOWS["member_char"](h.command(), reg)
        await h.press("Sync now")
        assert "Asking the armory" in h.screens[-2]["text"], "answered before the request"
        assert cq.armory_sync_sentence("Bram Kettle", "unchanged") in h.text or cq.armory_sync_sentence("Bram Kettle", "updated") in h.text
        await FLOWS["member_char"](h.command(), reg)
        await h.press("Sync now")
        assert discord_armory.SYNC_LIMITED in h.text

        await FLOWS["member_char"](h.command(), reg)
        await h.press("Unlink…")
        c = char(reg, TESSA, "Bram Kettle")
        assert h.text.endswith("\n".join(cq.armory_unlink_consequence(reg, tessa(reg), c, TESSA)))
        await h.press("Unlink it")
        assert "✅ Bram Kettle is unlinked from the armory." in h.text
    run(go())
    assert char(reg, TESSA, "Bram Kettle").armory is None


# ---------------------------------------------------------------- /roster char: an officer, rights on every press, Move

def test_an_officer_links_for_a_member_and_rights_are_checked_on_every_press(reg, rs, fake, clock, officer):
    people(reg)
    tess = types.SimpleNamespace(id=TESSA, display_name="Tessa", mention="@Tessa")
    h = harness(reg, rs, IVY, "Ivy")

    async def go():
        await FLOWS["member_char"](h.command(), reg, member=tess)
        assert {"Link to the armory…", "Move to another member…"} <= set(h.buttons())
        await h.press("Link to the armory…")
        officer["officer"] = False  # Ivy stops being an officer while the box is open
        await h.submit(h.modal, name="Bram", surname="Kettle")
        assert "Officers only" in h.text and h.screen["view"] is None
        assert not fake.calls, "nothing asked of the armory"
    run(go())
    assert char(reg, TESSA, "Bram").armory is None

    officer["officer"] = True
    h = harness(reg, rs, IVY, "Ivy")

    async def again():
        await FLOWS["member_char"](h.command(), reg, member=tess)
        await h.press("Link to the armory…")
        await h.submit(h.modal, name="Bram", surname="Kettle")
        assert "Tessa's Bram takes that name" in h.text
        officer["officer"] = False
        assert not await h.screen["view"].interaction_check(h._on_screen()), "the decide screen's press re-checks the officer"
    run(again())
    assert char(reg, TESSA, "Bram").armory is None


def test_an_officer_moves_a_character_with_the_user_select(reg, rs, fake, clock, officer):
    people(reg)
    h = harness(reg, rs, OWNER, "Owner")
    wren = types.SimpleNamespace(id=WREN, display_name="Wren", mention="@Wren")
    orla = types.SimpleNamespace(id=ORLA, display_name="Orla", mention="@Orla")

    async def go():
        await FLOWS["member_char"](h.command(), reg, member=wren)
        await h.press("Move to another member…")
        assert h.selects() == ["Pick the member it moves to"]
        before = commits(reg)
        await h.pick_member("Pick the member it moves to", orla)
        c = char(reg, WREN, "Pip")
        assert h.text.endswith("\n".join(cq.move_consequence(reg, reg.members[WREN], c, reg.members[ORLA])))
        assert h.buttons() == ["Move it", "Back"] and commits(reg) == before
        await h.press("Move it")
        assert "✅ Pip moved to Orla." in h.text
    run(go())
    assert any(c.label == "Pip" for c in reg.members[ORLA].active())
    assert not any(c.label == "Pip" for c in reg.members[WREN].active())


def test_a_move_refusal_is_the_verbs_sentence_with_a_way_back(reg, rs, fake, clock, officer):
    people(reg)
    h = harness(reg, rs, OWNER, "Owner")
    wren = types.SimpleNamespace(id=WREN, display_name="Wren", mention="@Wren")
    stranger = types.SimpleNamespace(id=999, display_name="Stranger", mention="@Stranger")

    async def go():
        await FLOWS["member_char"](h.command(), reg, member=wren)
        await h.press("Move to another member…")
        await h.pick_member("Pick the member it moves to", stranger)
        why = reg.move_refusal(WREN, "Pip", 999, rs=rs, by_id=OWNER)
        assert why and why in h.text and "Pick someone else" in h.buttons()
        await h.press("Pick someone else")
        assert h.selects() == ["Pick the member it moves to"]
    run(go())


def test_self_mode_never_offers_move(reg, rs, fake, clock):
    people(reg)
    h = harness(reg, rs, WREN)
    run(FLOWS["member_char"](h.command(), reg))
    assert "Move to another member…" not in h.buttons()


# ---------------------------------------------------------------- /me professions on a synced character

def test_a_synced_characters_professions_are_shown_not_edited(reg, rs, fake, clock):
    people(reg)
    link_bram(reg, rs)
    h = harness(reg, rs, TESSA)
    run(FLOWS["me_professions"](h.command(), reg))
    assert "Leatherworking" in h.text and "come from the armory" in h.text and "Keep professions by hand" in h.text and "/me char" in h.text
    assert not ({"Add profession…", "Change skill…", "Add recipe…", "Remove…"} & set(h.buttons()))
    run(discord_armory.set_profession_sync(Client(rs), reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    h2 = harness(reg, rs, TESSA)
    run(FLOWS["me_professions"](h2.command(), reg))
    assert "Change skill…" in h2.buttons() and "come from the armory" not in h2.text


# ---------------------------------------------------------------- /roster armory

def seed_people(reg):
    """Sure matches: Tessa's Bram (Bram Kettle, Warrior), Orla's Tamsin (Tamsin Pell, Druid), Wren's Pip (Pip Wobble)."""
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)
    reg.add_character(ORLA, "Orla", "Tamsin", "Druid", "Restoration", None, True)
    reg.add_character(WREN, "Wren", "Pip", "Rogue", "Combat", None, True)


def test_roster_armory_shows_the_counts_and_links_the_sure_matches(reg, rs, fake, clock, officer):
    seed_people(reg)
    link_guild(reg)
    run(discord_armory.roster_refresh(Client(rs), reg))
    seed = armory_seed.propose(reg, *discord_armory.roster_snapshot(reg)[:1], taken_at=discord_armory.roster_snapshot(reg)[1])
    assert len(seed.sure) == 3
    fake.chars["tamsin-pell"] = facts("Tamsin", "Pell", 700999, cls="Druid")  # the armory now shows another character
    h = harness(reg, rs, OWNER, "Owner")

    async def go():
        await FLOWS["roster_armory"](h.command(), reg)
        assert "Lantern Wardens" in h.text and "Linked to the armory: **0**" in h.text
        assert "**3** sure" in h.text and f"In the guild and not claimed: **{len(seed.unclaimed)}**" in h.text
        assert "Link the sure matches" in h.buttons()
        await h.press("Link the sure matches")
        assert "Tessa's Bram → Bram Kettle" in h.text and "Orla's Tamsin → Tamsin Pell" in h.text
        assert h.text.endswith("\n".join(cq.armory_seed_consequence(3)))
        assert h.buttons() == ["Link them", "Back"] and not reg.armory_linked()
        await h.press("Link them")
        assert "Linked 2 of 3." in h.text and armory_seed.ANOTHER_CHARACTER in h.text and "Orla's Tamsin" in h.text
    run(go())
    assert {c.label for _m, c in reg.armory_linked()} == {"Bram Kettle", "Pip Wobble"}


def test_roster_armory_shows_the_fresh_counts_when_they_moved(reg, rs, fake, clock, officer):
    seed_people(reg)
    link_guild(reg)
    run(discord_armory.roster_refresh(Client(rs), reg))
    h = harness(reg, rs, OWNER, "Owner")

    async def go():
        await FLOWS["roster_armory"](h.command(), reg)
        await h.press("Link the sure matches")
        reg.add_character(ORLA, "Orla", "Korrin", "Shaman", "Restoration", None, False)  # a new sure match meanwhile
        await h.press("Link them")
        assert "changed since you looked" in h.text and "**4** sure" in h.text and "Link the sure matches" in h.buttons()
    run(go())
    assert not reg.armory_linked(), "nothing linked from a stale count"


def test_roster_armory_empty_states_and_officers_only(reg, rs, fake, clock, officer):
    h = harness(reg, rs, OWNER, "Owner")
    run(FLOWS["roster_armory"](h.command(), reg))
    assert NO_GUILD_YET in h.text and "Link the sure matches" not in h.buttons()
    link_guild(reg)
    h = harness(reg, rs, OWNER, "Owner")
    run(FLOWS["roster_armory"](h.command(), reg))
    assert NOT_READ_YET in h.text and "Link the sure matches" not in h.buttons()
    officer["officer"] = False
    h = harness(reg, rs, WREN, "Wren")
    run(FLOWS["roster_armory"](h.command(), reg))
    assert "Officers only" in h.text and h.screen["view"] is None


def test_open_screens_are_mirrored_for_a_restart(reg, rs, fake, clock, officer, monkeypatch):
    monkeypatch.setattr(pending_views, "_live", {})
    monkeypatch.setattr(pending_views, "track_interaction", lambda key, interaction, title="": pending_views.track(key, app_id=1, token="t", title=title))
    people(reg)
    link_guild(reg)
    h = harness(reg, rs, OWNER, "Owner")

    async def go():
        await FLOWS["roster_armory"](h.command(), reg)
        assert pending_views.live_count() == 1
        await h.press("Done")
        assert pending_views.live_count() == 0
        await FLOWS["member_char"](h.command(), reg, member=types.SimpleNamespace(id=TESSA, display_name="Tessa", mention="@Tessa"))
        assert pending_views.live_count() == 1
        await h.press("Cancel")
        assert pending_views.live_count() == 0
    run(go())


# ---------------------------------------------------------------- a version without an armory (tbc)

@pytest.fixture
def tbc(monkeypatch, reg):
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    return reg


def test_tbc_shows_nothing_of_the_armory_but_the_officers_move(tbc, rs, fake, officer):
    reg = tbc
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)
    assert not armory.available(reg)
    h = harness(reg, rs, TESSA)
    run(FLOWS["member_char"](h.command(), reg))
    assert h.buttons() == ["Spec", "Main", "Flex", "Retire", "Delete", "Cancel"], "the tbc /me char hub is as before"
    assert "armory" not in h.text.lower()
    hp = harness(reg, rs, TESSA)
    run(FLOWS["me_professions"](hp.command(), reg))
    assert "armory" not in hp.text.lower()
    ho = harness(reg, rs, OWNER, "Owner")
    run(FLOWS["member_char"](ho.command(), reg, member=types.SimpleNamespace(id=TESSA, display_name="Tessa", mention="@Tessa")))
    assert ho.buttons() == ["Spec", "Main", "Flex", "Rank", "Retire", "Delete", "Move to another member…", "Cancel"], "R11: only Move is added"
    assert "armory" not in ho.text.lower()
    hr = harness(reg, rs, OWNER, "Owner")
    run(FLOWS["roster_armory"](hr.command(), reg))
    assert hr.text.endswith(armory.NO_ARMORY_WORDS) and hr.screen["view"] is None
    assert not fake.calls, "nothing asked of the armory"


# ---------------------------------------------------------------- the command

def test_roster_armory_fits_discords_limits(reg):
    import discord
    from discord import app_commands

    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree, Guilds.of(reg.store, reg), Ops(client), lambda k, v: "")
    roster = next(c for c in tree.get_commands() if c.name == "roster")
    assert len(roster.commands) <= 25
    cmd = next(c for c in roster.commands if c.name == "armory")
    assert len(cmd.description) <= 100 and not cmd.parameters
