import { useCallback, useEffect, useState } from "react";
import { Alert, Badge, Button, FileButton, Group, Modal, Select, Stack, Table, Tabs, Text, Textarea } from "@mantine/core";
import { api } from "../../api";
import { fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";

interface Row { line: number; character: string; item: string; received: string; received_label: string; boss: string | null; offspec: boolean; known_item: boolean; known_character: boolean; tier?: string; weight?: number; why?: string }
/** POST /api/loot/import's preview (Registry.import_awards): the groups and the rev an import must carry. */
interface LedgerPreview {
  kind: string; kind_words: string; rows: number; new: number; duplicates: number; unknown_items: number; unknown_characters: string[]; line: string; rev: string;
  groups: { new: Row[]; duplicates: Row[]; unknown_items: { item: string; rows: number }[] }; errors: string[]; skipped: number;
}

/** GET /api/loot/imports (Registry.import_batches): one earlier import, newest first; `consequence` is the server's Undo sentence. */
interface Batch { batch: string; kind: string | null; at: string | null; at_label: string; by: string | null; rows: number; live: number; consequence: string }

const KINDS = [
  { value: "auto", label: "Tell from the file" },
  { value: "biscouncil", label: "BisCouncil loot summary" },
  { value: "tmb", label: "ThatsMyBIS export (received rows)" },
  { value: "generic", label: "Generic CSV" },
];
const GENERIC = "character,item,received,boss,ms_os,note";

/** Loot › Items › Import loot history (L07, design §5.48): a CSV from another tool — upload or paste — previewed as three
 *  groups (new rows · duplicates · unknown items, recorded by name) with the server's own counts, then Confirm appends exactly
 *  the new rows; an unknown or untiered item is learned onto Loot › Items › Untiered, where it is tiered like a learned drop. An import never edits or deletes a ledger row; a row the ledger already holds (same character, item and day)
 *  is a duplicate. Gargul / RCLootCouncil aren't read yet (which addon is still open): the generic columns are the fallback. */
export function LedgerImport({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [csv, setCsv] = useState("");
  const [kind, setKind] = useState<string>("auto");
  const [pv, setPv] = useState<LedgerPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [ask, dialog] = useConfirm();
  const loadBatches = useCallback(() => { api.get<{ batches: Batch[] }>("/api/loot/imports").then((r) => setBatches(r.batches)).catch(fail); }, []);
  useEffect(loadBatches, [loadBatches]);
  /** Undo this import: a void row per award it added (the council's correction path); the rows stay in the ledger. */
  async function undo(b: Batch) {
    const r = await ask({ title: "Undo this import?", message: b.consequence, confirmLabel: "Undo the import", color: "red",
      note: { label: "Why (kept on each void row)", maxLength: 200, placeholder: "the received column held the export date" } });
    if (!r) return;
    try {
      const out = await api.post<{ message: string }>("/api/loot/import/undo", { batch: b.batch, reason: typeof r === "object" ? r.note : "" });
      ok(out.message); loadBatches(); onDone();
    } catch (e) { fail(e); }
  }
  async function preview(text = csv, k = kind) {
    setBusy(true);
    try { setPv((await api.post<{ preview: LedgerPreview }>("/api/loot/import", { csv: text, kind: k })).preview); } catch (e) { fail(e); setPv(null); } finally { setBusy(false); }
  }
  async function confirm() {
    if (!pv) return;
    setBusy(true);
    try {
      const r = await api.post<{ message: string }>("/api/loot/import", { csv, kind, preview: false, rev: pv.rev });
      ok(r.message); onDone(); setPv(null); setCsv(""); loadBatches();
    } catch (e) { fail(e); preview(); } finally { setBusy(false); }
  }
  function pick(f: File | null) {
    if (!f) return;
    f.text().then((t) => { setCsv(t); setPv(null); preview(t); }).catch(fail);
  }
  const table = (rows: Row[], why: boolean) => (
    <Table.ScrollContainer minWidth={520}>
      <Table verticalSpacing={2}>
        <Table.Thead><Table.Tr><Table.Th>Line</Table.Th><Table.Th>Character</Table.Th><Table.Th>Item</Table.Th><Table.Th>Received</Table.Th><Table.Th>{why ? "Why" : "Weight"}</Table.Th></Table.Tr></Table.Thead>
        <Table.Tbody>
          {rows.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">{why ? "No duplicates — none of these awards is in the history already." : (pv?.duplicates ? "Nothing new — every award in this file is in the history already." : "Nothing to add — this file has no awards the bot could read.")}</Text></Table.Td></Table.Tr>}
          {rows.map((r) => (
            <Table.Tr key={`${r.line}-${r.item}`}>
              <Table.Td><Text size="xs" c="dimmed">{r.line}</Text></Table.Td>
              <Table.Td><Text size="sm">{r.character}</Text>{!r.known_character && <Text size="xs" c="dimmed">nobody registered — by name</Text>}</Table.Td>
              <Table.Td><Text size="sm">{r.item}</Text>{!r.known_item && <Text size="xs" c="dimmed">not on the tables — by name</Text>}</Table.Td>
              <Table.Td><Text size="xs">{r.received_label || r.received}{r.offspec ? " · OS" : ""}</Text></Table.Td>
              <Table.Td><Text size="xs" c="dimmed">{why ? r.why : `${r.tier ?? "?"} · ${r.weight ?? ""}`}</Text></Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
  return (
    <Modal opened onClose={onClose} title="Import loot history" size="xl">
      {dialog}
      <Stack gap="sm">
        <Text size="xs" c="dimmed">A BisCouncil loot summary, a ThatsMyBIS export (its received rows), or a generic CSV with the columns <b>{GENERIC}</b> (item = an id or the item's name; received = the day, 2026-12-09). Gargul and RCLootCouncil exports aren't read yet — export to the generic columns. Nothing is written before Confirm, and an import only adds rows.</Text>
        <Group gap="xs" wrap="wrap">
          <Select size="xs" w={260} data={KINDS} value={kind} allowDeselect={false} onChange={(v) => { const k = v || "auto"; setKind(k); if (csv.trim()) preview(csv, k); }} />
          <FileButton onChange={pick} accept=".csv,text/csv">{(props) => <Button size="xs" variant="default" {...props}>Pick a file…</Button>}</FileButton>
          <Text size="xs" c="dimmed">or paste it below</Text>
        </Group>
        <Textarea autosize minRows={3} maxRows={8} value={csv} onChange={(e) => { setCsv(e.currentTarget.value); setPv(null); }} placeholder={GENERIC} />
        {pv && (
          <Stack gap={6}>
            <Alert color={pv.new ? "teal" : "gray"} py="xs"><Text size="sm" fw={600}>{pv.line}</Text><Text size="xs" c="dimmed">Read as a {pv.kind_words}{pv.skipped ? ` · ${pv.skipped} row(s) that aren't awards skipped` : ""}.</Text></Alert>
            {pv.errors.map((x) => <Text key={x} size="xs" c="red">{x}</Text>)}
            <Tabs defaultValue="new" keepMounted={false}>
              <Tabs.List>
                <Tabs.Tab value="new" rightSection={<Badge size="xs" variant="light">{pv.new}</Badge>}>New</Tabs.Tab>
                <Tabs.Tab value="dup" rightSection={<Badge size="xs" variant="light" color="gray">{pv.duplicates}</Badge>}>Duplicates</Tabs.Tab>
                <Tabs.Tab value="unknown" rightSection={<Badge size="xs" variant="light" color="yellow">{pv.unknown_items}</Badge>}>Unknown items</Tabs.Tab>
              </Tabs.List>
              <Tabs.Panel value="new" pt="xs">{table(pv.groups.new, false)}</Tabs.Panel>
              <Tabs.Panel value="dup" pt="xs">{table(pv.groups.duplicates, true)}</Tabs.Panel>
              <Tabs.Panel value="unknown" pt="xs">
                <Stack gap={2}>
                  {pv.groups.unknown_items.length === 0 && <Text size="sm" c="dimmed">None — every item is on the tables.</Text>}
                  {pv.groups.unknown_items.map((u) => <Text key={u.item} size="sm">{u.item} <Text span size="xs" c="dimmed">· {u.rows} row(s), recorded by name — after Confirm it is listed under Untiered on this page; tier it there (its awards can be re-weighted)</Text></Text>)}
                </Stack>
              </Tabs.Panel>
            </Tabs>
          </Stack>
        )}
        {batches.length > 0 && (
          <Stack gap={4}>
            <Text size="sm" fw={600}>Earlier imports</Text>
            {batches.map((b) => (
              <Group key={b.batch} gap="xs" justify="space-between" wrap="nowrap">
                <Text size="xs">{b.at_label} · {b.kind ?? "file"} · {b.by ?? "?"} · {b.live === b.rows ? `${b.rows} row(s)` : `${b.live} of ${b.rows} row(s) live`}</Text>
                <Button size="compact-xs" variant="subtle" color="red" disabled={b.live === 0} onClick={() => undo(b)}>{b.live === 0 ? "Undone" : "Undo this import"}</Button>
              </Group>
            ))}
          </Stack>
        )}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Close</Button>
          <Button variant="default" disabled={!csv.trim()} loading={busy && !pv} onClick={() => preview()}>Preview</Button>
          <Button disabled={!pv || pv.new === 0} loading={busy && !!pv} onClick={confirm}>Confirm — import {pv?.new ?? 0} row{pv?.new === 1 ? "" : "s"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
