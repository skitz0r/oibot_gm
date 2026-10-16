"""Parity B2, batch 6: the run-board ops over plain text. run_move / run_board / run_autofill write through RaidMixin's
board verbs (board_move / save_board); Apply carries the board fingerprint the diff showed, so a board someone changed
since is refused while the request's own earlier ops are not; run_propose previews in a worker thread and remembers
what it showed; a no-thanks with a reason is a callout; confirm_for answers a fill ask; run_sheet re-posts where it
was asked; the parser sees the board and each raid's unopened runs. The bot is the real RaidMixin over a recording
fake of Discord (test_parity_verbs.Bot); no LLM, no solver (the solver calls are stubbed)."""
from __future__ import annotations

import asyncio
import types

import pytest
from conftest import OWNER, join, lock_with_board, open_test_run
from test_parity_verbs import Bot, Chan, fill_ask

from oibot_gm import configops, discord_policy
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.registry import PLAIN_WEB, RegistryError

CH = 900_010


def run(coro):
    return asyncio.run(coro)


def apply(reg, bot, op, **kw):
    return run(configops.apply_async(reg, op, "Officer", True, bot=bot, by_id=OWNER, **kw))


def board_run(reg, rs, n=6):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:n]
    join(reg, rs, ev, ms)
    return ev, [m.display_name for m in ms]


# ---------------------------------------------------------------- run_move

def test_run_move_describes_and_moves_through_board_move(reg, rs):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    run(bot.save_board(reg, rs, ev, [names[:2], names[2:4]], by="t"))
    d = configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=names[4], value="g2"))
    assert f"move {names[4]}: the bench → group 2" in d and "the layout the lock will use" in d
    out = apply(reg, bot, ConfigOp(op="run_move", target=ev.key, member=names[4], value="g2"))
    assert out.startswith(ev.key) and ev.layout[1] == names[2:4] + [names[4]]
    apply(reg, bot, ConfigOp(op="run_move", target=ev.key, member=names[4], value="1.1"))  # roster.group
    assert names[4] in ev.layout[0]
    apply(reg, bot, ConfigOp(op="run_move", target=ev.key, member=names[4], value="bench"))
    assert names[4] not in [n for g in ev.layout for n in g]
    swap = configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=names[0], value=names[2]))
    assert f"swap {names[0]} (group 1) with {names[2]}" in swap
    apply(reg, bot, ConfigOp(op="run_move", target=ev.key, member=names[0], value=names[2]))
    assert names[0] in ev.layout[1] and names[2] in ev.layout[0], "a swap: the displaced member takes the mover's place"
    for bad, why in ((ConfigOp(op="run_move", target=ev.key, member=names[5], value=names[4]), "isn't in a group"),
                     (ConfigOp(op="run_move", target=ev.key, member=names[0], value="Nobody"), "not “Nobody”"),
                     (ConfigOp(op="run_move", target=ev.key, member=names[0], value="g99"), "no group g99")):
        assert why in configops.describe(reg, bad)
        with pytest.raises(RegistryError, match=why.split("“")[0]):
            apply(reg, bot, bad)
    other = reg.test_members()[8]
    assert "hasn't joined this run" in configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=other.display_name, value="g1"))
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, ConfigOp(op="run_move", target=ev.key, member=names[0], value="g1"), "Officer", True, raids=rs)


def test_run_move_refuses_a_full_group_before_apply(reg, rs):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs, 7)
    run(bot.save_board(reg, rs, ev, [names[:5]], by="t"))
    assert "is full" in configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=names[6], value="g1"))


def test_run_move_after_lock_confirms_or_frees(reg, rs):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    lock_with_board(reg, rs, ev, names[:4])
    d = configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=names[5], value="g1"))
    assert "rostered and asked to confirm" in d
    out = apply(reg, bot, ConfigOp(op="run_move", target=ev.key, member=names[5], value="g1"))
    assert f"asked {names[5]} to confirm" in out and ev.seat_of(names[5])
    assert "seat freed" in configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=names[0], value="bench"))
    out = apply(reg, bot, ConfigOp(op="run_move", target=ev.key, member=names[0], value="bench"))
    assert f"freed {names[0]}" in out and not ev.seat_of(names[0])


def test_board_ops_refuse_a_board_changed_since_the_preview(reg, rs):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    seen = rc.board_rev(reg, ev)
    run(bot.board_move(reg, rs, ev, names[0], (0, 0), by="someone else"))
    for op in (ConfigOp(op="run_move", target=ev.key, member=names[1], value="g2"),
               ConfigOp(op="run_board", target=ev.key, value=", ".join(names[:3]))):
        with pytest.raises(RegistryError, match="changed since you looked"):
            apply(reg, bot, op, rev=seen)
    assert ev.layout[0] == [names[0]], "nothing was written"
    assert apply(reg, bot, ConfigOp(op="run_board", target=ev.key, value=", ".join(names[:3])), rev=rc.board_rev(reg, ev)).endswith("board saved (the lock uses it)")


