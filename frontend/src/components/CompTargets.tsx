import { useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Badge, Box, Button, Group, NumberInput, Select, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import type { CompSlot, CompTarget, Meta } from "../api";
import { GameIcon } from "./Icons";
import { useOpsSave } from "./ReviewModal";

/** A comp slot's icon alone — role, class or spec — with its words in the tooltip (the iconography rule). */
export function SlotIcon({ meta, slot, label, size = 22 }: { meta: Meta; slot: string; label?: string; size?: number }) {
  const kind = meta.roles.includes(slot) ? "role" : slot.includes(":") ? "spec" : "class";
  return (
    <Tooltip label={label || slot}>
      <Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind={kind} id={slot} size={size} title={label || slot} /></Box>
    </Tooltip>
  );
}

type Row = { slot: string; min: number | string; max: number | string; note: string };

/** The "Desired comp" table: the officers' TARGETS for one raid ("we aim for" — what readiness measures; design §3.5
 *  RD-12), slot icon · at least · at most · why. View mode lists them; Edit targets turns the table into inputs with one
 *  Save: typed `comp` changes on the structured path (only the slots that differ; a removed row = null) with the raid's
 *  section rev, so someone else's change meanwhile is a 409, not an overwrite. The owner's BOUNDS (what the solver seats)
 *  are shown beside, read-only here — a target outside them is refused by the server with the owner pointer. */
export function CompTargets({ meta, rid, size, targets, bounds, slots, rev, editable, onSaved, onEditing }: {
  meta: Meta; rid: string; size: number; targets: Record<string, CompTarget>; bounds?: Record<string, { min: number; max: number }>;
  slots: CompSlot[] | null; rev?: string; editable: boolean; onSaved: () => void; onEditing?: (on: boolean) => void;
}) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [save, review] = useOpsSave();
  useDirty(`comp-targets:${rid}`, rows !== null);  // the guild switcher asks before dropping the draft
  const words = (slot: string) => slots?.find((x) => x.value === slot)?.label || slot;
  const toRows = (): Row[] => Object.entries(targets).map(([slot, t]) => ({ slot, min: t.min ?? 0, max: t.max ?? "", note: t.note || "" }));
  const start = () => { setRows(toRows()); onEditing?.(true); };
  const stop = () => { setRows(null); setAdding(null); onEditing?.(false); };

  /** the draft as one `comp` change: changed or new slots as whole entries, removed slots null — nothing else */
  function fields(): Record<string, CompTarget | null> {
    const out: Record<string, CompTarget | null> = {};
    const want = new Map((rows || []).map((r) => [r.slot, r]));
    for (const [slot, r] of want) {
      const entry: CompTarget = { min: Number(r.min || 0), max: r.max === "" || r.max === null ? null : Number(r.max), note: r.note.trim() || null };
      const have = targets[slot];
      if (!have || have.min !== entry.min || (have.max ?? null) !== entry.max || (have.note || null) !== entry.note) out[slot] = entry;
    }
    for (const slot of Object.keys(targets)) if (!want.has(slot)) out[slot] = null;
    return out;
  }

  async function submit() {
    const f = fields();
    if (!Object.keys(f).length) { stop(); return; }
    setBusy(true);
    try {
      const r = await save([{ kind: "comp", target: rid, fields: f }], { revs: rev ? { [`config:raids:${rid}`]: rev } : undefined, title: "Save the desired comp?" });
      if (r === null) return;  // Back to edits: the draft stays
      if (r.changed?.length) return;  // someone changed this raid meanwhile: the toast said so; the draft stays for a reload
      if (!r.refused?.length) { stop(); onSaved(); }
    } finally { setBusy(false); }
  }

  const b = bounds || {};
  const boundsLine = ["tank", "healer", "dps"].filter((role) => b[role]).map((role) => (
    <Group key={role} gap={4} wrap="nowrap">{role === "dps" ? <Text size="xs" fw={700}>DPS</Text> : <GameIcon meta={meta} kind="role" id={role} size={16} title={role} />}<Text size="xs" style={{ fontVariantNumeric: "tabular-nums" }}>{b[role].min}–{b[role].max}</Text></Group>
  ));
  const taken = new Set((rows || []).map((r) => r.slot));
  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Group gap="xs" wrap="wrap">
          <Text size="xs" c="dimmed">Bounds (the solver; the owner's):</Text>{boundsLine}
        </Group>
        {editable && !rows && <Button size="xs" variant="default" leftSection={<IconPencil size={13} />} onClick={start} disabled={!slots}>Edit targets</Button>}
      </Group>
      {!rows ? (
        Object.keys(targets).length === 0 ? <Text size="sm" c="dimmed">No officer targets: readiness uses the derived ones (the bounds and the buff matrix).</Text> : (
          <Table.ScrollContainer minWidth={420}>
            <Table verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th w={50} /><Table.Th w={90}>At least</Table.Th><Table.Th w={90}>At most</Table.Th><Table.Th>Why</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {Object.entries(targets).map(([slot, t]) => (
                  <Table.Tr key={slot}>
                    <Table.Td><SlotIcon meta={meta} slot={slot} label={words(slot)} /></Table.Td>
                    <Table.Td style={{ fontVariantNumeric: "tabular-nums" }}>{t.min ?? 0}</Table.Td>
                    <Table.Td style={{ fontVariantNumeric: "tabular-nums" }}>{t.max ?? "—"}</Table.Td>
                    <Table.Td>{t.note ? <Text size="sm">{t.note}</Text> : <Badge size="xs" color="yellow" variant="light">no reason given</Badge>}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )
      ) : (
        <Stack gap="xs">
          <Table.ScrollContainer minWidth={520}>
            <Table verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th w={50} /><Table.Th w={110}>At least</Table.Th><Table.Th w={110}>At most</Table.Th><Table.Th>Why</Table.Th><Table.Th w={40} /></Table.Tr></Table.Thead>
              <Table.Tbody>
                {rows.map((r, i) => (
                  <Table.Tr key={r.slot}>
                    <Table.Td><SlotIcon meta={meta} slot={r.slot} label={words(r.slot)} /></Table.Td>
                    <Table.Td><NumberInput size="xs" min={0} max={size} value={r.min} aria-label={`${words(r.slot)}: at least`} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, min: v } : x)))} /></Table.Td>
                    <Table.Td><NumberInput size="xs" min={0} max={size} value={r.max} placeholder="no limit" aria-label={`${words(r.slot)}: at most`} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, max: v } : x)))} /></Table.Td>
                    <Table.Td><TextInput size="xs" value={r.note} maxLength={200} placeholder="why — shown on the readiness card" aria-label={`${words(r.slot)}: why`} onChange={(e) => { const v = e.currentTarget.value; setRows(rows.map((x, j) => (j === i ? { ...x, note: v } : x))); }} /></Table.Td>
                    <Table.Td><ActionIcon variant="subtle" color="red" aria-label={`remove the ${words(r.slot)} target`} onClick={() => setRows(rows.filter((_, j) => j !== i))}><IconTrash size={14} /></ActionIcon></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          <Group gap="xs" wrap="wrap">
            <Select size="xs" w={240} searchable placeholder="Add a target for…" value={adding} onChange={setAdding} nothingFoundMessage="no such slot"
              data={(slots || []).filter((x) => !taken.has(x.value)).map((x) => ({ value: x.value, label: x.label }))} />
            <Button size="xs" variant="default" leftSection={<IconPlus size={13} />} disabled={!adding} onClick={() => { if (adding) { setRows([...rows, { slot: adding, min: 1, max: "", note: "" }]); setAdding(null); } }}>Add</Button>
          </Group>
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={stop} disabled={busy}>Cancel</Button>
            <Button size="xs" loading={busy} onClick={submit}>Save targets</Button>
          </Group>
        </Stack>
      )}
      {review}
    </Stack>
  );
}
