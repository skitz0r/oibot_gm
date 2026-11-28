import type { EventView, Meta } from "../../api";
import { EventDetails } from "./EventDetails";
import { ActivitySection } from "./ActivitySection";
import { EventBoard } from "./EventBoard";
import { EventHead } from "./shared";
import { EventMore } from "./EventMore";
import { EventTabs } from "./BotActivity";
import css from "../../pages/listpage.module.css";

/** A finished or cancelled event: its start and state, Details (Lineup, Schedule, Rostered, Ended) and Activity —
 *  read only, but for Reopen (in More) on an event ended by mistake within the hour, as the old history drawer
 *  had it. The board as it was locked sits between Details and Activity. Its Bot activity tab is the DMs' history, read
 *  only. */
export function HistoryView({ ev, meta, onChanged }: { ev: EventView; meta: Meta; onChanged: () => void }) {
  return (
    <section className={css.view} aria-label={ev.title}>
      <EventHead title={ev.title} chip={ev.chip} tag={ev.test_chip}><EventMore ev={ev} meta={meta} onChanged={onChanged} /></EventHead>
      <EventTabs ev={ev} meta={meta} onChanged={onChanged}>
        <EventDetails details={ev.details} />
        <EventBoard meta={meta} view={ev} onReload={() => undefined} />
        <ActivitySection activity={ev.activity} />
      </EventTabs>
    </section>
  );
}
