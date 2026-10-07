"""Applications in Discord: the apply wizard (picks → names → schedules → the guild's questions five per modal →
review → Submit), closed recruitment, required answers, the review card within Discord's limits, newcomers sent to
apply instead of registering (officers and member-role holders bypass), the owner's recruitment wizard, the plain-text
`recruit` op, and the one decide path that never removes anyone. No Discord, no LLM."""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import discord
import pytest

from oibot_gm import configops, discord_registry
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_registry import FIELD_VALUE_MAX, FIELDS_MAX, application_embed, settle_application
from oibot_gm.flows_register import NEWCOMER
from oibot_gm.registry import ANSWER_MAX, Applicant, RegistryError
from oibot_gm.wizard import FLOWS
from oibot_gm.wizard_flows import FLOWS as _ALL  # noqa: F401 — every flow registered
from wizard_harness import Harness

NEW_ID = 7_777_000_101
RID = "barrow_deeps"
SIX = [{"label": f"Question {i}", "hint": f"hint {i}", "required": i == 1} for i in range(1, 7)]


def run(coro):
    return asyncio.run(coro)


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


class FakeOps:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def emit(self, cfg, level, text, **kw):
        self.lines.append((level, text))


def person(reg, *, uid=NEW_ID, name="Newbie", roles=()):
    h = Harness(reg)
    h.user = SimpleNamespace(id=uid, display_name=name, mention=f"@{name}", roles=[SimpleNamespace(id=r) for r in roles])
    h.posted = []

    async def post_application(r, a):
        h.posted.append(a)
        return "posted for review in #applications"
    h.client = SimpleNamespace(ico=lambda kind, key: "", ops=FakeOps(), post_application=post_application,
                               registries=SimpleNamespace(for_interaction=lambda i: reg, for_guild=lambda gid: reg if gid == reg.config.discord_guild_id else None), raids=SimpleNamespace(store=lambda _r: None))
    return h


def a_class(reg):
    cls = next(c for c, s in reg.profile.classes.items() if len(s) >= 2)
    return cls, list(reg.profile.classes[cls])


def schedule_keys(reg) -> list[str]:
    from oibot_gm import wizard_opts

    return [o.value for o in wizard_opts.schedules(reg)]


# ---------------------------------------------------------------- the wizard end to end

def test_apply_end_to_end_with_schedules_and_two_pages_of_questions(reg):
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    reg.set_recruitment("t", questions=SIX, note="Looking for healers")
    keys = schedule_keys(reg)
    assert f"{RID}:alt" in keys
    h = person(reg)
    cls, specs = a_class(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, mode="apply")
        assert "Looking for healers" in h.text and h.selects() == ["Class", "Main spec", "Offspec (optional)"]
        await h.pick("Class", cls)
        await h.pick("Main spec", specs[0])
        await h.press("Continue")
        assert set(h.modal._items) == {"first", "last", "logs"}  # guild questions exist: no free box here
        await h.submit(first="Kessa", last="Dawnfire", logs="https://logs.example/k")
        sel = next(s for s in h._items() if isinstance(s, discord.ui.Select))
        assert sel.placeholder == "Raid schedules you can make" and sel.max_values == len(keys) and sel.min_values == 0
        assert any(o.label.startswith(reg.raid_def(RID)["name"] + " · ") for o in sel.options)
        await h.pick("Raid schedules you can make", f"{RID}:alt")
        await h.press("Continue")
        page1 = h.modal
        assert page1.title == "Questions (1 of 2)" and len(page1._items) == 5
        q1 = page1._items["q0"]
        assert q1.required and q1.max_length == ANSWER_MAX and q1.style == discord.TextStyle.paragraph
        label = next(c for c in page1.children if isinstance(c, discord.ui.Label) and c.component is q1)
        assert (label.text, label.description) == ("Question 1", "hint 1")
        await h.submit(page1, q0="Friends here", q1="Two tiers", q2="x" * 900, q3="", q4="Yes")
        assert h.buttons() == ["Next questions (2 of 2)", "Cancel"] and h.modal is None
        await h.press("Next questions (2 of 2)")
        assert h.modal.title == "Questions (2 of 2)" and list(h.modal._items) == ["q0"]
        n0 = len(reg.applicants)
        await h.submit(h.modal, q0="Last one")
        assert len(reg.applicants) == n0, "nothing written before Submit"
        e = h.screen["embed"]
        assert "Submit" in h.buttons() and e is not None and "Kessa Dawnfire" in e.title
        names = [f.name for f in e.fields]
        assert names[:2] == ["Logs", "Can raid"] and names[2:] == [q["label"] for q in SIX]
        assert all(len(f.value) <= 200 for f in e.fields)  # long answers shortened for the review only
        await h.press("Submit")
        assert "✅ Application received for **Kessa Dawnfire**" in h.text and "#applications" in h.text
    run(go())
    a = reg.applicants[NEW_ID]
    assert (a.name, a.surname, a.cls, a.spec, a.source, a.status) == ("Kessa", "Dawnfire", cls, specs[0], "discord", "open")
    assert a.schedules == [f"{RID}:alt"] and a.logs_url == "https://logs.example/k"
    assert [x["label"] for x in a.answers] == ["Question 1", "Question 2", "Question 3", "Question 5", "Question 6"]  # the empty optional one dropped
    assert a.answers[2]["answer"] == "x" * 900  # stored whole
    assert h.posted == [a]


