import { Table, Text } from "@mantine/core";
import type { Meta, ReadinessView as View } from "../../api";
import { Facts, ViewSection } from "../Facts";
import { LevelChip } from "../LevelChip";
import css from "../../pages/listpage.module.css";

/** The Raids page's Readiness view (design §7a; it was a page of its own): who could play — the standing as the level
 *  chip, mains for the raid's seats, alts, who could switch role — then the make-up's minimums against how many mains
 *  the guild has for each. ONE answer to "what are the minimums": the same rows as the Rules view (Registry.minimums),
 *  printed as the server gives them; Have is red when it is below the minimum. */
export function ReadinessView({ meta, view }: { meta: Meta; view: View | null }) {
  if (!view) return <ViewSection title="Who could play"><Text size="sm" c="dimmed">Loading…</Text></ViewSection>;
  return (
    <>
      <ViewSection title="Who could play">
        <Facts items={view.who_could_play} show={(f) => (f.level ? <LevelChip meta={meta} level={f.level} /> : undefined)} />
      </ViewSection>
      <ViewSection title="Against the make-up">
        {!view.make_up.length ? <Text size="sm" c="dimmed">{view.make_up_empty}</Text> : (
          <div className={css.scroll}>
            <Table maw={520} verticalSpacing={6}>
              <Table.Thead><Table.Tr><Table.Th>In each roster</Table.Th><Table.Th w={90}>Have</Table.Th><Table.Th w={90}>Minimum</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {view.make_up.map((m) => (
                  <Table.Tr key={m.slot}>
                    <Table.Td>{m.label}</Table.Td>
                    <Table.Td><Text span fw={700} c={m.have < m.min ? "red" : "teal"} style={{ fontVariantNumeric: "tabular-nums" }}>{m.have}</Text></Table.Td>
                    <Table.Td style={{ fontVariantNumeric: "tabular-nums" }}>{m.min}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
        )}
      </ViewSection>
    </>
  );
}
