"""The Confirm rule (user decision 2026-10-26, CLAUDE.md Principles; design §5.57): every sentence a confirm prints before
a press — a `decide` screen, a `useConfirm`, a review diff, a preview — is read aloud by a raid leader with a finger on
the button. This harness renders EVERY confirm generator across its cases and lints each sentence: a capital letter (or
a digit, or a name) to start, . ! or ? to end, twenty words or fewer, no parentheses, semicolons, ": " chaining two
clauses, " — " glue or "no new DM", and at most three sentences per confirm. A sentence that fails is rewritten in its
generator, never allow-listed (there is no allow list here, on purpose).

Every rendered confirm is also written to a file, one block per generator and case, so a person can read them all:
out/confirm_sentences.txt, and the path in OIBOT_CONFIRM_SENTENCES when it is set. Passing the lint is necessary, not
sufficient: read the file as a raid leader would."""
from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from conftest import OWNER, join, lock_with_board

from oibot_gm import consequences as cq
from oibot_gm import discord_people, inbox
from oibot_gm import raidcycle as rc
from oibot_gm.registry import Applicant

ROOT = Path(__file__).resolve().parents[1]
RID = "barrow_deeps"
REAL = 470_000_000_000_000_000
OFFICER_ID = 470_000_000_000_000_999

# ------------------------------------------------------------------------------------------------------- the lint
MAX_WORDS = 20
MAX_SENTENCES = 3
_SPLIT = re.compile(r"(?<=[.!?])\s+")
_LEAD = re.compile(r"^(?:-#\s*|[⚠✅❌⏳•☠🔒✓]️?\s*)+")
_BANNED = [("(", "a parenthesis"), (")", "a parenthesis"), (";", "a semicolon"), (": ", "a colon chaining two clauses"),
           (" — ", "a dash gluing fragments"), (" – ", "a dash gluing fragments"), ("no new DM", "\"no new DM\"")]


def sentences(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = _LEAD.sub("", str(line).replace("**", "").strip())
        out += [s.strip() for s in _SPLIT.split(text) if s.strip()]
    return out


def lint(lines: list[str], names: set[str]) -> list[str]:
    """The problems of one confirm (its lines), [] when it reads to the rule. `names`: the proper names a sentence may
    open with in lower case (a member called cbottoms is still a name)."""
    problems: list[str] = []
    ss = sentences(lines)
    if len(ss) > MAX_SENTENCES:
        problems.append(f"{len(ss)} sentences (at most {MAX_SENTENCES})")
    for s in ss:
        first = s.split()[0].strip("“”\"'’,") if s.split() else ""
        if not (s[:1].isupper() or s[:1].isdigit() or s[:1] in "<@" or first.split("'")[0] in names or first.removesuffix("’s") in names):
            problems.append(f"starts in lower case: {s!r}")
        if s[-1:] not in ".!?":
            problems.append(f"no full stop: {s!r}")
        if len(s.split()) > MAX_WORDS:
            problems.append(f"{len(s.split())} words: {s!r}")
        for bad, what in _BANNED:
            if bad in s:
                problems.append(f"{what}: {s!r}")
    return problems


# ------------------------------------------------------------------------------------------------------- the world
PEOPLE = [("Bramble", "Ashgrove", "Warrior", "Protection"), ("cbottoms", "Thornwick", "Warrior", "Protection"),
          ("Cedar", "Larkspur", "Priest", "Holy"), ("Dorran", "Mossvale", "Priest", "Holy"), ("Elske", "Fernhollow", "Priest", "Holy"),
          ("Ivy", "Quillon", "Mage", "Fire"), ("Juniper", "Rowanel", "Mage", "Fire"), ("Kasimir", "Sorrel", "Mage", "Fire"),
          ("Linnea", "Tamsin", "Mage", "Fire"), ("Marrow", "Umber", "Mage", "Fire"), ("Nettle", "Vesper", "Mage", "Fire"),
          ("Orrin", "Wrenna", "Mage", "Fire")]


def people(reg):
    ms = []
    for i, (who, char, cls, spec) in enumerate(PEOPLE):
        reg.add_character(REAL + i, who, char, cls, spec, None, True)
        ms.append(reg.members[REAL + i])
    reg.add_character(OFFICER_ID, "Officer", "Kingsley", "Mage", "Fire", None, True)
    return ms


def names_of(reg) -> set[str]:
    out = {"Barrow", "Nettle", "Ivy", "Group"}
    for m in reg.members.values():
        out.add(m.display_name)
        out |= {c.label for c in m.characters} | {c.name for c in m.characters}
    return out


def weekly(reg):
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30", "Thu 19:30", "Sat 19:30"], "first_open": "2026-01-01T00:00"}, "t")


def recurring_run(reg, rs, n: int = 0):
    now = reg.now_local()
    t = [t for _s, t in rc.upcoming(reg, RID, now, 24 * 21) if t - now >= timedelta(hours=30)][n]
    return rc.open_run(reg, rs, RID, t, by="t")


def adhoc_run(reg, rs, days: float = 2):
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, RID, start, by="t", schedule=rc.ADHOC_SCHEDULE)


