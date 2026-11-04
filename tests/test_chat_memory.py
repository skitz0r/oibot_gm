"""Chat memory, live (design §5.61): the per-channel Memory setting beside Listening's, the plain op that sets it, the
gate that captures and recalls in ONE decision request, the answers that cite memories as hearsay, and the surfaces.
No network, no Discord, no model: fake decisions, a fake embedder."""
from __future__ import annotations

import asyncio
import types

import pytest
from web_helpers import post_confirmed, web_client  # noqa: F401 — web_client is a shared fixture

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


# ---- the web and MCP (task 5): the map, the counts and the model's state; the same plain change

def test_api_memory_setting_reads_and_writes(web_client, g, monkeypatch):
    from oibot_gm.llm import decisions

    w = web_client
    monkeypatch.setattr(decisions, "DECISIONS", None)
    monkeypatch.delenv(decisions.KEY_ENV, raising=False)
    monkeypatch.setattr(mem, "MEMORIES", None)
    d = w.c.get("/api/plain-permissions", headers=w.as_(w.officer)).json()
    assert d["remember"] == {} and d["memory_modes"] == ["shared", "channel"] and d["memory_words"] == {"shared": "Remembers", "channel": "Remembers here only"}
    assert d["memory_counts"] == {} and d["memory_ready"] is False and d["memory_model"] == "off"
    owner = {"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"}
    r = post_confirmed(w.c, "/api/admin/plain-permissions", {"remember": {str(GENERAL_CH): "shared"}}, headers=owner)
    assert "isn't a listening channel" in r.text and g.reg.memory_mode(GENERAL_CH) is None, "refused by the verb, whatever the status"
    r = post_confirmed(w.c, "/api/admin/plain-permissions", {"listen": {str(GENERAL_CH): "watch"}, "remember": {str(GENERAL_CH): "shared"}}, headers=owner)
    assert r.status_code == 200, r.text
    assert g.reg.memory_mode(GENERAL_CH) == "shared" and r.json()["remember"] == {str(GENERAL_CH): "shared"}
    monkeypatch.setattr(mem, "MEMORIES", types.SimpleNamespace(counts=lambda reg: {str(GENERAL_CH): 3}, model_state=lambda: "loaded", count=lambda reg, channel=None: 3))
    monkeypatch.setenv(decisions.KEY_ENV, "x")
    d = w.c.get("/api/plain-permissions", headers=w.as_(w.officer)).json()
    assert d["memory_counts"] == {str(GENERAL_CH): 3} and d["memory_ready"] is True and d["memory_model"] == "loaded"
    r = w.c.post("/api/admin/plain-permissions", json={"set_remember": {str(GENERAL_CH): None}}, headers=owner)
    assert r.status_code == 200 and r.json().get("review_required"), "Off is reviewed first: it forgets"
    r = post_confirmed(w.c, "/api/admin/plain-permissions", {"set_remember": {str(GENERAL_CH): None}}, headers=owner)
    assert r.status_code == 200 and g.reg.memory_mode(GENERAL_CH) is None


def test_mcp_tools_carry_the_memory_setting():
    import json

    import httpx

    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.read()))
        if request.method == "GET":
            return httpx.Response(200, json={"groups": [], "channels": [], "listed": {}, "dm": "self", "default": "self", "guild_roles": [],
                                             "listen": {"1": "reply"}, "remember": {"1": "shared"}, "memory_ready": True, "memory_model": "loaded"})
        return httpx.Response(200, json={"applied": ["plain-text permissions: memory 1 changed"], "refused": []})

    ms.use(ms.Api(url="http://bot", token="a" * 64, transport=httpx.MockTransport(handler)))
    try:
        d = ms.plain_permissions()
        assert d["remember"] == {"1": "shared"} and d["memory_ready"] is True and d["memory_model"] == "loaded"
        assert ms.set_plain_permissions(set_remember={"1": "channel"}).startswith("plain-text permissions")
    finally:
        ms.use(None)
    assert json.loads(calls[1][2])["changes"] == [{"kind": "plain", "fields": {"set_remember": {"1": "channel"}}}]


