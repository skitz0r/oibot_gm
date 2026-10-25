import { Text, Tooltip } from "@mantine/core";
import type { RaidRule } from "../api";
import css from "./raidcard.module.css";

/** The raid card's pieces (the Raids page; mockup out/mockups/raidcard): a section heading with its one-line hint, a run's
 *  week as a timeline and the who-gets-a-seat bars. Read-only: the owner edits all of it under Edit rules. */

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

type Tick = { label: string; hours: number; changed: boolean; why: string };

/** A run's week, counted back from the raid: opens · reminder · locks · confirm by · the raid. A step the raid doesn't take
 *  (reminder off, no confirmations) has no tick — the sentence below says so. Amber = changed from the game's default. */
export function RunWeek({ r }: { r: RaidRule }) {
  const over = (...ks: string[]) => ks.some((k) => r.overridden.includes(k));
  const ticks: Tick[] = [
    { label: "opens", hours: r.signup_lead_hours, changed: over("signup_lead_hours"), why: "the sheet is posted and everyone can answer" },
    ...(r.nudge ? [{ label: "reminder", hours: r.nudge_hours_before, changed: over("nudge", "nudge_hours_before"), why: "one DM to the regulars who haven't answered (every main while the schedule has none)" }] : []),
    { label: "locks", hours: r.lock_hours_before, changed: over("lock_hours_before"), why: "the roster is decided" },
    ...(r.confirm !== false ? [{ label: "confirm by", hours: r.confirm_hours_before, changed: over("confirm", "confirm_hours_before"), why: "the rostered confirm or say they can't make it by then" }] : []),
  ].sort((a, b) => b.hours - a.hours);
  const labels = r.slot_labels || r.slots;
  // several schedules each set their own start: the raid's default times alone would leave the others out
  const many = (r.schedules || []).length > 1;
  const start = many || labels.length === 0 ? "each run's start" : labels.length === 1 ? labels[0] : `${labels.length} run times a week`;
  const startWhy = many ? "each schedule below sets its own start times" : labels.length > 1 ? labels.join(" · ") : "each run's start; the schedules below set it";
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
      <Text size="sm" c="dimmed">{weekSentence(r).map((p, i) => <span key={i}>{i > 0 && " · "}<Text span inherit c={p.changed ? "yellow" : undefined}>{p.text}</Text></span>)}</Text>
    </>
  );
}

function hoursBefore(h: number): string {
  return `${h} h before`;
}

/** What happens around the timeline, from the raid's switches — only what is true for it. */
function weekSentence(r: RaidRule): { text: string; changed: boolean }[] {
  const over = (...ks: string[]) => ks.some((k) => r.overridden.includes(k));
  return [
    { text: r.autofill ? "After the lock, the bot asks people to fill open seats (a role swap waits for an officer)" : "After the lock, an officer fills open seats (Fill seats on the Rosters page)", changed: over("autofill") },
    { text: `a fill ask gets ${r.fill_ask_hours} h to answer`, changed: over("fill_ask_hours") },
    { text: r.open_dm ? "the regulars (every main while the schedule has none) get a DM when the sheet opens" : "no DM when the sheet opens", changed: over("open_dm") },
    ...(r.nudge ? [] : [{ text: "no reminder before the lock", changed: over("nudge") }]),
    ...(r.confirm !== false ? [] : [{ text: "the rostered aren't asked to confirm", changed: over("confirm") }]),
  ];
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