def locked(reg, rs, ev, ms, *, asks: bool = True, off: int | None = None, autofill=True, paused=False):
    """`ms` joined; the first ten rostered by the board; Confirm / Can't asks for them when the run asks for them
    (`off`: that one waits on the site with DMs off)."""
    join(reg, rs, ev, ms)
    seated = ms[:10]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated], hours_to_confirm=2)
    team = rc.run_team(reg, ev)
    team["autofill"] = autofill
    ev.autofill_paused = paused
    if asks and rc.run_confirms(reg, ev):
        for i, m in enumerate(seated):
            reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock", channel="web" if i == off else "dm")
    rs.save(ev, "harness")
    return ev


class Dump:
    """Every confirm, rendered, linted and written out (the operator reads them all)."""

    def __init__(self, names: set[str]):
        self.names, self.blocks, self.problems = names, [], []

    def add(self, generator: str, case: str, lines, extra_names: set[str] = frozenset(), fates=()):
        """`fates`: the lines that are a ripple into runs, one sentence per fate (rc.character_effects, an absence's
        sheets — design §5.57 "one sentence per fate"): each is linted as a confirm of its own, and the rest of the
        lines (what changes · who is told · what can be undone) as one confirm of at most three sentences. The file
        shows them together, as the screen does."""
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(x) for x in (lines or [])]
        self.blocks.append(f"## {generator} · {case}\n" + ("\n".join(lines) if lines else "(nothing)"))
        fates = [f for f in fates if f in lines]
        names = self.names | set(extra_names)
        for p in lint([x for x in lines if x not in fates], names):
            self.problems.append(f"{generator} · {case}: {p}")
        for f in fates:
            for p in lint([f], names):
                self.problems.append(f"{generator} · {case} · a fate: {p}")

    def write(self):
        text = "\n\n".join(self.blocks) + "\n"
        paths = [ROOT / "out" / "confirm_sentences.txt"]
        if os.environ.get("OIBOT_CONFIRM_SENTENCES"):
            paths.append(Path(os.environ["OIBOT_CONFIRM_SENTENCES"]))
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)


# ------------------------------------------------------------------------------------------------------- the cases

def run_cases(reg, rs, ms):
    """name → run, every state a run verb's confirm can meet."""
    weekly(reg)
    cases = {}
    # open recurring: six joined (one with DMs off), two placed on the board
    ev = recurring_run(reg, rs, 0)
    join(reg, rs, ev, ms[:6])
    join(reg, rs, ev, ms[6:7], "sub")
    ms[3].dm_opt_out = True
    ev.layout = [[ms[0].display_name, ms[2].display_name]]
    rs.save(ev, "harness")
    cases["open recurring"] = ev
    cases["open recurring, nobody joined"] = recurring_run(reg, rs, 1)
    a = adhoc_run(reg, rs, 2)
    join(reg, rs, a, ms[:4])
    cases["open ad hoc"] = a
    cases["locked recurring, autofill on"] = locked(reg, rs, recurring_run(reg, rs, 2), ms, off=3)
    cases["locked recurring, autofill off"] = locked(reg, rs, recurring_run(reg, rs, 2 + 1), ms, autofill=False)
    cases["locked recurring, autofill paused"] = locked(reg, rs, recurring_run(reg, rs, 2 + 2), ms, paused=True)
    cases["locked ad hoc"] = locked(reg, rs, adhoc_run(reg, rs, 4), ms)
    d = locked(reg, rs, adhoc_run(reg, rs, 5), ms)
    rc.end_run(reg, rs, d, "t")
    cases["done"] = d
    c = adhoc_run(reg, rs, 6)
    c.state = "cancelled"
    rs.save(c, "harness")
    cases["cancelled"] = c
    return cases


