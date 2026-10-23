import { useEffect, useRef, useState } from "react";
import { Alert, Badge, Box, Button, Card, Group, NumberInput, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { api } from "../api";
import { CardHeader, fail } from "./Page";
import { useOpsSave } from "./ReviewModal";
import { useDirty } from "../hooks/useDirty";
import { useLive } from "../hooks/useLive";

/** GET /api/loot/weights: every weight with its value now and the game profile's default beside it. */
interface WeightRow { path: string; label: string; group: string; value: number; default: number; overridden: boolean; whole: boolean }
interface Weights { rows: WeightRow[]; groups: { key: string; title: string; hint: string }[]; rev: string; owner: boolean; note: string }
/** POST /api/loot/rescore: the last council's drops scored by the numbers alone, now vs the draft. */
interface Pick { character: string; score: number }
interface RescoreRow { drop_id: string; item: string; boss: string | null; awarded: string | null; old: Pick[]; new: Pick[]; old_pick: string | null; new_pick: string | null; moved: boolean }
interface Rescore { run: { label: string; day_label: string; roster: number } | null; rows: RescoreRow[]; moved: number; drops: number; message: string }

/** Config › Loot scoring (L06, design §5.48; the OWNER edits, officers read): each weight with the profile default beside
 *  it. Edit mode holds a draft; Save goes through the structured path (useOpsSave → the ReviewModal — weights are warn
 *  class, so every Save is reviewed, each number current → new). "Re-score last run" previews what the draft would have
 *  done to the newest council's picks — the numbers alone, no model, nothing written. No ledger row ever changes. */
export function LootWeightsCard() {
  const [data, setData] = useState<Weights | null>(null);
  const [draft, setDraft] = useState<Record<string, number | null> | null>(null);  // path → number (null = back to the default)
  const [stale, setStale] = useState(false);
  const [rescore, setRescore] = useState<Rescore | null>(null);
  const [busy, setBusy] = useState(false);
  const [save, review] = useOpsSave();
  useDirty("loot-weights", draft !== null);
  const editingRef = useRef(false);
  editingRef.current = draft !== null;
  const load = () => api.get<Weights>("/api/loot/weights").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  // a change made elsewhere (plain text, MCP) shows at once — never under an open draft: the draft is told instead
  useLive(() => { if (editingRef.current) setStale(true); else load(); }, ["config"]);
  if (!data) return null;
  const changed = (): Record<string, number | null> => {
    if (!draft) return {};
    const out: Record<string, number | null> = {};
    for (const r of data.rows) {
      if (!(r.path in draft)) continue;
      const v = draft[r.path];
      if (v === null ? r.overridden : v !== r.value) out[r.path] = v;
    }
    return out;
  };
  const diff = changed();
  async function doSave() {
    setBusy(true);
    try {
      const r = await save([{ kind: "loot_weights", fields: diff }], { revs: { "config:loot": data!.rev }, title: "Review the loot weights" });
      if (r && r.changed?.length) { setStale(true); return; }
      if (r && r.applied?.length) { setDraft(null); setStale(false); setRescore(null); await load(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function runRescore() {
    setBusy(true);
    try { setRescore(await api.post<Rescore>("/api/loot/rescore", { fields: diff })); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const shown = (r: WeightRow) => (draft && r.path in draft ? (draft[r.path] === null ? r.default : draft[r.path]) : r.value);
  return (
    <Card>
      <CardHeader title="Loot scoring" hint={data.owner ? "the weights behind every candidate's score" : "read-only — the owner edits"} action={
        <Group gap="xs">
          <Button size="xs" variant="default" loading={busy && !draft} onClick={runRescore}>Re-score last run</Button>
          {data.owner && !draft && <Button size="xs" variant="default" leftSection={<IconPencil size={14} />} onClick={() => { setDraft({}); setRescore(null); }}>Edit</Button>}
        </Group>} />
      <Stack p="md" gap="md">
        <Text size="xs" c="dimmed">{data.note}</Text>
        {stale && (
          <Alert color="yellow" py="xs">
            <Group justify="space-between" wrap="wrap"><Text size="sm">Someone changed the weights while you were editing.</Text>
              <Button size="xs" variant="default" onClick={async () => { setStale(false); await load(); }}>Reload — keep my edits</Button></Group>
          </Alert>
        )}
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
          {data.groups.map((g) => {
            const rows = data.rows.filter((r) => r.group === g.key);
            if (!rows.length) return null;
            return (
              <Box key={g.key}>
                <Text size="sm" fw={600}>{g.title}</Text>
                <Text size="xs" c="dimmed" mb={4}>{g.hint}</Text>
                <Table verticalSpacing={2}>
                  <Table.Tbody>
                    {rows.map((r) => (
                      <Table.Tr key={r.path}>
                        <Table.Td><Text size="sm">{r.label}</Text></Table.Td>
                        <Table.Td w={130}>
                          {draft ? (
                            <NumberInput size="xs" value={shown(r) ?? ""} min={r.whole ? 1 : 0} max={r.whole ? 365 : 100} step={r.whole ? 1 : 0.05} decimalScale={r.whole ? 0 : 4}
                              allowDecimal={!r.whole} aria-label={r.label}
                              onChange={(v) => setDraft((d) => ({ ...(d || {}), [r.path]: v === "" ? null : Number(v) }))} />
                          ) : <Text size="sm" fw={r.overridden ? 600 : undefined}>{r.value}</Text>}
                        </Table.Td>
                        <Table.Td w={120}>
                          <Group gap={4} wrap="nowrap">
                            <Text size="xs" c="dimmed">default {r.default}</Text>
                            {draft && shown(r) !== r.default && <Button size="compact-xs" variant="subtle" onClick={() => setDraft((d) => ({ ...(d || {}), [r.path]: null }))}>reset</Button>}
                            {!draft && r.overridden && <Badge size="xs" variant="light">set</Badge>}
                          </Group>
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Box>
            );
          })}
        </SimpleGrid>
        {draft && (
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={() => { setDraft(null); setStale(false); setRescore(null); }} disabled={busy}>Cancel</Button>
            <Button variant="default" onClick={runRescore} loading={busy} disabled={!Object.keys(diff).length}>Re-score last run with these</Button>
            <Button onClick={doSave} loading={busy} disabled={!Object.keys(diff).length}>Save {Object.keys(diff).length || ""} change{Object.keys(diff).length === 1 ? "" : "s"}</Button>
          </Group>
        )}
        {rescore && (
          <Stack gap={4}>
            <Text size="sm" fw={600}>{rescore.message}</Text>
            {rescore.rows.length > 0 && (
              <Table.ScrollContainer minWidth={620}>
                <Table verticalSpacing={2}>
                  <Table.Thead><Table.Tr><Table.Th>Item</Table.Th><Table.Th>Went to</Table.Th><Table.Th>Top now</Table.Th><Table.Th>{Object.keys(diff).length ? "Top with the draft" : "—"}</Table.Th><Table.Th /></Table.Tr></Table.Thead>
                  <Table.Tbody>
                    {rescore.rows.map((r) => (
                      <Table.Tr key={r.drop_id || r.item}>
                        <Table.Td><Text size="sm">{r.item}</Text>{r.boss && <Text size="xs" c="dimmed">{r.boss}</Text>}</Table.Td>
                        <Table.Td><Text size="xs">{r.awarded || "—"}</Text></Table.Td>
                        <Table.Td><Text size="xs">{r.old.map((p) => `${p.character} ${p.score}`).join(" · ") || "—"}</Text></Table.Td>
                        <Table.Td><Text size="xs">{Object.keys(diff).length ? (r.new.map((p) => `${p.character} ${p.score}`).join(" · ") || "—") : ""}</Text></Table.Td>
                        <Table.Td>{r.moved && <Badge size="xs" color="yellow" variant="light">top pick changes</Badge>}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            )}
            <Text size="xs" c="dimmed">Each drop scored against the loot recorded before that night — a comparison of weights, not a replay; nothing is saved.</Text>
          </Stack>
        )}
      </Stack>
      {review}
    </Card>
  );
}
