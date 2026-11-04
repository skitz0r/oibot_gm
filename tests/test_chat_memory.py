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


# ---- the plain op

@pytest.mark.parametrize("target,value,want", [
    (f"remember <#{GENERAL_CH}>", "shared", "shared"),
    (f"remember what's said in <#{GENERAL_CH}>", "on", "shared"),
    (f"remember in <#{GENERAL_CH}>", "anywhere", "shared"),
    (f"remember <#{GENERAL_CH}>", "here only", "channel"),
    (f"memory in <#{GENERAL_CH}>", "channel", "channel"),
    (f"remember <#{GENERAL_CH}>", "off", None),
    (f"stop remembering <#{GENERAL_CH}>", "", None),
    (f"forget <#{GENERAL_CH}>", "", None),
])
def test_remember_patch_text_forms(g, target, value, want):
    patch, line = configops._plain_patch(g.reg, ConfigOp(op="plain", target=target, value=value))
    assert patch == {"set_remember": {str(GENERAL_CH): want}}
    assert line.startswith(f"memory in <#{GENERAL_CH}>: off → {configops.memory_word(want)}")


def test_remember_patch_refuses_an_unknown_mode(g):
    with pytest.raises(RegistryError, match="shared, channel or off"):
        configops._plain_patch(g.reg, ConfigOp(op="plain", target=f"remember <#{GENERAL_CH}>", value="forever"))


def test_describe_words_the_key_note_and_the_dry_run_refusal(g, monkeypatch):
    from oibot_gm.llm import decisions

    op = ConfigOp(op="plain", target=f"remember <#{GENERAL_CH}>", value="shared")
    monkeypatch.setattr(decisions, "DECISIONS", None)
    monkeypatch.delenv(decisions.KEY_ENV, raising=False)
    assert configops.describe(g.reg, op) == f"memory in <#{GENERAL_CH}>: off → remembers — the host hasn't set the TypeSafe key, so nothing is remembered yet"
    monkeypatch.setenv(decisions.KEY_ENV, "x")
    assert configops.describe(g.reg, op) == f"memory in <#{GENERAL_CH}>: off → remembers"
    d = configops.describe_ops(g.reg, [op])
    assert d.lines[0].error and "isn't a listening channel" in d.lines[0].error, "the shadow dry run shows the verb's refusal"
    plain(g.reg, set_listen={str(GENERAL_CH): "reply"})
    d = configops.describe_ops(g.reg, [op])
    part = d.lines[0].parts[0]
    assert not d.lines[0].error and d.lines[0].label == "Plain-text permissions" and part.label == f"Memory in <#{GENERAL_CH}>" and (part.before, part.after) == ("off", "remembers")


def test_off_is_a_warn_op_with_the_count_in_its_sentence(g, monkeypatch):
    reg = g.reg
    plain(reg, set_listen={str(GENERAL_CH): "reply"}, set_remember={str(GENERAL_CH): "shared"})
    off = ConfigOp(op="plain", target=f"remember <#{GENERAL_CH}>", value="off")
    assert configops.is_warn(reg, off)
    assert not configops.is_warn(reg, ConfigOp(op="plain", target=f"remember <#{GENERAL_CH}>", value="channel"))
    assert configops.op_consequences(reg, off) == [f"The bot remembers nothing from <#{GENERAL_CH}> yet, so nothing is forgotten."]
    monkeypatch.setattr(mem, "MEMORIES", types.SimpleNamespace(count=lambda r, channel=None: 212 if channel == GENERAL_CH else 0))
    assert configops.op_consequences(reg, off) == [f"The bot forgets the 212 things it remembers from <#{GENERAL_CH}>."]
    listen_off = ConfigOp(op="plain", target=f"listen <#{GENERAL_CH}>", value="off")
    assert configops.is_warn(reg, listen_off) and configops.op_consequences(reg, listen_off) == [f"The bot forgets the 212 things it remembers from <#{GENERAL_CH}>."]
    assert not configops.is_warn(reg, ConfigOp(op="plain", target=f"listen <#{SIGNUP_CH}>", value="off")), "a channel that never remembered"
    d = configops.describe_ops(reg, [off])
    assert d.warn and d.consequences == [f"The bot forgets the 212 things it remembers from <#{GENERAL_CH}>."]


