"""The copy rule (user decision 2026-10-24, CLAUDE.md Principles; the glossary is docs/manual.md §1a "The words"): the
words a person reads — on the site and in Discord — never carry the code's nouns, and an empty state says what is
missing and how to fill it. Static checks, no runner (like tests/test_frontend_source.py): the extractors below pull
only the USER-VISIBLE strings — JSX text and the literals passed to label= / title= / description= / placeholder= /
intro= / tooltip= / message= in frontend/src/**/*.tsx, and the prose string literals of the Discord-facing modules
(not custom_id templates, dict keys, log lines, docstrings or comments) — and look for the forbidden words in them.

A hit is either a sentence to rewrite in the reader's words (the glossary's) or an entry in ALLOW below, with a reason
a reviewer could disagree with. The sweep list is out/copy_findings.md (`uv run python tests/test_copy.py` rewrites it).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
PY = ROOT / "src" / "oibot_gm"

# (path fragment, exact substring of the extracted text, reason). A reason is a sentence a reviewer could disagree with.
# The exceptions the rule names (CLAUDE.md): the operator surfaces (the Host card, the Agents page — launchd, the data
# repo's rev) and code a person types or pastes (an install line, a run key shown as a key). Text in backticks, URLs and
# Discord mentions / stamps are dropped before matching (_reads), so none of them needs an entry today; an entry is added
# only when one of those surfaces carries a hit, scoped to the substring that is the operator's or the code's word.
ALLOW: list[tuple[str, str, str]] = [
    # operator surfaces (CLAUDE.md's Copy rule): a news-review job's "run" is one pass of the apply job, not a raid event;
    # the proposal cards sit in the ops channel and on the Agents page, both read by the owner and officers who run them
    ("oibot_gm/news.py", "next run", "the apply job's next pass on the owner's proposal screens, a job run and never a raid event"),
    ("oibot_gm/news.py", "apply run", "one pass of the apply job named on the Agents page's recovery screen, a job run not an event"),
    ("oibot_gm/news.py", "run the jobs", "the verb: the owner's MCP token starts the news jobs, the lint's verb list misses this phrasing"),
    ("oibot_gm/discord_news.py", "next run", "the apply job's next pass on an ops-channel proposal card, a job run and never a raid event"),
    ("oibot_gm/discord_news.py", "has the full run", "the job's transcript on the Agents page, an operator surface where a run is one job pass"),
    # the Agents page (operator surface, owner only): a run is one pass of the review or apply job on the host, never a
    # raid event — the job's own word, the same as launchd's and the transcripts' (out/agents/runs/)
    ("pages/Agents.tsx", "at its next run", "the apply job's next pass after an approval, said on the owner's proposal screen"),
    ("pages/Agents.tsx", "Live run", "the card's title over the job pass being streamed now, a job run not an event"),
    ("pages/Agents.tsx", "no runs yet", "the job card's hint before either news job has made a pass on this host"),
    ("pages/Agents.tsx", "in this run yet", "a job pass with no transcript step yet, the operator's own word for it"),
    ("pages/Agents.tsx", "haven't run on this Mac", "the verb: the jobs have not been started on the host, the verb list misses it"),
    ("pages/Agents.tsx", "Past runs", "the table of the job's last thirty passes and their transcripts, never raid events"),
    ("pages/Agents.tsx", "each review or apply run", "the empty table names the passes of the two news jobs it will list"),
    ("pages/Agents.tsx", "Last run failed", "a job's state line: its last pass failed, read by the owner who installed the job"),
    ("pages/Agents.tsx", "never run", "a job's state line before its first pass: the verb, the job has never been started"),
    ("pages/Agents.tsx", "Follow this run", "opens the transcript of the job pass running now, an operator action"),
    ("pages/Agents.tsx", "Open the last run", "opens the transcript of the job's last pass, an operator action"),
    # the listening gate's inputs stay on the old wording on purpose (tests/test_listen.py pins them; the gate was tuned on
    # them and scripts/listen_probe.py would need a retune for any change) — members still say "run" in chat
    ("oibot_gm/discord_bot.py", "a raid or run happens",
     "the listening gate's question and criteria are tuned model inputs pinned by test_listen, kept on members' own word"),
]
# Every entry must still suppress at least one extracted string (test_allow_list_entries_have_reasons_and_still_match):
# a whole-file "" entry is not allowed — it would hide an empty state or a future offender along with the operator words.

FORBIDDEN = re.compile(
    r"\b(?:solver|solving|bounds|(?:comp )?targets?|cadence|derived|ephemeral|seated|pool|"
    r"rev|series|ev|rc|ctx|matrix|"
    # "override(s)" as a config noun; the loot verb "Override…" stays
    r"an override of|override(?:s|\(s\)|…)? go|overrides on|"
    # a setting's key where a person reads (a schedule's timing refused as "lock_hours_before ≤ nudge_hours_before"):
    # the editors' labels say it (Sheet opens, Locks, Signup Reminder goes out, Confirm by, An ask waits)
    r"[a-z]+_hours(?:_before)?|signup_lead\w*|nudge_\w+)\b", re.I)
# the whole text: "none yet" / "not set" / "none" / "nothing", or a bare dash (exactly — " — " with spaces is a separator,
# not a cell)
EMPTY_ALONE = re.compile(r"^(?:\s*(?:none yet|not set|none|nothing)\s*\.?\s*|—|–|-)$", re.I)
DASH_ALONE = re.compile(r"^\s*[—–-]\s*$")
# The retired words (phase 6, docs/superpowers/plans/2026-11-25-event-words.md, the word table): a run → an event, a
# follow-on → a later raid of the lineup, ad hoc → one-off, a pickup template → a schedule opened by an officer, a
# recurring run → a schedule's event, the lead run → the first raid, Who gets a seat → Priority. "Rehearse" stays.
RETIRED = re.compile(r"\b(?:recurring runs?|lead runs?|runs?|follow[- ]ons?|ad[- ]hoc|adhoc|pickups?|who gets a seat)\b",
                     re.I)
# "run" the verb stays ("it runs hourly", "run the command"). Only these verb phrases are taken out before RETIRED
# looks, each with a POSITIVE verb context — a subject that does the running, an auxiliary, a sentence start for the
# imperative — never "anything but a determiner": "Open runs on Tuesday", "Keep runs at their moment", "the schedule
# has run times" and "Tonight's run again" keep the noun caught.
_RUNNER = (r"it|that|which|who|whoever|bot|check|import|reminder|job|scheduler|clock|launchd|pc|leader|companion|"
           r"sweep|review|loop")
_AUX = r"can|can['’]t|cannot|could|will|won['’]t|would|must|should|may|has|have|had|do|does|did|didn['’]t"
# a sentence start (or after a dash, colon, bracket), or "or" / "and" / "then": where an imperative "Run …" begins
_IMPERATIVE = r"(?:^|(?<=[.!?:;—–(\n])|\b(?:or|and|then)\b)\s*"
VERB_RUN = re.compile("|".join((
    rf"\b(?:{_RUNNER}) runs\b",                                           # it runs, the check runs hourly, whose PC runs it
    r"\b(?:you|we|they) run\b",                                           # lets you run your purchase, if they run out
    r"\bto run (?=the\b|it\b|your\b|a\b|again\b|/)",                      # to run the check
    rf"\b(?:{_AUX}) (?:not )?run\b(?!\s+times\b)",                        # can run, once it has run — not "has run times"
    _IMPERATIVE + r"run (?:it\b|this\b(?!\s+(?:week|month|night|evening|time)\b)|the command\b|again\b|/)",
    r"\bis running\b", r"\bran\b")), re.I | re.M)
# The files that still hold a retired word (repo-relative). Each phase-6 task rewords its files and takes them out;
# test_retired_words_are_gone fails for a hit anywhere else, and for a file here that has none left (the list only
# shrinks). Computed by running the lint, never by hand.
WORDS_PENDING: set[str] = set()
# a dash in a table cell (under its column's header) is a cell, not an empty state: the Iconography rule keeps icon
# columns free of words, and "no offspec" beside every row is noise. The empty-state sentence is for a whole list, card or
# section. Only a bare dash is carved out: "none yet" in a cell still names nothing.
_CELL_OPEN = re.compile(r"<(?:Table\.Td|td)\b")
_CELL_CLOSE = re.compile(r"</(?:Table\.Td|td)>")


# ---------------------------------------------------------------------------------------------------------- frontend
TSX_ATTRS = ("label", "title", "description", "placeholder", "intro", "tooltip", "message", "aria-label", "hint",
             "confirmLabel", "emptyText", "nothingFoundMessage", "error", "alt")
_ATTR = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in TSX_ATTRS) + r")\s*=\s*(?:\{\s*)?(\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*')")
_KEY = re.compile(r"(?<![\w.])(?:label|title|message|description|intro|placeholder|confirm|hint|text|empty)\s*:\s*(\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*')")
# JSX text: between a tag's closing `>` and the next `<`. The `>` must end a tag (`<Text>`, `<b>`, `"/>`, `}>`), not an
# arrow `=>` or a comparison, and the text must not look like code.
_JSX_TEXT = re.compile(r"(?:<[A-Za-z][\w.]*(?:\s[^<>]*?)?>|</[\w.]*>|<>)([^<>]*?)(?=<)", re.S)
# a ternary branch that reads as words (a space or an ellipsis): `{busy ? "solving…" : <>…</>}` — nested JSX keeps the
# literal-children pass below from seeing it
_TERNARY = re.compile(r"(?:\?\s*|\s:\s*)(\"(?:[^\"\\\n]|\\.)*\")")
# any other object value that is a sentence (four words or more): a hint map such as Policy's DOC_HINT
_PROSE_VALUE = re.compile(r"(?<![\w.])[\w\"']+\s*:\s*(\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`)")
_CODEISH = re.compile(r"(=>|&&|\|\||===|!==|\)\s*;|;\s*$|\s=\s|\bconst\b|\breturn\b|\bfunction\b|\bimport\b|^\s*\)|\)\s*\{|\?\.|\w\((?!s\)))")


def _strip_ts_comments(text: str) -> str:
    """Blank out // and /* */ comments (strings and template literals kept), preserving line numbers."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif c != "`" and text[j] == "\n":
                    break
                j += 1
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith("//", i) and (i == 0 or text[i - 1] not in ":"):
            j = text.find("\n", i); j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j])); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def _unquote(lit: str) -> str:
    body = lit[1:-1]
    return re.sub(r"\$\{[^}]*\}", "…", body)  # template holes read as "…"


