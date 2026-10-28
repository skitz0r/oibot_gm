import { Text, Tooltip } from "@mantine/core";
import { Link } from "react-router-dom";
import type { LiveRun, NextRun, RaidRule } from "../api";
import type { WeekStep } from "./board/types";
import css from "./raidcard.module.css";

/** The raid card's pieces (the Raids page; mockup out/mockups/raidcard): a section heading with its one-line hint, when
 *  the raid runs, a run's week as a timeline and the who-gets-a-seat bars. Read-only: the owner edits all of it under
 *  Edit rules — the editors feed RunWeek their draft so the timeline moves as a value is typed. The words of a run's
 *  week live here once (HOUR_WORDS / SWITCH_WORDS / WEIGHT_WORDS): the card, the rules editor and the schedule editor
 *  all read them, so a step has one name everywhere. */

export function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className={css.section}>
      <div className={css.heading}>
        <Text size="xs" fw={700} tt="uppercase" c="dimmed" style={{ letterSpacing: ".08em" }}>{title}</Text>
        {hint && <Text size="xs" c="dimmed">{hint}</Text>}
      </div>
      {children}
    </section>
  );
}

export function Panel({ children }: { children: React.ReactNode }) {
  return <div className={css.panel}>{children}</div>;
}

export function Pair({ children }: { children: React.ReactNode }) {
  return <div className={css.pair}>{children}</div>;
}

/** The steps of a run's week, counted back from the raid — one name per step on every surface. */
export const HOUR_WORDS: Record<string, string> = { signup_lead_hours: "sheet opens", nudge_hours_before: "reminder", lock_hours_before: "locks", confirm_hours_before: "confirm by", fill_ask_hours: "a fill ask waits" };
export const HOUR_HINT: Record<string, string> = {
  signup_lead_hours: "the sheet is posted and everyone can answer", nudge_hours_before: "one DM to the regulars who haven't answered (every main while the schedule has none)",
  lock_hours_before: "the roster is decided", confirm_hours_before: "the rostered confirm or say they can't make it by then", fill_ask_hours: "how long someone asked to fill a seat has to answer; no answer counts as no",
};
export const SWITCH_WORDS: Record<string, string> = { nudge: "reminder", autofill: "fill seats automatically after the lock", open_dm: "DM the regulars when the sheet opens", confirm: "ask the rostered to confirm" };
export const SWITCH_HINT: Record<string, string> = {
  nudge: "one DM to the regulars who haven't answered", autofill: "off: an officer fills open seats from the Rosters page", open_dm: "every main while the schedule has no regulars", confirm: "off: a seat on the roster is final at the lock",
};

/** What RunWeek reads: a raid's rules, or an editor's draft shaped like one. */
export type WeekRule = Pick<RaidRule, "signup_lead_hours" | "nudge_hours_before" | "lock_hours_before" | "confirm_hours_before" | "fill_ask_hours" | "nudge" | "confirm" | "autofill" | "open_dm" | "overridden">
  & Partial<Pick<RaidRule, "slots" | "slot_labels" | "schedules">>;

type Tick = { label: string; hours: number; changed: boolean; why: string };

/** A run's week, counted back from the raid: sheet opens · reminder · locks · confirm by · the raid. A step the raid
 *  doesn't take (reminder off, no confirmations) has no tick — the sentence below says so. Amber = changed from the
 *  game's default. `start` / `startWhy` override the raid's own run times (an editor's draft). */
