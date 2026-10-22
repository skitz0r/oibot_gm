import { useRefresh } from "../hooks/usePoll";
import { useDirty } from "../hooks/useDirty";
import { useLive } from "../hooks/useLive";
import { useEffect, useMemo, useRef, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, NumberInput, Select, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPencil, IconRestore } from "@tabler/icons-react";
import { api, type Meta, type OpsChange } from "../api";
import { CardHeader, PageTitle, fail } from "../components/Page";
import { useOpsSave } from "../components/ReviewModal";
import { CLASS_COLOURS } from "../theme";

interface Family { id: string; name: string; value: Record<string, number>; status: string; note: string; overridden: string[]; declared: boolean; buffs: string[] }
interface Buff { id: string; name: string; abbr: string; colour: string; art: string | null; providers: string[]; scope: string; kind: string; slot: string | null; family: string; strength: number; status: string; note: string; overridden: string[]; choices: string[] }
/** `rev` = the buffs section rev (an edit-mode Save sends it back: someone else's change meanwhile is a 409, never overwritten). */
interface Auras { families: Family[]; buffs: Buff[]; value_keys: string[]; specs: { cls: string; spec: string; role: string; key: string }[]; scopes: string[]; statuses: string[]; owner: boolean; rev: string }

const STATUS_COLOUR: Record<string, string> = { confirmed: "teal", reported: "yellow", assumed: "gray" };
/** Strength presets (the Discord wizard's list); "other…" opens a number box. */
const STRENGTHS = ["0.5", "1", "1.5", "2", "3"];
const NEW_FAMILY = "__new";
type BuffDraft = { scope: string; family: string; strength: number | string; status: string; note: string; family_new?: string };

