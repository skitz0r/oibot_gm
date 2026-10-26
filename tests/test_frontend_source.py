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
    keywords are NewsCard's; only the loot-log switch and the homepage's listed switch (phase 4, B10 — a reversible
    routine setting, not review-class) save as they are flipped (through useOpsSave, which still reviews a warn-class batch)."""
    page = (SRC / "pages" / "Config.tsx").read_text()
    assert "<ChannelsCard />" in page and "<SetupCard " in page and "<NewsCard " in page
    assert "set(`channel:" not in page and 'set("officer_roles"' not in page and 'set("news_keywords"' not in page
    assert page.count("<TagsInput") == 1, "one news keywords editor"
    assert page.count('<MultiSelect label="Officer roles"') == 1 and "officer_roles\", \"Officer roles" not in page, "one officer roles editor"
    assert re.findall(r'\bset\("(\w+)"', page) == ["loot_log", "public"], "the only fields saved as they are flipped"


def _translator_routes() -> set[str]:
    """The /api/admin/* POST routes that translate into config ops (web/api_ops.py `translate`, web/api_setup.py through
    app.state.ops_translate): their bodies call translate(...)."""
    web = SRC.parents[1] / "src" / "oibot_gm" / "web"
    out = set()
    for f in ("api_ops.py", "api_setup.py"):
        text = (web / f).read_text()
        for m in re.finditer(r'@app\.post\("(/api/admin/[^"]+)"\)\n(.*?)(?=\n    @app\.|\Z)', text, re.S):
            if re.search(r"\btranslate\(", m.group(2)):
                out.add(m.group(1))
    return out


def test_no_page_posts_to_an_admin_translator_route_directly():
    """D51 regression: the Config page's loot-log switch, the Raids page's rules / reset, the schedule editor, Pause and
    Remove and the Recruitment Save posted straight to /api/admin/* translators, so a warn-class change (a schedule
    paused or removed, applications closed, a timezone) skipped the review. Every config edit goes through useOpsSave
    (POST /api/ops/change and the ReviewModal); no frontend source names a translator route in a POST."""
    routes = _translator_routes()
    assert {"/api/admin/config", "/api/admin/raid", "/api/admin/raid/schedule", "/api/admin/recruitment", "/api/admin/channels"} <= routes, routes
    post = re.compile(r"""(?:\.post\s*(?:<[^()]*?>)?\s*\(|fetch\s*\()\s*[`"'](/api/admin/[^`"'?$]+)""")
    hits = []
    for f in sorted(SRC.rglob("*.ts*")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for m in post.finditer(line):
                if m.group(1) in routes:
                    hits.append(f"{f.relative_to(SRC)}:{n}: {m.group(1)}")
    assert not hits, "save config edits through useOpsSave (the review step), not the admin translators:\n" + "\n".join(hits)


# a file whose edit state only mirrors a child's edit mode (the child registers it) — and why
DIRTY_MIRRORS = {"pages/Me.tsx": "its `editing` mirrors CharacterTable's edit mode (onEditing={setEditing}); CharacterTable registers it"}
EDIT_MODE = re.compile(r"useOpsSave\(\)|const \[(?:editing|edit|draft|drafts), set")


def test_every_edit_mode_registers_with_the_guild_switchers_guard():
    """G3 review: the guild switcher's "Save or discard your edits first" (hooks/useDirty) only knew the Channels card,
    so switching servers dropped a draft in Settings, Raids rules, the schedule editor, Auras, Recruitment, Plain
    permissions or the character tables. Every component with an edit mode (useOpsSave, or editing / draft state)
    calls useDirty."""
    missing = []
    for f in sorted(SRC.rglob("*.tsx")):
        rel = str(f.relative_to(SRC))
        if rel in DIRTY_MIRRORS or rel == "components/ReviewModal.tsx":  # ReviewModal defines useOpsSave
            continue
        text = f.read_text()
        if EDIT_MODE.search(text) and "useDirty(" not in text:
            missing.append(rel)
    assert not missing, "edit modes that don't call useDirty (the server switch would drop their draft):\n" + "\n".join(missing)
    for rel in ("pages/Config.tsx", "components/ChannelsCard.tsx", "pages/Raids.tsx", "components/ScheduleCard.tsx", "pages/Auras.tsx",
                "components/Recruitment.tsx", "components/PlainPermissions.tsx", "pages/Members.tsx", "components/CharacterTable.tsx"):
        assert "useDirty(" in (SRC / rel).read_text(), rel
    me = (SRC / "pages" / "Me.tsx").read_text()
    assert "onEditing={setEditing}" in me and "setEditing(true)" not in me, DIRTY_MIRRORS["pages/Me.tsx"]


# A payload field named *_iso / *_at is a machine stamp unless its route formats it (the clock rule: Registry.local12 /
# t12 / clock12 server side, never a 24-hour or ISO value a person reads). The ones below are already words — and why.
STAMP_FIELDS_FORMATTED = {
    "locked_at": "web/api.py run timeline: clock12(reg, ev.locked_at)",
    "reminded_at": "web/api.py run json: t12(reg, ev.reminded_at)",
    "closed_at": "web/api.py run json: t12(reg, ev.closed_at)",
    "pending_at": "policy.policy_state(…, when=reg.local12): the web route passes Registry.local12",
}
# printed: a JSX child `{x.y_at}` (not an attribute value) or a template-literal hole `${x.y_at}`
STAMP_PRINTED = re.compile(r"(?<![=\w])\{\s*[\w.?!]+\.(\w+_(?:iso|at))\s*\}|\$\{\s*[\w.?!]+\.(\w+_(?:iso|at))\s*\}")
DATETIME_INPUT = re.compile(r"datetime-local|DateTimePicker|DateInput")


def test_no_component_prints_a_raw_stamp_field():
    """UI audit: the Host card printed the MCP card's `last_used` (the request log's 24-hour stamp) next to its 12-hour
    label. No component prints a field named *_iso / *_at as text; a datetime-local input's value is for machines."""
    hits = []
    for f in sorted(SRC.rglob("*.tsx")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if DATETIME_INPUT.search(line):
                continue
            for m in STAMP_PRINTED.finditer(line):
                field = m.group(1) or m.group(2)
                if field not in STAMP_FIELDS_FORMATTED:
                    hits.append(f"{f.relative_to(SRC)}:{n}: {field}")
    assert not hits, "print the route's 12-hour label (a *_label field), never the stamp itself:\n" + "\n".join(hits)
    assert STAMP_PRINTED.search("<Text>{d.last_used_iso}</Text>") and STAMP_PRINTED.search("`since ${r.created_at}`")
    assert not STAMP_PRINTED.search("<Input value={d.start_at} />"), "an attribute value is not printed text"


def test_one_host_card_and_no_raw_key_value_dump():
    """UI audit: Config › Overview had TWO Host cards — the Setup card's (the MCP block as raw `key: value` lines, the
    ISO stamp among them) and the summary's. The Setup card prints the summary's Host lines (`host.facts`) and tells the
    page it did (`onHost`), so the Overview leaves its Host section out."""
    setup = (SRC / "components" / "SetupCard.tsx").read_text()
    assert "Object.entries" not in setup and "/api/agents" not in setup and "host.facts" in setup
    assert re.search(r'<Button[^>]*flexShrink: 0[^>]*>[^<]*?Copy</Button>', setup.replace("\n", " ")), "on a phone the Copy button read 'C'"
    page = (SRC / "pages" / "Config.tsx").read_text()
    assert "<SetupCard onHost={setHostCard} />" in page and "skipHost={hostCard}" in page
    assert re.search(r'filter\(\(x\) => !\(skipHost && x\.key === "host"\)\)', page)


def test_character_status_is_one_badge_in_full_words():
    """UI audit: the Status badges read 'CONFIRM…' / 'UNCONFI…' / 'PLAN…' — upper case in a column too narrow. One
    StatusBadge (Cells.tsx) for Me › Characters and Members: lower case, the word never clipped, a fixed-width column."""
    cells = (SRC / "components" / "Cells.tsx").read_text()
    badge = cells[cells.index("export function StatusBadge"):]
    assert 'tt="none"' in badge and 'textOverflow: "clip"' in badge and "flexShrink: 0" in badge
    for f in ("components/CharacterTable.tsx", "pages/Members.tsx"):
        text = (SRC / f).read_text()
        assert "<StatusBadge " in text and ">unconfirmed</Badge>" not in text and ">planned</Badge>" not in text, f
    assert '<Table.Th w={120}>Status</Table.Th>' in (SRC / "components" / "CharacterTable.tsx").read_text()


def test_absence_row_wraps_instead_of_overflowing_the_card():
    """UI audit: on the Me page's side column the row 'Wed 4 Nov → Fri 6 Nov · vacati… · I'm back' ran past the card's
    edge. The row wraps (the actions drop under the days), the reason chip shrinks with its full text on hover, I'm back
    never shrinks."""
    card = (SRC / "components" / "AbsencesCard.tsx").read_text()
    row = card[card.index("absences.map((a) =>"):card.index("</Stack>", card.index("absences.map((a) =>"))]
    assert 'justify="space-between" wrap="wrap"' in row and "title={a.reason}" in row and "flexShrink: 0" in row


def test_settings_card_names_the_owner():
    """UI audit: Config › Settings read "Owner: Discord id 137691328623542272". A person reads a name, never a snowflake."""
    page = (SRC / "pages" / "Config.tsx").read_text()
    assert "Discord id ${" not in page and "st.owner_name" in page
