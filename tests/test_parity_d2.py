"""Stage C D2 fixes (out/parity/stageC_report.md L1–L10, the open gaps and the three split findings): the help snapshot
reads what the site shows (open boards' auras, every proposal state, past runs, the change log, desired comp), members
never see advisories or who decided their application, the parser's context is fenced and 12-hour, run_sheet and
confirm "all pending" work by chat, every self "no thanks" is a callout, an absence previews the sheets it touches,
one test-run opener, and the small Discord fixes. No Discord, no LLM."""
from __future__ import annotations

import asyncio
import re
import types
from datetime import timedelta

import pytest
from conftest import OWNER, join, lock_with_board, open_test_run

from oibot_gm import configops, help as help_mod, raidcycle as rc
from oibot_gm.configops import ConfigOp


def state(reg, rs, uid, officer, q="", extras=None):
    return help_mod.guild_state(reg, rs, uid, officer, q, extras)


def real_member(reg, uid, name, cls="Priest", spec="Holy"):
    m, _c = reg.add_character(uid, name, name, cls, spec, None, True)
    return m


# ---------------------------------------------------------------- help: the snapshot reads what the site shows

def test_why_did_we_override_brings_the_precedents(reg, rs):
    reg.store.append_jsonl(f"{reg.key}/precedents.jsonl", {"date": "2026-09-01", "item": "Thunderfury", "bot": "A", "human": "B", "reason": "council call", "by": "Boss"})
    s = state(reg, rs, OWNER, True, "why did we override the thunderfury?")
    assert "### Loot" in s and "council call" in s


def test_a_past_run_named_in_words_gets_its_names(reg, rs):
    ms = reg.test_members()
    old = open_test_run(reg, rs, days=-6)
    join(reg, rs, old, ms[:3])
    old.state = "done"
    rs.save(old, "done")
    day = old.start.astimezone(reg.tz).strftime("%A")
    s = state(reg, rs, OWNER, True, f"who came last {day}?")
    assert f"closed run {old.key}" in s and ms[0].display_name in s.split(f"closed run {old.key}")[1]
    assert f"closed run {old.key}" not in state(reg, rs, OWNER, True, "who came to the onyxia raid last time?"), "another raid named: not this run"
    assert f"closed run {old.key}" in state(reg, rs, OWNER, True, "who came to the last barrow deeps?")


def test_status_carries_the_registry_change_log(reg, rs):
    real_member(reg, 700_000_000_000_000_101, "Changeling")
    s = state(reg, rs, OWNER, True, "what changed recently?")
    assert "recent registry changes (the analytics change log, newest first):" in s
    assert '<member-text source="changes">' in s and "Changeling" in s.split('<member-text source="changes">')[1].split("</member-text>")[0]


def test_open_board_shows_auras_with_provider_counts(reg, rs):
    ms = reg.test_members()
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, ms[:10])
    ev.layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:10]]]
    rs.save(ev, "board")
    s = state(reg, rs, OWNER, True, "which group is missing windfury?")
    line = next(l for l in s.splitlines() if l.startswith("  roster 1 auras: raid-wide "))
    assert "g1 missing " in line and "g2 missing " in line
    assert re.search(r"×\d", line), "raid-wide buffs carry their provider counts"


def test_double_booked_shows_on_a_locked_run_too(reg, rs):
    ms = reg.test_members()
    a = open_test_run(reg, rs, days=3)
    b = open_test_run(reg, rs, days=3.05, hard=1.4)
    join(reg, rs, a, ms[:10])
    join(reg, rs, b, ms[:2])
    lock_with_board(reg, rs, a, [m.display_name for m in ms[:10]])
    lines = help_mod.run_lines(reg, rs, a, True)
    assert any(l.startswith("  double-booked: ") and ms[0].display_name in l for l in lines)


def test_advisories_are_officer_only(reg, rs):
    ms = reg.test_members()
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, ms[:10])
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:10]])
    ev.rosters[0].advisories = ["Bench instead · Carol +4, Dan -12"]
    rs.save(ev, "advice")
    assert "Bench instead" in "\n".join(help_mod.run_lines(reg, rs, ev, True))
    assert "Bench instead" not in "\n".join(help_mod.run_lines(reg, rs, ev, False))
    assert "Bench instead" not in state(reg, rs, ms[0].discord_id, False, "why am I benched tonight?")


