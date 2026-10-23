import { useEffect, useMemo, useState } from "react";
import { useDirty } from "../../hooks/useDirty";
import { Alert, Badge, Box, Button, Card, FileButton, Group, Modal, SegmentedControl, Stack, Table, Text, TextInput, Textarea, Tooltip } from "@mantine/core";
import { api, guildQuery, type ItemBrief, type ItemGrid, type Meta } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { GameIcon } from "../Icons";
import { useLive } from "../../hooks/useLive";
import { LedgerImport } from "./LedgerImport";

const CLEAR = "–";

/** Claude's proposal for one item's tiers (POST /api/loot/items/{key}/draft): nothing is saved until an officer saves. */
interface TierDraft { tiers: Record<string, string>; why: Record<string, string>; dropped: string[]; summary: string; warnings: string[]; provenance: string; by?: string; at_label?: string }
type Grid = ItemGrid & { draft?: TierDraft | null };
/** The CSV import's preview (POST /api/loot/items/import, preview). */
interface ImportPreview {
  items: { key: string; name: string; changes: Record<string, [string | null, string | null]>; awards: number }[];
  cells: number; unchanged: number; duplicates: string[]; unknown: string[]; errors: string[]; rows: number; rev: string; with_awards: number;
}

/** Loot › Items (D4, D12): the untiered items — learned from drops by name, or on the tables without tiers — with when
 *  they were first seen, how often they dropped and how many awards they have; a row opens the tier grid. Saving tiers
 *  for an item that already has awards asks whether to re-weight them (default no; who got what never changes).
 *  Phase 3: "Draft with Claude" on the grid (a proposal, saved only by the officer's Save) and the CSV export / import
 *  of the whole grid (preview first). Phase 4: Import loot history (LedgerImport — BisCouncil, ThatsMyBIS, generic CSV). */
