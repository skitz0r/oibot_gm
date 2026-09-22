"""The test bench takes its shape from the private data repo when a guild supplies one (no real names in this repo)."""
import yaml


def test_builtin_roster_is_used_when_the_guild_supplies_none(reg):
    roster = reg.test_roster()
    assert len(roster) == len(reg.TEST_NAMES)
    assert [r["name"] for r in roster] == list(reg.TEST_NAMES)
    assert all(r["spec"] in reg.profile.classes[r["cls"]] for r in roster)


def test_private_roster_file_wins_and_seeds_from_it(reg):
    rows = [
        {"name": "Middle(Someone)", "character": "Someone", "cls": "Warrior", "spec": "Protection", "rank": "core"},
        {"name": "lowercase guy", "character": "Lowercase", "cls": "Shaman", "spec": "Restoration", "rank": "trial"},
        {"name": "No Character 9 Field!", "cls": "Mage", "spec": "Fire"},
    ]
    (reg.store.root / reg.key / "test_roster.yaml").write_text(yaml.safe_dump(rows))
    assert reg.test_roster() == rows
    reg.clear_test_members("t")
    made = reg.seed_test_members(3, "t")
    assert [m.display_name for m in made] == ["Middle(Someone)", "lowercase guy", "No Character 9 Field!"]
    # a Discord display name may hold spaces and punctuation; the CHARACTER is normalised to the 2-12 letter rule
    assert [m.main.name for m in made] == ["Someone", "Lowercase", "Nocharacterf"]  # letters only, cut to 12
    assert [(m.main.cls, m.main.spec) for m in made] == [("Warrior", "Protection"), ("Shaman", "Restoration"), ("Mage", "Fire")]
    assert [m.main.rank for m in made] == ["core", "trial", "trial"]  # rank defaults to trial
    assert all(m.test for m in made)
    reg.clear_test_members("t")


def test_a_bad_row_is_skipped_rather_than_crashing_the_seed(reg):
    (reg.store.root / reg.key / "test_roster.yaml").write_text(yaml.safe_dump(
        [{"name": "Fine", "cls": "Mage", "spec": "Fire"}, {"cls": "Mage"}, "not a dict", {"name": "NoClass"}]))
    assert [r["name"] for r in reg.test_roster()] == ["Fine"]
