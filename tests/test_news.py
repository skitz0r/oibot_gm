"""News review (design.md §5.26): the embed filter and dedupe, the proposal store and its state machine, the
news channel setting on every surface, the Discord card and its officer-gated buttons, and the on_message hook.
Offline: fake messages and interactions over the demo fixtures (conftest)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from oibot_gm import configops, news
from oibot_gm.news import NewsStore, ProposalError, ProposalStore

URL = "https://www.wowhead.com/news/barrow-deeps-lockout-changed-123"


def embed(title, description="", url=""):
    return SimpleNamespace(title=title, description=description, url=url)


def message(*embeds, content="", author_bot=True, webhook_id=99, channel_id=555, mid=1):
    return SimpleNamespace(embeds=list(embeds), content=content, author=SimpleNamespace(bot=author_bot, id=7), webhook_id=webhook_id,
                           created_at=datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc), id=mid, channel=SimpleNamespace(id=channel_id),
                           guild=SimpleNamespace(id=1))


def add_news(reg, url=URL, title="Barrow Deeps lockout is now 7 days on WoW: Forever realms", desc="The 10-player raid resets weekly."):
    return NewsStore(reg.store, reg.key).add({"url": url, "title": title, "description": desc}, ["WoW: Forever", "Barrow Deeps"], news.now())


def proposal_data(**kw):
    return {"title": "Barrow Deeps resets weekly", "news": [URL], "affects": "raid barrow_deeps lockout_days", "kind": "guild_setting",
            "change": "Barrow Deeps lockout 3 → 7 days", "edit": {"op": "raid_set", "target": "barrow_deeps", "field": "lockout_days", "value": "7"},
            "evidence": "The 10-player raid resets weekly.", "confidence": "high", **kw}


# ---- keywords and the filter

def test_keyword_sets_come_from_the_profile_and_the_guild(reg):
    reg.config.news_keywords = ["Season of Mastery"]
    sets = news.keyword_sets(reg)
    assert "WoW: Forever" in sets["version"] and "Barrow Deeps" in sets["raid"] and "Blood Pact" in sets["buff"]
    assert "Warlock" in sets["class"] and sets["extra"] == ["Season of Mastery"]


def test_match_needs_a_version_raid_or_extra_word(reg):
    sets = news.keyword_sets(reg)
    assert news.match(sets, "Retail patch 12.1: Warlock and Blood Pact changes") == (False, ["Blood Pact", "Warlock"])
    ok, hit = news.match(sets, "WoW Forever: Blood Pact now stacks with Fortitude")
    assert ok and hit[0] == "WoW Forever" and "Blood Pact" in hit
    assert news.match(sets, "Hotfixes for Barrow Deeps")[0]
    assert not news.match(sets, "Foreverwind mount revealed")[0], "word boundaries: Forever inside another word is not the version"


def test_embed_items_reads_only_the_embed_text():
    m = message(embed("Title A", "Desc A", "https://x/a"), embed("", "", "https://x/empty"))
    assert news.embed_items(m) == [{"title": "Title A", "description": "Desc A", "url": "https://x/a"}]
    plain = message(content="WoW: Forever hotfixes https://x/b\nmore")
    assert news.embed_items(plain)[0]["url"] == "https://x/b"


def test_ingest_keeps_relevant_items_and_dedupes_by_url(reg):
    m = message(embed("Barrow Deeps lockout is now 7 days", "WoW: Forever hotfix", URL), embed("Retail: new mount", "nothing for us", "https://x/r"))
    rows = news.ingest(reg, reg.store, m)
    assert [r["url"] for r in rows] == [URL] and rows[0]["matched"][:1] == ["WoW: Forever"]
    assert news.ingest(reg, reg.store, message(embed("Barrow Deeps lockout is now 7 days (updated)", "WoW: Forever", URL + "/"))) == [], "same url (trailing slash) = duplicate"
    items = NewsStore(reg.store, reg.key).all()
    assert len(items) == 1 and items[0]["posted_at"].startswith("2026-09-22T18:00")
    assert "news:" in reg.store._git("log", "-1", "--format=%s").stdout


def test_since_filters_by_when_the_bot_recorded_it(reg):
    add_news(reg)
    assert len(NewsStore(reg.store, reg.key).since("2000-01-01T00:00:00+00:00")) == 1
    assert NewsStore(reg.store, reg.key).since("2999-01-01T00:00:00Z") == []


# ---- proposals

def test_create_validates_news_and_edit(reg):
    ps = ProposalStore(reg.store, reg.key)
    with pytest.raises(ProposalError, match="not in the news list"):
        ps.create(reg, proposal_data())
    add_news(reg)
    with pytest.raises(ProposalError, match="guild setting is one of"):
        ps.create(reg, proposal_data(edit={"op": "run_lock", "target": "x"}))
    with pytest.raises(ProposalError, match="under profiles/forever"):
        ps.create(reg, proposal_data(kind="profile", edit={"file": "src/oibot_gm/registry.py", "key": "x", "value": 1}))
    with pytest.raises(ProposalError, match="under profiles/forever"):
        ps.create(reg, proposal_data(kind="profile", edit={"file": "profiles/tbc/raids.yaml", "key": "x", "value": 1}))
    p = ps.create(reg, proposal_data())
    assert p.state == "proposed" and p.history[0]["state"] == "proposed"
    q = ps.create(reg, proposal_data(kind="profile", edit={"file": "profiles/forever/raids.yaml", "key": "barrow_deeps.size", "value": 20}))
    assert ps.get(q.id).edit["value"] == 20 and {x.id for x in ps.all("proposed")} == {p.id, q.id}


def test_state_machine_and_who_may_move_it(reg):
    add_news(reg)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data())
    with pytest.raises(PermissionError):
        ps.move(p.id, "approved", "someone", officer=False, job=False)
    with pytest.raises(ProposalError, match="can't become applied"):
        ps.move(p.id, "applied", "job", officer=True, job=True)
    p = ps.move(p.id, "approved", "Officer", officer=True, job=False, by_id=5)
    assert (p.decided_by, p.decided_by_id) == ("Officer", 5) and p.decided_at
    with pytest.raises(PermissionError):
        ps.move(p.id, "applying", "Officer", officer=True, job=False)  # an officer doesn't report the job's steps
    p = ps.move(p.id, "applying", "mcp", officer=True, job=True)
    p = ps.move(p.id, "applied", "mcp", officer=True, job=True, commit="abc1234", note="done")
    assert p.state == "applied" and p.commit == "abc1234" and [h["state"] for h in p.history] == ["proposed", "approved", "applying", "applied"]
    p = ps.move(p.id, "reverted", "mcp", officer=True, job=True, note="bot unhealthy")
    with pytest.raises(ProposalError):
        ps.move(p.id, "approved", "Officer", officer=True, job=False)
    with pytest.raises(ProposalError, match="state is one of"):
        ps.move(p.id, "done", "x", officer=True, job=True)


def test_needs_developer_cannot_be_approved(reg):
    add_news(reg)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data(kind="needs_developer", edit={}))
    with pytest.raises(ProposalError, match="needs a developer"):
        ps.move(p.id, "approved", "Officer", officer=True, job=False)
    assert ps.move(p.id, "dismissed", "Officer", officer=True, job=False).state == "dismissed"


def test_bad_ids_are_refused(reg):
    with pytest.raises(ProposalError):
        ProposalStore(reg.store, reg.key).get("../guild")


# ---- the setting on every surface

def test_news_channel_and_keywords_via_plain_text(reg):
    out = configops.apply(reg, configops.ConfigOp(op="set", path="news_channel", value="<#4242>"), "Owner", True)
    assert reg.config.news_channel_id == 4242 and "news_channel" in out
    assert "news_channel" in configops.describe(reg, configops.ConfigOp(op="set", path="news_channel", value="<#1>"))
    configops.apply(reg, configops.ConfigOp(op="set", path="news_keywords", value="Season of Discovery, hardcore, hardcore"), "Owner", True)
    assert reg.config.news_keywords == ["Season of Discovery", "hardcore"]
    with pytest.raises(Exception):
        configops.apply(reg, configops.ConfigOp(op="set", path="news_channel", value="<#4242>"), "Officer", False)


def test_news_channel_kind_and_command_exist():
    from oibot_gm.discord_pool import CHANNEL_KINDS

    assert CHANNEL_KINDS["news"] == "news_channel_id"
    import inspect

    from oibot_gm import discord_registry

    assert 'name="news-channel"' in inspect.getsource(discord_registry.register_commands)


# ---- the card and its buttons

def run(coro):
    return asyncio.run(coro)


def test_card_renders_state_sources_and_buttons(reg):
    from oibot_gm.discord_news import proposal_embed, proposal_view

    add_news(reg)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data())
    rows = {NewsStore.ident(r): r for r in NewsStore(reg.store, reg.key).all()}

    async def go():
        e = proposal_embed(p, rows)
        fields = {f.name: f.value for f in e.fields}
        assert p.title in e.title and "Barrow Deeps lockout is now 7 days" in fields["News"] and fields["Confidence"] == "high"
        assert "Waiting for an officer" in fields["Status"]
        v = proposal_view(p)
        assert [(b.item.label, b.item.disabled) for b in v.children] == [("Approve", False), ("Dismiss", False)]
        q = ps.move(p.id, "approved", "Kessa", officer=True, job=False)
        q = ps.move(q.id, "applying", "mcp", officer=True, job=True)
        q = ps.move(q.id, "applied", "mcp", officer=True, job=True, commit="abc1234def")
        e2 = proposal_embed(q, rows)
        status = next(f.value for f in e2.fields if f.name == "Status")
        assert "Applied" in status and "Kessa" in status and "abc1234def" in status
        assert all(b.item.disabled for b in proposal_view(q).children)
        dev = ps.create(reg, proposal_data(kind="needs_developer", edit={}))
        labels = [(b.item.label, b.item.disabled) for b in proposal_view(dev).children]
        assert labels == [("Needs a developer", True), ("Dismiss", False)]
        assert all(len(b.item.custom_id) <= 100 for b in v.children)
        assert "<t:" in next(f.value for f in proposal_embed(ps.move(dev.id, "dismissed", "Kessa", officer=True, job=False)).fields if f.name == "Status"), "a Discord timestamp, never a typed clock"

    run(go())


class FakeResp:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, content=None, **kw):
        self.sent.append(content)

    async def edit_message(self, **kw):
        self.edited.append(kw)


def fake_client(reg, officer: bool):
    lines = []

    async def is_officer(_reg, uid):
        return officer

    async def emit(cfg, level, text, exc=None):
        lines.append(text)

    client = SimpleNamespace(registries=SimpleNamespace(for_interaction=lambda i: reg, store=reg.store), is_officer_anywhere=is_officer,
                             news_rows=lambda r: {}, ops=SimpleNamespace(emit=emit))
    return client, lines


def test_buttons_are_officer_only_and_flip_the_state(reg):
    from oibot_gm.discord_news import ProposalButton

    add_news(reg)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data())

    async def go():
        client, lines = fake_client(reg, officer=False)
        itx = SimpleNamespace(client=client, user=SimpleNamespace(id=9, display_name="Rando"), response=FakeResp())
        await ProposalButton(p.id, "approve").callback(itx)
        assert itx.response.sent == ["Officers only."] and ps.get(p.id).state == "proposed"
        client, lines = fake_client(reg, officer=True)
        itx = SimpleNamespace(client=client, user=SimpleNamespace(id=5, display_name="Kessa"), response=FakeResp())
        await ProposalButton(p.id, "approve").callback(itx)
        q = ps.get(p.id)
        assert q.state == "approved" and q.decided_by == "Kessa" and q.decided_by_id == 5
        assert itx.response.edited and "embed" in itx.response.edited[0] and any("approved by Kessa" in x for x in lines)
        itx2 = SimpleNamespace(client=client, user=SimpleNamespace(id=5, display_name="Kessa"), response=FakeResp())
        await ProposalButton(p.id, "approve").callback(itx2)  # a second press: refused, nothing changes
        assert itx2.response.sent and "can't become approved" in itx2.response.sent[0]

    run(go())


def test_dynamic_item_template_round_trips():
    import re

    from oibot_gm.discord_news import ProposalButton

    m = re.fullmatch(ProposalButton.__discord_ui_compiled_template__, "newsprop:260922-ab12:dismiss")
    assert m and m["pid"] == "260922-ab12" and m["action"] == "dismiss"


# ---- the on_message hook

def test_news_message_hook_consumes_webhook_posts_only_in_the_news_channel(reg):
    from oibot_gm.discord_news import NewsMixin

    reg.config.news_channel_id = 555
    lines = []

    async def emit(cfg, level, text, exc=None):
        lines.append(text)

    bot = NewsMixin()
    bot.registries = SimpleNamespace(by_discord={1: reg}, store=reg.store)
    bot.user = SimpleNamespace(id=1234)
    bot.ops = SimpleNamespace(emit=emit)

    async def go():
        assert await bot.news_message(message(embed("WoW: Forever hotfixes", "Barrow Deeps", URL))) is True
        assert any("kept 1 item" in x for x in lines)
        other = message(embed("WoW: Forever", "", "https://x/other"), channel_id=777)
        assert await bot.news_message(other) is False, "another channel: normal routing"
        person = message(embed("WoW: Forever news", "", "https://x/p"), author_bot=False, webhook_id=None)
        assert await bot.news_message(person) is False, "a person's post is read but still routed"
        assert len(NewsStore(reg.store, reg.key).all()) == 2
        dm = SimpleNamespace(guild=None)
        assert await bot.news_message(dm) is False

    run(go())


def test_on_message_calls_the_hook_first():
    import inspect

    from oibot_gm.discord_bot import OibotGM

    src = inspect.getsource(OibotGM.on_message)
    assert src.index("self.news_message(message)") < src.index("message.author.bot")