def _prose(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s)) and (" " in s.strip() or EMPTY_ALONE.match(s) is not None or len(s.strip()) > 2)


_CELL_FN = re.compile(r"\bfunction\s+([A-Z]\w*)\s*\(")


def _in_cell(text: str, pos: int) -> bool:
    """Whether `pos` sits inside a table cell (<Table.Td> / <td>) that is still open there, or inside a cell renderer —
    a component named …Cell / …Icons (SpecCell, FlexIcons) that a table column draws."""
    fns = list(_CELL_FN.finditer(text, 0, pos))
    if fns and re.search(r"(?:Cell|Icons)$", fns[-1].group(1)):
        return True
    opens = [m.start() for m in _CELL_OPEN.finditer(text, 0, pos)]
    closes = [m.start() for m in _CELL_CLOSE.finditer(text, 0, pos)]
    return bool(opens) and (not closes or opens[-1] > closes[-1])


def tsx_strings(path: Path) -> list[tuple[int, str]]:
    """The user-visible strings of a .tsx file; a bare dash inside a table cell is left out (the cell carve-out)."""
    return [(n, s) for n, s, pos, text in _tsx_strings(path) if not (DASH_ALONE.match(s) and _in_cell(text, pos))]


def _tsx_strings(path: Path) -> list[tuple[int, str, int, str]]:
    raw = path.read_text()
    text = _strip_ts_comments(raw)
    line_of = lambda pos: text.count("\n", 0, pos) + 1  # noqa: E731
    hits: list[tuple[int, str, int, str]] = []
    for rx in (_ATTR, _KEY, _PROSE_VALUE, _TERNARY):
        for m in rx.finditer(text):
            s = _unquote(m.group(1))
            if s.startswith("/") or s.startswith("#") or not s.strip():
                continue  # a path or an anchor
            if rx is _PROSE_VALUE and len(s.split()) < 4:
                continue
            if rx is _TERNARY and not (" " in s.strip() or "…" in s) or rx is _TERNARY and not re.search(r"[A-Za-z]", s):
                continue
            hits.append((line_of(m.start(1)), s, m.start(1), text))
    for m in _JSX_TEXT.finditer(text):
        body = m.group(1)
        if not body.strip():
            continue
        # literal children inside {…}: `{busy ? "Saving…" : "Save"}` / `{x ?? "—"}`
        for lit in re.finditer(r"\{[^{}]*\}", body):
            for q in re.finditer(r"\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'", lit.group(0)):
                s = q.group(1) if q.group(1) is not None else q.group(2)
                if s and _prose(s) and not re.fullmatch(r"[\w.-]+", s) or EMPTY_ALONE.match(s or "x"):
                    pos = m.start(1) + lit.start() + q.start()
                    hits.append((line_of(pos), s, pos, text))
        plain = re.sub(r"\{[^{}]*\}", " … ", body)
        if _CODEISH.search(plain) or re.search(r"[{}]", plain):
            continue
        plain = " ".join(plain.split())
        whole_child = not m.group(0).startswith("</") and text.startswith("</", m.end())
        if plain and re.search(r"[A-Za-z]", plain) or (whole_child and EMPTY_ALONE.match(plain or "x")):
            if plain.strip(" …"):
                pos = m.start(1) + len(body) - len(body.lstrip())
                hits.append((line_of(pos), plain, pos, text))
    return hits