# ---- the gate: capture and recall in one request

from oibot_gm import discord_bot as db  # noqa: E402

BOT_ID = 777


class Ask:
    """A fake decision service answering one `ask` with fixed probabilities; keeps every request."""

    available = True

    def __init__(self, p, remember=None, relevance=None):
        self.p, self.remember, self.relevance, self.requests = p, remember, relevance, []

    async def ask(self, state, questions, workload="listen"):
        self.requests.append((state, questions, workload))
        out = {}
        for name, q in questions.items():
            if q["type"] == "choice":
                if self.relevance is None:
                    continue
                probs = {o: 0.0 for o in q["criteria"]}
                probs.update(self.relevance)
                out[name] = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs}
            elif name == "remember":
                if self.remember is not None:
                    out[name] = {"type": "noul", "noul": self.remember}
            else:
                out[name] = {"type": "noul", "noul": self.p}
        return out

    async def noul(self, state, instructions, criteria=None, workload="listen"):
        a = await self.ask(state, {"decision": {"type": "noul", "instructions": instructions, "criteria": criteria}}, workload)
        return a["decision"]["noul"]

    async def choice(self, state, instructions, criteria, workload="listen"):
        a = await self.ask(state, {"decision": {"type": "choice", "instructions": instructions, "criteria": criteria}}, workload)
        return a.get("decision", {}).get("probabilities")


class Ops:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None, **kw):
        self.lines.append((level, text))


class Bot(db.ListenMixin):
    on_message = db.OibotGM.on_message
    _addressed = db.OibotGM._addressed

    def __init__(self, reg):
        self.reg = reg
        self.registries = types.SimpleNamespace(by_key={reg.key: reg}, for_guild=lambda gid: reg, ordered=lambda: [reg])
        self.ops = Ops()
        self.user = types.SimpleNamespace(id=BOT_ID)
        self.handled = []

    async def plain_text(self, message, reg, text, channel, memory=None):
        self.handled.append((message.id, text, channel, memory))

    async def news_message(self, message):
        return False

    def saw_member(self, gid, author):
        pass

    def event_for(self, channel_id):
        return None


_next_id = iter(range(7000, 9000))


def message(author, channel=GENERAL_CH, text="our only jc is Ysolde", *, bot=False, mentions=(), reply=None, name="general"):
    reacts = []

    async def add_reaction(emoji):
        reacts.append(emoji)

    async def remove_reaction(emoji, member):
        reacts.remove(emoji)

    ref = None if reply is None else types.SimpleNamespace(resolved=reply)
    return types.SimpleNamespace(id=next(_next_id), content=text, author=types.SimpleNamespace(id=author, display_name=f"user{author}", bot=bot),
                                 channel=types.SimpleNamespace(id=channel, name=name, topic="General chat", category=types.SimpleNamespace(name="Guild")),
                                 guild=types.SimpleNamespace(id=1, me=None), created_at=None,
                                 mentions=list(mentions), role_mentions=[], reference=ref, add_reaction=add_reaction, remove_reaction=remove_reaction, reacts=reacts)


@pytest.fixture
def gate(g, monkeypatch, tmp_path):
    from oibot_gm.llm import decisions

    monkeypatch.setattr(db, "LISTEN_LOG", tmp_path / "listen.jsonl")
    monkeypatch.delenv("OIBOT_LISTEN_SURE", raising=False)
    monkeypatch.delenv("OIBOT_REMEMBER_AT", raising=False)
    monkeypatch.setenv(decisions.KEY_ENV, "x")

    class Emb:
        ready = True

        def passages(self, texts):
            import numpy as np

            return np.stack([mem._unit(np.ones(mem.EMBED_DIMS, dtype=np.float32)) for _ in texts])

        def query(self, text):
            import numpy as np

            return mem._unit(np.ones(mem.EMBED_DIMS, dtype=np.float32))

    svc = mem.Memories(tmp_path / "out", embedder=Emb())
    monkeypatch.setattr(mem, "MEMORIES", svc)
    bot = Bot(g.reg)

    def run(p, msg, *, listen="reply", remember_mode=None, remember=None, relevance=None):
        patch = {"set_listen": {str(msg.channel.id): listen}}
        if remember_mode:
            patch["set_remember"] = {str(msg.channel.id): remember_mode}
        g.reg.set_plain_policy(g.reg.merge_plain_policy(patch), "owner")
        dec = Ask(p, remember=remember, relevance=relevance)
        monkeypatch.setattr(decisions, "DECISIONS", dec)
        asyncio.run(bot.on_message(msg))
        return dec

    return types.SimpleNamespace(bot=bot, run=run, g=g, svc=svc, reg=g.reg)


