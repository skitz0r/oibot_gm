"""The loot council's Discord surface (dual interfaces phase 1, design §3.1 decisions 5, 6, 8, 9): the thread on the
officer channel with its pinned session card, the proposal cards (≤5 items per message, grouped by boss, the controls on
the newest message), the persistent LootButton / LootPick / lootmodal ids (a press or a submit after a restart), the
Tick drops… and Override… wizards, the ephemeral item card that edits itself, the companion lines' buttons, End with the
decide screen, the scheduler's loot steps, the addressed-only thread chat and /gm precedents.

Discord is faked (channels, threads and messages that record what was sent and edited); the verbs are the real
LootMixin's over a scratch GitStore, so every press is checked against what the session and the ledger now hold."""
from __future__ import annotations

import asyncio
import re
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import pytest
import yaml

from conftest import GUILD, join, lock_with_board
from oibot_gm import discord_registry, flows_loot, raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm import wizard_flows  # noqa: F401 — registers every flow
from oibot_gm.discord_feed import FeedMixin
from oibot_gm.discord_loot import LootMixin
from oibot_gm.discord_loot_surface import LootChatConfirmView, LootSurfaceMixin, loot_digest
from oibot_gm.loot import session as ls
from oibot_gm.loot.session import DISENCHANT
from oibot_gm.raid_buttons import LootButton, LootPick, LootStartButton, RunButton
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

OFFICER = 777001
MEMBER = 777002
BOT_ID = 990


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- a fake Discord

class FakeMsg:
    def __init__(self, ch, mid, content=None, view=None, embed=None):
        self.channel, self.id, self.content, self.view, self.embed = ch, mid, content, view, embed
        self.pinned = False
        self.thread = None

    async def edit(self, **kw):
        if "content" in kw:
            self.content = kw["content"]
        if "view" in kw:
            self.view = kw["view"]
        self.channel.edits.append((self.id, kw))

    async def pin(self):
        self.pinned = True

    async def unpin(self):
        self.pinned = False

    async def create_thread(self, name):
        t = self.channel.world.thread(name, parent=self.channel)
        self.thread = t
        return t


class FakeChan:
    def __init__(self, world, cid, name="officers", parent=None):
        self.world, self.id, self.name = world, cid, name
        self.parent_id = parent.id if parent is not None else None
        self.sent: list[FakeMsg] = []
        self.edits: list = []
        self.archived = False

    async def send(self, content=None, *, view=None, embed=None, **kw):
        m = FakeMsg(self, self.world.next_id(), content, view, embed)
        self.sent.append(m)
        return m

    def message(self, mid):
        return next((m for m in self.sent if m.id == mid), None)

    def get_partial_message(self, mid):
        return self.message(mid) or FakeMsg(self, mid)

    async def fetch_message(self, mid):
        return self.get_partial_message(mid)

    async def edit(self, **kw):
        self.archived = kw.get("archived", self.archived)

    def typing(self):
        class _T:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False
        return _T()


class World:
    def __init__(self):
        self.ids = 5000
        self.channels: dict[int, FakeChan] = {}

    def next_id(self):
        self.ids += 1
        return self.ids

    def chan(self, name="officers"):
        c = FakeChan(self, self.next_id(), name)
        self.channels[c.id] = c
        return c

    def thread(self, name, parent):
        t = FakeChan(self, self.next_id(), name, parent=parent)
        self.channels[t.id] = t
        return t


class RecOps:
    def __init__(self):
        self.lines: list[tuple[str, str, str]] = []

    async def emit(self, cfg, level, text, exc=None, dm=True):
        self.lines.append((cfg.key, level, text))


class FakeUser:
    def __init__(self, uid, dms):
        self.id, self.dms = uid, dms

    async def send(self, text):
        self.dms.append((self.id, text))


class SurfBot(FeedMixin, LootSurfaceMixin, LootMixin):
    """The verbs + the Discord surface as the client has them, on a fake Discord."""

    def __init__(self, reg, rs, world, *, officer_ch=True, provider=None):
        self.registries = types.SimpleNamespace(by_key={reg.key: reg}, many=False, only=lambda: reg, ordered=lambda: [reg],
                                                get=lambda k: reg if k in (reg.key, reg.config.name) else None,
                                                cfg_for=lambda k: reg.config, for_interaction=lambda i: reg)
        self.raids = types.SimpleNamespace(store=lambda r: rs)
        self.events: dict = {}
        self.ctx = None
        self.loot_provider = provider
        self.ops = RecOps()
        self.feed = None
        self.world = world
        self.officer_ch = world.chan("officers") if officer_ch else None
        self.officers = {OFFICER, reg.config.owner_discord_id}
        self.dms: list = []
        self.asked: list = []
        self.user = types.SimpleNamespace(id=BOT_ID)
        self.help_reply = "Because the table says so."

    @staticmethod
    def ico(kind, key):
        return f"<{kind}:{key}>"

    def get_channel(self, cid):
        return self.world.channels.get(cid)

    def officer_channel(self, reg, ev=None):
        return self.officer_ch

    async def role_of(self, reg, user, *, max_age=0):
        uid = getattr(user, "id", user)
        return "officer" if uid in self.officers else "member"

    async def identity(self, reg, user, *, max_age=0):
        from oibot_gm.guild_routing import Resolved

        return Resolved(await self.role_of(reg, user, max_age=max_age), [], False)

    async def plain_identity(self, reg, user, *, max_age=0):
        r = await self.identity(reg, user, max_age=max_age)
        return r.officer, r.role_ids

    async def fetch_user(self, uid):
        return FakeUser(uid, self.dms)

    async def help_answer(self, reg, uid, officer, text, may_act=False, public=False, data=None):
        self.asked.append({"text": text, "data": data, "may_act": may_act})
        return self.help_reply


def real_run(reg, rs, n=10, days=3):
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    ms = reg.test_members()[:n]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    return ev, ms


@pytest.fixture
def officer(monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)


@pytest.fixture
def night(reg, rs):
    """A locked real run with its council open on the fake Discord."""
    world = World()
    ev, ms = real_run(reg, rs)
    reg.config.roster_channel_id = 4242  # configured; the fake officer channel stands for it
    bot = SurfBot(reg, rs, world)
    s, created = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=OFFICER))
    assert created
    return types.SimpleNamespace(bot=bot, reg=reg, rs=rs, ev=ev, s=s, members=ms, world=world, ch=bot.officer_ch,
                                 thread=world.channels[s.raid_thread_id])


def officer_h(n, user_id=OFFICER, name="Mira"):
    h = Harness(n.reg, n.rs, client=n.bot, user=types.SimpleNamespace(id=user_id, display_name=name, mention=f"@{name}"))
    return h


def items_of(view):
    """Every component in a layout or view, DynamicItems as themselves."""
    out = []

    def walk(it):
        out.append(it)
        for c in getattr(it, "children", []) or []:
            walk(c)
        acc = getattr(it, "accessory", None)
        if acc is not None:
            walk(acc)
    for c in view.children:
        walk(c)
    return out


def loot_buttons(view) -> list[LootButton]:
    return [it for it in items_of(view) if isinstance(it, LootButton)]


def button(view, label) -> LootButton:
    hit = [b for b in loot_buttons(view) if b.item.label == label]
    assert hit, f"no {label!r}; have {[b.item.label for b in loot_buttons(view)]}"
    return hit[-1]


def labels(view) -> list[str]:
    """A plain View's button labels (a thread line's), DynamicItems or not."""
    return [getattr(b, "item", b).label for b in view.children]


def text_of(view) -> str:
    return "\n".join(it.content for it in items_of(view) if isinstance(it, discord.ui.TextDisplay))


def count_components(view) -> int:
    def walk(node) -> int:
        if isinstance(node, list):
            return sum(walk(x) for x in node)
        if not isinstance(node, dict):
            return 0
        n = 1 if "type" in node else 0
        return n + walk(node.get("components") or []) + (walk(node["accessory"]) if node.get("accessory") else 0)
    return walk(view.to_components())


def card(n):
    return n.thread.message(n.s.cards["loot"]).view


def batch_msgs(n):
    return [n.thread.message(mid) for k, mid in n.s.cards.items() if k.startswith("batch:")]


def tick(n, name="Shard of Doom", boss="Gatekeeper", copies=1, by="Mira"):
    return run(n.bot.tick(n.reg, n.s, add=[{"name": name, "boss": boss, "copies": copies}], by=by, by_id=OFFICER))


# ---------------------------------------------------------------- open: the thread, the card

