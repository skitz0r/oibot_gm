"""The answer loop's officer-aware lookups (answer tools, design §7, §12): loot, attendance, manual and section, each a
capped read over an existing reader, and the loot log's rows in one function the site and the lookup share. The `loot`
lookup shows ANY member the Loot log's rows — day, item, character, boss, MS/OS — also where others read the reply
(§12, the owner's decision); scores, tiers, the bot's pick, reasons and precedents only for an officer in private.
No model, no network."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from answer_bench import LOOT_ITEM, LOOT_REASON, MARROW, PELLOW, WREN, an_award_with_reason, askers
from test_answer_tools import body, dentist, many_crafters, wrapper_len
from test_help import applications
from test_loot_web import OFFICER, as_user, env, w  # noqa: F401 — the web fixtures

from oibot_gm import answer_tools as at
from oibot_gm import help as help_mod
from oibot_gm import loot_ops
from oibot_gm.lootctx import RegistryLootContext

MEMBER_FIELDS = ["item", "item_id", "key", "character", "disenchant", "cls", "spec", "boss", "instance", "received", "date_label",
                 "offspec", "mine"]
SCORE_FIELDS = ["tier", "weight", "source", "bot", "by", "import_id", "candidates", "reason"]
OTHER_ITEM = "Cloak of Quiet Steps"


def viewer(reg, rs, uid, officer=False, public=False, question=""):
    return at.Viewer.of(reg, rs, uid, officer, public=public, question=question)


def inner(result) -> str:
    """The text inside the data block."""
    return body(result).split("\n", 1)[1].rsplit("\n", 1)[0]


def ctx_of(reg):
    return RegistryLootContext(reg, reg.store, None)


def awards(reg, rs):
    """Thornwick (Wrenfield's) got LOOT_ITEM over the bot's pick Briarwood, with the precedent's reason; Briarwood
    (Pellow's) got OTHER_ITEM, recorded by Marrowby."""
    an_award_with_reason(reg, rs)
    reg.store.append_jsonl(f"{reg.key}/ledger.jsonl", {
        "raider": "Briarwood", "discord_id": PELLOW, "item_id": 0, "item_name": OTHER_ITEM, "tier": "A", "total_weight": 2.5,
        "received": "2026-09-09", "instance": "bd", "boss": "Warden", "offspec": True, "import_id": "bench-d2",
        "source": "bot", "bot_pick": "Briarwood", "by": "Marrowby"})


# the four viewers of the visibility table: (uid, officer, public)
MEMBER_PRIVATE = (PELLOW, False, False)
MEMBER_PUBLIC = (PELLOW, False, True)
OFFICER_PRIVATE = (MARROW, True, False)
OFFICER_PUBLIC = (MARROW, True, True)
EVERY_VIEWER = (MEMBER_PRIVATE, MEMBER_PUBLIC, OFFICER_PRIVATE, OFFICER_PUBLIC)
OFFICER_WORDS = (LOOT_REASON, "tier", "weight", "bot pick", "bot picked", "precedent", "by Marrowby")


# ---------------------------------------------------------------- the loot log's rows, moved out of the route


def test_loot_log_rows_keep_the_routes_fields(reg, rs):
    awards(reg, rs)
    ctx = ctx_of(reg)
    rows = loot_ops.log_rows(reg, ctx)
    assert rows, "the demo ledger has awards"
    assert all(list(r) == MEMBER_FIELDS for r in rows)
    assert loot_ops.log_rows(reg, ctx, officer=True) == rows, "an officer without scores: the members' fields"
    assert loot_ops.log_rows(reg, ctx, scores=True) == rows, "scores without an officer: the members' fields"
    full = loot_ops.log_rows(reg, ctx, officer=True, scores=True)
    assert all(list(r) == MEMBER_FIELDS + SCORE_FIELDS for r in full)
    assert [r["received"] for r in rows] == sorted((r["received"] for r in rows), reverse=True), "newest first"
    mine = loot_ops.log_rows(reg, ctx, member_uid=PELLOW, viewer_uid=PELLOW)
    assert [r["item"] for r in mine] == [OTHER_ITEM] and mine[0]["mine"]
    assert [r["item"] for r in loot_ops.log_rows(reg, ctx, item="quiet STEPS")] == [OTHER_ITEM]
    assert len(loot_ops.log_rows(reg, ctx, limit=1)) == 1


def test_the_loot_log_route_answers_log_rows(w):
    ctx = ctx_of(w.reg)
    member = w.ms[0].discord_id
    got = w.c.get("/api/loot/log", headers=as_user(w.c, member)).json()
    assert got["rows"] == json.loads(json.dumps(loot_ops.log_rows(w.reg, ctx, limit=200, viewer_uid=member)))
    off = w.c.get("/api/loot/log", params={"scores": "true"}, headers=as_user(w.c, OFFICER)).json()
    assert off["rows"] == json.loads(json.dumps(loot_ops.log_rows(w.reg, ctx, limit=200, officer=True, scores=True, viewer_uid=OFFICER)))


# ---------------------------------------------------------------- loot: the visibility table


@pytest.mark.parametrize("who", EVERY_VIEWER, ids=["member", "member-public", "officer", "officer-public"])
def test_loot_tool_member_sees_log_rows_never_scores_or_reasons(reg, rs, who):
    """Someone else's awards (Thornwick's, asked by Pellow or Marrowby): the Loot log's row for every viewer; the
    officer-only fields only for the officer in private."""
    awards(reg, rs)
    uid, officer, public = who
    text = inner(at.run_tool(viewer(reg, rs, uid, officer, public), "loot", {"member": "Thornwick"}))
    assert "Wed 2 Sep · Robe of the Bench · Thornwick · Gatekeeper · MS" in text
    assert OTHER_ITEM not in text.split("### Loot")[0], "only the member asked about"
    if uid == MARROW and not public:
        return
    for word in OFFICER_WORDS:
        assert word not in text, word
    assert "Briarwood" not in text, "the bot's pick is the officers'"


@pytest.mark.parametrize("who", (MEMBER_PRIVATE, MEMBER_PUBLIC), ids=["private", "public"])
def test_loot_tool_without_filters_is_the_askers_own_awards(reg, rs, who):
    awards(reg, rs)
    _uid, officer, public = who
    text = inner(at.run_tool(viewer(reg, rs, WREN, officer, public), "loot", {}))
    assert text.split("\n") == ["The asker's own awards, newest first:", "Wed 2 Sep · Robe of the Bench · Thornwick · Gatekeeper · MS"]
    pellow = inner(at.run_tool(viewer(reg, rs, PELLOW, officer, public), "loot", {}))
    assert pellow.split("\n")[1:] == ["Wed 9 Sep · Cloak of Quiet Steps · Briarwood · Warden · OS"]
    downgraded = inner(at.run_tool(viewer(reg, rs, MARROW, True, True), "loot", {}))
    assert LOOT_ITEM not in downgraded and OTHER_ITEM not in downgraded, "an officer where members read: their own awards"


def test_loot_tool_officer_private_gets_precedents_and_reasons(reg, rs):
    awards(reg, rs)
    text = inner(at.run_tool(viewer(reg, rs, MARROW, True), "loot", {"member": "Thornwick"}))
    assert "Wed 2 Sep · Robe of the Bench · Thornwick · Gatekeeper · MS" in text
    assert LOOT_REASON in text and "### Loot (officers" in text, "help._loot's council view"
    own = inner(at.run_tool(viewer(reg, rs, MARROW, True), "loot", {}))
    assert own.startswith("Awards from the loot log, newest first:") and LOOT_REASON in own


def test_loot_tool_officer_public_gets_the_member_rows_only(reg, rs):
    awards(reg, rs)
    text = inner(at.run_tool(viewer(reg, rs, MARROW, True, True), "loot", {"item": "quiet steps"}))
    assert text.split("\n") == ["Awards from the loot log, newest first:", "Wed 9 Sep · Cloak of Quiet Steps · Briarwood · Warden · OS"]


def test_loot_tool_counts_what_it_leaves_out_and_says_when_nothing_matches(reg, rs):
    awards(reg, rs)
    for i in range(20):
        reg.store.append_jsonl(f"{reg.key}/ledger.jsonl", {"raider": "Thornwick", "discord_id": WREN, "item_id": 0,
                                                          "item_name": f"Ring of Rows {i}", "tier": "B", "total_weight": 1.0,
                                                          "received": "2026-09-03", "instance": "bd", "boss": "Warden",
                                                          "offspec": False, "import_id": f"rows-{i}"})
    text = inner(at.run_tool(viewer(reg, rs, PELLOW), "loot", {"item": "ring of rows"}))
    assert len(text.split("\n")) == 1 + at.LOOT_ROWS + 1 and text.endswith("And 5 more awards match.")
    assert inner(at.run_tool(viewer(reg, rs, PELLOW), "loot", {"item": "nothing like it"})) == "No award in the loot log matches that."
    assert inner(at.run_tool(viewer(reg, rs, PELLOW), "loot", {"member": "Nobodyhere"})) == at.NO_MATCH.format(name="Nobodyhere")


# ---------------------------------------------------------------- attendance


def test_attendance_tool_own_and_officers_only_for_others(reg, rs):
    askers(reg)
    own = inner(at.run_tool(viewer(reg, rs, WREN), "attendance", {}))
    assert own == f"Wrenfield's attendance: {reg.attendance_text(reg.members[WREN])}."
    assert inner(at.run_tool(viewer(reg, rs, WREN, public=True), "attendance", {"member": "Thornwick"})) == own
    for v in (viewer(reg, rs, WREN), viewer(reg, rs, WREN, public=True), viewer(reg, rs, MARROW, True, True)):
        assert inner(at.run_tool(v, "attendance", {"member": "Briarwood"})) == at.OFFICERS_ONLY
        assert inner(at.run_tool(v, "attendance", {"member": "Nobodyhere"})) == at.OFFICERS_ONLY, "never says who exists"
    officer = inner(at.run_tool(viewer(reg, rs, MARROW, True), "attendance", {"member": "Briarwood"}))
    assert officer == f"Pellow's attendance: {reg.attendance_text(reg.members[PELLOW])}."


# ---------------------------------------------------------------- manual


def _officer_part():
    tiers = {s["id"]: s["tier"] for s in help_mod.manual_sections()}
    return next(p for p in help_mod.manual_parts() if tiers.get(p["section"]) == "officers")


def test_manual_tool_reads_parts_and_marks_officer_sections_as_today(reg, rs):
    askers(reg)
    part = _officer_part()
    head = f"### {part['id']} (§{part['section']} {part['title']})"
    assert "officer" in head.lower(), "the heading carries the section's tier, as help.manual_read prints it"
    member = inner(at.run_tool(viewer(reg, rs, WREN), "manual", {"ids": [part["id"]]}))
    assert head in member and member.startswith(at.MEMBER_READS)
    officer = inner(at.run_tool(viewer(reg, rs, MARROW, True), "manual", {"ids": [part["id"]]}))
    assert head in officer and at.MEMBER_READS not in officer
    plain = next(p for p in help_mod.manual_parts() if not help_mod.section_tier(p["title"]))
    text = inner(at.run_tool(viewer(reg, rs, WREN), "manual", {"ids": [plain["id"]]}))
    assert text.startswith(f"### {plain['id']} ") or at.MEMBER_READS in text
    assert inner(at.run_tool(viewer(reg, rs, WREN), "manual", {"ids": ["99.9"], "query": "zzqx"})) == at.NO_MANUAL


def test_manual_and_section_results_cap_at_4000(reg, rs):
    askers(reg)
    many_crafters(reg, 80)
    assert at.TOOLS["manual"].cap == at.TOOLS["section"].cap == at.BIG_RESULT_CAP == 4_000
    big = sorted(help_mod.manual_parts(), key=lambda p: -len(p["body"]))[:3]
    text = body(at.run_tool(viewer(reg, rs, MARROW, True), "manual", {"ids": [p["id"] for p in big]}))
    assert len(text) <= 4_000 + wrapper_len("manual") and text.endswith("more lines left out.\n</data>")
    who = body(at.run_tool(viewer(reg, rs, MARROW, True), "section", {"key": "who"}))
    assert len(who) <= 4_000 + wrapper_len("section") and "narrow it" not in who


# ---------------------------------------------------------------- section


def test_section_tool_officer_only_section_refused_for_member(reg, rs):
    askers(reg)
    dentist(reg)
    for v in (viewer(reg, rs, PELLOW), viewer(reg, rs, WREN), viewer(reg, rs, PELLOW, public=True)):
        assert inner(at.run_tool(v, "section", {"key": "absences"})) == at.OFFICERS_ONLY
    officer = inner(at.run_tool(viewer(reg, rs, MARROW, True), "section", {"key": "absences"}))
    assert "dentist" in officer


def test_section_tool_officer_public_is_refused(reg, rs):
    askers(reg)
    dentist(reg)
    v = viewer(reg, rs, MARROW, True, public=True)
    assert inner(at.run_tool(v, "section", {"key": "absences"})) == at.OFFICERS_ONLY
    who = inner(at.run_tool(v, "section", {"key": "who"}))
    assert help_mod.who_line(reg, reg.members[WREN]) in who


def test_section_tool_builds_todays_section(reg, rs):
    askers(reg)
    applications(reg)
    v = viewer(reg, rs, MARROW, True, question="what did Newname say in the application?")
    text = inner(at.run_tool(v, "section", {"key": "applications"}))
    _p, build, _cap, _o = help_mod.SECTIONS["applications"]
    today = build(v.ask())
    assert text.split("\n")[0] == today[0] and len(text.split("\n")) == len(today), "the builder's own lines, unchanged"
    assert "Newname" in text and "Friends here" in text
    assert at.NO_MATCH.format(name="nope") in inner(at.run_tool(v, "section", {"key": "nope"}))