def test_a_remembering_channel_asks_two_questions_and_keeps_a_kept_message(gate):
    me = gate.g.me.discord_id
    msg = message(me)
    dec = gate.run(0.2, msg, remember_mode="shared", remember=0.9)
    state, questions, workload = dec.requests[0]
    assert set(questions) == {"decision", "remember"} and workload == "listen"
    assert state["memory"] == [] and "replying_to" not in state["message"]
    assert "`memory` lists things members said that the bot remembers" in db.LISTEN_QUESTION
    kept = gate.svc.list(gate.reg)[0]
    assert len(kept) == 1 and kept[0].text == msg.content and kept[0].channel == GENERAL_CH and kept[0].scope == "shared"
    assert kept[0].author == gate.g.me.display_name and kept[0].author_id == me and kept[0].p == 0.9 and kept[0].id == mem.memory_key(gate.reg.key, msg.id)
    row = gate.bot.listen_recent(gate.reg)[0]
    assert row["did"] == "quiet" and row["remembered"] is True
    assert gate.svc.rows(gate.reg)[-1]["kind"] == "memory"


def test_watches_remembers_too_off_never_and_under_the_line_is_not_kept(gate):
    me = gate.g.me.discord_id
    msg = message(me, text="the horde runs are on thursday")
    gate.run(0.2, msg, listen="watch", remember_mode="shared", remember=0.8)
    assert gate.svc.count(gate.reg) == 1
    msg2 = message(me, text="lol nice pull")
    dec = gate.run(0.1, msg2, listen="reply", remember_mode="shared", remember=0.3)
    assert gate.svc.count(gate.reg) == 1 and gate.bot.listen_recent(gate.reg)[0]["remembered"] is False
    msg3 = message(me, channel=SIGNUP_CH, text="i can make devilsaur gauntlets now", name="signups")
    dec = gate.run(0.2, msg3, listen="reply", remember=0.95)
    assert "remember" not in dec.requests[0][1] and gate.svc.count(gate.reg) == 1 and gate.bot.listen_recent(gate.reg)[0]["remembered"] is None


def test_the_hosts_line_raises_the_keep_line(gate, monkeypatch):
    monkeypatch.setenv("OIBOT_REMEMBER_AT", "0.8")
    gate.run(0.2, message(gate.g.me.discord_id), remember_mode="shared", remember=0.7)
    assert gate.svc.count(gate.reg) == 0


def test_a_reply_carries_what_it_answered_and_a_gone_reference_carries_nothing(gate):
    me, other = gate.g.me.discord_id, gate.g.other.discord_id
    asked = types.SimpleNamespace(content="anyone have a jewelcrafter?", author=types.SimpleNamespace(id=other, display_name="Rolf"))
    msg = message(me, text="yep, Ysolde", reply=asked)
    dec = gate.run(0.2, msg, remember_mode="shared", remember=0.9)
    state = dec.requests[0][0]
    assert state["message"]["replying_to"].endswith(": anyone have a jewelcrafter?")
    kept = gate.svc.list(gate.reg)[0][0]
    assert kept.context.endswith(": anyone have a jewelcrafter?") and kept.citation().endswith("(replying to " + kept.context + ")")
    gone = message(me, text="yep i do", reply=types.SimpleNamespace(content="", author=None))
    dec = gate.run(0.2, gone, remember_mode="shared", remember=0.9)
    assert "replying_to" not in dec.requests[0][0]["message"]
    # by its key: both rows share the same second, and the list's newest-first order does not break that tie
    assert gate.svc.store(gate.reg.key).get(gate.reg.key, mem.memory_key(gate.reg.key, gone.id)).context == ""