def test_open_posts_a_public_thread_on_the_officer_channel_with_the_pinned_session_card(night):
    n = night
    starter = n.ch.sent[0]
    assert starter.thread is n.thread and f"<#{n.thread.id}>" in starter.content and "Loot council" in starter.content
    first = n.thread.sent[0]
    assert first.id == n.s.cards["loot"] and first.pinned and n.s.cards["starter"] == starter.id and n.s.cards["channel"] == n.ch.id
    v = first.view
    assert [b.item.label for b in loot_buttons(v)] == ["Tick drops…", "Distribute", "Scores only: off", "End council"]
    assert button(v, "Distribute").item.disabled, "nothing to judge yet"
    body = text_of(v)
    seated = [p for r in n.ev.all_rosters for p in r.selected]
    lines = [ln for ln in body.split("\n") if ln.startswith("<spec:")]
    assert len(lines) == len(seated), "one member per line, the spec icon alone (no role icon beside it)"
    assert not any("<role:" in ln for ln in lines) and "waiting for drops" in body
    assert all(re.fullmatch(rf"loot\.{GUILD}:{re.escape(n.s.id)}:-:[a-z]+", b.item.custom_id) for b in loot_buttons(v)), "ids carry the guild key"
    saved = ls.load_sessions(n.reg.store)[(GUILD, n.s.id)]
    assert saved.cards["loot"] == first.id, "the card ids are on the session (a restart finds them)"


def test_open_refuses_without_an_officer_channel(reg, rs):
    ev, _ = real_run(reg, rs)
    bot = SurfBot(reg, rs, World(), officer_ch=False)
    from oibot_gm.registry import RegistryError

    with pytest.raises(RegistryError, match="Set a roster or ops channel first"):
        run(bot.open_loot(reg, rs, ev, by="Officer"))
    assert not bot.events


def test_a_test_run_council_is_a_rehearsal_thread(reg, rs):
    from conftest import open_test_run

    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    reg.config.roster_channel_id = 4242
    world = World()
    bot = SurfBot(reg, rs, world)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    assert s.test and world.channels[s.raid_thread_id].name.startswith("rehearsal · ")
    assert "Rehearsal" in text_of(world.channels[s.raid_thread_id].sent[0].view)


# ---------------------------------------------------------------- tick, distribute, the proposal cards

def test_a_tick_posts_its_line_and_redraws_the_card(night):
    n = night
    tick(n)
    assert any("📥 **Shard of Doom** 🆕 from **Gatekeeper**" in (m.content or "") for m in n.thread.sent)
    body = text_of(card(n))
    assert "Gatekeeper ▫️ 1" in body and "1 to judge" in body and "untiered tonight" in body
    assert not button(card(n), "Distribute").item.disabled
    line = next(m for m in n.thread.sent if (m.content or "").startswith("📥"))
    assert line.view is not None and labels(line.view) == ["Distribute"], "a human tick's line offers Distribute"


def test_distribute_posts_cards_of_at_most_five_items_grouped_by_boss_controls_last(night):
    n = night
    tick(n, copies=5)
    tick(n, name="Gem of Testing", boss="Gatekeeper")
    tick(n, name="Cloak of Later", boss="Second Boss")
    res = run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    assert len(res["proposals"]) == 7 and res["scores_only"]
    msgs = batch_msgs(n)
    assert [len(k[6:].split(",")) for k in n.s.cards if k.startswith("batch:")] == [5, 1, 1], "≤5 per card, a boss per card"
    for i, m in enumerate(msgs):
        labels = [b.item.label for b in loot_buttons(m.view)]
        assert ("Confirm the rest (7)" in labels) == (i == len(msgs) - 1), "the batch controls only on the newest card"
        assert count_components(m.view) <= 40, "Discord's 40 components per message"
    assert "### ⚖️ Gatekeeper" in text_of(msgs[0].view) and "### ⚖️ Second Boss" in text_of(msgs[2].view)
    first = text_of(msgs[0].view)
    assert "→ " in first and "✎ scores only" in first and "🆕 untiered" in first
    assert "1 to judge" not in text_of(card(n)) and "7 proposal(s) pending" in text_of(card(n))