export function RunWeek({ r, start: startOverride, startWhy: startWhyOverride, sentence = true }: { r: WeekRule; start?: string; startWhy?: string; sentence?: boolean }) {
  const over = (...ks: string[]) => ks.some((k) => r.overridden.includes(k));
  const ticks: Tick[] = [
    { label: HOUR_WORDS.signup_lead_hours, hours: r.signup_lead_hours, changed: over("signup_lead_hours"), why: HOUR_HINT.signup_lead_hours },
    ...(r.nudge ? [{ label: HOUR_WORDS.nudge_hours_before, hours: r.nudge_hours_before, changed: over("nudge", "nudge_hours_before"), why: HOUR_HINT.nudge_hours_before }] : []),
    { label: HOUR_WORDS.lock_hours_before, hours: r.lock_hours_before, changed: over("lock_hours_before"), why: HOUR_HINT.lock_hours_before },
    ...(r.confirm !== false ? [{ label: HOUR_WORDS.confirm_hours_before, hours: r.confirm_hours_before, changed: over("confirm", "confirm_hours_before"), why: HOUR_HINT.confirm_hours_before }] : []),
  ].sort((a, b) => b.hours - a.hours);
  const labels = r.slot_labels || r.slots || [];
  // several schedules each set their own start: the raid's default times alone would leave the others out
  const many = (r.schedules || []).length > 1;
  const start = startOverride ?? (many || labels.length === 0 ? "each run's start" : labels.length === 1 ? labels[0] : `${labels.length} run times a week`);
  const startWhy = startWhyOverride ?? (many ? "each schedule below sets its own start times" : labels.length > 1 ? labels.join(" · ") : "each run's start; the schedules below set it");
  return (
    <>
      <ol className={css.week}>
        {ticks.map((t, i) => (
          <li key={t.label} className={[css.tick, i === 0 ? css.first : "", t.changed ? css.changed : ""].join(" ")} style={{ "--i": i, "--n": ticks.length } as React.CSSProperties}>
            <span className={css.dot} />
            <Tooltip label={t.why}>
              <span className={css.text}><span className={css.label}>{t.label}</span><span className={css.when}>{hoursBefore(t.hours)}</span></span>
            </Tooltip>
          </li>
        ))}
        <li className={[css.tick, css.last].join(" ")}>
          <span className={css.end} />
          <Tooltip label={startWhy}>
            <span className={css.text}><span className={css.label}>raid</span><span className={css.when}>{start}</span></span>
          </Tooltip>
        </li>
      </ol>
      {sentence && <Text size="sm" c="dimmed">{weekSentence(r).map((p, i) => <span key={i}>{i > 0 && " · "}<Text span inherit c={p.changed ? "yellow" : undefined}>{p.text}</Text></span>)}</Text>}
    </>
  );
}

function hoursBefore(h: number): string {
  return `${h} h before`;
}

/** What happens around the timeline, from the raid's switches — only what is true for it. */
function weekSentence(r: WeekRule): { text: string; changed: boolean }[] {
  const over = (...ks: string[]) => ks.some((k) => r.overridden.includes(k));
  return [
    { text: r.autofill ? "After the lock, the bot asks people to fill open seats (a role swap waits for an officer)" : "After the lock, an officer fills open seats (Fill seats on the Rosters page)", changed: over("autofill") },
    { text: `a fill ask waits ${r.fill_ask_hours} h for an answer`, changed: over("fill_ask_hours") },
    { text: r.open_dm ? "the regulars (every main while the schedule has none) get a DM when the sheet opens" : "no DM when the sheet opens", changed: over("open_dm") },
    ...(r.nudge ? [] : [{ text: "no reminder before the lock", changed: over("nudge") }]),
    ...(r.confirm !== false ? [] : [{ text: "the rostered aren't asked to confirm", changed: over("confirm") }]),
  ];
}

/** What WhenItRuns reads: the Raids card's raid, or the Rosters page's (both carry the same server fields). */
export type RunsOf = { live_runs?: LiveRun[]; next_run?: NextRun | null; first_open_words?: string; opened: boolean; schedules?: unknown[] };

/** When the raid runs, from the runs themselves (design §5.54): the sheets open or locked right now, then the next run
 *  its schedules will open, then — before the first sheet ever opens — when that is. A raid with none of these says so
 *  and how to change it. The Rosters page (design §5.56) passes `here` (a live run's time jumps to its card on this
 *  page), `upcoming` (its rows with Open, in place of the next-run line) and `nothing` (its own way out). */
