import type { EventView, Meta } from "../../api";
import { EventDetails } from "./EventDetails";
import { ActivitySection } from "./ActivitySection";
import { EventBoard } from "./EventBoard";
import { EventHead } from "./shared";
import { EventMore } from "./EventMore";
import css from "../../pages/listpage.module.css";

/** A finished or cancelled event: its start and state, Details (Lineup, Comes from, Rostered, Ended) and Activity —
 *  read only, but for Reopen (in More) on an event ended by mistake within the hour, as the Rosters page's history drawer
 *  had it. The board as it was locked sits between Details and Activity. */
export function HistoryView({ ev, meta, onChanged }: { ev: EventView; meta: Meta; onChanged: () => void }) {
  return (
    <section className={css.view} aria-label={ev.title}>
      <EventHead title={ev.title} chip={ev.chip}><EventMore ev={ev} meta={meta} onChanged={onChanged} /></EventHead>
      <EventDetails details={ev.details} />
      <EventBoard meta={meta} view={ev} onReload={() => undefined} />
      <ActivitySection activity={ev.activity} />
    </section>
  );
}