def test_review_change_goes_back_to_a_step_and_returns(reg):
    reg.set_recruitment("t", questions=SIX[:2])
    h = person(reg)
    cls, specs = a_class(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, mode="apply")
        await h.pick("Class", cls)
        await h.pick("Main spec", specs[0])
        await h.press("Continue")
        await h.submit(first="Kessa", last="Dawnfire")
        if "Raid schedules you can make" in h.selects():
            await h.press("Continue")
        else:
            await h.press("Answer the questions")
        await h.submit(h.modal, q0="first answer", q1="")
        await h.pick("Change a step", "q0")
        assert h.modal._items["q0"].default == "first answer"
        await h.submit(h.modal, q0="better answer", q1="now filled")
        assert "Submit" in h.buttons()  # straight back to the review, not through the steps again
        await h.pick("Change a step", "names")
        await h.submit(h.modal, first="Kessa", last="Brightfire")
        assert "Kessa Brightfire" in h.screen["embed"].title
        await h.press("Submit")
    run(go())
    a = reg.applicants[NEW_ID]
    assert a.label == "Kessa Brightfire" and a.answers == [{"label": "Question 1", "answer": "better answer"}, {"label": "Question 2", "answer": "now filled"}]


def test_closed_recruitment_is_refused_at_entry_and_on_the_card_button(reg):
    reg.set_recruitment("t", open=False)
    h = person(reg)
    run(FLOWS["register"](h.command(), reg, mode="apply"))
    assert "isn't taking applications" in h.text and h.screen["view"] is None
    run(discord_registry.RegisterButton("apply", reg.key).callback(h.card_press()))
    assert "isn't taking applications" in h.text and h.screen["view"] is None and h.modal is None
    assert NEW_ID not in reg.applicants


def test_a_required_question_left_empty_is_refused_and_fix_this_keeps_the_answers(reg):
    reg.set_recruitment("t", questions=SIX[:3])
    h = person(reg)
    cls, specs = a_class(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, mode="apply")
        await h.pick("Class", cls)
        await h.pick("Main spec", specs[0])
        await h.press("Continue")
        await h.submit(first="Kessa", last="Dawnfire")
        if "Raid schedules you can make" in h.selects():
            await h.press("Continue")
        else:
            await h.press("Answer the questions")
        await h.submit(h.modal, q0="", q1="kept", q2="also kept")
        assert "❌" in h.text and "Question 1" in h.text and h.buttons() == ["Fix this", "Cancel"]
        await h.press("Fix this")
        assert h.modal._items["q1"].default == "kept" and h.modal._items["q2"].default == "also kept"
        await h.submit(h.modal, q0="now answered", q1="kept", q2="also kept")
        # meanwhile the owner adds another required question: apply refuses at Submit, the draft is kept
        reg.set_recruitment("t", questions=SIX[:3] + [{"label": "Added later", "required": True}])
        await h.press("Submit")
        assert "❌" in h.text and "Added later" in h.text and "Fix this" in h.buttons()
    run(go())
    assert NEW_ID not in reg.applicants and not h.posted


# ---------------------------------------------------------------- the review card

def big_applicant(reg, n=15) -> Applicant:
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    return Applicant(discord_id=NEW_ID, display_name="Newbie", name="Kessa", surname="Dawnfire", cls="Mage", spec="Frost", offspec="Fire",
                     logs_url="https://logs.example/" + "k" * 150, schedules=[f"{RID}:alt", "gone:raid"],
                     answers=[{"label": f"Question number {i}", "answer": f"{i} " + "word " * 199} for i in range(n)])