def test_recall_feeds_the_gate_and_the_judged_memories_reach_the_reply(gate):
    me = gate.g.me.discord_id
    gate.run(0.2, message(me, text="our only jc is Ysolde"), remember_mode="shared", remember=0.9)
    ask = message(me, text="who is our jc?")
    dec = gate.run(0.95, ask, remember_mode="shared", remember=0.1, relevance={"m1": 0.9})
    state, questions, _ = dec.requests[-1]
    assert set(questions) == {"decision", "remember", "relevance"}
    assert state["memory"][0]["id"] == "m1" and state["memory"][0]["text"] == "our only jc is Ysolde"
    assert questions["relevance"]["criteria"]["m1"].startswith(f"{gate.g.me.display_name} said on ")
    mid, text, channel, memory = gate.bot.handled[-1]
    assert mid == ask.id and [c.memory.text for c in memory] == ["our only jc is Ysolde"] and memory[0].judged == 0.9
    assert gate.svc.count(gate.reg) == 1, "a question is not a memory"


def test_a_channel_scoped_memory_is_recalled_only_in_its_channel(gate):
    me = gate.g.me.discord_id
    gate.run(0.2, message(me, channel=OFFICER_CH, text="we bench whoever is late twice", name="officer-chat"), remember_mode="channel", remember=0.9)
    dec = gate.run(0.95, message(me, channel=GENERAL_CH, text="what happens when someone is late?"), remember_mode="shared", remember=0.1, relevance={"m1": 0.9})
    assert dec.requests[-1][0]["memory"] == [] and "relevance" not in dec.requests[-1][1] and gate.bot.handled[-1][3] == []
    dec = gate.run(0.95, message(me, channel=OFFICER_CH, text="what happens when someone is late?", name="officer-chat"), remember_mode="channel", remember=0.1, relevance={"m1": 0.9})
    assert len(dec.requests[-1][0]["memory"]) == 1 and [c.memory.channel for c in gate.bot.handled[-1][3]] == [OFFICER_CH]


def test_an_unsure_ask_keeps_its_memories_for_the_click(gate):
    me = gate.g.me.discord_id
    gate.run(0.2, message(me, text="our only jc is Ysolde"), remember_mode="shared", remember=0.9)
    ask = message(me, text="jc?")
    gate.run(0.6, ask, remember_mode="shared", remember=0.1, relevance={"m1": 0.85})
    assert ask.reacts == ["👀"] and gate.bot.handled == []
    payload = types.SimpleNamespace(emoji="👀", message_id=ask.id, user_id=me, channel_id=GENERAL_CH, guild_id=1)
    asyncio.run(gate.bot.on_raw_reaction_add(payload))
    assert [c.judged for c in gate.bot.handled[-1][3]] == [0.85]


def test_no_key_captures_nothing_and_a_failed_request_is_skipped(gate, monkeypatch):
    from oibot_gm.llm import decisions

    me = gate.g.me.discord_id
    monkeypatch.delenv(decisions.KEY_ENV, raising=False)

    class Down(Ask):
        available = False

    gate.reg.set_plain_policy(gate.reg.merge_plain_policy({"set_listen": {str(GENERAL_CH): "reply"}, "set_remember": {str(GENERAL_CH): "shared"}}), "owner")
    monkeypatch.setattr(decisions, "DECISIONS", Down(0.9))
    asyncio.run(gate.bot.on_message(message(me)))
    assert gate.svc.count(gate.reg) == 0 and gate.bot.ops.lines and gate.bot.ops.lines[0][0] == "warn"

    class Failing(Ask):
        async def ask(self, state, questions, workload="listen"):
            return None

    monkeypatch.setenv(decisions.KEY_ENV, "x")
    monkeypatch.setattr(decisions, "DECISIONS", Failing(0.9))
    asyncio.run(gate.bot.on_message(message(me)))
    assert gate.svc.count(gate.reg) == 0 and gate.bot.listen_recent(gate.reg)[0]["did"] == "skipped"


