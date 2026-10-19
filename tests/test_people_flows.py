"""People wizards driven end to end (tests/wizard_harness.py, every screen linted): /me char on the MemberWizard's self
mode (spec, flex, delete with the ripple on the decide screen), /roster char, the absence wizards (an overlap refused on
the preview), I'm back (/me absent clear, /roster back, the absences card's buttons), the owner transfer (D50)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import OWNER, join, lock_with_board
from test_people import real, real_run
from wizard_harness import Harness

from oibot_gm import discord_registry, guild_routing
from oibot_gm import raidcycle as rc
from oibot_gm.discord_pool import AbsenceButton, absences_view
from oibot_gm.discord_registry import Guilds
from oibot_gm.wizard import FLOWS


def run(coro):
    asyncio.run(coro)


def as_member(h, m):
    h.user = SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention=f"@{m.display_name}")
    return h


def user(m, bot=False):
    return SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention=f"@{m.display_name}", bot=bot)


@pytest.fixture
def officer(monkeypatch):
    state = {"officer": True}
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: state["officer"])
    return state


# ---------------------------------------------------------------- /me char (self mode)

def test_me_char_spec_through_the_hub_is_one_table_save(reg, rs):
    m = real(reg, 0, "Priest", "Holy")
    ev = real_run(reg, rs)
    join(reg, rs, ev, [m])
    h = as_member(Harness(reg, rs), m)

    async def go():
        await FLOWS["member_char"](h.command(), reg)
        assert {"Spec", "Main", "Flex", "Retire", "Delete"} <= set(h.buttons()) and "Rank" not in h.buttons()  # rank is an officer's
        await h.press("Spec")  # one character: straight to its spec screen
        await h.pick("Main spec", "Shadow")
        await h.press("Review")
        assert "Shadow" in h.text and "now reads Shadow" in h.text and "Save the spec" in h.buttons()  # the ripple, from code
        await h.press("Save the spec")
        assert "✅" in h.text and h.screen["view"] is None
    run(go())
    assert reg.members[m.discord_id].main.spec == "Shadow" and ev.signups[str(m.discord_id)].spec == "Shadow"


def test_me_char_flex_picks_only_the_roles_the_spec_doesnt_give(reg, rs):
    m = real(reg, 0, "Priest", "Holy")
    h = as_member(Harness(reg, rs), m)

    async def go():
        await FLOWS["member_char"](h.command(), reg, action="flex")
        opts = [o.value for o in h._items()[0].options]
        assert "healer" not in opts and "tank" in opts and "none" in opts
        await h.pick("Extra roles", "tank", "melee")
        assert "could also play: **tank, melee**" in h.text
        await h.press("Save")
    run(go())
    assert reg.members[m.discord_id].main.flex == ["melee", "tank"]


def test_me_char_delete_of_a_seated_character_says_the_seat_goes(reg, rs):
    ms = [real(reg, i, "Mage", "Fire") for i in range(5)]
    ev = real_run(reg, rs)
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    m = ms[1]
    reg.add_character(m.discord_id, m.display_name, "Keeper", "Warrior", "Protection", None, False)
    h = as_member(Harness(reg, rs), m)

    async def go():
        await FLOWS["member_char"](h.command(), reg, action="delete")
        await h.pick("Pick the character", next(o.value for o in h._items()[0].options if o.label == m.main.label))
        assert "handed back and the bench is asked" in h.text and f"Delete {m.main.label}" in h.buttons()
        await h.press(f"Delete {m.main.label}")
        assert "✅ Deleted" in h.text
    run(go())
    assert ev.seat_of(m) is None and reg.members[m.discord_id].main.label == "Keeper"


def test_me_char_add_opens_the_registration(reg, rs):
    m = real(reg, 0)
    h = as_member(Harness(reg, rs), m)
    run(FLOWS["member_char"](h.command(), reg, action="add"))
    assert "alt" in h.text.lower()


def test_me_char_name_relabels_the_open_sheet_through_the_verb(reg, rs):
    """/me char → Name hands over to the naming wizard, which writes a one-row table through THE character verb: an
    open sheet's Join on the planned label ('Priest (Holy)') takes the new name (Registry.name_character alone left it,
    and a later retire / respec ripple, matching by label, then missed it)."""
    m = reg.set_plan(9_200_000_000_000_001, "Launcher", "Priest", "Holy", None, "main")[0]
    planned = m.main.label
    ev = real_run(reg, rs)
    rc.set_signup(reg, rs, ev, m, planned, "in", source="member")
    h = as_member(Harness(reg, rs), m)

    async def go():
        await FLOWS["member_char"](h.command(), reg)
        assert "Name" in h.buttons()
        await h.press("Name")
        await h.submit(first="Brightwing", last="")
        assert "now names Brightwing" in h.text and "Name it Brightwing" in h.buttons()  # the ripple, from code, before the write
        assert ev.signups[str(m.discord_id)].character == planned
        await h.press("Name it Brightwing")
        assert "✅" in h.text and "now names Brightwing" in h.text
    run(go())
    assert reg.members[m.discord_id].main.label == "Brightwing"
    assert ev.signups[str(m.discord_id)].character == "Brightwing" and ev.signups[str(m.discord_id)].status == "in"


def test_roster_confirm_is_a_one_row_table_save(reg, rs, officer, monkeypatch):
    from oibot_gm import discord_people

    calls = []
    verb = discord_people.save_characters

    async def spy(*a, **kw):
        calls.append((a[2], a[3], kw.get("officer")))
        return await verb(*a, **kw)
    monkeypatch.setattr(discord_people, "save_characters", spy)
    m = real(reg, 0)
    h = Harness(reg, rs)

    async def go():
        await FLOWS["roster_confirm"](h.command(), reg)
        await h.pick(h.selects()[0], next(o.value for o in h._items()[0].options if o.value.endswith("|" + m.main.label)))
        await h.press("Confirm")
        assert f"Confirmed **{m.main.label}**" in h.text
    run(go())
    assert calls == [(m.discord_id, [{"label": m.main.label, "confirm": True}], True)]
    assert reg.members[m.discord_id].main.confirmed_by


# ---------------------------------------------------------------- /roster char (an officer)

def test_roster_char_is_officer_only_and_offers_rank(reg, rs, officer):
    m = real(reg, 0)
    h = Harness(reg, rs)
    run(FLOWS["member_char"](h.command(), reg, member=user(m)))
    assert "Rank" in h.buttons() and m.display_name in h.text
    officer["officer"] = False
    h2 = Harness(reg, rs)
    run(FLOWS["member_char"](h2.command(), reg, member=user(m), action="rank"))
    assert "Officers only" in h2.text


# ---------------------------------------------------------------- absences: the preview refuses an overlap; I'm back

def test_absence_wizard_shows_an_overlap_before_confirm(reg, rs):
    m = real(reg, 0)
    start = reg.now_local().date() + timedelta(days=4)
    reg.add_absence(m.discord_id, start.isoformat(), (start + timedelta(days=2)).isoformat(), None, "Kessa")
    h = as_member(Harness(reg, rs), m)

    async def go():
        await FLOWS["absence"](h.command(), reg)
        await h.submit(start=(start + timedelta(days=1)).isoformat(), days="3")
        assert "already away" in h.text and "Kessa" in h.text and "Change the dates" in h.buttons() and "Confirm" not in h.buttons()
    run(go())
    assert len(reg.members[m.discord_id].absences) == 1


def test_me_absent_clear_is_im_back_with_what_reopens(reg, rs):
    m = real(reg, 0)
    ev = real_run(reg, rs)
    day = rc.run_day(reg, ev)
    reg.add_absence(m.discord_id, day, None, None, m.display_name)
    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
    h = as_member(Harness(reg, rs), m)

    async def go():
        await FLOWS["absence_clear"](h.command(), reg)  # one absence: straight to the decide screen
        assert "re-opens" in h.text and "I'm back" in h.buttons()
        await h.press("I'm back")
        assert "✅ Your absence" in h.text
    run(go())
    assert not reg.members[m.discord_id].absences


def test_roster_back_clears_someone_elses_absence(reg, rs, officer):
    m = real(reg, 0)
    start = (reg.now_local().date() + timedelta(days=6)).isoformat()
    reg.add_absence(m.discord_id, start, None, "dentist", m.display_name)
    reg.add_absence(m.discord_id, (reg.now_local().date() + timedelta(days=20)).isoformat(), None, None, m.display_name)
    h = Harness(reg, rs)

    async def go():
        await FLOWS["absence_clear"](h.command(), reg, member=user(m))
        await h.pick("Pick the absence", start)  # two on file: pick one
        assert "dentist" in h.text and "I'm back" in h.buttons()
        await h.press("I'm back")
        assert f"{m.display_name}'s absence" in h.text
    run(go())
    assert [a.start for a in reg.members[m.discord_id].absences] != [start] and len(reg.members[m.discord_id].absences) == 1
    officer["officer"] = False  # a later press by someone who lost the role is refused (the guard)


def test_the_absences_card_has_im_back_and_a_clear_reaches_the_flow(reg, rs):
    m = real(reg, 0)
    start = (reg.now_local().date() + timedelta(days=6)).isoformat()
    reg.add_absence(m.discord_id, start, None, None, m.display_name)
    assert [b.item.label for b in absences_view(reg).children] == ["I'll be away", "I'm back", "My absences"]
    h = as_member(Harness(reg, rs), m)
    h.client.registries = Guilds.of(reg.store, reg)

    async def go():
        await AbsenceButton("back", reg.key).callback(h.card_press())
        assert "I'm back" in h.buttons()
        clear = AbsenceButton("clear", reg.key, m.discord_id, start, label="Clear")
        assert clear.item.custom_id == f"abs.{reg.key}:clear:{m.discord_id}:{start}"
        await clear.callback(h.card_press())
        assert "I'm back" in h.buttons()
        await h.press("I'm back")
    run(go())
    assert not reg.members[m.discord_id].absences


# ---------------------------------------------------------------- owner transfer (D50)

@pytest.fixture
def in_server(monkeypatch):
    who = {}

    async def member_of(bot, reg, uid, timeout=None, max_age=None):
        return who.get(uid, ("no", None))
    monkeypatch.setattr(guild_routing, "member_of", member_of)
    return who


def test_owner_transfer_one_decide_screen(reg, rs, in_server):
    m = real(reg, 0)
    in_server[m.discord_id] = ("yes", SimpleNamespace(display_name=m.display_name, bot=False))
    h = Harness(reg, rs, user=SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"))

    async def go():
        await FLOWS["owner_transfer"](h.command(), reg, user=user(m))
        assert "becomes the owner" in h.text and "You lose owner rights" in h.text and "Transfer ownership" in h.buttons()
        await h.press("Transfer ownership")
        assert "✅" in h.text
    run(go())
    assert reg.config.owner_discord_id == m.discord_id


@pytest.mark.parametrize("case", ["bot", "self", "outsider"])
def test_owner_transfer_refusals(reg, rs, in_server, case):
    m = real(reg, 0)
    h = Harness(reg, rs, user=SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"))
    target = {"bot": user(m, bot=True), "self": SimpleNamespace(id=OWNER, display_name="Owner", bot=False), "outsider": user(m)}[case]
    if case == "bot":
        in_server[m.discord_id] = ("yes", SimpleNamespace(display_name=m.display_name, bot=True))
    run(FLOWS["owner_transfer"](h.command(), reg, user=target))
    assert {"bot": "A bot can't", "self": "already own", "outsider": "not in"}[case] in h.text and "Pick someone else" in h.buttons()
    assert reg.config.owner_discord_id == OWNER


def test_owner_transfer_is_the_owners(reg, rs, in_server):
    m = real(reg, 0)
    h = as_member(Harness(reg, rs), m)
    run(FLOWS["owner_transfer"](h.command(), reg, user=user(m)))
    assert "Owner only" in h.text and reg.config.owner_discord_id == OWNER


# ---------------------------------------------------------------- /roster overview: ephemeral, one field per schedule, Post

def test_roster_overview_is_ephemeral_and_officers_can_post_it(reg, officer):
    import discord
    from discord import app_commands
    from test_parity_verbs import Chan

    reg.set_raid_overrides("barrow_deeps", {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    reg.set_schedule("barrow_deeps", "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    for i in range(3):
        m = real(reg, i)
        reg.regular_add(m.discord_id, "barrow_deeps:default", "t")
    reg.config.roster_channel_id = 66
    ch = Chan(66, "roster")
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    discord_registry.register_commands(tree, SimpleNamespace(for_interaction=lambda i: reg), None, lambda kind, key: "")
    overview = tree.get_command("roster").get_command("overview")
    said, edits, follow = [], [], []

    async def send_message(content=None, **kw):
        said.append(kw)

    async def edit_message(**kw):
        edits.append(kw)

    async def followup(content=None, **kw):
        follow.append(content)
    itx = SimpleNamespace(guild_id=reg.config.discord_guild_id, user=SimpleNamespace(id=1, roles=[]), response=SimpleNamespace(send_message=send_message))
    run(overview.callback(itx))
    out = said[0]
    names = [f.name for f in out["embed"].fields]
    assert out["ephemeral"] is True and "Regulars · Raid nights" in names and "Regulars · Alt run" in names
    btn = out["view"].children[0]
    assert btn.label == "Post to the roster channel"
    press = SimpleNamespace(guild_id=reg.config.discord_guild_id, user=SimpleNamespace(id=1, roles=[]), client=SimpleNamespace(get_channel=lambda cid: ch if cid == 66 else None),
                            response=SimpleNamespace(send_message=send_message, edit_message=edit_message), followup=SimpleNamespace(send=followup))
    run(btn.callback(press))
    assert ch.sent and any(f.name.startswith("Regulars") for f in ch.sent[0]["embed"].fields) and edits and follow
