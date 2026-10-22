"""Static checks on frontend/src that tsc cannot make (there is no frontend test runner): code that a line comment
swallowed still type-checks, so it is caught here."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
# statement-shaped code after a `//` on the same line: a swallowed finally/catch/else block or a setter call
SWALLOWED = re.compile(r"(?<![:\"'`])//.*?(\bfinally\s*\{|\bcatch\s*\(|\}\s*else\s*\{|\bset[A-Z]\w*\([^)]*\);)")


def test_no_code_hidden_in_a_line_comment():
    """Regression: ScheduleEditor.save() had `finally { setBusy(false); }` at the end of a `//` comment, so a refused
    save (409 / 400) left the Save button spinning for good."""
    hits = []
    for f in sorted(SRC.rglob("*.ts*")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if SWALLOWED.search(line):
                hits.append(f"{f.relative_to(SRC)}:{n}: {line.strip()}")
    assert not hits, "code after a // comment never runs:\n" + "\n".join(hits)


def test_schedule_editor_save_always_clears_busy():
    page = (SRC / "components" / "ScheduleCard.tsx").read_text()
    body = page[page.index("async function save()"):]
    body = body[:body.index("\n  }\n")]
    code = "\n".join(line.split("//")[0] for line in body.splitlines())
    assert re.search(r"finally\s*\{\s*setBusy\(false\);\s*\}", code), "a refused save must leave the button usable"


def test_config_page_has_one_editor_per_setting_and_no_auto_saved_review_class_field():
    """Merge regression (G1b × G3): the Config page kept two editors each for channels, officer roles and news keywords,
    and the old per-field auto-save (POST /api/admin/config) applied channel and officer-role changes — review-class
    (D51) — with no review. Channels are ChannelsCard's edit mode, officer roles are in the Settings edit mode, the
    keywords are NewsCard's; only the loot-log switch still saves as it is flipped."""
    page = (SRC / "pages" / "Config.tsx").read_text()
    assert "<ChannelsCard />" in page and "<SetupCard />" in page and "<NewsCard " in page
    assert "set(`channel:" not in page and 'set("officer_roles"' not in page and 'set("news_keywords"' not in page
    assert page.count("<TagsInput") == 1, "one news keywords editor"
    assert page.count('<MultiSelect label="Officer roles"') == 1 and "officer_roles\", \"Officer roles" not in page, "one officer roles editor"
    assert re.findall(r'\bset\("(\w+)"', page) == ["loot_log"], "the only field left on the old auto-save"
