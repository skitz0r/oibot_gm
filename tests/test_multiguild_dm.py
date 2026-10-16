"""Several Discord servers, stage S3 (design §5.32 "DMs and buttons"): the DM server picker (asked every time, only the
asker may press, it expires), persistent custom ids that carry their guild (`<prefix>.<gk>:…`, old ids still working),
guild-name prefixes in DMs only when the bot serves several guilds, and must_apply in a DM. Isolation tests stand on
the two-guild fixture (`duo`) and ONE run key that exists in both guilds (`same_run`)."""
from __future__ import annotations

import asyncio
import functools
import re
import types

import discord
import pytest
from conftest import DUO_SERVER, GUILD, GUILD2, MemberDirectory, same_run

from oibot_gm import discord_help, discord_news, discord_pool, discord_registry as dr, flows_register, guild_picker, guild_routing
from oibot_gm import raid_views, raidcycle as rc
from oibot_gm.discord_bot import OibotGM
from oibot_gm.discord_raid import RaidMixin
from oibot_gm.discord_registry import Guilds
from oibot_gm.raid_buttons import FillButton, PlaceButton, RunButton, SignupButton
from oibot_gm.registry import Registry

PUPPET = Registry.TEST_BASE  # the first puppet: the same Discord id in both guilds (their test benches share ids)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- fakes

class Resp:
    def __init__(self, itx):
        self.itx, self._done = itx, False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kw):
        assert not self._done, "a second response to one interaction"
        self._done = True
        self.itx._sent_id = 700_000 + len(self.itx.said)
        self.itx.said.append(content if content is not None else kw.get("embed"))
        self.itx.views.append(kw.get("view"))

    async def edit_message(self, **kw):
        self._done = True
        self.itx.edits.append(kw)

    async def defer(self, **kw):
        self._done = True

    async def send_modal(self, modal):
        self._done = True


class Itx:
    """A fake interaction: a DM (guild_id None) unless `guild_id`; `said` = every message sent on it."""

    def __init__(self, client, uid=PUPPET, guild_id=None, name="Pressed"):
        self.client, self.guild_id, self.guild = client, guild_id, None
        self.user = types.SimpleNamespace(id=uid, display_name=name, mention=f"<@{uid}>", roles=[], guild_permissions=types.SimpleNamespace(manage_guild=True))
        self.extras: dict = {}
        self.said: list = []
        self.views: list = []
        self.edits: list = []
        self.original_edits: list = []
        self._sent_id = None
        self.response = Resp(self)
        self.message = CardMessage("the question")
        self.type = discord.InteractionType.component
        self.followup = types.SimpleNamespace(send=self._followup)
        self.namespace = types.SimpleNamespace()

    async def _followup(self, content=None, **kw):
        self.said.append(content if content is not None else kw.get("embed"))
        self.views.append(kw.get("view"))

    async def edit_original_response(self, **kw):
        self.original_edits.append(kw)

    async def original_response(self):
        return types.SimpleNamespace(id=self._sent_id)


def dm_bot(duo, members=None, flaky=None):
    """A client over the two guilds: the real RaidMixin.may_answer_for, recording stubs for the verbs a press ends in."""
    stores = {GUILD: duo.rs_a, GUILD2: duo.rs_b}
    bot = types.SimpleNamespace(registries=duo.guilds, raids=types.SimpleNamespace(store=lambda r: stores[r.key]), did=[], ico=lambda kind, key: "")

    async def is_officer_anywhere(reg, uid):
        bot.did.append(("officer?", reg.key))
        return True

    async def after_fill_answer(reg, rs, ev, ask, line):
        bot.did.append(("fill", reg.key, line))

    async def after_placement_answer(reg, uid, roster, yes, line):
        bot.did.append(("place", reg.key, yes))

    async def apply_signup(interaction, reg, rs, ev, m, character, status):
        bot.did.append(("signup", reg.key, status))
        await interaction.response.send_message("ok", ephemeral=True)

    async def lock_run(reg, rs, ev, by="scheduler"):
        bot.did.append(("lock", reg.key))
        return f"locked {ev.key}"

    async def emit(cfg, level, text, exc=None, dm=True):
        bot.did.append(("ops", cfg.key, text))
    bot.is_officer_anywhere, bot.after_fill_answer, bot.after_placement_answer = is_officer_anywhere, after_fill_answer, after_placement_answer
    bot.apply_signup, bot.lock_run, bot.ops = apply_signup, lock_run, types.SimpleNamespace(emit=emit)
    bot.may_answer_for = functools.partial(RaidMixin.may_answer_for, bot)
    MemberDirectory(members if members is not None else {(1, PUPPET): object(), (DUO_SERVER, PUPPET): object()}, flaky).install(bot)
    return bot