# ---------------------------------------------------------------------------------------------------------- python
PY_FILES = ["raid_views.py", "wizard.py", "inbox.py", "consequences.py", "discord_pool.py", "discord_raid.py",
            "discord_registry.py", "configops.py", "help.py", "presets.py",
            # refusals and run-log lines a person reads come from these too
            "registry.py", "raidcycle.py", "discord_loot_surface.py", "discord_people.py", "appeals.py", "discord_policy.py",
            "discord_help.py",
            # the leveling-guide tool's refusals (§5.55) and its routes' sentences
            "guides/build.py", "guides/rxp_decode.py", "web/api_guides.py",
            # the guild guide library: its refusals and the /guide commands (design §5.63)
            "guides/library.py", "discord_guides.py",
            # auction prices: the price table's states and every price sentence (spec §3)
            "discord_auctions.py", "prices.py",
            # the Events page's read model and its routes (phase 4)
            "event_view.py", "web/api_events.py",
            # phase 6 (event words): every other module that builds text a person reads — the shared word tables, the
            # run buttons' labels, the Schedules / Raids pages' read models, the scheduler's and loot's lines, the routes'
            # sentences. Not here: web/api.py and web/api_agents.py (old-lint hits in MCP-facing errors and the Agents
            # page's setting ops), mcp_server.py / agents.py (operator tools), comp.py and loot/recommend.py (old-lint
            # hits not yet reworded), nl.py (the parser's schemas).
            "constants.py", "raid_buttons.py", "schedule_view.py", "raid_page.py", "event_board.py", "raid_scheduler.py",
            "raid_commands.py", "testfeed.py", "discord_loot.py", "loot_ops.py", "discord_bot.py", "discord_feed.py",
            "discord_news.py", "discord_armory.py", "armory_seed.py", "armory.py", "auctions.py", "professions.py",
            "lootctx.py", "loot/session.py", "news.py", "feed.py", "guild_routing.py", "guild_picker.py", "perms.py",
            "pending_views.py", "ops.py", "policy.py", "wizard_opts.py", "roster/explain.py", "roster/solver.py",
            "loot/scoring.py", "loot/distil.py", "loot/tierdraft.py", "conversation.py", "importers/ledger_csv.py",
            "web/app.py", "web/api_armory.py", "web/api_loot.py", "web/api_me.py", "web/api_memory.py", "web/api_ops.py",
            "web/api_policy.py", "web/api_prices.py", "web/api_professions.py", "web/api_schedules.py",
            "web/api_setup.py"]
