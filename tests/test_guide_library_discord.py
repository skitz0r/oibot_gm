"""/guide list|get|upload|withdraw|restore (design §5.63): the guild
guide library in Discord, driven end to end through tests/wizard_harness.py — every screen and modal linted. A get DMs a
file that unlocks with the typed BattleTag (or attaches it to the private reply when the DM is refused); an upload reads
the attachment only after the membership, type and size checks and prints exactly library.upload_consequence before the
write; withdraw / restore are officers' with the rights re-checked on every press. D7: no BattleTag and no import string
in a log record, an ops line, the open-screen mirror or the data repo."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import types
from datetime import datetime, timezone

import discord
import pytest

from oibot_gm import flows_guides, pending_views, wizard_flows  # noqa: F401 — registers every flow
from oibot_gm import guides as g
from oibot_gm.guides import library as lib
from oibot_gm.guides.build import BAD_TAG
from oibot_gm.guides.rxp_decode import decode, encode
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

AUTHOR_TAG = "Quillwright#48213"
READER_TAG = "Marrowfen#77104"
AUTHOR, READER, OFFICER = 480_000_000_000_000_001, 480_000_000_000_000_002, 480_000_000_000_000_003
TEXTS = ["#name 1-6 Guildwood\n#group Our Route\nstep\n.goto 1429/0,-8949,-132\n.accept 783 >>Take it\n",
         "#name 6-12 Guildshore\n#group Our Route\nstep\n.hs Sentinel Hill\n"]
STRING = encode(list(TEXTS), AUTHOR_TAG, sections=2)


class FakeOps:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def emit(self, cfg, level, text, exc=None, **kw):
        self.lines.append((level, text))


class FakeUser:
    """A Discord user with a DM channel: `refuse` = the person's DMs are closed (discord.Forbidden)."""

    def __init__(self, uid: int, name: str, refuse: bool = False):
        self.id, self.display_name, self.mention, self.refuse = uid, name, f"<@{uid}>", refuse
        self.dms: list[dict] = []

    async def send(self, content=None, *, file=None, **kw):
        if self.refuse:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        self.dms.append({"text": content, "file": file})


class FakeAttachment:
    """A slash command's attachment: `size` is Discord's metadata; read() counts its calls (never before the checks)."""

    def __init__(self, data: str, filename: str = "our-route.txt", size: int | None = None):
        self.filename, self._data = filename, data.encode()
        self.size = len(self._data) if size is None else size
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self._data