def test_readiness_gives_the_desired_comp_and_the_over_cap_reason(reg, rs):
    reg.config.raids["barrow_deeps"] = {"comp_targets": {"healer": {"min": 1, "max": 2, "note": "cap because Trueshot"}}}
    s = state(reg, rs, OWNER, True, "what is the desired comp and are we over cap?")
    over = next(l for l in s.splitlines() if l.startswith("  over cap: ") and "healer" in l)
    assert "cap because Trueshot" in over
    assert any(l.startswith("  desired comp: ") and "healer" in l and "(officer)" in l for l in s.splitlines())


def test_unconfirmed_list_survives_a_long_member_list(reg, rs):
    with reg.store.batch("many members"):
        for i in range(61):
            reg.add_character(700_000_000_000_000_200 + i, f"A rather long display name number {i:02d}", f"Longname{chr(97 + i % 26)}{chr(97 + i // 26)}", "Priest", "Holy", "Shadow", True)
    s = state(reg, rs, OWNER, True, "who is still unconfirmed?")
    assert "unconfirmed (" in s.split("### Members and characters")[1]


def test_news_lists_every_proposal_state_and_the_jobs_past_runs(reg, rs):
    from oibot_gm.news import Proposal, ProposalStore, new_id

    ps = ProposalStore(reg.store, reg.key)
    p = Proposal(id=new_id(), title="Blood Pact stacks", affects="guild setting", kind="guild_setting", change="x")
    p.state, p.commit = "applied", "abc1234"
    ps.save(p, "t")
    x = help_mod.HelpExtras(agents=lambda: {"jobs": [{"job": "review", "name": "News review", "state": "idle", "light": "green", "result": "ok",
                                                      "finished_label": "Mon 28 Sep 9:00 AM", "next_label": "Tue 29 Sep 9:00 AM", "schedule": "daily", "launchd": {}}]},
                            agent_runs=lambda job: [{"id": "review-1", "started_label": "Sun 27 Sep 9:00 AM", "outcome": "2 proposals"}, {"id": "review-0", "started_label": "Sat 26 Sep 9:00 AM", "outcome": "nothing new"}])
    s = state(reg, rs, OWNER, True, "did the Blood Pact change go through?", x)
    assert f"{p.id} “Blood Pact stacks” · guild_setting · confidence " in s and "· applied" in s and "commit abc1234" in s
    assert "last runs of review: Sun 27 Sep 9:00 AM 2 proposals ; Sat 26 Sep 9:00 AM nothing new" in s


def test_help_extras_pass_the_agents_past_runs(reg, rs):
    from oibot_gm import discord_help

    class Bot(discord_help.HelpMixin):
        def __init__(self):
            self.ops = types.SimpleNamespace(recent=[])
            self.ctx = types.SimpleNamespace(provider=None)
            self.feed = self.started_at = self.tree = None
            self.raids = types.SimpleNamespace(store=lambda r: rs)

        def get_guild(self, gid):
            return None

    x = Bot().help_extras(reg, True)
    assert callable(x.agent_runs)


def test_recruitment_questions_carry_their_hints(reg, rs):
    reg.set_recruitment("t", questions=[{"label": "Why us?", "hint": "a line or two", "required": True}])
    s = state(reg, rs, reg.test_members()[0].discord_id, False)
    assert "1. Why us?* [hint “a line or two”]" in s


def test_applications_header_names_plain_text_not_the_site(reg, rs):
    s = state(reg, rs, OWNER, True, "any applications?")
    head = next(l for l in s.splitlines() if l.startswith("### Applications"))
    assert "the site" not in head and "plain text ('accept X')" in head


def test_an_applicant_never_learns_who_decided(reg, rs):
    uid = 555_000_000_000_000_009
    reg.apply(uid, "Newbie", "Newname", "Mage", "Frost", None, None, None, None)
    reg.decide(uid, False, "Officer Grim", "not now")
    own = state(reg, rs, uid, False, "why was I declined?")
    assert "not now" in own and "Officer Grim" not in own
    assert "Officer Grim" in state(reg, rs, OWNER, True, "what about Newname's application?")