LOG_CALLS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
# calls whose string arguments are machine-facing (patterns, keys, ids), not words a person reads
MACHINE_CALLS = {"compile", "match", "search", "fullmatch", "sub", "findall", "finditer", "split", "startswith", "endswith",
                 "get", "getattr", "setattr", "hasattr", "pop", "setdefault", "strftime", "strptime", "join", "replace",
                 "removeprefix", "removesuffix", "index", "count", "find", "rfind", "lstrip", "rstrip", "strip", "encode",
                 "decode", "open", "Path", "glob", "rglob", "getenv", "environ", "isinstance", "partition", "rpartition",
                 # commit messages: the data repo's git log is the operator's audit trail, not a surface a member reads
                 "batch", "save", "commit"}
# module-level names that hold text for the model or for machines (prompts, schemas), not words a person reads
MACHINE_NAMES = re.compile(r"(PROMPT|SYSTEM|SCHEMA|INSTRUCTIONS|_RE$|PATTERN|TEMPLATE_ID|_RULES$|EXEMPT|PARITY_PENDING|BY_DESIGN)", re.I)
# a review diff's before / after values ("not set → #loot", "none → 3"): a value in a diff, not an empty state. Only the
# bare "none" / "not set" / dash is spared there; an internal word in a diff value is still a hit.
DIFF_KWARGS = {"before", "after"}
DIFF_NAMES = {"cur", "now", "new"}  # the current / new value dicts a describe() reads its before / after from


def _flowfiles() -> list[Path]:
    return sorted(PY.glob("flows_*.py")) + [PY / f for f in PY_FILES]