export function WhenItRuns({ r, owner, here = false, upcoming, nothing: nothingText }: { r: RunsOf; owner: boolean; here?: boolean; upcoming?: React.ReactNode; nothing?: React.ReactNode }) {
  const live = r.live_runs || [];
  const nothing = !live.length && !(upcoming ?? r.next_run) && !(r.first_open_words && !r.opened);
  const how = (r.schedules || []).length ? "its schedules are paused or have no run coming" : owner ? "add a schedule below, or open a run from the Rosters page" : "the owner adds a schedule below; an officer can open a run from the Rosters page";
  return (
    <ul className={css.runs}>
      {live.map((e) => (
        <li key={e.key} className={css.run}>
          <span className={[css.runDot, e.state === "locked" ? css.runLocked : css.runOpen].join(" ")} />
          {here ? <a href={`#run-${e.key}`} className={css.runWhen}>{e.when}</a> : <Link to={`/rosters#run-${e.key}`} className={css.runWhen}>{e.when}</Link>}
          <span className={css.runWhat}>{e.state_word || e.state} · {e.schedule}</span>{/* the server's state words (rc.RUN_STATE_WORDS) — the Rosters card's chip says the same */}
        </li>
      ))}
      {upcoming ?? (r.next_run && (
        <li className={css.run}>
          <span className={[css.runDot, css.runNext].join(" ")} />
          <span className={css.runWhen}>{r.next_run.when}</span>
          <span className={css.runWhat}>next run · {r.next_run.schedule}{r.next_run.opens ? ` — its sheet opens ${r.next_run.opens}` : ""}</span>
        </li>
      ))}
      {r.first_open_words && !r.opened && (
        <li className={css.run}>
          <span className={[css.runDot, css.runNext].join(" ")} />
          <span className={css.runWhen}>{r.first_open_words}</span>
          <span className={css.runWhat}>the first scheduled sheet opens — its schedules open nothing before it</span>
        </li>
      )}
      {nothing && <li className={css.run}><Text size="sm" c="dimmed">{nothingText ?? <>No run coming up: {how}.</>}</Text></li>}
    </ul>
  );
}

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
            <span className={[css.step, i === 0 ? css.stepFirst : "", i === n - 1 ? css.stepLast : "", s.done ? "" : css.todo].join(" ")} style={{ left: `${(i / (n - 1)) * 100}%` }}>
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

export const WEIGHT_WORDS: Record<string, string> = { regular: "regular", rank: "rank", main: "main", sat_out: "sat out", signup_order: "signup order" };
export const WEIGHT_HINT: Record<string, string> = {
  rank: "core counts most, then raider, then trial; ranks are set on the Members page",
  main: "joined on their main character, not an alt",
  sat_out: "joined a run last lockout but wasn't rostered",
  signup_order: "the earlier someone joined, the more it counts",
  regular: "joined with the character they're a regular of this schedule with; officers name the regulars on the Rosters page",
};

/** Who gets a seat: the weights as bars, highest first; a bar's length is its weight against the highest. */
export function SeatWeights({ r, keys }: { r: RaidRule; keys: string[] }) {
  const rows = keys.map((k) => ({ k, w: Number(r.weights[k] ?? 0), changed: r.overridden.includes(`weight_${k}`) })).sort((a, b) => b.w - a.w);
  const top = Math.max(1, ...rows.map((x) => x.w));
  return (
    <>
      {rows.map((x) => (
        <Tooltip key={x.k} label={WEIGHT_HINT[x.k] || x.k} position="top-start">
          <div className={css.bar}>
            <span>{WEIGHT_WORDS[x.k] || x.k.replace(/_/g, " ")}</span>
            <span className={css.track}><span className={[css.fill, x.changed ? css.changedFill : ""].join(" ")} style={{ width: `${Math.max(0, Math.min(100, (x.w / top) * 100))}%` }} /></span>
            <span className={[css.num, x.changed ? css.changedNum : ""].join(" ")}>{x.w}</span>
          </div>
        </Tooltip>
      ))}
    </>
  );
}

export { css as raidCardCss };