def test_turning_a_channel_off_forgets_through_the_save_listener(gate, monkeypatch):
    from oibot_gm import registry as registry_mod

    me = gate.g.me.discord_id
    gate.run(0.2, message(me), remember_mode="shared", remember=0.9)
    assert gate.svc.count(gate.reg) == 1
    monkeypatch.setattr(registry_mod, "SAVE_LISTENERS", [mem.on_save])
    gate.reg.set_plain_policy(gate.reg.merge_plain_policy({"set_remember": {str(GENERAL_CH): None}}), "owner")
    assert gate.svc.count(gate.reg) == 0 and gate.svc.rows(gate.reg)[-1]["kind"] == "forget_channel"


def test_the_hourly_tick_fills_vectors_and_the_run_wiring_exists():
    assert callable(getattr(db.ListenMixin, "memory_tick", None))
    import inspect

    src = inspect.getsource(db.run)
    assert "memory.MEMORIES = " in src or "mem.MEMORIES = " in src
    assert "on_save" in src
    assert "memory_tick" in inspect.getsource(__import__("oibot_gm.raid_scheduler", fromlist=["x"]).RaidSchedulerMixin.scheduler_tick)


def test_a_recall_that_hangs_or_raises_still_decides_and_replies_with_no_memory(gate, monkeypatch):
    import time as _time

    me = gate.g.me.discord_id
    gate.run(0.2, message(me, text="our only jc is Ysolde"), remember_mode="shared", remember=0.9)
    monkeypatch.setattr(db, "RECALL_TIMEOUT_S", 0.05)

    def hangs(reg, text, *, channel=None):
        _time.sleep(0.5)
        return []

    monkeypatch.setattr(gate.svc, "recall_for", hangs)
    ask = message(me, text="who is our jc?")
    dec = gate.run(0.95, ask, remember_mode="shared", remember=0.1, relevance={"m1": 0.9})
    assert dec.requests[-1][0]["memory"] == [] and "relevance" not in dec.requests[-1][1]
    assert gate.bot.handled[-1][0] == ask.id and gate.bot.handled[-1][3] == []

    def raises(reg, text, *, channel=None):
        raise RuntimeError("index gone")

    monkeypatch.setattr(gate.svc, "recall_for", raises)
    ask2 = message(me, text="who is our jc?")
    dec = gate.run(0.95, ask2, remember_mode="shared", remember=0.1, relevance={"m1": 0.9})
    assert dec.requests[-1][0]["memory"] == [] and gate.bot.handled[-1][0] == ask2.id and gate.bot.handled[-1][3] == []
    assert gate.bot.listen_recent(gate.reg)[0]["did"] == "replied"


