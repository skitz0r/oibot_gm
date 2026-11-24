import { Tooltip } from "@mantine/core";
import type { WeekStep } from "./board/types";
import css from "./raidcard.module.css";

/** A run's week as a strip (RunStrip): the test bench's tempo and SheetCard's run card. The words of a run's week live
 *  here once (HOUR_WORDS / DONE_WORDS / HOUR_HINT). The raid card's other pieces (Section, Panel, Pair, RunWeek,
 *  WhenItRuns, SeatWeights) went with the old Raids and Schedules pages (phase 4, Task 11): the Schedules page prints
 *  a schedule's week in the server's words. */

/** The steps of a run's week, counted back from the raid — one name per step on every surface. */
export const HOUR_WORDS: Record<string, string> = { signup_lead_hours: "sheet opens", nudge_hours_before: "reminder", lock_hours_before: "locks", confirm_hours_before: "confirm by", fill_ask_hours: "a fill ask waits" };
export const HOUR_HINT: Record<string, string> = {
  signup_lead_hours: "the sheet is posted and everyone can answer", nudge_hours_before: "one DM to the regulars who haven't answered (every main while the schedule has none)",
  lock_hours_before: "the roster is decided", confirm_hours_before: "the rostered confirm or say they can't make it by then", fill_ask_hours: "how long someone asked to fill a seat has to answer; no answer counts as no",
};

/** A step of a run's week once it has happened (the run card's strip); the rest read HOUR_WORDS. */
export const DONE_WORDS: Record<string, string> = { signup_lead_hours: "sheet opened", nudge_hours_before: "reminder", lock_hours_before: "locked", confirm_hours_before: "confirm by" };

/** One run's week as a strip (the Rosters card, design §5.56): the steps it takes in time order, evenly spaced, done in
 *  teal and the rest grey, the raid a square at the end, and a "now" mark between the steps it falls between. Every
 *  date comes in words from the server (ev_json `week`); the browser only places the mark. `still` = a week that hasn't
 *  started (the test bench's tempo, /api/test `tempos[].week`): the mark stays on the first step, nothing drawn as done. */
export function RunStrip({ steps, still = false }: { steps: WeekStep[]; still?: boolean }) {
  if (steps.length < 2) return null;
  const n = steps.length;
  const at = steps.map((s) => Date.parse(s.at));
  const t = still ? at[0] - 1 : Date.now();
  let now: number | null = null;
  if (t < at[0]) now = 0;
  else for (let i = 0; i < n - 1; i++) {
    if (at[i] <= t && t < at[i + 1]) { now = (i + (t - at[i]) / Math.max(1, at[i + 1] - at[i])) / (n - 1); break; }
  }
  return (
    <div className={css.strip} aria-label="the run's week">
      <span className={css.stripLine} />
      <span className={css.stripDone} style={{ width: `${(now ?? (t >= at[n - 1] ? 1 : 0)) * 100}%` }} />
      {steps.map((s, i) => {
        const raid = s.step === "raid";
        const label = raid ? "raid" : (s.done ? DONE_WORDS[s.step] : HOUR_WORDS[s.step]) || s.step;
        return (
          <Tooltip key={s.step} label={raid || !HOUR_HINT[s.step] ? s.when : `${s.when} · ${HOUR_HINT[s.step]}`}>
            <span className={[css.step, i === 0 ? css.stepFirst : "", i === n - 1 ? css.stepLast : "", i % 2 ? css.stepAlt : "", s.done ? "" : css.todo].join(" ")} style={{ left: `${(i / (n - 1)) * 100}%` }}>
              <span className={raid ? css.stepEnd : css.stepDot} />
              <span className={css.stepLabel}>{label}</span>
              <span className={css.stepWhen}>{s.when}</span>
            </span>
          </Tooltip>
        );
      })}
      {now !== null && <span className={css.now} style={{ left: `${now * 100}%` }}><span className={css.nowWord}>now</span><span className={css.nowBar} /></span>}
    </div>
  );
}