def test_every_confirm_reads_aloud(reg, rs, monkeypatch):
    ms = people(reg)
    runs = run_cases(reg, rs, ms)
    dump = Dump(names_of(reg))

    # ---- consequences.consequence: every verb × every run
    for case, ev in runs.items():
        for verb in cq.VERBS:
            dump.add(f"consequence({verb})", case, cq.consequence(verb, reg, ev, name="Nettle", other="Ivy", to="Group 2"))
        dump.add("consequence(release, no name)", case, cq.consequence("release", reg, ev))
        for k, v in cq.consequences_json(reg, ev).items():
            dump.add(f"consequences_json[{k}]", case, v.replace(cq.NAME_TOKEN, "Nettle").replace(cq.OTHER_TOKEN, "Ivy").replace(cq.TO_TOKEN, "Group 2"))
    # remind: reminded within the hour, everyone answered, all on the site
    ev = runs["locked recurring, autofill on"]
    ev.reminded_at, ev.reminded_by = rc.now(), "Officer"
    dump.add("consequence(remind)", "reminded within the hour", cq.consequence("remind", reg, ev))
    ev.reminded_at = None
    for m in ms[:10]:
        for a in m.placement_asks:
            a["answer"] = "yes"
    dump.add("consequence(remind)", "everyone answered", cq.consequence("remind", reg, ev))
    for m in ms[:10]:
        for a in m.placement_asks:
            a["answer"], a["channel"] = None, "web"
    dump.add("consequence(remind)", "every unanswered on the site", cq.consequence("remind", reg, ev))
    for i, m in enumerate(ms[:10]):
        for a in m.placement_asks:
            a["channel"] = "web" if i == 3 else "dm"
    # lock: a test run
    from conftest import open_test_run
    t = open_test_run(reg, rs)
    join(reg, rs, t, reg.test_members()[:6])
    dump.add("consequence(lock)", "test run", cq.consequence("lock", reg, t))

    # ---- regular_remove
    m = ms[0]
    reg.regular_add(m.discord_id, f"{RID}:default", "t")
    dump.add("regular_remove", "a running schedule", cq.regular_remove(reg, reg.members[m.discord_id], f"{RID}:default"))
    dump.add("regular_remove", "a removed schedule", cq.regular_remove(reg, reg.members[m.discord_id], f"{RID}:gone"))

    # ---- inbox.action_consequence: every key member_actions offers, every run, by an officer and on their own record
    for case, ev in runs.items():
        if ev.state not in ("open", "locked"):
            continue
        seen = set()
        for m in ms:
            for act in rc.member_actions(reg, ev, m):
                k = act["key"]
                state = ("rostered" if ev.state == "locked" and ev.seat_of(m) else "not rostered") + ", DMs off" * bool(m.dm_opt_out)
                if (k, state) in seen:
                    continue
                seen.add((k, state))
                dump.add(f"action_consequence({k})", f"{case}, {state}", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))
                dump.add(f"action_consequence({k})", f"{case}, {state}, own record", inbox.action_consequence(reg, ev, m, k, by_id=m.discord_id))
    # fill asks: a fill after the lock (with its DM card) and a pre-lock ask
    ev = runs["locked recurring, autofill on"]
    m = ms[10]
    ev.fill_asks.append(rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="ranged", character=m.main.label,
                                   spec=m.main.spec, reason="short 1", expires_at=ev.start.isoformat(), dm_message_id=5))
    for k in ("fill_yes", "fill_no", "callout", "no_thanks"):
        dump.add(f"action_consequence({k})", "locked recurring, fill ask with its DM card", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))
    ev = runs["open recurring"]
    m = ms[8]
    ev.fill_asks.append(rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="ranged", character=m.main.label,
                                   spec=m.main.spec, reason="short 1", phase="prelock", expires_at=ev.start.isoformat()))
    for k in ("fill_yes", "fill_no"):
        dump.add(f"action_consequence({k})", "open recurring, pre-lock ask", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))
    # Join after the lock with a seat free: DMs on, DMs off, and on an ad hoc run
    for case in ("locked recurring, autofill on", "locked ad hoc"):
        ev = runs[case]
        held = ms[9]
        rc.free_seat(reg, rs, ev, held.discord_id, "officer")
        m = ms[11]
        rc.set_signup(reg, rs, ev, m, None, "sub", source="test")
        for off in (False, True):
            m.dm_opt_out = off
            assert any(a["key"] == "join" for a in rc.member_actions(reg, ev, m))
            dump.add("action_consequence(join)", f"{case}, a seat free" + ", DMs off" * off, inbox.action_consequence(reg, ev, m, "join", by_id=OFFICER_ID))
        m.dm_opt_out = False
        rc.set_signup(reg, rs, ev, m, None, "in", source="test")
        rc.set_signup(reg, rs, ev, held, None, "in", source="test")
        rc.seat_player(reg, ev, ev.signups[str(held.discord_id)])
    # a Confirm card in a DM
    ev = runs["locked recurring, autofill on"]
    m = ms[4]
    for a in m.placement_asks:
        a["dm"] = 77
    for k in ("confirm_yes", "confirm_no", "callout", "no_thanks"):
        dump.add(f"action_consequence({k})", "locked recurring, Confirm card in a DM", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))

    # ---- the Me page's own Can't make it… (web/api_me.cant_consequence)
    from oibot_gm.web.api_me import QUEUE_NOTE, cant_consequence

    for case in ("locked recurring, autofill on", "locked recurring, autofill off", "locked ad hoc"):
        dump.add("cant_consequence", f"{case}, rostered", cant_consequence(reg, runs[case], True))
        dump.add("cant_consequence", f"{case}, joined, not rostered", cant_consequence(reg, runs[case], False))
    dump.add("QUEUE_NOTE", "ad hoc, joined", QUEUE_NOTE)

    # ---- raidcycle's refusals and previews
    for case, ev in runs.items():
        dump.add("cancel_effects via consequence(cancel)", case, cq.consequence("cancel", reg, ev))
        dump.add("remind_refusal", case, rc.remind_refusal(reg, ev) or "")
        dump.add("reopen_refusal", case, rc.reopen_refusal(reg, ev) or "")
        dump.add("end_refusal", case, rc.end_refusal(ev) or "")
    ev = runs["locked recurring, autofill on"]
    dump.add("end_refusal", "locked, after the start", rc.end_refusal(ev, ev.start + timedelta(minutes=5)) or "")
    d = runs["done"]
    dump.add("reopen_refusal", "done, over an hour ago", rc.reopen_refusal(reg, d, d.start + timedelta(days=30)) or "")
    dump.add("closed_run_refusal", "done", rc.closed_run_refusal(rs, d.key) or "")
    dump.add("closed_run_refusal", "cancelled", rc.closed_run_refusal(rs, runs["cancelled"].key) or "")
    far = reg.now_local() + timedelta(days=40)
    dump.add("open_row_refusal", "too far ahead", rc.open_row_refusal(reg, rs, RID, far, rc.event_key(RID, far, "default"), far - timedelta(days=2)) or "")
    dump.add("open_row_refusal", "too far ahead, the sheet opens before Open works", rc.open_row_refusal(reg, rs, RID, far, rc.event_key(RID, far, "default"), far - timedelta(days=30)) or "")
    dump.add("open_row_refusal", "closed", rc.open_row_refusal(reg, rs, RID, d.start, d.key, d.start) or "")
    p = rc.open_preview(reg, rs, RID, None, None)
    dump.add("open_preview", "the next scheduled run", [p["rules"], p["refusal"] or "", p["when_note"]])
    when = (reg.now_local() + timedelta(days=3)).strftime("%Y-%m-%d 21:15")
    p = rc.open_preview(reg, rs, RID, when, rc.ADHOC_SCHEDULE)
    dump.add("open_preview", "an ad hoc run", [p["rules"], p["refusal"] or "", p["when_note"]])
    from oibot_gm.wizard import resolve_local  # noqa: F401 — dst_words is open_preview's when_note
    dump.add("dst_words", "gap", rc.dst_words(reg, "gap", reg.now_local()))
    dump.add("dst_words", "ambiguous", rc.dst_words(reg, "ambiguous", reg.now_local()))

    # ---- absences: what recording one does, and "I'm back"
    for case in ("open recurring", "locked recurring, autofill on"):
        ev = runs[case]
        day = rc.run_day(reg, ev)
        for who in (ms[0], ms[11]):
            dump.add("absence_lines", f"{case}, {'rostered' if ev.seat_of(who) else 'not rostered'}", discord_people.absence_lines(reg, rs, who, day, day))
    m = ms[0]
    ev = runs["open recurring"]
    day = rc.run_day(reg, ev)
    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
    dump.add("absence_clear_lines", "open sheet", discord_people.absence_clear_lines(reg, rs, m, SimpleNamespace(start=day, end=day)))
    ev = runs["locked recurring, autofill on"]
    day2 = rc.run_day(reg, ev)
    ev.signups[str(m.discord_id)].status, ev.signups[str(m.discord_id)].source = "out", "absence"
    dump.add("absence_clear_lines", "locked run", discord_people.absence_clear_lines(reg, rs, m, SimpleNamespace(start=day2, end=day2)))
    ev.signups[str(m.discord_id)].status = "in"
    rc.set_signup(reg, rs, runs["open recurring"], m, None, "in", source="member")

    # ---- character changes ripple into runs
    ev = runs["locked recurring, autofill on"]
    tank = ms[1]
    for change in ({"kind": "retire", "label": tank.main.label}, {"kind": "delete", "label": tank.main.label},
                   {"kind": "spec", "label": tank.main.label, "spec": "Arms", "role": "melee", "old_spec": "Protection", "old_role": "tank"},
                   {"kind": "spec", "label": tank.main.label, "spec": "Protection", "role": "tank", "old_spec": "Protection", "old_role": "tank"},
                   {"kind": "name", "label": tank.main.label, "new_label": "Thornwick Vale"}):
        for e in rc.character_effects(reg, rs, tank, change):
            dump.add(f"character_effects({change['kind']})", f"{e['kind']} on {'a run' if e['ev'] else 'the record'}", e["text"])
    adhoc = runs["locked ad hoc"]
    for e in rc.character_effects(reg, rs, tank, {"kind": "spec", "label": tank.main.label, "spec": "Arms", "role": "melee", "old_spec": "Protection", "old_role": "tank"}):
        if e["ev"] is adhoc:
            dump.add("character_effects(spec)", "role flip on an ad hoc run", e["text"])
    opener = ms[11]
    reg.add_character(opener.discord_id, opener.display_name, "Spareling", "Mage", "Frost", None, False)
    reg.regular_add(opener.discord_id, f"{RID}:default", "t", "Spareling")
    o = runs["open recurring"]
    rc.set_signup(reg, rs, o, opener, "Spareling", "in", source="member")
    for change in ({"kind": "retire", "label": "Spareling"}, {"kind": "spec", "label": "Spareling", "spec": "Arcane", "role": "ranged", "old_spec": "Frost", "old_role": "ranged"},
                   {"kind": "name", "label": "Spareling", "new_label": "Sparrow"}):
        for e in rc.character_effects(reg, rs, opener, change):
            if e["ev"] is o or e["ev"] is None:
                dump.add(f"character_effects({change['kind']})", f"{e['kind']} on {'an open sheet' if e['ev'] else 'the record'}", e["text"])
    rc.set_signup(reg, rs, o, opener, "Spareling", "out", source="member")
    for e in rc.character_effects(reg, rs, opener, {"kind": "retire", "label": "Spareling"}):
        if e["ev"] is o:
            dump.add("character_effects(retire)", "a No thanks moves to another character", e["text"])
    for e in rc.character_effects(reg, rs, opener, {"kind": "main", "label": "Spareling"}):
        dump.add("character_effects(main)", "a new main", e["text"])

    # ---- people (design §5.58): one generator per people verb, by an officer and on the member's own record
    people_cases(reg, rs, runs, ms, dump)

    # ---- the loot council's End and the done line
    from oibot_gm.discord_loot import LootMixin

    ev = runs["locked recurring, autofill on"]
    bot = SimpleNamespace(events={})
    dump.add("end_consequence", "no council", LootMixin.end_consequence(bot, reg, rs, ev))
    sess = SimpleNamespace(guild=reg.key, run_key=ev.key)
    bot = SimpleNamespace(events={"s": sess}, loot_summary=lambda reg, s: {"awards": 6, "overrides": 1, "never_confirmed": [1], "undistributed": [1, 2],
                                                                           "reasons_pending": [1]})
    dump.add("end_consequence", "a council with loose ends", LootMixin.end_consequence(bot, reg, rs, ev))
    bot.loot_summary = lambda reg, s: {"awards": 1, "overrides": 0, "never_confirmed": [], "undistributed": [], "reasons_pending": []}
    dump.add("end_consequence", "a tidy council", LootMixin.end_consequence(bot, reg, rs, ev))
    dump.add("ended_line", "after End", cq.ended_line(reg, ev))

    # ---- registry
    dump.add("owner_consequence", "the owner hands it over", reg.owner_consequence("Bramble", OWNER))
    from oibot_gm import registry as registry_mod
    monkeypatch.setattr(registry_mod, "is_superuser", lambda uid: uid == 9)
    dump.add("owner_consequence", "a host superuser", reg.owner_consequence("Bramble", 9))
    for kw in ({"bot": True}, {}, {"membership": "no"}, {"membership": "unknown"}):
        uid = OWNER if not kw else REAL
        dump.add("owner_refusal", str(kw or "the current owner"), reg.owner_refusal(uid, by_id=REAL + 5, name="Bramble", **kw) or "")
    dump.add("owner_refusal", "yourself", reg.owner_refusal(REAL + 5, by_id=REAL + 5, name="Bramble") or "")
    for mode in ("", "keep", "move"):
        dump.add("timezone_change_effects", f"mode {mode or 'none'}, every live run", reg.timezone_change_effects("America/New_York", rs, mode))
    rows = reg.timezone_runs("America/New_York", rs)
    picks = {"locked": next(r for r in rows if r["locked"]), "test": next(r for r in rows if r["test"]),
             "open": next(r for r in rows if not r["locked"] and not r["test"])}
    real_runs = reg.timezone_runs
    for what, row in picks.items():
        monkeypatch.setattr(reg, "timezone_runs", lambda zone, rs=None, row=row: [row])
        for mode in ("", "keep", "move"):
            dump.add("timezone_change_effects", f"mode {mode or 'none'}, one {what} run", reg.timezone_change_effects("America/New_York", rs, mode))
        if what == "open":
            monkeypatch.setattr(reg, "timezone_runs", lambda zone, rs=None, row=row: [{**row, "movable": False}])
            dump.add("timezone_change_effects", "mode move, one open run whose lock would pass", reg.timezone_change_effects("America/New_York", rs, "move"))
    monkeypatch.setattr(reg, "timezone_runs", real_runs)
    dump.add("timezone_refusal", "a locked run, no mode", reg.timezone_refusal("America/New_York", rs, "") or "")
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt nights"}, "t")
    reg.regular_add(ms[5].discord_id, f"{RID}:alt", "t")
    reg.regular_add(ms[6].discord_id, f"{RID}:alt", "t")
    fx = reg.schedule_removal_effects(RID, "alt", rs)
    dump.add("schedule_removal_effects.lines", "two regulars, nothing open", fx.lines())
    dump.add("schedule_removal_effects.sentence", "two regulars, nothing open", fx.sentence())
    fx = reg.schedule_removal_effects(RID, "default", rs)
    dump.add("schedule_removal_effects.lines", "the raid's own run times, runs open", fx.lines())
    dump.add("schedule_removal_effects.sentence", "the raid's own run times, runs open", fx.sentence())
    dump.add("aura_reset_sentence", "nothing changed", reg.aura_reset_sentence())
    b = reg.profile.buffs[0]
    reg.set_buff_overrides(b.id, {"note": "ours"}, "t")
    dump.add("aura_reset_sentence", "one buff changed", reg.aura_reset_sentence())
    from oibot_gm import configops

    dump.add("aura_reset_one_sentence", "a changed buff", configops.aura_reset_one_sentence(reg, b.id))
    dump.add("aura_reset_one_sentence", "an unchanged buff", configops.aura_reset_one_sentence(reg, reg.profile.buffs[1].id))
    fam = next(iter(reg.profile.families), None)
    if fam:
        reg.set_family_overrides(fam, {"note": "ours"}, "t")
        dump.add("aura_reset_one_sentence", "a changed family", configops.aura_reset_one_sentence(reg, fam))
    dump.add("undo_consequence", "rows live", reg.undo_consequence({"live": 3}))
    dump.add("undo_consequence", "already voided", reg.undo_consequence({"live": 0}))
    a = Applicant(discord_id=REAL + 50, display_name="Pell", name="Saffron", cls="Mage", spec="Fire", status="declined", decided_by="Bramble", decided_at=rc.now())
    dump.add("decided_refusal", "declined", reg.decided_refusal(a))
    dump.add("decided_refusal", "none", reg.decided_refusal(None))
    puppet = reg.test_members()[0]
    reg.config.rosters.append({"key": "x-test", "test": True, "test_by": REAL + 1})
    dump.add("puppet_refusal", "another officer's rehearsal", reg.puppet_refusal(puppet, REAL + 2) or "", {puppet.display_name})
    reg.config.rosters.pop()
    dump.add("live_council_refusal", "a live council", reg.live_council_refusal(["Barrow Deeps on Tue 3 Nov"]) or "")

    # ---- configops
    dump.add("schedule_pause_sentence", "pause, nothing open", configops.schedule_pause_sentence(reg, RID, "alt", True, rs))
    dump.add("schedule_pause_sentence", "pause, runs open", configops.schedule_pause_sentence(reg, RID, "default", True, rs))
    dump.add("schedule_pause_sentence", "resume", configops.schedule_pause_sentence(reg, RID, "alt", False, rs))
    Op = configops.ConfigOp
    reg.config.officer_role_ids = [5]
    for op in (Op(op="schedule_remove", target=RID, field="alt"), Op(op="aura_reset"), Op(op="aura_reset", target=b.id),
               Op(op="role_remove", value="5"), Op(op="recruit", path="open", value="off"),
               Op(op="set", path="timezone", value="America/New_York", field="keep")):
        try:
            got = configops.op_consequences(reg, op)
        except Exception as e:  # noqa: BLE001 — an op this guild can't take still shows what it says
            got = [f"(raised {type(e).__name__})"]
        dump.add(f"op_consequences({op.op})", op.path or op.target or "", got)
    reg.config.signup_channel_id = 11
    reg.config.ops_channel_id = 12
    for path in ("signup_channel", "ops_channel"):
        dump.add("op_consequences(set clear)", path, configops.op_consequences(reg, Op(op="set", path=path, value="none")))
    dump.add("listen_reply_sentence", "a channel starts replying", configops.listen_reply_sentence(11))
    dump.add("op_consequences(plain listen)", "reply", configops.op_consequences(reg, Op(op="plain", target="listen <#11>", value="reply")))
    dump.add("_timezone_consequences", "move", configops._timezone_consequences(reg, Op(op="set", path="timezone", value="America/New_York", field="move")))
    d = configops.describe_ops(reg, [Op(op="schedule_remove", target=RID, field="alt")])
    dump.add("describe_ops consequences", "schedule remove", d.consequences)
    dump.add("plain-text diff (as_discord)", "schedule remove", [x for x in d.as_discord().splitlines() if x.startswith("⚠")])

    # ---- discord_registry
    from oibot_gm import discord_registry

    dump.add("remove_record_consequence", "a member who left", discord_registry.remove_record_consequence(reg, ms[11]))
    ap = Applicant(discord_id=REAL + 51, display_name="Pell", name="Saffron", cls="Mage", spec="Fire")
    dump.add("application_consequence", "accept, no member role", discord_registry.application_consequence(reg, ap, True))
    reg.config.member_role_ids = [33]
    dump.add("application_consequence", "accept, member role", discord_registry.application_consequence(reg, ap, True))
    dump.add("application_consequence", "decline", discord_registry.application_consequence(reg, ap, False))
    ap.history = [{"status": "declined", "at": rc.now(), "by": "Bramble", "note": "full on mages"}]
    dump.add("application_consequence", "decline, applied before", discord_registry.application_consequence(reg, ap, False))

    # ---- discord_pool
    from oibot_gm import discord_pool

    for kind in ("signup", "registration", "absences", "analytics", "applications", "roster", "ops", "news", "loot"):
        cur = getattr(reg.config, discord_pool.CHANNEL_KINDS[kind], None)
        dump.add("channel_consequences", f"{kind} → a new channel", discord_pool.channel_consequences(reg, kind, 99))
        setattr(reg.config, discord_pool.CHANNEL_KINDS[kind], 98)
        dump.add("channel_consequences", f"{kind} cleared", discord_pool.channel_consequences(reg, kind, None))
        dump.add("channel_consequences", f"{kind} the same", discord_pool.channel_consequences(reg, kind, 98))
        dump.add("channel_consequences", f"{kind} moved", discord_pool.channel_consequences(reg, kind, 97))
        setattr(reg.config, discord_pool.CHANNEL_KINDS[kind], cur)
    from oibot_gm import perms

    dump.add("cant_post", "one missing", perms.cant_post("raids", ["embed_links"]))
    dump.add("cant_post", "two missing, one with a reason", perms.cant_post("raids", ["send_messages", "manage_messages"]))
    dump.add("cant_post", "three missing, two with reasons", perms.cant_post("raids", ["send_messages", "manage_messages", "create_public_threads"]))
    roles = {1: SimpleNamespace(is_default=lambda: True, managed=False, name="everyone"),
             2: SimpleNamespace(is_default=lambda: False, managed=True, name="Carl-bot")}
    gfake = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(get_role=lambda rid: roles.get(rid)))
    for rid in (1, 2, 3):
        dump.add("role_refusal", str(rid), discord_pool.SetupMixin.role_refusal(gfake, reg, rid) or "")

    # ---- feed (companion tokens)
    from oibot_gm import feed

    dump.add("issue_consequence", "DMed", feed.issue_consequence(reg, "Raid PC", "Bramble"))
    dump.add("issue_consequence", "shown once", feed.issue_consequence(reg, "Raid PC"))
    rec = SimpleNamespace(label="Raid PC")
    for state in ("live", "disconnected", None):
        dump.add("revoke_consequence", str(state), feed.revoke_consequence(reg, rec, {"state": state, "characters": ["Ashgrove"]} if state else None))

    # ---- appeals
    from oibot_gm import appeals

    award = SimpleNamespace(item_name="Robe of Tests", item_id=1)
    dump.add("open_consequence", "with an officers' channel", appeals.open_consequence(reg, award))
    reg.config.roster_channel_id = reg.config.ops_channel_id = None
    dump.add("open_consequence", "no officers' channel", appeals.open_consequence(reg, award))
    apl = SimpleNamespace(member_name="Bramble", item="Robe of Tests")
    for outcome in appeals.OUTCOMES:
        dump.add("resolve_consequence", outcome, appeals.resolve_consequence(reg, apl, outcome))

    # ---- news proposals
    from oibot_gm import news
    from oibot_gm.news import Proposal

    for kind in ("guild_setting", "profile", "needs_developer"):
        p = Proposal(id="261026-abcd", title="t", affects="timezone", kind=kind, change="Raise the raid size to 12", news=[], state="proposed",
                     edit={"op": "set", "path": "timezone", "value": "America/New_York"} if kind == "guild_setting" else {})
        for state in ("approved", "dismissed"):
            dump.add(f"news.consequence({state})", kind, news.consequence(p, reg, state, None, busy={reg.key: {"raiding": True, "why": "Barrow Deeps is locked"}},
                                                                            next_run="Tue 7:45 PM"))
        p.state = "approved"
        dump.add("news.consequence(dismissed)", f"{kind}, withdrawing an approval", news.consequence(p, reg, "dismissed"))
        dump.add("news.consequence(interrupted)", kind, news.consequence(p, reg, "interrupted"))
    for text in (news.PROFILE_OWNER_ONLY, news.SETTING_OWNER_ONLY, news.INTERRUPTED):
        dump.add("news refusal", text[:20], text)
    p.decided_by, p.decided_at = "Bramble", rc.now()
    dump.add("news.already_text", "approved", news.already_text(p, reg.local12))
    dump.add("news.profile_approval_refusal", "an officer approved it", news.profile_approval_refusal(None, reg, SimpleNamespace(kind="profile", decided_by_id=5, decided_by="Offi")) or "")

    # ---- flows_misc.test_clear_consequence
    from oibot_gm import flows_misc

    dump.add("test_clear_consequence", "puppets and a test run", flows_misc.test_clear_consequence(reg, rs))

    dump.write()
    assert not dump.problems, f"{len(dump.problems)} confirm sentences break the Confirm rule:\n" + "\n".join(dump.problems)