export function Items({ meta }: { meta: Meta }) {
  const [data, setData] = useState<{ untiered: ItemBrief[]; tiered: ItemBrief[] } | null>(null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [history, setHistory] = useState(false);
  const load = () => api.get<{ untiered: ItemBrief[]; tiered: ItemBrief[] }>("/api/loot/items").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["items", "loot:"]);
  const match = (i: ItemBrief) => !q.trim() || i.name.toLowerCase().includes(q.trim().toLowerCase());
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Card>
        <CardHeader title={`Untiered (${data.untiered.length})`} hint="scored the same for everyone until tiered" action={
          <Group gap="xs">
            <TextInput size="xs" placeholder="filter by name" value={q} onChange={(e) => setQ(e.currentTarget.value)} w={180} />
            <Button size="xs" variant="default" component="a" href={`/api/loot/items/export${guildQuery()}`} download>Export CSV</Button>
            <Button size="xs" variant="default" onClick={() => setImporting(true)}>Import CSV…</Button>
            <Button size="xs" variant="default" onClick={() => setHistory(true)}>Import loot history…</Button>
          </Group>} />
        <Table.ScrollContainer minWidth={600}>
          <Table highlightOnHover>
            <Table.Thead><Table.Tr><Table.Th>Item</Table.Th><Table.Th>Boss</Table.Th><Table.Th>First seen</Table.Th><Table.Th>Dropped</Table.Th><Table.Th>Awards</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.untiered.filter(match).length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">Nothing untiered.</Text></Table.Td></Table.Tr>}
              {data.untiered.filter(match).map((i) => (
                <Table.Tr key={i.key} style={{ cursor: "pointer" }} onClick={() => setOpen(i.key)}>
                  <Table.Td><Text size="sm" fw={600}>{i.name}</Text>{i.learned && <Badge size="xs" variant="outline" color="yellow">{i.source === "import" ? "learned from an import" : "learned from a drop"}</Badge>}</Table.Td>
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
      {importing && <ImportModal meta={meta} onClose={() => setImporting(false)} onDone={load} />}
      {history && <LedgerImport onClose={() => setHistory(false)} onDone={load} />}
    </Stack>
  );
}

/** The re-weight question (D12) — Yes · No (default) · Back; shared by the grid's Save and the CSV import. */
function ReweightAsk({ ask, onAnswer }: { ask: string | null; onAnswer: (reweight: boolean | null) => void }) {
  return (
    <Modal opened={!!ask} onClose={() => onAnswer(null)} title="Re-weight past awards?" centered>
      <Stack gap="md">
        <Text size="sm">{ask}</Text>
        <Group justify="flex-end" gap="sm">
          <Button variant="subtle" color="gray" onClick={() => onAnswer(null)}>Back</Button>
          <Button variant="default" onClick={() => onAnswer(false)}>No, keep their weight</Button>
          <Button onClick={() => onAnswer(true)}>Re-weight them</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function TierGrid({ meta, itemKey, onClose, onSaved }: { meta: Meta; itemKey: string; onClose: () => void; onSaved: () => void }) {
  const [item, setItem] = useState<Grid | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  useDirty(`tiers:${itemKey}`, Object.keys(draft).length > 0);
  const [drafted, setDrafted] = useState<TierDraft | null>(null);  // Claude's proposal, shown until saved or discarded
  const [busy, setBusy] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [reweightAsk, setReweightAsk] = useState<string | null>(null);
  useEffect(() => {
    api.get<Grid>(`/api/loot/items/${encodeURIComponent(itemKey)}`).then((g) => { setItem(g); setDraft({}); setDrafted(g.draft || null); if (g.draft) adopt(g, g.draft); }).catch(fail);
  }, [itemKey]);
  const changed = useMemo(() => Object.keys(draft).length, [draft]);
  /** Put the proposal into the editor: only cells that differ from what is saved, so Save writes exactly the draft. */
  function adopt(g: Grid, d: TierDraft) {
    const next: Record<string, string> = {};
    for (const r of g.rows) {
      const want = d.tiers[r.key];
      if (want && r.equippable && want !== (r.own ?? r.tier ?? CLEAR)) next[r.key] = want;
    }
    setDraft(next);
  }
  if (!item) return null;
  async function askClaude() {
    setDrafting(true);
    try {
      const r = await api.post<{ draft: TierDraft; item: Grid }>(`/api/loot/items/${encodeURIComponent(itemKey)}/draft`, {});
      setItem(r.item); setDrafted(r.draft); adopt(r.item, r.draft);
      ok(Object.keys(r.draft.tiers).length ? "Draft ready — review it, then Save" : "Claude drafted no tiers for this item");
    } catch (e) { fail(e); } finally { setDrafting(false); }
  }
  async function save(reweight?: boolean) {
    setBusy(true);
    try {
      const tiers = Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, v === CLEAR ? "-" : v]));
      const r = await api.post<{ message?: string; ask?: string; item?: Grid }>(`/api/loot/items/${encodeURIComponent(itemKey)}/tiers`, reweight === undefined ? { tiers } : { tiers, reweight });
      if (r.ask) { setReweightAsk(r.ask); return; }
      ok(r.message || "saved");
      if (r.item) setItem(r.item);
      setDraft({}); setDrafted(null);
      onSaved();
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const byClass = item.rows.reduce<Record<string, ItemGrid["rows"]>>((acc, r) => { (acc[r.cls] ||= []).push(r); return acc; }, {});
  return (
    <Modal opened onClose={onClose} title={`${item.name}${item.boss ? ` · ${item.boss}` : ""}`} size="xl">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Text size="xs" c="dimmed">One tier per spec (the guild's over the game profile's). Greyed rows can't equip it. {item.awards ? `${item.awards} award(s) so far.` : ""}</Text>
          <Button size="xs" variant="light" loading={drafting} onClick={askClaude}>Draft with Claude</Button>
        </Group>
        {drafted && (
          <Alert color="violet" py={6} title={`Draft — ${drafted.provenance}`} withCloseButton onClose={() => { setDrafted(null); setDraft({}); }}>
            <Stack gap={2}>
              {drafted.summary && <Text size="sm">{drafted.summary}</Text>}
              {drafted.warnings.map((x) => <Text key={x} size="xs" c="yellow">{x}</Text>)}
              {drafted.dropped.length > 0 && <Text size="xs" c="dimmed">Left out: {drafted.dropped.join(" · ")}</Text>}
              <Text size="xs" c="dimmed">{drafted.by ? `asked by ${drafted.by}` : ""}{drafted.at_label ? ` · ${drafted.at_label}` : ""} — the drafted cells are marked below; change any, then Save (or close this to drop the draft).</Text>
            </Stack>
          </Alert>
        )}
        {Object.keys(item.wide).length > 0 && <Text size="xs">Wider tiers: {Object.entries(item.wide).map(([k, v]) => `${k} ${v}`).join(" · ")}</Text>}
        <Table verticalSpacing={3}>
          <Table.Tbody>
            {Object.entries(byClass).map(([cls, rows]) => rows.map((r) => {
              const cur = draft[r.key] ?? r.own ?? r.tier ?? CLEAR;
              const fromDraft = drafted && drafted.tiers[r.key] && draft[r.key] === drafted.tiers[r.key];
              return (
                <Table.Tr key={r.key} style={{ opacity: r.equippable ? 1 : 0.35 }}>
                  <Table.Td w={40}><Tooltip label={`${r.spec} ${cls} · ${r.role}${r.why ? ` — ${r.why}` : ""}`}><Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="spec" id={r.key} size={24} title={`${r.spec} ${cls}`} /></Box></Tooltip></Table.Td>
                  <Table.Td>
                    <SegmentedControl size="xs" disabled={!r.equippable} value={cur} data={[...item.tiers, CLEAR]} color={fromDraft ? "violet" : undefined}
                      onChange={(v) => setDraft((d) => { const next = { ...d }; if (v === (r.own ?? r.tier ?? CLEAR)) delete next[r.key]; else next[r.key] = v; return next; })} />
                  </Table.Td>
                  <Table.Td>
                    {fromDraft
                      ? <Tooltip label={drafted?.why[r.key] || "Claude's draft"}><Badge size="xs" variant="light" color="violet">draft</Badge></Tooltip>
                      : <Text size="xs" c="dimmed">{r.profile && !r.own ? `profile ${r.profile}` : r.own ? "guild" : ""}</Text>}
                  </Table.Td>
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
      <ReweightAsk ask={reweightAsk} onAnswer={(a) => { setReweightAsk(null); if (a !== null) save(a); }} />
    </Modal>
  );
}

/** Import a tier CSV (the export's columns): paste it or pick the file → Preview (what would change per item, duplicates
 *  merged, unknown items, bad cells) → Import, carrying the preview's rev (a tier saved meanwhile → preview again). */
function ImportModal({ meta, onClose, onDone }: { meta: Meta; onClose: () => void; onDone: () => void }) {
  const [csv, setCsv] = useState("");
  const [pv, setPv] = useState<ImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [reweightAsk, setReweightAsk] = useState<string | null>(null);
  async function preview(text = csv) {
    setBusy(true);
    try { setPv((await api.post<{ preview: ImportPreview }>("/api/loot/items/import", { csv: text })).preview); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function apply(reweight?: boolean) {
    if (!pv) return;
    setBusy(true);
    try {
      const r = await api.post<{ message?: string; ask?: string; preview?: ImportPreview }>("/api/loot/items/import", { csv, preview: false, rev: pv.rev, ...(reweight === undefined ? {} : { reweight }) });
      if (r.ask) { setReweightAsk(r.ask); return; }
      ok(r.message || "imported"); onDone(); onClose();
    } catch (e) { fail(e); preview(); } finally { setBusy(false); }
  }
  function pick(f: File | null) {
    if (!f) return;
    f.text().then((t) => { setCsv(t); setPv(null); preview(t); }).catch(fail);
  }
  return (
    <Modal opened onClose={onClose} title="Import tiers from a CSV" size="xl">
      <Stack gap="sm">
        <Text size="xs" c="dimmed">The export's columns: item_id, item, boss, then one per spec ("Priest:Holy"). A tier sets the cell, an empty cell leaves it, "-" clears a tier the guild set. Nothing is written before Import.</Text>
        <Group gap="xs">
          <FileButton onChange={pick} accept=".csv,text/csv">{(props) => <Button size="xs" variant="default" {...props}>Pick a file…</Button>}</FileButton>
          <Text size="xs" c="dimmed">or paste it below</Text>
        </Group>
        <Textarea autosize minRows={3} maxRows={8} value={csv} onChange={(e) => { setCsv(e.currentTarget.value); setPv(null); }} placeholder="item_id,item,boss,Druid:Balance,…" />
        {pv && (
          <Stack gap={6}>
            <Text size="sm" fw={600}>{pv.cells} tier(s) on {pv.items.length} item(s) would change · {pv.unchanged} row(s) change nothing · {pv.rows} row(s) read</Text>
            {pv.duplicates.length > 0 && <Text size="xs" c="yellow">Given twice (merged — the later row's cells win): {pv.duplicates.join(", ")}</Text>}
            {pv.unknown.length > 0 && <Text size="xs" c="yellow">Not an item the council knows (skipped — it can be tiered after its first drop): {pv.unknown.join(", ")}</Text>}
            {pv.errors.map((x) => <Text key={x} size="xs" c="red">{x}</Text>)}
            {pv.items.length > 0 && (
              <Table.ScrollContainer minWidth={500}>
                <Table verticalSpacing={2}>
                  <Table.Tbody>
                    {pv.items.map((it) => (
                      <Table.Tr key={it.key}>
                        <Table.Td><Text size="sm" fw={600}>{it.name}</Text>{it.awards > 0 && <Text size="xs" c="dimmed">{it.awards} past award(s)</Text>}</Table.Td>
                        <Table.Td>
                          <Group gap={8} wrap="wrap">
                            {Object.entries(it.changes).map(([spec, [b, a]]) => (
                              <Group key={spec} gap={4} wrap="nowrap">
                                {spec.includes(":") && !spec.startsWith("role:") && !spec.endsWith(":*")
                                  ? <GameIcon meta={meta} kind="spec" id={spec} size={18} title={spec} />
                                  : <Badge size="xs" variant="outline">{spec}</Badge>}
                                <Text size="xs">{b || "—"} → {a || "—"}</Text>
                              </Group>
                            ))}
                          </Group>
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            )}
          </Stack>
        )}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Close</Button>
          <Button variant="default" disabled={!csv.trim()} loading={busy && !pv} onClick={() => preview()}>Preview</Button>
          <Button disabled={!pv || pv.items.length === 0} loading={busy && !!pv} onClick={() => apply()}>Import {pv?.cells || ""} tier{pv?.cells === 1 ? "" : "s"}</Button>
        </Group>
      </Stack>
      <ReweightAsk ask={reweightAsk} onAnswer={(a) => { setReweightAsk(null); if (a !== null) apply(a); }} />
    </Modal>
  );
}