def test_the_capture_comes_after_the_reply_and_the_verdict_says_it_was_kept(gate, monkeypatch):
    me = gate.g.me.discord_id
    order = []
    real_plain, real_remember = gate.bot.plain_text, gate.svc.remember

    async def plain_text(message, reg, text, channel, memory=None):
        order.append(("reply", gate.svc.count(gate.reg)))
        await real_plain(message, reg, text, channel, memory=memory)

    def remember(*a, **kw):
        order.append(("keep", None))
        return real_remember(*a, **kw)

    monkeypatch.setattr(gate.bot, "plain_text", plain_text)
    monkeypatch.setattr(gate.svc, "remember", remember)
    gate.run(0.95, message(me, text="i can make devilsaur gauntlets now"), remember_mode="shared", remember=0.9)
    assert order == [("reply", 0), ("keep", None)] and gate.svc.count(gate.reg) == 1
    assert gate.bot.listen_recent(gate.reg)[0]["remembered"] is True

    def broken(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(gate.svc, "remember", broken)
    gate.run(0.95, message(me, text="i can make the elemental flux too"), remember_mode="shared", remember=0.9)
    row = gate.bot.listen_recent(gate.reg)[0]
    assert row["did"] == "replied" and row["remembered"] is False, "a capture that failed is not recorded as kept"


# ---- every answer path recalls

def test_plain_text_recalls_only_without_the_gates_list_and_the_block_reaches_help_answer(gate, monkeypatch):
    """The real plain_text three ways: memory=None (an @mention, a DM) → ONE recall + judge (workload memory) and the block
    into help_answer; memory=[] (the gate found nothing) → no recall, no block; the gate's judged list → no recall, its block."""
    import contextlib

    me = gate.g.me.discord_id
    gate.run(0.2, message(me, text="our only jc is Ysolde"), remember_mode="shared", remember=0.9)
    from oibot_gm.llm import decisions

    dec = Ask(0.0, relevance={"m1": 0.9})
    monkeypatch.setattr(decisions, "DECISIONS", dec)
    recalls, seen = [], []

    async def help_answer(reg, uid, officer, text, may_act=False, public=False, **kw):
        seen.append(kw.get("memory"))
        return "answered"

    async def plain_identity(reg, user, **_):
        return False, []

    class Chan:
        @contextlib.asynccontextmanager
        async def typing(self):
            yield

    async def reply(content=None, **kw):
        return types.SimpleNamespace(id=None)

    stub = types.SimpleNamespace(ctx=types.SimpleNamespace(provider=object()), policies=types.SimpleNamespace(store=lambda r: None), ops=None,
                                 help_answer=help_answer, plain_identity=plain_identity, reply_private=lambda r, ch: True)

    async def recall_memories(reg, text, channel):
        recalls.append(channel)
        return await db.OibotGM.recall_memories(stub, reg, text, channel)

    stub.recall_memories = recall_memories

    def send(**kw):
        msg = types.SimpleNamespace(id=next(_next_id), author=types.SimpleNamespace(id=me, display_name="Mara"), channel=Chan(), reply=reply,
                                    guild=None, reference=None)
        asyncio.run(db.OibotGM.plain_text(stub, msg, gate.reg, "who is our jc?", GENERAL_CH, **kw))

    send()
    assert recalls == [GENERAL_CH] and len(dec.requests) == 1 and dec.requests[0][2] == "memory" and set(dec.requests[0][1]) == {"decision"}
    assert seen[-1].startswith("<memory>\n") and " said on " in seen[-1] and "our only jc is Ysolde" in seen[-1]
    send(memory=[])
    assert recalls == [GENERAL_CH] and len(dec.requests) == 1 and seen[-1] is None
    judged = asyncio.run(db.OibotGM.recall_memories(stub, gate.reg, "who is our jc?", GENERAL_CH))
    n = len(dec.requests)
    send(memory=judged)
    assert recalls == [GENERAL_CH] and len(dec.requests) == n and seen[-1] == mem.memory_block(judged)
    monkeypatch.setattr(gate.reg.config, "ask_audience", "officers")
    send()
    assert recalls == [GENERAL_CH] and len(dec.requests) == n, "outside the ask audience nothing is recalled or judged"
    monkeypatch.setattr(decisions, "DECISIONS", None)
    assert asyncio.run(db.OibotGM.recall_memories(stub, gate.reg, "who is our jc?", None)) == [], "no decision service: no memories"


def test_the_memory_share_of_the_budget():
    from oibot_gm.llm import provider

    assert provider.SHARES["memory"] == 0.1


def test_the_probe_reads_the_live_question_and_the_manual_names_the_feature():
    import importlib.util

    spec = importlib.util.spec_from_file_location("listen_probe", "scripts/listen_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    st = probe.state("who is our jc?")
    assert st["memory"] == [] and st["message"]["text"] == "who is our jc?" and len(probe.BAIT) >= 8 and len(probe.BANTER) >= 8
    manual = open("docs/manual.md", encoding="utf-8").read()
    assert "**Memory**" in manual and "Remembers here only" in manual and "said on" in manual and "Forget" in manual


# ---- the routes

def seeded(gate):
    """Two memories: one of `me` in #general (shared), one of `other` in #officer-chat (channel), five minutes apart
    (distinct `at`: the list is newest first and two rows of one second have no fixed order)."""
    from datetime import datetime, timezone

    me, other = gate.g.me.discord_id, gate.g.other.discord_id
    first = message(me, text="our only jc is Ysolde")
    first.created_at = datetime(2026, 9, 25, 19, 0, tzinfo=timezone.utc)
    gate.run(0.2, first, remember_mode="shared", remember=0.9)
    second = message(other, channel=OFFICER_CH, text="we bench whoever is late twice", name="officer-chat")
    second.created_at = datetime(2026, 9, 25, 19, 5, tzinfo=timezone.utc)
    gate.run(0.2, second, remember_mode="channel", remember=0.9)
    return gate.svc.list(gate.reg)[0]


def test_officers_list_search_filter_and_forget(web_client, gate, monkeypatch):
    import re

    w = web_client
    rows = seeded(gate)
    d = w.c.get("/api/memories", headers=w.as_(w.officer)).json()
    assert d["total"] == 2 and len(d["rows"]) == 2 and d["more"] is False and d["words"] == {"shared": "Remembers", "channel": "Remembers here only"}
    assert d["channels"] == {str(GENERAL_CH): "shared", str(OFFICER_CH): "channel"} and d["model"] in ("off", "loading", "loaded", "failed")
    r0 = d["rows"][0]
    assert r0["text"] == "we bench whoever is late twice" and r0["scope"] == "channel" and r0["scope_word"] == "Remembers here only" and r0["channel_name"] == "officer-chat"
    assert re.fullmatch(r"\w{3} \d{1,2} \w{3} \d{1,2}:\d{2} (AM|PM)", r0["when"]), r0["when"]
    assert w.c.get(f"/api/memories?q=jc", headers=w.as_(w.officer)).json()["total"] == 1
    assert w.c.get(f"/api/memories?channel={OFFICER_CH}", headers=w.as_(w.officer)).json()["total"] == 1
    assert w.c.get(f"/api/memories?member={gate.g.me.discord_id}", headers=w.as_(w.officer)).json()["rows"][0]["author_id"] == str(gate.g.me.discord_id)
    assert w.c.get("/api/memories?limit=1", headers=w.as_(w.officer)).json()["more"] is True
    assert w.c.get("/api/memories", headers=w.as_(w.member)).status_code == 403
    mid = next(r.id for r in rows if r.channel == GENERAL_CH)
    r = w.c.post(f"/api/memories/{mid}/forget", json={"preview": True}, headers=w.as_(w.officer))
    assert r.status_code == 200 and r.json()["sentences"] == [f"The bot forgets what {gate.g.me.display_name} said on {rows[1].when()}. It never repeats it again."]
    r = w.c.post(f"/api/memories/{mid}/forget", json={}, headers=w.as_(w.officer))
    assert r.status_code == 200 and gate.svc.count(gate.reg) == 1
    r = w.c.post(f"/api/memories/{mid}/forget", json={}, headers=w.as_(w.officer))
    assert r.status_code == 404 and r.json()["error"] == "That memory is already forgotten."
    owner = {"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"}
    other_id = next(r.id for r in rows if r.channel == OFFICER_CH)
    r = w.c.post(f"/api/memories/{other_id}/forget", json={}, headers=owner)
    assert r.status_code == 403 and "OIBOT_MCP_ACTOR" in r.json()["error"], "a bare bearer never forgets"


def test_the_list_answers_the_empty_shape_without_the_service(web_client, monkeypatch):
    w = web_client
    monkeypatch.setattr(mem, "MEMORIES", None)
    r = w.c.get("/api/memories", headers=w.as_(w.officer))
    assert r.status_code == 200 and r.json()["rows"] == [] and r.json()["total"] == 0 and r.json()["model"] == "off"
    assert w.c.get("/api/me", headers=w.as_(w.member)).json()["memories"] == {"rows": [], "total": 0, "more": False}


def test_a_forget_writes_one_audit_line_naming_the_author_never_the_words(web_client, gate):
    w = web_client
    rows = seeded(gate)
    mid = next(r.id for r in rows if r.channel == GENERAL_CH)
    assert w.c.post(f"/api/memories/{mid}/forget", json={}, headers=w.as_(w.officer)).status_code == 200
    line = next(t for _lvl, t in w.bot.ops.lines if "forgot" in t)
    assert gate.g.me.display_name in line and rows[1].when() in line and "#general" in line and "Ysolde" not in line


def test_a_member_sees_and_forgets_their_own_only(web_client, gate):
    w = web_client
    rows = seeded(gate)
    me_id = gate.g.me.discord_id
    gate.reg.rename(me_id, "Renamed", "owner") if hasattr(gate.reg, "rename") else None
    d = w.c.get("/api/me", headers=w.as_(w.member)).json()
    assert d["memory_on"] is True and d["memories"]["total"] == 1 and d["memories"]["rows"][0]["text"] == "our only jc is Ysolde"
    mine = d["memories"]["rows"][0]["id"]
    theirs = next(r.id for r in rows if r.channel == OFFICER_CH)
    assert w.c.post(f"/api/me/memories/{theirs}/forget", json={}, headers=w.as_(w.member)).status_code == 403
    r = w.c.post(f"/api/me/memories/{mine}/forget", json={"preview": True}, headers=w.as_(w.member))
    assert r.status_code == 200 and r.json()["sentences"][0].startswith("The bot forgets what you said on ")
    assert w.c.post(f"/api/me/memories/{mine}/forget", json={}, headers=w.as_(w.member)).status_code == 200
    assert w.c.get("/api/me", headers=w.as_(w.member)).json()["memories"]["total"] == 0
    owner = {"Authorization": f"Bearer {w.token}", "X-Requested-With": "oibot"}
    assert w.c.post(f"/api/me/memories/{theirs}/forget", json={}, headers=owner).status_code == 403, "never MCP"
    gate.reg.set_plain_policy(gate.reg.merge_plain_policy({"set_remember": {str(GENERAL_CH): None, str(OFFICER_CH): None}}), "owner")
    assert w.c.get("/api/me", headers=w.as_(w.member)).json()["memory_on"] is False


def test_a_write_publishes_the_memory_topic(web_client, gate, monkeypatch):
    assert any(getattr(f, "__name__", "") == "memory_changed" for f in mem.MEMORY_LISTENERS)


def test_mcp_memory_tools_call_the_routes():
    import json

    import httpx

    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, str(request.url), request.read()))
        if request.method == "GET":
            return httpx.Response(200, json={"rows": [{"id": "m-1", "when": "Wed 5 Nov 9:00 PM", "channel_name": "general", "author": "Mara", "text": "x", "scope_word": "Remembers"}], "total": 1, "more": False})
        return httpx.Response(200, json={"message": "The bot forgets what Mara said on 5 Nov."})

    ms.use(ms.Api(url="http://bot", token="a" * 64, transport=httpx.MockTransport(handler)))
    try:
        d = ms.list_memories(q="x", channel="1")
        assert d["rows"][0]["author"] == "Mara" and d["total"] == 1
        assert ms.forget_memory("m-1").startswith("The bot forgets")
    finally:
        ms.use(None)
    assert calls[0][:2] == ("GET", "/api/memories") and "q=x" in calls[0][2] and calls[1][:2] == ("POST", "/api/memories/m-1/forget")


def test_the_by_design_entries_name_living_routes():
    assert configops.BY_DESIGN_ONLY["memories_list"]["path"] == "GET /api/memories" and configops.BY_DESIGN_ONLY["my_memories"]["path"] == "POST /api/me/memories/{key}/forget"