def _said(fn, *a, **kw) -> list[str]:
    """A people generator's sentences, or its refusal (RegistryError: one sentence) — both are read before a press."""
    from oibot_gm.registry import RegistryError

    try:
        return fn(*a, **kw)
    except RegistryError as e:
        return [str(e)]


def people_cases(reg, rs, runs, ms, dump):
    """Every people generator (consequences.people_consequence) across its cases: an officer on someone, and the member
    on their own record ("your"); a rostered main with an alt, an alt, a lone main; a role flip on a recurring and an
    ad hoc run; the refusals."""
    tank = ms[1]  # rostered on the locked runs
    reg.add_character(tank.discord_id, tank.display_name, "Briarvane", "Warrior", "Arms", None, False)
    tank = reg.members[tank.discord_id]
    solo, _c = reg.add_character(REAL + 60, "Solo", "Soloist", "Mage", "Fire", None, True)
    fates = lambda m, changes: rc.character_preview(reg, rs, m, changes)  # noqa: E731
    for who, by in (("an officer", OFFICER_ID), ("own record", tank.discord_id)):
        main, alt = tank.main.label, "Briarvane"
        for verb in ("retire", "delete"):
            for which, label in (("a rostered main with an alt", main), ("an alt", alt)):
                dump.add(f"{verb}_consequence", f"{which}, {who}", _said(cq.people_consequence, reg, rs, tank, verb, by_id=by, label=label),
                         fates=fates(tank, [{"kind": verb, "label": label}]))
            dump.add(f"{verb}_consequence", f"a lone main, {who}", _said(cq.people_consequence, reg, rs, solo, verb, by_id=solo.discord_id if who == "own record" else by,
                                                                        label=solo.main.label), {"Solo", "Soloist"})
        dump.add("main_consequence", f"an alt becomes the main, {who}", _said(cq.main_consequence, reg, rs, tank, alt, by_id=by),
                 fates=fates(tank, [{"kind": "main", "label": alt}]))
        dump.add("main_consequence", f"already the main, {who}", _said(cq.main_consequence, reg, rs, tank, main, by_id=by))
        dump.add("spec_consequence", f"a role flip on a recurring and an ad hoc run, {who}", _said(cq.spec_consequence, reg, rs, tank, main, "Arms", None, by_id=by),
                 fates=fates(tank, rc.character_changes(reg, tank, [{"label": main, "cls": "Warrior", "spec": "Arms"}])))
        dump.add("spec_consequence", f"an offspec only, {who}", _said(cq.spec_consequence, reg, rs, tank, main, "Protection", "Arms", by_id=by))
        dump.add("spec_consequence", f"nothing changes, {who}", _said(cq.spec_consequence, reg, rs, tank, main, "Protection", None, by_id=by))
        dump.add("flex_consequence", f"two extra roles, {who}", _said(cq.flex_consequence, reg, rs, tank, main, ["healer", "ranged"], by_id=by))
        tank.main.flex = ["healer"]
        dump.add("flex_consequence", f"cleared, {who}", _said(cq.flex_consequence, reg, rs, tank, main, [], by_id=by))
        dump.add("flex_consequence", f"the same roles, {who}", _said(cq.flex_consequence, reg, rs, tank, main, ["healer"], by_id=by))
        tank.main.flex = []
        dump.add("profession_consequence", f"one recorded, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}], by_id=by))
        dump.add("profession_consequence", f"two recorded, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "alchemy", "skill": 120}, {"id": "cooking", "skill": 300}], by_id=by))
        tank.main.professions = reg.check_professions([{"id": "alchemy", "skill": 120}])
        dump.add("profession_consequence", f"removed, {who}", _said(cq.profession_consequence, reg, tank, main, [], by_id=by))
        dump.add("profession_consequence", f"the same professions, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "alchemy", "skill": 120}], by_id=by))
        tank.main.professions = []
        dump.add("rank_consequence", f"trial to core, {who}", _said(cq.rank_consequence, reg, tank, "core", by_id=by))
        dump.add("rank_consequence", f"the same rank, {who}", _said(cq.rank_consequence, reg, tank, tank.rank, by_id=by))
        dump.add("rank_consequence", f"alt, {who}", _said(cq.rank_consequence, reg, tank, "alt", by_id=by))
        dump.add("confirm_consequence", f"a named character, {who}", _said(cq.confirm_consequence, reg, tank, alt, by_id=by))
        for on in (False, True):
            dump.add("dm_consequence", f"turn {'on' if on else 'off'}, {who}", _said(cq.dm_consequence, reg, tank, on, by_id=by))
            tank.dm_opt_out = not on
    tank.dm_opt_out = False
    c = next(x for x in tank.active() if x.label == "Briarvane")
    c.confirmed_by = "Officer"
    dump.add("confirm_consequence", "already confirmed, an officer", _said(cq.confirm_consequence, reg, tank, "Briarvane", by_id=OFFICER_ID))
    reg.set_plan(REAL + 61, "Planner", "Priest", "Holy", None, "main")
    dump.add("confirm_consequence", "a planned character, an officer", _said(cq.confirm_consequence, reg, reg.members[REAL + 61], "Priest (Holy)", by_id=OFFICER_ID), {"Priest"})
    nomain = SimpleNamespace(discord_id=REAL + 62, display_name="Nomain", main=None, rank=None, dm_opt_out=False)
    dump.add("rank_consequence", "no main yet, an officer", _said(cq.rank_consequence, reg, nomain, "core", by_id=OFFICER_ID), {"Nomain"})
    # I'm back: an absence over the open sheet it answered No thanks, and one that touched no sheet
    ev = runs["open recurring"]
    m = ms[0]
    day = rc.run_day(reg, ev)
    reg.add_absence(m.discord_id, day, day, None, "t")
    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
    far = (reg.now_local().date() + timedelta(days=60)).isoformat()
    reg.add_absence(m.discord_id, far, (reg.now_local().date() + timedelta(days=62)).isoformat(), None, "t")
    for who, by in (("an officer", OFFICER_ID), ("own record", m.discord_id)):
        a = next(x for x in m.absences if x.start == day)
        dump.add("back_consequence", f"an open sheet answered for it, {who}", _said(cq.back_consequence, reg, rs, m, day, by_id=by),
                 fates=discord_people.absence_clear_lines(reg, rs, m, a))
        dump.add("back_consequence", f"no sheet touched, {who}", _said(cq.back_consequence, reg, rs, m, far, by_id=by))
    dump.add("back_consequence", "no such absence", _said(cq.back_consequence, reg, rs, m, "2020-01-01", by_id=OFFICER_ID))
    # Add the absence: one over a live sheet, one over no sheet, for an officer and the member's own record
    other = ms[1]
    for who, by in (("an officer", OFFICER_ID), ("own record", other.discord_id)):
        out = cq.absence_consequence(reg, rs, other, day, day, by_id=by)
        dump.add("absence_consequence", f"over a live sheet, {who}", out, fates=discord_people.absence_lines(reg, rs, other, day, day))
        dump.add("absence_consequence", f"no sheet in those days, {who}", cq.absence_consequence(reg, rs, other, far, far, by_id=by))
    span = cq.absence_consequence(reg, rs, other, far, (reg.now_local().date() + timedelta(days=62)).isoformat(), by_id=OFFICER_ID)
    dump.add("absence_consequence", "several days, an officer", span)
    rc.set_signup(reg, rs, ev, m, None, "in", source="member")
    # a Save of edited rows: one sentence per fate (the ripple), nothing else
    rows = [{"label": tank.main.label, "cls": "Warrior", "spec": "Arms"}]
    out = cq.save_consequence(reg, rs, tank, rows)
    dump.add("save_consequence", "a respec on two locked runs", out, fates=out)
    # Remove record with one, two and many characters
    from oibot_gm import discord_registry

    dump.add("remove_record_consequence", "two characters", discord_registry.remove_record_consequence(reg, tank))
    dump.add("remove_record_consequence", "no characters", discord_registry.remove_record_consequence(reg, SimpleNamespace(display_name="Pell", characters=[])), {"Pell"})


def test_the_lint_catches_the_two_blobs():
    """The two confirms the user called unintelligible (2026-10-26) fail the lint; their rewrites pass."""
    names = {"cbottoms"}
    blob1 = ["The board becomes the roster. No confirmations — Join is the commitment. the lock decides the roster.",
             "cbottoms sees a notice on their Me page — no new DM."]
    blob2 = ["cbottoms goes to No thanks on Barrow Deeps Wed 4 Nov 7:00 PM and a callout is recorded (hours before counted; "
             "your note, if any, is for officers). cbottoms sees a notice on their Me page — no new DM."]
    assert lint(blob1, names) and lint(blob2, names)
    assert not lint(["The board becomes the roster. On an ad hoc run Join is the commitment, so nobody is asked to confirm."], names)
    assert not lint(["cbottoms goes to No thanks for Barrow Deeps on Wed 4 Nov. The bot records a callout and how late it came.",
                     "They see it on their Me page."], names)