def test_structured_fields_and_the_merged_patch(g):
    reg = g.reg
    plain(reg, set_listen={str(GENERAL_CH): "reply", str(OFFICER_CH): "reply"})
    ops = configops.ops_from_fields(reg, "plain", None, {"remember": {str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"}})
    assert [(o.target, o.value) for o in ops] == [(f"remember <#{GENERAL_CH}>", "shared"), (f"remember <#{OFFICER_CH}>", "channel")]
    merged = configops.coalesce(ops)
    assert len(merged) == 1
    reg.set_plain_policy(reg.merge_plain_policy(configops._plain_merged(reg, merged[0].parts)), "owner")
    assert reg.memory_channels() == {str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"}
    ops = configops.ops_from_fields(reg, "plain", None, {"remember": {str(OFFICER_CH): "channel"}})
    assert [(o.target, o.value) for o in ops] == [(f"remember <#{GENERAL_CH}>", "off")]
    ops = configops.ops_from_fields(reg, "plain", None, {"set_remember": {str(OFFICER_CH): None}})
    assert [(o.target, o.value) for o in ops] == [(f"remember <#{OFFICER_CH}>", "off")]
    ops = configops.ops_from_fields(reg, "plain", None, {"listen": {}, "remember": {}})
    assert [(o.target, o.value) for o in ops] == [(f"listen <#{GENERAL_CH}>", "off"), (f"listen <#{OFFICER_CH}>", "off"), (f"remember <#{GENERAL_CH}>", "off"), (f"remember <#{OFFICER_CH}>", "off")]
    with pytest.raises(RegistryError, match="remember, set_remember"):
        configops.ops_from_fields(reg, "plain", None, {"recall": {}})


def test_summary_policy_text_schema_and_the_read_view(g):
    reg = g.reg
    sec = next(s for s in configops.config_summary(reg) if s.key == "plain")
    assert "Memory: off in every channel — the owner turns it on per listening channel on the Ops page." in sec.lines
    plain(reg, set_listen={str(GENERAL_CH): "reply", str(OFFICER_CH): "reply"}, set_remember={str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"})
    sec = next(s for s in configops.config_summary(reg) if s.key == "plain")
    assert f"Memory: <#{GENERAL_CH}> remembers, <#{OFFICER_CH}> remembers here only" in sec.lines
    text = configops.plain_text_policy(reg)
    assert f"- memory in <#{GENERAL_CH}>: shared" in text and f"- memory in <#{OFFICER_CH}>: channel" in text
    assert "remember <#channel>" in ConfigOp.model_fields["target"].description and "target=remember <channel mention>" in configops.SCHEMA_TEXT
    assert len(ConfigOp.model_fields) <= 13

    from oibot_gm.discord_pool import SetupMixin, plain_read_text

    class C(SetupMixin):
        def get_guild(self, gid):
            return None

        def guild_channels(self, r):
            return [{"id": str(OPS_CH), "name": "ops"}, {"id": str(GENERAL_CH), "name": "general"}, {"id": str(OFFICER_CH), "name": "officer-chat"}]

    table = C().plain_table(reg)
    assert table["remember"] == [{"id": str(GENERAL_CH), "name": "general", "mode": "shared"}, {"id": str(OFFICER_CH), "name": "officer-chat", "mode": "channel"}]
    text = plain_read_text(reg, table, OPS_CH)
    assert "• #general: Replies — listening, remembers" in text and "• #officer-chat: Replies — listening, remembers here only" in text
    assert plain_read_text(reg, table, GENERAL_CH).split("\n", 1)[0].endswith("— listening (Replies), remembers")
    assert plain_read_text(reg, table, OFFICER_CH).split("\n", 1)[0].endswith("— listening (Replies), remembers here only")


def test_host_facts_name_the_memory_model_never_a_path(g, monkeypatch):
    monkeypatch.setattr(mem, "MEMORIES", None)
    assert "Memory model: not loaded yet (the bot downloads it once; until then it remembers words without the meaning search)" in configops.host_facts(g.reg)
    monkeypatch.setattr(mem, "MEMORIES", types.SimpleNamespace(model_state=lambda: "loaded"))
    assert "Memory model: loaded" in configops.host_facts(g.reg)