def test_confirm_on_a_card_records_and_edits_the_card_in_place(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    h = officer_h(n)
    p = n.s.pending("d1")
    ok = [b for b in loot_buttons(m.view) if b.action == "ok" and b.drop == "d1"][0]
    assert ok.arg == views.loot_pick_hash(p.to)
    run(ok.callback(h.card_press()))
    a = n.s.awarded("d1")
    assert a is not None and a.raider == p.to and a.by == "Mira"
    (m2,) = batch_msgs(n)
    assert m2.id == m.id, "edited in place, not re-posted"
    assert f"✅ **Shard of Doom** → **{p.to}** · recorded by Mira" in text_of(m2.view)
    assert "Confirm the rest (1)" in [b.item.label for b in loot_buttons(m2.view)]
    assert any(line[2].startswith("Mira: loot") or "recorded" in line[2] for line in n.bot.ops.lines)


def test_a_stale_confirm_the_rest_is_refused_and_the_cards_redrawn(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    stale = button(m.view, "Confirm the rest (2)")
    other = next(p.character or p.signup_name for p in n.bot.loot_roster(n.s) if (p.character or p.signup_name) != n.s.pending("d1").to)
    run(n.bot.override(n.reg, n.s, "d1", other, "tank needs it more", by="Jonny", rejudge=False))
    h = officer_h(n)
    run(stale.callback(h.card_press()))
    assert "The batch changed" in h.text and "Jonny overrode" in h.text and "as it is now" in h.text
    assert not n.s.awards, "nothing recorded from a stale view"


def test_a_moved_pick_refuses_the_cards_confirm(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    ok = [b for b in loot_buttons(m.view) if b.action == "ok"][0]
    other = next(p.character or p.signup_name for p in n.bot.loot_roster(n.s) if (p.character or p.signup_name) != n.s.pending("d1").to)
    run(n.bot.override(n.reg, n.s, "d1", other, "tank needs it more", by="Jonny", rejudge=False))
    h = officer_h(n)
    run(ok.callback(h.card_press()))
    assert "The pick changed" in h.text and not n.s.awards


def test_decide_later_holds_one_drop_and_discard_asks_first(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    h = officer_h(n)
    later = [b for b in loot_buttons(m.view) if b.action == "later" and b.drop == "d1"][0]
    run(later.callback(h.card_press()))
    assert n.s.pending("d1") is None and n.s.drop("d1").held
    (m,) = batch_msgs(n)
    assert "held (Decide later)" in text_of(m.view)

    async def go():
        await button(m.view, "Discard batch").callback(h.card_press())
        assert "The proposal goes, and the ticks stay." in h.text and "Nothing was recorded" in h.text
        assert n.s.pending("d2") is not None, "nothing before the press"
        await h.press("Discard")
    run(go())
    assert not n.s.proposals and "discarded" in h.text


def test_the_item_card_edits_itself_after_confirm(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    h = officer_h(n)

    async def go():
        await button(m.view, "Details").callback(h.card_press())
        v = h.screen["view"]
        body = text_of(v)
        assert "No tier data for this item" in body and "**Candidates**" in body and "<spec:" in body
        assert [b.item.label for b in loot_buttons(v)] == ["Confirm", "Override…", "Disenchant"]
        assert any(isinstance(it, LootPick) and it.kind == "whynot.d1" for it in items_of(v))
        iok = button(v, "Confirm")
        itx = h._on_screen()
        await iok.callback(itx)
    run(go())
    v = h.screen["view"]
    assert "✅ recorded by you" in text_of(v) and not loot_buttons(v), "the ephemeral card shows the outcome, buttons gone"
    assert n.s.awarded("d1") is not None


def test_why_not_is_code_comparing_two_candidates(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    p = n.s.pending("d1")
    other = next(c.character for c in p.result.candidates if c.character != p.to)
    h = officer_h(n)
    pick = LootPick(GUILD, n.s.id, "whynot.d1", [discord.SelectOption(label=other, value=other)])
    run(flows_loot.loot_pick(h.card_press(), pick, [other]))
    assert h.text.startswith(f"**{p.item_name}**: {p.to}") and other in h.text


# ---------------------------------------------------------------- Override… (two presses: the grouped select, the reason modal)

def test_override_is_a_grouped_select_then_the_reason_modal_whose_submit_is_the_write(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    p = n.s.pending("d1")
    h = officer_h(n)
    rev = ls.batch_rev(n.s)

    async def go():
        await button(m.view, "Override…").callback(h.card_press())
        assert h.selects() == ["Award to…"] and "the bot picked" in h.text
        sel = h.screen["view"].children[0]
        descs = [o.description for o in sel.options]
        assert any(d.startswith("In the table") for d in descs) and descs[-2:] == ["nobody gets it — recorded as disenchanted", "type their character's name"]
        assert p.to not in [o.value for o in sel.options], "the current pick isn't offered again"
        target = next(o.value for o in sel.options if (o.description or "").startswith("In the table"))
        await h.pick("Award to…", target)
        modal = h.modal
        assert isinstance(modal, flows_loot.LootModal) and modal.title.startswith("Shard of Doom → ")
        assert modal.custom_id == f"lootmodal.{GUILD}:{n.s.id}:d1:ovr:{rev}", "the reason box's id survives a restart"
        assert not n.s.pending("d1").override, "nothing before the submit"
        await h.submit(who=target, reason="tanks first this tier")
        return target
    target = run(go())
    o = n.s.pending("d1").override
    assert o.to == target and o.reason == "tanks first this tier" and o.by == "Mira" and o.by_id == OFFICER
    assert "not recorded until Confirm" in h.text and h.screen["view"] is None, "the wizard message became the outcome"


def test_override_for_someone_not_on_the_roster_takes_a_validated_name(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    h = officer_h(n)

    async def go():
        await FLOWS["loot_override"](h.card_press(), n.reg, sid=n.s.id, drop="d1", rev=None)
        await h.pick("Award to…", flows_loot.NOT_LISTED)
        assert isinstance(h.modal._items["who"], discord.ui.TextInput)
        modal = h.modal
        await h.submit(modal, who="Bad Name 9", reason="a guest helped us")
        assert "isn't a character name" in h.text and not n.s.pending("d1").override
        await h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d1", "ovr", item="Shard of Doom", who_text=True), who="Strangér", reason="a guest helped us")
        # nobody by that name (not signed up, not registered, never a guest): a typo or a new guest — a second press
        assert "Nobody called **Strangér**" in h.text and not n.s.pending("d1").override
        guest = button(h.screen["view"], "Record Strangér as a guest")
        assert re.fullmatch(rf"loot\.{GUILD}:{re.escape(n.s.id)}:d1:guest", guest.item.custom_id)
        await guest.callback(h.card_press())
    run(go())
    o = n.s.pending("d1").override
    assert o.to == "Strangér" and o.reason == "a guest helped us"


def test_a_typed_name_takes_the_spelling_the_signups_registry_or_ledger_have(night):
    """L16: 'not on the roster' is validated against the run's signups (a benched joiner), the registered characters and
    the guests the ledger already knows — a match records at once, in their spelling."""
    n = night
    benched = n.reg.test_members()[10]
    join(n.reg, n.rs, n.ev, [benched], status="sub")
    bench_char = n.ev.signups[str(benched.discord_id)].character
    tick(n, copies=3)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    h = officer_h(n)
    run(h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d1", "ovr", item="Shard of Doom", who_text=True), who=bench_char.upper(), reason="bench gets one"))
    assert n.s.pending("d1").override.to == bench_char and "Nobody called" not in h.text
    # a registered character who never signed up
    seated = {(p.character or p.signup_name).lower() for p in n.bot.loot_roster(n.s)} | {bench_char.lower()}
    other = next(c for m in n.reg.members.values() for c in m.characters if c.name and c.label.lower() not in seated and " " not in c.label)
    run(h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d2", "ovr", item="Shard of Doom", who_text=True), who=other.label.lower(), reason="an alt's turn"))
    assert n.s.pending("d2").override.to == other.label
    # a guest the ledger recorded by name before
    n.bot.loot_ctx(n.s).ledger.append(types.SimpleNamespace(raider="Pugzilla", discord_id=None))
    run(h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d3", "ovr", item="Shard of Doom", who_text=True), who="pugzilla", reason="our regular pug"))
    assert n.s.pending("d3").override.to == "Pugzilla"


def test_a_second_officers_override_asks_to_replace(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    names = [p.character or p.signup_name for p in n.bot.loot_roster(n.s) if (p.character or p.signup_name) != n.s.pending("d1").to]
    run(n.bot.override(n.reg, n.s, "d1", names[0], "first officer's call", by="Jonny", rejudge=False))
    h = officer_h(n)

    async def go():
        await h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d1", "ovr", item="Shard of Doom", who=names[1]), who=names[1], reason="my call instead")
        assert "Jonny already set this" in h.text and "replace?" in h.text
        rep = button(h.screen["view"], "Replace their override…")
        await rep.callback(h.card_press())
        assert isinstance(h.modal, flows_loot.LootModal) and h.modal.custom_id.endswith(":ovr:r")
        await h.submit(who=names[1], reason="my call instead")
    run(go())
    assert n.s.pending("d1").override.to == names[1] and n.s.pending("d1").override.by == "Mira"


def test_disenchant_from_the_item_card_records_at_once(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    h = officer_h(n)

    async def go():
        await LootButton(GUILD, n.s.id, "d1", "info").callback(h.card_press())
        await button(h.screen["view"], "Disenchant").callback(h._on_screen())
        assert h.modal.custom_id.startswith(f"lootmodal.{GUILD}:{n.s.id}:d1:de")
        await h.submit(who=DISENCHANT, reason="nobody needs it")
    run(go())
    a = n.s.awarded("d1")
    assert a.raider == DISENCHANT and a.source == "override"


def test_override_on_a_recorded_drop_points_at_correct(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    run(n.bot.confirm(n.reg, n.s, only={"d1"}, by="Mira"))
    h = officer_h(n)
    run(FLOWS["loot_override"](h.card_press(), n.reg, sid=n.s.id, drop="d1"))
    assert "use Correct" in h.text


# ---------------------------------------------------------------- Tick drops…

def guild_items(reg, boss="Gatekeeper", n=30):
    d = reg.store.root / reg.key / "items"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"{91000 + i}.yaml").write_text(yaml.safe_dump({"id": 91000 + i, "name": f"Item {i:02d}", "slot": "chest", "type": "cloth", "boss": boss, "tiers": {}}))
    reg.__dict__.pop("_items_cache", None)


def test_tick_wizard_names_a_boss_and_an_item_not_on_the_table(night):
    n = night
    h = officer_h(n)

    async def go():
        await button(card(n), "Tick drops…").callback(h.card_press())
        assert h.selects() == ["Which boss?"] and "Un-tick…" not in h.buttons()
        await h.pick("Which boss?", flows_loot.NEW_BOSS)
        await h.submit(boss="Gatekeeper")
        assert "no loot table yet" in h.text and "Not on the table…" in h.buttons()
        await h.press("Not on the table…")
        await h.submit(name="Shard of Doom", copies="2")
        assert "Tick **Shard of Doom** from **Gatekeeper** — 2 of each?" in h.text
        assert not n.s.drop_list, "nothing before Tick"
        await h.press("Tick")
    run(go())
    assert [d.item_name for d in n.s.drop_list] == ["Shard of Doom", "Shard of Doom"] and n.s.drop_list[0].boss == "Gatekeeper"
    assert "📥 Ticked" in h.text and h.buttons() == ["More from Gatekeeper", "Another boss", "Done"]


def test_tick_wizard_pages_25_items_at_a_time(night):
    n = night
    guild_items(n.reg)
    tick(n, name="Item 00", boss="Gatekeeper")  # the boss is now known to the council
    h = officer_h(n)

    async def go():
        await FLOWS["loot_tick"](h.card_press(), n.reg, sid=n.s.id)
        await h.pick("Which boss?", "Gatekeeper")
        assert "page 1 of 2" in h.text and len(h.screen["view"].children[0].options) == 25
        await h.press("Next ▶")
        assert "page 2 of 2" in h.text
        opts = [o.value for o in h.screen["view"].children[0].options]
        assert len(opts) == 5
        await h.pick("What dropped from Gatekeeper?", opts[0], opts[1])
        await h.pick("How many of each?", "1")
        await h.press("Tick")
    run(go())
    assert [d.item_name for d in n.s.drop_list][1:] == ["Item 25", "Item 26"] and all(d.item_id for d in n.s.drop_list[1:])


def test_untick_refuses_a_recorded_drop_and_removes_the_rest(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    run(n.bot.confirm(n.reg, n.s, only={"d1"}, by="Mira"))
    h = officer_h(n)

    async def go():
        await FLOWS["loot_tick"](h.card_press(), n.reg, sid=n.s.id)
        await h.press("Un-tick…")
        assert [o.value for o in h.screen["view"].children[0].options] == ["d2"], "a recorded drop isn't offered"
        await h.pick("Un-tick…", "d2")
        await h.press("Un-tick")
    run(go())
    assert [d.drop_id for d in n.s.drop_list] == ["d1"]


# ---------------------------------------------------------------- companion lines and their buttons

def test_a_mismatch_line_carries_give_the_reason_and_mistake_correct(night):
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    run(n.bot.loot_notify(n.reg, n.s, "⚠ mismatch", kind="mismatch", drop_id="d1"))
    line = n.thread.sent[-1]
    assert labels(line.view) == ["Give the reason", "Mistake — correct…"]
    assert all(b.item.custom_id.startswith(f"loot.{GUILD}:{n.s.id}:d1:") for b in line.view.children)
    run(n.bot.loot_notify(n.reg, n.s, "📒 manual", kind="manual", drop_id="d1"))
    assert labels(n.thread.sent[-1].view) == ["Wrong? Fix…"]
    run(n.bot.loot_notify(n.reg, n.s, "⚠ conflict", kind="conflict", drop_id="d1"))
    assert labels(n.thread.sent[-1].view) == ["Correct…"]


def companion_mismatch(n):
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    p = n.s.pending("d1")
    other = next(c.character for c in p.result.candidates if c.character != p.to)
    from oibot_gm.feed import Companion

    comp = Companion("Lootmaster", "test", GUILD, "peer")
    run(n.bot.handle_feed_event({"type": "loot", "item_name": "Shard of Doom", "recipient": other, "idem": "m1"}, comp))
    assert n.s.awarded("d1").raider == other and n.s.pending_reasons
    return other


def test_give_the_reason_writes_the_precedent_and_a_second_asks_to_replace(night):
    n = night
    other = companion_mismatch(n)
    line = next(m for m in n.thread.sent if isinstance(m.view, discord.ui.View) and "Give the reason" in labels(m.view))
    why = next(b.item for b in line.view.children if b.item.label == "Give the reason")
    assert "Give a reason or correct…" in [it.item.placeholder for it in items_of(card(n)) if isinstance(it, LootPick)], "the card lists what is owed"
    h = officer_h(n)

    async def go():
        btn = await LootButton.from_custom_id(h.card_press(), why, re.fullmatch(LootButton.__discord_ui_compiled_template__, why.custom_id))
        await btn.callback(h.card_press())
        assert h.modal.custom_id == f"lootmodal.{GUILD}:{n.s.id}:d1:why"
        await h.submit(reason="he was promised it last week")
        assert "📌 Precedent" in h.text
        await h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d1", "why", item="Shard of Doom"), reason="a different reason")
        assert "already gave a reason" in h.text and "replace?" in h.text
        await button(h.screen["view"], "Replace with my reason…").callback(h.card_press())
        assert h.modal.custom_id.endswith(":why:r")
        await h.submit(reason="a different reason")
    run(go())
    precs = n.reg.store.read_jsonl(Path(GUILD) / "precedents.jsonl")
    assert [p["reason"] for p in precs] == ["he was promised it last week", "a different reason"] and precs[-1]["human"] == other
    assert not n.s.pending_reasons


def test_mistake_correct_is_the_grouped_select_then_the_reason(night):
    n = night
    companion_mismatch(n)
    bot_pick = n.s.pending_reasons[0]["bot"]
    h = officer_h(n)

    async def go():
        await LootButton(GUILD, n.s.id, "d1", "fix", label="Mistake — correct…").callback(h.card_press())
        assert h.selects() == ["Record it to…"] and "no precedent" in h.text
        await h.pick("Record it to…", bot_pick)
        assert h.modal.custom_id == f"lootmodal.{GUILD}:{n.s.id}:d1:fix"
        await h.submit(who=bot_pick, reason="the ML misclicked")
    run(go())
    live = [a for a in n.s.awards if not a.void and a.drop_id == "d1"]
    assert [a.raider for a in live] == [bot_pick] and live[0].source == "correct" and not n.s.pending_reasons


# ---------------------------------------------------------------- restarts

def test_every_loot_id_round_trips_its_template(night):
    n = night
    for cid, cls in ((LootButton(GUILD, n.s.id, "d12", "ovr", arg="abc123def0").item.custom_id, LootButton),
                     (LootButton(GUILD, n.s.id, "-", "all", arg="0123456789ab").item.custom_id, LootButton),
                     (LootPick(GUILD, n.s.id, "whynot.d3", [discord.SelectOption(label="x", value="x")]).item.custom_id, LootPick),
                     (LootStartButton(GUILD).item.custom_id, LootStartButton)):
        assert re.fullmatch(cls.__discord_ui_compiled_template__, cid), cid
        assert len(cid) <= 100
    m = re.fullmatch(LootButton.__discord_ui_compiled_template__, f"loot.{GUILD}:{n.s.id}:d12:ovr:abc123def0")
    assert (m["g"], m["sid"], m["drop"], m["action"], m["arg"]) == (GUILD, n.s.id, "d12", "ovr", "abc123def0")
    assert flows_loot.MODAL_RE.fullmatch(f"lootmodal.{GUILD}:{n.s.id}-r2:d4:why:r")
    split = LootButton(GUILD, f"{n.s.id}-r2", "d1", "ok").item.custom_id
    assert re.fullmatch(LootButton.__discord_ui_compiled_template__, split)["sid"] == f"{n.s.id}-r2"


def test_a_press_after_a_restart_reads_the_council_from_disk(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    (m,) = batch_msgs(n)
    cid = [b for b in loot_buttons(m.view) if b.action == "ok" and b.drop == "d2"][0].item.custom_id
    # the process restarts: a new client, the sessions read back from the data repo
    bot2 = SurfBot(n.reg, n.rs, n.world)
    bot2.events = ls.load_sessions(n.reg.store)
    for s in bot2.events.values():
        bot2.loot_index(s)
    h = Harness(n.reg, n.rs, client=bot2, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))

    async def go():
        itx = h.card_press()
        btn = await LootButton.from_custom_id(itx, discord.ui.Button(custom_id=cid), re.fullmatch(LootButton.__discord_ui_compiled_template__, cid))
        await btn.callback(itx)
    run(go())
    s2 = bot2.events[(GUILD, n.s.id)]
    assert s2.awarded("d2") is not None and s2.awarded("d2").by == "Mira"


def test_a_reason_box_submitted_after_a_restart_is_the_same_write(night):
    n = night
    companion_mismatch(n)
    bot2 = SurfBot(n.reg, n.rs, n.world)
    bot2.events = ls.load_sessions(n.reg.store)
    for s in bot2.events.values():
        bot2.loot_index(s)
    h = Harness(n.reg, n.rs, client=bot2, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))
    itx = h.card_press()
    itx.type = discord.InteractionType.modal_submit
    itx.data = {"custom_id": f"lootmodal.{GUILD}:{n.s.id}:d1:why",
                "components": [{"type": 18, "component": {"type": 4, "custom_id": "reason", "value": "promised after the restart"}}]}
    assert run(flows_loot.loot_modal_raw(itx))
    assert "📌 Precedent" in h.text
    assert n.reg.store.read_jsonl(Path(GUILD) / "precedents.jsonl")[-1]["reason"] == "promised after the restart"
    itx2 = h.card_press()
    itx2.data = {"custom_id": "cfgapply.demo:abcdefgh1234:apply"}
    assert not run(flows_loot.loot_modal_raw(itx2)), "not ours: left alone"


def test_a_member_cannot_press_council_buttons(night):
    n = night
    tick(n)
    h = officer_h(n, user_id=MEMBER, name="Pleb")
    run(button(card(n), "Distribute").callback(h.card_press()))
    assert h.text == "Officers only." and n.s.undistributed()


def test_a_press_in_another_server_is_refused(night):
    n = night
    h = officer_h(n)
    h.guild_id = 999
    run(button(card(n), "Tick drops…").callback(h.card_press()))
    assert "another server" in h.text.lower() or "isn't" in h.text.lower()


def test_ready_reports_an_interrupted_distribute_with_a_distribute_button(night):
    n = night
    tick(n)
    n.s.distributing = {"by": "Mira", "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "drops": ["d1"]}
    n.s.save("crash")
    bot2 = SurfBot(n.reg, n.rs, n.world)
    bot2.events = ls.load_sessions(n.reg.store)
    for s in bot2.events.values():
        bot2.loot_index(s)
    run(bot2.loot_restore())
    run(bot2.loot_cards_restore())
    line = n.thread.sent[-1]
    assert line.content.startswith("⚠ Distribute was interrupted at ") and re.search(r"\d{1,2}:\d\d [AP]M", line.content)
    assert labels(line.view) == ["Distribute"]
    assert "interrupted" in text_of(n.thread.message(n.s.cards["loot"]).view)


# ---------------------------------------------------------------- End

def test_end_council_shows_the_decide_screen_then_archives_and_closes_the_thread(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    run(n.bot.confirm(n.reg, n.s, only={"d1"}, by="Mira"))
    h = officer_h(n)

    async def go():
        await button(card(n), "End council").callback(h.card_press())
        assert "is finished and its loot council closes with 1 award." in h.text.split("\n")[1] and "1 proposal never confirmed" in h.text
        assert h.buttons() == ["End", "Not yet"] and n.ev.state == "locked"
        await h.press("End")
    run(go())
    assert n.rs.events[n.ev.key].state == "done" and (GUILD, n.s.id) not in n.bot.events
    assert "1 proposal(s) never confirmed" in h.text
    closed = n.thread.message(n.s.cards["loot"]).view
    assert "Loot council ended" in text_of(closed) and not loot_buttons(closed), "the card's final state has no buttons"
    assert n.thread.archived and "ended" in n.ch.sent[0].content
    assert all(not loot_buttons(m.view) for m in batch_msgs(n))
    assert not n.bot.dms, "the council sends no DMs"


def test_raid_end_prefers_the_run_with_a_council(night, officer):
    n = night
    other = rc.open_run(n.reg, n.rs, "barrow_deeps", (n.reg.now_local() + timedelta(days=6)).replace(second=0, microsecond=0), by="t")
    join(n.reg, n.rs, other, n.members)
    lock_with_board(n.reg, n.rs, other, [m.display_name for m in n.members])
    h = officer_h(n)
    run(FLOWS["loot_end"](h.command(), n.reg))
    assert f"{rc.run_on(n.reg, n.ev)} is finished" in h.text and h.buttons() == ["End", "Not yet"]


def test_raid_distribute_judges_the_live_council(night):
    n = night
    tick(n)
    h = officer_h(n)
    run(FLOWS["loot_distribute"](h.command(), n.reg))
    assert n.s.pending("d1") is not None and "proposal(s)" in h.text


def test_the_lock_cards_loot_button_opens_the_council_flow(reg, rs, monkeypatch, officer):
    world = World()
    ev, _ = real_run(reg, rs)
    reg.config.roster_channel_id = 4242
    bot = SurfBot(reg, rs, world)
    h = Harness(reg, rs, client=bot, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))
    async def anywhere(self, r, uid):
        return True
    monkeypatch.setattr(type(bot), "is_officer_anywhere", anywhere, raising=False)
    btn = RunButton(reg.key, ev.key, "loot")
    assert btn.item.label == "Loot" and re.fullmatch(RunButton.__discord_ui_compiled_template__, btn.item.custom_id)
    run(btn.callback(h.card_press()))
    s = bot.events.get((GUILD, ev.key))
    assert s is not None and s.raid_thread_id and f"<#{s.raid_thread_id}>" in h.text


def test_the_lock_cards_loot_button_opens_its_own_runs_council_when_two_are_locked(reg, rs, monkeypatch, officer):
    """Regression: the button carries its run's key — never the 'Which run?' picker, never the other run's council."""
    world = World()
    ev_a, _ = real_run(reg, rs, days=2)
    ev_b, _ = real_run(reg, rs, days=5)
    assert ev_a.key != ev_b.key and ev_a.state == ev_b.state == "locked"
    reg.config.roster_channel_id = 4242
    bot = SurfBot(reg, rs, world)
    h = Harness(reg, rs, client=bot, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))

    async def anywhere(self, r, uid):
        return True
    monkeypatch.setattr(type(bot), "is_officer_anywhere", anywhere, raising=False)
    run(RunButton(reg.key, ev_b.key, "loot").callback(h.card_press()))
    assert "Which run?" not in h.text
    s = bot.events.get((GUILD, ev_b.key))
    assert s is not None and s.run_key == ev_b.key and f"<#{s.raid_thread_id}>" in h.text
    assert (GUILD, ev_a.key) not in bot.events, "run A's council was not opened by run B's card"


# ---------------------------------------------------------------- closing, the scheduler's loot steps

def test_closing_notice_has_keep_open_and_the_press_edits_it(night):
    n = night
    run(n.bot.loot_closing(n.reg, n.s))
    line = n.thread.sent[-1]
    assert line.content.startswith("⏳ This council closes in 15 min") and n.s.cards["closing"] == line.id
    assert labels(line.view) == ["Keep open"]
    h = officer_h(n)
    btn = LootButton(GUILD, n.s.id, "-", "keep")
    run(btn.callback(h.card_press()))
    assert n.s.keep_open_until and "Kept open until" in h.text and not re.search(r"\b\d\d:\d\d\b(?! [AP]M)", h.text)


def held(bot, reg, kind="drop"):
    from oibot_gm.feed import Companion

    comp = Companion("Lootmaster", "test", GUILD, "peer")
    ev = {"type": "drop", "boss": "Gatekeeper", "items": [{"name": "Shard"}]} if kind == "drop" else {"type": "loot", "item_name": "Shard", "recipient": "Bob"}
    return run(bot.handle_feed_event(ev, comp))


def test_a_companion_two_councils_could_take_is_bound_on_a_card(reg, rs):
    """L10: two live councils and a companion rostered on neither → refused (the error ack, an ops warn) and every live
    council card offers Bind companion…; binding it on run B's card sends its next drop to B."""
    from oibot_gm.feed import Companion

    world = World()
    ev_a, _ = real_run(reg, rs, days=2)
    ev_b, _ = real_run(reg, rs, days=5)
    reg.config.roster_channel_id = 4242
    bot = SurfBot(reg, rs, world)
    s_a, _ = run(bot.open_loot(reg, rs, ev_a, by="Officer", by_id=OFFICER))
    s_b, _ = run(bot.open_loot(reg, rs, ev_b, by="Officer", by_id=OFFICER))
    comp = Companion("Lootmaster", "test", GUILD, "peer")
    drop = {"type": "drop", "boss": "Gatekeeper", "items": [{"name": "Shard of Doom"}]}
    ack = run(bot.handle_feed_event(dict(drop, idem="e1"), comp))
    assert "several loot councils are live" in ack["error"] and not s_a.drop_list and not s_b.drop_list
    assert bot.loot_unrouted(GUILD) == ["Lootmaster"]

    def card_of(s):
        return world.channels[s.raid_thread_id].message(s.cards["loot"]).view

    for s in (s_a, s_b):
        picks = [it for it in items_of(card_of(s)) if isinstance(it, LootPick) and it.kind == "bind"]
        assert picks and picks[0].item.placeholder == "Bind companion…" and [o.value for o in picks[0].item.options] == ["Lootmaster"]
        assert re.fullmatch(LootPick.__discord_ui_compiled_template__, picks[0].item.custom_id)
        assert "Companion not routed" in text_of(card_of(s))
    edits = len(world.channels[s_a.raid_thread_id].edits)
    run(bot.handle_feed_event(dict(drop, idem="e2"), comp))
    assert len(world.channels[s_a.raid_thread_id].edits) == edits, "the card is redrawn once per companion, not per event"

    h = Harness(reg, rs, client=bot, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))
    pick = LootPick(GUILD, s_b.id, "bind", [discord.SelectOption(label="Lootmaster", value="Lootmaster")])
    run(flows_loot.loot_pick(h.card_press(), pick, ["Lootmaster"]))
    assert "Lootmaster's companion now feeds this council (bound by Mira)" in h.text
    assert not bot.loot_unrouted(GUILD) and not any(isinstance(it, LootPick) and it.kind == "bind" for it in items_of(card_of(s_a)))
    assert any("Lootmaster's companion bound" in t for _, _, t in bot.ops.lines)
    ack = run(bot.handle_feed_event(dict(drop, idem="e3"), comp))
    assert ack.get("session") == f"{GUILD}:{s_b.id}" and [d.item_name for d in s_b.drop_list] == ["Shard of Doom"] and not s_a.drop_list
    run(flows_loot.loot_pick(h.card_press(), pick, ["Lootmaster"]))
    assert "isn't waiting to be bound any more" in h.text


def test_drops_with_no_run_live_prompt_an_ad_hoc_run_once(reg, rs):
    world = World()
    bot = SurfBot(reg, rs, world)
    assert held(bot, reg)["held"]
    run(bot.loot_scheduler_step(reg))
    run(bot.loot_scheduler_step(reg))
    prompts = [m for m in bot.officer_ch.sent if "no run is live" in (m.content or "")]
    assert len(prompts) == 1 and "Lootmaster's log" in prompts[0].content
    (b,) = prompts[0].view.children
    assert b.item.label == "Start an ad hoc run…" and b.item.custom_id == f"lootstart.{GUILD}:open"
    assert not bot.dms


def test_the_ad_hoc_prompt_goes_to_the_officer_channel_never_the_public_loot_log(reg, rs):
    """Regression (integration item 3): the "no run is live — start an ad hoc run?" prompt is officer-only; with the
    public loot log's channel set it still goes to the officer channel, and the log channel gets nothing."""
    world = World()
    bot = SurfBot(reg, rs, world)
    log_ch = world.chan("loot-log")
    reg.config.loot_channel_id, reg.config.loot_log = log_ch.id, True
    held(bot, reg)
    run(bot.loot_scheduler_step(reg))
    assert [m for m in bot.officer_ch.sent if "no run is live" in (m.content or "")], "the officer channel has the prompt"
    assert not log_ch.sent, "the public loot log carries award lines only"


def test_an_award_after_the_council_ended_dms_the_officers_once(night, monkeypatch):
    n = night
    run(n.bot.end_run(n.reg, n.rs, n.ev, by="Mira"))
    monkeypatch.setattr(type(n.bot), "loot_officer_ids", lambda self, reg: [OFFICER, 555])
    held(n.bot, n.reg, kind="loot")
    run(n.bot.loot_scheduler_step(n.reg))
    run(n.bot.loot_scheduler_step(n.reg))
    assert sorted(u for u, _ in n.bot.dms) == [555, OFFICER], "one DM per officer: they must record or correct it"
    assert "after the loot council" in n.bot.dms[0][1] and "Shard → Bob" in n.bot.dms[0][1]
    assert not [m for m in n.ch.sent if "no run is live" in (m.content or "")], "an award after the end is not an ad hoc run"


def test_the_presence_chip_has_three_states(reg):
    rows = [{"character": "Ann", "state": "live"}, {"character": "Bo", "state": "quiet", "last_seen_label": "8:12 PM"},
            {"character": "Cy", "state": "disconnected", "last_seen_label": "8:01 PM"}]
    chip = views.presence_line(reg, rows)
    assert chip == "📡 🟢 Ann live · 🟠 Bo quiet · last seen 8:12 PM · 🔴 Cy disconnected · last seen 8:01 PM"


# ---------------------------------------------------------------- the thread chat (addressed only)

class Msg:
    def __init__(self, n, text, *, author=OFFICER, mention=False, reply_to=None):
        self.content = text
        self.author = types.SimpleNamespace(id=author, display_name="Mira")
        self.mentions = [n.bot.user] if mention else []
        self.role_mentions = []
        self.guild = None
        self.reference = types.SimpleNamespace(message_id=reply_to) if reply_to else None
        self.channel = n.thread
        self.replies: list = []

    async def reply(self, content=None, view=None, **kw):
        self.replies.append((content, view))
        return types.SimpleNamespace(edit=None, id=1)


def test_only_addressed_messages_reach_the_model(night):
    n = night
    line_id = n.thread.sent[-1].id if n.thread.sent else 0
    tick(n)
    companion_line = n.thread.sent[-1].id
    assert n.bot.loot_addressed(Msg(n, "nice drop"), n.s) is None
    assert n.bot.loot_addressed(Msg(n, "nice", reply_to=companion_line), n.s) is None, "a reply to a line is banter"
    assert n.bot.loot_addressed(Msg(n, "? why Bob"), n.s) == "why Bob"
    assert n.bot.loot_addressed(Msg(n, f"<@{BOT_ID}> why Bob", mention=True), n.s) == "why Bob"
    assert n.bot.loot_addressed(Msg(n, "why Bob", reply_to=n.s.cards["loot"]), n.s) == "why Bob"
    assert line_id is not None


def test_a_question_goes_to_help_with_the_proposals_as_data(night):
    n = night
    n.bot.loot_provider = object()
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER, ))
    msg = Msg(n, "? why them")
    run(n.bot.loot_chat(msg, n.s))
    (ask,) = n.bot.asked
    assert ask["text"] == "why them" and "drop d1: Shard of Doom" in ask["data"]
    assert msg.replies == [("Because the table says so.", None)]
    member = Msg(n, "? why them", author=MEMBER)
    run(n.bot.loot_chat(member, n.s))
    assert not member.replies and len(n.bot.asked) == 1, "members' chat in the thread never reaches the model"


def test_without_a_model_the_chat_points_at_the_buttons(night):
    n = night
    msg = Msg(n, "? why")
    run(n.bot.loot_chat(msg, n.s))
    assert msg.replies[0][0].startswith("The bot can't read sentences right now")


def act_here(n, mode="act"):
    """The plain-text mode of the council thread's parent (the officer channel): a thread reads its parent's mode."""
    n.reg.config.plain.channels[str(n.ch.id)] = mode


def test_an_override_sentence_becomes_a_diff_and_apply_waits_for_a_real_reason(night, monkeypatch):
    n = night
    act_here(n)
    from oibot_gm import nl
    from oibot_gm.discord_help import ACT

    n.bot.loot_provider = object()
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    p = n.s.pending("d1")
    other = next(c.character for c in p.result.candidates if c.character != p.to)
    n.bot.help_reply = ACT
    monkeypatch.setattr(n.bot, "loot_ctx", lambda s: types.SimpleNamespace(provider=object(), policy_for_llm=lambda: "policy"))
    monkeypatch.setattr(nl, "parse_loot_feedback", lambda *a: nl.LootFeedback(kind="override", reply="", changes=[nl.LootChange(item_id=1, award_to=other, reason="he needs it")]))
    msg = Msg(n, f"? give the shard to {other}")
    run(n.bot.loot_chat(msg, n.s))
    text, view = msg.replies[-1]
    assert isinstance(view, LootChatConfirmView) and f"council awards {other} — why? (my guess: “he needs it”)" in text and view.apply.item.disabled
    assert view.edit.item.label == "Give the reason…"
    assert not n.s.pending("d1").override, "nothing before Apply"
    view.changes[0].update(reason="tanks progression first", inferred=False)
    view._sync_apply()
    h = officer_h(n)

    async def go():
        itx = h.card_press()
        await view.press_apply(itx)
    run(go())
    o = n.s.pending("d1").override
    assert o.to == other and o.reason == "tanks progression first" and o.by == "Mira"


def chat_diff(n, monkeypatch, fb, mode="act"):
    from oibot_gm import nl
    from oibot_gm.discord_help import ACT

    n.bot.loot_provider = object()
    n.bot.help_reply = ACT
    if mode is not None:
        act_here(n, mode)
    monkeypatch.setattr(n.bot, "loot_ctx", lambda s: types.SimpleNamespace(provider=object(), policy_for_llm=lambda: "policy"))
    monkeypatch.setattr(nl, "parse_loot_feedback", lambda *a: fb)
    msg = Msg(n, "? do it")
    run(n.bot.loot_chat(msg, n.s))
    return msg.replies[-1]


def test_a_request_to_act_follows_the_parent_channels_plain_text_mode(night, monkeypatch):
    """Design §3.1 decision 8: a thread reads its parent channel's plain-text mode and the loot group gates writes
    (Registry.may_plain, the one plain-text check). A 'self' or 'answer' parent answers questions only."""
    from oibot_gm import nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    for mode in ("self", "answer"):
        text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""), mode=mode)
        assert view is None and text.startswith("⛔ Plain text") and f"<#{n.ch.id}>" in text, text
    n.reg.config.plain.dm = "act"  # the DM policy is never what a thread reads
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""), mode="answer")
    assert view is None and "only answers questions" in text
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""), mode="act")
    assert isinstance(view, LootChatConfirmView) and view.channel == n.ch.id


def test_the_chat_parse_is_attributed_to_the_councils_guild(night, monkeypatch):
    """D45: the loot thread's plain-text parse (nl.parse_loot_feedback, in a worker thread) records the council's guild,
    never '' (unattributed); the context is restored afterwards."""
    from oibot_gm import nl
    from oibot_gm.llm import provider as pv

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    seen = []
    from oibot_gm.discord_help import ACT

    n.bot.help_reply = ACT
    act_here(n, "act")
    monkeypatch.setattr(n.bot, "loot_ctx", lambda s: types.SimpleNamespace(provider=object(), policy_for_llm=lambda: "policy"))

    def parse(*a):
        seen.append(pv.CURRENT_GUILD.get())
        return nl.LootFeedback(kind="ignore", reply="")
    monkeypatch.setattr(nl, "parse_loot_feedback", parse)
    run(n.bot.loot_chat(Msg(n, "? do it"), n.s))
    assert seen == [n.reg.key]
    assert pv.CURRENT_GUILD.get() == ""


def test_the_loot_group_gates_a_chat_write(night, monkeypatch):
    """Who may change loot by plain text is the owner's PLAIN_GROUPS choice: closed to officers → refused, even in an act channel."""
    from oibot_gm import nl
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    n.reg.config.plain.groups["loot"] = ["owner"]
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""))
    assert view is None and text.startswith("⛔ Only") and not n.s.awards


def test_apply_rechecks_the_parent_channels_mode_at_the_press(night, monkeypatch):
    """The diff was drawn in an act channel; the owner turned it to 'answer' before Apply → nothing is recorded."""
    from oibot_gm import nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""))
    monkeypatch.undo()
    act_here(n, "answer")
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert "only answers questions" in h.text and not n.s.awards