def ask(uid=PUPPET, name="Puppet"):
    return rc.FillAsk(discord_id=uid, display_name=name, kind="sub", role="healer", character=name, spec="Holy", reason="short 1 healer")


async def press_picker(itx_or_msg, label, uid=PUPPET, client=None):
    """Wait until the picker is on screen, then press the button named `label` as `uid`; returns the press."""
    for _ in range(200):
        views = [v for v in getattr(itx_or_msg, "views", []) if isinstance(v, guild_picker.GuildPicker)]
        if views:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("no picker was shown")
    view = views[-1]
    btn = next(b for b in view.children if b.label == label)
    press = Itx(client or getattr(itx_or_msg, "client", None), uid=uid)
    if await view.interaction_check(press):
        await btn.callback(press)
    return press


# ---------------------------------------------------------------- custom ids

TEMPLATES = [
    (RunButton, "runact:k-1:lock", "runact.duo:k-1:lock"),
    (SignupButton, "raid:k-1:in", "raid.duo:k-1:in"),
    (FillButton, "fill:k-1:42:yes", "fill.duo:k-1:42:yes"),
    (PlaceButton, "place:k-1:42:no", "place.duo:k-1:42:no"),
    (dr.RegisterButton, "reg:apply", "reg.duo:apply"),
    (dr.ApplicationButton, "app:accept:42", "app.duo:accept:42"),
    (discord_help.GuideSelect, "guide:menu", "guide.duo:menu"),
    (discord_pool.AbsenceButton, "abs:new", "abs.duo:new"),
    (discord_news.ProposalButton, "newsprop:260101-abcd:approve", "newsprop.duo:260101-abcd:approve"),
]


@pytest.mark.parametrize("cls,old,new", TEMPLATES, ids=[t[0].__name__ for t in TEMPLATES])
def test_every_template_matches_its_old_and_new_id_and_only_its_own(cls, old, new):
    t = cls.__discord_ui_compiled_template__
    assert t.fullmatch(old)["g"] is None, "an old id still matches (g None)"
    assert t.fullmatch(new)["g"] == GUILD2
    for other, o_old, o_new in TEMPLATES:
        if other is not cls:
            assert not t.fullmatch(o_old) and not t.fullmatch(o_new), f"{cls.__name__} must not claim {other.__name__}'s ids"


def test_the_bot_emits_new_ids_and_they_round_trip():
    async def go():
        made = [RunButton(GUILD2, "k-1", "lock"), SignupButton(GUILD2, "k-1", "in"), FillButton(GUILD2, "k-1", 42, "yes"), PlaceButton(GUILD2, "k-1", 42, "no"),
                dr.RegisterButton("apply", GUILD2), dr.ApplicationButton("accept", 42, GUILD2), discord_help.GuideSelect(GUILD2),
                discord_pool.AbsenceButton("new", GUILD2), discord_news.ProposalButton(GUILD2, "260101-abcd", "approve")]
        for item, (_, _, new) in zip(made, TEMPLATES):
            assert item.custom_id == new
    run(go())


def test_the_longest_ids_fit_discords_100():
    key = "bd-" + "x" * 39  # a 42-character run key
    gk = "g" * 16
    assert len(key) == 42 and len(RunButton(gk, key, "suggest", rev="f" * 16).custom_id) <= 100
    assert len(FillButton(gk, key, 10 ** 19, "yes").custom_id) <= 100 and len(PlaceButton(gk, key, 10 ** 19, "yes").custom_id) <= 100
    assert len(SignupButton(gk, key, "cant").custom_id) <= 100


