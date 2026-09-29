"""Parity B1, batch 4: deciding an application in plain text. The `application` op resolves the applicant against the
OPEN applications (they aren't members, so `_member` can't), describes the decision, and applies only through the
bot's decide_application — the card's buttons, /roster applicant, the web and MCP path — so the applicant is DMed
(with the note) and the card updates. Offline it needs the bot. Officers only by default. No Discord, no LLM."""
from __future__ import annotations

import asyncio
import types

import pytest
from conftest import OWNER

from oibot_gm import configops
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_registry import settle_application
from oibot_gm.registry import PLAIN_WEB, RegistryError

NYX = 7_070_000_001


@pytest.fixture
def app(reg):
    return reg.apply(NYX, "nyxie", "Nyx", "Priest", "Holy", None, None, None, None, surname="Starfall")


class Bot:
    """decide_application is the real settle_application on a client whose DMs and ops lines are recorded."""

    def __init__(self):
        self.dms, self.lines = [], []
        self.ops = types.SimpleNamespace(emit=self._emit)

    async def _emit(self, cfg, level, text, **kw):
        self.lines.append(text)

    async def fetch_user(self, uid):
        async def send(text):
            self.dms.append((uid, text))
        return types.SimpleNamespace(send=send)

    def get_channel(self, cid):
        return None

    async def decide_application(self, reg, discord_id, accept, by, note):
        return await settle_application(self, reg, discord_id, accept, by, note)


def test_applicant_resolution(reg, app):
    for ref in ("nyxie", "Nyx Starfall", "nyx", f"<@{NYX}>", str(NYX)):
        assert configops._applicant(reg, ref).discord_id == NYX, ref
    with pytest.raises(RegistryError, match=r"no open application from Bob \(open: nyxie \(Nyx Starfall\)\)"):
        configops._applicant(reg, "Bob")
    with pytest.raises(RegistryError, match="say whose application"):
        configops._applicant(reg, "")


def test_describe_and_offline_refusal(reg, app):
    op = ConfigOp(op="application", member="nyxie", value="accept", reason="Welcome aboard")
    assert configops.describe(reg, op) == ("application from nyxie (Nyx Starfall, Priest Holy): open → accepted — registers Nyx Starfall as their main, "
                                           "rank trial; note to them: “Welcome aboard”; they get a DM and the review card updates")
    assert "open → declined" in configops.describe(reg, ConfigOp(op="application", member="Nyx Starfall", value="decline"))
    assert "accept | decline" in configops.describe(reg, ConfigOp(op="application", member="nyxie", value="maybe"))
    with pytest.raises(RegistryError, match="needs the bot"):
        configops.apply(reg, op, "Officer", is_owner=False)
    assert reg.applicants[NYX].status == "open"


def test_accept_goes_through_the_bots_decide_path(reg, app):
    bot = Bot()
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="application", member="nyxie", value="accept", reason="Welcome aboard"), "Officer", False, bot=bot))
    assert line == "✅ Accepted nyxie as Nyx Starfall (trial, confirmed)"
    assert reg.applicants[NYX].status == "accepted" and reg.members[NYX].main.label == "Nyx Starfall"
    assert bot.dms and bot.dms[0][0] == NYX and "Welcome aboard" in bot.dms[0][1]
    assert bot.lines and "Accepted nyxie" in bot.lines[-1]
    with pytest.raises(RegistryError, match="no open application"):
        asyncio.run(configops.apply_async(reg, ConfigOp(op="application", member="nyxie", value="accept"), "Officer", False, bot=bot))


def test_decline_sends_the_note_and_keeps_them_off_the_roster(reg, app):
    bot = Bot()
    line = asyncio.run(configops.apply_async(reg, ConfigOp(op="application", member=f"<@{NYX}>", value="decline", reason="Full on healers"), "Officer", False, bot=bot))
    assert line == "⛔ Declined nyxie (Nyx Starfall)"
    assert reg.applicants[NYX].status == "declined" and reg.applicants[NYX].decision_note == "Full on healers"
    assert "Full on healers" in bot.dms[0][1] and NYX not in reg.members


def test_only_officers_decide_by_default(reg, app):
    member = reg.test_members()[0].discord_id
    op = ConfigOp(op="application", member="nyxie", value="accept")
    allowed, refused = configops.authorize(reg, [op], member, officer=False, channel=PLAIN_WEB)
    assert not allowed and refused and "other people's records" in refused[0].lower()
    allowed, refused = configops.authorize(reg, [ConfigOp(op="application", member="nyxie", value="accept")], OWNER, officer=True, channel=PLAIN_WEB)
    assert allowed and not refused
    assert configops.OP_GROUP["application"] == "members" and "application" not in configops.SELF_OPS


def test_the_parser_sees_open_applications(reg, app):
    text = configops.current_config_text(reg)
    assert f"- nyxie<@{NYX}>: Nyx Starfall, Priest Holy" in text
    reg.decide(NYX, False, "t")
    assert "## Open applications (applicant<@id>: character, class spec)\n(none)" in configops.current_config_text(reg)