def test_apply_through_the_loot_op_authorizes_in_the_threads_parent_channel(night, monkeypatch):
    """Regression (once L3's `loot` op is in configops): configops.authorize gets channel=<the thread's parent>, never
    the DM policy (channel=None) — and the presser's live identity."""
    from oibot_gm import configops, nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""))
    seen = {}

    def authorize(reg, ops, author_id, *, officer, role_ids=(), channel="unset", raids=None):
        seen.update(channel=channel, officer=officer, author=author_id, op=ops[0])
        return [], ["refused here"]
    monkeypatch.setattr(configops, "authorize", authorize)
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert seen["channel"] == n.ch.id and seen["officer"] is True and seen["author"] == OFFICER
    assert seen["op"].op == "loot" and seen["op"].field == "confirm" and "refused here" in h.text and not n.s.awards


def test_a_chat_confirm_never_records_a_pick_added_after_the_sentence(night, monkeypatch):
    from oibot_gm import nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="confirm", reply=""))
    from oibot_gm import configops

    line = configops.describe(n.reg, view.req.ops[0])
    assert isinstance(view, LootChatConfirmView) and f"• {line}" in text and "record every pick still pending — Shard of Doom →" in line, "the one describe() wording"
    monkeypatch.undo()  # the real context again for the verbs
    tick(n, name="Unseen")
    run(n.bot.distribute(n.reg, n.s, by="auto"))  # meanwhile a pick nobody saw arrives
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert "The batch changed" in h.text and not n.s.awards, "nothing recorded unseen"


