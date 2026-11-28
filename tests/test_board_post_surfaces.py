"""Post board on Discord, plain text and MCP (GitHub issue 16; design 2026-11-27-post-board). The site's press
(POST /api/event/{key}/board, tests/test_board_post.py) and these three surfaces call the SAME engine:
BoardPostMixin.post_board / remove_board_post and consequences.board_post_box. Discord: /raid board opens the box —
its title, Discord's own channel select preset to where the post is or would go, Post · Back (Remove board post while a
post is up) and no sentences. Plain text: run_board_post / run_board_remove, always reviewed first, the review naming
the box's title and the channel. MCP: post_board(run, channel=None) and remove_board_post(run) through the site's
routes. A channel picked on a first post becomes the event's own Board posts channel; picked with a post up, the post
moves. Refusals are the engine's one sentence; nobody is DMed; the post and a removal write one log and ops line."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import discord
import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from conftest import OWNER
from test_board_post import ALT_BOARD, BOARD, BoardBot, BRoom, ids, one_raid
from test_raid_night import H, run
from test_setup import pick_widget
from wizard_harness import Harness

from oibot_gm import configops, discord_registry, perms
from oibot_gm import consequences as cq
from oibot_gm import mcp_server as ms
from oibot_gm import raidcycle as rc
from oibot_gm.board_post import BOARD_NONE, BOARD_OVER, BOARD_POST, BOARD_REMOVE, BOARD_UPDATE
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError
from oibot_gm.wizard import FLOWS

OFFICER = SimpleNamespace(id=OWNER, display_name="Kara", mention="@Kara")  # the engine checks a move's officer live


@pytest.fixture
def officer(monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda *a, **k: True)


def posted_lines(bot, what="posted the board") -> list[str]:
    return [x[-1] for x in bot.ops.lines if what in x[-1]]


# ---------------------------------------------------------------- Discord: /raid board

def _open(reg, rs, bot):
    from oibot_gm import flows_raid  # noqa: F401 — registers the flow

    h = Harness(reg, rs, user=OFFICER, client=bot)
    run(FLOWS["raid_board"](h.command(), reg))
    return h


def _select(h) -> discord.ui.ChannelSelect:
    (sel,) = [s for s in h._items() if isinstance(s, discord.ui.ChannelSelect)]
    return sel


def test_raid_board_opens_the_box_on_the_resolved_channel_and_posts_through_the_engine(reg, rs, officer):
    bot = BoardBot(reg, rs)
    ev, ms_ = one_raid(reg, rs, bot)
    h = _open(reg, rs, bot)
    b = cq.board_post_box(reg, rs, ev, [], bot.board_where(reg, ev))
    assert h.text.startswith(f"**{BOARD_POST}**") and h.buttons() == ["Post", "Back"], (h.text, h.buttons())
    assert not any(s in h.text for s in b["sentences"]) and b["sentences"] == [], "no sentences: the owner's exception"
    sel = _select(h)
    assert sel.placeholder == cq.BOARD_POST_TO and [d.id for d in sel.default_values] == [BOARD], "preset to Board posts"
    assert sel.channel_types == [discord.ChannelType.text]
    assert not bot.rooms[BOARD].sent, "nothing before the press"
    run(h.press("Post"))
    ev = rs.events[ev.key]
    (mid,), = ids(rs, ev).values()
    assert mid in bot.rooms[BOARD].msgs and ev.board_channel_id == BOARD and len(bot.rooms[BOARD].sent) == 1
    assert "posted in #board" in h.text
    assert len(posted_lines(bot)) == 1 and sum("board posted in" in x for x in ev.log) == 1
    assert not bot.dms


def test_a_channel_picked_on_a_first_post_becomes_the_events_own(reg, rs, officer):
    bot = BoardBot(reg, rs)
    ev, ms_ = one_raid(reg, rs, bot)
    h = _open(reg, rs, bot)
    run(pick_widget(h, discord.ui.ChannelSelect, cq.BOARD_POST_TO, bot.rooms[ALT_BOARD]))
    assert not bot.rooms[ALT_BOARD].sent and not bot.rooms[BOARD].sent, "a pick writes nothing"
    assert [d.id for d in _select(h).default_values] == [ALT_BOARD] and h.buttons() == ["Post", "Back"]
    run(h.press("Post"))
    ev = rs.events[ev.key]
    assert ev.board_channel_id == ALT_BOARD and rc.event_channel(reg, ev, "board_channel") == ALT_BOARD
    assert not bot.rooms[BOARD].sent and len(posted_lines(bot)) == 1


def test_with_a_post_up_it_reads_update_moves_on_a_pick_and_removes(reg, rs, officer):
    bot = BoardBot(reg, rs)
    ev, ms_ = one_raid(reg, rs, bot)
    run(bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()
    h = _open(reg, rs, bot)
    assert h.text.startswith(f"**{BOARD_UPDATE}**") and h.buttons() == ["Post", BOARD_REMOVE, "Back"], h.buttons()
    run(pick_widget(h, discord.ui.ChannelSelect, cq.BOARD_POST_TO, bot.rooms[ALT_BOARD]))
    run(h.press("Post"))
    ev = rs.events[ev.key]
    (new,), = ids(rs, ev).values()
    assert ev.board_channel_id == ALT_BOARD and new in bot.rooms[ALT_BOARD].msgs and old in bot.rooms[BOARD].deleted, h.text
    h = _open(reg, rs, bot)
    run(h.press(BOARD_REMOVE))
    ev = rs.events[ev.key]
    assert ev.board_channel_id is None and not ev.board_posts and new in bot.rooms[ALT_BOARD].deleted
    assert "removed from #alt-board" in h.text and len(posted_lines(bot, "removed the board post")) == 1
    assert not bot.dms


def test_a_box_changed_while_open_is_shown_again_and_nothing_is_posted(reg, rs, officer):
    bot = BoardBot(reg, rs)
    ev, ms_ = one_raid(reg, rs, bot)
    h = _open(reg, rs, bot)
    run(bot.post_board(reg, rs, ev, by="Other"))  # another officer posts while this screen is open
    n = len(bot.rooms[BOARD].sent)
    run(h.press("Post"))
    assert len(bot.rooms[BOARD].sent) == n and BOARD_UPDATE in h.text and "Post" in h.buttons()


def test_an_ended_event_is_the_engines_one_sentence_back_alone(reg, rs, officer):
    from oibot_gm.raid_buttons import board_post_decide

    bot = BoardBot(reg, rs)
    ev, ms_ = one_raid(reg, rs, bot)
    run(bot.cancel_run(reg, rs, ev, by="Jon", reason=None, notify=False))
    h = Harness(reg, rs, user=OFFICER, client=bot)
    run(board_post_decide(h.card_press(), bot, reg, rs, rs.events[ev.key]))
    assert h.buttons() == ["Back"] and BOARD_OVER in h.text and not bot.rooms[BOARD].sent
    h = _open(reg, rs, bot)
    assert "No event is live" in h.text, h.text


def test_raid_board_is_for_officers(reg, rs, monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda *a, **k: False)
    bot = BoardBot(reg, rs)
    ev, ms_ = one_raid(reg, rs, bot)
    h = _open(reg, rs, bot)
    assert h.text == "Officers only." and not bot.rooms[BOARD].sent


def test_raid_board_is_a_raid_subcommand_with_no_typed_argument():
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "oibot_gm" / "raid_commands.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "raid_board")
    assert [a.arg for a in fn.args.args] == ["interaction"], "nothing typed: the channel is picked inside"
    assert '@raid.command(name="board"' in src


# ---------------------------------------------------------------- plain text

@pytest.fixture
def named(monkeypatch):
    """The running client names a channel ('#board'); without one a review prints Discord's mention."""
    def use(bot):
        monkeypatch.setattr(perms, "CLIENT", bot)
    return use