def test_the_review_card_stays_within_discords_limits(reg, monkeypatch):
    monkeypatch.delenv("OIBOT_WEB_URL", raising=False)
    a = big_applicant(reg)
    e = application_embed(reg, a)
    assert "Kessa Dawnfire" in e.title and "Mage Frost" in e.description
    assert len(e.fields) <= FIELDS_MAX and all(len(f.value) <= FIELD_VALUE_MAX and len(f.name) <= 256 for f in e.fields)
    assert len(e) <= 6000
    can = next(f for f in e.fields if f.name == "Can raid")
    assert f"{reg.raid_def(RID)['name']} · " in can.value and "no longer scheduled" in can.value  # human labels, never '<raid>:<id>'
    answers = [f for f in e.fields if f.name.startswith("Question number")]
    assert len(answers) == 15 and all(f.value.endswith("…") for f in answers)
    assert not any("site" in f.value for f in e.fields)  # the site has no applications page: never pointed at
    assert any("Full answers: ask me (“show Kessa's application”) or MCP list_applications." in f.value for f in e.fields)


def test_a_short_application_card_is_whole(reg, monkeypatch):
    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    a = big_applicant(reg, n=0)
    a.answers = [{"label": "Why us?", "answer": "friends"}]
    e = application_embed(reg, a)
    assert [f.name for f in e.fields] == ["Logs", "Can raid", "Why us?"] and e.fields[-1].value == "friends"


# ---------------------------------------------------------------- newcomers apply instead of registering

def test_a_newcomer_pressing_register_is_sent_to_apply(reg):
    h = person(reg)
    run(FLOWS["register"](h.card_press(), reg, slot="main"))
    assert NEWCOMER in h.text and "Apply to" in h.text
    h2 = person(reg)
    run(FLOWS["register"](h2.command(), reg, slot="alt", plan_only=True))  # /me plan alt, /me char add alike
    assert NEWCOMER in h2.text and h2.buttons() == ["Continue", "Cancel"]


def test_officers_and_member_role_holders_register_without_applying(reg, monkeypatch):
    reg.set_recruitment("t", member_role_ids=[4242])
    h = person(reg, roles=[4242])
    run(FLOWS["register"](h.command(), reg, slot="main"))
    assert "Register or plan your main" in h.text and NEWCOMER not in h.text
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    h2 = person(reg, uid=NEW_ID + 1)
    run(FLOWS["register"](h2.command(), reg, slot="main", plan_only=True))
    assert "Plan your main" in h2.text and NEWCOMER not in h2.text


def test_a_registered_member_still_adds_alts(reg):
    m = reg.test_members()[0]
    h = person(reg, uid=m.discord_id, name=m.display_name)
    cls, specs = a_class(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, slot="alt")
        assert "Add an alt" in h.text and NEWCOMER not in h.text
        await h.pick("Class", cls)
        await h.pick("Main spec", specs[0])
        await h.press("Continue")
        await h.submit(first="Altie", last="Second")
        assert "✅ Registered alt" in h.text
    run(go())


def test_plain_text_character_add_sends_a_newcomer_to_apply(reg):
    op = lambda: ConfigOp(op="character", field="add", character="Kessa Dawnfire", value="Mage Frost")  # noqa: E731
    allowed, refused = configops.authorize(reg, [op()], NEW_ID, officer=False, role_ids=[])
    assert not allowed and refused == [configops.APPLY_FIRST] and "/apply" in refused[0]
    reg.set_recruitment("t", member_role_ids=[4242])
    _, refused = configops.authorize(reg, [op()], NEW_ID, officer=False, role_ids=[4242])
    assert configops.APPLY_FIRST not in refused
    m = reg.test_members()[0]
    _, refused = configops.authorize(reg, [op()], m.discord_id, officer=False, role_ids=[])
    assert configops.APPLY_FIRST not in refused


# ---------------------------------------------------------------- the owner's recruitment wizard

@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)


def role_pick(h, *ids):
    sel = next(s for s in h._items() if isinstance(s, discord.ui.RoleSelect))
    sel._values = [SimpleNamespace(id=i) for i in ids]
    return sel.callback(h._on_screen())