def test_a_chat_award_never_overrides_a_pick_that_moved_after_the_sentence(night, monkeypatch):
    """Regression (integration item 1): the chat diff's award carries the pick the officer saw on that drop
    ('to:<pick>' → LootMixin.override(expect_to=…)); a pick moved since — here by the same officer on the site, so the
    another-officer rule can't catch it — is refused with what changed, never silently replaced."""
    from oibot_gm import nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    p = n.s.pending("d1")
    others = [c.character for c in p.result.candidates if c.character != p.to]
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="override", reply="", changes=[nl.LootChange(item_id=1, award_to=others[0], reason="do it")]))
    assert view.revs == {f"loot:{n.s.id}#d1": f"to:{p.to}"}, "the pick the officer saw"
    monkeypatch.undo()
    run(n.bot.override(n.reg, n.s, "d1", others[1], "changed my mind on the site", by="Mira", by_id=OFFICER, via="web", rejudge=False))
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert f"The pick changed: Shard of Doom now goes to {others[1]}" in h.text, h.text
    assert n.s.pending("d1").override.to == others[1] and n.s.pending("d1").override.reason == "changed my mind on the site"


def test_two_awards_in_one_chat_diff_both_apply(night, monkeypatch):
    """Each award checks only its own drop: the first override (our own write) never trips the second."""
    from oibot_gm import nl

    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    picks = {d: n.s.pending(d) for d in ("d1", "d2")}
    to = {d: next(c.character for c in p.result.candidates if c.character != p.to) for d, p in picks.items()}
    fb = nl.LootFeedback(kind="override", reply="", changes=[nl.LootChange(item_id=1, award_to=to["d1"], reason="do it"),
                                                             nl.LootChange(item_id=2, award_to=to["d2"], reason="do it")])
    text, view = chat_diff(n, monkeypatch, fb)
    monkeypatch.undo()
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert "⛔" not in h.text, h.text
    assert [n.s.pending(d).override.to for d in ("d1", "d2")] == [to["d1"], to["d2"]]