def test_run_autofill_saves_through_save_board(reg, rs, monkeypatch):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    monkeypatch.setattr(rc, "autofill", lambda reg_, rs_, ev_: [names[:3], names[3:]])
    stale = rc.board_rev(reg, ev)
    run(bot.board_move(reg, rs, ev, names[0], (0, 0), by="someone else"))
    with pytest.raises(RegistryError, match="changed since you looked"):
        apply(reg, bot, ConfigOp(op="run_autofill", target=ev.key), rev=stale)
    assert not any("auto-filled" in line for line in ev.log)
    out = apply(reg, bot, ConfigOp(op="run_autofill", target=ev.key), rev=rc.board_rev(reg, ev))
    assert out == f"{ev.key}: board auto-filled (6 rostered in 2 groups)" and ev.layout == [names[:3], names[3:]]


# ---------------------------------------------------------------- Apply: the preview's fingerprint, updated by the request's own writes

class PressBot(Bot):
    async def plain_identity(self, reg, user, **_):
        return True, []


def press(bot, uid, log):
    async def edit(content=None, **kw):
        log.append(content)

    async def acknowledge(content=None, **kw):  # Apply's "Applying…" edit (not logged)
        pass

    async def followup(content=None, **kw):
        log.append(content)

    return types.SimpleNamespace(user=types.SimpleNamespace(id=uid, display_name="Officer"), client=bot,
                                 response=types.SimpleNamespace(edit_message=acknowledge), edit_original_response=edit, followup=types.SimpleNamespace(send=followup))


def test_confirm_view_passes_the_preview_rev_and_tracks_its_own_writes(reg, rs):
    bot = PressBot(reg, rs)
    ev, names = board_run(reg, rs)
    ops = [ConfigOp(op="run_move", target=ev.key, member=names[0], value="g1"), ConfigOp(op="run_move", target=ev.key, member=names[1], value="g2")]
    revs = configops.board_revs(reg, ops, rs)
    assert revs == {ev.key: rc.board_rev(reg, ev)}
    log = []
    view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=ops), None, bot.ops, OWNER, "Owner", CH, revs=revs)
    run(view.apply.callback(press(bot, OWNER, log)))
    assert "⛔" not in log[0], log[0]
    assert ev.layout[0] == [names[0]] and ev.layout[1] == [names[1]], "the second move is not refused by the first"
    # someone else moves between the preview and Apply: refused, nothing written
    revs = configops.board_revs(reg, [ops[0]], rs)
    run(bot.board_move(reg, rs, ev, names[2], (0, 1), by="someone else"))
    log.clear()
    view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="run_move", target=ev.key, member=names[3], value="g1")]),
                                            None, bot.ops, OWNER, "Owner", CH, revs=revs)
    run(view.apply.callback(press(bot, OWNER, log)))
    assert "changed since you looked" in log[0] and names[3] not in ev.layout[0]


# ---------------------------------------------------------------- run_propose

def fake_preview(names):
    calls = []

    def split_preview(reg, rs_, ev, strategy, avoid=None):
        calls.append((strategy, avoid))
        layout = [names[:3], names[3:]] if not avoid else [names[3:], names[:3]]
        return layout, rc.board_rosters(reg, ev, layout, rc.run_size(reg, ev))
    return split_preview, calls


def test_run_propose_previews_remembers_and_uses(reg, rs, monkeypatch):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    preview, calls = fake_preview(names)
    monkeypatch.setattr(rc, "split_preview", preview)
    assert "a balanced proposal from the solver — shown here, nothing saved" in configops.describe(reg, ConfigOp(op="run_propose", target=ev.key))
    with pytest.raises(RegistryError, match="no proposal to use yet"):
        apply(reg, bot, ConfigOp(op="run_propose", target=ev.key, field="use"))
    out = apply(reg, bot, ConfigOp(op="run_propose", target=ev.key, value="first"))
    assert out.startswith(f"{ev.key}: first proposal — g1 {', '.join(names[:3])} | g2 {', '.join(names[3:])} · synergy 0")
    assert "nothing is saved" in out and "remembered until the bot restarts" in out and ev.layout is None
    again = apply(reg, bot, ConfigOp(op="run_propose", target=ev.key, field="another"))
    assert calls[-1] == ("first", [[names[:3], names[3:]]]), "another keeps the strategy and avoids what was shown"
    assert "first proposal #2" in again
    out = apply(reg, bot, ConfigOp(op="run_propose", target=ev.key, field="use"))
    assert "first proposal used" in out and ev.layout == [names[3:], names[:3]] and ev.split_strategy == "first"
    apply(reg, bot, ConfigOp(op="run_propose", target=ev.key, field="another"))  # used: the memory starts over
    assert calls[-1][1] is None
    with pytest.raises(RegistryError, match="strategy is one of"):
        apply(reg, bot, ConfigOp(op="run_propose", target=ev.key, value="chaos"))
    lock_with_board(reg, rs, ev, names[:4])
    assert "proposals shape an open run" in configops.describe(reg, ConfigOp(op="run_propose", target=ev.key))