def test_layouts_carry_the_guild_key(duo):
    from oibot_gm.wizard import lint

    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    _, view = raid_views.sheet_message(duo.b, ev_b, rc.run_team(duo.b, ev_b), lambda k, v: "")
    assert {c.custom_id.split(":")[0] for c in view.children} == {"raid.duo"} and not lint(view)
    health = raid_views.health_layout(duo.b, duo.rs_b, ev_b, rc.run_team(duo.b, ev_b), lambda k, v: "")
    ids = [c.custom_id for c in health.walk_children() if getattr(c, "custom_id", None)]
    assert ids and all(i.startswith("runact.duo:") for i in ids) and not lint(health)
    fill = raid_views.fill_layout(duo.b, ev_b, rc.run_team(duo.b, ev_b), lambda k, v: "", ask())
    assert all(c.custom_id.startswith("fill.duo:") for c in fill.walk_children() if getattr(c, "custom_id", None))


# ---------------------------------------------------------------- new ids in a DM, with one run key in both guilds

def test_a_fill_answer_lands_in_its_own_guild_only(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    for rs, ev in ((duo.rs_a, ev_a), (duo.rs_b, ev_b)):
        ev.fill_asks.append(ask())
        rs.save(ev, "test ask")
    bot = dm_bot(duo)
    i = Itx(bot)
    run(FillButton(GUILD2, ev_b.key, PUPPET, "no").callback(i))
    assert [d for d in bot.did if d[0] == "fill"] == [("fill", GUILD2, "Puppet can't (sub)")]
    assert duo.rs_b.events[ev_b.key].fill_asks[-1].answer == "no" and duo.rs_a.events[ev_a.key].fill_asks[-1].answer is None, "demo's ask is untouched"
    assert i.extras["guild"] == GUILD2 and not any(isinstance(v, guild_picker.GuildPicker) for v in i.views), "never asked"


def test_a_placement_confirm_lands_in_its_own_guild_only(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    for r in (duo.a, duo.b):
        r.add_placement_ask(PUPPET, ev_b.team, "Puppet", "t")
    bot = dm_bot(duo)
    run(PlaceButton(GUILD2, ev_b.team, PUPPET, "yes").callback(Itx(bot)))
    assert ("place", GUILD2, True) in bot.did
    assert not duo.b.open_placement_asks(PUPPET) and duo.a.open_placement_asks(PUPPET), "demo still waits for its answer"


def test_a_sheet_join_and_an_officer_lock_land_in_their_own_guild(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    bot = dm_bot(duo)
    run(SignupButton(GUILD2, ev_b.key, "in").callback(Itx(bot)))
    i = Itx(bot)
    run(RunButton(GUILD2, ev_b.key, "lock").callback(i))  # the decide screen first (consequence · Lock now · Back): nothing locked yet
    assert [d for d in bot.did if d[0] == "lock"] == [] and i.views[-1] is not None
    screen = i.views[-1]
    press = Itx(bot)
    press.message = types.SimpleNamespace(id=screen.message_id)
    run(next(b for b in screen.children if b.label == "Lock now").callback(press))
    assert [d for d in bot.did if d[0] in ("signup", "lock")] == [("signup", GUILD2, "in"), ("lock", GUILD2)]


def test_a_guild_button_pressed_in_another_server_is_refused_before_any_officer_check(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    bot = dm_bot(duo)
    for server in (1, 999):  # demo's own server, and an unconfigured one whose admin has Manage Server (K1)
        i = Itx(bot, guild_id=server)
        run(RunButton(GUILD2, ev_b.key, "lock").callback(i))
        assert i.said == [guild_routing.OTHER_SERVER], server
    gone = Itx(bot)
    run(RunButton("nope", ev_b.key, "lock").callback(gone))
    assert gone.said == [guild_routing.GONE]
    assert bot.did == [], "is_officer_anywhere never ran, nothing was locked"


def test_a_channel_card_with_a_guild_works_only_in_that_guilds_server(duo, monkeypatch):
    seen = []
    monkeypatch.setattr(dr, "is_officer", lambda i, r: seen.append(r.key) or False)
    bot = dm_bot(duo)
    for server, gk in ((1, GUILD2), (DUO_SERVER, GUILD), (None, GUILD2)):
        i = Itx(bot, guild_id=server)
        run(dr.ApplicationButton("accept", 5, gk).callback(i))
        assert i.said == ["Officers only."]
    assert seen == []
    run(dr.ApplicationButton("accept", 5, GUILD2).callback(Itx(bot, guild_id=DUO_SERVER)))
    assert seen == [GUILD2]


# ---------------------------------------------------------------- old ids

def test_an_old_button_with_one_guild_resolves_as_before(reg, rs, store):
    ev = rc.open_run(reg, rs, "barrow_deeps", reg.now_local().replace(second=0, microsecond=0) + __import__("datetime").timedelta(days=3), by="t",
                     cutoffs={"soft": 2, "hard": 1.5, "confirm": 1})
    ev.fill_asks.append(ask())
    rs.save(ev, "ask")
    bot = types.SimpleNamespace(registries=Guilds.of(store, reg), raids=types.SimpleNamespace(store=lambda r: rs), did=[])

    async def after(reg_, rs_, ev_, ask_, line):
        bot.did.append(line)
    bot.after_fill_answer = after
    bot.is_officer_anywhere = None
    bot.may_answer_for = functools.partial(RaidMixin.may_answer_for, bot)
    run(FillButton(None, ev.key, PUPPET, "no").callback(Itx(bot)))
    assert bot.did == ["Puppet can't (sub)"]


def test_an_old_button_with_several_guilds_uses_its_matcher(duo, monkeypatch):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    duo.b.add_placement_ask(PUPPET, ev_b.team, "Puppet", "t")  # only duo asked
    bot = dm_bot(duo)
    run(PlaceButton(None, ev_b.team, PUPPET, "yes").callback(Itx(bot)))
    assert ("place", GUILD2, True) in bot.did, "one guild matches: no question"
    nobody = Itx(bot)
    run(PlaceButton(None, "nothing-here", PUPPET, "yes").callback(nobody))
    assert nobody.said == ["That question has closed — nothing changed."], "no match: the button's own closed line"


def test_an_old_button_matching_both_guilds_asks_which(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    for rs, ev in ((duo.rs_a, ev_a), (duo.rs_b, ev_b)):
        ev.fill_asks.append(ask())
        rs.save(ev, "test ask")
    bot = dm_bot(duo)

    async def go():
        i = Itx(bot)
        task = asyncio.create_task(FillButton(None, ev_b.key, PUPPET, "no").callback(i))
        press = await press_picker(i, "Duo Guild", client=bot)
        await task
        return i, press
    i, press = run(go())
    assert i.said[0] == guild_picker.PROMPT and press.extras["guild"] == GUILD2
    assert duo.rs_b.events[ev_b.key].fill_asks[-1].answer == "no" and duo.rs_a.events[ev_a.key].fill_asks[-1].answer is None
    line = next(d[2] for d in bot.did if d[0] == "fill")
    assert press.edits == [{"content": f"**→ {line}**", "view": None}], "the picker shows the answer"
    assert i.message.edits == [{"content": f"the question\n\n**→ {line}**", "view": None}], "and the DM card itself loses its buttons"


class CardMessage:
    """The DM card a button sits on: the bot's own message, edited directly after the server picker."""

    def __init__(self, content):
        self.id, self.content, self.edits = 1, content, []

    async def edit(self, **kw):
        self.edits.append(kw)


def test_an_old_placement_button_answered_through_the_picker_closes_the_card(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    for r, ev in ((duo.a, ev_a), (duo.b, ev_b)):
        r.add_placement_ask(PUPPET, ev.team, "Puppet", "t")
    bot = dm_bot(duo)

    async def go():
        i = Itx(bot)
        i.message = CardMessage("You're rostered — confirm?")
        task = asyncio.create_task(PlaceButton(None, ev_b.team, PUPPET, "yes").callback(i))
        press = await press_picker(i, "Duo Guild", client=bot)
        await task
        return i, press
    i, press = run(go())
    assert ("place", GUILD2, True) in bot.did and not duo.b.open_placement_asks(PUPPET) and duo.a.open_placement_asks(PUPPET)
    assert len(press.edits) == 1 and press.edits[0]["content"].startswith("**→ ") and press.edits[0]["view"] is None
    assert len(i.message.edits) == 1 and i.message.edits[0]["content"].startswith("You're rostered — confirm?\n\n**→ ") and i.message.edits[0]["view"] is None


def test_a_new_id_answer_edits_only_its_own_card(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    duo.b.add_placement_ask(PUPPET, ev_b.team, "Puppet", "t")
    bot = dm_bot(duo)
    i = Itx(bot)
    i.message = CardMessage("You're rostered — confirm?")
    run(PlaceButton(GUILD2, ev_b.team, PUPPET, "yes").callback(i))
    assert len(i.edits) == 1 and i.edits[0]["content"].startswith("You're rostered — confirm?\n\n**→ ")
    assert i.message.edits == [], "no picker: the press's own edit_message is the card's"


# ---------------------------------------------------------------- the picker

def test_a_slash_command_in_a_dm_asks_every_time_and_continues_on_the_press(duo):
    bot = dm_bot(duo)

    async def once(label):
        i = Itx(bot)
        task = asyncio.create_task(guild_routing.need(i))
        press = await press_picker(i, label, client=bot)
        reg, got = await task
        return i, press, reg, got
    i, press, reg, got = run(once("Duo Guild"))
    assert reg is duo.b and got is press and press.extras["guild"] == GUILD2 and not press.response.is_done(), "the command answers the press"
    assert i.said == [guild_picker.PROMPT] and i.original_edits[-1]["content"] == "For **Duo Guild**:" and i.original_edits[-1]["view"] is None
    assert duo.guilds.for_interaction(press) is duo.b, "the sync resolver reads the choice"
    _, _, again, _ = run(once("Demo Guild"))
    assert again is duo.a, "never remembered: asked again, and the other answer counts"


def test_only_the_asker_may_press_and_the_picker_expires(duo):
    bot = dm_bot(duo)
    regs = duo.guilds.ordered()

    async def stranger():
        i = Itx(bot)
        task = asyncio.create_task(guild_picker.choose_guild(i, regs, timeout=0.2))
        p = await press_picker(i, "Duo Guild", uid=9999, client=bot)
        out = await task
        return i, p, out
    i, p, out = run(stranger())
    assert p.said == ["That question was for someone else."] and out == (None, None)
    assert i.original_edits[-1] == {"content": guild_picker.EXPIRED, "view": None}, "the expired picker says so and loses its buttons"


def test_picker_labels_are_cut_and_names_escaped(duo):
    duo.b.config.name = "*Duo*" + "x" * 120

    async def go():
        v = guild_picker.GuildPicker(duo.guilds.ordered(), 1)
        return [b.label for b in v.children]
    labels = run(go())
    assert all(len(lab) <= 80 for lab in labels) and guild_picker.guild_name(duo.b).startswith("\\*Duo\\*")


def test_help_in_a_dm_asks_then_shows_that_guilds_tiers(duo):
    from discord import app_commands

    from oibot_gm.discord_help import register_help_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_help_commands(tree, duo.guilds, None, None)
    bot = dm_bot(duo)

    async def plain_identity(reg, user, **_):
        return reg.key == GUILD2, []
    bot.plain_identity = plain_identity

    async def go():
        i = Itx(bot)
        task = asyncio.create_task(tree.get_command("help").callback(i))
        press = await press_picker(i, "Duo Guild", client=bot)
        await task
        return press
    press = run(go())
    assert any(f.name == "Officers" for f in press.said[0].fields), "an officer in duo (the guild picked) sees the officer tier"


def test_plain_dm_from_someone_in_both_asks_then_answers_for_the_chosen_guild(duo):
    bot = dm_bot(duo)
    handled = []

    async def plain_text(message, reg, text, channel):
        handled.append((reg.key, text, channel))

    async def news_message(message):
        return False
    bot.plain_text, bot.news_message = plain_text, news_message
    bot.dm_guild = functools.partial(OibotGM.dm_guild, bot)

    class Sent:
        def __init__(self):
            self.edits = []

        async def edit(self, **kw):
            self.edits.append(kw)

    class Msg:
        guild = None
        content = "when is the next raid?"

        def __init__(self):
            self.author = types.SimpleNamespace(id=PUPPET, bot=False)
            self.views, self.replies, self.sent = [], [], Sent()

        async def reply(self, text, view=None, **kw):
            self.replies.append(text)
            self.views.append(view)
            return self.sent

    async def go():
        m = Msg()
        task = asyncio.create_task(OibotGM.on_message(bot, m))
        press = await press_picker(m, "Duo Guild", client=bot)
        await task
        return m, press
    m, press = run(go())
    assert m.replies == [guild_picker.PROMPT] and handled == [(GUILD2, "when is the next raid?", None)]
    assert press.edits == [{"content": "For **Duo Guild**:", "view": None}], "the press is acknowledged on the picker itself"


def test_plain_dm_from_someone_in_neither_lists_both_join_links(duo):
    duo.a.config.invite_url, duo.b.config.invite_url = "https://discord.gg/demo", "https://discord.gg/duo"
    bot = dm_bot(duo, members={})
    out = []
    msg = types.SimpleNamespace(author=types.SimpleNamespace(id=PUPPET), reply=lambda text, **kw: _rec(out, text))
    assert run(OibotGM.dm_guild(bot, msg)) is None
    assert "https://discord.gg/demo" in out[0] and "https://discord.gg/duo" in out[0]


async def _rec(out, text):
    out.append(text)


# ---------------------------------------------------------------- the guild's name in DMs, only with several guilds

class DMBot(RaidMixin):
    def __init__(self, registries):
        self.registries = registries
        self.sent = []

    async def fetch_user(self, uid):
        bot = self

        class U:
            async def send(self, text=None, view=None):
                bot.sent.append(text)
        return U()


def test_dm_text_and_cards_name_the_guild_only_when_there_are_several(duo, reg, store):
    one = Guilds.of(store, reg)
    m1 = reg.members[PUPPET]
    m1.test = False
    bot1 = DMBot(one)
    run(bot1.send_member_dm(reg, m1, "Your seat was released."))
    assert bot1.sent == ["Your seat was released."] and not reg.many_guilds, "one guild: exactly as before"
    m2 = duo.b.members[PUPPET]
    m2.test = False
    bot2 = DMBot(duo.guilds)
    run(bot2.send_member_dm(duo.b, m2, "Your seat was released."))
    run(bot2.send_member_dm(duo.b, m2, "Duo Guild · the sheet is open"))
    assert bot2.sent == ["**Duo Guild** · Your seat was released.", "Duo Guild · the sheet is open"], "named once, never twice"
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    head = raid_views._header(duo.b, ev_b, "You're rostered", [])
    text = head.content if isinstance(head, discord.ui.TextDisplay) else head.children[0].content
    assert text.startswith("## Duo Guild · You're rostered")
    single = raid_views._header(reg, ev_a, "You're rostered", [])
    assert (single.content if isinstance(single, discord.ui.TextDisplay) else single.children[0].content).startswith("## You're rostered")


# ---------------------------------------------------------------- must_apply in a DM

def test_register_in_a_dm_reads_roles_from_the_guild_member(reg):
    reg.config.member_role_ids = [777]
    newcomer = 42

    async def plain_identity(reg_, user, **_):
        return False, [777]
    dm = types.SimpleNamespace(guild_id=None, user=types.SimpleNamespace(id=newcomer, roles=[]), client=types.SimpleNamespace(plain_identity=plain_identity))
    assert run(flows_register.must_apply(dm, reg)) is False, "a member-role holder registers directly, DMs included"
    bare = types.SimpleNamespace(guild_id=None, user=types.SimpleNamespace(id=newcomer, roles=[]), client=types.SimpleNamespace())
    assert run(flows_register.must_apply(bare, reg)) is True


# ---------------------------------------------------------------- the claim guard is off (K11)

def test_a_second_server_can_be_claimed_now(duo):
    reg = duo.guilds.create(types.SimpleNamespace(id=31, name="Third"), 1, "forever")
    duo.guilds.add(reg)
    assert reg.key == "third" and len(duo.guilds.ordered()) == 3 and all(r.many_guilds for r in duo.guilds.ordered())


def test_every_persistent_custom_id_in_the_source_carries_the_guild():
    """One rule (CLAUDE.md): each DynamicItem template has the optional `.<gk>` group."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
    templates = re.findall(r'DynamicItem\[[^\]]+\], template=r"([^"]+)"', "\n".join(p.read_text() for p in src.glob("*.py")))
    assert len(templates) == 10 and all(r"(?:\.(?P<g>[a-z0-9_-]{1,16}))?" in t for t in templates), templates  # + cfgapply (the diff Apply/Cancel)


def test_help_in_a_dm_when_the_picker_runs_out_answers_once(duo, monkeypatch):
    """The picker was /help's response; when nobody presses it says "No server picked" and /help stops there (a second
    response would raise InteractionResponded and post an apology under the expired picker)."""
    from discord import app_commands

    from oibot_gm.discord_help import register_help_commands

    monkeypatch.setattr(guild_picker.choose_guild, "__defaults__", (guild_picker.PROMPT, 0.05))
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_help_commands(tree, duo.guilds, None, None)
    bot = dm_bot(duo)
    i = Itx(bot)
    run(tree.get_command("help").callback(i))  # the fake's send_message asserts "a second response to one interaction"
    assert i.said == [guild_picker.PROMPT] and i.original_edits[-1] == {"content": guild_picker.EXPIRED, "view": None}


def test_a_command_failing_after_the_picker_reaches_the_chosen_guild_and_answers_the_press(duo):
    """discord.py hands tree.on_error the ORIGINAL interaction: pick() tags it with the chosen guild (ops line and owner
    DM go there, not just the log) and keeps the press, which the apology answers."""
    bot = dm_bot(duo)
    errs = []

    async def emit(cfg, level, text, exc=None, dm=True):
        errs.append((cfg.key, level, text))
    owner = types.SimpleNamespace(registries=duo.guilds, ops=types.SimpleNamespace(emit=emit))

    async def go():
        i = Itx(bot)
        i.command = types.SimpleNamespace(qualified_name="raid lock")
        task = asyncio.create_task(guild_routing.need(i))
        press = await press_picker(i, "Duo Guild", client=bot)
        reg, got = await task
        assert reg is duo.b and got is press
        await OibotGM._on_command_error(owner, i, RuntimeError("boom"))
        return i, press
    i, press = run(go())
    assert i.extras["guild"] == GUILD2 and errs and errs[0][:2] == (GUILD2, "error") and "boom" in errs[0][2]
    assert press.said == ["Something went wrong; the owner has been notified."] and press.response.is_done()
    assert i.said == [guild_picker.PROMPT], "nothing more under the picker itself"


def test_an_error_with_no_guild_never_claims_the_owner_was_told(duo):
    owner = types.SimpleNamespace(registries=duo.guilds, ops=types.SimpleNamespace(emit=None))
    i = Itx(dm_bot(duo))
    i.command = None
    run(OibotGM._on_command_error(owner, i, RuntimeError("boom")))
    assert i.said == ["Something went wrong; it has been logged."]


def test_a_guild_that_ignores_dms_is_never_offered_and_all_ignoring_is_silence(duo):
    """The DM picker counts only the guilds that answer DMs (plain-text policy dm != ignore): one of two ignoring → the
    other, no question; both ignoring → silence, for members and strangers alike (no Join-links reply)."""
    bot = dm_bot(duo)
    bot.dm_guild = functools.partial(OibotGM.dm_guild, bot)
    out = []
    msg = types.SimpleNamespace(author=types.SimpleNamespace(id=PUPPET), reply=lambda text, **kw: _rec(out, text))
    duo.a.config.plain.dm = "ignore"
    assert run(OibotGM.dm_guild(bot, msg)) is duo.b and out == [], "only duo answers DMs: no picker"
    duo.b.config.plain.dm = "ignore"
    assert run(OibotGM.dm_guild(bot, msg)) is None and out == [], "nobody answers DMs: no picker leading nowhere"
    stranger = dm_bot(duo, members={})
    assert run(OibotGM.dm_guild(stranger, msg)) is None and out == [], "and no Join-links reply either"
    duo.b.config.plain.dm = "self"
    assert run(OibotGM.dm_guild(stranger, msg)) is None and len(out) == 1 and "not in any server" in out[0]