def test_the_recruitment_wizard_edits_questions_and_saves_once(reg, store, owner):
    h = person(reg, uid=reg.config.owner_discord_id, name="Owner")
    n0 = commits(store)

    async def go():
        await FLOWS["recruitment"](h.command(), reg)
        assert "Applications:** open" in h.text and h.buttons()[:4] == ["Close applications", "Note…", "Invite link…", "Add a question…"]
        await h.press("Add a question…")
        assert set(h.modal._items) == {"label", "hint", "required"}
        await h.submit(h.modal, label="Why us?", hint="a line or two", required="yes")
        await h.press("Add a question…")
        await h.submit(h.modal, label="x" * 46, hint="", required="no")  # past Discord's 45: the registry's own rule refuses it
        assert "❌" in h.text and "45" in h.text
        await h.press("Fix this")
        assert h.modal._items["label"].default == "x" * 46
        await h.submit(h.modal, label="Raiding experience", hint="", required="no")
        await h.press("Add a question…")
        await h.submit(h.modal, label="Anything else?", hint="", required="no")
        await h.pick("Pick a question to change", "1")
        await h.press("Move up")
        assert "Question 1 of 3: **Raiding experience**" in h.text
        await h.press("Back")
        await h.pick("Pick a question to change", "2")
        await h.press("Remove")
        await h.pick("Pick a question to change", "1")
        await h.press("Edit…")
        await h.submit(h.modal, label="Why us?", hint="a sentence", required="yes")
        await role_pick(h, 4242, 4343)
        await h.press("Close applications")
        await h.press("Note…")
        await h.submit(h.modal, note="Looking for healers")
        assert commits(store) == n0, "nothing written before Save"
        assert "change(s) in the draft" in h.text and "<@&4242>" in h.text
        await h.press("Save")
        assert "✅ Saved" in h.text
    run(go())
    r = reg.config.recruitment
    assert [(q.label, q.hint, q.required) for q in r.questions] == [("Raiding experience", None, False), ("Why us?", "a sentence", True)]
    assert r.open is False and r.note == "Looking for healers" and reg.config.member_role_ids == [4242, 4343]
    assert commits(store) == n0 + 1


def test_a_bad_invite_is_refused_at_save_and_the_draft_is_kept(reg, store, owner):
    h = person(reg, uid=reg.config.owner_discord_id, name="Owner")
    n0 = commits(store)

    async def go():
        await FLOWS["recruitment"](h.command(), reg)
        await h.press("Invite link…")
        await h.submit(h.modal, invite="https://evil.example/x")
        await h.press("Save")
        assert "❌" in h.text and "discord.gg" in h.text
        await h.press("Fix this")
        assert h.modal._items["invite"].default == "https://evil.example/x"
        await h.submit(h.modal, invite="https://discord.gg/abc123")
        await h.press("Save")
        assert "✅" in h.text
    run(go())
    assert reg.config.invite_url == "https://discord.gg/abc123" and commits(store) == n0 + 1


def test_the_recruitment_wizard_is_owner_only(reg):
    h = person(reg)
    run(FLOWS["recruitment"](h.command(), reg))
    assert h.text == "Owner only." and h.screen["view"] is None


# ---------------------------------------------------------------- plain text

def test_the_plain_text_recruit_op(reg):
    assert configops.OP_GROUP["recruit"] == "setup" and "recruit" in configops.OWNER_OPS
    add = ConfigOp(op="recruit", field="question_add", value="Why us?", reason="a line", text="required")
    assert configops.describe(reg, add) == "add question 1: “Why us?” (required) — hint: a line"
    configops.apply(reg, add, "Owner", True)
    configops.apply(reg, ConfigOp(op="recruit", field="question_add", value="Raiding experience"), "Owner", True)
    assert [(q.label, q.hint, q.required) for q in reg.config.recruitment.questions] == [("Why us?", "a line", True), ("Raiding experience", None, False)]
    rm = ConfigOp(op="recruit", field="question_remove", value="1")
    assert configops.describe(reg, rm) == "remove question 1: “Why us?”"
    configops.apply(reg, rm, "Owner", True)
    configops.apply(reg, ConfigOp(op="recruit", field="question_remove", value="raiding experience"), "Owner", True)
    assert reg.config.recruitment.questions == []
    close = ConfigOp(op="recruit", field="open", value="false")
    assert configops.describe(reg, close) == "applications: open → closed"
    configops.apply(reg, close, "Owner", True)
    configops.apply(reg, ConfigOp(op="recruit", field="note", value="Healers wanted"), "Owner", True)
    configops.apply(reg, ConfigOp(op="recruit", field="invite", value="https://discord.gg/abc123"), "Owner", True)
    assert not reg.config.recruitment.open and reg.config.recruitment.note == "Healers wanted" and reg.config.invite_url == "https://discord.gg/abc123"
    with pytest.raises(RegistryError, match="looks like"):
        configops.apply(reg, ConfigOp(op="recruit", field="invite", value="https://evil.example"), "Owner", True)
    with pytest.raises(RegistryError, match="no question"):
        configops.apply(reg, ConfigOp(op="recruit", field="question_remove", value="7"), "Owner", True)
    with pytest.raises(RegistryError, match="needs the owner"):
        configops.apply(reg, ConfigOp(op="recruit", field="open", value="true"), "Officer", False)
    assert "recruit" in configops.SCHEMA_TEXT and "question_add" in configops.SCHEMA_TEXT