def test_run_propose_memory_is_per_requester(reg, rs, monkeypatch):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    monkeypatch.setattr(rc, "split_preview", fake_preview(names)[0])
    apply(reg, bot, ConfigOp(op="run_propose", target=ev.key))
    with pytest.raises(RegistryError, match="no proposal to use yet"):
        run(configops.apply_async(reg, ConfigOp(op="run_propose", target=ev.key, field="use"), "Other", True, bot=bot, by_id=OWNER + 1))


# ---------------------------------------------------------------- callouts, fill asks, the sheet

def test_no_thanks_with_a_reason_is_a_callout(reg, rs):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs, 3)
    m = reg.test_members()[1]
    op = ConfigOp(op="run_answer", target=ev.key, member=m.display_name, value="no thanks", reason="work trip")
    assert "a callout; note for the officers: “work trip”" in configops.describe(reg, op)
    out = apply(reg, bot, op)
    assert "noted for the officers" in out and "work trip" not in out
    assert ev.callouts[-1].note == "work trip" and ev.signups[str(m.discord_id)].status == "out"
    # the member's own no-thanks (bind_self pins them; by = their name) reads in their voice
    me = reg.test_members()[2]
    own = run(configops.apply_async(reg, ConfigOp(op="run_answer", target=ev.key, member=f"<@{me.discord_id}>", value="no thanks", reason="sick"), me.display_name, True, bot=bot, by_id=me.discord_id))
    assert own.startswith("Noted: out for") and ev.callouts[-1].note == "sick"


def test_confirm_for_answers_a_fill_ask(reg, rs):
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs, 5)
    ms = reg.test_members()
    lock_with_board(reg, rs, ev, names[:4])
    fill_ask(reg, ev, ms[5])
    rs.save(ev, "fill ask (test)")
    op = ConfigOp(op="confirm_for", target=ev.key, member=ms[5].display_name, value="yes")
    assert "answers their fill ask" in configops.describe(reg, op) and "takes the seat" in configops.describe(reg, op)
    apply(reg, bot, op)
    assert ev.seat_of(ms[5].display_name)
    assert "has no confirmation waiting" in configops.describe(reg, ConfigOp(op="confirm_for", target=ev.key, member=ms[7].display_name, value="yes"))


def test_run_sheet_posts_where_it_was_asked(reg, rs):
    bot = Bot(reg, rs)
    ev, _ = board_run(reg, rs, 2)
    bot.channels[CH] = Chan(CH, "raid-chat")
    op = ConfigOp(op="run_sheet", target=ev.key)
    assert "in this channel" in configops.describe(reg, op, CH) and "not DMs" in configops.describe(reg, op, None)
    assert "#raid-chat" in apply(reg, bot, op, channel_id=CH) and ev.channel_id == CH
    for where in (None, PLAIN_WEB):
        with pytest.raises(RegistryError, match="ask in a server channel"):
            apply(reg, bot, op, channel_id=where)


# ---------------------------------------------------------------- what the parser sees; run_open picks a listed run

def test_live_runs_text_has_the_board_and_unopened_runs(reg, rs):
    reg.set_raid_override("barrow_deeps", "first_open", "2026-01-01T00:00", "t")
    reg.set_raid_override("barrow_deeps", "slots", "Tue 19:30, Thu 19:30, Sat 19:30", "t")
    bot = Bot(reg, rs)
    ev, names = board_run(reg, rs)
    run(bot.save_board(reg, rs, ev, [names[:2]], by="t"))
    text = configops.live_runs_text(reg, rs)
    assert f"\n<data>\n{ev.key} board: g1 {names[0]}, {names[1]} · bench: {', '.join(names[2:])}\n</data>" in text, "names people chose are fenced as data (stage C L1)"
    assert "## Upcoming runs not opened yet" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("- barrow_deeps: "))
    assert line.count(" default") == 6 and " 19:30 default" in line


def test_open_time_matches_a_listed_run_to_its_schedule(reg):
    reg.set_raid_override("barrow_deeps", "first_open", "2026-01-01T00:00", "t")
    reg.set_schedule("barrow_deeps", "alt", {"slots": "Sat 20:00"}, "t")
    nxt = next(t for s, t in rc.upcoming(reg, "barrow_deeps", reg.now_local(), 24 * 21) if s["id"] == "alt")
    start, sid = configops._open_time(reg, "barrow_deeps", nxt.strftime("%Y-%m-%d %H:%M"))
    assert (start, sid) == (nxt, "alt"), "a listed time opens that schedule's run, not a one-off"
    start, sid = configops._open_time(reg, "barrow_deeps", "20:00")
    assert (start, sid) == (nxt, "alt")
    with pytest.raises(RegistryError, match="no upcoming run at 6:15 AM"):
        configops._open_time(reg, "barrow_deeps", "06:15")
    assert configops._open_time(reg, "barrow_deeps", "2030-01-08 19:30")[1] == "adhoc", "any other time is a one-off (ad hoc)"


def test_board_ops_are_mapped_and_documented():
    for op in ("run_move", "run_propose", "run_sheet"):
        assert configops.OP_GROUP[op] == "runs" and op in configops.RUN_OPS
        assert op in configops.ConfigOp.model_fields["op"].description and op + ":" in configops.SCHEMA_TEXT
    assert configops.BOARD_OPS <= configops.RUN_OPS