def test_a_chat_award_with_no_reason_asks_why_instead_of_an_error_line(night, monkeypatch):
    """Regression (integration item 4): describe() refuses an award with no reason ("An award needs a reason") — in the
    chat diff that error took the change's place. The line shows the change and asks "why?"; Give the reason… opens the
    reason modal; Apply waits for it."""
    from oibot_gm import nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    p = n.s.pending("d1")
    other = next(c.character for c in p.result.candidates if c.character != p.to)
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="override", reply="", changes=[nl.LootChange(item_id=1, award_to=other, reason="")]))
    assert f"Shard of Doom (d1): bot picked {p.to} → council awards {other} — why? — press Give the reason…" in text, text
    assert "needs a reason" not in text and view.apply.item.disabled and view.edit.item.label == "Give the reason…"
    monkeypatch.undo()
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert not n.s.pending("d1").override, "Apply waits for the reason"


def test_a_chat_override_never_replaces_another_officers(night, monkeypatch):
    from oibot_gm import nl

    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    p = n.s.pending("d1")
    others = [c.character for c in p.result.candidates if c.character != p.to]
    run(n.bot.override(n.reg, n.s, "d1", others[0], "Jonny's call", by="Jonny", rejudge=False))
    text, view = chat_diff(n, monkeypatch, nl.LootFeedback(kind="override", reply="", changes=[nl.LootChange(item_id=1, award_to=others[1], reason="my call")]))
    view.changes[0].update(inferred=False)
    view._sync_apply()
    monkeypatch.undo()
    h = officer_h(n)
    run(view.press_apply(h.card_press()))
    assert n.s.pending("d1").override.by == "Jonny" and "already set this" in h.text