@pytest.fixture
def clock(monkeypatch):
    t = [datetime(2026, 11, 9, 23, 0, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(lib, "_now", lambda: t[0])
    return t


@pytest.fixture
def people(reg, monkeypatch):
    reg.add_character(AUTHOR, "Quill", "Quillon", "Mage", "Fire", None, True)
    reg.add_character(READER, "Marrow", "Mossvale", "Priest", "Holy", None, True)
    reg.add_character(OFFICER, "Offi", "Ashgrove", "Warrior", "Protection", None, True)
    officers = {OFFICER}

    async def belongs(_interaction, _reg):
        return True

    async def is_officer(_bot, _reg, user):
        return user.id in officers

    monkeypatch.setattr(flows_guides, "belongs", belongs)
    monkeypatch.setattr(flows_guides, "is_officer", is_officer)
    reg._officers = officers  # the test's handle on who is an officer right now
    return reg


def harness(reg, uid, name, refuse=False):
    user = FakeUser(uid, name, refuse=refuse)
    client = types.SimpleNamespace(ops=FakeOps())
    return Harness(reg, None, user=user, client=client)


def options(h, placeholder):
    sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == placeholder)
    return [(o.value, o.label, o.description) for o in sel.options]


def stored(reg, faction="horde", uid=AUTHOR, name="Quill"):
    return lib.upload(reg, STRING, AUTHOR_TAG, faction, uid, name)


# ---------------------------------------------------------------- /guide get

def test_get_dms_a_file_that_unlocks_with_the_typed_battletag(people, clock, caplog):
    reg = people
    v = stored(reg)
    h = harness(reg, READER, "Marrow")
    caplog.set_level(logging.DEBUG)

    async def go():
        await FLOWS["guide_get"](h.command(), reg)
        assert [o[0] for o in options(h, "Pick the faction…")] == ["horde"]  # only lines with a live version
        await h.pick("Pick the faction…", "horde")
        opts = options(h, "Pick the version…")
        assert opts[0][0] == v.id and opts[0][1] == lib.when(reg, v) and "Latest" in opts[0][2]
        await h.pick("Pick the version…", v.id)
        assert h.modal is not None and h.modal.title == "Your BattleTag"
        assert json.dumps(pending_views._live).find("Marrowfen") < 0
        await h.submit(h.modal, tag=READER_TAG)
    asyncio.run(go())
    dm = h.user.dms[-1]
    assert dm["text"] == f"Your guild guide (Horde · {lib.when(reg, v)}), locked to **{READER_TAG}**: import the attached string with RestedXP."
    f = dm["file"]
    assert f.filename == lib.issue_filename(v, READER_TAG)
    assert decode(f.fp.read().decode(), READER_TAG) == TEXTS
    assert "Sent to your DMs" in h.text and h.screen["view"] is None and not h.screen["files"]
    assert h.client.ops.lines == [], "no ops line for a get"
    for rec in caplog.records:
        assert READER_TAG not in rec.getMessage() and "Marrowfen" not in rec.getMessage()


def test_get_names_the_guild_when_the_bot_serves_several(people, clock):
    reg = people
    v = stored(reg)
    reg.many_guilds = True
    h = harness(reg, READER, "Marrow")

    async def go():
        await FLOWS["guide_get"](h.command(), reg)
        await h.pick("Pick the faction…", "horde")
        await h.pick("Pick the version…", v.id)
        await h.submit(h.modal, tag=READER_TAG)
    asyncio.run(go())
    assert h.user.dms[-1]["text"].startswith(f"Your guild guide from {reg.config.name} (Horde · ")


def test_a_refused_dm_puts_the_file_on_the_private_reply(people, clock):
    reg = people
    v = stored(reg)
    h = harness(reg, READER, "Marrow", refuse=True)

    async def go():
        await FLOWS["guide_get"](h.command(), reg)
        await h.pick("Pick the faction…", "horde")
        await h.pick("Pick the version…", v.id)
        await h.submit(h.modal, tag=READER_TAG)
    asyncio.run(go())
    assert not h.user.dms
    files = h.screen["files"]
    assert len(files) == 1 and decode(files[0].fp.read().decode(), READER_TAG) == TEXTS
    assert "I can't DM you" in h.text and f"locked to **{READER_TAG}**" in h.text


def test_get_refuses_a_bad_battletag_and_lets_the_person_fix_it(people, clock):
    reg = people
    v = stored(reg)
    h = harness(reg, READER, "Marrow")

    async def go():
        await FLOWS["guide_get"](h.command(), reg)
        await h.pick("Pick the faction…", "horde")
        await h.pick("Pick the version…", v.id)
        await h.submit(h.modal, tag="not a tag")
        assert BAD_TAG in h.text and "Fix my BattleTag" in h.buttons()
        await h.press("Fix my BattleTag")
        await h.submit(h.modal, tag=READER_TAG)
    asyncio.run(go())
    assert h.user.dms and decode(h.user.dms[-1]["file"].fp.read().decode(), READER_TAG) == TEXTS


def test_get_with_an_empty_library_says_how_to_fill_it(people):
    h = harness(people, READER, "Marrow")
    asyncio.run(FLOWS["guide_get"](h.command(), people))
    assert h.text == flows_guides.EMPTY and "/guide upload" in h.text


def test_an_unexpected_failure_logs_the_type_alone(people, clock, monkeypatch, caplog):
    reg = people
    v = stored(reg)
    h = harness(reg, READER, "Marrow")

    def boom(*_a, **_k):
        raise RuntimeError(f"broke on {READER_TAG}")
    monkeypatch.setattr(lib, "issue", boom)
    caplog.set_level(logging.DEBUG)

    async def go():
        await FLOWS["guide_get"](h.command(), reg)
        await h.pick("Pick the faction…", "horde")
        await h.pick("Pick the version…", v.id)
        await h.submit(h.modal, tag=READER_TAG)
    asyncio.run(go())
    from oibot_gm.configops import GENERIC_ERROR
    assert GENERIC_ERROR in h.text
    text = "\n".join(r.getMessage() + (str(r.exc_info) if r.exc_info else "") for r in caplog.records)
    assert "RuntimeError" in text and READER_TAG not in text
    assert all(READER_TAG not in line for _lv, line in h.client.ops.lines)


# ---------------------------------------------------------------- /guide upload

def test_upload_prints_the_consequence_then_writes_one_version_and_one_ops_line(people, clock, caplog):
    reg = people
    h = harness(reg, AUTHOR, "Quill")
    att = FakeAttachment(STRING)
    caplog.set_level(logging.DEBUG)

    async def go():
        await FLOWS["guide_upload"](h.command(), reg, file=att, faction="horde")
        assert att.reads == 0, "nothing read before the BattleTag"
        assert h.modal.title == "Your BattleTag"
        await h.submit(h.modal, tag=AUTHOR_TAG)
        assert h.text.split("\n", 1)[1] == lib.upload_consequence(reg, "horde")
        assert h.buttons() == ["Upload", "Back"]
        assert AUTHOR_TAG not in json.dumps(pending_views._live) and STRING[:40] not in json.dumps(pending_views._live)
        await h.press("Upload")
    asyncio.run(go())
    v = lib.latest(reg, "horde")
    assert v is not None and v.by_id == AUTHOR and v.by_name == "Quill"
    assert h.text.endswith(f"✅ Uploaded: Horde · {lib.when(reg, v)}.")
    assert h.client.ops.lines == [("info", f"Quill: guild guide uploaded: Horde · {lib.when(reg, v)}")]
    # D7: the tag and the string nowhere on disk, in git or in a log record
    root = reg.store.root
    disk = "".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    gitlog = subprocess.run(["git", "-C", str(root), "log", "-p", "--all"], capture_output=True, text=True).stdout
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for hay in (disk, gitlog, logs):
        assert "Quillwright" not in hay and STRING[:40] not in hay


def test_upload_refuses_a_wrong_type_or_a_too_big_file_before_reading_it(people):
    reg = people
    for att, words in ((FakeAttachment(STRING, filename="guide.pdf"), flows_guides.NOT_TXT),
                       (FakeAttachment(STRING, size=g.TEXT_MAX + 1), g.TOO_BIG)):
        h = harness(reg, AUTHOR, "Quill")
        asyncio.run(FLOWS["guide_upload"](h.command(), reg, file=att, faction="horde"))
        assert h.text == words and att.reads == 0 and h.modal is None


def test_upload_from_someone_outside_the_guild_is_refused_before_reading(people, monkeypatch):
    reg = people

    async def outside(_i, _r):
        return False
    monkeypatch.setattr(flows_guides, "belongs", outside)
    att = FakeAttachment(STRING)
    h = harness(reg, AUTHOR, "Quill")
    asyncio.run(FLOWS["guide_upload"](h.command(), reg, file=att, faction="horde"))
    assert att.reads == 0 and h.modal is None and reg.config.name in h.text


def test_upload_with_a_battletag_that_does_not_unlock_offers_a_fix(people, clock):
    reg = people
    h = harness(reg, AUTHOR, "Quill")

    async def go():
        await FLOWS["guide_upload"](h.command(), reg, file=FakeAttachment(STRING), faction="both")
        await h.submit(h.modal, tag=READER_TAG)
        await h.press("Upload")
        assert "doesn't unlock" in h.text and "Fix my BattleTag" in h.buttons()
        await h.press("Fix my BattleTag")
        await h.submit(h.modal, tag=AUTHOR_TAG)
        assert lib.upload_consequence(reg, "both") in h.text
        await h.press("Upload")
    asyncio.run(go())
    assert lib.latest(reg, "both") is not None and "✅ Uploaded: Both factions · " in h.text


def test_no_tool_answers_every_command(people, monkeypatch):
    monkeypatch.setattr(lib, "available", lambda _r: False)
    for name, kw in (("guide_get", {}), ("guide_list", {}), ("guide_withdraw", {}), ("guide_restore", {}),
                     ("guide_upload", {"file": FakeAttachment(STRING), "faction": "horde"})):
        h = harness(people, OFFICER, "Offi")
        asyncio.run(FLOWS[name](h.command(), people, **kw))
        assert h.text == lib.NO_TOOL, name


# ---------------------------------------------------------------- /guide list

def test_list_shows_the_latest_and_four_older_per_faction_with_the_uploader_escaped(people, clock, monkeypatch):
    reg = people
    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    ids = []
    for i in range(6):
        clock[0] = datetime(2026, 11, 3 + i, 20, 0, 0, tzinfo=timezone.utc)
        ids.append(stored(reg, name="Q_uill*" if i == 5 else "Quill").id)
    h = harness(reg, READER, "Marrow")
    asyncio.run(FLOWS["guide_list"](h.command(), reg))
    t = h.text
    assert "**Horde**" in t and "Alliance" not in t
    assert t.count("\n• ") == 5, "the latest and four older"
    assert "Q\\_uill\\*" in t and "Latest" in t
    assert "https://gm.example/app/me?g=" in t and "tab=guide" in t
    assert "/guide get" in t


def test_list_with_an_empty_library_says_how_to_fill_it(people):
    h = harness(people, READER, "Marrow")
    asyncio.run(FLOWS["guide_list"](h.command(), people))
    assert flows_guides.EMPTY in h.text


# ---------------------------------------------------------------- /guide withdraw · restore

def test_withdraw_is_refused_to_a_member(people, clock):
    reg = people
    stored(reg)
    h = harness(reg, READER, "Marrow")
    asyncio.run(FLOWS["guide_withdraw"](h.command(), reg))
    assert h.text == "Officers only." and h.screen["view"] is None


def test_withdraw_prints_the_consequence_writes_and_says_so_in_the_ops_channel(people, clock):
    reg = people
    v = stored(reg)
    h = harness(reg, OFFICER, "Offi")

    async def go():
        await FLOWS["guide_withdraw"](h.command(), reg)
        await h.pick("Pick the version…", v.id)
        assert h.text.split("\n", 1)[1] == lib.withdraw_consequence(reg, v, by_id=OFFICER)
        await h.press("Withdraw")
    asyncio.run(go())
    assert lib.get(reg, v.id).withdrawn_by == "Offi" and lib.latest(reg, "horde") is None
    assert h.text.endswith(f"✅ Withdrawn: Horde · {lib.when(reg, v)}.")
    assert h.client.ops.lines == [("info", f"Offi: guild guide withdrawn: Horde · {lib.when(reg, v)}")]

    h2 = harness(reg, OFFICER, "Offi")

    async def back():
        await FLOWS["guide_restore"](h2.command(), reg)
        await h2.pick("Pick the version…", v.id)
        assert h2.text.split("\n", 1)[1] == lib.restore_consequence(reg, lib.get(reg, v.id))
        await h2.press("Restore")
    asyncio.run(back())
    assert lib.latest(reg, "horde").id == v.id
    assert h2.client.ops.lines == [("info", f"Offi: guild guide restored: Horde · {lib.when(reg, v)}")]


def test_rights_lost_between_open_and_press_stop_the_write(people, clock):
    reg = people
    v = stored(reg)
    h = harness(reg, OFFICER, "Offi")

    async def go():
        await FLOWS["guide_withdraw"](h.command(), reg)
        await h.pick("Pick the version…", v.id)
        reg._officers.discard(OFFICER)  # demoted while the screen is open
        view = h.screen["view"]
        assert not await view.interaction_check(h._on_screen())
    asyncio.run(go())
    assert h.text == "Officers only."
    assert lib.get(reg, v.id).withdrawn_at is None and h.client.ops.lines == []


def test_restore_with_nothing_withdrawn_says_how(people, clock):
    reg = people
    stored(reg)
    h = harness(reg, OFFICER, "Offi")
    asyncio.run(FLOWS["guide_restore"](h.command(), reg))
    assert h.text == flows_guides.NOTHING_TO_RESTORE and h.screen["view"] is None


# ---------------------------------------------------------------- the commands

def test_the_guide_group_is_registered_with_short_descriptions(reg):
    from discord import app_commands

    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree, Guilds.of(reg.store, reg), Ops(client), lambda k, v: "")
    grp = tree.get_command("guide")
    assert grp is not None and sorted(c.name for c in grp.commands) == ["get", "list", "restore", "upload", "withdraw"]
    for c in [grp, *grp.commands]:
        assert 0 < len(c.description) <= 100
    up = next(c for c in grp.commands if c.name == "upload")
    assert [p.name for p in up.parameters] == ["file", "faction"]
    assert [ch.value for ch in up.parameters[1].choices] == list(lib.FACTIONS)
    from oibot_gm.help import gate_of
    assert gate_of("/guide get") == gate_of("/guide list") == gate_of("/guide upload") == "member"
    assert gate_of("/guide withdraw") == gate_of("/guide restore") == "officer"
