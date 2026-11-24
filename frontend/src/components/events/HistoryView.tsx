import type { EventView } from "../../api";
import { EventDetails } from "./EventDetails";
import { ActivitySection } from "./ActivitySection";
import { EventHead } from "./shared";
import css from "../../pages/listpage.module.css";

/** A finished or cancelled event: its start and state, Details (Lineup, Comes from, Rostered, Ended) and Activity —
 *  read only; nothing on it can be pressed. (Its board as it was locked joins in a later step.) */
export function HistoryView({ ev }: { ev: EventView }) {
  return (
    <section className={css.view} aria-label={ev.title}>
      <EventHead title={ev.title} chip={ev.chip} />
      <EventDetails details={ev.details} />
      <ActivitySection activity={ev.activity} />
    </section>
  );
}
