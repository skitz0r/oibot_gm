import type { EventView, Meta } from "../../api";
import { EventDetails } from "./EventDetails";
import { ActivitySection } from "./ActivitySection";
import { EventBoard } from "./EventBoard";
import { EventHead } from "./shared";
import css from "../../pages/listpage.module.css";

/** A finished or cancelled event: its start and state, Details (Lineup, Comes from, Rostered, Ended) and Activity —
 *  read only; nothing on it can be pressed. The board as it was locked sits between Details and Activity. */
export function HistoryView({ ev, meta }: { ev: EventView; meta: Meta }) {
  return (
    <section className={css.view} aria-label={ev.title}>
      <EventHead title={ev.title} chip={ev.chip} />
      <EventDetails details={ev.details} />
      <EventBoard meta={meta} view={ev} onReload={() => undefined} />
      <ActivitySection activity={ev.activity} />
    </section>
  );
}
