"""The model being down is a sentence to the asker and an ops error — never silence (dual interfaces §2.7).

Seen live on 2026-10-24: an @mention in the ops channel with the API key rejected (401) raised out of handle_change,
discord.py logged "Ignoring exception in on_message" and the officer got nothing back."""
from __future__ import annotations

import asyncio
import types

import discord
from conftest import OWNER

from oibot_gm import configops, discord_policy

OPS_CH = 910_001


class ModelDown(Exception):
    """Stands in for the SDK's own exceptions (anthropic.AuthenticationError, APIConnectionError, …): what makes one a
    model error is its module, not its class — configops.is_model_error reads that."""


ModelDown.__module__ = "anthropic"


class RaisingProvider:
    def complete(self, workload, system, user, schema):
        raise ModelDown("Error code: 401 - invalid x-api-key")


class Msg(discord.Message):
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Feed:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, line, **kw):
        self.lines.append((level, line))


def test_user_error_names_a_model_failure_without_the_sdk_details():
    assert configops.user_error(ModelDown("Error code: 401 - invalid x-api-key")) == configops.MODEL_ERROR
    assert "401" not in configops.MODEL_ERROR and "key" not in configops.MODEL_ERROR
    assert configops.user_error(RuntimeError("boom")) == configops.GENERIC_ERROR  # anything else stays generic


def test_a_plain_text_parse_that_cannot_reach_the_model_replies_and_tells_the_owner(reg):
    replies, ops = [], Feed()

    async def plain_identity(reg_, user, **_):
        return True, []

    async def reply(content=None, **kw):
        replies.append(content)

    bot = types.SimpleNamespace(plain_identity=plain_identity, help_answer=None, raids=None, reply_private=lambda r, ch: True)
    msg = Msg(author=types.SimpleNamespace(id=OWNER, display_name="Owner"), guild=object(), channel=object(), reply=reply)
    asyncio.run(discord_policy.handle_change(msg, reg, None, RaisingProvider(), ops, "moonkin aura is raid wide now", bot=bot, channel=OPS_CH))
    assert replies == [configops.MODEL_ERROR]  # the asker reads a sentence, not silence
    assert ops.lines and ops.lines[0][0] == "error" and "plain-text parse" in ops.lines[0][1]  # the owner gets the details (an ops error = a throttled DM)
