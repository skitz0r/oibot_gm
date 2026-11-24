import { Anchor, Table, Text } from "@mantine/core";
import { Link } from "react-router-dom";
import type { ScheduleView } from "../../api";
import { Facts, ViewSection } from "../Facts";
import { guardClick } from "../LeaveGuard";
import css from "../../pages/listpage.module.css";

/** One schedule's sections under its header (Schedules.dc.html): Details, Lineup (the builder's picture), Sheet timing
 *  and Next events (`lineup` = the page's LineupPicture of it) — every value the server's words (schedule_view.schedule_json). A night of Next events links its
 *  event when it has one, else its coming-up page on the Events page (Open the sheet now lives there); a paused
 *  schedule or a pickup template has no rows and the server's sentence says why; one whose raid left the game has no
 *  Next events at all (the page's header says why, once). */
export function ScheduleDetails({ view, lineup }: { view: ScheduleView; lineup: React.ReactNode }) {
  const night = (n: ScheduleView["next"][number]) => n.key
    ? `/events/${encodeURIComponent(n.key)}`
    : `/events/coming/${encodeURIComponent(view.id)}/${encodeURIComponent(n.occurrence)}`;
  return (
    <>
      <ViewSection title="Details"><Facts items={view.details} /></ViewSection>
      <ViewSection title="Lineup">{lineup}</ViewSection>
      <ViewSection title="Sheet timing"><Facts items={view.timing} /></ViewSection>
      {!view.gone && <ViewSection title="Next events">
        {view.next.length === 0 ? <Text size="sm" c="dimmed">{view.next_empty}</Text> : (
          <div className={css.scroll}>
            <Table verticalSpacing={8} horizontalSpacing={8}>
              <Table.Thead>
                <Table.Tr><Table.Th>Starts</Table.Th><Table.Th>Sheet opens</Table.Th><Table.Th>Locks</Table.Th></Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {view.next.map((n) => (
                  <Table.Tr key={n.occurrence}>
                    <Table.Td fw={700}><Anchor component={Link} to={night(n)} onClick={guardClick(night(n))} fw={700}>{n.starts}</Anchor></Table.Td>
                    <Table.Td>{n.opens}</Table.Td>
                    <Table.Td>{n.locks}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
        )}
      </ViewSection>}
    </>
  );
}