def test_plain_text_post_is_reviewed_names_the_box_and_the_channel_and_posts(reg, rs, named):
    bot = BoardBot(reg, rs)
    named(bot)
    ev, ms_ = one_raid(reg, rs, bot)
    op = ConfigOp(op="run_board_post", target=ev.key)
    assert configops.is_warn(reg, op), "always reviewed first"
    d = configops.describe(reg, op, strict=True)
    assert d.startswith(f"{BOARD_POST} for ") and d.endswith(" in #board."), d
    n = len(bot.ops.lines)
    said = run(configops.apply_async(reg, op, "Kara", False, bot=bot, via="chat"))
    assert "posted in #board" in said and rs.events[ev.key].board_channel_id == BOARD
    assert len(posted_lines(bot)) == 1 and len(bot.ops.lines) == n + 1, "the engine's one ops line, no second"
    up = configops.describe(reg, op, strict=True)
    assert up.startswith(f"{BOARD_UPDATE} for ") and up.endswith(" in #board."), up
    assert not bot.dms


def test_plain_text_names_a_channel_first_post_owns_it_then_moves(reg, rs, named):
    bot = BoardBot(reg, rs)
    named(bot)
    ev, ms_ = one_raid(reg, rs, bot)
    op = ConfigOp(op="run_board_post", target=ev.key, value=f"<#{ALT_BOARD}>")
    assert configops.describe(reg, op, strict=True).endswith(" in #alt-board.")
    run(configops.apply_async(reg, op, "Kara", False, bot=bot, via="chat"))
    ev = rs.events[ev.key]
    assert ev.board_channel_id == ALT_BOARD and rc.event_channel(reg, ev, "board_channel") == ALT_BOARD
    (old,), = ids(rs, ev).values()
    run(configops.apply_async(reg, ConfigOp(op="run_board_post", target=ev.key, value=f"<#{BOARD}>"), "Kara", False, bot=bot, via="chat"))
    ev = rs.events[ev.key]
    assert ev.board_channel_id == BOARD and old in bot.rooms[ALT_BOARD].deleted


