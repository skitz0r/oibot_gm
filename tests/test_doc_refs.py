"""Every "§5.NN" reference in the code and the docs names a section design.md actually has (the phase 3 merge renumbered
sections after their cross-references were written)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = re.compile(r"§\s?(5\.\d+)\b")


def _headings() -> set[str]:
    return set(re.findall(r"^#{2,4} (5\.\d+) ", (ROOT / "docs" / "design.md").read_text(), re.M))


def _sources():
    yield from (ROOT / "docs").glob("*.md")
    yield ROOT / "CLAUDE.md"
    for base, pats in (("src", ("*.py", "*.md")), ("scripts", ("*.py", "*.md")), ("frontend/src", ("*.ts", "*.tsx"))):
        for pat in pats:
            yield from (p for p in (ROOT / base).rglob(pat) if "static" not in p.parts)


def test_every_section_reference_names_an_existing_heading():
    heads = _headings()
    assert {"5.43", "5.44", "5.45", "5.46", "5.47", "5.48", "5.49", "5.50"} <= heads
    bad = []
    for f in _sources():
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            for m in REF.finditer(line):
                if m.group(1) not in heads:
                    bad.append(f"{f.relative_to(ROOT)}:{n}: §{m.group(1)}")
    assert not bad, "references to a design section that doesn't exist:\n" + "\n".join(bad)


def test_the_phase_3_sections_are_the_ones_the_code_points_at():
    """The phase 3 parts: G1a = 5.43 structured ops, G1b = 5.44 config edit modes, G2 = 5.45 governance, G3 = 5.46 setup,
    G4 = 5.47 loot / attendance — a module's own reference names its part."""
    design = (ROOT / "docs" / "design.md").read_text()
    titles = dict(re.findall(r"^### (5\.4[3-7]) (.*)$", design, re.M))
    assert "structured ops path" in titles["5.43"] and "config edit modes" in titles["5.44"] and "governance" in titles["5.45"]
    assert "setup" in titles["5.46"] and "loot and attendance" in titles["5.47"]
    owners = {"src/oibot_gm/web/api_ops.py": "5.43", "src/oibot_gm/news.py": "5.45", "src/oibot_gm/policy.py": "5.45",
              "src/oibot_gm/pending_views.py": "5.45", "src/oibot_gm/flows_news.py": "5.45", "src/oibot_gm/web/api_policy.py": "5.45",
              "src/oibot_gm/perms.py": "5.46", "src/oibot_gm/web/api_setup.py": "5.46", "tests/test_phase3_setup.py": "5.44"}
    for rel, sec in owners.items():
        first = (ROOT / rel).read_text().split('"""', 2)[1] if rel.endswith(".py") else ""
        assert f"§{sec}" in first, (rel, sec)


def test_every_design_section_is_numbered_once():
    numbers = re.findall(r"^#{2,4} (5\.\d+) ", (ROOT / "docs" / "design.md").read_text(), re.M)
    dups = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not dups, f"design sections numbered twice: {dups}"


def test_the_phase_4_sections_are_the_ones_the_code_points_at():
    """Phase 4: W1 = 5.48 the loot data on-ramp, W2 = 5.49 members and the public (appeals), W3 = 5.50 companion tokens —
    a module's own docstring names its part."""
    design = (ROOT / "docs" / "design.md").read_text()
    titles = dict(re.findall(r"^### (5\.(?:48|49|50)) (.*)$", design, re.M))
    assert "loot data on-ramp" in titles["5.48"] and "members and the public" in titles["5.49"] and "companion tokens" in titles["5.50"]
    owners = {"src/oibot_gm/importers/ledger_csv.py": "5.48", "src/oibot_gm/loot/distil.py": "5.48", "src/oibot_gm/appeals.py": "5.49",
              "src/oibot_gm/flows_appeal.py": "5.49", "src/oibot_gm/feed.py": "5.50"}
    for rel, sec in owners.items():
        first = (ROOT / rel).read_text().split('"""', 2)[1]
        assert f"§{sec}" in first, (rel, sec)
    layout = (ROOT / "CLAUDE.md").read_text()
    for name in ("appeals.py", "flows_appeal.py", "ledger_csv.py", "distil.py"):
        assert name in layout, f"CLAUDE.md's layout names {name}"