class _Collector(ast.NodeVisitor):
    def __init__(self):
        self.out: list[tuple[int, str]] = []
        self.skip: set[int] = set()
        self.shown: set[int] = set()  # fallback positions where a bare dash is what a person reads
        self.diff: set[int] = set()  # values in a review diff (before / after): an empty word there is a value, not a state

    def visit_BoolOp(self, node):  # `x or "—"`
        self.shown.update(id(v) for v in node.values[1:]); self.generic_visit(node)

    def visit_IfExp(self, node):  # `a if c else "—"`
        self.shown.update((id(node.body), id(node.orelse))); self.generic_visit(node)

    def visit_Return(self, node):  # `return "not set"`
        if node.value is not None:
            self.shown.add(id(node.value))
        self.generic_visit(node)

    def _skip_tree(self, node):
        for n in ast.walk(node):
            self.skip.add(id(n))

    def visit_Module(self, node):  # docstrings
        self._docstring(node); self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._docstring(node)
        if node.name == "words" or node.name.endswith("_words"):  # a describe() helper: the words of a before / after value
            for r in ast.walk(node):
                if isinstance(r, ast.Return) and r.value is not None:
                    self.diff.update(id(n) for n in ast.walk(r.value))
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._docstring(node); self.generic_visit(node)

    def _docstring(self, node):
        b = getattr(node, "body", [])
        if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
            self._skip_tree(b[0])

    def visit_Assign(self, node):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if any(MACHINE_NAMES.search(n) for n in names):
            self._skip_tree(node.value)
        if any(n in DIFF_NAMES for n in names):
            self.diff.update(id(n) for n in ast.walk(node.value))
        self.generic_visit(node)

    def visit_AnnAssign(self, node):  # `BY_DESIGN_EXEMPT: dict[...] = {...}`
        if isinstance(node.target, ast.Name) and node.value is not None and MACHINE_NAMES.search(node.target.id):
            self._skip_tree(node.value)
        self.generic_visit(node)

    def visit_Dict(self, node):
        for k in node.keys:
            if k is not None:
                self._skip_tree(k)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        self._skip_tree(node.slice); self.generic_visit(node)

    def visit_Compare(self, node):  # x == "…" / "…" in x: machine values
        for n in [node.left, *node.comparators]:
            if isinstance(n, (ast.Constant, ast.Tuple, ast.List, ast.Set)):
                self._skip_tree(n)
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
        if name in MACHINE_CALLS or (name in LOG_CALLS and owner in {"log", "logger", "logging", "LOG"}):
            for a in node.args:
                self._skip_tree(a)
            if name in LOG_CALLS:
                self._skip_tree(node)
        for kw in node.keywords:
            if kw.arg in {"label", "name", "description", "placeholder", "before", "after", "text", "title", "content"}:
                self.shown.add(id(kw.value))
            if kw.arg in DIFF_KWARGS:
                self.diff.update(id(n) for n in ast.walk(kw.value))
            if kw.arg in {"custom_id", "key", "kind", "style", "fmt", "format", "route", "workload", "via", "level", "id", "value", "path", "op"}:
                self._skip_tree(kw.value)
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        if id(node) not in self.skip:
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("…")
            self.out.append((node.lineno, "".join(parts)))
        self._skip_tree(node)  # its constants are covered by the joined text

    def visit_Constant(self, node):
        if id(node) not in self.skip and isinstance(node.value, str):
            if EMPTY_ALONE.match(node.value) and (id(node) not in self.shown or id(node) in self.diff):
                return  # a dash as an argument or an id part (`LootButton(gk, run, "-", …)`), not a cell a person reads
            self.out.append((node.lineno, node.value))


def _py_prose(s: str) -> bool:
    t = s.strip()
    if not t or not re.search(r"[A-Za-z]", t):
        return EMPTY_ALONE.match(s) is not None
    if " " not in t:
        return EMPTY_ALONE.match(s) is not None
    # code-shaped: a custom_id template, a format spec, a path, a regex, an identifier list
    if re.fullmatch(r"[\w.:<>{}\[\]|/*+?^$\\-]+", t) or t.startswith(("%", "^", "(?")) or re.search(r"\\[dswb]", t):
        return False
    return True


def py_strings(path: Path) -> list[tuple[int, str]]:
    c = _Collector()
    c.visit(ast.parse(path.read_text()))
    return [(n, s) for n, s in c.out if _py_prose(s)]


# ---------------------------------------------------------------------------------------------------------- the check
def _allowed(rel: str, text: str) -> bool:
    return any(frag in rel and sub in text for frag, sub, _ in ALLOW)


def _reads(text: str) -> str:
    """What a reader sees: placeholders and inline code (`…`) dropped — a code-shaped thing in backticks is a
    command or key a person types, not a label."""
    t = re.sub(r"\{[^{}]*\}", " ", text)
    t = re.sub(r"`[^`]*`", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"<[@#&!t:][^>]*>", " ", t)  # Discord mentions and stamps
    return t


def retired_words(text: str) -> list[str]:
    """The retired words a reader would see in `text` (the verb phrases of VERB_RUN taken out first)."""
    return sorted({" ".join(m.group(0).lower().split()) for m in RETIRED.finditer(VERB_RUN.sub(" ", _reads(text)))})


_HELP_TAIL = " (in the data the help answer is written from — the answer repeats it)"


def _strings():
    """(path, line, text) for every user-visible string, each once, the ALLOW entries left out."""
    for f, extract in _sources():
        rel = str(f.relative_to(ROOT))
        seen: set[tuple[int, str]] = set()
        for line, text in extract(f):
            if (line, text) in seen:
                continue
            seen.add((line, text))
            if not _allowed(rel, text):
                yield rel, line, text


