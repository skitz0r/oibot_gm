# Discord for events, lineups and reschedule (phase 5) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every confirm a raid leader presses in Discord prints the same server box the site prints (title, "label: old →
new" lines, ≤ 3 sentences, the action's own verb, red only when it can't be undone, a changed box shown again). Officers
can also move an event, edit its own settings, change its lineup, open a one-off lineup, edit a schedule's lineup and
switch someone's role from Discord, so `configops.PARITY_PENDING` ends empty.

**Architecture:** One new confirm screen, `Wizard.decide_box`, renders a `consequences.box` dict. At the press it
rebuilds the box through a `refresh` callable and compares `fp`. A different `fp` shows the fresh box again and nothing
is written, which is the Discord twin of the site's `seen` → 409. Every new wizard is a thin caller of a verb that
already exists (`RaidMixin.reschedule_event`, `edit_lineup`, `open_run_and_post`, `inbox.do_member_action`,
`configops.apply_ops`) and of a box builder that already exists. Two exceptions: `consequences.member_action_box` (the
row ⋮ menu's per-action box) and `consequences.open_box_for` (moved out of `web/api.py`). The new event wizards live in
one new module, `flows_event.py`. The schedule wizard is a second new module, `flows_schedule.py`, reached from
`/gm config raid`. That command's own schedule section is removed.

**Tech Stack:** Python 3.12, discord.py 2.x (views, modals with `ui.Label`, `DynamicItem`), FastAPI (one route
touched), React 19 + Mantine 9 (one component), pytest (+ xdist).

**Spec:** `docs/superpowers/specs/2026-11-22-raids-events-schedules-design.md`. This plan covers §9 item 5, §7's
Discord bullet, the **Discord.** bullet of §7b (with §7b's box rules, which Discord now shares), §8's anti-goals and §10.
Bound also by the owner's tweaks B and C (`.superpowers/common/tweaks-spec.md`: Edit event's fields, the board stays the
site's) and `.superpowers/common/phase4-carried.md`, where **the code at HEAD is the truth**. Read `CLAUDE.md` first:
the Principles, then `docs/layout/discord.md` and `docs/layout/confirms.md` for the modules you touch.

## What this plan found already built (do not rebuild)

- `Wizard.decide` exists, and 33 of its 34 callers already pass a verb. Only `flows_professions.py:302` falls back to
  "Do it". The fallback label and "<verb> with a note…" are what change.
- Server boxes exist for everything the Discord screens need: `consequences.box` / `refusal_box_for` / `lock_box` /
  `cancel_box` / `reschedule_pick_box` / `reschedule_box(edit=)` + `event_edit_lines` / `lineup_box` / `remind_box` /
  `end_box` (through `LootMixin.end_box`) / `answer_box` / `open_now_box` / `schedule_*_box` (through `configops.op_box`,
  `Described.box`) / `raid_save_box` / `raid_reset_box`. So do `CONFIRM_WORDS["changed"]` and
  `PAGE_WORDS["schedule_fields" | "schedule_field_help" | "priority" | "split" | "edit_locked" | "edit_over" |
  "first_to_join" | "by_priority"]`.
- The verbs exist and decide officers live: `RaidMixin.reschedule_event(…, start=, timing=, preview=, rev=)` (timing
  covers every Edit event field, `rc.EVENT_TIMING`; after the lock `rc.AFTER_LOCK`), `edit_lineup(…, lineup, rev=)`,
  `set_answer(spec=)`, `send_plan(seats=)`, `inbox.do_member_action(spec=)`, and
  `open_run_and_post(lineup=, lock_hours=, first_rosters=)`. `rc.move_start` refuses a skipped or doubled wall time
  (`MOVE_GAP`, `MOVE_TWICE`). `event_view.edit_options` lists the fields allowed now.
- `/raid open` (`flows_raid.OpenWizard`) already has the raid's next night, another upcoming night (paged by week), a
  pickup template, and a one-off day and time with the DST note. It has **no** lineup step for a one-off, prints its own
  confirm words instead of box 11, and still says "in the next 21 days" (`no_slot_reason`; tweak A removed that limit).
- `/gm config raid` (`flows_config.RaidConfigWizard`) still edits a raid's schedules as a whole list through
  `set_raid_overrides`. It has **no** lineup step, prints its own Save words instead of boxes 13–17 and 19, and uses the
  old labels ("Nudge the unanswered", "A run's week", `BOOL_WORDS`, `FIELD_WORDS`).
- `/raid set` (`flows_roster.SetWizard`) prints `inbox.action_consequence` text with its own "keep going?" re-check. It
  offers no spec. The site's ⋮ menu (`frontend/src/components/board/MemberMenu.tsx` `useRecord`) still uses `useConfirm`
  for callouts, pins and the confirm or fill answers.
- The officer card's action row (`raid_views.run_actions_row`) already holds five buttons: Open the board · Lock · Fill ·
  Loot · Cancel. It is full, so Reschedule is reached through `/raid reschedule` only (Decision D2 below).

## Decisions this plan makes (the owner may disagree)

- **D1.** The site keeps its ellipses. Words shared with the site (`raid_buttons.RUN_WORDS`, `rc.ACTION_LABELS`,
  `flows_armory.ARMORY_ACTIONS` / `MOVE_LABEL`) keep "…", because they are the site's menu words.
  `wizard.button_label()` drops the ellipsis wherever Discord renders a button. A literal used only by Discord loses
  its "…" at the source.
- **D2.** No Reschedule button on a card. The officer row is full (five buttons), and a third row on every card is
  clutter. The commands are `/raid reschedule`, `/raid edit` and `/raid lineup`. These are three new subcommands, which
  is allowed: C15 forbids renaming commands, not adding them. `/raid` has 12 of 25.
- **D3.** A plain "Cancel event" press cancels with no reason and posts the channel line, the old form's default. "Cancel
  event with a note" opens the form (reason + Tell the channel?).
- **D4.** The schedule section of `/gm config raid` is replaced by a hand-off to a new schedule wizard
  (`flows_schedule.py`, flow `schedule`). That wizard saves through `configops` like the site (boxes 13–17). The raid
  wizard's own Save goes through `configops` too (box 19). `set_raid_overrides` with `schedules` is no longer called from
  Discord.
- **D5.** Edit event is a hub: one "Change a setting" select, then one screen per setting. The five weights are one
  modal of five selects, which is exactly Discord's limit. The tooltips (`schedule_field_help`) are all over 100
  characters, so they are left out of option descriptions.

## Global Constraints

- **Confirm rule:** ≤ 3 sentences, each ≤ 20 words, a capital letter and a period, no parentheses, semicolons or
  dash-glued fragments. A Discord screen prints a box's sentences **as given** and composes none of its own. A new
  generator (`member_action_box`, `open_box_for`) is rendered by `tests/test_confirm_readability.py`.
- **§7b on Discord:** the first button is the box's `verb`. A note button is `f"{verb} with a note"`. `Back` stays.
  Never Yes, No, OK, Cancel or "Do it" as a verb; the only exception is "Cancel event". The verb is red
  (`ButtonStyle.danger`) iff `box["danger"]`, else `ButtonStyle.success`. A refusal box (`verb is None`) shows its
  sentence and **Back alone**. A box whose `fp` changed between show and press is shown again under
  `CONFIRM_WORDS["changed"]` and waits.
- **No Discord button label ends in "…" or "..."** (`wizard.lint` and `tests/test_discord_labels.py`).
- **Clock / Wizard rules:** no typed date, time, run, spec or number. Days come from `wizard.day_opts` (the GUILD's date
  through `reg.now_local()`), hours from `hour_opts` (12-hour), minutes in **15-minute steps**, numbers from preset
  selects. A wall time goes through `rc.move_start(reg, "YYYY-MM-DD HH:MM")`, which refuses `MOVE_GAP` / `MOVE_TWICE`
  and never shifts the time. Times a person reads come from `reg.local12`.
- **No run key shown to a person.** A run is named with `raid_views.run_title(reg, ev)`, or in a sentence
  `rc.run_words(reg, ev)`. Keys are select **values** only.
- **Officers decided LIVE:** every write passes `by_id=interaction.user.id` to a verb that calls
  `guild_routing.resolve_role(…, max_age=PRIV_LIVE)`. The wizard's `guard` (`flows_raid.officer_guard`) re-checks at
  every press and at a modal submit.
- **DM rule:** none of these wizards DMs anyone. Outcomes are the verbs' notices and card edits.
- **Copy rule:** new or rewritten screens use the glossary's words and `PAGE_WORDS`: **event** for what a person reads
  on a new screen, Signup Reminder, Lock Confirmation, Automated Fill Requests, Sheet Opening DM, Locks, Confirm by, An
  ask waits, Priority. Phase 6's rename of every other string is **not** done here. An empty state names what is missing
  and how to fill it. A screen nobody answered is deleted (`Wizard.on_timeout`, unchanged).
- **Discord limits** (`wizard.py`: `MAX_OPTIONS, MAX_MODAL_ITEMS, MAX_LABEL, MAX_DESCRIPTION, MAX_TITLE, MAX_BUTTON =
  25, 5, 45, 100, 45, 80`): 5 rows per message (a select takes a row, buttons pack 5 to a row), ≤ 25 options per
  select, option label / value / description ≤ 100, modal ≤ 5 inputs, modal labels ≤ 45, message ≤ 2000 characters. An
  interaction answers within 3 s: any read that can take longer (a verb's write, `describe_ops`) goes behind
  `Wizard.working`. A box builder used as `refresh` must not solve.
- **Persistent buttons** stay `DynamicItem`s whose `custom_id` carries the guild key. No new persistent button is added.
- **No real guild data** in tests, fixtures or docs. 4-raid lineups are tested only on the widened TEST profile
  (`tests/test_lineups.py`'s `reg` fixture), never by widening `profiles/`.
- **Tests per task:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" <files>`. A failure under `-n` is re-run
  alone before it counts. Never `scripts/check_commands.py` inside a task (the controller runs it). Frontend:
  `cd frontend && npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py`, and commit the bundle.
- **Docs in the same commit** as the behaviour: `docs/manual.md` (the section each task names), and the touched modules'
  entries in `docs/layout/discord.md` / `confirms.md` / `events.md` / `web.md` / `frontend.md`.

## Review Focus

1. **The event locks while Edit event's or Edit lineup's screen is open.** The officer drafted a confirmation or
   priority change, the scheduler locked meanwhile, then they press Save. Expect the refusal box (`rc.CONFIRM_LOCKED` /
   `PRIORITY_LOCKED` / `LINEUP_LOCKED`) with Back only and nothing written. Owned by Task 3
   (`test_the_event_locks_while_edit_is_open`) and Task 4 (`test_a_lineup_edit_after_the_lock_is_refused_at_the_press`).
2. **Two officers on one event.** A's Move box is open and B moves the event first. A's press must show A the fresh box
   under `CONFIRM_WORDS["changed"]` and write nothing; A's second press then acts. Owned by Task 1 (the mechanism) and
   Task 3 (`test_another_officer_moved_it_first`).
3. **A reschedule onto a clocks-change night.** 2:30 AM on the spring-forward night must be refused with `rc.MOVE_GAP`
   and Back alone, never silently moved to 3:30. Owned by Task 3 (`test_a_time_the_clocks_skip_is_refused`).
4. **A 4-raid lineup at Discord's limits.** The step select, the add-a-raid select, the starts select (≤ 25), the
   buttons (≤ 5 rows) and the 2000-character text must all pass `wizard.lint` with four raids of four rosters each.
   Adding a fifth raid shows `LINEUP_MAX_WORDS`. Owned by Task 4 (`test_a_four_raid_lineup_fits_one_screen`).
5. **A role switch on someone already rostered after the lock.** `/raid set` → Join as a spec of another role must
   print the `rc.SPEC_SEATED` refusal box (Back only), never a write that half-applies. Owned by Task 7
   (`test_a_spec_switch_on_a_seated_member_is_refused`).

---

### Task 1: The confirm screen speaks the box, and no button ends in an ellipsis

**Files:**
- Modify: `src/oibot_gm/wizard.py` (`Wizard.decide`, new `Wizard.decide_box`, `button_label`, `Wizard.button`, `lint`,
  `when_fields(step=)`)
- Modify (labels only, mechanical): every Discord module with a button literal ending in "…": `flows_raid.py`,
  `flows_config.py`, `flows_professions.py` (also its verb-less `decide` → `do_label="Save"`), `flows_recruit.py`,
  `flows_loot.py`, `flows_wishlist.py`, `flows_register.py`, `flows_test.py`, `discord_registry.py`,
  `discord_loot_surface.py`, `raid_buttons.py` (`RunButton`, `LootButton`, `LootStartButton` labels through
  `button_label`), `appeals.py` (`AppealButton.LABELS`), `raid_views.py` ("Judging…", "Re-scoring … item(s)…" → no
  ellipsis). Find them all with the test in Step 1.
- Test: `tests/test_wizard_box.py` (new), `tests/test_discord_labels.py` (new). Model: `tests/test_flows_raid.py`
  (Harness + `officer` fixture).

**Interfaces:**
- Consumes: `consequences.box(title, sentences, verb, *, danger=False, diff=(), rev="") -> dict` (keys `title, sentences,
  verb, danger, diff, fp`), `constants.CONFIRM_WORDS`.
- Produces:
  - `def button_label(text: str) -> str`: drops ONE trailing "…" or "..." and the space before it.
  - `Wizard.button(label, on_press, style=…, *, disabled=False, emoji=None)` applies `button_label`.
  - `Wizard.decide(interaction, text, *, do, do_label: str, note=None, danger=False, back_label="Back", extra=(),
    hint=None)`: `do_label` is now **required**, and the note button is `f"{do_label} with a note"`.
  - `def box_text(box: dict, *, changed: bool = False) -> str`: `CONFIRM_WORDS["changed"]` first when `changed`, then
    `**{title}**`, then each diff line, then each sentence on its own line. Nothing is added or reworded.
  - `async def Wizard.decide_box(self, interaction, box: dict, *, do: Callable[[discord.Interaction, dict[str, str]],
    Awaitable[None]], refresh: Callable[[], dict | Awaitable[dict]] | None = None, note: Sequence[Field] = (), extra:
    Sequence[discord.ui.Item] = (), back: Callable[[discord.Interaction], Awaitable[None]] | None = None, hint: str |
    None = None) -> None`. The screen's text is `box_text(box)` in place of the wizard's own bold title (one heading,
    not two). Buttons, in order: the verb (danger / success); `f"{verb} with a note"` when `note` (a `Form` of those
    Fields titled `f"{verb} with a note"[:MAX_TITLE]`, whose Submit is the write); `extra`; then `Back`. `back=None`
    finishes "Nothing was changed."; otherwise it goes to that screen. `verb is None` gives `Back` alone. At the verb's
    press, and again at the note's submit (owner and guard re-checked there), `refresh()` is awaited if it is a
    coroutine. A different `fp` re-renders `decide_box(fresh, changed=True)` with the same callbacks and writes nothing.
    A fresh refusal re-renders as a refusal. Otherwise `working()` runs, then `do(i, values)` (`{}` for a plain press).
  - `when_fields(reg, *, day0, default=None, later=True, day_note=None, step: int = 5)`: `step` reaches `minute_opts`.
  - `lint()` adds `button label ends in an ellipsis: {label!r}` for any Button label ending in "…" or "...".

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wizard_box.py
import asyncio, inspect
import discord
from oibot_gm import consequences as cq
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.wizard import Field, Opt, Wizard, button_label, lint
from wizard_harness import Harness

def run(c): return asyncio.run(c)

def test_decide_needs_a_verb_and_the_note_button_is_verb_with_a_note(reg):
    assert inspect.signature(Wizard.decide).parameters["do_label"].default is inspect.Parameter.empty
    h = Harness(reg); wiz = Wizard(reg, h.user.id, title="T")
    async def do(i, note): pass
    run(wiz.decide(h.command(), "It moves.", do=do, do_label="Move", note={"label": "Note"}))
    assert h.buttons() == ["Move", "Move with a note", "Back"]

def test_the_box_prints_title_diff_sentences_and_its_verb(reg):
    h = Harness(reg); wiz = Wizard(reg, h.user.id, title="Edit event")
    b = cq.box("Change Barrow Deeps on Tue 9 Dec?", ["It locks a day before.", "Nobody is told."], "Save",
               diff=["Locks: 2 hours before → 24 hours before"])
    async def do(i, v): pass
    run(wiz.decide_box(h.command(), b, do=do))
    t = h.text
    assert t.index("**Change Barrow Deeps") < t.index("Locks: 2 hours") < t.index("It locks a day before.")
    assert "**Edit event**" not in t, "one heading: the box's title"
    assert h.buttons() == ["Save", "Back"]
    assert h.screen["view"].children[0].style is discord.ButtonStyle.success

def test_a_danger_box_is_red_and_a_refusal_has_back_alone(reg):
    h = Harness(reg); wiz = Wizard(reg, h.user.id, title="T")
    async def do(i, v): pass
    run(wiz.decide_box(h.command(), cq.box("Cancel X?", ["It goes."], "Cancel event", danger=True), do=do))
    assert h.screen["view"].children[0].style is discord.ButtonStyle.danger
    run(wiz.decide_box(h.command(), cq.refusal_box("That time is taken", "Barrow Deeps already has an event then."), do=do))
    assert h.buttons() == ["Back"] and "already has an event then." in h.text

def test_a_box_that_changed_is_shown_again_and_writes_nothing(reg):
    h = Harness(reg); wiz = Wizard(reg, h.user.id, title="T")
    calls, now = [], {"b": cq.box("Move X?", ["It moves to 8:00 PM."], "Move")}
    async def do(i, v): calls.append(v); await wiz.finish(i, "done")
    shown = now["b"]
    run(wiz.decide_box(h.command(), shown, do=do, refresh=lambda: now["b"]))
    now["b"] = cq.box("Move X?", ["It moves to 9:00 PM."], "Move")
    run(h.press("Move"))
    assert calls == [] and h.text.startswith(CONFIRM_WORDS["changed"]) and "9:00 PM" in h.text
    run(h.press("Move"))
    assert calls == [{}]

def test_the_note_form_is_the_write_and_rechecks_the_box(reg):
    h = Harness(reg); wiz = Wizard(reg, h.user.id, title="T"); calls = []
    async def do(i, v): calls.append(v); await wiz.finish(i, "done")
    b = cq.box("Cancel X?", ["It goes."], "Cancel event", danger=True)
    run(wiz.decide_box(h.command(), b, do=do, refresh=lambda: b, note=[Field("reason", "Why? (optional)", required=False, paragraph=True)]))
    run(h.press("Cancel event with a note"))
    run(h.submit(reason="Half the raid is sick."))
    assert calls == [{"reason": "Half the raid is sick."}]

def test_button_label_and_lint():
    assert button_label("Fill seats…") == "Fill seats" and button_label("Move it...") == "Move it" and button_label("Lock") == "Lock"
    v = discord.ui.View(); v.add_item(discord.ui.Button(label="Remove…"))
    assert any("ellipsis" in e for e in lint(v))
```

```python
# tests/test_discord_labels.py — every button the bot can render (§7b Discord: "No Discord button label ends in an ellipsis")
import ast
from pathlib import Path
from oibot_gm import raid_buttons as rb, appeals, flows_test, discord_registry
from oibot_gm.raid_buttons import RUN_WORDS, RunButton, LootButton, LootStartButton

PY = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
ELLIPSIS = ("…", "...")

def _tail(node) -> str | None:  # the literal end of a str / f-string argument
    if isinstance(node, ast.Constant) and isinstance(node.value, str): return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[-1], ast.Constant): return node.values[-1].value
    return None

def test_no_button_literal_ends_in_an_ellipsis():
    bad = []
    for f in PY.rglob("*.py"):
        if "web" in f.parts: continue
        for n in ast.walk(ast.parse(f.read_text())):
            if not isinstance(n, ast.Call): continue
            name = getattr(n.func, "attr", getattr(n.func, "id", ""))
            args = ([n.args[0]] if name == "button" and n.args else []) + [k.value for k in n.keywords if k.arg == "label" and name != "Field"]
            bad += [f"{f.name}:{n.lineno} {t!r}" for a in args if (t := _tail(a)) and t.rstrip().endswith(ELLIPSIS)]
    assert not bad, bad

def test_every_persistent_button_renders_without_one():
    items = [RunButton("demo", "bd-abcdef", a) for a in RunButton.LABELS] + [LootButton("demo", "s", "-", k) for k in LootButton.LABELS]
    items += [LootStartButton("demo", "open"), flows_test.TestAnswerButton("demo", "bd-abcdef")]
    items += [appeals.AppealButton("demo", k, "a1") for k in appeals.AppealButton.LABELS]
    items += [discord_registry.ApplicationButton(a, 1, "demo") for a in ("accept", "decline")]  # (action, discord_id, gk)
    assert not [i.item.label for i in items if (i.item.label or "").endswith(ELLIPSIS)]

def test_the_sites_shared_words_keep_theirs():
    assert RUN_WORDS["fill"] == "Fill seats…", "the site's menu word (D1)"
    assert RunButton("demo", "bd-abcdef", "fill").item.label == "Fill seats"
```

(Check each `DynamicItem` constructor's real argument order with grep before running. The list in
`test_every_persistent_button_renders_without_one` must name every `DynamicItem[discord.ui.Button]` subclass in the
package: `grep -rn "DynamicItem\[discord.ui.Button\]" src/oibot_gm`.)

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_wizard_box.py tests/test_discord_labels.py`
Expected: FAIL (`button_label` / `decide_box` not defined; the AST test lists every ellipsis literal).

- [ ] **Step 3: Implement the Interfaces block in `wizard.py`, then sweep the labels.** Drop each listed literal's
  ellipsis at its source. Pass shared words (`RUN_WORDS`, `rc.ACTION_LABELS`, `ARMORY_ACTIONS`, `MOVE_LABEL`, `SET_WHAT`
  where it becomes a button) through `button_label` where they become a Discord button. Existing tests that press a label
  by its old "…" spelling change with it: `grep -rn 'press("[^"]*…")' tests/`.

- [ ] **Step 4: Run the two new files plus every flow test that presses a renamed button**

Run: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_wizard_box.py tests/test_discord_labels.py tests/test_flows_raid.py tests/test_raid_night_flows.py tests/test_flows_config.py tests/test_professions_flows.py tests/test_loot_surface.py tests/test_appeals.py tests/test_armory_flows.py tests/test_flows_misc.py`
Expected: PASS.

- [ ] **Step 5: Docs and commit.** `docs/layout/discord.md` › `wizard.py`: `decide_box`, `box_text`, `button_label` and
  the lint rule. `docs/manual.md` §1a *The words*: one line saying a Discord confirm's first button is the action's verb.
  `git commit -m "Discord confirms: the server's box on one screen; no button ends in an ellipsis."`

---

### Task 2: The run cards' Lock, Cancel, Remind and End print the server's boxes

**Files:**
- Modify: `src/oibot_gm/raid_buttons.py` (`lock_decide`, `cancel_decide`, `remind_decide`, `end_decide`)
- Modify: `src/oibot_gm/flows_loot.py` (the `/raid end` decide, `loot_end` — its `do_label="End"` screen)
- Test: `tests/test_discord_boxes.py` (new). Model: `tests/test_raid_night_flows.py::test_lock_card_remind_pause_end_and_reopen` (its `NightFlowBot`).

**Interfaces:**
- Consumes: Task 1's `Wizard.decide_box`; `cq.lock_box(reg, ev, rs)`, `cq.cancel_box(reg, ev, rs)`,
  `cq.remind_box(reg, ev, rs)`, `bot.end_box(reg, rs, ev)` (LootMixin), `rc.cancel_tells_channel(rs, ev)`.
- Produces: the same four entry points with unchanged signatures (`lock_decide(interaction, bot, reg, rs, ev, wiz=None)`
  etc.). Each builds `refresh=lambda: <same builder>(… rs.events.get(key) or ev …)`. Verbs come from the boxes: Lock,
  Cancel event, Send reminder, End the run. Lock keeps `extra=[Preview lock]`; its preview screen's Lock press goes back
  through `decide_box`, so a box that changed during the preview is shown again. Cancel's
  `note=[reason Field, notify Field when rc.cancel_tells_channel]`: a plain press calls `bot.cancel_run(…, reason=None,
  notify=True)` (D3). The early `interaction.response.send_message(why)` refusals in `end_decide` / `remind_decide`
  become the refusal box.

- [ ] **Step 1: Write the failing tests** (in `tests/test_discord_boxes.py`; reuse `NightFlowBot`, `channel`, `officer`
  and `Harness(reg, rs, client=bot)`; open and join with `conftest.open_test_run` / `join` as the model test does)

```python
def test_lock_screen_is_the_lock_box(reg, rs, officer, channel):
    ev = joined_run(reg, rs, 6); h = Harness(reg, rs, client=bot_for(reg, rs))
    run(FLOWS["raid_lock"](h.command(), reg))
    box = cq.lock_box(reg, ev, rs)
    assert box["title"] in h.text and all(s in h.text for s in box["sentences"])
    assert h.buttons() == ["Lock", "Preview lock", "Back"]

def test_a_lock_box_that_changed_is_shown_again(reg, rs, officer, channel):
    ev = joined_run(reg, rs, 6); bot = bot_for(reg, rs); h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_lock"](h.command(), reg))
    join(reg, rs, ev, reals(reg, 1, start=6))          # someone joins while the box is open
    run(h.press("Lock"))
    assert not [c for c in bot.calls if c[0] == "lock_run"] and h.text.startswith(CONFIRM_WORDS["changed"])

def test_cancel_is_red_and_its_note_carries_the_reason_and_the_channel(reg, rs, officer, channel):
    ev = joined_run(reg, rs, 3); bot = bot_for(reg, rs); h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_cancel"](h.command(), reg))
    assert h.buttons() == ["Cancel event", "Cancel event with a note", "Back"]
    assert h.screen["view"].children[0].style is discord.ButtonStyle.danger
    run(h.press("Cancel event with a note")); run(h.submit(reason="Server maintenance.", notify="quiet"))
    assert ("cancel_run", ev.key, "Server maintenance.", False) in [c[:4] for c in bot.calls]

def test_a_closed_run_shows_the_refusal_box_with_back_only(reg, rs, officer, channel): ...
    # rs.events[key].state = "cancelled" before the card press → buttons == ["Back"], text holds cq.cancel_box's one sentence

def test_remind_and_end_print_their_boxes(reg, rs, monkeypatch): ...
    # locked run with a pending confirmation: the lock card's Remind → buttons[0] == "Send reminder";
    # past its start: End → buttons[0] == "End the run" and the box sentences == bot.end_sentences(reg, rs, ev)
```

(Record `cancel_run` in `NightFlowBot` as `("cancel_run", ev.key, reason, notify)` if it is not recorded that way yet.
Change the fake, not the assertion.)

- [ ] **Step 2: Run them to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_discord_boxes.py`. Expected: FAIL (buttons read "Lock now" / "Cancel the run").
- [ ] **Step 3: Rewrite the four entry points and `loot_end` onto `decide_box` as the Interfaces block says.** No
  sentence is composed here. `run_label` stays off these screens, because the box title names the event.
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_discord_boxes.py tests/test_raid_night_flows.py tests/test_flows_raid.py tests/test_loot_followthrough.py`. Expected: PASS (update the old tests' pressed labels only).
- [ ] **Step 5: Docs and commit.** `docs/manual.md` §4 *Where the buttons are* (the box verbs on Discord) and §9 *End*.
  `docs/layout/discord.md` › `raid_buttons.py`. `git commit -m "Discord: Lock, Cancel, Remind and End print the server's boxes."`

---

### Task 3: Reschedule and Edit event wizards

**Files:**
- Create: `src/oibot_gm/flows_event.py`
- Modify: `src/oibot_gm/raid_commands.py` (`/raid reschedule`, `/raid edit`), `src/oibot_gm/wizard_flows.py` (import
  `flows_event`)
- Test: `tests/test_flows_event.py` (new). Model: `tests/test_flows_raid.py` for the Harness, `tests/test_reschedule.py`
  for the run (`open_one_off`, `real_people`, `moved_notices`, `OFFICER_ID`), and `tests/test_raid_night.NightBot` as the
  client (the real `reschedule_event`).

**Interfaces:**
- Consumes: Task 1 (`decide_box`, `when_fields(step=15)`, `button_label`); `RaidMixin.reschedule_event(reg, rs, ev, *,
  start=None, timing=None, by, by_id=None, via="discord", preview=False, rev=None) -> list[str]`; `rc.move_start(reg,
  value) -> datetime`; `rc.event_rev(reg, rs, ev) -> str`; `rc.first_part`; `rc.run_timing(reg, first) -> dict`;
  `event_view.edit_options(reg, rs, first, timing) -> {fields, values, splits, hours, max_weight, split_policies}`;
  `event_view.EDIT_AFTER_LOCK`; `cq.reschedule_pick_box(reg, rs, ev)`, `cq.reschedule_box(reg, rs, ev, start, timing=None,
  *, edit=False)`, `cq.refusal_box_for(sentence)`; `PAGE_WORDS`.
- Produces (Task 4 and Task 5 use these):
  - `async def pick_event(interaction, reg, *, title: str, then: Callable[[discord.Interaction, Wizard, rc.RaidStore,
    rc.RaidEvent | None], Awaitable[None]], states: tuple[str, ...] = ("open", "locked"), wizard: type[Wizard] =
    Wizard) -> None`. It lists live FIRST parts only (`rc.first_part(rs, ev) is ev`), soonest first, ≤ 25. Option label
    `raid_views.run_title`, value = key. One event goes straight to `then`. None gives `NO_EVENT = "No event is open.
    Open one with /raid open."`.
  - `class EventWizard(Wizard)`: `__init__(self, reg, rs, owner_id, key, *, title)`, guard = `flows_raid.officer_guard`.
    Helpers `ev()` (re-read `rs.events.get(key)`) and `async def act(i, coro_fn)`: `working`, then `finish("✅ " + line)`;
    a `RegistryError` finishes with its sentence.
  - flow `raid_reschedule` → `RescheduleWizard(EventWizard)`. First screen: `reschedule_pick_box` text and a "Pick the
    day and time" button. That opens a Form of `when_fields(reg, day0=reg.now_local().date(), default=first.start,
    step=15)`. "later" gives a week select, as in `OpenWizard.when_submitted`. Then
    `rc.move_start(reg, f"{day} {h:02d}:{m:02d}")`, and its `RegistryError` becomes `decide_box(refusal_box_for(e),
    back=<this screen>)`. Then `decide_box(cq.reschedule_box(…, start), do=move, refresh=…, extra=[button("Pick another
    time", …)])`. `move` calls `reschedule_event(…, start=start, by_id=i.user.id, rev=<event_rev when the box was
    shown>)`.
  - flow `raid_edit` → `EditEventWizard(EventWizard)` with `self.timing: dict` (the draft, the `EVENT_TIMING` keys; the
    split as `{raid key: strategy}`).
    - Hub: an over or started event finishes `PAGE_WORDS["edit_over"]`. A locked one prefixes
      `PAGE_WORDS["edit_locked"]`. One line per allowed field reads `label: value` (draft → shown when changed) through
      `consequences.event_value_words`.
    - Components: select "Change a setting" (one option per allowed field; `weights` → "Priority"; `split_policy` →
      `PAGE_WORDS["schedule_fields"]["split_policy"]`), button "Review changes" (disabled without a change), Back.
    - Setting screens:
      - an hours field: a select of `edit_options()["hours"][f]`, words as labels, trimmed to 25 keeping the current
        value (`flows_config.target_numbers`'s rule);
      - a switch: a select of on / off (`PAGE_WORDS["on"|"off"]`);
      - `first_come`: `PAGE_WORDS["first_to_join"]` / `["by_priority"]`;
      - `weights`: one Form of five selects 0…`MAX_WEIGHT`, labelled `PAGE_WORDS["priority"][k]`;
      - `split_policy`: one select per live raid (≤ 4 rows) + Back.
    - Review changes → `decide_box(cq.reschedule_box(reg, rs, ev, None, self.timing, edit=True), refresh=same)`. Save →
      `reschedule_event(…, timing=self.timing, rev=…)`.
- Commands: `/raid reschedule` → `FLOWS["raid_reschedule"](interaction, reg)` and `/raid edit` → `FLOWS["raid_edit"]`,
  each officer-only with no arguments (Wizard rule). Descriptions: "Officer: move an open event to another day or time"
  and "Officer: change one event's own settings — when it locks, reminders, confirmation, priority".

- [ ] **Step 1: Write the failing tests** (`tests/test_flows_event.py`; client = `NightBot(reg, rs)`; Harness user id =
  `OFFICER_ID`; the `officer` fixture from `test_flows_raid`)

```python
def test_reschedule_picks_day_and_time_and_moves_with_the_box(reg, rs, officer):
    first = open_one_off(reg, rs, days=3); people = real_people(reg, 3); join(reg, rs, first, people)
    bot = NightBot(reg, rs); h = Harness(reg, rs, user=officer_user(), client=bot)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    assert cq.reschedule_pick_box(reg, rs, first)["title"] in h.text and h.buttons() == ["Pick the day and time", "Back"]
    run(h.press("Pick the day and time"))
    day = (first.start + timedelta(days=1)).date().isoformat()
    run(h.submit(day=day, hour="20", minute="15"))
    assert h.buttons() == ["Move", "Pick another time", "Back"] and "Move " in h.text
    run(h.press("Move"))
    assert first.start.astimezone(reg.tz).strftime("%Y-%m-%d %H:%M") == f"{day} 20:15"
    assert all(moved_notices(reg, p.discord_id) for p in people) and not bot.dm_channels, "a notice, never a DM"

def test_a_time_the_clocks_skip_is_refused(reg, rs, officer, monkeypatch):
    # guild zone America/New_York; pick the 2nd Sunday of March 2:30 AM (mock reg.now_local to a day before it)
    ...
    assert rc.MOVE_GAP in h.text and h.buttons() == ["Back"]
    run(h.press("Back")); assert h.buttons() == ["Pick the day and time", "Back"]

def test_another_officer_moved_it_first(reg, rs, officer):
    ...  # box for 20:15 shown, then rc.reschedule(reg, rs, first, start=<21:00>, by="Other") before the press
    run(h.press("Move"))
    assert h.text.startswith(CONFIRM_WORDS["changed"]) and first.start.hour == 21, "nothing written by this press"

def test_edit_event_changes_lock_and_seating_through_the_edit_box(reg, rs, officer):
    first = open_one_off(reg, rs, days=4); h = ...
    run(FLOWS["raid_edit"](h.command(), reg))
    assert PAGE_WORDS["schedule_fields"]["nudge"] in h.text and "Nudge" not in h.text
    run(h.pick("Change a setting", "lock_hours_before")); run(h.pick(PAGE_WORDS["schedule_fields"]["lock_hours_before"], "24"))
    run(h.pick("Change a setting", "first_come")); run(h.pick(PAGE_WORDS["schedule_fields"]["first_come"], "priority"))
    run(h.press("Review changes"))
    box = cq.reschedule_box(reg, rs, first, None, {"lock_hours_before": 24, "first_come": False}, edit=True)
    assert all(d in h.text for d in box["diff"]) and h.buttons() == ["Save", "Back"]
    run(h.press("Save"))
    t = rc.run_timing(reg, first); assert t["lock_hours_before"] == 24 and t["first_come"] is False

def test_edit_after_the_lock_offers_only_the_asks_and_the_wait(reg, rs, officer):
    ...  # lock_with_board(reg, rs, first, …)
    sel = next(s for s in h.screen["view"].children if getattr(s, "placeholder", "") == "Change a setting")
    assert {o.value for o in sel.options} == set(EDIT_AFTER_LOCK) and PAGE_WORDS["edit_locked"] in h.text

def test_the_event_locks_while_edit_is_open(reg, rs, officer):
    ...  # draft confirm=False, press Review changes, THEN lock_with_board, then press Save
    assert h.buttons() == ["Back"] and rc.CONFIRM_LOCKED in h.text and rc.run_confirms(reg, first) is True

def test_the_weights_are_one_modal_of_five_selects(reg, rs, officer):
    ...  # pick "weights" → h.modal has 5 selects whose labels == list(PAGE_WORDS["priority"].values())

def test_the_commands_take_no_typed_value():
    # build the tree offline (as test_parity_matrix does): /raid reschedule and /raid edit have no parameters
```

- [ ] **Step 2: Run them to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_event.py`.
- [ ] **Step 3: Implement `flows_event.py` and the two commands per the Interfaces block.** Every screen stays at 5 rows
  or fewer. The weights screen is a modal because five selects take five rows.
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_event.py tests/test_flows_raid.py`. Expected: PASS.
- [ ] **Step 5: Docs and commit.** `docs/manual.md` §4 *Moving a run, and its own sheet timing (officers)*: the Discord
  path (`/raid reschedule`, `/raid edit`, the hub and the box). Do not rename the heading (phase 6). Then §7 *Officers:
  day-to-day commands*. `docs/layout/discord.md`: a `flows_event.py` entry and `raid_commands.py`.
  `git commit -m "Discord: Reschedule and Edit event wizards on the server's boxes."`

---

### Task 4: Edit lineup from Discord (the lineup block)

**Files:**
- Modify: `src/oibot_gm/flows_event.py` (add `LineupBlock` and `EditLineupWizard`), `src/oibot_gm/raid_commands.py`
  (`/raid lineup`)
- Test: `tests/test_flows_lineup.py` (new). The 4-raid cases use `tests/test_lineups.py`'s widened-profile `reg` fixture
  (import it, as `test_events_lineups.py` does).

**Interfaces:**
- Consumes: Task 3's `pick_event`, `EventWizard`; `rc.run_lineup(reg, rs, ev) -> [{raid, rosters, starts}]`;
  `rc.lineup_fixed(rs, ev)`; `rc.after_minutes(start, minutes)`; `Registry.lineup_offsets(lineup) -> list[int]`;
  `registry.LINEUP_MAX` (4), `LINEUP_MAX_WORDS`, `MAX_SCHEDULE_ROSTERS`; `cq.lineup_box(reg, rs, ev, lineup)`;
  `RaidMixin.edit_lineup(reg, rs, ev, lineup, *, by, by_id=None, via="discord", preview=False, rev=None) -> list[str]`.
- Produces (Task 5 and Task 6 reuse it):
  - `class LineupBlock`, a mixin on a `Wizard` subclass that holds `self.lineup: list[dict]` (entry 0 = the first raid,
    `starts` 0; later entries carry `starts` in minutes after the first). It needs `def lineup_start(self) -> datetime
    | None` (for clock words; None for a schedule, which then shows "N h M min after the first") and `async def
    lineup_done(self, i)` (what Review does).
    - `async def lineup_hub(self, i, note="")`:
      - text: one line per step, `"{n}. {raid name} · {rosters} roster(s) · {start words}"`;
      - select "Pick a step" (one option per step);
      - select "Add a raid": the profile's raids not in the lineup. The select is disabled at `LINEUP_MAX`, with
        `LINEUP_MAX_WORDS` in the text. Over 25 raids it pages with `OwnerWizard.paged`;
      - buttons "Review changes" and Back.
    - `async def lineup_step(self, i, n)`:
      - select "Rosters" (1…`MAX_SCHEDULE_ROSTERS`);
      - for n ≥ 1, select "Starts": 24 options in 15-minute steps beginning at the previous step's start plus 15
        minutes, with the `lineup_offsets` default ("When <previous raid> ends · 9:15 PM") first, ≤ 25;
      - buttons Move earlier (n ≥ 2), Move later, Remove (n ≥ 1) and Back. Step 1's raid never changes (`LINEUP_FIRST`).
    - Moving or removing a step clears the later steps' `starts`, so each takes its `lineup_offsets` default again. The
      box's sentences then say the new times (requirement 7).
  - flow `raid_lineup` → `EditLineupWizard(LineupBlock, EventWizard)`, which starts from `rc.run_lineup`. A
    `rc.lineup_fixed` refusal goes straight to its refusal box. `lineup_done` →
    `decide_box(cq.lineup_box(reg, rs, ev, self.lineup), refresh=same)` → `edit_lineup(…, rev=)`.
  - `/raid lineup` (officer, no arguments): "Officer: change the raids of an open event before it locks".

- [ ] **Step 1: Write the failing tests**

```python
def test_add_a_raid_and_save_through_the_lineup_box(reg, rs, officer):  # the widened TEST profile
    first = open_one_off(reg, rs, days=3); h = officer_harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Add a raid", RAID_B)); run(h.press("Review changes"))
    box = cq.lineup_box(reg, rs, first, [{"raid": first.instance, "rosters": 1, "starts": 0}, {"raid": RAID_B, "rosters": 1}])
    assert box["verb"] and all(d in h.text for d in box["diff"])
    run(h.press(box["verb"]))
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [first.instance, RAID_B]

def test_a_four_raid_lineup_fits_one_screen(reg, rs, officer):
    ...  # add three raids, four rosters each; every screen passes the Harness lint (≤ 5 rows, ≤ 25 options, ≤ 2000 chars)
    add = next(s for s in h.screen["view"].children if getattr(s, "placeholder", "") == "Add a raid")
    assert add.disabled and LINEUP_MAX_WORDS in h.text
    run(h.pick("Pick a step", "3"))
    starts = next(s for s in h.screen["view"].children if getattr(s, "placeholder", "") == "Starts")
    assert len(starts.options) <= 25 and all(o.label[-2:] in ("AM", "PM") for o in starts.options[1:])

def test_removing_a_middle_raid_says_the_next_moves_earlier(reg, rs, officer):
    ...  # a 3-raid event; Pick a step 2 → Remove → Review changes: the box's sentences name raid 3's new start

def test_a_lineup_edit_after_the_lock_is_refused_at_the_press(reg, rs, officer):
    ...  # draft shown, then lock_with_board, then press the verb → h.buttons() == ["Back"] and rc.LINEUP_LOCKED in h.text

def test_the_first_raid_never_changes(reg, rs, officer):
    ...  # Pick a step 1 → no Remove / Move button, only Rosters
```

- [ ] **Step 2: Run them to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_lineup.py`.
- [ ] **Step 3: Implement `LineupBlock`, `EditLineupWizard` and `/raid lineup`.** Start words come from
  `reg.local12(rc.after_minutes(start, m), "%I:%M %p")` (local12 strips the zero).
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_lineup.py tests/test_flows_event.py tests/test_lineups.py`. Expected: PASS.
- [ ] **Step 5: Docs and commit.** `docs/manual.md` §4 *Changing the raids of an open run (officers)*: the Discord path.
  `docs/layout/discord.md` › `flows_event.py`. `git commit -m "Discord: Edit lineup, up to four raids, on the lineup box."`

---

### Task 5: `/raid open` builds a one-off lineup and prints box 11 for a schedule's night

**Files:**
- Modify: `src/oibot_gm/flows_raid.py` (`OpenWizard`), `src/oibot_gm/consequences.py` (`open_box_for`, moved from
  `web/api.py`'s nested `open_box`), `src/oibot_gm/web/api.py` (the open routes call it)
- Test: `tests/test_flows_open.py` (new). Model: `tests/test_flows_raid.py::test_open_next_slot_is_one_press_after_the_raid`.

**Interfaces:**
- Consumes: Task 4's `LineupBlock`; `rc.open_preview(reg, rs, rid, value=None, schedule=None, follow=None, lineup=None,
  first_rosters=None) -> dict`; `event_view.new_event_view(reg, rs, rid, p, first_rosters) -> {facts, lineup, notes,
  refusal_box}`; `event_view.NEW_EVENT_LOCKS` (2, 24) / `lock_choices`; `cq.open_now_box(reg, start, opens, lock_at, *,
  lineup=None, channel=True, rev="")`; `RaidMixin.open_run_and_post(reg, rs, instance, start, by, schedule=None,
  lineup=None, lock_hours=None, first_rosters=None)`.
- Produces:
  - `def open_box_for(reg, rid: str, p: dict) -> dict` in `consequences.py`: exactly the old nested `open_box`, plus
    `rev=p.get("occurrence") or ""`. It raises `KeyError` / `TypeError` / `ValueError` when the schedule's lead or lock
    can't be read; the route keeps its `OPEN_UNREAD` handling. `web/api.py` calls it, so both surfaces print one box.
  - `OpenWizard` changes:
    - **A schedule's night** (Next / Another upcoming / a one-off minute that `rc.which_run` says is scheduled) →
      `decide_box(open_box_for(reg, rid, rc.open_preview(…)), refresh=same, do=save)`. The box verb is its own. A
      different occurrence at the press is a fresh box, never an open of another night.
    - **A one-off** (OpenWizard + `LineupBlock`) → day and time (`when_fields(step=15)`) → `lineup_hub` (first raid
      fixed = the picked raid) → a "Locks" select of `NEW_EVENT_LOCKS` in words (`hours_words(h) + " before"`) and
      "Rosters" for the first raid → "What this makes": `new_event_view`'s facts as `label: value` lines, then its notes,
      then the lineup lines. Buttons: "Open the sheet" (success) · Change · Back. This screen is the confirmation, as on
      the site (§7b), so there is no extra box. `new_event_view(...)["refusal_box"]` → `decide_box(refusal)`.
    - Pickup templates keep their path.
    - `no_slot_reason` loses "in the next N days". It says the raid has no schedule yet, or names the first opening.
- Keep every screen ≤ 5 rows: the "When" screen is a modal, and the one-off's settings are two selects + three buttons.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_schedules_night_ahead_prints_box_11(reg, rs, officer, channel):
    with_slots(reg); h = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg)); run(h.press(next(b for b in h.buttons() if b.startswith("Next run"))))
    p = rc.open_preview(reg, rs, "barrow_deeps", None, h_sched(reg))  # the night the button names
    box = cq.open_box_for(reg, "barrow_deeps", p)
    assert all(s in h.text for s in box["sentences"]) and h.buttons()[0] == box["verb"]

def test_a_one_off_with_a_second_raid_opens_one_sheet(reg, rs, officer, channel):
    ...  # Ad hoc → One-off → submit day/hour/minute → Add a raid → Review changes → Locks "24" → What this makes
    assert "One signup sheet" in h.text and h.buttons()[0] == "Open the sheet"
    run(h.press("Open the sheet"))
    assert ("open_run_and_post", ...) carries lineup=[{"raid": RAID_B, ...}] and lock_hours=24

def test_the_open_box_is_one_function_for_both_surfaces(reg, rs, web_bot):
    ...  # POST /api/raid/{rid}/open/preview's "box" == cq.open_box_for(reg, rid, preview body)

def test_no_horizon_words_left(reg, rs, officer, channel):
    ...  # a raid with no schedule: "next 21 days" not in h.text and the text says how to add a schedule
```

(The bot here is `test_flows_raid.FakeBot`. Record `open_run_and_post`'s `lineup`, `lock_hours` and `first_rosters` in
it if it does not yet.)

- [ ] **Step 2: Run them to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_open.py`.
- [ ] **Step 3: Move `open_box` into `consequences.open_box_for` and point `web/api.py` at it. Then change `OpenWizard` as
  the Interfaces block says.**
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_open.py tests/test_flows_raid.py tests/test_raid_night_flows.py tests/test_confirm_boxes.py tests/test_events_api.py`. Expected: PASS.
- [ ] **Step 5: Docs and commit.** `docs/manual.md` §4: the paragraph on `/raid open` (a one-off lineup, what this
  makes, the box for a night ahead). `docs/layout/discord.md` › `flows_raid.py`. `docs/layout/confirms.md` ›
  `open_box_for`. `git commit -m "Discord: /raid open builds a one-off lineup and prints the open box."`

---

### Task 6: A schedule wizard with its lineup step, and the raid wizard's Save box

**Files:**
- Create: `src/oibot_gm/flows_schedule.py`
- Modify: `src/oibot_gm/flows_config.py` (`RaidConfigWizard`: the `times` section hands off to `FLOWS["schedule"]`. The
  schedule methods are deleted: `times` … `time_added`. So are `BOOL_WORDS` / `FIELD_WORDS` entries no longer read. The
  `review` / `save` go through configops with box 19), `src/oibot_gm/wizard_flows.py`
- Test: `tests/test_flows_schedule.py` (new). Model: the AuraWizard and CompTargetsBlock tests in
  `tests/test_flows_config.py` (structured ops through `apply_ops`).

**Interfaces:**
- Consumes: Task 4's `LineupBlock` (`lineup_start()` → None: words "N min after the first");
  `Registry.all_schedules()`, `schedule_entry(sid)`, `first_raid(sid)`, `config_rev("schedule:<id>")`;
  `registry.schedule_name(row)`, `RESERVED_SCHEDULE_IDS`; `schedule_view.stored_entry(row) -> dict`, `editor_options(reg)`
  (`hours`, `presets`, `split_policies`, `max_weight`), `schedule_list(reg)`; `configops.ops_from_fields(reg,
  "schedule" | "schedule_remove" | "raid", target, fields, revs=) -> list[ConfigOp]`, `describe_ops(reg, ops,
  by_id=) -> Described` (`.box`, `.lines[].error`), `apply_ops(reg, ops, *, by, by_id, bot, via="discord", revs=) ->
  {applied, refused, results, error, changed, stale_box}`, `CONFIG_REV`.
- Produces:
  - flow `schedule`: `async def open_schedule(interaction, reg, *, raid: str | None = None, **_) -> None` →
    `ScheduleWizard(LineupBlock, OwnerWizard)`.
    - **List:** the guild's schedules (those whose first raid is `raid` when given) in two groups, Active and Paused,
      named by `schedule_name`. A "New schedule" button. An empty list says "No schedule runs <raid> yet. Add one with
      New schedule."
    - **One schedule hub** (draft = `stored_entry(row)` + changes; rev = `config_rev(f"schedule:{sid}")` read when the
      hub opened):
      - lines Repeats, Lineup, Sheet timing (`PAGE_WORDS["schedule_fields"]` labels, `hours_words`), Priority, Split;
      - select "Change": Name (modal, prose) · Repeats (kind; weekly run times as day + hour + 15-min selects; lockout
        days) · Lineup (`lineup_hub`) · Sheet timing (a preset select from `editor_options()["presets"]`, or one hours
        select per setting) · the four switches (one select `field=on|off`, labelled `PAGE_WORDS`) · Priority (a modal of
        five selects) · Split;
      - buttons "Review changes" · Pause or Resume · Remove · Back.
    - **Save:** `ops_from_fields(reg, "schedule", sid, draft, revs={f"{CONFIG_REV}schedule:{sid}": rev})` →
      `describe_ops(…, by_id=i.user.id).box` (box 14, or box 13 for a new one created with `kind` + `lineup` and no rev)
      → `decide_box(refresh=describe again)` → `apply_ops(…, revs=)`. A refused result finishes with its line.
    - **Pause / Resume:** fields `{"active": False | True}` → box 15 / 16. **Remove:** kind `schedule_remove` → box 17.
    - `describe_ops` and `apply_ops` run behind `working`, through `asyncio.to_thread` for the describe.
  - `RaidConfigWizard`: the `times` section → `FLOWS["schedule"](i, reg, raid=self.raid)`. Its `review` →
    `ops_from_fields(reg, "raid", rid, draft)` → `decide_box(describe_ops(...).box)` → `apply_ops`. The section names and
    switch labels it keeps come from `PAGE_WORDS` (`raid_fields`, `split`).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_schedules_lineup_step_saves_through_box_14(reg, rs, officer):
    sid = weekly(reg, "barrow_deeps", "Tue 19:30"); h = harness(reg, rs)
    run(FLOWS["schedule"](h.command(), reg, raid="barrow_deeps")); run(h.pick("Pick a schedule", sid))
    run(h.pick("Change", "lineup")); run(h.pick("Add a raid", RAID_B)); run(h.press("Review changes"))
    run(h.press("Review changes"))  # the hub's: the box
    assert h.buttons()[0] == "Save" and "Lineup:" in h.text
    run(h.press("Save"))
    assert [e["raid"] for e in reg.schedule_entry(sid)["lineup"]] == ["barrow_deeps", RAID_B]

def test_new_schedule_is_box_13_and_pause_is_box_15(reg, rs, officer): ...
def test_remove_is_box_17_and_red_only_if_the_box_says_so(reg, rs, officer): ...

def test_another_officer_saved_first(reg, rs, officer):
    ...  # hub open, reg.set_schedule(sid, {"name": "Other"}, "t"), then Save → text starts CONFIRM_WORDS["changed"]
    # or apply_ops's ConfigChanged sentence; the schedule keeps "Other"

def test_the_words_are_the_pages(reg, rs, officer):
    ...  # every screen of the hub: "Signup Reminder", "Lock Confirmation", "Automated Fill Requests", "Sheet Opening DM";
    # never "Nudge", "A run's week", "Fill seats automatically", "DM everyone on open"

def test_the_raid_wizards_save_is_box_19_and_its_schedules_section_hands_off(reg, rs, officer):
    ...  # /gm config raid → Schedules → the schedule wizard's list; Selection weights change → Review → box 19's verb
```

- [ ] **Step 2: Run them to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_schedule.py`.
- [ ] **Step 3: Implement `flows_schedule.py`, then trim `RaidConfigWizard`.** Delete the schedule methods; do not leave
  them unused. Update or delete the old `tests/test_flows_config.py` cases that drove the removed section. Each deleted
  test's behaviour must have its twin in `test_flows_schedule.py`.
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_flows_schedule.py tests/test_flows_config.py tests/test_phase3_setup.py tests/test_copy.py`. Expected: PASS.
- [ ] **Step 5: Docs and commit.** `docs/manual.md` §4 *Schedules* and *More raids the same night* (the Discord path),
  and §8 *Owner: setup* (`/gm config raid`'s sections). `docs/layout/discord.md`: a `flows_schedule.py` entry and
  `flows_config.py`. `git commit -m "Discord: a schedule wizard with its lineup step; schedule and raid saves on their boxes."`

---

### Task 7: Set for someone prints the box, with the role switch

**Files:**
- Modify: `src/oibot_gm/consequences.py` (`member_action_box`), `src/oibot_gm/web/api_me.py` (`admin_action`'s preview and
  `seen` use it), `src/oibot_gm/flows_roster.py` (`SetWizard`)
- Test: `tests/test_set_box.py` (new), plus `tests/test_confirm_readability.py` (render the new generator). Model:
  `tests/test_flows_raid.py::test_set_records_a_callout_with_a_note_before_lock`.

**Interfaces:**
- Consumes: Task 1's `decide_box`; `inbox.action_lines(reg, ev, m, key, character=None, *, spec=None, by_id=None)`,
  `inbox.no_longer_applies`, `inbox.do_member_action(bot, reg, rs, ev, m, key, *, character=None, note=None, spec=None,
  by, by_id, via="discord") -> str`; `rc.member_actions`, `rc.member_state_rev(ev, m)`, `rc.spec_role(reg, c, spec)`,
  `rc.SPEC_SEATED`; `wizard_opts.specs_of(reg, cls, ico, current=)`.
- Produces:
  - `def member_action_box(reg, ev, m, key: str, character: str | None = None, spec: str | None = None, *, by_id: int |
    None = None, rev: str = "") -> dict`. Answers (`join`, `bench`, `no_thanks`) keep `answer_box`'s title and verb
    ("Answer for someone" / "Record answer"). Every other key gets a title naming the person and a verb:
    - `callout`: "Record a callout for {name}?" / "Record the callout";
    - `confirm_yes`: "Confirm for {name}?" / "Confirm for them";
    - `confirm_no` / `fill_no`: "Can't make it for {name}?" / the `SET_WHAT` word;
    - `fill_yes`: "Confirm {name}'s fill?" / "Confirm their fill";
    - `pin_in`: "Pin {name} to the roster?" / "Pin";
    - `pin_out`: "Keep {name} off the roster?" / "Keep off";
    - `unpin`: "Unpin {name}?" / "Unpin".
    The sentences are always `action_lines`. `danger` is False for all of them, since each can be answered again.
    `answer_box` becomes a thin call of it for the three answers, so the site's Answer for someone is unchanged.
  - `admin_action`'s `preview` / `seen` branch builds `member_action_box` (was `answer_box`). It returns the same
    `{box}` / 409 / 400 shapes.
  - `SetWizard`:
    - `join` / `bench` with a character get one extra screen, the select "Plays which spec?" (`specs_of` for the
      character's class, current = their spec on the sheet, else the character's main spec).
    - `confirm()` → `decide_box(member_action_box(…, rev=member_state_rev), refresh=same, note=[Field("note", "Note for the
      officers (optional)", …)] if key == "callout")`.
    - The old "keep going?" screen and `self.draft["rev"]` check are deleted; the box's `fp` does that job now.
    - A preview `RegistryError` (a spec the write would refuse, `no_longer_applies`) → `decide_box(refusal_box_for(e))`.
    - The write passes `spec=`.

- [ ] **Step 1: Write the failing tests**

```python
def test_join_as_another_spec_switches_the_role_before_the_lock(reg, rs, officer, channel):
    ev = open_real_run(reg, rs); m = a_member_with_a_healer_main(reg); h = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    run(h.pick("Plays which spec?", DPS_SPEC_OF_THAT_CLASS))
    box = cq.member_action_box(reg, ev, m, "join", m.main.label, DPS_SPEC_OF_THAT_CLASS, by_id=h.user.id)
    assert h.buttons() == ["Record answer", "Back"] and all(s in h.text for s in box["sentences"])
    run(h.press("Record answer"))
    assert ev.signups[str(m.discord_id)].spec == DPS_SPEC_OF_THAT_CLASS

def test_a_spec_switch_on_a_seated_member_is_refused(reg, rs, officer, channel):
    ...  # locked run, m holds a seat → pick a spec of another role → h.buttons() == ["Back"] and SPEC_SEATED's words in h.text

def test_a_callout_is_its_own_box_with_a_note(reg, rs, officer, channel):
    ...  # buttons == ["Record the callout", "Record the callout with a note", "Back"]; the note reaches do_member_action

def test_an_answer_changed_meanwhile_is_a_fresh_box(reg, rs, officer, channel):
    ...  # the member answers Bench between show and press → CONFIRM_WORDS["changed"], nothing written; second press writes

def test_the_route_prints_the_same_box(reg, rs, web_bot):
    ...  # POST /api/admin/action {action: "pin_out", preview: True} → box == cq.member_action_box(..., rev=member_state_rev)
```

Add to `tests/test_confirm_readability.py` beside the `answer_box` rows: `add("member_action_box", "<key>", …)` for
`callout`, `pin_in`, `pin_out`, `unpin`, `confirm_yes`, `fill_yes` and `fill_no`.

- [ ] **Step 2: Run them to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_set_box.py`.
- [ ] **Step 3: Implement `member_action_box`, point the route at it, and rewrite `SetWizard.chosen` / `confirm` /
  `save`.**
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_set_box.py tests/test_flows_raid.py tests/test_confirm_readability.py tests/test_confirm_boxes.py tests/test_event_surfaces.py`. Expected: PASS.
- [ ] **Step 5: Docs and commit.** `docs/manual.md` §4 *Officers record anything a member can do* and *Role switches*
  (the Discord spec select). `docs/layout/confirms.md` › `member_action_box`. `docs/layout/discord.md` ›
  `flows_roster.py`. `docs/layout/web.md` › `api_me.py`.
  `git commit -m "Discord: /raid set prints the server's box and picks the spec (the role switch)."`

---

### Task 8: The site's row ⋮ menu prints the server's box

**Files:**
- Modify: `frontend/src/components/board/MemberMenu.tsx` (`useRecord` → `useConfirmBox`), the committed bundle under
  `src/oibot_gm/web/static/app`
- Test: `tests/test_frontend_source.py` (extend `test_event_controls_*`'s MemberMenu assertions)

**Interfaces:**
- Consumes: Task 7's `POST /api/admin/action {run, member, action, character?, note?, preview?, seen?}` → `{box}` /
  409 `{error, box}` / 400 `{error, box}`; `useConfirmBox(topics)` → `[ask({box, fields?, refresh?, act}), node,
  replace]`; `boxOf(e)` from `../events/shared`.
- Produces: `useRecord(run, onDone)` keeps its signature. A pick previews the box, then
  `ask({box, fields: <the callout's Textarea "Note for the officers (optional)"> when a.key === "callout", refresh:
  preview again, act: post {…, note, seen: fp}})`. `useConfirm` and every page-made title, message and confirmLabel are
  removed from the file. The Test bench's `SheetCard` menu gets the same box, because it passes no `onAnswer`.

- [ ] **Step 1: Write the failing test** (add to `tests/test_frontend_source.py`)

```python
def test_the_row_menu_prints_the_servers_box():
    menu = _code_only((SRC / "components" / "board" / "MemberMenu.tsx").read_text())
    rec = menu[menu.index("function useRecord"):menu.index("export type MoveTarget")]
    assert "useConfirmBox(" in rec and "preview: true" in rec and "seen: fp" in rec
    assert "useConfirm(" not in menu and "confirmLabel" not in menu and "message: consequence" not in menu
```

- [ ] **Step 2: Run to see it fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_frontend_source.py`.
- [ ] **Step 3: Rewrite `useRecord`** on the pattern of `useAnswerBox` in the same file (preview → ask → `seen`).
- [ ] **Step 4: Verify.** Run `cd frontend && npx tsc --noEmit && npm run build`, then
  `uv run python scripts/check_bundle.py`, then `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_frontend_source.py tests/test_copy.py`. Expected: all PASS.
- [ ] **Step 5: Docs and commit (with the bundle).** `docs/layout/frontend.md` › `MemberMenu`. `docs/manual.md` §2a
  *The website*: the row ⋮ asks in the same box as every other press. `git add frontend src/oibot_gm/web/static/app
  docs && git commit -m "Events page: the row menu's callouts and pins ask in the server's box."`

---

### Task 9: Parity closed, the readability sweep, and the docs

**Files:**
- Modify: `src/oibot_gm/configops.py` (`PARITY_PENDING = {}`), `tests/test_parity_matrix.py` (the Discord rows,
  `RUN_SWITCH_DISCORD_PENDING` removed, BY_DESIGN reasons for the board), `docs/design.md` (§5.67 "Phase 5 built"),
  `docs/manual.md` (§7 command list, §11 if it lists these as missing), `CLAUDE.md` only if its Layout index names a
  file this phase added (`flows_event.py`, `flows_schedule.py` belong in `docs/layout/discord.md`)
- Test: `tests/test_parity_matrix.py`, `tests/test_confirm_readability.py`, `tests/test_quiet_edges.py`,
  `tests/test_copy.py`, `tests/test_discord_labels.py`

**Interfaces:**
- Consumes: flows `raid_reschedule`, `raid_edit`, `raid_lineup` (Tasks 3–4), `raid_set` with spec (Task 7),
  `schedule` (Task 6).
- Produces: `SURFACES` rows:
  - `"run_reschedule": {"discord": "flow:raid_reschedule", "web": "POST /api/run/{key}/reschedule", "mcp": "reschedule_run"}`;
  - `"run_lineup": {"discord": "flow:raid_lineup", "web": "POST /api/run/{key}/lineup", "mcp": "edit_lineup"}`;
  - `RUN_SWITCH["run_switch"]["discord"] = "flow:raid_set"`.

  The board rows (`run_strategy`, `run_autofill`, `run_board`, `run_move`, `run_propose`) stay without a Discord path.
  If the matrix asks for an entry, each gets a `BY_DESIGN_EXEMPT` reason: "The board is the site's: an officer drags
  names on the Events page; Discord shows the roster the lock made."

- [ ] **Step 1: Write the failing assertions**

```python
# tests/test_parity_matrix.py
def test_no_parity_is_pending():
    assert configops.PARITY_PENDING == {}, "phase 5 closed the last Discord rows"
    assert "RUN_SWITCH_DISCORD_PENDING" not in globals()

def test_the_new_flows_are_registered_and_commands_take_no_typed_value(slash_paths):
    for f in ("raid_reschedule", "raid_edit", "raid_lineup", "schedule"):
        assert f in FLOWS
    for p in ("raid reschedule", "raid edit", "raid lineup"):
        assert p in slash_paths
```

Readability: every `decide_box` caller added in Tasks 2–7 prints a box built by a generator that
`tests/test_confirm_readability.py` already renders, or by `member_action_box` / `open_box_for` (added in Tasks 5 and
7). Add `open_box_for` with a scheduled night three days ahead and a lineup of two.

- [ ] **Step 2: Run to see them fail:** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_parity_matrix.py tests/test_confirm_readability.py`.
- [ ] **Step 3: Empty `PARITY_PENDING`, write the rows, and write the docs.** `docs/design.md` §5.67 gets **"Phase 5
  built (date): Discord."**, one paragraph naming:
  - `decide_box` and the `fp` re-check;
  - `button_label`, and D1's split between the site's and Discord's words;
  - the three commands;
  - `flows_event.py` / `flows_schedule.py`;
  - `member_action_box` and `open_box_for`;
  - the parity rows closed;
  - the decisions D1–D5 of this plan;
  - what phase 6 still owes (the word rename).

  Its "Later phases" line drops phase 5. Check `docs/manual.md` §1a's Discord line (Task 1) and every manual section
  named in Tasks 2–8 for one consistent account.
- [ ] **Step 4: Run** `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_parity_matrix.py tests/test_confirm_readability.py tests/test_quiet_edges.py tests/test_copy.py tests/test_discord_labels.py tests/test_wizard_box.py`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "Phase 5: Discord parity closed; design and manual say what was built."`

---

## Self-review (done)

- **Spec coverage:**

  | Spec item | Task |
  |---|---|
  | §9 item 5, the wizards for lineups | 4, 5, 6 |
  | §9 item 5, the wizard for reschedule | 3 |
  | §9 item 5, the confirm screens' buttons | 1, 2, 7 |
  | §7 Discord: `/gm config raid` loses run times and timing | 6 |
  | §7 Discord: the schedules wizard's lineup step | 6 |
  | §7 Discord: `/raid open` picks a night or builds a one-off | 5 |
  | §7 Discord: the Reschedule wizard | 3 |
  | §7b Discord: the verb first, "with a note", Back | 1 |
  | §7b Discord: no ellipsis | 1 |
  | §7b Discord: the same generators | 1–7 |
  | §7b: a changed box is shown again | 1, with tests in 2, 3, 4, 6, 7 |
  | §7b: a refusal has Back alone | 1, 3, 4, 7 |
  | Tweak B on Discord | 3 |
  | Tweak C, the board, stays the site's | 9 (BY_DESIGN) |
  | Role switch (`run_switch`) | 7 |
  | Parity | 9 |
  | The site's ⋮ leftover | 8 |
  | Docs | every task + 9 |

  The sheet's title naming the lineup (§7) is already built by phases 3–4 (`test_lineups.py::test_the_sheet_and_the_health_card_name_every_later_raid`).
- **Types:** `decide_box(…, do(i, values: dict))` is used the same way in Tasks 2–7. `pick_event` / `EventWizard` /
  `LineupBlock` are defined in Tasks 3–4 before Tasks 5–6 use them. `member_action_box`'s signature matches
  `answer_box`'s.
- **Proportion:** signatures, test assertions and decisions only. No bodies but the tests'.
