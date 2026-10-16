import { useEffect, useMemo, useState } from "react";
import { Badge, Box, Button, Card, Group, Modal, SegmentedControl, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { api, type ItemBrief, type ItemGrid, type Meta } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { GameIcon } from "../Icons";
import { useLive } from "../../hooks/useLive";

const CLEAR = "–";

/** Loot › Items (D4, D12): the untiered items — learned from drops by name, or on the tables without tiers — with when
 *  they were first seen, how often they dropped and how many awards they have; a row opens the tier grid. Saving tiers
 *  for an item that already has awards asks whether to re-weight them (default no; who got what never changes). */
export function Items({ meta }: { meta: Meta }) {
  const [data, setData] = useState<{ untiered: ItemBrief[]; tiered: ItemBrief[] } | null>(null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const load = () => api.get<{ untiered: ItemBrief[]; tiered: ItemBrief[] }>("/api/loot/items").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["items", "loot:"]);
  const match = (i: ItemBrief) => !q.trim() || i.name.toLowerCase().includes(q.trim().toLowerCase());
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Card>
        <CardHeader title={`Untiered (${data.untiered.length})`} hint="scored the same for everyone until tiered" action={<TextInput size="xs" placeholder="filter by name" value={q} onChange={(e) => setQ(e.currentTarget.value)} w={200} />} />
        <Table.ScrollContainer minWidth={600}>
          <Table highlightOnHover>
            <Table.Thead><Table.Tr><Table.Th>Item</Table.Th><Table.Th>Boss</Table.Th><Table.Th>First seen</Table.Th><Table.Th>Dropped</Table.Th><Table.Th>Awards</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.untiered.filter(match).length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">Nothing untiered.</Text></Table.Td></Table.Tr>}
              {data.untiered.filter(match).map((i) => (
                <Table.Tr key={i.key} style={{ cursor: "pointer" }} onClick={() => setOpen(i.key)}>
                  <Table.Td><Text size="sm" fw={600}>{i.name}</Text>{i.learned && <Badge size="xs" variant="outline" color="yellow">learned from a drop</Badge>}</Table.Td>
                  <Table.Td>{i.boss || "—"}</Table.Td><Table.Td><Text size="xs" c="dimmed">{i.first_seen_label || "—"}</Text></Table.Td><Table.Td>{i.times_dropped ?? "—"}</Table.Td><Table.Td>{i.awards ?? 0}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      <Card>
        <CardHeader title={`Tiered (${data.tiered.length})`} />
        <Group p="md" gap={6} wrap="wrap">
          {data.tiered.filter(match).slice(0, 200).map((i) => <Button key={i.key} size="compact-xs" variant="default" onClick={() => setOpen(i.key)}>{i.name}</Button>)}
          {data.tiered.length === 0 && <Text size="sm" c="dimmed">No item has tiers yet.</Text>}
        </Group>
      </Card>
      {open && <TierGrid meta={meta} itemKey={open} onClose={() => setOpen(null)} onSaved={load} />}
    </Stack>
  );
}

function TierGrid({ meta, itemKey, onClose, onSaved }: { meta: Meta; itemKey: string; onClose: () => void; onSaved: () => void }) {
  const [item, setItem] = useState<ItemGrid | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [reweightAsk, setReweightAsk] = useState<string | null>(null);  // D12: "re-weight past awards?" — Yes · No (default) · Back
  useEffect(() => { api.get<ItemGrid>(`/api/loot/items/${encodeURIComponent(itemKey)}`).then((g) => { setItem(g); setDraft({}); }).catch(fail); }, [itemKey]);
  const changed = useMemo(() => Object.keys(draft).length, [draft]);
  if (!item) return null;
  async function save(reweight?: boolean) {
    setBusy(true);
    try {
      const tiers = Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, v === CLEAR ? "-" : v]));
      const r = await api.post<{ message?: string; ask?: string; item?: ItemGrid }>(`/api/loot/items/${encodeURIComponent(itemKey)}/tiers`, reweight === undefined ? { tiers } : { tiers, reweight });
      if (r.ask) { setReweightAsk(r.ask); return; }
      ok(r.message || "saved");
      if (r.item) setItem(r.item);
      setDraft({});
      onSaved();
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const byClass = item.rows.reduce<Record<string, ItemGrid["rows"]>>((acc, r) => { (acc[r.cls] ||= []).push(r); return acc; }, {});
  return (
    <Modal opened onClose={onClose} title={`${item.name}${item.boss ? ` · ${item.boss}` : ""}`} size="xl">
      <Stack gap="sm">
        <Text size="xs" c="dimmed">One tier per spec (the guild's over the game profile's). Greyed rows can't equip it. {item.awards ? `${item.awards} award(s) so far.` : ""}</Text>
        {Object.keys(item.wide).length > 0 && <Text size="xs">Wider tiers: {Object.entries(item.wide).map(([k, v]) => `${k} ${v}`).join(" · ")}</Text>}
        <Table verticalSpacing={3}>
          <Table.Tbody>
            {Object.entries(byClass).map(([cls, rows]) => rows.map((r) => {
              const cur = draft[r.key] ?? r.own ?? r.tier ?? CLEAR;
              return (
                <Table.Tr key={r.key} style={{ opacity: r.equippable ? 1 : 0.35 }}>
                  <Table.Td w={40}><Tooltip label={`${r.spec} ${cls} · ${r.role}${r.why ? ` — ${r.why}` : ""}`}><Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="spec" id={r.key} size={24} title={`${r.spec} ${cls}`} /></Box></Tooltip></Table.Td>
                  <Table.Td>
                    <SegmentedControl size="xs" disabled={!r.equippable} value={cur} data={[...item.tiers, CLEAR]}
                      onChange={(v) => setDraft((d) => { const next = { ...d }; if (v === (r.own ?? r.tier ?? CLEAR)) delete next[r.key]; else next[r.key] = v; return next; })} />
                  </Table.Td>
                  <Table.Td><Text size="xs" c="dimmed">{r.profile && !r.own ? `profile ${r.profile}` : r.own ? "guild" : ""}</Text></Table.Td>
                </Table.Tr>
              );
            }))}
          </Table.Tbody>
        </Table>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Close</Button>
          <Button disabled={!changed} loading={busy} onClick={() => save()}>Save {changed || ""} tier{changed === 1 ? "" : "s"}</Button>
        </Group>
      </Stack>
      <Modal opened={!!reweightAsk} onClose={() => setReweightAsk(null)} title="Re-weight past awards?" centered>
        <Stack gap="md">
          <Text size="sm">{reweightAsk}</Text>
          <Group justify="flex-end" gap="sm">
            <Button variant="subtle" color="gray" onClick={() => setReweightAsk(null)}>Back</Button>
            <Button variant="default" onClick={() => { setReweightAsk(null); save(false); }}>No, keep their weight</Button>
            <Button onClick={() => { setReweightAsk(null); save(true); }}>Re-weight them</Button>
          </Group>
        </Stack>
      </Modal>
    </Modal>
  );
}
