"""Recruitment settings and the extended application: validated in one write, questions within Discord's limits,
answers as asked, closed means closed, and who must apply instead of register."""
import pytest

from oibot_gm.registry import RegistryError

Q = [{"label": "Why us?", "required": True}, {"label": "Raiding experience", "hint": "last tier, logs welcome"}]


def test_questions_and_invite_validate_before_anything_is_saved(reg):
    reg.set_recruitment("t", questions=Q, invite_url="https://discord.gg/abc123", note="healers wanted")
    assert [q.label for q in reg.config.recruitment.questions] == ["Why us?", "Raiding experience"] and reg.config.invite_url == "https://discord.gg/abc123"
    for bad, why in (({"questions": [{"label": "x" * 46}]}, "45"), ({"questions": [{"label": "a", "hint": "h" * 101}]}, "100"),
                     ({"questions": [{"label": "a"}, {"label": "A"}]}, "twice"), ({"invite_url": "https://evil.example/x"}, "looks like"),
                     ({"questions": [{"label": f"q{i}"} for i in range(16)]}, "At most")):
        with pytest.raises(RegistryError, match=why):
            reg.set_recruitment("t", **bad)
    assert len(reg.config.recruitment.questions) == 2 and reg.config.invite_url == "https://discord.gg/abc123"


def test_application_carries_surname_schedules_and_answers(reg):
    reg.set_recruitment("t", questions=Q)
    with pytest.raises(RegistryError, match="Why us"):
        reg.apply(5551, "Newbie", "Kessa", "Mage", "Frost", None, None, None, None, surname="Dawn", answers=[])
    a = reg.apply(5551, "Newbie", "Kessa", "Mage", "Frost", None, None, None, None, surname="Dawn",
                  answers=[{"label": "Why us?", "answer": "friends here"}, {"label": "Gone question", "answer": "dropped"}], schedules=["nope:x"])
    assert a.label == "Kessa Dawn" and a.answers == [{"label": "Why us?", "answer": "friends here"}] and a.schedules == []
    a2, char = reg.decide(5551, True, "officer")
    assert char.label == "Kessa Dawn" and char.confirmed_by == "officer"


def test_closed_recruitment_refuses(reg):
    reg.set_recruitment("t", open=False)
    with pytest.raises(RegistryError, match="isn't taking applications"):
        reg.apply(5552, "Other", "Borin", "Warrior", "Arms", None, None, None, None)


def test_who_must_apply(reg):
    newcomer, member = 7770001, reg.test_members()[0].discord_id
    assert reg.must_apply(newcomer, officer=False) and not reg.must_apply(newcomer, officer=True)
    assert not reg.must_apply(member, officer=False)  # already registered: /register adds alts as before
    reg.set_recruitment("t", member_role_ids=[42])
    assert not reg.must_apply(newcomer, officer=False, role_ids=[42])