def test_the_digest_numbers_each_drop(night):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    text = loot_digest(n.s, numbered=True)
    assert "item_id 1 = drop d1" in text and "item_id 2 = drop d2" in text


# ---------------------------------------------------------------- /gm precedents → Retire

def test_gm_precedents_retires_one_with_a_reason(night):
    n = night
    companion_mismatch(n)
    run(n.bot.set_reason(n.reg, n.s, "d1", "promised last week", by="Mira"))
    h = officer_h(n)
    iid = f"{n.s.id}-d1"

    async def go():
        await FLOWS["gm_precedents"](h.command(), n.reg)
        assert h.selects() == ["Pick a precedent"]
        await h.pick("Pick a precedent", iid)
        assert "promised last week" in h.text and h.buttons() == ["Retire…", "Back"]
        # the clock rule: the precedent's stored date (ISO) is never what a person reads
        assert not re.search(r"\d{4}-\d\d-\d\d|T\d\d:|\b\d\d:\d\d\b(?! [AP]M)", h.text), h.text
        await h.press("Retire…")
        await h.submit(reason="policy changed this tier")
    run(go())
    assert "Precedent retired" in h.text
    rows = n.reg.store.read_jsonl(Path(GUILD) / "precedents.jsonl")
    assert rows[-1] == {**rows[-1], "kind": "status", "import_id": iid, "status": "retired", "by": "Mira", "reason": "policy changed this tier"}
    assert not n.bot.loot_precedents(n.reg), "the bot stops citing it"
    assert "promised last week" not in n.bot._guild_ctx(n.reg).policy_for_llm()


def test_precedent_dates_read_as_a_person_reads_them(reg):
    """Regression (clock rule): a companion award's reason stores `received` as a full ISO stamp, a council override
    the session's ISO date — /gm precedents and the session picker show neither raw."""
    stamp = flows_loot.when_label(reg, "2026-10-15T23:30:00+00:00")
    assert stamp == reg.local12("2026-10-15T23:30:00+00:00") and re.search(r"\d{1,2}:\d\d [AP]M$", stamp)
    assert "T23" not in stamp and "2026-" not in stamp and "23:30" not in stamp
    assert flows_loot.when_label(reg, "2026-12-24") == "Thu 24 Dec"
    assert flows_loot.when_label(reg, None) == "" and flows_loot.when_label(reg, "someday") == "someday"


def test_gm_precedents_shows_a_companion_reasons_stamp_in_12_hour_time(reg, rs, monkeypatch):
    bot = SurfBot(reg, rs, World())
    row = {"import_id": "x-d1", "item": "Shard", "bot": "Ann", "human": "Bob", "reason": "promised", "by": "Mira",
           "date": "2026-10-15T23:30:00+00:00"}
    monkeypatch.setattr(type(bot), "loot_precedents", lambda self, r, active=True: [row], raising=False)
    h = Harness(reg, rs, client=bot, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))

    async def go():
        await FLOWS["gm_precedents"](h.command(), reg)
        await h.pick("Pick a precedent", "x-d1")
    run(go())
    assert reg.local12("2026-10-15T23:30:00+00:00") in h.text and "2026-10-15T23:30" not in h.text


def test_gm_precedents_with_none_says_so(reg, rs):
    bot = SurfBot(reg, rs, World())
    h = Harness(reg, rs, client=bot, user=types.SimpleNamespace(id=OFFICER, display_name="Mira", mention="@Mira"))
    run(FLOWS["gm_precedents"](h.command(), reg))
    assert "No active precedents yet" in h.text


# ---------------------------------------------------------------- budget (L36)

class QuickProvider:
    """A model stand-in that picks the top score at once and reports a month nearly spent."""

    name = "quick"

    def __init__(self, cost=4.6, budget=5.0):
        self.cost_usd, self.budget_usd = cost, budget

    def cap_for(self, workload):
        return self.budget_usd

    def spent(self, workload):
        return 0.0

    def complete(self, workload, system, user, schema):
        from oibot_gm.models import LootRecommendation

        first = user.split("| 1 | ", 1)[1].split(" |", 1)[0] if "| 1 | " in user else "(nobody)"
        return LootRecommendation(primary=first, justification="top score [R1]", close_call=False, deviates_from_score=False)

    def summary(self):
        return "quick"


def test_the_thread_is_told_once_when_the_loot_budget_is_nearly_spent(night):
    from oibot_gm.discord_loot_surface import loot_budget_note

    assert loot_budget_note(QuickProvider(cost=1.0)) is None and loot_budget_note(None) is None
    assert loot_budget_note(QuickProvider()).startswith("⚠ 92% of this month's LLM budget for loot is spent")
    n = night
    n.bot.loot_provider = QuickProvider()
    n.bot.contexts = {}
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    tick(n, name="Second Thing")
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    notes = [m for m in n.thread.sent if (m.content or "").startswith("⚠ 92%")]
    assert len(notes) == 1, "once per council"


# ---------------------------------------------------------------- the client's wiring

def test_the_client_routes_a_council_threads_messages_and_restart_modals(night, monkeypatch):
    """OibotGM.on_message: in a council's thread only addressed text reaches loot_chat (the /mock chat, its views and
    its per-boss selects are gone); on_interaction hands a modal submit to flows_loot.loot_modal_raw (the restart path);
    the client carries the surface and every loot DynamicItem."""
    from oibot_gm import discord_bot
    from oibot_gm.discord_bot import OibotGM

    n = night
    assert OibotGM.__mro__.index(LootSurfaceMixin) < OibotGM.__mro__.index(LootMixin)
    assert OibotGM.loot_chat is LootSurfaceMixin.loot_chat
    gone = ("mock_loot_chat", "distribute_ui", "post_boss_tables", "post_proposals", "gate")
    assert not any(hasattr(OibotGM, a) for a in gone) and not any(hasattr(discord_bot, a) for a in ("DropsSelect", "DistributeView", "ConfirmView", "MockEvent"))
    src = Path(discord_bot.__file__).read_text()
    assert re.search(r"add_dynamic_items\([^)]*LootButton, LootPick, LootStartButton", src)
    bot = OibotGM.__new__(OibotGM)
    bot.__dict__.update(n.bot.__dict__)
    bot.registries = types.SimpleNamespace(for_guild=lambda gid: n.reg, by_key={GUILD: n.reg})
    chats: list = []

    async def news(m):
        return False

    async def chat(message, session, text=None):
        chats.append((session.id, text))
    bot.news_message = news
    bot.loot_chat = chat
    monkeypatch.setattr(OibotGM, "user", property(lambda self: types.SimpleNamespace(id=BOT_ID)))

    def msg(text):
        m = Msg(n, text)
        m.author.bot = False
        m.guild = types.SimpleNamespace(id=1, me=types.SimpleNamespace(roles=[]))
        n.thread.parent_id = n.ch.id
        return m
    run(bot.on_message(msg("nice drop")))
    run(bot.on_message(msg("? why them")))
    assert chats == [(n.s.id, "why them")]
    seen: list = []

    async def raw(itx):
        seen.append(itx.data["custom_id"])
        return True
    monkeypatch.setattr(flows_loot, "loot_modal_raw", raw)
    itx = types.SimpleNamespace(guild_id=None, type=discord.InteractionType.modal_submit, data={"custom_id": f"lootmodal.{GUILD}:x:d1:why"}, user=None)
    run(bot.on_interaction(itx))
    assert seen == [f"lootmodal.{GUILD}:x:d1:why"]


