import { Group, Select, Table, Text, Tooltip } from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import type { CompSlot, RaidPage } from "../../api";
import { Facts, ViewSection } from "../Facts";
import css from "../../pages/listpage.module.css";

/** One minimum in the draft: the slot, its words, at least how many (a select's string), the stored reason kept. */
export type MinRow = { slot: string; label: string; min: string; note: string | null };
/** The raid's edit draft (Rules and Priority share it; one Save writes both). Selects hold strings. */
export type RaidDraft = { lockout_days: string; duration_hours: string; first_open: string; weights: Record<string, string>; mins: MinRow[] };

const LOCKOUT_DAYS = Array.from({ length: 14 }, (_, i) => String(i + 1));
const HALF_HOURS = Array.from({ length: 16 }, (_, i) => String((i + 1) / 2));

/** The draft keeps the API's machine form ('2026-12-09T15:00', guild time); the picker speaks 'YYYY-MM-DD HH:mm:ss'. */
function pickerValue(iso: string): string | null {
  return iso ? `${iso.slice(0, 16).replace("T", " ")}:00` : null;
}
function fromPicker(v: string | null, keep: string): string {
  return v ? v.slice(0, 16).replace(" ", "T") : keep;
}

/** The Raids page's Rules view (design §7a): Details (the server's facts — Size, Groups, Lockout, Length, First opens,
 *  Played in) and Group make-up (In each roster · Minimum: Registry.minimums, the one answer). In the edit mode the
 *  Lockout, the Length (half hours) and First opens become pickers, each minimum a select from 0 to the raid's size
 *  (0 takes the guild's own minimum away: it is sent as a clear, never saved), and Add a role or class adds a row.
 *  The size is shown, never edited (the game's). No other time than First opens is a raid's. */
export function RulesView({ page, size, draft, setDraft, slots }: {
  page: RaidPage; size: number; draft: RaidDraft | null; setDraft: (d: RaidDraft) => void; slots: CompSlot[] | null;
}) {
  const sizes = Array.from({ length: size + 1 }, (_, i) => String(i));
  const editValue = draft ? (label: string) => {
    if (label === "Lockout") return <Select aria-label="Lockout in days" w={120} data={LOCKOUT_DAYS} value={draft.lockout_days} allowDeselect={false} onChange={(v) => v && setDraft({ ...draft, lockout_days: v })} rightSection={<Text size="xs" c="dimmed">days</Text>} />;
    if (label === "Length") return <Select aria-label="Length in hours" w={120} data={HALF_HOURS} value={draft.duration_hours} allowDeselect={false} onChange={(v) => v && setDraft({ ...draft, duration_hours: v })} rightSection={<Text size="xs" c="dimmed">h</Text>} />;
    if (label === "First opens") return (
      <DateTimePicker aria-label="First opens" value={pickerValue(draft.first_open)} onChange={(v) => setDraft({ ...draft, first_open: fromPicker(v, draft.first_open) })}
        valueFormat="ddd D MMM YYYY h:mm A" timePickerProps={{ format: "12h", withDropdown: true, popoverProps: { withinPortal: false } }} popoverProps={{ withinPortal: true }} w={230} />
    );
    return undefined;
  } : null;
  const taken = new Set((draft?.mins || []).map((m) => m.slot));
  const setMin = (i: number, v: string) => draft && setDraft({ ...draft, mins: draft.mins.map((m, j) => (j === i ? { ...m, min: v } : m)) });
  return (
    <>
      <ViewSection title="Details">
        <Facts items={page.details} show={editValue ? (f) => editValue(f.label) : undefined} />
      </ViewSection>
      <ViewSection title="Group make-up">
        {!draft && !page.make_up.length && <Text size="sm" c="dimmed">{page.make_up_empty}</Text>}
        {(draft ? draft.mins.length > 0 : page.make_up.length > 0) && (
          <div className={css.scroll}>
            <Table maw={520} verticalSpacing={6}>
              <Table.Thead><Table.Tr><Table.Th>In each roster</Table.Th><Table.Th w={140}>Minimum</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {draft ? draft.mins.map((m, i) => (
                  <Table.Tr key={m.slot}>
                    <Table.Td>{m.label}</Table.Td>
                    <Table.Td><Select aria-label={`${m.label}: minimum`} w={110} data={sizes} value={m.min} allowDeselect={false} onChange={(v) => v !== null && setMin(i, v)} /></Table.Td>
                  </Table.Tr>
                )) : page.make_up.map((m) => (
                  <Table.Tr key={m.slot}>
                    <Table.Td>{m.label}</Table.Td>
                    <Table.Td>{m.note ? <Tooltip label={m.note}><Text span fw={700}>{m.min}</Text></Tooltip> : <Text span fw={700}>{m.min}</Text>}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
        )}
        {draft && (
          <Group mt="sm" gap="xs" wrap="wrap">
            <Select w={260} searchable aria-label="Add a role or class" placeholder="Add a role or class" value={null} nothingFoundMessage="No role, class or spec by that name"
              data={(slots || []).filter((x) => !taken.has(x.value)).map((x) => ({ value: x.value, label: x.label }))}
              onChange={(v) => {
                const s = (slots || []).find((x) => x.value === v);
                if (s) setDraft({ ...draft, mins: [...draft.mins, { slot: s.value, label: s.label, min: "1", note: null }] });
              }} />
          </Group>
        )}
      </ViewSection>
    </>
  );
}
