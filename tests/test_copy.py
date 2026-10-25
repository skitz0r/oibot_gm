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
ALLOW: list[tuple[str, str, str]] = []
# Every entry must still suppress at least one extracted string (test_allow_list_entries_have_reasons_and_still_match):
# a whole-file "" entry is not allowed — it would hide an empty state or a future offender along with the operator words.

FORBIDDEN = re.compile(
    r"\b(?:solver|solving|bounds|(?:comp )?targets?|cadence|derived|ephemeral|seated|pool|"
    r"rev|series|ev|rc|ctx|matrix|"
    # "override(s)" as a config noun; the loot verb "Override…" stays
    r"an override of|override(?:s|\(s\)|…)? go|overrides on)\b", re.I)
# the whole text: "none yet" / "not set" / "none" / "nothing", or a bare dash (exactly — " — " with spaces is a separator,
# not a cell)
EMPTY_ALONE = re.compile(r"^(?:\s*(?:none yet|not set|none|nothing)\s*\.?\s*|—|–|-)$", re.I)
DASH_ALONE = re.compile(r"^\s*[—–-]\s*$")
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
            "discord_help.py"]
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


def findings() -> list[tuple[str, int, str, str]]:
    """(path, line, text, why) for every user-visible string that breaks the copy rule."""
    rows: list[tuple[str, int, str, str]] = []
    for f, extract in _sources():
        rel = str(f.relative_to(ROOT))
        seen: set[tuple[int, str]] = set()
        for line, text in extract(f):
            if (line, text) in seen:
                continue
            seen.add((line, text))
            if _allowed(rel, text):
                continue
            words = sorted({m.group(0).lower() for m in FORBIDDEN.finditer(_reads(text))})
            if words:
                why = "internal word: " + ", ".join(words)
                if rel.endswith("help.py"):
                    why += " (in the data the help answer is written from — the answer repeats it)"
                rows.append((rel, line, text, why))
            elif EMPTY_ALONE.match(text):
                rows.append((rel, line, text, "empty state that names nothing"))
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
                if sub in text and (FORBIDDEN.search(_reads(text)) or EMPTY_ALONE.match(text)):
                    used = True
                    break
            if used:
                break
        assert used, f"stale allow-list entry: {frag} / {sub!r} suppresses nothing"


@pytest.mark.xfail(strict=True, reason="the copy sweep (2026-10-24) is the fix")
def test_user_visible_copy_uses_the_readers_words():
    rows = findings()
    assert not rows, (f"{len(rows)} user-visible strings break the copy rule (CLAUDE.md; the glossary is docs/manual.md §1a "
                      "— rewrite in the reader's words, or allow-list with a reason):\n"
                      + "\n".join(f"{p}:{n}: {_one_line(t)}  [{why}]" for p, n, t, why in rows))


def write_findings(path: Path) -> int:
    rows = findings()
    by: dict[str, list[tuple[int, str, str]]] = {}
    for p, n, t, why in rows:
        by.setdefault(p, []).append((n, t, why))
    out = ["# Copy findings — the sweep list (2026-10-24)", "",
           "Generated by `uv run python tests/test_copy.py` from tests/test_copy.py (the copy rule, CLAUDE.md; the glossary is",
           "docs/manual.md §1a *The words*). Each line is a user-visible string to rewrite in the reader's words; when the sweep",
           "is done, drop the xfail on `test_user_visible_copy_uses_the_readers_words`.", "",
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