def findings() -> list[tuple[str, int, str, str]]:
    """(path, line, text, why) for every user-visible string that breaks the copy rule."""
    rows: list[tuple[str, int, str, str]] = []
    for rel, line, text in _strings():
        words = sorted({m.group(0).lower() for m in FORBIDDEN.finditer(_reads(text))})
        if words:
            why = "internal word: " + ", ".join(words)
            if rel.endswith("help.py"):
                why += _HELP_TAIL
            rows.append((rel, line, text, why))
        elif EMPTY_ALONE.match(text):
            rows.append((rel, line, text, "empty state that names nothing"))
    return rows


def retired_findings() -> list[tuple[str, int, str, str]]:
    """(path, line, text, why) for every user-visible string that still says a retired word (phase 6)."""
    rows: list[tuple[str, int, str, str]] = []
    for rel, line, text in _strings():
        words = retired_words(text)
        if words:
            why = "retired word: " + ", ".join(words)
            if rel.endswith("help.py"):
                why += _HELP_TAIL
            rows.append((rel, line, text, why))
    return rows


def _one_line(text: str, width: int = 200) -> str:
    t = " ".join(text.split())
    return t if len(t) <= width else t[:width - 1] + "…"


def test_extractors_take_prose_not_code(tmp_path):
    """Guards the extractors themselves, so the lint stays quiet on code and loud on copy."""
    tsx = """
    import { solver } from "./x";   // the solver
    const rev = props.rev; const ev = getEv();
    export function A({ bounds }: { bounds: number }) {
      const ok = a < b && c > d;
      return <Stack><Text size="xs" c="dimmed">Bounds (the solver; the owner's):</Text>
        <TextInput label="Min seated" placeholder="—" />
        <Text>{busy ? "The solver is working…" : x}</Text>
        {list.map((s) => <Badge key={s.series}>{s.series_name}</Badge>)}
        <Text>{v ?? "—"}</Text>
      </Stack>;
    }"""
    p = tmp_path / "selftest.tsx"
    p.write_text(tsx)
    got = [s for _, s in tsx_strings(p)]
    assert "Bounds (the solver; the owner's):" in got and "Min seated" in got and "—" in got
    assert "The solver is working…" in got
    assert not any("import" in s or "series_name" in s or "a < b" in s for s in got), got
    # the cell carve-out: a bare dash in a table cell or a …Cell / …Icons renderer is a cell, not an empty state;
    # "none yet" in a cell still names nothing; a ternary branch under nested JSX is still read
    cells = """
    export function FlexIcons({ flex }: { flex?: string[] }) {
      if (!flex) return <Text c="dimmed">—</Text>;
      return <Group>{flex.map((r) => <Icon key={r} />)}</Group>;
    }
    export function T({ rows, busy }: any) {
      return <Table><Table.Tbody>{rows.map((r) => <Table.Tr><Table.Td>{r.off ? <Spec /> : <Text>—</Text>}</Table.Td></Table.Tr>)}
        <Table.Tr><Table.Td><Text>none yet</Text></Table.Td></Table.Tr></Table.Tbody></Table>
        <Text>{busy ? "solving…" : <>Preview of <b>{x}</b></>}</Text>;
    }"""
    c = tmp_path / "cells.tsx"
    c.write_text(cells)
    got = [s for _, s in tsx_strings(c)]
    assert "—" not in got and "none yet" in got and "solving…" in got, got
    assert all(FORBIDDEN.search(w) for w in ("Edit targets", "Signup cadence", "how the solver weighs seats",
                                             "The solver seated them", "live pool-readiness cards",
                                             "Amber marks an override of the game defaults", "3 override… go"))
    assert not any(FORBIDDEN.search(w) for w in ("Who gets a seat", "Your seat is released", "a run seats", "use Override… on the card", "Override…"))
    py = '''
"""Module docstring with the solver."""
import logging
log = logging.getLogger(__name__)
SYSTEM_PROMPT = "You read the solver output."
def f(ev, rc):
    """Docstring: rev of the series."""
    log.info("solver ran for %s", ev.key)
    d = {"the pool size": 1}
    if x == "bounds check": pass
    cid = f"raid.{gk}:{ev.key}:lock"
    return f"The solver seated {n} of {ev.size}.", "Nothing to do here."
def g(cid, new):
    def words(v):
        return v or "not set"
    part = DescribedPart(label="Roster channel", before=f"<#{cid}>" if cid else "not set", after=words(new))
    shown = names or "none"
    return part, shown
'''
    q = tmp_path / "selftest.py"
    q.write_text(py)
    got = [s for _, s in py_strings(q)]
    # the review diff's "not set" (before= / a words() helper) is a value, not an empty state; `names or "none"` is one
    assert got == ["The solver seated … of ….", "Nothing to do here.", "Roster channel", "none"], got


