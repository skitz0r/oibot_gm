"""Chat memory, live (design §5.61): the per-channel Memory setting beside Listening's, the plain op that sets it, the
gate that captures and recalls in ONE decision request, the answers that cite memories as hearsay, and the surfaces.
No network, no Discord, no model: fake decisions, a fake embedder."""
from __future__ import annotations

import asyncio
import types

import pytest

from oibot_gm import configops
from oibot_gm import memory as mem
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import MEMORY_MODES, MEMORY_WORDS, RegistryError

OPS_CH, SIGNUP_CH, GENERAL_CH, OFFICER_CH = 920_001, 920_002, 920_003, 920_004


@pytest.fixture
def g(reg):
    reg.config.ops_channel_id, reg.config.signup_channel_id = OPS_CH, SIGNUP_CH
    reg.save_config("channels (test)")
    me, other = reg.test_members()[:2]
    return types.SimpleNamespace(reg=reg, me=me, other=other)


def plain(reg, **patch):
    return reg.set_plain_policy(reg.merge_plain_policy(patch), "owner")


# ---- the setting

def test_constants_and_default_is_off(g):
    assert MEMORY_MODES == ("shared", "channel") and MEMORY_WORDS == {"shared": "Remembers", "channel": "Remembers here only"}
    assert g.reg.memory_channels() == {} and g.reg.memory_mode(GENERAL_CH) is None and g.reg.memory_mode(None) is None


def test_memory_needs_listening_and_a_known_mode(g):
    reg = g.reg
    with pytest.raises(RegistryError, match="isn't a listening channel"):
        plain(reg, set_remember={str(GENERAL_CH): "shared"})
    plain(reg, set_listen={str(GENERAL_CH): "watch"})
    with pytest.raises(RegistryError, match="shared or channel"):
        plain(reg, set_remember={str(GENERAL_CH): "forever"})
    with pytest.raises(RegistryError, match="by channel id"):
        plain(reg, remember={"dm": "shared"})
    line = plain(reg, set_remember={str(GENERAL_CH): "shared"})
    assert "memory 1 changed" in line and reg.memory_mode(GENERAL_CH) == "shared" and reg.memory_channels() == {str(GENERAL_CH): "shared"}
    assert reg.memory_mode(str(GENERAL_CH)) == "shared"


def test_listening_off_drops_memory_in_the_same_patch_only(g):
    reg = g.reg
    plain(reg, set_listen={str(GENERAL_CH): "reply", str(OFFICER_CH): "reply"}, set_remember={str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"})
    m = reg.merge_plain_policy({"set_listen": {str(GENERAL_CH): None}})
    assert m["listen"] == {str(OFFICER_CH): "reply"} and m["remember"] == {str(OFFICER_CH): "channel"}, "the channel the patch stops listening in forgets its setting"
    whole = reg.merge_plain_policy({"listen": {str(OFFICER_CH): "reply"}, "remember": {str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"}})
    assert whole["remember"] == {str(OFFICER_CH): "channel"}, "the Ops page sends both maps: the lost channel drops silently"
    assert reg.merge_plain_policy({"dm": "answer"})["remember"] == {str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"}
    rev = reg.config_rev("plain")
    line = plain(reg, set_listen={str(GENERAL_CH): None})
    assert "listening 1 changed" in line and "memory 1 changed" in line and reg.memory_channels() == {str(OFFICER_CH): "channel"} and reg.config_rev("plain") != rev
    plain(reg, set_remember={str(OFFICER_CH): "off"})
    assert reg.memory_channels() == {} and reg.listen_mode(OFFICER_CH) == "reply"