# ---------------------------------------------------------------- one decide path

class FakeMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeGuild:
    """A server the decide path must never act on: any kick or ban is recorded (and fails the test)."""

    def __init__(self):
        self.removed = []

    async def kick(self, *a, **kw):
        self.removed.append(("kick", a))

    async def ban(self, *a, **kw):
        self.removed.append(("ban", a))


def fake_client(reg):
    msg, guild, dms = FakeMsg(), FakeGuild(), []

    async def fetch_user(uid):
        async def send(text):
            dms.append((uid, text))
        return SimpleNamespace(id=uid, send=send, kick=guild.kick, ban=guild.ban)

    async def fetch_message(mid):
        return msg
    ch = SimpleNamespace(fetch_message=fetch_message, name="applications")
    return SimpleNamespace(fetch_user=fetch_user, get_channel=lambda cid: ch, get_guild=lambda gid: guild, ops=FakeOps(), ico=lambda k, v: ""), msg, guild, dms


def test_decide_application_returns_the_line_and_never_removes_anyone(reg):
    from oibot_gm.discord_bot import OibotGM

    reg.config.applications_channel_id = 555
    a = reg.apply(NEW_ID, "Newbie", "Kessa", "Mage", "Frost", None, None, None, None, surname="Dawnfire")
    a.message_id = 777
    reg.save_applicant(a, "card posted (test)")
    client, msg, guild, dms = fake_client(reg)
    line = run(OibotGM.decide_application(client, reg, NEW_ID, False, "Officer", "not this time"))
    assert line == "⛔ Declined Newbie (Kessa Dawnfire)"
    assert guild.removed == [] and reg.applicants[NEW_ID].status == "declined" and NEW_ID not in reg.members
    assert "welcome to stay" in dms[0][1] and "not this time" in dms[0][1]
    assert msg.edits and msg.edits[0]["view"] is None and "declined" in msg.edits[0]["embed"].footer.text
    assert any("Declined" in t for _, t in client.ops.lines)
    with pytest.raises(RegistryError, match="No open application"):
        run(settle_application(client, reg, NEW_ID, True, "Officer", None))
    # accept: registered as a confirmed trial, surname carried
    b = reg.apply(NEW_ID + 1, "Other", "Borin", "Warrior", "Arms", None, None, None, None, surname="Stone")
    line = run(settle_application(client, reg, b.discord_id, True, "Officer", None))
    assert line.startswith("✅ Accepted Other as Borin Stone") and reg.members[b.discord_id].main.label == "Borin Stone"
    assert guild.removed == []


def test_the_card_buttons_use_the_clients_decide_path(reg, monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    reg.apply(NEW_ID, "Newbie", "Kessa", "Mage", "Frost", None, None, None, None, surname="Dawnfire")
    h = person(reg, uid=1, name="Officer")
    calls = []

    async def decide(r, uid, accept, by, note):
        calls.append((uid, accept, by, note))
        return "✅ line from the client"
    h.client.decide_application = decide
    view = discord_registry.ApplicationView(reg, NEW_ID)
    assert [b.item.custom_id for b in view.children] == [f"app.{reg.key}:accept:{NEW_ID}", f"app.{reg.key}:decline:{NEW_ID}"]
    run(view.children[0].callback(h.card_press()))
    assert calls == [(NEW_ID, True, "Officer", None)] and h.text == "✅ line from the client"
    run(view.children[1].callback(h.card_press()))
    assert isinstance(h.modal, discord_registry.DeclineModal)