def test_retired_words_take_the_noun_not_the_verb():
    """Phase 6 (event words): 'run' the dated occasion is retired for 'event'; 'run' the verb stays. VERB_RUN takes out
    only verb phrases with a positive verb context, so each of them goes quiet while a noun directly followed by the
    alternative's own word is still caught."""
    pairs = [  # (a verb that stays, a noun that is retired) — per alternative: its verb, then the noun beside its word
        # a subject that runs: it / that / who / the bot / the check / the import / a reminder / PC / leader …
        ("The check runs hourly.", "Open runs on Tuesday are listed."),
        ("It runs on Tuesdays at 7:30 PM.", "Two runs on Wednesday."),
        ("The import runs at noon.", "Locked runs at 8 PM."),
        ("A reminder runs every hour.", "Upcoming runs every Tuesday"),
        ("Whose PC runs the companion?", "Both runs the same night"),
        ("The bot runs each check before it posts.", "Your runs on the Me page."),
        ("Whoever runs that companion needs a new token.", "Past runs that ended."),
        ("The raid leader runs it next to the game.", "one-off runs on Fridays"),
        ("A 12-hour clock runs 1–12.", "Save · keep runs at their moment"),
        ("The job runs every 15 minutes.", "Keep runs at their real moment"),
        ("It runs raid signups.", "Pausing stops new schedule runs."),
        # you / we / they run
        ("This lets you run your purchase there.", "We'll show you runs this week."),
        ("Fewer if they run out.", "your run"),
        # to run + an object
        ("Ask the bot to run the check.", "Move them to run 2."),
        # an auxiliary + run, never "run times"
        ("A bot can't run the companion.", "No event opens until the schedule has run times."),
        ("Once it has run, it is listed here.", "Can't make this run"),
        ("A schedule can't run the same raid twice.", "a run's roster"),
        # the imperative at a sentence start (or after a dash, "or", "and", "then")
        ("Run it again later.", "Tonight's run again needs a tank."),
        ("Run this from the Members page.", "Every run this week is short."),
        ("Run the command in your server.", "Open a new run this week."),
        ("Run again after the lock.", "the run again"),
        ("Run /raid sheet in a server channel.", "the run /raid open made"),
        ("I can't post here — run /raid sheet in a channel.", "this run's sheet"),
        ("It is pending, or run /raid loot again.", "the runs the bot opened"),
        # is running / ran
        ("The lock is running.", "The run is running late."),
        ("The import ran overnight.", "The run ran long."),
        # nouns with no verb near
        ("Run this in the server you want to set up.", "the run"),
        ("A reminder runs every hour.", "3 runs"),
        ("Run it again later.", "test run"),
        ("The check runs hourly.", "Live runs"),
        ("The check runs hourly.", "a recurring run"),
        ("The check runs hourly.", "the lead run"),
        ("The check runs hourly.", "the bot's runs"),
        ("The check runs hourly.", "the leader's runs"),
        ("The check runs hourly.", "these runs this month"),
    ]
    for verb, noun in pairs:
        assert not retired_words(verb), verb
        assert retired_words(noun), noun
    for old in ("Barrow Deeps, then its follow-on", "a follow on raid", "an ad hoc run", "an ad-hoc raid", "adhoc",
                "a pickup template", "Who gets a seat", "Lead runs first"):
        assert retired_words(old), old
    for fine in ("Rehearse a raid", "a rehearsal", "running total", "Event", "the rerun", "run_open", "Priority",
                 "Barrow Deeps, then Onyxia's Lair"):
        assert not retired_words(fine), fine


def test_retired_words_are_gone():
    """The retired words (the plan's word table) are out of every user-visible string, but for the files still in
    WORDS_PENDING — and that list only shrinks: a file in it with no hit left must leave it."""
    rows = retired_findings()
    hit = {p for p, *_ in rows}
    stray = [r for r in rows if r[0] not in WORDS_PENDING]
    assert not stray, (f"{len(stray)} user-visible strings carry a retired word (event, lineup, one-off, Priority — the "
                       "plan's word table; docs/manual.md §1a):\n"
                       + "\n".join(f"{p}:{n}: {_one_line(t)}  [{why}]" for p, n, t, why in stray))
    done = sorted(WORDS_PENDING - hit)
    assert not done, f"these files hold no retired word any more — take them out of WORDS_PENDING: {done}"


def _sources() -> list[tuple[Path, object]]:
    return [(f, tsx_strings) for f in sorted(SRC.rglob("*.tsx"))] + [(f, py_strings) for f in _flowfiles()]


