import type { Fact } from "../../api";
import { Facts, ViewSection } from "../Facts";

/** An event's Details section: the read model's facts as they come (event_view — Lineup, Comes from, Priority,
 *  Confirmation, Sheet opened / opens, Locks, Signup sheet; history's Rostered and Ended), linked where the server says. */
export function EventDetails({ details }: { details: Fact[] }) {
  return (
    <ViewSection title="Details">
      <Facts items={details} />
    </ViewSection>
  );
}