def test_plain_text_remove_and_its_refusals(reg, rs, named):
    bot = BoardBot(reg, rs)
    named(bot)
    ev, ms_ = one_raid(reg, rs, bot)
    rm = ConfigOp(op="run_board_remove", target=ev.key)
    assert configops.is_warn(reg, rm)
    with pytest.raises(RegistryError) as e:
        configops.describe(reg, rm, strict=True)
    assert str(e.value) == BOARD_NONE
    run(bot.post_board(reg, rs, ev, by="Kara"))
    d = configops.describe(reg, rm, strict=True)
    assert d.startswith(f"{BOARD_REMOVE} for ") and d.endswith(" from #board."), d
    said = run(configops.apply_async(reg, rm, "Kara", False, bot=bot, via="chat"))
    assert "removed from #board" in said and not rs.events[ev.key].board_posts
    assert len(posted_lines(bot, "removed the board post")) == 1
    run(bot.cancel_run(reg, rs, rs.events[ev.key], by="Jon", reason=None, notify=False))
    with pytest.raises(RegistryError) as e:
        configops.describe(reg, ConfigOp(op="run_board_post", target=ev.key), strict=True)
    assert str(e.value) == BOARD_OVER
    assert "Post board" in configops.describe(reg, ConfigOp(op="run_board_post", target=ev.key)), "listed as refused, by its name"


def test_a_reviewed_post_is_refused_when_the_box_changed_before_apply(reg, rs, named):
    bot = BoardBot(reg, rs)
    named(bot)
    ev, ms_ = one_raid(reg, rs, bot)
    op = ConfigOp(op="run_board_post", target=ev.key)
    revs = configops.board_revs(reg, [op], rs)
    key = configops.op_run_key(reg, op, rs)
    seen = configops.op_rev(op, key, revs)
    assert seen == cq.board_post_box(reg, rs, ev, [], bot.board_where(reg, ev))["fp"]
    run(bot.post_board(reg, rs, ev, by="Other"))
    n = len(bot.rooms[BOARD].sent)
    with pytest.raises(RegistryError, match="changed this event"):
        run(configops.apply_async(reg, op, "Kara", False, bot=bot, rev=seen))
    assert len(bot.rooms[BOARD].sent) == n


def test_the_parser_is_told_both_ops():
    text = configops.SCHEMA_TEXT
    post = text[text.index("  run_board_post:"):].split("\n  run_")[0]
    assert '"post the board for' in post and "#channel" in post and "run_board_remove" in text
    desc = ConfigOp.model_fields["op"].description
    assert "run_board_post" in desc and "run_board_remove" in desc
    for op in ("run_board_post", "run_board_remove"):
        assert configops.OP_GROUP[op] == "runs" and op in configops.RUN_OPS and op in configops.RUN_VERBS_AUDITED


# ---------------------------------------------------------------- MCP, through the site's routes

@pytest.fixture
def site_mcp(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = BoardBot(reg, rs)
    bot.get_guild = lambda gid: SimpleNamespace(unavailable=False)
    bot.guild_channels = lambda reg_: [{"id": str(c), "name": r.name, "category": None} for c, r in bot.rooms.items() if isinstance(r, BRoom)]
    c = TestClient(web.create_app(bot))

    def handler(request: httpx.Request) -> httpx.Response:
        r = c.request(request.method, request.url.path, content=request.content, headers={**H(), "Content-Type": "application/json"})
        return httpx.Response(r.status_code, json=r.json())
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    yield bot
    ms.use(None)


def test_mcp_tools_take_the_run_and_a_channel(reg):
    assert list(inspect.signature(ms.post_board).parameters) == ["run", "channel", "guild"]
    assert list(inspect.signature(ms.remove_board_post).parameters) == ["run", "guild"]
    for fn in (ms.post_board, ms.remove_board_post):
        assert fn.__doc__ and "DM" in fn.__doc__


def test_mcp_posts_moves_and_removes_through_the_site(reg, rs, site_mcp):
    bot = site_mcp
    ev, ms_ = one_raid(reg, rs, bot)
    assert "posted in #board" in ms.post_board(ev.key)
    (old,), = ids(rs, ev).values()
    assert "moved" in ms.post_board(ev.key, channel="#alt-board")
    ev = rs.events[ev.key]
    assert ev.board_channel_id == ALT_BOARD and old in bot.rooms[BOARD].deleted
    assert "removed from #alt-board" in ms.remove_board_post(ev.key)
    with pytest.raises(ToolError, match=BOARD_NONE):
        ms.remove_board_post(ev.key)
    with pytest.raises(ToolError, match="no channel named #nowhere"):
        ms.post_board(ev.key, channel="nowhere")
    assert len(posted_lines(bot)) == 1 and not bot.dms


def test_mcp_post_on_an_ended_event_is_the_engines_sentence(reg, rs, site_mcp):
    bot = site_mcp
    ev, ms_ = one_raid(reg, rs, bot)
    run(bot.cancel_run(reg, rs, ev, by="Jon", reason=None, notify=False))
    with pytest.raises(ToolError, match=BOARD_OVER):
        ms.post_board(ev.key)
