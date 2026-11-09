"""The guild guide library (spec docs/superpowers/specs/2026-11-09-guide-library-design.md): a member
uploads a guild-made guide unlocked once by their own BattleTag, the bot keeps the unlocked texts as a dated version per
faction, and any member gets a version locked to their own BattleTag. No BattleTag and no import string is ever written
(D7): the last tests grep the scratch data repo, its git log and the log records."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from oibot_gm import guides as g
from oibot_gm.guides import library as lib
from oibot_gm.guides.build import EMPTY_TEXT
from oibot_gm.guides.reissue import SLOTS
from oibot_gm.guides.rxp_decode import GuideError, decode, encode

AUTHOR_TAG = "Quillwright#48213"
READER_TAG = "Marrowfen#77104"
AUTHOR, READER, OFFICER = 480_000_000_000_000_001, 480_000_000_000_000_002, 480_000_000_000_000_003

TEXTS = ["#name 1-6 Guildwood\n#group Our Route\nstep\n.goto 1429/0,-8949,-132\n.accept 783 >>Take it\n",
         "#name 6-12 Guildshore\n#group Our Route\nstep\n.goto 1436/0,-10600,1030\n.turnin 1 >>Hand it in\n",
         "#name 12-20 Guildhill\n#group Our Route\nstep\n.hs Sentinel Hill\n"]


def string(tag: str = AUTHOR_TAG, sections: int = 2, texts=TEXTS) -> str:
    return encode(list(texts), tag, sections=sections)


@pytest.fixture
def clock(monkeypatch):
    """The library's clock, moved by hand: t[0] is now (UTC)."""
    t = [datetime(2026, 11, 9, 23, 0, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(lib, "_now", lambda: t[0])
    return t


@pytest.fixture
def people(reg):
    reg.add_character(AUTHOR, "Quill", "Quillon", "Mage", "Fire", None, True)
    reg.add_character(READER, "Marrow", "Mossvale", "Priest", "Holy", None, True)
    reg.add_character(OFFICER, "Offi", "Ashgrove", "Warrior", "Protection", None, True)
    return reg


def up(reg, faction="horde", tag=AUTHOR_TAG, uid=AUTHOR, name="Quill", text=None):
    return lib.upload(reg, text if text is not None else string(tag), tag, faction, uid, name)


# ---------------------------------------------------------------- upload → issue

def test_an_upload_issues_for_another_tag_with_the_same_texts_and_sections(people, clock):
    reg = people
    assert lib.available(reg), "the forever profile has the guide tool"
    v = up(reg)
    assert v.id == "horde-20261109-230012" and v.faction == "horde" and v.guides == 3 and v.sections == 2
    assert v.by_id == AUTHOR and v.by_name == "Quill" and v.size == sum(len(t) for t in TEXTS) and v.withdrawn_at is None
    out = lib.issue(reg, v.id, READER_TAG)
    assert decode(out, READER_TAG) == TEXTS
    with pytest.raises(GuideError):
        decode(out, AUTHOR_TAG)
    assert out.count("%") == 2, "the issued string keeps the upload's section count"
    assert lib.get(reg, v.id) == v
    assert lib.latest(reg, "horde").id == v.id and lib.latest(reg, "alliance") is None
    # on disk: the index and one text file per guide, in order
    d = reg.store.root / reg.key / "guides"
    assert (d / "index.yaml").exists()
    assert [(d / v.id / f"guide-{i:03d}.txt").read_text() for i in (1, 2, 3)] == TEXTS
    assert lib.issue_filename(v, READER_TAG) == "GuildGuide-Horde-20261109-Marrowfen.txt"


def test_an_upload_is_one_commit_naming_the_version_and_the_uploader(people, clock):
    reg = people
    root = reg.store.root
    before = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
    v = up(reg)
    after = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert int(after) == int(before) + 1
    head = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
    assert head == f"{reg.key}: guild guide {v.id} uploaded by Quill"


def test_refusals_are_in_the_tools_words(people, clock):
    reg = people
    cases = [
        (lambda: up(reg, text=""), EMPTY_TEXT),
        (lambda: up(reg, text="1|" + "x" * g.TEXT_MAX), g.TOO_BIG),
        (lambda: up(reg, tag="Wrongname#11111", text=string()), "doesn't unlock"),
        (lambda: up(reg, tag="", text=string()), "Enter the BattleTag"),
        (lambda: up(reg, tag="Quill", text=string()), "name, a # and 4 to 7 digits"),
        (lambda: up(reg, faction="goblin"), lib.BAD_FACTION),
        (lambda: lib.issue(reg, "horde-20990101-000000", READER_TAG), lib.NO_VERSION),
        (lambda: lib.issue(reg, "../../etc", READER_TAG), lib.NO_VERSION),
    ]
    for fn, words in cases:
        with pytest.raises(GuideError) as e:
            fn()
        assert words in str(e.value)
    assert lib.versions(reg) == [], "nothing was written by a refusal"
    v = up(reg)
    with pytest.raises(GuideError, match="name, a # and 4"):
        lib.issue(reg, v.id, "nobody")


def test_both_slots_taken_is_busy_and_never_a_queue(people, clock):
    reg = people
    v = up(reg)
    assert SLOTS.acquire(blocking=False) and SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(GuideError) as e:
            lib.issue(reg, v.id, READER_TAG)
        assert str(e.value) == lib.BUSY
        with pytest.raises(GuideError) as e:
            up(reg, faction="alliance")
        assert str(e.value) == lib.BUSY
    finally:
        SLOTS.release()
        SLOTS.release()


def test_five_uploads_a_day_per_member_on_the_guilds_day(people, clock):
    reg = people
    # 23:00 UTC on 9 Nov is 3 PM on Mon 9 Nov in Los Angeles: the guild's day
    for i in range(lib.UPLOADS_PER_DAY):
        clock[0] += timedelta(minutes=1)
        up(reg)
    clock[0] += timedelta(minutes=1)
    with pytest.raises(GuideError) as e:
        up(reg)
    assert str(e.value) == lib.DAILY_CAP
    clock[0] += timedelta(minutes=1)
    up(reg, uid=READER, name="Marrow", tag=READER_TAG, text=string(READER_TAG))  # the cap is per member
    # 07:30 UTC on 10 Nov is still 9 Nov in Los Angeles; 08:30 UTC is 10 Nov there
    clock[0] = datetime(2026, 11, 10, 7, 30, tzinfo=timezone.utc)
    with pytest.raises(GuideError):
        up(reg)
    clock[0] = datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc)
    up(reg)


def test_two_uploads_in_the_same_second_get_a_second_id(people, clock):
    reg = people
    a, b, c = up(reg), up(reg), up(reg)
    assert (a.id, b.id, c.id) == ("horde-20261109-230012", "horde-20261109-230012-2", "horde-20261109-230012-3")
    assert lib.issue(reg, b.id, READER_TAG) and lib.latest(reg, "horde").id == c.id
    other = up(reg, faction="both")
    assert other.id == "both-20261109-230012", "the faction is part of the id"


# ---------------------------------------------------------------- withdraw / restore

def test_latest_skips_a_withdrawn_version_and_a_member_cannot_get_it(people, clock):
    reg = people
    old = up(reg)
    clock[0] += timedelta(days=1)
    new = up(reg)
    assert [v.id for v in lib.versions(reg, "horde")] == [new.id, old.id], "newest first"
    lib.withdraw(reg, new.id, "Offi", OFFICER)
    assert lib.latest(reg, "horde").id == old.id
    assert [v.id for v in lib.versions(reg, "horde", live_only=True)] == [old.id]
    with pytest.raises(GuideError) as e:
        lib.issue(reg, new.id, READER_TAG)
    assert str(e.value) == lib.WITHDRAWN
    assert decode(lib.issue(reg, new.id, READER_TAG, officer=True), READER_TAG) == TEXTS, "an officer may still get it"
    lib.withdraw(reg, old.id, "Offi", OFFICER)
    assert lib.latest(reg, "horde") is None
    lib.restore(reg, new.id, "Offi", OFFICER)
    assert lib.latest(reg, "horde").id == new.id and lib.get(reg, new.id).withdrawn_at is None


def test_withdraw_and_restore_refuse_twice_and_notice_the_uploader(people, clock):
    reg = people
    v = up(reg)
    w = lib.withdraw(reg, v.id, "Offi", OFFICER)
    assert w.withdrawn_by == "Offi" and w.withdrawn_by_id == OFFICER and w.withdrawn_at
    notes = reg.notices(AUTHOR)
    assert len(notes) == 1 and notes[0]["kind"] == "guide"
    assert notes[0]["text"] == "Your Horde guide of Mon 9 Nov 3:00 PM was withdrawn by Offi."
    with pytest.raises(GuideError, match="Already withdrawn by Offi"):
        lib.withdraw(reg, v.id, "Offi", OFFICER)
    lib.restore(reg, v.id, "Offi", OFFICER)
    with pytest.raises(GuideError) as e:
        lib.restore(reg, v.id, "Offi", OFFICER)
    assert str(e.value) == lib.NOT_WITHDRAWN
    with pytest.raises(GuideError) as e:
        lib.withdraw(reg, "horde-20990101-000000", "Offi", OFFICER)
    assert str(e.value) == lib.NO_VERSION
    assert len(reg.notices(AUTHOR)) == 1, "a restore is no notice"


def test_an_officer_withdrawing_their_own_upload_gets_no_notice(people, clock):
    reg = people
    v = up(reg, uid=OFFICER, name="Offi")
    lib.withdraw(reg, v.id, "Offi", OFFICER)
    assert reg.notices(OFFICER) == []


def test_rows_hide_withdrawn_versions_from_members(people, clock):
    reg = people
    old = up(reg)
    clock[0] += timedelta(hours=2)
    new = up(reg)
    al = up(reg, faction="alliance")
    lib.withdraw(reg, new.id, "Offi", OFFICER)
    member = lib.rows(reg, officer=False)
    assert [r["id"] for r in member] == [al.id, old.id]
    by = {r["id"]: r for r in member}
    assert by[old.id] == {"id": old.id, "faction": "horde", "faction_words": "Horde", "when": "Mon 9 Nov 3:00 PM", "by": "Quill",
                          "guides": 3, "latest": True, "withdrawn": False}
    officer = {r["id"]: r for r in lib.rows(reg, officer=True)}
    assert officer[new.id]["withdrawn"] is True and officer[new.id]["withdrawn_by"] == "Offi" and officer[new.id]["latest"] is False
    assert officer[new.id]["withdrawn_when"] == "Mon 9 Nov 5:00 PM"
    assert lib.FACTIONS == ("alliance", "horde", "both") and lib.FACTION_WORDS["both"] == "Both factions"


def test_the_sentences(people, clock):
    reg = people
    assert lib.upload_consequence(reg, "horde") == "This becomes the latest Horde guide for Demo Guild. Nobody gets a DM. An officer can withdraw it."
    old = up(reg)
    clock[0] += timedelta(hours=1)
    new = up(reg)
    s = lib.withdraw_consequence(reg, new, by_id=OFFICER)
    assert s.startswith("The Horde guide of Mon 9 Nov 4:00 PM is withdrawn and Quill gets a notice.")
    assert "Members get the one from Mon 9 Nov 3:00 PM as the latest." in s and s.endswith("You can restore it.")
    lib.withdraw(reg, new.id, "Offi", OFFICER)
    assert lib.restore_consequence(reg, lib.get(reg, new.id)) == "The Horde guide of Mon 9 Nov 4:00 PM is back. It becomes the latest again."
    lib.withdraw(reg, old.id, "Offi", OFFICER)
    assert "No Horde guide is left for members." in lib.withdraw_consequence(reg, lib.get(reg, old.id))
    assert lib.restore_consequence(reg, lib.get(reg, old.id)).endswith("It becomes the latest again."), "nothing else is live"
    lib.restore(reg, new.id, "Offi", OFFICER)
    assert lib.withdraw_consequence(reg, lib.get(reg, old.id)).endswith("is withdrawn and Quill gets a notice. "
                                                                      "The latest stays the one from Mon 9 Nov 4:00 PM. You can restore it.")
    assert lib.restore_consequence(reg, lib.get(reg, old.id)) == "The Horde guide of Mon 9 Nov 3:00 PM is back."


# ---------------------------------------------------------------- D7: no tag, no string, anywhere

def test_no_battletag_or_string_reaches_the_repo_or_a_log(people, clock, caplog):
    reg = people
    caplog.set_level(logging.DEBUG)
    s = string()
    v = up(reg, text=s)
    lib.issue(reg, v.id, READER_TAG)
    for bad in [lambda: up(reg, tag="Wrongname#11111", text=s), lambda: lib.issue(reg, v.id, "x#1")]:
        with pytest.raises(GuideError) as e:
            bad()
        assert "Wrongname" not in str(e.value) and "x#1" not in str(e.value)
    lib.withdraw(reg, v.id, "Offi", OFFICER)
    lib.restore(reg, v.id, "Offi", OFFICER)
    root = reg.store.root
    secrets = [AUTHOR_TAG, READER_TAG, AUTHOR_TAG.split("#")[0], READER_TAG.split("#")[0], "Wrongname", s[:40]]
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            text = p.read_text(errors="replace")
            for t in secrets:
                assert t not in text, f"{t!r} in {p}"
    log = subprocess.run(["git", "-C", str(root), "log", "--all", "-p"], capture_output=True, text=True).stdout
    for t in secrets:
        assert t not in log, f"{t!r} in git log"
        assert t not in caplog.text
