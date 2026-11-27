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
    page = (SRC / "pages" / "LineupPage.tsx").read_text()
    body = page[page.index("async function saveSchedule()"):]
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
    for rel in ("pages/Config.tsx", "components/ChannelsCard.tsx", "pages/Raids.tsx", "pages/LineupPage.tsx", "pages/Auras.tsx",
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
    StatusBadge (Cells.tsx) for Me › Characters: lower case, the word never clipped, a fixed-width column. (Members marks
    an unconfirmed character with a quiet outline instead — test_members_page_reads_by_its_shape.)"""
    cells = (SRC / "components" / "Cells.tsx").read_text()
    badge = cells[cells.index("export function StatusBadge"):]
    assert 'tt="none"' in badge and 'textOverflow: "clip"' in badge and "flexShrink: 0" in badge
    for f in ("components/CharacterTable.tsx",):
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


def _events_files() -> dict[str, str]:
    """The Events page and its pieces (phase 4: it replaced the Rosters page)."""
    files = {"Events": (SRC / "pages" / "Events.tsx").read_text()}
    for f in sorted((SRC / "components" / "events").glob("*.tsx")):
        files[f.stem] = f.read_text()
    return files


def test_events_page_reads_by_its_shape_and_keeps_the_clock_rule():
    """Was the Rosters page's shape test (design §5.56), now the Events page that replaced it (phase 4, Task 10): the
    list's groups are Open · Coming up · History, each event's dates come from the server (Clock rule: no two-digit day
    anywhere a person reads, a picker's format is the house one), and amber marks nothing on it."""
    ev = _events_files()
    page = _code_only(ev["Events"])
    assert re.findall(r'label: "([^"]+)", items:', page) == ["Open", "Coming up", "History"]
    assert "Sparkles" not in "".join(ev.values()) + (SRC / "components" / "board" / "BoardView.tsx").read_text(), "a plain icon, not one that reads as AI"
    for f in SRC.rglob("*.tsx"):
        text = f.read_text()
        assert 'day: "2-digit"' not in text, f"{f.name}: a two-digit day breaks the Clock rule"
        assert not re.search(r"toLocale(?:Date|Time)?String\(", _code_only(text)), f"{f.name}: a time the browser composed, not the server's words"
        for fmt in re.findall(r'valueFormat="([^"]+)"', text):
            assert "DD" not in fmt and "HH" not in fmt, f"{f.name}: {fmt}"
    board = [(SRC / "components" / "board" / n).read_text() for n in ("BoardView.tsx", "SheetCard.tsx", "FillModal.tsx", "shared.tsx")]
    for f in [*ev.values(), *board]:
        assert not re.search(r'(?:color|c)=\{?"yellow', f), "amber marks a value changed from the default; the event has none"


def test_test_runs_live_on_the_test_bench_never_in_the_events_list():
    """The Rosters page's per-raid Test tab is gone (phase 4, Task 10): the rehearsals' cards — the whole SheetCard, its
    lock, fill and remind — are on the Members page's Test bench, each linking its event page; the Events list never
    holds one (event_view.listed), and Try a comp lands on the test run's event."""
    bench = _code_only((SRC / "components" / "TestBench.tsx").read_text())
    assert "<SheetCard" in bench and "e.test" in bench and '"/api/rosters"' in bench
    assert "navigate(`/events/${" in bench and "navigate(`/rosters" not in bench and "#run-" not in bench
    assert "lives on Rosters" not in bench


def test_answer_for_someone_offers_only_members_without_an_answer():
    """The owner, 2026-11-22: someone with a recorded answer (Join, Bench or No thanks) is changed from their row's menu.
    On the Events page the list is the server's (event_json `people`: members with no answer), passed as it comes; the
    Test bench's SheetCard keeps its own filter."""
    sign = _code_only((SRC / "components" / "events" / "SignupsSection.tsx").read_text())
    assert "<SomeoneElse run={ev.key} people={ev.people}" in sign
    card = (SRC / "components" / "board" / "SheetCard.tsx").read_text()
    assert "<SomeoneElse run={e.key} people={people.filter((p) => !signups.some((s) => s.uid === p.uid))}" in card


def _code_only(text: str) -> str:
    """The file without its comments (// … and /* … */, JSX {/* … */} included) — a comment may name a word, code may not."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"(?<![:\"'`])//.*$", "", line) for line in text.splitlines())


def test_the_run_vocabulary_has_no_copy_in_the_frontend():
    """Design §5.56 phase 2: every state word, confirmation mark, ask kind, answer word and run verb's label is defined
    ONCE in Python (constants / raidcycle / raid_buttons.RUN_WORDS) and reaches the site through /api/meta or the run's
    JSON — the pages print meta.run_states / run_labels / marks / ask_kinds / ask_answers / run_words, never a literal."""
    import sys

    sys.path.insert(0, str(SRC.parents[1] / "src"))
    from oibot_gm import raidcycle as rc
    from oibot_gm.constants import MARK, MARK_WORDS
    from oibot_gm.raid_buttons import RUN_WORDS

    # the Rosters page's place is the Events page's (phase 4, Task 10): the page, its sections, the lineup editor
    files = [SRC / "pages" / "Events.tsx", SRC / "pages" / "LineupPage.tsx", SRC / "pages" / "Me.tsx", SRC / "components" / "MyAnswer.tsx", SRC / "components" / "RaidCard.tsx",
             *sorted((SRC / "components" / "schedules").glob("*.tsx")), SRC / "pages" / "Schedules.tsx", *sorted((SRC / "components" / "events").glob("*.tsx")), *sorted((SRC / "components" / "board").glob("*.tsx"))]
    words = ([w for k, w in RUN_WORDS.items() if k not in ("ask", "loot", "lock", "locked", "reopen")]  # short verbs other screens share
             + [w for w in rc.RUN_STATE_WORDS.values() if " " in w] + ["finished"]
             + [w for w in rc.ASK_HOW.values() if " " in w] + [w for k, w in rc.ASK_ANSWER_WORDS.items() if k in ("yes", "no", "expired")]
             + list(rc.BANK_WORDS.values()) + [rc.NO_CONFIRMATIONS, rc.FIRST_COME, rc.LEFTOVERS, "Join is the commitment", "autofill paused"]
             + [MARK_WORDS[k] for k in ("yes", "expired")])
    marks = [m for m in MARK.values()] + ["✓", "✗"]
    hits = []
    for f in files:
        code = _code_only(f.read_text())
        for w in words:
            if re.search(r"[\"'`>]\s*" + re.escape(w) + r"\s*[\"'`<]", code):
                hits.append(f"{f.name}: {w!r}")
        for m in marks:
            if m in code:
                hits.append(f"{f.name}: mark {m!r}")
    assert not hits, "a copy of the server's words in the page:\n" + "\n".join(hits)
    card = (SRC / "components" / "board" / "SheetCard.tsx").read_text()
    for field in ("meta.run_states", "markOf(meta", "run_labels", "run_words", "ask_kinds", "ask_answers"):
        assert field in card or field in (SRC / "components" / "board" / "shared.tsx").read_text(), field
    assert "s.badge" in (SRC / "components" / "MyAnswer.tsx").read_text(), "Me's badge is the server's words (rc.answer_word)"
    assert "r.words" in (SRC / "components" / "schedules" / "RegularsSection.tsx").read_text() and "<LevelChip" in (SRC / "components" / "raids" / "ReadinessView.tsx").read_text()
    more = (SRC / "components" / "events" / "EventMore.tsx").read_text()
    assert "meta.run_labels" in more, "the event's More menu names each verb as Discord's buttons do"


def test_the_board_draws_no_text_beside_icons():
    """Design §5.56 phase 2 (the Iconography rule): no "totems: Earth SoE" line under a group, no "3 tanks (max 2)" beside
    the count, no "synergy 41" / "gap 3" numbers, no "pinned" / "bench" badge (a pin icon with its tooltip), and the
    still-short line is role icons + numbers from the plan's data — never the plain-text sentence."""
    board = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    preview = _code_only((SRC / "components" / "board" / "BoardPreview.tsx").read_text())
    fill = _code_only((SRC / "components" / "board" / "FillModal.tsx").read_text())
    shared = _code_only((SRC / "components" / "board" / "shared.tsx").read_text())
    split = _code_only((SRC / "components" / "board" / "SplitModal.tsx").read_text())
    card = _code_only((SRC / "components" / "board" / "SheetCard.tsx").read_text())
    assert "totems:" not in board + preview and "picks.join" not in board + preview
    assert "(max ${" not in board and "synergy" not in shared + split and "gap {" not in split
    assert "plan.still_short" not in fill and "short_after" in fill and "const KIND" not in fill
    assert '"pinned" : "bench"' not in card and "<PinMark" in card and "<PinMark" in board
    assert "Cancel run</Button>" not in card and "Post a line in the signup channel" not in card and "Tell the channel — a fixed line, no reason" in card


def test_the_event_confirms_print_only_the_servers_sentence():
    """Design §5.56 phase 3 (findings 32–34), carried from the Rosters page to the Events page that replaced it (phase 4,
    Task 10): every confirm prints the server's sentence (ev_json.consequences, End's preview, a regular's `remove`, an
    orphan's `clear`, a §7b box) — no template literal or string literal composes a consequence in BoardView / SheetCard
    / the event's More menu / the regulars, and no literal stands in when the server's is missing. The board's sentences
    only get the names put in (shared.fillNames)."""
    files = {n: _code_only((SRC / p).read_text()) for n, p in (("BoardView", "components/board/BoardView.tsx"), ("SheetCard", "components/board/SheetCard.tsx"),
                                                                ("EventMore", "components/events/EventMore.tsx"), ("Regulars", "components/schedules/RegularsSection.tsx"),
                                                                ("Schedules", "pages/Schedules.tsx"),
                                                                ("Events", "pages/Events.tsx"), ("LineupPage", "pages/LineupPage.tsx"))}
    for name, code in files.items():
        assert not re.search(r"message:\s*[`\"']", code), f"{name}: a confirm message written in the browser"
        assert not re.search(r"consequences?\??\.\w+\s*\|\|\s*[`\"']", code), f"{name}: a browser sentence standing in for the server's"
        assert not re.search(r"sentences:\s*\[\s*[`\"']", code), f"{name}: a box sentence written in the browser"
    assert "e.consequences?.remind" in files["SheetCard"] and "message: preview," in files["SheetCard"]
    # the final review: Remind and End print the SERVER's box (remind_box, end_box) and press with its fp
    assert "boxed(`/api/run/${encodeURIComponent(s.key)}/remind`)" in files["EventMore"] and "boxed(`/api/loot/${encodeURIComponent(s.key)}/end`)" in files["EventMore"]
    assert "{ seen: fp }" in files["EventMore"] and "{ preview: true }" in files["EventMore"] and "title:" not in files["EventMore"]
    assert "useConfirm(" not in files["EventMore"], "the event page's box, never the older modal"
    assert "Reopen works for an hour" not in files["SheetCard"] + files["EventMore"] and "gets a confirmation DM" not in files["BoardView"]
    # the regulars (phase 4, Task 11): each row's and each leftover's Remove prints its server box and sends the record's rev
    assert "box: x.box" in files["Regulars"] and "box: o.box" in files["Regulars"] and "rev: shown.rev" in files["Regulars"] and "seen: fp" in files["Regulars"]
    shared = _code_only((SRC / "components" / "board" / "shared.tsx").read_text())
    assert 'split("{name}").join(names.name)' in shared


def test_the_events_more_menu_keeps_every_raid_night_lever():
    """Phase 4, Task 10 (the capability inventory): what the Rosters page's card offered on a raid night and the approved
    mockup does not draw stays on the event, in one compact More menu in its header — Fill seats / Suggest (the fill
    screen), Remind, Pause / Resume automatic asks, Re-post the sheet, the loot council, End, the asks out (Withdraw) and,
    on a finished event, Reopen — each per raid from the run's own JSON (part.sheet), greyed with the server's reason
    when it can't, and asking first with the server's sentence where the Rosters card did."""
    more = _code_only((SRC / "components" / "events" / "EventMore.tsx").read_text())
    for needle in ("<FillModal", "<RepostSheetModal", "/remind`", "/pause`", "/api/loot/${", "/end`", "/reopen`", "<Detail e={{ ...asks, log_rows: [], log: [] }}",
                   "s.may_remind", "s.remind_note", "s.may_end", "s.end_note", "s.may_reopen", "s.loot?.may_open", "classNames={{ item: css.menuItem }}"):
        assert needle in more, needle
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "<EventMore" in page and "Edit lineup" in page and "/lineup`" in page
    hist = _code_only((SRC / "components" / "events" / "HistoryView.tsx").read_text())
    assert "<EventMore" in hist, "Reopen an end by mistake, as the Rosters page's history drawer did"


def test_move_to_posts_the_same_move_a_drag_does():
    """Finding 38: each board chip's ⋮ menu has Move to… (every group with room, and Not placed / Not rostered) — a tap or a
    keyboard, no drag needed. Both go through ONE function (`place`) and one `send("move", …)`, so Move to… posts the very
    body a drag does ({member, to: {r, g, i}, state}), with the same rev / state handling."""
    board = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    assert board.count('send("move"') == 1, "one move body for both paths"
    assert 'place(d.name, d.from, target === "bank" ? null : target, "drag")' in board
    assert 'place(name, from, { r: ri, g: gi, i: g.length }, "menu")' in board and 'place(name, from, null, "menu")' in board
    assert "moves={movesFor(s.display_name, from)}" in board and "moves?: MoveTarget[]" in menu and "onClick={m.onPick}" in menu
    assert 'send("autofill", { rev: e.rev, wait: true })' in board, "Place the rest carries the board's rev and waits for the board (the route answers it)"


def test_event_controls_are_44px_on_a_phone():
    """Finding 40, carried to the Events page (phase 4, Task 10): one phone-only rule makes every control at least 44 px
    (the ⋮ buttons, compact buttons, the sort control, the tabs) — the board's rule moved with the board to
    components/board/board.module.css, the list pages' .touch wraps the Events page and the lineup editor — and the menus
    (portals) take .menuItem, never per-element sizes."""
    assert not (SRC / "pages" / "rosters.module.css").exists()
    for path in ("components/board/board.module.css", "pages/listpage.module.css"):
        css = (SRC / path).read_text()
        rule = css[css.index("@media (max-width: 48em)"):]
        assert "min-height: 44px; min-width: 44px" in rule and ".mantine-ActionIcon-root" in rule and ".mantine-SegmentedControl-label" in rule and ".menuItem" in rule, path
    assert "className={css.touch}" in (SRC / "components" / "ListPage.tsx").read_text()
    assert "className={css.touch}" in (SRC / "pages" / "LineupPage.tsx").read_text()
    for f in ("components/board/MemberMenu.tsx", "components/board/SheetCard.tsx", "components/events/EventMore.tsx"):
        assert "classNames={{ item: css.menuItem }}" in (SRC / f).read_text(), f
    for f in ("BoardView.tsx", "BoardPreview.tsx", "SheetCard.tsx", "MemberMenu.tsx"):
        assert 'from "./board.module.css"' in (SRC / "components" / "board" / f).read_text(), f


def _members_files() -> dict[str, str]:
    return {n: _code_only((SRC / p).read_text()) for n, p in (("Members", "pages/Members.tsx"), ("MemberEdit", "components/members/MemberEdit.tsx"),
                                                                ("PeopleDialogs", "components/members/PeopleDialogs.tsx"))}


def test_members_page_reads_by_its_shape():
    """Design §5.58 phase 2: one row per member (no table with a row per character, no sticky member cell, no sideways
    scroll container, no guild-wide edit grid, no live rank select), tabs Members · Absences · Test bench with the
    #test-bench anchor kept, count chips that filter, a search box, Confirm characters… as a walk, the folds for who is
    not in the server / has no character yet, LeftServer gone (its content is the fold). Lower case badges, no amber."""
    f = _members_files()
    page = f["Members"]
    tabs = re.findall(r'<Tabs\.Tab value="(\w+)"', page)
    assert tabs == ["members", "absences", "test"], tabs
    assert '"#test-bench"' in page and "<TestBench " in page and "<AbsencesTable " in page
    assert "<Table" not in page and "STICKY" not in page and "position: \"sticky\"" not in page and "ScrollContainer" not in page and "minWidth={1" not in page
    assert "/api/members/save" not in page, "the page never posts the whole guild; one member is edited in place (MemberEdit)"
    assert "<Select" not in page, "no live select in a row: Rank… is a dialog from the member's ⋮"
    assert not (SRC / "components" / "LeftServer.tsx").exists() and "/api/admin/left" not in page and "/api/members/attendance/all" not in page
    assert "Not in the Discord server" in page and "No character yet" in page and "r.left_line" in page and "r.remove_consequence" in page
    assert 'aria-label="Find a member or a character"' in page and "Confirm characters…" in page and "<ConfirmWalk " in page
    for f_ in ("away", "confirm", "left", "bare"):
        assert f'chip("{f_}"' in page, f_
    assert 'tt="none"' in page and 'tt="uppercase"' not in page
    for name, code in f.items():
        assert not re.search(r'(?:color|c)=\{?"(?:yellow|orange)', code), f"{name}: amber marks a changed value; Members has none"
    assert "yellow-5" not in page
    assert "useDirty(" in f["MemberEdit"] and "useDirty(" in page


def test_members_confirms_print_only_the_servers_sentences():
    """Design §5.58 (findings 2, 3): every press on the Members page previews through the server (POST /api/members/set,
    /api/members/dm, /api/members/save with `preview`) and the confirm prints ONLY its `sentences` (or Remove record's
    `remove_consequence`); no literal message, no browser-composed promise of what the bot will do, no rank list kept in
    the browser (the payload's rank_choices) and the own-record reasons are the server's (`own`)."""
    f = _members_files()
    for name, code in f.items():
        assert not re.search(r"message:\s*[`\"'\[]", code), f"{name}: a confirm message written in the browser"
        assert "It leaves sheets and rosters" not in code and "alts always read alt" not in code and "The rank (" not in code
    page = f["Members"]
    assert 'message: p.sentences.join("\\n")' in page and "preview: true" in page and "message: r.remove_consequence" in page
    assert '"/api/members/dm"' in page and '"/api/members/set"' in page
    assert "rank_choices" in page and '"alt"' not in page and "own.rank" in page and "own.confirm" in page
    edit = f["MemberEdit"]
    assert 'message: said.join("\\n")' in edit and "body(true)" in edit and "rev: r.characters_rev" in edit
    dialogs = f["PeopleDialogs"]
    assert dialogs.count("preview: true") == 2 and "said.sentences.map" in dialogs and "rev: said.rev" in dialogs
    assert "r.attendance" in page and "att " not in page


def test_members_controls_are_44px_on_a_phone_and_cards_below_sm():
    """Findings 4, 19, 21: below the sm breakpoint every member is a card (grid areas name · menu / standing / characters),
    nothing scrolls sideways, and one phone-only rule makes every control 44 px — the ⋮ buttons, buttons, tabs, inputs,
    the folds and the portal menus' items (.menuItem)."""
    css = (SRC / "pages" / "members.module.css").read_text()
    phone = css[css.index("@media (max-width: 48em)"):]
    assert 'grid-template-areas: "name menu" "standing standing" "chars chars"' in phone and ".head { display: none; }" in phone
    assert "min-height: 44px; min-width: 44px" in phone and ".mantine-ActionIcon-root" in phone and ".mantine-Tabs-tab" in phone and ".menuItem { min-height: 44px; }" in phone
    page = (SRC / "pages" / "Members.tsx").read_text()
    assert "className={css.touch}" in page and page.count("classNames={{ item: css.menuItem }}") == 2
    assert "overflow-x" not in css and "min-width: 0" in css


def test_test_bench_reads_by_its_shape():
    """Design §5.58 phase 3 (findings 22–26, 31): ONE Rehearse with raid picker for Try a comp and the test run alike,
    defaulting to the server's first raid (remembered per browser), never a browser pick like "the 20-player raid"; the
    seed labels and the tempo's week are the server's (RunStrip over `tempos[].week`, no line assembled here); a why line
    under greyed buttons; sentence case; the feed names each run by its title, never its key, and wraps on a phone."""
    bench = _code_only((SRC / "components" / "TestBench.tsx").read_text())
    assert bench.count('aria-label="Rehearse with"') == 1 and bench.count("meta.raids.map(") == 1, "one raid picker"
    assert "size === 20" not in bench and "meta.raids[0]" in bench and "localStorage" in bench
    assert "<RunStrip steps={t.week} still />" in bench and "Starts in" not in bench and "nudge " not in bench
    assert "{s.label}" in bench and "all ${" not in bench and "of up to {MAX}" in bench
    assert "<Why>" in bench and "state.open_refusal ? <Why>{state.open_refusal}</Why>" in bench
    assert "<Eyebrow>" not in bench and "<Eyebrow plain>" in bench and not re.search(r"message:\s*[`\"'\[]", bench)
    assert "meta.run_states[" in bench
    feed = _code_only((SRC / "components" / "TestFeed.tsx").read_text())
    assert "{e.run} ·" not in feed and "e.title" in feed and "w={150}" not in feed and 'wrap="wrap"' in feed and "<Eyebrow plain>" in feed
    strip = (SRC / "components" / "RaidCard.tsx").read_text()
    assert "still = false" in strip


def test_absences_and_the_me_page_read_the_servers_words():
    """Findings 29, 30, 33 and 17's Me half: the absences tab stacks on a phone (no table scrolling sideways), its empty
    state says how to fill it, I'm back is a real button and "away now" a grey word; the absence modal is "Record an
    absence for <name>" with "Add the absence" and only the server's sentences above it; the Me page's Retire, Delete,
    Make main and Save print ONLY the server's sentences (CharacterTable composes none); one intro line on Me."""
    table = _code_only((SRC / "components" / "AbsencesTable.tsx").read_text())
    assert "<Table" not in table and "minWidth" not in table and "css.absRow" in table and "className={css.touch}" in table
    assert "Nobody is away. " in table and "Record an absence from a member's ⋮" in table and 'color="yellow"' not in table
    assert '<Button size="sm" variant="light"' in table and ">away now</Text>" in table
    css = (SRC / "pages" / "members.module.css").read_text()
    assert 'grid-template-areas: "who back" "when when" "reason reason" "by by"' in css[css.index(".absHead"):]
    modal = _code_only((SRC / "components" / "AbsencesModal.tsx").read_text())
    assert "Record an absence for ${who.display_name}" in modal and "Add the absence" in modal and "touches" not in modal
    assert "preview.sentences" in modal and "No live sheet falls" not in modal and "preview.lines" not in modal
    chars = _code_only((SRC / "components" / "CharacterTable.tsx").read_text())
    assert not re.search(r"message:\s*[`\"'\[]", chars) and chars.count("preview: true") == 2 and chars.count("sentencesOf(p)") == 2
    for said in ("It leaves sheets and rosters", "Deleting removes it outright", "Nothing else changes", "your next character becomes main", "p.ripple"):
        assert said not in chars, said
    assert 'tt="uppercase"' not in chars and "yellow" not in chars
    card = (SRC / "components" / "AbsencesCard.tsx").read_text()
    assert "None upcoming" not in card and "No absences on file. Add one if you'll miss a raid." in card
    me = _code_only((SRC / "pages" / "Me.tsx").read_text())
    assert '.join(" · ")' not in me[me.index("const summary"):me.index("if (!me) return <Text")]


def test_ops_page_listening_column_and_recent_decisions():
    """Listening on the Ops page (design §5.59 phase 2): a Listening column (a Watches / Replies badge, a Select Off ·
    Watches · Replies in edit mode) sent as `listen` in the one plain change; the hint and the no-key line in the
    glossary's voice; Recent decisions with the did-words, reloading on the `listen` topic; a card per verdict below sm."""
    src = (SRC / "components" / "PlainPermissions.tsx").read_text()
    assert "<Table.Th w={140}>Listening</Table.Th>" in src
    assert '{ value: "off", label: "Off" }' in src and "listen_words" in src
    assert "listen: d.listen" in src, "listening rides the same plain change as the rest of the table"
    assert ("Listening: the bot reads these channels and answers what is clearly for it, without a tag. "
            "Watches only notes what it would have answered.") in src
    assert "Listening needs the host's TypeSafe key." in src and "listen_ready === false" in src
    tab = (SRC / "components" / "ListenDecisions.tsx").read_text()
    ops = (SRC / "pages" / "Ops.tsx").read_text()
    assert "Recent decisions" in tab and '["listen", "config"]' in tab and "Recent decisions" not in src, "the decisions live on the Listening tab"
    assert 'value="listening">Listening' in ops and 'value="permissions">Plain-text permissions' in ops and 'value="actions">Recent actions' in ops
    assert '"#listening"' in ops, "the tab is linkable"
    for word in ("Replied", "Asked with 👀", "Stayed quiet", "Would have replied", "Would have asked", "Skipped"):
        assert word in tab
    assert "<Progress" in tab and "% likely for the bot" in tab and "not judged" in tab, "a skipped message says so, never a bare dash"
    assert 'import css from "./plain.module.css"' in tab
    for bad in ("classifier", "threshold", "shadow", "Jev", "model"):
        assert bad not in src.split("// ---- shapes", 1)[1].replace("PlainPermissionsData", ""), bad
        assert bad not in tab.replace("PlainPermissionsData", ""), bad
    css = (SRC / "components" / "plain.module.css").read_text()
    phone = css.split("@media (max-width: 48em)", 1)[1]
    assert ".head { display: none; }" in phone and "grid-template-areas" in phone and "border-radius: 8px" in phone
    assert "overflow-x" not in css and "minmax(0," in css, "nothing scrolls sideways"


def test_professions_line_and_editor_on_me_and_members():
    """Professions (design §5.60): one line under each character — a profession icon with its skill beside it (no recipes on
    the row since 2026-11-13), or the empty state in words; Professions… in the character ⋮ on Me and Members; the editor posts a
    preview first and prints only the server's sentences; skill is a number input 1..skill_cap."""
    line = (SRC / "components" / "ProfessionLine.tsx").read_text()
    assert 'kind="profession"' in line
    assert "Badge" not in line and "notable" not in line, "no recipe chips on the row: icons and skill (the tooltip: specialization, count)"
    assert "p.specialization" in line and "p.recipes.length" in line
    assert "no professions on record — add them with Professions… in this row's ⋮" in line
    assert "no professions on record — ${member} adds them on the Me page`" in line and "/me professions" not in line
    ed = (SRC / "components" / "ProfessionsEditor.tsx").read_text()
    assert "preview: true" in ed and "sentences" in ed and "NumberInput" in ed and "skill_cap" in ed and "trade_slots" in ed
    assert "A gathering trade has no recipes." in ed and "has no recipes.`" in ed
    # the site knows Fishing makes nothing (meta.professions.list[].makes), never by group
    for src in (ed, (SRC / "pages" / "Professions.tsx").read_text()):
        assert "makes" in src and 'group !== "gathering"' not in src
    assert 'group !== "gathering"' not in line and "d?.makes" in ed  # the row shows no recipes, so it asks nothing about them
    # a cleared skill box stays empty while typing; the server refuses an out-of-range skill (never clamped to 1)
    assert 'clampBehavior="strict"' not in ed and "|| 1" not in ed
    me = (SRC / "components" / "CharacterTable.tsx").read_text()
    mem = (SRC / "pages" / "Members.tsx").read_text()
    assert "<ProfessionLine" in me and "Professions…" in me and "<ProfessionLine" in mem and "Professions…" in mem
    assert "member={r.display_name}" in mem, "the Members empty state names who fills it"
    icons = (SRC / "components" / "Icons.tsx").read_text()
    assert '"profession"' in icons


def test_professions_page_is_the_finder_with_officer_maintenance():
    src = (SRC / "pages" / "Professions.tsx").read_text()
    app = (SRC / "App.tsx").read_text()
    assert 'path="/professions"' in app and '{ to: "/professions", label: "Professions"' in app
    assert "/api/professions/find" in src and "Who can make what" in src
    assert "Nobody has registered" in src and "no recipes on record — the crafter adds them on the Me page" in src
    assert 'useLive(load, ["member", "config", "prices"])' in src
    # armory link §4.3–4.4 / design §5.64: recipes are the game's — Rename… / Merge… are retired, the page prints the server's
    # sentences (the empty answer, "Nobody in the guild makes … yet.", "And N more recipes match.") and never composes them
    assert "Rename…" not in src and "Merge…" not in src and "/api/professions/rename" not in src and "/api/professions/merge" not in src
    assert "registered by" not in src and "No registered recipe matches" not in src
    assert "found.message" in src and "r.nobody" in src and "found.more" in src
    assert "r.category" in src and "makesWords(r.makes)" in src and "r.reagents" in src
    # officers' card: the recipes not in the game's list, Pick the recipe… (a search of that profession) / Remove, preview first
    assert "not in the game's list" in src and "Pick the recipe…" in src and ">Remove<" in src
    assert '"/api/professions/unmatched", { ...payload, preview: true }' in src and "p.sentences" in src and "useConfirm" in src
    assert "<RecipePicker profession={pick.profession}" in src
    # the tabs: with the game's list, what the guild makes grouped by category
    assert "<GuildMakes" in src and "What the guild makes" in src
    from test_copy import tsx_strings  # the copy lint's own extractor of what a person reads
    for f in ("pages/Professions.tsx", "components/ProfessionsEditor.tsx", "components/RecipePicker.tsx", "components/ProfessionLine.tsx"):
        reads = [s for _, s in tsx_strings(SRC / f)]
        assert not [x for x in reads if re.search(r"\b(?:unmatched|sync|slug)\b", x, re.I)], f  # the Copy rule: "not in the game's list"
    css = (SRC / "pages" / "professions.module.css").read_text()
    phone = css.split("@media (max-width: 48em)", 1)[1]
    assert ".fixRow { grid-template-columns: minmax(0, 1fr);" in phone and "border-radius: 8px" in phone and ".makeRow { grid-template-columns: minmax(0, 1fr); }" in phone


def test_professions_editor_picks_from_the_game_list():
    """design §5.64: with the game's list (meta.professions.recipe_list) the editor's Add box is a search over THAT profession's
    recipes (RecipePicker → /api/professions/find?profession=), a pick sets the recipe's id, twins are told apart by
    category; a recipe the list lacks arrives unmatched, is marked, and goes back untouched; without the list it is typed."""
    ed = (SRC / "components" / "ProfessionsEditor.tsx").read_text()
    assert "meta.professions?.recipe_list" in ed and "<RecipePicker profession={p.id}" in ed and "{ id: r.id, name: r.name }" in ed
    assert "{!fromList && (" in ed and "<TextInput" in ed, "without the list the typed entry stays"
    assert "r.unmatched" in ed and "not in the game's list" in ed and "useDirty(" in ed
    picker = (SRC / "components" / "RecipePicker.tsx").read_text()
    assert "/api/professions/find?q=" in picker and "&profession=" in picker and "r.category" in picker
    assert "value: String(r.id)" in picker, "a pick is the recipe's id (twins share a name)"
    assert "current?.message" in picker and "current.more" in picker
    # task 5 review: Mantine's Select resets a controlled search to "" on mount (value={null}); the picker ignores an empty
    # reset until the person has typed, so Pick the recipe… opens on the typed name (`initial`)
    assert "useState(initial || \"\")" in picker and "if (!v && !typed.current) return;" in picker and "onSearchChange={onSearch}" in picker
    # an answer counts only for the (profession, query) on screen; a failed search says so instead of "Searching…" forever
    assert "`${profession}\\n${search.trim()}`" in picker and "found.key === key" in picker and "failed ? SEARCH_FAILED" in picker
    page = (SRC / "pages" / "Professions.tsx").read_text()
    assert '"${found.q.trim()}"' in page, "the search card's title names the query its count answers"
    line = (SRC / "components" / "ProfessionLine.tsx").read_text()
    assert "r.unmatched" not in line, "the row shows no recipes (2026-11-13): the officers' card and the editor mark an unmatched one"


def test_memory_surfaces_read_by_their_shape():
    """The Memory column sits beside Listening in the permissions table and is sent in the one plain change; the Ops page has a
    Memory tab; the Me card shows own rows with Forget previewing the server's sentence; no literal 'embedding' / 'vector' words."""
    pp = (SRC / "components" / "PlainPermissions.tsx").read_text()
    assert "<Table.Th w={140}>Listening</Table.Th><Table.Th w={190}>Memory</Table.Th>" in pp
    assert "remember: d.remember" in pp and "memoryWords[m] || m" in pp, "the select's words come from the server (memory_words)"
    assert "Turn on Listening first." in pp and "Memory needs the host's TypeSafe key." in pp
    ops = (SRC / "pages" / "Ops.tsx").read_text()
    assert '<Tabs.Tab value="memory">Memory</Tabs.Tab>' in ops and 'memory: "#memory"' in ops
    m = (SRC / "components" / "Memories.tsx").read_text()
    assert '"/api/memories' in m and "/forget" in m and "preview: true" in m and 'useLive(load, ["memory", "config"])' in m
    assert "The bot remembers nothing yet." in m
    assert "&offset=${offset}&limit=${limit}" in m and "page(data.rows.length, PAGE)" in m and "[...data.rows, ...p.rows]" in m, \
        "Show more pages by offset and appends (the server caps one page at 200)"
    assert "The bot is still getting ready to search memories by meaning. Until then it finds them by their words." in m
    assert "The bot couldn't get ready to search memories by meaning, so it finds them by their words. Tell the host." in m
    assert "memory model" not in m
    my = (SRC / "components" / "MyMemories.tsx").read_text()
    assert "What the bot remembers you saying" in my and "/api/me/memories/" in my and "preview: true" in my and "Nothing remembered yet." in my
    me = (SRC / "pages" / "Me.tsx").read_text()
    assert "me.memory_on && <MyMemories" in me
    for f in (pp, m, my):
        assert not re.search(r"\b(embedding|vector|cosine|judge|RRF|index)\b", f, re.I)


def test_me_page_sheet_rows_take_the_sheet_title_deep_link():
    """Sheets posted before phase 5 link /app/me#run-<key> directly: the Me page gives each sheet row that id and scrolls
    to the hash once the record has loaded."""
    me = (SRC / "pages" / "Me.tsx").read_text()
    row = (SRC / "components" / "MyAnswer.tsx").read_text()  # the row (MyAnswer) the page draws per sheet
    assert 'id={`run-${s.key}`}' in row and "scrollMarginTop: 72" in row  # the row lands below the phone header, not under it
    assert 'window.location.hash.startsWith("#run-")' in me and "scrollIntoView" in me
    assert "window.history.replaceState" in me  # once: the hash goes, so the poll's reload never jumps back
    assert "flash === s.key" in me  # the row is lit for a moment so the eye lands on it


def test_an_event_link_opens_the_member_event_view():
    """Every Discord link to an event opens /app/events/<key>?g=<guild> (raid_views.event_url). The route is not
    officer()-wrapped: an officer gets the Events page, a member who is not one the members' event view (MemberEvents) —
    never a redirect to the Me page any more. Old /me#run-<key> links still land on the Me page's row."""
    app = _code_only((SRC / "App.tsx").read_text())
    assert '<Route path="/events/:key" element={<EventRoute meta={meta} />} />' in app
    route = app[app.index("function EventRoute("):]
    route = route[:route.index("\n}\n")]
    assert "if (meta.viewer.officer) return <EventsPage meta={meta} />;" in route
    assert "<MemberEventsPage" in route
    assert 'pathname: "/me"' not in route and "Navigate" not in route
    events = app[app.index('<Route path="/events" '):]
    events = events[:events.index("\n")]
    assert "<EventRoute meta={meta} />" in events and "officer(" not in events, "a member opens /events too"


def test_a_member_has_events_in_the_rail_and_an_officer_has_it_once():
    """The members' rail gains Events after Me for a viewer who is not an officer; an officer keeps only the Officers
    section's Events, so the rail never shows two."""
    app = _code_only((SRC / "App.tsx").read_text())
    assert 'const MEMBER_EVENTS = { to: "/events", label: "Events", icon: IconCalendarEvent };' in app
    assert "!meta.viewer.officer ? [NAV[0], MEMBER_EVENTS, ...NAV.slice(1)] : NAV" in app
    officer = app[app.index("const OFFICER = ["):]
    officer = officer[:officer.index("];")]
    assert officer.count('to: "/events"') == 1
    start = app.index("const NAV = [")
    assert '"/events"' not in app[start:app.index("\n", start)], "NAV itself stays the same for everyone"


def test_the_member_event_page_reads_only_member_routes():
    """The members' event view reads Task 1's member routes alone and draws none of the officers' controls."""
    page = _code_only((SRC / "pages" / "MemberEvents.tsx").read_text())
    assert "/api/me/events" in page and "/api/me/event/" in page
    for w in ("/api/event/", "/api/event-list", "/api/run/", "MemberMenu", "EventBoard", "RoleSwitches", "ActivitySection"):
        assert w not in page, w
    assert "export function MemberEventsPage(" in page
    assert 'useLive(load, ["run:", "member"])' in page and "usePoll(load)" in page
    assert "replace: true" in page, "a later raid's key moves the address to its event's own"
    assert "page_words.answer_needs_character" in page and "list.empty" in page, "the words are the server's"


def test_the_member_event_page_uses_the_shared_pieces():
    page = _code_only((SRC / "pages" / "MemberEvents.tsx").read_text())
    for name in ("MyAnswer", "SignupTabs", "MemberRoster", "AskCard", "EventHead", "EventDetails", "ListPage"):
        assert f"<{name}" in page and re.search(rf"import \{{[^}}]*\b{name}\b", page), name
    tabs = page[page.index("<SignupTabs"):]
    tabs = tabs[:tabs.index("/>")]
    assert '"away"' in tabs and '"none"' not in tabs and "right=" not in tabs


def test_me_rows_link_to_their_event():
    """The Me page's rows link to the event's page; the row keeps its #run-<key> anchor for older links."""
    me = _code_only((SRC / "pages" / "Me.tsx").read_text())
    row = me[me.index("<MyAnswer "):]
    row = row[:row.index("/>")]
    assert " link" in row


def test_a_list_item_without_an_address_is_not_a_link():
    """ListPage: a Coming up night has no page of its own for a member, so its row is plain text, not a link."""
    lp = _code_only((SRC / "components" / "ListPage.tsx").read_text())
    assert "to?: string" in lp
    assert re.search(r"it\.to \?\s*\(?\s*<a ", lp) and "<div key={it.id}" in lp


def test_follow_on_surfaces_exist():
    """Design §5.62 (D2): the site sets a follow-on — the schedule editor's
    Then row and the ad hoc Open's Then field — and the Me row offers leaving early and I'm staying with the server's sentence only.
    Phase 3: the schedule editor's Then block holds up to three later raids and sends the whole lineup (never `follow_raid`)."""
    sched = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    editor = sched[sched.index("function ScheduleEditor("):]
    assert "follow_raid" not in sched and "<LineupBuilder" in editor and "fields.lineup = " in editor and "max={opts.lineup_max}" in editor
    # phase 4, Task 7: the ad hoc Open's Then field became the New event page's lineup builder (later raids as `lineup`)
    lineup = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert "lineup: later" in lineup and "<LineupBuilder" in lineup
    me = (SRC / "components" / "MyAnswer.tsx").read_text()  # the Me page's answer row
    assert "s.may_leave" in me and "s.may_stay" in me and "/stay`" in me and "s.leave_consequence" in me
    card = (SRC / "components" / "board" / "SheetCard.tsx").read_text()
    assert "e.follow" in card and "e.lead" in card


def test_guide_reissue_card_on_the_leveling_guide_tab():
    """Me › Leveling guide (design §5.55 addendum 2026-11-05): the card that locks the member's own guide to another of
    their own accounts — three boxes, one press, a download; the inputs are dropped after it and never kept."""
    comp = (SRC / "components" / "GuideTools.tsx").read_text()
    api = (SRC / "api.ts").read_text()
    assert 'title="Issue for your other account"' in comp and "Issue an import string" in comp
    assert '"/api/me/guide/reissue"' in comp and "new_battletag: newTag.trim()" in comp
    assert "api.download(" in comp and "download:" in api and "X-Requested-With" in api
    assert 'setReText("")' in comp and 'setFromTag("")' in comp and 'setNewTag("")' in comp, "the string and both tags are dropped after the download"
    assert "RestedXP sells" in comp and "your own" in comp, "the card says it is for the member's own accounts"
    assert "localStorage" not in comp


def test_armory_link_pages_preview_and_print_only_the_servers_sentences():
    """Armory link §5.4, §5.5 (task 14): Me's ⋮ and Members' character ⋮ carry Link to the armory… · Sync now · Keep
    professions by hand / Follow the armory again · Unlink…, all behind `meta.armory`; Move to another member… is on every
    version (R11). The link dialog links exactly the character its lookup showed (armory_id) and drops a stale answer;
    every write previews and the confirm prints the server's sentences (no literal confirm message anywhere); the review
    groups Sure / Likely / No match, sends the proposal's rev and takes the fresh one from a 409; Give to a member…
    previews and prints the half-done answer as it is; a synced character's professions are read-only with why."""
    link = _code_only((SRC / "components" / "ArmoryLink.tsx").read_text())
    amap = _code_only((SRC / "components" / "members" / "ArmoryMap.tsx").read_text())
    seed = _code_only((SRC / "components" / "members" / "ArmorySeed.tsx").read_text())
    move = _code_only((SRC / "components" / "members" / "MoveCharacter.tsx").read_text())
    page = _code_only((SRC / "pages" / "Members.tsx").read_text())
    me = _code_only((SRC / "components" / "CharacterTable.tsx").read_text())
    for name, code in (("ArmoryLink", link), ("ArmoryMap", amap), ("ArmorySeed", seed), ("MoveCharacter", move)):
        assert not re.search(r"message:\s*[`\"'\[]", code), f"{name}: a confirm message written in the browser"
    for label in ("Link to the armory…", "Sync now", "Keep professions by hand", "Follow the armory again", "Unlink…"):
        assert f'"{label}"' in link, label
    assert "if (!meta.armory || !c.armory || !linkable(c)) return [];" in link, "no armory action where the version has none"
    assert 'message: p.sentences.join("\\n")' in link and "preview: true" in link
    assert "${base}/find" in link and "armory_id: looked.found.id" in link and "typed.current === asked" in link
    assert "e.status === 409" in link, "a name that now finds another character asks for a new look"
    assert "Look it up" in link and "Link it" in link and "useDirty(" in link
    assert "Not updated from the armory yet" in link and "in the guild" in link and "not in the guild" in link
    assert "Keep professions by hand to edit them." in link
    # Members: the mark and flags on each row (not for puppets), the actions, Move on every version, the card behind meta.armory
    assert "<ArmoryMark c={c} />" in page and "armory.actions(c, r.uid)" in page and "Move to another member…" in page
    assert "{meta.armory && <Box" in page and "<ArmoryMap " in page and "<MoveCharacter " in page
    assert "meta.armory" not in move, "a move works on every game version (R11)"
    assert '"/api/members/move"' in move and move.count("preview: true") == 1 and "said.sentences.join" in move
    assert "In the guild, not claimed (" in amap and "Give to a member…" in amap and "Link characters to the armory…" in amap
    assert '"/api/members/armory/give"' in amap and "preview: true" in amap and "x.linked" in amap and "x.message" in amap
    assert "r.words" in amap and "data.roster.words" in seed, "an empty roster says the server's sentence"
    for words in ('"Sure"', '"Likely"', '"No match"', "Pick another…", "Confirm", "Look it up by name…", "Link the sure matches…"):
        assert words in seed, words
    assert '"/api/armory/seed/apply"' in seed and "rev: data.rev" in seed and "e.status === 409" in seed and "setChanged(e.message)" in seed
    assert "p.results" in seed and "x.results" in seed, "each refused pick is named before and after the press"
    # Me: the line and the actions; a synced character's professions are read-only with why
    assert "armory.actions(c)" in me and "armoryWords(c)" in me and "syncedSentence(c)" in me and "synced(c)" in me
    assert "synced={synced(c) ? syncedSentence(c) : undefined}" in page
    # review round: Enter never spends a lookup while one is out; the answer's key names the character too; the ⋮ spins per
    # member + character; Give and Move register as edit modes; a Give refusal leaves Give it pressable; the review's link
    # dialog is prefilled from the member's record; no toast on a failed reload
    assert "busy !== null) return;" in link and "target?.uid" in link and "target?.label" in link and "isBusy" in link
    assert "useDirty(" in amap and "useDirty(" in move and "setRefused(e.message)" in amap
    assert "rows.find((m) => m.uid === r.uid)" in seed and "c?.surname" in seed
    assert "fail(e); });" not in amap and "fail(e); });" not in seed
    css = (SRC / "pages" / "members.module.css").read_text()
    phone = css[css.rindex("@media (max-width: 48em)"):]
    assert ".seedRow { grid-template-columns: minmax(0, 1fr); }" in phone, "the review's rows stack on a phone"
    # final review: C2 — the character whose professions editor is open greys its armory actions and Move, and the editor
    # closes when its character went or became synced (Me and Members)
    assert "EDITING_FIRST" in link and "editingThis(r, c) ? EDITING_FIRST : null" in page and page.count("EDITING_FIRST") >= 3
    assert "prof?.label === c.label" in me and "EDITING_FIRST" in me
    assert "if (!now || synced(now)) setProf(null);" in page and "if (!now || synced(now)) setProf(null);" in me
    # C4 — the map and the review reload at most every few seconds and never under a dialog or a press
    live = (SRC / "hooks" / "useLive.ts").read_text()
    assert "export function useHeldLive" in live and "every" in live
    assert 'useHeldLive(load, ["member"], give !== null)' in amap and 'useHeldLive(load, ["member"], busy !== null)' in seed
    # C5 — one spinner per member + character; a cancelled confirm reloads nothing
    assert "new Set(b)" in link and "busy.has(busyKey(uid, c.label))" in link and "if (wrote) onChanged();" in link
    # C6 — the shield is an image a keyboard reaches; the link dialog's controls are 44 px on a phone
    assert 'role="img" tabIndex={0}' in link and "className={css.touch}" in link
    # D1 — the not-claimed fold says what is true; C7 — the guild card's row says "Realm type"
    assert "that no record here is linked to" in amap and "nobody here has registered" not in amap
    guild = (SRC / "components" / "ArmoryGuildCard.tsx").read_text()
    assert ">Realm type<" in guild and ">Kind<" not in guild
    # D5 — a puppet's characters never offer the armory: Members gates the mark and the actions on !r.test
    assert "{!r.test && armory.actions(c, r.uid)" in page and "{!r.test && <ArmoryMark c={c} />}" in page


# auction prices §4.6: the pages that print item names, and the one place in each that is not a link (a button's label —
# a link can't sit inside a button; the button opens the item's tiers)
ITEM_PAGES = ("pages/Professions.tsx", "pages/LootLog.tsx", "components/loot/Tonight.tsx", "components/loot/History.tsx",
              "components/loot/Items.tsx", "components/loot/Wishlists.tsx", "components/loot/MyLoot.tsx", "components/loot/Appeals.tsx",
              "components/LootWeightsCard.tsx", "pages/Prices.tsx")
ITEM_NAME_NOT_A_LINK = {"components/loot/Items.tsx": 'onClick={() => setOpen(i.key)}>{i.name}</Button>'}
# a JSX child printing an item row's name: `{r.item}` anywhere, `{x.name}` on the pages whose `.name` rows are items
# (never `${…}` inside a template string — a title — nor `={…}` — an attribute)
ITEM_CHILD = re.compile(r"(?<![$=\w])\{\s*[\w!?.]+\.item\s*\}")
NAME_CHILD = re.compile(r"(?<![$=\w])\{\s*(?:i|it|w|e|r)\.name\s*\}")


def test_item_names_link_through_one_component():
    """Auction prices §4.6 (D4, C12): every item name with an id on the Professions page, the loot pages and wishlists is
    ItemName — a plain link built from /api/meta `item_url` alone (new tab, no referrer), else plain text. No page builds
    an item address itself or loads a third-party script or tooltip."""
    item = _code_only((SRC / "components" / "ItemName.tsx").read_text())
    assert 'target="_blank"' in item and 'rel="noopener noreferrer"' in item
    assert 'template.replace("{id}", String(id))' in item and "template && id ?" in item, "no id or no template: plain text"
    assert "setItemUrl(m.item_url)" in (SRC / "App.tsx").read_text()
    for f in sorted(SRC.rglob("*.ts*")):
        text = f.read_text()
        assert "wowhead" not in text.lower(), f"{f.relative_to(SRC)}: the address comes from the server's template"
        assert "<script" not in text and "tooltip.js" not in text.lower(), f.relative_to(SRC)
    for rel in ITEM_PAGES:
        code = _code_only((SRC / rel).read_text())
        assert "<ItemName " in code, rel
        code = code.replace(ITEM_NAME_NOT_A_LINK.get(rel, "\0"), "")
        hits = ITEM_CHILD.findall(code) + ([] if rel == "pages/Professions.tsx" else NAME_CHILD.findall(code))
        assert not hits, f"{rel}: an item name printed outside ItemName: {hits}"
    prof = _code_only((SRC / "pages" / "Professions.tsx").read_text())
    for link in ("<ItemName id={r.makes.id} name={r.makes.name} />", "<ItemName id={x.id} name={x.name} />", "<ItemName id={p.id} name={p.name} />",
                 "<Text fw={700}><ItemName id={r.makes?.id} name={r.name} /></Text>",  # a recipe's name links to what it makes (the hit, and What the guild makes)
                 "className={css.name}><ItemName id={r.makes?.id} name={r.name} /></Text>"):
        assert link in prof, link


def test_professions_page_prints_the_servers_price_words():
    """Auction prices §4.4–4.5: the cost to make (a line per reagent, then the server's sentence and what a craft makes)
    and the price box print the server's fields; the page composes no sentence about a price and writes no money. The
    status line is once per card; with prices off the owner reads the server's one line. Cards below sm."""
    src = _code_only((SRC / "pages" / "Professions.tsx").read_text())
    assert "{r.cost && <CostLines cost={r.cost} />}" in src and "!r.cost &&" in src, "the plain reagents line only without a cost"
    for field in ("cost.sentence", "cost.made_words", "x.unit_words", "x.total_words", "r.cost.sentence", "r.cost.made_words"):
        assert field in src, field
    assert "What does it go for?" in src and "/api/prices/find?q=${encodeURIComponent(text)}" in src
    assert "found.message" in src and "found.more" in src and "found.words" in src and "p.low_words" in src
    assert "want.current === d.q.trim()" in src, "an answer for text no longer in the box is dropped"
    assert "{pricesOn && <PriceBox " in src and "{!pricesOn && meta.prices?.words &&" in src
    assert 'useLive(load, ["member", "config", "prices"])' in src
    assert src.count(">{priceWords}<") == 2, "how recent the prices are: once on the finder's card, once on What the guild makes"
    from test_copy import tsx_strings
    reads = " ".join(s for _, s in tsx_strings(SRC / "pages" / "Professions.tsx"))
    for composed in ("Cost to make", "Prices as of", "none listed", "at least", "six hours", "out of date"):
        assert composed not in reads, f"the server says {composed!r}"
    assert not re.search(r"\b\d+[gsc]\b|copper|toLocaleString", src), "money arrives as words"
    css = (SRC / "pages" / "professions.module.css").read_text()
    phone = css.split("@media (max-width: 48em)", 1)[1]
    assert ".priceRow { grid-template-columns: minmax(0, 1fr) auto;" in phone and "border-radius: 8px" in phone


def test_market_card_picks_the_auction_house_with_the_servers_sentences():
    """Auction prices §4.1: Config › Settings shows the auction house card under the guild card where the version has
    auction houses; the owner sees the choices with their sample prices, confirms with the server's sentences, and can
    turn prices off; a stale page (409) reloads; the open choices register with the server switcher's guard."""
    config = (SRC / "pages" / "Config.tsx").read_text()
    assert config.index("<ArmoryGuildCard />") < config.index("{meta.armory && meta.prices?.market && <MarketCard />}")
    card = _code_only((SRC / "components" / "MarketCard.tsx").read_text())
    assert not re.search(r"message:\s*[`\"'\[]", card), "a confirm message written in the browser"
    for words in ("Pick the auction house…", "Turn prices off…", "Pick this one…"):
        assert words in card, words
    assert '{ choices: true }' in card and "preview: true" in card and "p.sentences.join" in card and "d.clear_sentences.join" in card
    assert "rev: d.rev" in card and "e.status === 409" in card and "e.status === 404" in card
    assert "c.listings_words" in card and "s.low_words" in card and "choices.words" in card and "<ItemName id={s.id}" in card
    assert 'useDirty("market", choices !== null)' in card and '["config", "prices"]' in card


def test_the_prices_tab():
    """Price watch §4.3 (D1, D2, C5, C6, C16, C17): the Prices tab for every member where the version has auction houses —
    the search with Watch / Stop watching, Your watch list opened in place to its chart and the server's sentences. Every
    time and every price is the server's words; the chart is the site's own inline SVG in theme colours, no library."""
    root = SRC.parents[1]
    app = (SRC / "App.tsx").read_text()
    page = _code_only((SRC / "pages" / "Prices.tsx").read_text())
    assert 'path="/prices"' in app and '"/prices", label: "Prices"' in app and "meta.prices?.market" in app
    for route in ("/api/prices/find", "/api/prices/watching", "/api/prices/watch", "/api/prices/history"):
        assert route in page, route
    for label in ("Watch", "Stop watching", "Your watch list"):
        assert label in page, label
    assert "<ItemName " in page and "<PriceChart" in page
    # 2026-11-15: the watch list takes Movers' time and item-group choices (the server's windows and groups)
    assert "list.windows.map(" in page and "watchGroups.map(" in page and "window: watchWinRef.current" in page
    # 2026-11-15: the Guild tab — what members watch and how many, never who
    assert "/api/prices/watch/share" in page and "list.shared" in page
    # 2026-11-16: the switch is Visibility · Public | Private on the tabs' line, and the list has a type-ahead filter above the time and group choices
    assert "Share with the guild\"" not in page and 'aria-label="Visibility"' in page and '"Public"' in page and '"Private"' in page
    assert 'placeholder="Filter this list by name"' in page and "r.name.toLowerCase().includes(typed)" in page
    assert page.index("Filter this list by name") < page.index("list.windows.map(") < page.index("watchGroups.map(")
    assert "/api/prices/guild-watching" in page and "r.watchers_words" in page and "never who" in page
    assert "list?.checked" in page and "again * 1000" in page, "the last / next check beside Prices as of, asked again after the next check"
    # 2026-11-14: a search row opens to the chart too (watched or not), and a watched row carries its small line
    # movers (an experiment): the server's two lists, every figure its words; a row opens the same panel
    assert "/api/prices/movers" in page and 'moversSide("dips", movers.dips, movers.dips_words)' in page
    # 2026-11-15: Dips and Rising are tabs — one list at a time, each tab with its count
    assert "`Dips · ${chosenOf(movers.dips).length}`" in page and "`Rising · ${chosenOf(movers.rising).length}`" in page
    # 2026-11-15: Climbing / Sliding — a price that went one way step after step — are two more tabs on the same rows
    assert 'moversSide("climbing", movers.climbing, movers.climbing_words)' in page and "movers.trend_words" in page
    assert 'moversSide("sliding", movers.sliding, movers.sliding_words)' in page
    assert "`Climbing · ${chosenOf(movers.climbing).length}`" in page and "`Sliding · ${chosenOf(movers.sliding).length}`" in page
    assert 'moversSide("rising", movers.rising, movers.rising_words)' in page and "r.change_words" in page and "hist.used_in" in page
    # Bargains: the card's second tab, the server's rows and sentences
    for piece in ("/api/prices/bargains", "bargains.hint_words", "b.typical_words", "b.source_words", "b.none_words", "r.gap_words", "r.ref_words"):
        assert piece in page, piece
    # Prep: the third tab, the server's rows and sentences
    for piece in ("/api/prices/prep", "prep?.hint_words", "p.none_words", "p.below_words", "p.below_none", "r.uses_words", "r.profit_words"):
        assert piece in page, piece
    # 2026-11-15: the card browses every mover — the server's groups, orders, steps, page size and empty sentence
    for piece in ("movers.groups.map(", "movers.sorts.map(", "movers.steps.map(", "movers?.none_words", "movers?.page", "r.diff_words", "Show more"):
        assert piece in page, piece
    assert 'toggle(p.id, "search")' in page and 'isOpen(p.id, "search") && panel(' in page, "a search result opens its chart without watching"
    assert "<Sparkline data={r.spark}" in page, "the watch list's rows draw the server's 24-hour line"
    # 2026-11-15: the watch list beside the search and Movers on a wide screen, first on a narrow one
    sheet = (SRC / "pages" / "prices.module.css").read_text()
    assert "css.cols" in page and "css.main" in page and "className={css.side}" in page
    assert "position: sticky" in sheet and ".side { order: -1;" in sheet and "@media (min-width: 80em)" in sheet
    assert "r.spark.change_words" in page and "trendOf(r.spark.up)" in page, "the day's change beside the price, the server's words and direction"
    assert 'useState("1")' in page, "the chart opens on 24 hours"
    spark = _code_only((SRC / "components" / "PriceChart.tsx").read_text())
    assert "export function Sparkline" in spark and "data.since" in spark and "data.until" in spark
    assert "toLocale" not in page and "new Date(" not in page, "times and money are the server's words"
    assert 'useLive(' in page and '"prices"' in page
    assert "useDirty" not in page, "the tab has no edit mode"
    chart = _code_only((SRC / "components" / "PriceChart.tsx").read_text())
    # 2026-11-15: how many were listed, as bars under the line — the server's words, no strip without a quantity
    assert "most.listed_words" in chart and "shown.listed_words" in chart and "H + volH" in chart
    # the read-out at the pointer: price and how many listed beside the crosshair, and no native tooltip over it
    assert "tip.words" in chart and chart.count("<title>") == 1, "only the small line keeps a <title>"
    assert "<svg" in chart and "var(--mantine-" in chart
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", chart), "theme colours only"
    assert "toLocale" not in chart and "new Date(" not in chart, "the chart's labels are the server's words"
    for clock in ("Date.now", "performance.now", "Date("):
        assert clock not in chart and clock not in page, f"x runs over the server's since..until, never the browser's clock ({clock})"
    assert "data.since" in chart and "data.until" in chart, "the span's start and end are the server's"
    assert "if (p.low == null) { drawing = false; return; }" in chart, "a null low breaks the line: nothing listed or not read (F-5)"
    assert "${shown.when} · ${shown.words}" in chart, "the read-out prints the server's words (not read, none listed, a price)"
    assert "unread: boolean" in (SRC / "api.ts").read_text()
    assert "pts.length === 0" in chart and "pts.length < 2" not in chart, "one point is a held line; only no point has no frame"
    assert "key={i}" in chart and "key={p.t}" not in chart, "band keys by index (two points may share a time)"
    assert "span={span}" in page and "busy={" in page, "the switch shows the span pressed while its answer loads"
    pkg = (root / "frontend" / "package.json").read_text()
    assert not any(lib in pkg for lib in ("recharts", "@mantine/charts", "chart.js", "d3")), "no chart library (C17)"
    assert "function PriceBox(" in (SRC / "pages" / "Professions.tsx").read_text(), "the Professions page keeps its box (D2)"
    css = (SRC / "pages" / "prices.module.css").read_text()
    assert "@media (max-width: 48em)" in css, "a card per row below sm"
    from test_copy import tsx_strings
    reads = " ".join(s for _, s in tsx_strings(SRC / "pages" / "Prices.tsx"))
    for composed in ("Prices as of", "none listed", "aren't watching", "Register first", "holds 100", "kept prices since", "Lowest in the last"):
        assert composed not in reads, f"the server says {composed!r}"


def test_raids_and_readiness_have_no_raid_timing_and_one_minimum():
    """Phase 2 (raids without times; C7, D8, D16): sheet timing is each schedule's own, so the Raids page's rules
    editor holds none of it; a raid's make-up is one minimum per role or class (no limits row, no aims, no upper
    number); every officer on the page edits (no `owner` prop on RuleCard); the schedule editor shows the schedule's
    own values and never says what "the raid's" would be."""
    raids = (SRC / "pages" / "Raids.tsx").read_text()
    card = raids[raids.index("function RaidDetail("):]
    assert "owner" not in card[:card.index(") {\n")], "RaidDetail takes no owner prop: every officer edits"
    use = raids[raids.index("<RaidDetail"):]
    assert "owner=" not in use[:use.index("/>")]
    assert "The owner edits the rules" not in raids
    views = {rel: (SRC / rel).read_text() for rel in ("pages/Raids.tsx", "components/raids/RulesView.tsx", "components/raids/PriorityView.tsx",
                                                     "components/raids/ReadinessView.tsx")}
    for rel, text in views.items():
        for key in ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours"):
            assert key not in text, f"{rel} still names the raid's {key}"
        for word in ("limits", "aims", "at most"):
            assert word not in _code_only(text).lower(), f"{rel} still says {word!r}"
        assert "max:" not in text, f"{rel} still sends or reads an upper number"
        assert "(owner)" not in text and "(officers)" not in text
    # the make-up's minimums: one `comp` change with the raid's rev; the empty make-up is the server's sentence
    assert 'kind: "comp"' in raids and "`config:raids:${r.id}`" in raids
    assert "page.make_up_empty" in views["components/raids/RulesView.tsx"] and "view.make_up_empty" in views["components/raids/ReadinessView.tsx"]
    for f in SRC.rglob("*.tsx"):
        assert "Add one for a role or class below" not in f.read_text(), f"{f.name}: an empty sentence pointing below at a button (carried 3)"
    ready = views["components/raids/ReadinessView.tsx"]
    assert "Against the make-up" in ready and "on the Raids page</Anchor>" not in ready
    sched = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    for gone in ("SOURCE_WORDS", "inherited(", "ADHOC_DEFAULT"):
        assert gone not in sched, f"LineupPage.tsx still has {gone}"
    assert ".timing" in sched and "opts.adhoc" in sched, "a new schedule's sheet timing starts from the server's suggestion"


def test_the_schedule_editor_names_a_schedule_by_its_id():
    """Phase 3 (Task 2): every schedule write targets the schedule's id alone and carries its own rev
    (config:schedule:<id>); a new schedule sends its lineup (this raid, then the follow-on) and takes an id no schedule in
    the guild holds — there is no `default` schedule any more."""
    src = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert "target: `${" not in src and "target: id" in src and "config:schedule:${" in src and "is_default" not in src
    assert "slug(d.name, taken, list?.reserved_ids || [])" in src  # phase 4: the reserved ids are the server's
    regs = _code_only((SRC / "components" / "schedules" / "RegularsSection.tsx").read_text())
    assert 'split(":")' not in regs and '|| "default"' not in regs
    assert "schedule: sid" in regs and "schedule: o.key" in regs


def test_the_run_card_reschedules_and_the_schedule_editor_sends_a_lineup():
    """Phase 3, Task 7: the run card's Reschedule… picks the new start in a datetime-local input (the Clock rule's machine
    input), POSTs a preview to /reschedule and prints ONLY the server's `sentences` before Move, which sends the preview's
    rev back; the schedule editor sends `lineup` (up to three later raids, 30-minute start steps) and no `follow_raid`.
    The picker is the site's WhenPicker — a calendar day and a 15-minute select (the phase 4 final review: a
    DateTimePicker's time is typed, and the Clock rule's lint refuses it)."""
    card = (SRC / "components" / "board" / "SheetCard.tsx").read_text()
    assert "/reschedule`" in card and "<WhenPicker" in card and "<DateTimePicker" not in card and "pv.sentences.join" in card and "rev: pv.rev" in card
    assert 'confirmLabel: "Move"' in card and "Reschedule…" in card
    assert "e.cancel_tells_channel !== false" in card  # a later raid whose first finished: no channel line to offer
    sched = (SRC / "pages" / "LineupPage.tsx").read_text()
    assert "fields.lineup" in sched and "follow_raid" not in sched
    assert "Back" in card and card.index("setMoveOpen(false)") > card.index("confirmLabel: \"Move\""), "Back keeps the picked time"
    assert "useDirty(`reschedule:" in card
    # a picker's minDate from the browser's UTC date is tomorrow west of UTC in the evening, and Mantine clamps tonight's
    # start to it: never toISOString for a minDate (the server refuses a past start in words)
    for f in SRC.rglob("*.tsx"):
        assert "minDate={new Date().toISOString()" not in f.read_text(), f


def test_confirm_box_prints_the_servers_box():
    box = _code_only((SRC / "components" / "ConfirmBox.tsx").read_text())
    assert "box.sentences" in box and "box.diff" in box and "box.verb" in box and 'box.danger ? "red"' in box
    assert "confirm_words.changed" in box and not re.search(r">\s*(Yes|No|OK|Cancel|Confirm)\s*<", box)


def test_leaving_asks_first():
    app = _code_only((SRC / "App.tsx").read_text()); guard = _code_only((SRC / "components" / "LeaveGuard.tsx").read_text())
    assert "<LeaveGuard" in app and ("guardNav(" in app or "guardClick(" in app) and "confirm_words.discard" in guard and "beforeunload" in guard


def test_raids_page_is_a_list_with_three_views():
    page = _code_only((SRC / "pages" / "Raids.tsx").read_text())
    assert "<ListPage" in page and "<SegmentedControl" in page and all(v in page for v in ('"rules"', '"priority"', '"readiness"'))
    assert not (SRC / "pages" / "Readiness.tsx").exists() and not (SRC / "components" / "CompTargets.tsx").exists()
    for f in ("Raids.tsx", "raids/RulesView.tsx", "raids/PriorityView.tsx", "raids/ReadinessView.tsx"):
        code = _code_only(((SRC / "pages") if f == "Raids.tsx" else (SRC / "components")).joinpath(f).read_text())
        assert not re.search(r"slot_labels|signup_lead_hours|lock_hours_before|run_times|WhenItRuns", code), f
        assert "useConfirm(" not in code, f"{f}: the §7b box only"


def test_the_raids_page_carries_the_earlier_reviews():
    """Carried 1, 2, 4 and the size rule (phase 4, Task 6): the Reset prints the server's box (page.boxes.reset) and is
    sent with config:raid_all:<rid>; the Save is the server's box too (useOpsSave with `always` and the verb); a minimum
    of 0 is sent as a clear, never as 0; Readiness prints one Minimum column from the server and never "the guild's";
    the size is displayed, never edited; SlotIcon / RoleIcon live in components/Icons."""
    page = _code_only((SRC / "pages" / "Raids.tsx").read_text())
    assert "page.boxes.reset" in page and "`config:raid_all:${r.id}`" in page and "opsApi.confirm(" in page
    assert "always: true" in page and "box: true" in page and "useDirty(`raid:${r.id}`, editing, { n, name: r.name })" in page
    assert 'm.min === "0" ? null' in page, "a minimum of 0 goes as a clear"
    rules = _code_only((SRC / "components" / "raids" / "RulesView.tsx").read_text())
    assert 'label === "Size"' not in rules, "the size is the game's: shown by the server's Details, never a field"
    ready = _code_only((SRC / "components" / "raids" / "ReadinessView.tsx").read_text())
    assert "the guild's" not in ready.lower() and len(re.findall(r"<Table\.Th\b", ready)) == 3 and "m.have < m.min" not in ready
    review = (SRC / "components" / "ReviewModal.tsx").read_text()
    assert "d?.box" in review and "box?: boolean" in review and "always?: boolean" in review


def test_the_raids_page_fix_round_one():
    """Task 6 review: (1) the Reset sends the rev of the box ON SCREEN — a 409's fresh box comes with its fresh rev, so
    the next press goes through instead of 409 forever; (2) a row only the game sets never offers 0 (its select starts
    at the game's number), and with a verb the review is always a §7b box — a refusal box with Back alone, never
    "Nothing to review" / "Apply N changes" / the result sheet; (3) Readiness prints the server's Standing level and its
    sentence and compares nothing; (4) an internal Facts link goes through the leave guard; (8) a modified click is
    the browser's, the boxes' buttons are thumb-sized on a phone, an edit mode without a count still gets a sentence."""
    page = _code_only((SRC / "pages" / "Raids.tsx").read_text())
    reset = page[page.index("async function onReset()"):]
    reset = reset[:reset.index("\n  }\n")]
    assert "shown.rev = fresh.reset_rev" in reset and "shown.rev ? { [`config:raid_all:${r.id}`]: shown.rev }" in reset
    assert "r.reset_rev ? {" not in reset, "the rev of the box the page opened with goes stale after a 409"
    assert "floor: m.own ? 0 : m.min" in page
    rules = _code_only((SRC / "components" / "raids" / "RulesView.tsx").read_text())
    assert "sizes.slice(m.floor)" in rules and "floor: 0" in rules
    review = _code_only((SRC / "components" / "ReviewModal.tsx").read_text())
    box_mode = review[review.index("if (boxed) {"):review.index("title={result ?")]
    assert "<BoxButtons" in box_mode and "verb: null" in box_mode and "Nothing to review" not in box_mode and "Apply {n}" not in box_mode and "Back to edits" not in box_mode
    assert review.index("if (boxed) {") < review.index("<ResultSheet"), "in box mode the result sheet is never reached"
    ready = _code_only((SRC / "components" / "raids" / "ReadinessView.tsx").read_text())
    assert "level={view.level}" in ready and "view.standing_why" in ready and "HAVE_COLOUR[m.level]" in ready
    assert "onEdit" in ready, "the empty make-up's 'Edit the raid' sentence has its Edit beside it"
    assert "guardClick(href)" in _code_only((SRC / "components" / "Facts.tsx").read_text())
    guard = _code_only((SRC / "components" / "LeaveGuard.tsx").read_text())
    assert "e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey" in guard and "meta.leave.page" in guard
    for f in ("ConfirmBox.tsx", "ReviewModal.tsx"):
        assert "content: touch.touch" in (SRC / "components" / f).read_text(), f"{f}: 44 px buttons on a phone"
    app = _code_only((SRC / "App.tsx").read_text())
    assert "e.preventDefault(); guardNav" not in app and "onClick={guardClick(n.to)}" in app
    import sys

    sys.path.insert(0, str(SRC.parents[1] / "src"))
    from oibot_gm import consequences as cq

    assert cq.leave_unsaved_sentence(0, "") == cq.LEAVE_TEMPLATE["page"] == "Your edits on this page are not saved yet."


def test_the_schedules_page_replaces_the_interim_one():
    """Phase 4, Task 11: /schedules is the Schedules page (list · one schedule · its editor); the interim page, the old
    editor, its raid header and the regulars block are gone, and RaidCard keeps only the run's week strip (SheetCard and
    the test bench); /readiness keeps its raid."""
    app = _code_only((SRC / "App.tsx").read_text())
    for path in ('path="/schedules"', 'path="/schedules/new"', 'path="/schedules/:id"', 'path="/schedules/:id/edit"'):
        assert path in app, path
    assert "<SchedulesPage" in app and 'mode="new-schedule"' in app and 'mode="edit-schedule"' in app and "RaidSchedulesPage" not in app
    assert 'path="/raids/:id"' in app and "?view=readiness" in app and 'path="/readiness"' in app
    for gone in ("pages/RaidSchedules.tsx", "components/ScheduleCard.tsx", "components/RaidHeader.tsx", "components/RegularsBlock.tsx"):
        assert not (SRC / gone).exists(), gone
    card = _code_only((SRC / "components" / "RaidCard.tsx").read_text())
    assert "export function RunStrip(" in card
    for gone in ("WhenItRuns", "RunWeek", "SeatWeights", "function Section(", "function Panel(", "function Pair("):
        assert gone not in card, gone
    for f in SRC.rglob("*.ts*"):
        text = f.read_text()
        for name in ("ScheduleCard", "RaidHeader", "RegularsBlock", "RaidSchedules"):
            assert f'/{name}"' not in text, f"{f.name} imports {name}"


def test_schedules_page_reads_by_its_sections():
    page = _code_only((SRC / "pages" / "Schedules.tsx").read_text())
    assert "<ListPage" in page and '"Active"' in page and '"Paused"' in page and "<LineupPicture" in page and "<RegularsSection" in page
    assert not (SRC / "components" / "ScheduleCard.tsx").exists()


def test_the_schedule_editor_is_the_builder_with_repeats():
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert '"edit-schedule"' in page and "<SlotEditor" in page and "always: true" in page and "config:schedule:" in page
    assert 'useDirty(`schedule:' in page and "status === 409" in page


def test_the_schedules_page_keeps_every_old_lever():
    """Task 11's inventory: Pause / Resume / Remove print the read model's box and press through the TOKEN path (a
    preview whose box must be the one on screen, then the token) — never `confirmed`; a 409 brings the fresh box and its
    rev; a schedule whose raid left the game says so and still has Remove; Next events link each night (an event's
    page, else its coming-up page); the regulars' next four weeks with Open this run early; a removed schedule's
    leftover regulars with their Remove; members added from the server's `people` by a search."""
    page = _code_only((SRC / "pages" / "Schedules.tsx").read_text())
    details = _code_only((SRC / "components" / "schedules" / "ScheduleDetails.tsx").read_text())
    regs = _code_only((SRC / "components" / "schedules" / "RegularsSection.tsx").read_text())
    for needle in ("boxes.pause", "boxes.resume", "boxes.remove", "opsApi.preview(", "opsApi.apply(", "b.fp !== fp", "shown.rev = v.rev",
                   "view.gone", "useConfirmBox(", "list.orphans"):
        assert needle in page, needle
    for f, code in (("Schedules", page), ("ScheduleDetails", details), ("RegularsSection", regs)):
        assert "confirmed" not in code and "opsApi.confirm(" not in code, f"{f}: a press never writes unseen"
        assert "useConfirm(" not in code and "toISOString" not in code, f
        assert not re.search(r">\s*(Yes|No|OK|Cancel|Confirm)\s*<", code), f
    assert "view.next_empty" in details and "/events/coming/" in details and "/events/${" in details
    for needle in ("Open this event early", "r.forecast", "f.can_open", "f.why", "/api/event/coming/", "searchable", "view.people",
                   "Add a member", "search members", "/api/admin/raid/regulars", "r.outlook", "r.expected"):
        assert needle in regs, needle
    assert "<LevelChip" not in regs, "the mockup's Regulars: facts, no chip (spec §7)"
    # fix round 1: never a code word in a toast; each night's why on its own row; a covered night with people away too;
    # the regulars' buttons 44 px on a phone
    for f, code in (("Schedules", page), ("RegularsSection", regs)):
        assert '"gone"' not in code, f
    assert "goneWords.current" in page and "shown.changes = p.changes" in page and "preview: true" in regs
    assert "css.night" in regs and "f.away_words" in regs and "f.away.length > 0" in regs and regs.count("className={css.touch}") == 2


def test_the_view_pages_show_the_editors_own_help():
    """Issue #13 (the owner, 2026-11-25): a Details row naming a setting the editors explain shows the editors' (?) with
    the SAME sentence — one component (components/FieldHelp.tsx) used by SettingsFields and by Facts, the sentence the
    server's (a Fact's `help`, constants.field_help over PAGE_WORDS schedule_field_help), never typed in a page file."""
    fh = _code_only((SRC / "components" / "FieldHelp.tsx").read_text())
    assert "export function FieldHelp(" in fh and "size={44}" in fh and "onClick=" in fh and "onFocus=" in fh and 'role="tooltip"' in fh
    facts = _code_only((SRC / "components" / "Facts.tsx").read_text())
    assert 'from "./FieldHelp"' in facts and "<FieldHelp label={help.about} text={help.text} inline />" in facts
    assert "f.help ?" in facts and 'whiteSpace: "nowrap"' in facts, "the (?) never wraps onto a line of its own"
    shared = _code_only((SRC / "components" / "schedules" / "SettingsFields.tsx").read_text())
    assert 'from "../FieldHelp"' in shared and "function FieldHelp(" not in shared, "one component, not a copy"
    for f in [*(SRC / "components").rglob("*.tsx"), *(SRC / "pages").glob("*.tsx")]:
        code = _code_only(f.read_text())
        assert "after the lock, the bot DMs" not in code.lower() and "At the lock, everyone rostered" not in code, f.name


def test_the_schedule_editor_keeps_every_setting():
    """Task 11's inventory, the editor: name (and a new schedule's id, the reserved ids from the API), what kind (weekly
    run times · days of each lockout · opened by an officer), the lineup (the ONE builder; the first raid fixed on an
    edit — the server refuses a change), run times picked, the four hours plus a fill ask's wait from the server's
    choices, the four switches, a preset, the split, the five weights with Use the raid's; one Save through the token
    path (never `confirmed`), create-shaped for a new one; a 409 keeps the draft and offers Reload through the guard."""
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    editor = page[page.index("function ScheduleEditor("):]
    for needle in ("fixedFirst={!!sid}", "opts.hours", "opts.presets", "opts.split_policies", "opts.max_weight", "W.raids_own",
                   "F = W.schedule_fields", "<TimingFields", "<PriorityFields", "kind: d.kind", "QUARTER_HOURS", "guardNav(", "Reload",
                   'useDirty(`schedule:${sid ?? "new"}`', "box: true"):
        assert needle in editor, needle
    # the Sheet timing and Priority pieces are ONE component the schedule editor and Edit event share (the owner's tweaks, B)
    shared = _code_only((SRC / "components" / "schedules" / "SettingsFields.tsx").read_text())
    for needle in ("label={F[k]}", 'toggle("nudge")', 'toggle("autofill")', 'toggle("open_dm")', 'toggle("confirm")', "words.split[p]"):
        assert needle in shared, needle
    assert "confirmed" not in editor and "opsApi.confirm(" not in editor and "staleOnPage: true" in editor
    # fix round 1: the labels are the server's words (the Save box's diff lines say the same), none written here
    for literal in ('"Sheet opens"', '"Locks"', '"Reminder"', '"Confirm by"', '"An ask waits"', '"The bot asks people for open seats"',
                    '"Signup Reminder"', '"Lock Confirmation"', '"Automated Fill Requests"', '"Sheet Opening DM"', "SPLIT_LABEL"):
        assert literal not in editor and literal not in shared, literal
    assert "<NameFields" in editor and "TimePicker" not in editor and "<NumberInput" not in editor, "a time is picked, a number is a select"
    names = _code_only((SRC / "components" / "schedules" / "NameFields.tsx").read_text())
    assert names.count("<TextInput") == 2 and 'label="Name"' in names and 'label="Id"' in names, "prose is typed: the name and a new id"
    slots = _code_only((SRC / "components" / "SlotEditor.tsx").read_text())
    assert "TimePicker" not in slots and "QUARTER_HOURS" in slots, "a run time is a 15-minute select (the Clock rule)"



def test_the_builder_works_without_a_drag():
    b = _code_only((SRC / "components" / "lineup" / "LineupBuilder.tsx").read_text())
    for needle in ("draggable", "onDrop", '"Add"', "Move earlier", "Move later", '"Remove"', "aria-label={`one roster fewer for ${name}`}",
                   "aria-label={`one roster more for ${name}`}", "fixedFirst"):
        assert needle in b, needle
    assert "const full = value.length >= max" in b and 'useMediaQuery("(max-width: 48em)")' in b, "steps stack below sm"


def test_new_event_picks_a_time_never_types_one():
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text()); when = _code_only((SRC / "components" / "WhenPicker.tsx").read_text())
    assert "<WhenPicker" in page and "<DatePickerInput" in when and "clock12" in when and "TextInput" not in when + page
    assert 'useDirty("lineup:new"' in page and "guardNav(" in page and not (SRC / "pages" / "Rosters.tsx").exists()


def test_a_taken_time_shows_no_borrowed_times():
    """The New event review (P1, P4): under a refusal the server answers the LIVE event's lineup, so the builder takes the
    preview's step times only when there is no refusal and the preview's raids are the builder's, and the facts hide
    behind the refusal box; at a full lineup the builder says so in the server's sentence (/api/event-new lineup_full)."""
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    b = _code_only((SRC / "components" / "lineup" / "LineupBuilder.tsx").read_text())
    assert "times={p && !checking ? p.lineup.map" not in page
    assert "p.lineup.map((s) => s.raid).join() === lineup.map((e) => e.raid).join()" in page and "!refusal" in page
    assert "{p && !refusal && <Facts items={p.facts}" in page
    assert "fullWords={opts.lineup_full}" in page and "{full && fullWords" in b


def test_new_event_prints_only_the_servers_words():
    """Carried 7, 8, 9 (phase 4, Task 7): every change re-asks the preview (the latest answer wins) and the page prints its
    facts, step times, notes and refusal box — never its own; the Day's lower bound is the GUILD's date (/api/event-new
    `today`), never the browser's; the four-step limit is the server's (lineup_max); a schedule's night opens through
    the server's box with `seen`; a one-off opens with no box and lands on the run."""
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    when = _code_only((SRC / "components" / "WhenPicker.tsx").read_text())
    assert "/open/preview`" in page and "seq.current" in page, "the latest answer wins"
    assert "<Facts items={p.facts}" in page and "p.notes" in page and "p.refusal_box" in page and "p.lineup" in page
    assert "minDate={minDate}" in when and "opts.today" in page and "toISOString" not in when + page
    assert "max={opts.lineup_max}" in page and '"/api/event-new"' in page
    assert "useConfirmBox(" in page and "seen: fp" in page and 'p.kind === "scheduled" ? p.schedule :' in page
    assert 'dayFormat = "ddd D MMM"' in when and "valueFormat={dayFormat}" in when and "Array.from({ length: 96 }, (_, i) => ({ value: hhmm(i * 15), label: clock12(i * 15) }))" in when
    b = _code_only((SRC / "components" / "lineup" / "LineupBuilder.tsx").read_text())
    assert "Drop a raid here for step" in b and "-player · about" in b and "aria-label={`Move ${" in b
    assert "fetch(" not in b and "api." not in b, "the builder is pure: it never fetches"
    assert "<LineupPicture lineup={p.lineup}" in page, "a schedule's night is drawn as its own lineup"
    events = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert 'to="/events/new"' in events and "New event" in events
    assert 'path="/events/new"' in _code_only((SRC / "App.tsx").read_text())


def test_events_page_reads_by_its_sections():
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "<ListPage" in page and "/api/event-list" in page and all(g in page for g in ('"Open"', '"Coming up"', '"History"'))
    order = [page.index(s) for s in ("<EventDetails", "<SignupsSection", "<ActivitySection")]
    assert order == sorted(order)
    for f in (SRC / "components" / "events").glob("*.tsx"):
        code = _code_only(f.read_text())
        assert "useConfirm(" not in code and not re.search(r"message:\s*[`\"']", code), f.name
        assert not re.search(r"[\"'`](First to join|Not asked|Asked at the lock|One-off, no schedule|N/A|Lock Confirmation|Automated Fill Requests)[\"'`]", code), f.name


def test_reschedule_picks_never_types():
    box = _code_only((SRC / "components" / "events" / "RescheduleBox.tsx").read_text())
    assert "<WhenPicker" in box and "preview: true" in box and "seen" in box and "datetime-local" not in box


def test_the_events_page_asks_with_the_servers_box_and_lives():
    """Phase 4, Task 8: every action prints the SERVER's box and sends the fp of the box on screen as `seen` (a 409 brings
    the fresh box — ConfirmBox passes the fp, never a closure over a stale prop); Reschedule opens on the server's pick
    box and re-asks the preview on each pick (New day from a calendar whose lower bound is the guild's date, Starts from
    quarter hours, Locks only while the event is open); a coming-up night has no Reschedule and opens through its first
    raid; the page lives on run: / member / config and keeps an open box over a reload; the routes are the read model's."""
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert 'useLive(load, ["run:", "member", "config"])' in page and "usePoll(load)" in page
    assert "ev.boxes.lock" in page and "lockBox(ev.key, { box: ev.boxes.lock })" in page, "the lock box with its choice (LockBox)"
    lock = _code_only((SRC / "components" / "events" / "LockBox.tsx").read_text())
    assert "`/api/run/${encodeURIComponent(key)}/lock`" in lock and "{ seen: fp, fill: fillOf() }" in lock
    assert "ev.boxes.cancel" in page and "{ seen: fp, notify: false }" in page and "{ preview: true }" in page
    assert "ev.actions.edit_lineup" in page, "Edit lineup (Task 10) only while the event may change its raids"
    assert "Show older" in page and "history_offset=" in page and 'to="/events/new"' in page
    assert "<HistoryView" in page and "<ComingUpView" in page
    box = _code_only((SRC / "components" / "events" / "RescheduleBox.tsx").read_text())
    assert "ev.boxes.reschedule" in box and 'dayLabel="New day"' in box and "minDate={ev.today}" in box and "replace(" in box
    assert 'ev.state === "open"' in box and 'label="Locks"' in box and "toISOString" not in box
    coming = _code_only((SRC / "components" / "events" / "ComingUpView.tsx").read_text())
    assert "Reschedule" not in coming and "view.open.raid" in coming and "seen: fp" in coming and "navigate(`/events/${" in coming
    assert "disabled={!view.can_open}" in coming and "view.why" in coming
    hist = _code_only((SRC / "components" / "events" / "HistoryView.tsx").read_text())
    assert "<EventDetails" in hist and "<ActivitySection" in hist
    app = _code_only((SRC / "App.tsx").read_text())
    for r in ('path="/events"', 'path="/events/:key"', 'path="/events/coming/:sid/:occurrence"', 'path="/events/new"'):
        assert r in app, r
    assert '<Navigate to="/rosters"' not in app
    assert "navigate(`/events/${encodeURIComponent(r.key)}`)" in _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    some = menu[menu.index("type AnswerPick"):menu.index("export function RepostSheetModal")]
    assert "useConfirmBox(" in some and "preview: true" in some and "seen: fp" in some and "useConfirm(" not in some
    assert "spec" in some and 'label="Character"' in some and 'label="Answer"' in some and 'label="Member"' in some
    sign = _code_only((SRC / "components" / "events" / "SignupsSection.tsx").read_text())
    tabs = _code_only((SRC / "components" / "events" / "SignupTabs.tsx").read_text())  # the tabs body, shared with the members' page
    assert "<MemberMenu" in sign and "<SomeoneElse" in sign and 'tabs={["in", "sub", "out", "none"]}' in sign
    assert "useState<SignupTab>(tabs[0])" in tabs and "<Tabs.Tab" in tabs and 'kind="spec"' in tabs and 'kind="class"' in tabs, "tabs, Joined first, grouped by class"
    act = _code_only((SRC / "components" / "events" / "ActivitySection.tsx").read_text())
    assert "Activity · " in act and "aria-expanded" in act


def test_the_event_board_replaces_suggest_and_preview_lock():
    board = _code_only((SRC / "components" / "events" / "EventBoard.tsx").read_text())
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    assert 'variant="event"' in board and "undo_box" in board and "board_footer" in board and "Suggest" not in board and "lock/preview" not in board
    assert 'variant === "event"' in view and "open seat" in view and "live.dragging" in view and "useConfirmBox" in view


def test_role_switches_are_one_line_moves():
    sw = _code_only((SRC / "components" / "events" / "RoleSwitches.tsx").read_text())
    assert "switches.short" in sw and "show_switches" in sw and "hide_switches" in sw and "/switch" in sw and "row.box" in sw
    assert 'aria-hidden' in sw


def test_a_signups_row_menu_offers_that_members_role_switch():
    """Issue #10 (the owner, 2026-11-25): a Signups row's ⋮ offers the Role switches row the server lists for that member,
    after Join and before Bench, through THE section's own press (useSwitchPress: one route, one box) — the item reads
    page_words.switch_item over the row's button word and `to`, with the spec icon; its box is mounted on the section so a
    menu that closes (or a live reload) never takes it away. The Test bench's cards and the members' page gain nothing."""
    routes = [f for f in SRC.rglob("*.tsx") if re.search(r"/api/run/[^`\"']*/switch", f.read_text())]
    routes += [f for f in SRC.rglob("*.ts") if re.search(r"/api/run/[^`\"']*/switch", _code_only(f.read_text()))]
    assert [f.name for f in routes] == ["RoleSwitches.tsx"], "the switch route is written in one place"
    assert len(re.findall(r"/api/run/[^`\"']*/switch", (SRC / "components" / "events" / "RoleSwitches.tsx").read_text())) == 1
    sw = _code_only((SRC / "components" / "events" / "RoleSwitches.tsx").read_text())
    assert "export function useSwitchPress(" in sw and "useSwitchPress(view, onReload)" in sw, "the section's button presses through the shared hook"
    sec = _code_only((SRC / "components" / "events" / "SignupsSection.tsx").read_text())
    assert "useSwitchPress(ev, onDone)" in sec and "switchPress(r)" in sec and "{switchDialog}" in sec, "the menu item takes the same press; its box lives on the section"
    assert "page_words.switch_item" in sec and "r.button" in sec and "r.to" in sec and 'kind="spec"' in sec and "r.uid === p.uid" in sec
    assert not re.search(r"[\"'`>]\s*(Switch|Answer for them|Ask)\b", sec), "the item's words are the server's"
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    assert "switches?: SwitchItem[]" in menu and "withSwitches(" in menu and '"bench"' in menu and "/switch" not in menu
    assert "switches=" not in _code_only((SRC / "components" / "board" / "SheetCard.tsx").read_text()), "the Test bench's menu is unchanged"
    tabs = _code_only((SRC / "components" / "events" / "SignupTabs.tsx").read_text())
    members = _code_only((SRC / "pages" / "MemberEvents.tsx").read_text())
    assert "switch" not in tabs.lower() and "useSwitchPress" not in members and "MemberMenu" not in members and "right=" not in members


def test_the_event_board_draws_the_servers_picture_and_never_fights_a_drag():
    """Phase 4, Task 9 (Review Focus 3 on the page), as the owner's tweaks C left it: a drop sends ONE move and the page
    draws what the server answers (no optimistic place); a reload that lands mid-drag waits for the drop; open seats are
    the server's roles (role icon + page_words.open_seat); Not placed carries the server's note (a name there is only not
    placed yet — removed by issue #12) and a kept-off name the server's word; Auto-fill groups sits beside Not placed with the server's label,
    working line and outcome sentence; Undo is the server's box and verb; the history shows the board as it was locked.
    Role switches print the server's rows, why sentences and button words, send the plan rev and `seen`, and keep their
    fold per viewer."""
    board = _code_only((SRC / "components" / "events" / "EventBoard.tsx").read_text())
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    hist = _code_only((SRC / "components" / "events" / "HistoryView.tsx").read_text())
    sw = _code_only((SRC / "components" / "events" / "RoleSwitches.tsx").read_text())
    assert view.count('send("move"') == 1 and "shown" not in view, "a drop is one move on the board as the server holds it"
    assert "page_words.open_seat" in view and "r.open" in view and "page_words.kept_off" in view
    assert "not_placed_off" not in view and "board_working" not in board and "board_error" not in board
    assert "bankAction={af ?" in board and "words.autofill_button" in board and "af.words" in board and "<Loader" in board
    assert "/autofill`" in board and "af.working" in board
    assert "undo: true" in board and "seen: fp" in board and "preview: true" in board and "useConfirmBox(" in board
    assert '"Board, as it was locked"' in board and "<EventBoard" in hist
    assert "afterDrag(" in page, "a reload answered mid-drag is drawn after the drop"
    order = [page.index(s) for s in ("<SignupsSection", "<RoleSwitches", "<EventBoard", "<ActivitySection")]
    assert order == sorted(order)
    assert "row.button" in sw and "s.why" in sw and "switches.revs[row.part]" in sw and "seen: fp" in sw and "localStorage" in sw
    assert not re.search(r"[\"'`>]\s*(Switch|Answer for them|Ask)\s*[\"'`<]", sw), "the button words are the server's"
    assert "VisuallyHidden" in sw


def test_the_events_page_fix_round_one():
    """Phase 4, Task 8 fix round 1. (1) The Signups row ⋮ sends Join / Bench / No thanks through the SERVER's answer box
    (useAnswerBox, the member fixed); the rest of the menu (useRecord) asks in the server's box since phase 5, Task 8
    (test_the_row_menu_prints_the_servers_box), and the Test bench passes no `onAnswer`. (2a) Reschedule starts from the server's start_day /
    start_time. (2d) Answer for someone stays mounted while live, the rows' box lives on the section, and EventPane keeps
    the live view while a box is open. (2e) the latest GET wins. (2f) the "changed" line only for a 409's fresh box."""
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    assert "onAnswer && ANSWERS.has(it.a.key) ? onAnswer(it.a.key, it.character) : record(uid, it)" in menu
    assert "meta.classes" not in menu and "c.plays" in menu, "only the specs the server takes"
    sign = _code_only((SRC / "components" / "events" / "SignupsSection.tsx").read_text())
    assert "useAnswerBox(ev.key, onDone)" in sign and "onAnswer={answerFor(p)}" in sign and "{answerDialog}" in sign
    assert "live ? <SomeoneElse" in sign and "ev.people.length > 0 ?" not in sign
    for f in ("components/board/SheetCard.tsx", "components/TestBench.tsx"):
        assert "onAnswer" not in (SRC / f).read_text(), f
    for f in (SRC / "components" / "events").glob("*.tsx"):
        assert "useConfirm(" not in _code_only(f.read_text()), f.name
    box = _code_only((SRC / "components" / "events" / "RescheduleBox.tsx").read_text())
    assert "day: ev.start_day, time: ev.start_time" in box and "start_iso" not in box
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "if (n !== seq.current) return;" in page and "useAnyBoxOpen()" in page and "held && lastLive.current" in page
    confirm = _code_only((SRC / "components" / "ConfirmBox.tsx").read_text())
    assert "setChanged(fresh.verb !== null)" in confirm and "setChanged(true); } else close(true)" not in confirm
    tabs = _code_only((SRC / "components" / "events" / "SignupTabs.tsx").read_text())  # the tabs body moved there
    assert "{words[t].empty}" in tabs and "disabled" not in sign + tabs, "an empty tab says so in a sentence, never a greyed tab"
    assert "const first = await preview(now);\n      setBusy(false);" in menu, "the button behind an open box is not busy"


def test_the_row_menu_prints_the_servers_box():
    """Phase 5, Task 8: the row ⋮ menu's callouts, pins, Keep off the roster and the confirm / fill answers ask in the
    SERVER's box (consequences.member_action_box through POST /api/admin/action `preview`) and write with `seen` — on the
    Events page and on the Test bench's SheetCard alike (it passes no `onAnswer`). No page-made title, message or verb."""
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    rec = menu[menu.index("function useRecord"):menu.index("export type MoveTarget")]
    assert "useConfirmBox(" in rec and "preview: true" in rec and "seen: fp" in rec
    assert "useConfirm(" not in menu and "confirmLabel" not in menu and "message: consequence" not in menu
    assert 'it.a.key === "callout" ? <NoteField' in rec and "<Textarea" in menu, "a callout's note is a field inside the box"
    assert "rev: list.rev" not in menu, "the box's fp is the check, never the list's older rev"


def test_a_drop_shifts_nothing_and_draws_the_moves_own_answer():
    """Issue #12 (the owner, 2026-11-25): a drop pops in no "saving…" line and Not placed carries no how-to sentence —
    nothing under the bank's heading but Auto-fill groups; the moved name is dimmed in place while the move is written
    (an opacity, never a line that pushes the page down) and a refusal leaves it where it was with the server's sentence
    (`fail`) and draws the board the refusal carries (a 409's fresh board). The Events board is drawn from the move's own
    answer at once — the page fetches nothing of its own after a move (the live `run:` reload brings the rest, ONE read
    per drop, not two) — and a read SENT before that answer never draws the board back (`sentAt`)."""
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    board = _code_only((SRC / "components" / "events" / "EventBoard.tsx").read_text())
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "saving…" not in view and "saving..." not in view, "no saving line"
    assert "placing the rest…" in view, "the Test bench's Place the rest keeps its working line (a solve, seconds long)"
    assert "not_placed_note" not in view and "not_placed_note" not in _code_only((SRC / "api.ts").read_text())
    assert "opacity: moving === s.display_name ? 0.5 : undefined" in view and "setMoving(null)" in view
    assert 'send("move", { member, to, state: e.state === "open" ? "open" : "locked" }, member)' in view
    assert "if (b?.board) onBoard(e.key, b.board, b.needs, b.rev);" in view and "fail(err);" in view, "a refusal draws the board it carries and says why"
    assert "onBoard={() => onReload()}" not in board and "setAnswered({ key, board, needs, rev, at: performance.now() })" in board
    assert "answered.at > sentAt" in board and "e={sheet}" in board
    assert "const at = performance.now();" in page and "setSentAt(at)" in page and "sentAt={sentAt}" in page


def test_the_event_board_fix_round_one():
    """Phase 4, Task 9 fix round 1: the event board prints no advisory line ("🔴 Tanks 0/2 · …": a text count beside
    icons, duplicating Short) and no ⓘ legend (not on the mockup; its words were page copy); its empty Not placed and
    Undo's greyed reason are the server's words; a finished board shows no confirmation marks; Move to… offers a swap
    with a named person in a full group (a phone can't drag); a locked raid reads Rostered; one name when the character
    is the member's name; reloads held by a drag share ONE pending slot and one watcher, cleared on unmount."""
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    board = _code_only((SRC / "components" / "events" / "EventBoard.tsx").read_text())
    shared = _code_only((SRC / "components" / "board" / "shared.tsx").read_text())
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    assert "!event && r.advisories.length > 0" in view and "!event && !readOnly && <Legend" in view
    assert "meta.page_words.bank_empty" in view and '"Everyone who joined is placed."' not in view
    assert "locked && !readOnly" in view, "a finished board's names carry no confirmation mark"
    assert "swapWith" in view and "aria-label={m.aria" in menu
    assert "part.undo_why" in board and "disabled" in board and "rostered_words" in board and '"Rostered"' in board
    assert "s.character !== s.display_name" in shared
    assert "export function useAfterDrag" in shared and "clearInterval" in shared and "pending.current = fn" in shared
    assert "function afterDrag" not in shared, "no per-call interval left"
    assert "const afterDrag = useAfterDrag()" in page


def test_rosters_is_gone_and_redirects_to_events():
    app = _code_only((SRC / "App.tsx").read_text())
    assert not (SRC / "pages" / "Rosters.tsx").exists() and "RostersRedirect" in app and '"/events/:key/lineup"' in app


def test_edit_lineup_keeps_the_first_raid():
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert 'mode === "edit-lineup"' in page and "fixedFirst" in page and "/lineup" in page and "preview: true" in page


def test_edit_lineup_prints_the_servers_box_and_guards_its_draft():
    """Phase 4, Task 10: Edit lineup is the builder with its first raid fixed, seeded from the event's own lineup and
    started at its clock; "What this makes" and every step's start are the server's (taken only while the answer's raids
    and starts ARE the builder's — never times borrowed from another lineup); Save asks with the server's lineup box
    and presses with the fp of the box on screen (a 409's fresh box goes through on the next press); a locked or finished
    event shows the server's refusal and only Back; the draft is registered with its count and the event's title."""
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert "/api/event/${" in page and "/lineup`" in page and "fixedFirst" in page and "startClock={edit.start_time}" in page
    assert "useDirty(dirtyKey, changes > 0 && !edit?.refusal_box, { n: changes, name: dirtyName })" in page and "`lineup:${evKey}`" in page
    assert "x.lineup.map((s) => `${s.raid}:${s.starts}`).join() === lineup.map((e) => `${e.raid}:${e.starts}`).join()" in page
    assert "box: first.box" in page and "seen: fp" in page and "refresh:" in page
    assert "edit.refusal_box" in page and "guardNav(back)" in page
    assert "navigate(`/events/${encodeURIComponent(evKey)}`" in page
    app = _code_only((SRC / "App.tsx").read_text())
    assert 'mode="edit-lineup"' in app
    events = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "ev.actions.edit_lineup" in events, "hidden once the roster locks"


def test_every_link_to_a_run_lands_on_its_event():
    """Phase 4, Task 10: the Rosters page is gone. Old addresses redirect (/rosters → /events, /rosters#run-<key> →
    /events/<key>, a later raid's key answers as its event, a gone key prints the server's sentence) and nothing on the
    site links /rosters any more, and nothing links a Me row (#run-<key>): every event link opens the event's page."""
    app = _code_only((SRC / "App.tsx").read_text())
    red = app[app.index("function RostersRedirect"):]
    red = red[:red.index("\n}\n")]
    assert 'hash.startsWith("#run-")' in red and "`/events/${encodeURIComponent(" in red and "`/events${search}`" in red and "replace" in red
    assert 'path="/rosters" element={<RostersRedirect />}' in app
    hits = []
    for f in SRC.rglob("*.ts*"):
        code = _code_only(f.read_text())
        if f.name == "App.tsx":
            code = code.replace('path="/rosters"', "")
        if re.search(r"[\"'`]/rosters\b|#run-\$\{", code) and f.name not in ("Me.tsx",):
            hits.append(f.name)
    assert not hits, hits
    assert '{ to: "/rosters"' not in app, "no rail entry points at a page that is gone"


GONE = re.compile(r"Rosters (page|tab|card|strip)|Rosters ›|on Rosters\b|Propose roster", re.I)
# a line that tells the history (the page BECAME the Events page) may name it
HISTORY = re.compile(r"became the Events page|is the Events page now|Rosters page became|replaced the Rosters page|was the Rosters")


def test_nothing_points_a_reader_at_a_page_or_control_that_is_gone():
    """Fix round 1: the Rosters page, its tabs and the event board's Propose roster are gone, so no word a person reads —
    a page, a tooltip, a toast, a Discord reply, a tool docstring a model reads — and no comment points at them. The
    only Propose roster left is the Test bench card's board (BoardView / SplitModal) and the manual's account of it."""
    hits = []
    for f in sorted(SRC.rglob("*.ts*")):
        if f.name in ("BoardView.tsx", "SplitModal.tsx"):
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if GONE.search(line) and not HISTORY.search(line):
                hits.append(f"{f.relative_to(SRC)}:{n}: {line.strip()[:120]}")
    py = SRC.parents[1] / "src" / "oibot_gm"
    for f in sorted(py.rglob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if GONE.search(line) and not HISTORY.search(line):
                hits.append(f"{f.relative_to(py)}:{n}: {line.strip()[:120]}")
    assert not hits, "\n".join(hits)


def test_the_event_page_shows_its_raid_night_facts_and_split_strategy():
    """Fix round 1: the restored raid-night facts are the server's Details (the page adds none), the time zone line and
    the test run chip are the server's words; More's Split strategy… asks through the server's box with the choices it
    sends and presses with the box's fp; Withdraw spins on its own key; Remind and End name the run as the server does;
    Edit lineup's draft counts against the event's own lineup and lands on the encoded key."""
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "intro={list.tz_words}" in page and "tag={ev.test_chip}" in page
    assert "tag={ev.test_chip}" in _code_only((SRC / "components" / "events" / "HistoryView.tsx").read_text())
    more = _code_only((SRC / "components" / "events" / "EventMore.tsx").read_text())
    # each raid's split moved to Edit event (the owner's tweaks, B): one editor for the event's own settings
    assert "Split strategy…" not in more and "/strategy`" not in more
    edit = _code_only((SRC / "components" / "events" / "EditEvent.tsx").read_text())
    assert "split_policy" in edit and "e.splits.map(" in edit
    assert "busy={busy}" in more and 'busy ? "withdraw" : null' not in more and "post(path, payload, k)" in more
    assert "${L.remind} for" not in more and "End ${s.run_title}?" not in more and " on ${name}?" not in more, "the run is named in the server's box"
    lineup = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    edit = lineup[lineup.index("function EditLineup"):]
    assert "navigate(`/events/${encodeURIComponent(evKey)}`" in edit and "setChanges" not in edit
    assert "JSON.stringify(lineup[k] ?? null) !== JSON.stringify(seed[k] ?? null)" in edit


def test_the_officers_rail_is_the_final_set():
    """Phase 4, Task 12: the officers' rail = Events, Schedules, Raids, then Members, Applications, Loot, Auras, Ops,
    Policy, Agents, Config with the brief's icons; no entry for a page that is gone; Tell the bot covers every officer
    page's own sub-paths (an event, a schedule, a raid, New event, Edit lineup)."""
    app = _code_only((SRC / "App.tsx").read_text())
    rail = app[app.index("const OFFICER = ["):]
    rail = rail[:rail.index("];")]
    entries = re.findall(r'\{ to: "([^"]+)", label: "([^"]+)", icon: (\w+) \}', rail)
    assert [label for _, label, _ in entries] == ["Events", "Schedules", "Raids", "Members", "Applications", "Loot", "Auras",
                                                  "Ops", "Policy", "Agents", "Config"]
    icons = {label: icon for _, label, icon in entries}
    assert icons["Events"] == "IconCalendarEvent" and icons["Schedules"] == "IconRepeat" and icons["Raids"] == "IconMountain"
    for gone in ("/rosters", "/readiness", "/bank", "/admin"):
        assert gone not in [to for to, _, _ in entries], gone
    for to, _, _ in entries:
        assert f'path="{to}"' in app, f"{to}: a rail entry with no page"
    assert 'OFFICER.some((n) => loc.pathname === n.to || loc.pathname.startsWith(n.to + "/"))' in app, "Tell the bot on sub-paths"


def test_shots_shoots_the_final_pages():
    shots = (SRC.parents[1] / "scripts" / "shots.py").read_text()
    pages = re.search(r"PAGES = \[(.*?)\]", shots, re.S).group(1)
    for p in ("/app/events", "/app/events/new", "/app/schedules", "/app/schedules/new", "/app/raids"):
        assert f'"{p}"' in pages, p
    for gone in ("/app/rosters", "/app/readiness", "/app/bank"):
        assert f'"{gone}"' not in pages, gone


def test_a_refusal_without_a_server_box_never_carries_the_question_title():
    """Task 12 (Task 6's carried minor): with a verb, a refusal the server sent no box for (line errors, `refused`, a
    write refused after the press) is titled with the server's refusal title (/api/meta.confirm_words.refused), never the
    page's question ("Save Hyjal Summit?")."""
    review = _code_only((SRC / "components" / "ReviewModal.tsx").read_text())
    box_mode = review[review.index("if (boxed) {"):review.index("title={result ?")]
    fallback = box_mode[box_mode.index("const box: Box = said ||"):]
    fallback = fallback[:fallback.index("\n")]
    assert "title: boxWords().refused" in fallback and "title: title" not in fallback and "title ||" not in fallback
    assert "refused: string" in (SRC / "api.ts").read_text()


def test_the_final_look_fixes():
    """Task 12's final look, each a difference from the mockups or the phone rules found on the scratch server:
    the phone header's menu button is thumb-sized and the brand in the headings' font (a bare "Manrope" fell back to a
    serif); Tell the bot… and Refresh are buttons, so the keyboard reaches them; the rail's links, a select's options,
    a Back to … link and a switch's row are 44 px on a phone; Reschedule's box is wide enough for New day · Starts ·
    Locks on one row, as the mockup; Open this run early is a plain button (red is for what cannot be undone)."""
    app = _code_only((SRC / "App.tsx").read_text())
    assert 'aria-label="Pages" style={{ width: 44, height: 44 }}' in app and 'ff="heading"' in app and 'ff="Manrope"' not in app
    assert '<NavLink component="button" mt="md" label="Tell the bot…"' in app and '<NavLink component="button" mt="auto" label="Refresh"' in app
    assert app.count("minHeight: mobile ? 44 : undefined") >= 3
    css = (SRC / "pages" / "listpage.module.css").read_text()
    for rule in ("[data-combobox-option][data-combobox-option]) { min-height: 44px", ".touch :global(.mantine-Anchor-root) { display: inline-flex; align-items: center; min-height: 44px",
                 ".touch :global(.mantine-Switch-body) { min-height: 44px"):
        assert rule in css, rule
    box = _code_only((SRC / "components" / "ConfirmBox.tsx").read_text())
    assert 'size={open?.size ?? (open?.wide ? 600 : "md")}' in box and "wide?: boolean" in box
    assert "wide: true" in _code_only((SRC / "components" / "events" / "RescheduleBox.tsx").read_text())
    regs = _code_only((SRC / "components" / "schedules" / "RegularsSection.tsx").read_text())
    early = regs[regs.index("openEarly(f)}>") - 200:regs.index("openEarly(f)}>")]
    assert 'color="red"' not in early


def test_the_final_review_pages_fixes():
    """The phase 4 final review, the pages' half. (1) Clock rule: a raid's First opens is a calendar day and a 15-minute
    select (WhenPicker), never a typed DateTimePicker, and no phase 4 page imports a typing time picker. (2) Remind, End
    the run and a LOCKED event's board moves print the server's box (preview) and press with `seen`; a 409's box comes
    back; the Test bench's board keeps its own confirm. (3) New event and New schedule count differences from their
    seed, never presses. (4) A Save's spinner stops when the server answers (onShown), never behind the open box. (5)
    Split strategy's picks: the newest answer wins. (6) A regular's Remove refreshes the rev with a 409's box. (7)
    The pages' GETs: the latest answer wins; Coming up waits for an open box before it follows the night's event. (8)
    The pages' own sentences are the server's PAGE_WORDS; the box's verb is the server's. (10) No dead icon helpers;
    keys in URLs are encoded."""
    rules = _code_only((SRC / "components" / "raids" / "RulesView.tsx").read_text())
    assert "DateTimePicker" not in rules and "<WhenPicker" in rules and 'label="First opens"' in rules
    assert "`${day}T${time}`" in rules, "the Save sends the same machine form as before"
    for f in ("pages/Raids.tsx", "pages/Events.tsx", "pages/Schedules.tsx", "pages/LineupPage.tsx", "components/raids/RulesView.tsx",
              "components/events/RescheduleBox.tsx", "components/WhenPicker.tsx"):
        code = _code_only((SRC / f).read_text())
        assert not re.search(r"\b(DateTimePicker|TimeInput|TimePicker)\b", code), f"{f}: a time is picked from 15-minute steps"
    more = _code_only((SRC / "components" / "events" / "EventMore.tsx").read_text())
    boxed = more[more.index("async function boxed("):more.index("const remind")]
    assert "preview: true" in boxed and "seen: fp" in boxed and "boxOf_(e)" in boxed and "refresh: preview" in boxed
    board = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    server = board[board.index("async function serverMove("):board.index("async function place(")]
    assert "preview: true" in server and "seen: fp" in server and 'state: "locked"' in server and "b?.board" in server
    assert 'fp: ""' not in board, "no page-made box on the board"
    assert "if (event && locked) {" in board and "await serverMove(name, target)" in board
    place = board[board.index("async function place("):]
    assert "confirmMove(" in place, "the Test bench's board keeps its own confirm"
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    new = page[page.index("function NewEvent("):page.index("function EditLineup(")]
    assert "setChanges" not in new and "seed.lineup[k]" in new and "lock !== seed.lock" in new
    assert "{!refusal && <Button disabled={!ready}" in new, "a refusal box has Back alone"
    editor = page[page.index("function ScheduleEditor("):]
    assert "setMade" not in editor and "newEdits(d, seed.current)" in editor and "onShown: () => setBusy(false)" in editor
    assert "title:" not in editor[editor.index("async function saveSchedule"):editor.index("const fromData")], "the box's title is the server's"
    assert "W.weight_same" in editor and "W.schedule_no_lineup" in editor
    assert "meta.page_words.new_event_no_lineup" in new and "meta.page_words.new_event_no_time" in new
    raids = _code_only((SRC / "pages" / "Raids.tsx").read_text())
    assert "onShown: () => setBusy(false)" in raids and "n === seq.current" in raids
    assert "meta.page_words.raids_empty" in raids and "meta.page_words.raid_missing" in raids
    review = _code_only((SRC / "components" / "ReviewModal.tsx").read_text())
    assert "? said.verb : null" in review and "opts?.onShown?.()" in review
    regs = _code_only((SRC / "components" / "schedules" / "RegularsSection.tsx").read_text())
    assert "if (x) shown.rev = x.rev;\n            return b;" in regs
    sched = _code_only((SRC / "pages" / "Schedules.tsx").read_text())
    assert "n === listSeq.current" in sched and "n === seq.current" in sched and "meta.page_words.schedules_empty" in sched
    coming = _code_only((SRC / "components" / "events" / "ComingUpView.tsx").read_text())
    assert "n === seq.current" in coming and "useAnyBoxOpen()" in coming and "if (openedAt && !held)" in coming and "boxWords().refused" in coming
    events = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "meta.page_words.events_empty" in events
    assert "pageWords().activity_empty" in _code_only((SRC / "components" / "events" / "ActivitySection.tsx").read_text())
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    assert "pageWords().nothing_applies" in menu and "pageWords().repost_sheet" in menu
    for f, words in (("pages/Events.tsx", "No event yet"), ("pages/Schedules.tsx", "No schedule yet"), ("pages/Raids.tsx", "isn't in this game"),
                     ("components/events/ActivitySection.tsx", "The log fills"), ("components/board/MemberMenu.tsx", "Nothing applies"),
                     ("components/board/MemberMenu.tsx", "The live sheet moves"), ("pages/LineupPage.tsx", "Add a raid to the lineup")):
        assert words not in _code_only((SRC / f).read_text()), f"{f}: {words} is the server's word now"
    icons = _code_only((SRC / "components" / "Icons.tsx").read_text())
    assert "SlotIcon" not in icons and "RoleIcon" not in icons
    for f in ("components/events/EventMore.tsx", "components/board/BoardView.tsx", "pages/LineupPage.tsx", "components/board/FillModal.tsx"):
        code = _code_only((SRC / f).read_text())
        assert not re.search(r"`/(api/run|api/loot|events|api/raid)/\$\{(?!encodeURIComponent)", code), f"{f}: an unencoded key in a URL"
    board_head = _code_only((SRC / "components" / "events" / "EventBoard.tsx").read_text())
    assert "!(finished && parts.length === 1)" in board_head, "a finished event says Rostered once"
    assert "minHeight: mobile ? 44 : undefined }}>log out</a>" in _code_only((SRC / "App.tsx").read_text())


def test_edit_event_is_one_editor_on_the_schedule_editors_own_pieces():
    """The owner's tweak B: Edit event (the header's third action) edits the event's every own setting with the SAME
    Sheet timing and Priority components the schedule editor uses (one component, never a copy), pickers only; Save
    previews the server's box (`edit: true, preview: true`), presses with the box's fp as `seen` (a 409's fresh box is
    the next press's), and the draft is compared with its seed under the leave guard."""
    shared = _code_only((SRC / "components" / "schedules" / "SettingsFields.tsx").read_text())
    assert "export function TimingFields(" in shared and "export function PriorityFields(" in shared
    sched = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    edit = _code_only((SRC / "components" / "events" / "EditEvent.tsx").read_text())
    for f in (sched, edit):
        assert "<TimingFields" in f and "<PriorityFields" in f
    assert "hourSelect" not in sched and "weightData" not in sched, "the schedule editor keeps no copy of the pieces"
    assert "edit: true" in edit and "preview: true" in edit and "seen: fp" in edit and "boxOf(x)" in edit and "refresh: preview" in edit
    assert "useDirty(`event:${evKey}`" in edit and "changesOf(d, seed.current, fields)" in edit and "className={css.touch}" in edit
    assert not re.search(r"\b(DateTimePicker|TimeInput|TimePicker|TextInput|NumberInput)\b", edit), "pickers only"
    assert "W.edit_locked" in edit and "W.edit_over" in edit, "the editor's states are the server's words"
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "ev.actions.edit" in page and "/edit`" in page and "Edit event" in page
    app = _code_only((SRC / "App.tsx").read_text())
    assert 'path="/events/:key/edit"' in app


def test_each_sheet_timing_switch_carries_the_servers_help():
    """The owner, 2026-11-24: the four Sheet timing switches read as what they do (Signup Reminder, Lock Confirmation,
    Automated Fill Requests, Sheet Opening DM) and each has a (?) whose sentence is the SERVER's (PAGE_WORDS
    schedule_field_help) — hover, keyboard focus and a tap open it, a 44 px target, a screen-reader name from the
    server's help_about. One component, so the schedule editor and Edit event both carry it."""
    from oibot_gm.constants import PAGE_WORDS, schedule_line_label

    F, help_ = PAGE_WORDS["schedule_fields"], PAGE_WORDS["schedule_field_help"]
    assert (F["nudge"], F["confirm"], F["autofill"], F["open_dm"]) == ("Signup Reminder", "Lock Confirmation", "Automated Fill Requests", "Sheet Opening DM")
    for sw, sel in (("nudge", "nudge_hours_before"), ("confirm", "confirm_hours_before"), ("autofill", "fill_ask_hours")):
        assert not set(F[sw].lower().split()) & set(F[sel].lower().split()), (sw, sel, "a switch and its select never say one word twice")
    assert schedule_line_label("nudge_hours_before") == "Signup Reminder goes out"
    from oibot_gm import raidcycle as rc
    assert (rc.FILL_OVERASK, rc.FILL_MAX_OPEN) == (1, 3) and "one more person than there are open seats, up to three at once" in help_["autofill"], \
        "the help says the fill engine's over-ask and cap as they are"
    assert "confirming a fill request isn't asked again" in help_["confirm"]
    # the cleanup after issue #13: the switch names itself, and its select reads as a phrase ("as long as An ask waits")
    assert f"with {F['autofill']} off" in help_["autofill"] and "with this off" not in help_["autofill"]
    assert f"no answer for as long as {F['fill_ask_hours']}, asks the next person" in help_["autofill"]
    assert set(help_) == {"nudge", "confirm", "autofill", "open_dm", "auto_lock", "auto_lock_fill"}
    assert f"With {F['auto_lock']} off" in help_["auto_lock"] and f"With {F['auto_lock_fill']} off" in help_["auto_lock_fill"]
    for k, text in help_.items():
        sentences = [x for x in text.split(". ") if x]
        assert 1 <= len(sentences) <= 4 and text.endswith(".") and text[0].isupper(), (k, text)
        assert all(len(x.split()) <= 20 for x in sentences), (k, text)
        assert not re.search(r"\b(nudge|autofill|solver|cadence|pool|seated|series)\b|:\d\d|\d{2}:\d{2}", text, re.I), (k, text)
    shared = _code_only((SRC / "components" / "schedules" / "SettingsFields.tsx").read_text())
    for needle in ("words.schedule_field_help", "<FieldHelp", "words.help_about"):
        assert needle in shared, needle
    fh = _code_only((SRC / "components" / "FieldHelp.tsx").read_text())  # the one (?) — the editors' and the views' (issue #13)
    for needle in ("size={44}", "onFocus=", "onMouseEnter=", "onClick=", "closeOnEscape", 'role="tooltip"', "aria-label={label}",
                   'maw="calc(100vw - 32px)"'):
        assert needle in fh, needle
    for page in ("pages/LineupPage.tsx", "components/events/EditEvent.tsx"):
        assert "<TimingFields" in _code_only((SRC / page).read_text()), page


def test_the_boards_older_confirms_are_the_test_benchs_alone():
    """Phase 5, Task 9 (H): BoardView's useConfirm ("Clear the board?" and confirmMove) never runs on the Events page
    (variant="event"): Clear is drawn only off the Events page, and an event's move either goes at once (before the lock)
    or asks the server's box (serverMove, after it) before any confirmMove. Only the Test bench's SheetCard reaches them;
    the Events page clears through More › Clear the board (consequences.clear_board_box)."""
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    clear_block = view.index("{!locked && !event && (")
    assert clear_block < view.index("onClick={clear}") < view.index("</Group>", clear_block), "Clear: the Test bench's toolbar only"
    assert view.count("onClick={clear}") == 1
    assert 'const asks = locked || (via === "menu" && !event);' in view, "before the lock an event's move asks nothing"
    head = view.index("if (event && locked) {")
    assert head < view.index("await serverMove(name, target); return;") < view.index('confirmMove("release"'), "a locked event asks the server's box first"
    assert view.index("async function place(") < head


def test_member_menu_record_reloads_after_a_refusal_without_a_box_too():
    """Final fix 9: useRecord's onDone runs in a finally — after a press and after an error the server sent no box for."""
    src = (SRC / "components" / "board" / "MemberMenu.tsx").read_text()
    body = src[src.index("function useRecord("):src.index("export function MemberMenu(")]
    assert "catch (e) { fail(e); reload = true; }" in body and "finally { if (reload) await onDone(); }" in body


def test_me_page_says_when_the_event_link_matches_no_row():
    """Final fix 7: a finished or cancelled event's sheet title lands on /app/me#run-<key> with no row; the page prints
    the server's words (PAGE_WORDS), none typed in the page."""
    from oibot_gm.constants import PAGE_WORDS

    src = (SRC / "pages" / "Me.tsx").read_text()
    assert "meta.page_words.me_event_over" in src and "meta.page_words.me_event_over_tail" in src
    assert "finished or was cancelled" in PAGE_WORDS["me_event_over"] and "finished or was cancelled" not in src


def test_tell_the_bot_preview_reply_keeps_its_line_breaks():
    """Answer tools, Task 8 fix 2: the loop's answer in the preview frame carries "• choice" lines and a blank line
    before its commands — the reply keeps line breaks, as the answer Alert does."""
    src = (SRC / "components" / "TellTheBot.tsx").read_text()
    line = next(ln for ln in src.splitlines() if "{p.reply &&" in ln)
    assert 'whiteSpace: "pre-line"' in line, line


def test_one_answer_component_serves_me_and_the_member_event_page():
    """The Me page's answer row (Join / Bench / No thanks, the character pick, Can't make it, Leaving after, I'm staying)
    is ONE component, MyAnswer, that the Me page uses and the members' event page will: the routes live there alone."""
    comp = _code_only((SRC / "components" / "MyAnswer.tsx").read_text())
    assert "/api/me/run/" in comp and "export function MyAnswer(" in comp
    assert "characters" in comp and "link" in comp and "`/events/${encodeURIComponent(s.key)}`" in comp
    me = _code_only((SRC / "pages" / "Me.tsx").read_text())
    assert 'from "../components/MyAnswer"' in me and "<MyAnswer " in me
    assert "function SheetRow" not in me and "/api/me/run/" not in me


def test_signup_tabs_are_shared_and_worded_by_the_server():
    """The Signups tabs grouped by class are one component, SignupTabs; their labels and empty sentences come from
    meta.page_words.signup_tabs, never typed in either file."""
    sign = _code_only((SRC / "components" / "events" / "SignupsSection.tsx").read_text())
    tabs = _code_only((SRC / "components" / "events" / "SignupTabs.tsx").read_text())
    assert 'from "./SignupTabs"' in sign and "<SignupTabs " in sign
    for f in (sign, tabs):
        assert '"No answer yet"' not in f and '"Nobody has joined yet."' not in f and '"Joined"' not in f
    assert "page_words.signup_tabs" in tabs and "page_words.called_out" in tabs


def test_the_member_roster_has_no_officer_controls():
    """The members' read-only roster: the board's group layout, one member per line, no menu, drag, pin or write."""
    roster = _code_only((SRC / "components" / "events" / "MemberRoster.tsx").read_text())
    for w in ("MemberMenu", "draggable", "PinMark", "onBoard", "api.post"):
        assert w not in roster, w
    assert "<SeatLine" in roster and "<RoleCounts" in roster and "run_words.bank.locked" in roster and "part.marks" in roster


def test_the_event_board_draws_a_bench_section_from_the_servers_words():
    """Bench last, Task 7: before the lock the event board's bank has a second section under Not placed — the Bench
    answers no group holds (`board.bench`), headed by the server's word (run_words.bank.bench) and its count, each a
    draggable line like a Not placed line; an empty Bench section is not drawn (no sentence on the board), and a drop on
    it un-places the name as a drop on Not placed does."""
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    api = _code_only((SRC / "api.ts").read_text())
    assert "bank.bench" in view and "board.bench" in view and '"Bench"' not in view and ">Bench<" not in view
    assert "bench?: Seat[]" in api
    assert 'zone("bench", () => drop("bank"))' in view, "a name dropped on the Bench section goes back to the bank"
    assert "bench_note" not in view and "bench_empty" not in view, "no filler text on the board (the owner)"


def test_a_bench_names_move_to_says_bench():
    """The cleanup after issue #13: before the lock a placed Bench answer's ⋮ Move to… names its way back with the
    server's Bench word (run_words.bank.bench), since the name lands in the Bench section, not Not placed. Who is a Bench
    answer comes from the sheet's own signups (status "sub"); a joiner, and any name once locked, keep the bank's word."""
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    moves = view[view.index("const movesFor"):view.index("const swapWith")]
    assert 'answers[name] === "sub"' in view and "words.bank.bench" in moves, moves
    assert "event && !locked" in moves, "a locked board has no Bench section: its bank word stays"


def test_an_open_event_board_never_draws_open_seats():
    """F4 (2026-11-27): the Events page drew a move's own answer, and that answer carried the locked roster's open seats,
    so the dashed red "open seat" rows flashed on an open board. The server answers `open: None` before the lock
    (web.api board_reply); BoardView also draws them only on a locked event board, whatever an answer carries."""
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    at = view[view.index("const openAt = "):]
    at = at[:at.index("\n  };\n")]
    assert re.search(r"const roles = event && locked && r\.open \?", at), "open seats only on a locked event board"
