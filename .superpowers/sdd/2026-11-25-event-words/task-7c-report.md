# Task 7c report: the lint's gaps, the pending list, the manual lint, the docs

## Extractor changes (tests/test_copy.py)
- (a) f-string braces: `visit_JoinedStr` still records the joined text with "…" per expression, but now visits each
  expression inside the braces, so a quoted string there (`f"{'live run' if live else 'closed run'} …"`) is extracted,
  both branches. A format spec (`:>10`) is skipped as code. An empty word spliced in by an f-string (`{x or '—'}`) is a
  value inside a sentence, not an empty state (`_Collector.inline`), so the copy rule's empty-state check does not fire
  on it (without this, ~45 old-lint "none"/"—" hits appeared, all values inside sentences).
- (b) `replace` is out of MACHINE_CALLS: `.replace(old, new)`'s arguments are read; `_py_prose` still drops code-shaped ones.
- (c) `save`, `batch`, `commit` are out of MACHINE_CALLS; COMMIT_CALLS = save, save_quiet, save_many, batch, commit marks
  their arguments as commit messages. Commit messages are held to the RETIRED words only: `py_shown` (= py_strings
  without them) feeds FORBIDDEN / empty states (`findings`, `_strings(commits=False)`, `_sources(commits)`), since the
  copy rule is for the site and Discord. (Without this, registry.py's migration message "rosters = regular series only"
  would fail FORBIDDEN.)
- Exclusions kept: dict keys, custom_ids, log calls, MACHINE_NAMES prompts, docstrings (pinned by the new test).
- `retired_words` keeps a backticked slash command's slash (`` `/apply` `` → `/`) before `_reads` drops code, so
  "or run `/apply`" reads as the imperative; the paired noun "the run `/raid open` made" is still caught.
- New test: `test_extractors_read_fstring_branches_replace_and_save_messages`.

## Strings newly caught
None. With all three gaps closed the lint finds 0 retired-word hits and 0 copy hits (out/copy_findings.md: 0). The
reviewers' "A run can't hold…" was already reworded in raidcycle.py:1970 by an earlier task (now extracted: "An event
can't hold the same raid twice."); every save/batch/commit message and f-string branch is already in the new words.
No source file changed.

## VERB_RUN / ALLOW
- `_AUX` gains hasn't / haven't / hadn't; the imperative gains `run the job(s)`. Pairs added to
  test_retired_words_take_the_noun_not_the_verb: "The jobs haven't run on this Mac yet." / "The schedule hasn't run
  times yet."; "Read proposals, post them and run the jobs." / "the run the jobs opened"; "Run the job from the Agents
  page." / "Every run the job made"; "Press Apply to join, or run `/apply`." / "the run `/raid open` made".
- ALLOW entries removed (now verbs by VERB_RUN): news.py "run the jobs"; Agents.tsx "haven't run on this Mac".
  "never run" (Agents.tsx) kept: no aux, a bare "never run" pattern would risk hiding nouns.
- No ALLOW entries added.

## WORDS_PENDING
Deleted. `test_retired_words_are_gone` asserts no retired word in any scanned string. The "list only shrinks" half is
gone; write_findings' header no longer names the list.

## Manual lint
`test_the_manual_uses_the_new_words` + `manual_findings()` + `MANUAL` + `MANUAL_ALLOW`: retired_words line by line
(backticked text dropped), with MANUAL_ALLOW = two substrings, both lines of §1a's older-words paragraph:
"Older words: *run* is now **event**" and `is **Priority**. People may still say "run"` (the paragraph spans three lines;
its third line holds no retired word). The test also checks each entry has a reason and is still in the manual.
Marked `@pytest.mark.xfail(strict=False, reason="the manual sweep lands in the next commit")` — at the time of writing
216 manual lines still hit. Of those, a handful are VERBS the lint cannot tell from nouns, which the manual sweep must
reword or the controller may allow-list: L9 "and runs a loot council", L355 "The explorer also runs on the page", L428
"or runs `/register`", L834 "a raid runs;", L2190 "jobs run on the host", L2674 "re-runs on its own" (line numbers
from the manual as it stood mid-sweep).

## Docs
- docs/design.md §5.67: "Phase 6 built (2026-11-25): the words." before "Later phases".
- docs/layout/events.md: the lock card's quoted "All bosses down — End the event?" / "End the event", and end_decide's
  "(End the event)". docs/layout/loot.md: loot_scheduler_step's "Start a one-off event" prompt.
  Not changed (identifiers / explanations, not quoted labels): discord.md "test run" (verb args / keys), events.md
  "follow-on"/"ad hoc"/"test run" explanations, registry.md "AD HOC (pickup templates…)", frontend.md "the test run".
- CLAUDE.md still quotes "All bosses down — End the run?" in its layout block; not mine to edit (shared-docs protocol).

## Suite
Fast suite (-n 8): `1 failed, 5461 passed, 1 xfailed, 939 warnings in 402.94s` — the one failure,
tests/test_solver_kept_answer.py::test_a_solve_the_wall_clock_cut_is_never_kept, is a wall-clock solver test under
-n 8 load; re-run alone: `6 passed` (unrelated to this change). No manual-caused failures besides the xfailed manual lint;
tests/test_copy.py + tests/test_doc_refs.py: `12 passed, 1 xfailed`.