def test_allow_list_entries_have_reasons_and_still_match():
    """Each entry has a reason, is scoped to a substring (never a whole file) and still suppresses at least one
    extracted string that would otherwise be a hit — a stale entry is deleted, not kept "just in case"."""
    sources = _sources()
    for frag, sub, why in ALLOW:
        assert len(why.split()) >= 8, f"{frag}: say why in a sentence a reviewer could disagree with"
        assert sub.strip(), f"{frag}: an entry names the substring it exempts, never the whole file"
        used = False
        for f, extract in sources:
            if frag not in str(f.relative_to(ROOT)):
                continue
            for _, text in extract(f):
                if sub in text and (FORBIDDEN.search(_reads(text)) or EMPTY_ALONE.match(text) or retired_words(text)):
                    used = True
                    break
            if used:
                break
        assert used, f"stale allow-list entry: {frag} / {sub!r} suppresses nothing"


def test_user_visible_copy_uses_the_readers_words():
    rows = findings()
    assert not rows, (f"{len(rows)} user-visible strings break the copy rule (CLAUDE.md; the glossary is docs/manual.md §1a "
                      "— rewrite in the reader's words, or allow-list with a reason):\n"
                      + "\n".join(f"{p}:{n}: {_one_line(t)}  [{why}]" for p, n, t, why in rows))


def write_findings(path: Path) -> int:
    rows = findings() + retired_findings()
    by: dict[str, list[tuple[int, str, str]]] = {}
    for p, n, t, why in rows:
        by.setdefault(p, []).append((n, t, why))
    out = ["# Copy findings — the sweep list (2026-10-24)", "",
           "Generated by `uv run python tests/test_copy.py` from tests/test_copy.py (the copy rule, CLAUDE.md; the glossary is",
           "docs/manual.md §1a *The words*). Each line is a user-visible string to rewrite in the reader's words; when the sweep",
           "is done, this test is the gate. A \"retired word\" line is phase 6's (event words): its file stays in",
           "WORDS_PENDING until the word is gone.", "",
           f"**{len(rows)} hits in {len(by)} files.**", "", "| file | hits |", "|---|---|"]
    out += [f"| {p} | {len(v)} |" for p, v in sorted(by.items(), key=lambda kv: -len(kv[1]))]
    for p, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        out += ["", f"## {p} ({len(v)})", ""]
        out += [f"- L{n} — {why}: `{_one_line(t).replace('`', chr(39))}`" for n, t, why in sorted(v)]
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    return len(rows)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "copy_findings.md"
    print(write_findings(target), "hits →", target)


def test_a_schedules_timing_refusal_reads_in_the_editors_labels(reg):
    """The owner, 2026-11-24: a schedule's timing refused ('the nudge must fall between signup opening and lock
    (lock_hours_before ≤ nudge_hours_before ≤ signup_lead_hours)') says what must come before what in the labels the
    editors show — Sheet opens, Locks, Signup Reminder goes out, Confirm by. FORBIDDEN catches a setting's key in any
    person-facing string; here the refusals themselves, and no 'nudge' in a refusal the registry or the run code raises."""
    from oibot_gm import raidcycle as rc
    from oibot_gm.registry import TIMING_REFUSALS, RegistryError, hours_label

    for text in (*TIMING_REFUSALS.values(), rc.TIMING_ORDER):
        assert not FORBIDDEN.search(_reads(text)) and not re.search(r"\bnudg|[<>≤≥]", text), text
        assert (text[0].isupper() or text.startswith("{")) and text.endswith("."), text
    for key in ("lock_hours_before", "nudge_hours_before", "signup_lead_hours"):
        assert FORBIDDEN.search(f"the nudge must fall ({key})"), key
    assert hours_label("nudge_hours_before") == "Signup Reminder goes out" and hours_label("fill_ask_hours") == "An ask waits"
    sid = "tue"
    reg.set_schedule(sid, {"slots": "Tue 19:30", "name": "Tuesday raid"}, "t", raid="barrow_deeps")
    for fields, key in (({"nudge_hours_before": 2}, "reminder"), ({"signup_lead_hours": 20}, "opens"),
                        ({"confirm_hours_before": 30}, "confirm")):
        with pytest.raises(RegistryError) as e:
            reg.set_schedule(sid, fields, "t")
        assert str(e.value) == TIMING_REFUSALS[key], (fields, str(e.value))
    with pytest.raises(RegistryError) as e:
        reg.set_schedule(sid, {"lock_hours_before": "soon"}, "t")
    assert str(e.value) == "Locks takes a number of hours."
    for mod in ("registry.py", "raidcycle.py", "consequences.py"):
        said = [s for _n, s in py_strings(PY / mod) if re.search(r"\bnudg", s, re.I)]
        assert not said, (mod, said)