export function AurasPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Auras | null>(null);
  const [editing, setEditing] = useState<string | null>(null); // "f:<id>" | "f:new"
  const [save, reviewDialog] = useOpsSave();
  const load = () => api.get<Auras>("/api/auras").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useRefresh(load);
  const [drafts, setDrafts] = useState<Record<string, BuffDraft> | null>(null);  // the Buffs table's edit mode: one Save for the whole table
  useDirty("auras", drafts !== null || editing !== null);
  const editingRef = useRef(false);
  editingRef.current = drafts !== null || editing !== null;
  useLive(() => { if (!editingRef.current) load(); }, ["config"]);  // a change made elsewhere shows — never under an edit in progress
  const [saving, setSaving] = useState(false);
  const [refused, setRefused] = useState<Set<string>>(new Set());  // buffs whose last Save was refused: still drafted, marked
  const draftOf = (b: Buff): BuffDraft => ({ scope: b.scope, family: b.family, strength: b.strength, status: b.status, note: b.note });
  /** The table's draft as typed changes: one `buff` change per edited buff, only the fields that differ (the server coalesces a buff's fields into ONE write). */
  function buffChanges(): OpsChange[] {
    if (!data || !drafts) return [];
    const out: OpsChange[] = [];
    for (const b of data.buffs) {
      const d = drafts[b.id];
      if (!d) continue;
      const fields: Record<string, unknown> = {};
      if (d.scope !== b.scope) fields.scope = d.scope;
      if (d.family === NEW_FAMILY && d.family_new?.trim()) fields.family_new = d.family_new.trim();
      else if (d.family !== b.family && d.family !== NEW_FAMILY) fields.family = d.family;
      if (Number(d.strength) !== Number(b.strength)) fields.strength = Number(d.strength);
      if (d.status !== b.status) fields.status = d.status;
      if ((d.note || "") !== (b.note || "")) fields.note = d.note;
      if (Object.keys(fields).length) out.push({ kind: "buff", target: b.id, fields });
    }
    return out;
  }
  async function saveBuffs() {
    if (!data || !drafts) return;
    const changes = buffChanges();
    if (!changes.length) { setDrafts(null); setRefused(new Set()); return; }  // nothing changed: leave edit mode, nothing to send
    setSaving(true);
    try {
      const r = await save(changes, { revs: { "config:buffs": data.rev }, title: "Review the aura changes" });
      if (r === null) return;  // Back to edits: the draft stays
      if (r.changed?.length) { fail(new Error("Someone else changed the auras since you opened them — reload to see their change, your edits are kept.")); return; }
      const bad = new Set((r.results || []).filter((x) => !x.ok).map((x) => data.buffs.find((b) => b.name === x.label)?.id).filter(Boolean) as string[]);
      const fresh = await api.get<Auras>("/api/auras");
      setData(fresh);
      if (bad.size) {  // edit mode stays open holding only the refused buffs
        setRefused(bad);
        setDrafts(Object.fromEntries(fresh.buffs.map((b) => [b.id, bad.has(b.id) && drafts[b.id] ? drafts[b.id] : draftOf(b)])));
      } else { setRefused(new Set()); setDrafts(null); }
    } catch (e) { fail(e); } finally { setSaving(false); }
  }
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const famName = (id: string) => data.families.find((f) => f.id === id)?.name || id;
  const known = new Set(data.buffs.filter((b) => (b.kind === "aura")).map((b) => b.id));
  const overBuffs = data.buffs.filter((b) => b.overridden.length), overFams = data.families.filter((f) => f.overridden.length);
  /** Every aura back to the defaults: warn-class, so the review shows the server's sentence (Registry.aura_reset_sentence) before anything goes. */
  async function resetAll() {
    const r = await save([{ kind: "aura_reset", target: null }], { revs: { "config:buffs": data!.rev }, title: "Back to the game defaults for every aura?" }).catch((e) => { fail(e); return null; });
    if (r && !r.changed?.length) { setDrafts(null); load(); }
  }
  /** One buff or family back to the game defaults (its own overrides only) — warn-class like the AuraWizard's decide screen: the review
   *  shows the server's sentence (configops.aura_reset_one_sentence) before anything goes. */
  async function resetOne(id: string, onDone: () => void) {
    const name = data!.buffs.find((b) => b.id === id)?.name || famName(id);
    const r = await save([{ kind: "aura_reset", target: id }], { revs: { "config:buffs": data!.rev }, title: `Back to the game defaults for ${name}?` }).catch((e) => { fail(e); return null; });
    if (r && !r.changed?.length) onDone();
  }
  return (
    <Stack gap="lg">
      <PageTitle title="Auras" intro={`What we know about ${meta.game}'s buffs. A stacking family says which buffs don't stack (the strongest present one counts) and who benefits from it; each buff says who casts it, whether it reaches the party or the whole raid, how strong it is, and how sure we are. Amber marks a guild override of the game defaults. The solver, the cards and the board all read this.`} />

      <Card>
        <CardHeader title="Buffs" hint={data.owner ? undefined : "the owner edits these"} action={data.owner && !drafts ? <Button size="xs" variant="default" leftSection={<IconPencil size={13} />} onClick={() => setDrafts(Object.fromEntries(data.buffs.map((b) => [b.id, draftOf(b)])))}>Edit auras</Button> : undefined} />
        <Table.ScrollContainer minWidth={820}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Buff</Table.Th><Table.Th>Cast by</Table.Th><Table.Th>Scope</Table.Th><Table.Th>Family</Table.Th><Table.Th w={90}>Strength</Table.Th><Table.Th w={drafts ? 150 : 110}>Status</Table.Th><Table.Th>Note</Table.Th><Table.Th w={40} /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.buffs.map((b) => drafts
                ? <BuffEdit key={b.id} b={b} data={data} d={drafts[b.id]} refused={refused.has(b.id)} setD={(d) => setDrafts({ ...drafts, [b.id]: d })} onReset={() => resetOne(b.id, () => { setDrafts(null); load(); })} />
                : (
                  <Table.Tr key={b.id}>
                    <Table.Td><Group gap="sm" wrap="nowrap">{b.art ? <Box component="img" src={`/img/icon/${b.art}.jpg`} alt="" style={{ width: 26, height: 26, borderRadius: 5, border: "1px solid var(--mantine-color-slate-5)" }} /> : <Box style={{ width: 26, height: 26, borderRadius: 5, background: b.colour }} />}<Box><Text size="sm" fw={600}>{b.name}</Text><Text size="xs" c="dimmed">{b.id}{b.slot ? ` · slot ${b.slot.replace("shaman_", "")}` : ""}</Text></Box></Group></Table.Td>
                    <Table.Td><Group gap={4}>{b.providers.map((p) => <Text key={p} size="xs" c={CLASS_COLOURS[p.split(":")[0]]}>{p.replace(":*", "")}</Text>)}</Group></Table.Td>
                    <Table.Td><Badge variant="light" color={b.overridden.includes("scope") ? "yellow" : b.scope === "raid" ? "teal" : "gray"}>{b.scope}</Badge></Table.Td>
                    <Table.Td><Text size="sm" c={b.overridden.includes("family") ? "yellow" : undefined}>{famName(b.family)}</Text>{b.family !== b.id && <Text size="xs" c="dimmed">doesn't stack with {data.buffs.filter((x) => x.family === b.family && x.id !== b.id).map((x) => x.name).join(", ") || "—"}</Text>}</Table.Td>
                    <Table.Td><Text size="sm" c={b.overridden.includes("strength") ? "yellow" : undefined}>×{b.strength}</Text></Table.Td>
                    <Table.Td><Badge size="xs" variant="outline" color={b.overridden.includes("status") ? "yellow" : STATUS_COLOUR[b.status]}>{b.status}</Badge></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed" lineClamp={2}>{b.note}</Text></Table.Td>
                    <Table.Td />
                  </Table.Tr>
                ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {drafts && (
          <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
            {(overBuffs.length > 0 || overFams.length > 0) && <Button size="sm" variant="subtle" color="red" onClick={resetAll}>Reset all to game defaults</Button>}
            <Text size="sm" c="dimmed" mr="auto">Edits apply on save · yellow = differs from the game default</Text>
            <Button variant="default" size="sm" onClick={() => { setDrafts(null); setRefused(new Set()); }}>Cancel</Button>
            <Button size="sm" loading={saving} onClick={saveBuffs}>Save changes</Button>
          </Group>
        )}
      </Card>

      <Card>
        <CardHeader title="Stacking families · who benefits" hint="points per beneficiary; a buff gives its family's points × its strength" action={data.owner ? <Button size="xs" variant="default" onClick={() => setEditing("f:new")}>New family</Button> : null} />
        <Table.ScrollContainer minWidth={820}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Family</Table.Th><Table.Th>Buffs</Table.Th><Table.Th>Benefits</Table.Th><Table.Th w={110}>Status</Table.Th><Table.Th>Note</Table.Th><Table.Th w={40} /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {editing === "f:new" && <FamilyEdit f={null} data={data} save={save} onDone={() => { setEditing(null); load(); }} />}
              {data.families.filter((f) => f.buffs.some((b) => known.has(b)) || f.declared).map((f) => editing === `f:${f.id}`
                ? <FamilyEdit key={f.id} f={f} data={data} save={save} onDone={() => { setEditing(null); load(); }} onReset={() => resetOne(f.id, () => { setEditing(null); load(); })} />
                : (
                  <Table.Tr key={f.id} style={{ cursor: data.owner ? "pointer" : undefined }} onClick={() => data.owner && setEditing(`f:${f.id}`)}>
                    <Table.Td><Text size="sm" fw={600} c={f.overridden.includes("name") ? "yellow" : undefined}>{f.name}</Text><Text size="xs" c="dimmed">{f.id}</Text></Table.Td>
                    <Table.Td><Text size="sm">{f.buffs.map((id) => data.buffs.find((b) => b.id === id)?.name || id).join(", ") || <Text span c="dimmed">no buff yet</Text>}</Text></Table.Td>
                    <Table.Td><Group gap={4}>{Object.entries(f.value).length === 0 && <Text size="xs" c="dimmed">nobody</Text>}{Object.entries(f.value).map(([k, v]) => <Badge key={k} size="xs" variant="outline" color={f.overridden.includes("value") ? "yellow" : "gray"}>{k.replace("spec:", "")} {v}</Badge>)}</Group></Table.Td>
                    <Table.Td><Badge size="xs" variant="outline" color={f.overridden.includes("status") ? "yellow" : STATUS_COLOUR[f.status]}>{f.status}</Badge></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed" lineClamp={2}>{f.note}</Text></Table.Td>
                    <Table.Td>{data.owner && <IconPencil size={14} opacity={0.5} />}</Table.Td>
                  </Table.Tr>
                ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      <Text size="xs" c="dimmed">Also in plain text, in the ops channel or with /gm change: "fortitude and blood pact don't stack", "sanctity aura is raid-wide", "only mana users benefit from intellect", "windfury is confirmed". Or /gm config aura.</Text>
      {reviewDialog}
    </Stack>
  );
}

function BuffEdit({ b, data, d, setD, onReset, refused }: { b: Buff; data: Auras; d: BuffDraft; setD: (d: BuffDraft) => void; onReset: () => void; refused?: boolean }) {
  const fams = [...data.families.map((f) => ({ value: f.id, label: f.name })), { value: NEW_FAMILY, label: "New family…" }];
  const [other, setOther] = useState(!STRENGTHS.includes(String(Number(d.strength))));
  return (
    <Table.Tr style={refused ? { background: "var(--mantine-color-red-light)" } : undefined}>
      <Table.Td><Text size="sm" fw={600}>{b.name}</Text><Text size="xs" c="dimmed">{b.id}</Text></Table.Td>
      <Table.Td><Text size="xs" c="dimmed">{b.providers.join(", ")}</Text></Table.Td>
      <Table.Td><Select size="xs" w={100} data={data.scopes} value={d.scope} onChange={(v) => setD({ ...d, scope: v || d.scope })} /></Table.Td>
      <Table.Td>
        <Select size="xs" data={fams} value={d.family} onChange={(v) => setD({ ...d, family: v || d.family })} searchable />
        {d.family === NEW_FAMILY && <TextInput size="xs" mt={4} placeholder="the new family's name" value={d.family_new || ""} maxLength={40} onChange={(e) => setD({ ...d, family_new: e.currentTarget.value })} />}
      </Table.Td>
      <Table.Td>
        {other
          ? <NumberInput size="xs" min={0} step={0.5} value={d.strength} onChange={(v) => setD({ ...d, strength: v })} rightSectionWidth={0} />
          : <Select size="xs" data={[...STRENGTHS.map((s) => ({ value: s, label: `×${s}` })), { value: "other", label: "other…" }]} value={String(Number(d.strength))}
              onChange={(v) => { if (v === "other") setOther(true); else if (v) setD({ ...d, strength: Number(v) }); }} />}
      </Table.Td>
      <Table.Td><Select size="xs" data={data.statuses} value={d.status} onChange={(v) => setD({ ...d, status: v || d.status })} /></Table.Td>
      <Table.Td><TextInput size="xs" value={d.note} onChange={(e) => setD({ ...d, note: e.currentTarget.value })} placeholder="source, date, what was tested" /></Table.Td>
      <Table.Td>{b.overridden.length > 0 && <Tooltip label={`${b.name} back to the game defaults`}><ActionIcon size="sm" variant="subtle" color="red" onClick={onReset}><IconRestore size={14} /></ActionIcon></Tooltip>}</Table.Td>
    </Table.Tr>
  );
}

function FamilyEdit({ f, data, onDone, onReset, save }: { f: Family | null; data: Auras; onDone: () => void; onReset?: () => void; save: ReturnType<typeof useOpsSave>[0] }) {
  const [d, setD] = useState({ name: f?.name || "", status: f?.status || "assumed", note: f?.note || "", value: { ...(f?.value || {}) } as Record<string, number | string> });
  const [busy, setBusy] = useState(false);
  const byClass = useMemo(() => Object.entries(data.specs.reduce((m, s) => { (m[s.cls] ||= []).push(s); return m; }, {} as Record<string, Auras["specs"]>)), [data.specs]);
  const setVal = (k: string, v: number | string) => setD({ ...d, value: { ...d.value, [k]: v } });
  /** Name / status / note and PER-KEY deltas (value:<key>: a number, or null to remove it) — never the whole map, so a key someone else set
   *  meanwhile survives. A new family has no id field: the server makes it from the name. */
  async function saveFamily() {
    if (!d.name.trim()) { fail(new Error("a family needs a name")); return; }
    const fields: Record<string, unknown> = {};
    if (!f || d.name !== f.name) fields.name = d.name.trim();
    if (!f || d.status !== f.status) fields.status = d.status;
    if (!f || (d.note || "") !== (f.note || "")) fields.note = d.note;
    const before = f?.value || {};
    for (const k of new Set([...Object.keys(before), ...Object.keys(d.value)])) {
      const raw = d.value[k];
      const want = raw === "" || raw === undefined || raw === null ? null : Number(raw);
      const had = before[k] ?? null;
      if (want === had || (want === 0 && had === null && !k.startsWith("spec:"))) continue;
      fields[`value:${k}`] = want === 0 && !k.startsWith("spec:") ? null : want;
    }
    setBusy(true);
    try {
      const r = await save([{ kind: "family", target: f?.id || null, fields }], { revs: { "config:buffs": data.rev } });
      if (r && !r.changed?.length && !(r.results || []).some((x) => !x.ok)) onDone();
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const cell = (k: string, label: string) => <NumberInput key={k} size="xs" w={78} min={0} step={1} value={d.value[k] ?? ""} onChange={(v) => setVal(k, v)} placeholder="0" label={label} styles={{ label: { fontSize: 11 } }} />;
  return (
    <Table.Tr style={{ background: "var(--mantine-color-slate-6)" }}>
      <Table.Td colSpan={6}>
        <Stack gap="sm">
          <Group gap="sm" align="flex-end" wrap="wrap">
            {f && <Box><Text size="sm" fw={600}>{f.name}</Text><Text size="xs" c="dimmed">{f.id}</Text></Box>}
            <TextInput size="xs" label="name" value={d.name} onChange={(e) => setD({ ...d, name: e.currentTarget.value })} w={200} />
            <Select size="xs" label="status" data={data.statuses} value={d.status} onChange={(v) => setD({ ...d, status: v || d.status })} w={130} />
            <TextInput size="xs" label="note" value={d.note} onChange={(e) => setD({ ...d, note: e.currentTarget.value })} style={{ flex: 1, minWidth: 200 }} />
          </Group>
          <Box>
            <Text size="xs" c="dimmed" mb={4}>Points per beneficiary. Broad keys first; a spec entry overrides the broad ones for that spec. 0 = does not benefit.</Text>
            <Group gap="xs" wrap="wrap">{data.value_keys.map((k) => cell(k, k))}</Group>
          </Box>
          <Box>
            <Text size="xs" c="dimmed" mb={4}>Per spec (optional)</Text>
            <Stack gap={4}>
              {byClass.map(([cls, specs]) => (
                <Group key={cls} gap="xs" wrap="wrap" align="flex-end"><Text size="xs" fw={600} c={CLASS_COLOURS[cls]} w={64}>{cls}</Text>{specs.map((s) => cell(s.key, s.spec))}</Group>
              ))}
            </Stack>
          </Box>
          <Group gap="xs" justify="flex-end">
            {f && f.overridden.length > 0 && onReset && <Button size="xs" variant="subtle" color="red" mr="auto" onClick={onReset}>Back to game defaults</Button>}
            <Button size="xs" variant="default" onClick={onDone}>Cancel</Button>
            <Button size="xs" loading={busy} onClick={saveFamily}>Save family</Button>
          </Group>
        </Stack>
      </Table.Td>
    </Table.Tr>
  );
}