# ---------------------------------------------------------------- final review fixes (each fails without its fix)

def _spy_answered(n, monkeypatch, verb: str, itx_box: dict) -> list[bool]:
    """Wrap a LootMixin verb: record whether the interaction in `itx_box` was already answered when the verb ran."""
    seen: list[bool] = []
    orig = getattr(n.bot, verb)

    async def spy(*a, **kw):
        seen.append(itx_box["itx"].response.is_done())
        return await orig(*a, **kw)
    monkeypatch.setattr(n.bot, verb, spy)
    return seen


def _submit(h, modal, itx_box, message_id=None, **values):
    """h.submit, keeping the interaction (so a spy can ask whether it was answered before the write)."""
    from wizard_harness import FakeInteraction

    payload = []
    for key, item in modal._items.items():
        if key in values:
            payload.append({"type": 18, "component": ({"type": 3, "custom_id": item.custom_id, "values": [str(values[key])]} if isinstance(item, discord.ui.Select)
                                                      else {"type": 4, "custom_id": item.custom_id, "value": str(values[key])})})
    modal._refresh(None, payload, {})
    itx_box["itx"] = FakeInteraction(h, kind=discord.InteractionType.modal_submit, message_id=message_id)
    return modal.on_submit(itx_box["itx"])


def test_a_reason_or_override_submit_is_answered_before_the_write(night, monkeypatch):
    n = night
    tick(n, copies=2)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    h, box = officer_h(n), {}
    seen = _spy_answered(n, monkeypatch, "override", box)
    target = next(char.main.label for char in n.members if char.main.label != n.s.pending("d1").to)
    run(_submit(h, flows_loot.reason_modal(GUILD, n.s.id, "d1", "ovr", item="Shard of Doom", who=target), box, message_id=4321, who=target, reason="tank first"))
    assert seen == [True], "Discord's 3 seconds: the submit is acknowledged before the git commit, the ops line and the card edits"
    assert n.s.pending("d1").override.to == target and "not recorded until Confirm" in h.text, "the outcome still edits the wizard message"


def test_a_give_the_reason_submit_is_answered_before_the_write(night, monkeypatch):
    n = night
    companion_mismatch(n)
    h, box = officer_h(n), {}
    seen = _spy_answered(n, monkeypatch, "set_reason", box)
    run(_submit(h, flows_loot.reason_modal(GUILD, n.s.id, "d1", "why", item="Shard of Doom"), box, reason="raid leader's call"))
    assert seen == [True] and "📌 Precedent" in h.text


def test_keep_open_and_the_tick_wizard_answer_before_the_write(night, monkeypatch):
    n = night
    run(n.bot.loot_closing(n.reg, n.s))
    h, box = officer_h(n), {}
    seen = _spy_answered(n, monkeypatch, "keep_open", box)
    box["itx"] = h.card_press()
    run(LootButton(GUILD, n.s.id, "-", "keep").callback(box["itx"]))
    assert seen == [True] and n.s.keep_open_until and "Kept open until" in h.text
    seen = _spy_answered(n, monkeypatch, "tick", box)

    async def go():
        await button(card(n), "Tick drops…").callback(h.card_press())
        await h.pick("Which boss?", flows_loot.NEW_BOSS)
        await h.submit(boss="Gatekeeper")
        await h.press("Not on the table…")
        await h.submit(name="Shard of Doom", copies="1")
        btn = next(b for b in h._items() if isinstance(b, discord.ui.Button) and b.label == "Tick")
        box["itx"] = h._on_screen()
        await btn.callback(box["itx"])
    run(go())
    assert seen == [True] and [d.item_name for d in n.s.drop_list] == ["Shard of Doom"] and "📥 Ticked" in h.text


def test_an_off_roster_name_may_be_first_and_last(night):
    """WoW: Forever characters are 'First Last': Someone not on the roster… takes both words (the box allows 49)."""
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    h = officer_h(n)
    modal = flows_loot.reason_modal(GUILD, n.s.id, "d1", "ovr", item="Shard of Doom", who_text=True)
    assert modal._items["who"].max_length >= 49
    run(h.submit(modal, who="Kestrel Pugwood", reason="a guest helped us"))
    assert "isn't a character name" not in h.text and "Nobody called **Kestrel Pugwood**" in h.text
    run(button(h.screen["view"], "Record Kestrel Pugwood as a guest").callback(h.card_press()))
    assert n.s.pending("d1").override.to == "Kestrel Pugwood"
    run(h.submit(flows_loot.reason_modal(GUILD, n.s.id, "d1", "ovr", item="Shard of Doom", who_text=True), who="Bad Name 9", reason="x y z"))
    assert "isn't a character name" in h.text


def test_a_rehearsals_late_award_never_dms_the_officers(reg, rs, monkeypatch):
    world = World()
    start = (reg.now_local() + timedelta(days=3, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t", cutoffs={"soft": 2, "hard": 1.5, "confirm": 1})
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    reg.config.roster_channel_id = 4242
    bot = SurfBot(reg, rs, world)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=OFFICER))
    assert s.test
    run(bot.end_run(reg, rs, ev, by="Mira"))
    monkeypatch.setattr(type(bot), "loot_officer_ids", lambda self, reg: [OFFICER, 555])
    held(bot, reg, kind="loot")
    run(bot.loot_scheduler_step(reg))
    assert not bot.dms, "test traffic: never a DM to every officer (the DM rule)"


def test_the_unrouted_companion_block_names_no_companion_option_that_doesnt_exist(reg, rs):
    from oibot_gm.feed import Companion

    world = World()
    ev_a, _ = real_run(reg, rs, days=2)
    ev_b, _ = real_run(reg, rs, days=5)
    reg.config.roster_channel_id = 4242
    bot = SurfBot(reg, rs, world)
    s_a, _ = run(bot.open_loot(reg, rs, ev_a, by="Officer", by_id=OFFICER))
    run(bot.open_loot(reg, rs, ev_b, by="Officer", by_id=OFFICER))
    ack = run(bot.handle_feed_event({"type": "drop", "items": [{"name": "X"}], "idem": "u1"}, Companion("Lootmaster", "test", GUILD, "peer")))
    body = text_of(world.channels[s_a.raid_thread_id].message(s_a.cards["loot"]).view)
    assert "Companion not routed" in body and "--run" not in body and "--run" not in ack["error"]


def test_retiring_a_companion_awards_reason_never_puts_it_back_on_the_needs_a_reason_list(night):
    n = night
    companion_mismatch(n)
    run(n.bot.set_reason(n.reg, n.s, "d1", "raid leader's call", by="Mira", by_id=OFFICER))
    iid = f"{n.s.id}-d1"
    run(n.bot.retire_precedent(n.reg, iid, "no longer policy", by="Jonny", by_id=OFFICER))
    assert not [p for p in n.bot.loot_precedents(n.reg) if p.get("import_id") == iid], "retired: no longer cited"
    assert not n.bot.loot_reasons_pending(n.reg), "a retired reason was still given"
    with pytest.raises(ls.LootChanged, match="already gave a reason"):
        run(n.bot.set_reason(n.reg, n.s, "d1", "sneaking it back", by="Jonny"))


def test_an_ended_council_gets_no_batch_cards_and_its_file_is_never_written_again(night):
    """End landing between a distribute's merge and its after-write hook: the hook posts nothing into the archived
    thread and saves no card ids (that would re-create events/<id>.json and load the council as live after a restart)."""
    n = night
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    run(n.bot.end_run(n.reg, n.rs, n.ev, by="Mira"))
    live = n.reg.store.root / GUILD / "events" / f"{n.s.id}.json"
    sent = len(n.thread.sent)
    run(n.bot.loot_notify(n.reg, n.s, "⚖️ 1 proposal(s)", kind="distribute"))
    run(n.bot._loot_cards_save(n.s, {"batch:x": 1}, message="late"))
    assert not live.exists() and len(n.thread.sent) == sent and (GUILD, n.s.id) not in ls.load_sessions(n.reg.store)
