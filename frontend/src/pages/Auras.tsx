import { useEffect, useMemo, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, NumberInput, Select, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconCheck, IconPencil, IconX } from "@tabler/icons-react";
import { api } from "../api";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { CLASS_COLOURS } from "../theme";

interface Family { id: string; name: string; value: Record<string, number>; status: string; note: string; overridden: string[]; declared: boolean; buffs: string[] }
interface Buff { id: string; name: string; abbr: string; colour: string; art: string | null; providers: string[]; scope: string; kind: string; slot: string | null; family: string; strength: number; status: string; note: string; overridden: string[]; choices: string[] }
interface Auras { families: Family[]; buffs: Buff[]; value_keys: string[]; specs: { cls: string; spec: string; role: string; key: string }[]; scopes: string[]; statuses: string[]; owner: boolean }

const STATUS_COLOUR: Record<string, string> = { confirmed: "teal", reported: "yellow", assumed: "gray" };

export function AurasPage() {
  const [data, setData] = useState<Auras | null>(null);
  const [editing, setEditing] = useState<string | null>(null); // "b:<id>" | "f:<id>" | "f:new"
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<Auras>("/api/auras").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const famName = (id: string) => data.families.find((f) => f.id === id)?.name || id;
  const known = new Set(data.buffs.filter((b) => (b.kind === "aura")).map((b) => b.id));
  const overBuffs = data.buffs.filter((b) => b.overridden.length), overFams = data.families.filter((f) => f.overridden.length);
  async function resetAll() {
    const what = [overBuffs.length ? `${overBuffs.length} buff${overBuffs.length === 1 ? "" : "s"} (${overBuffs.map((b) => b.name).join(", ")})` : null, overFams.length ? `${overFams.length} famil${overFams.length === 1 ? "y" : "ies"} (${overFams.map((f) => f.name).join(", ")})` : null].filter(Boolean).join(" and ");
    if (!(await ask({ title: "Back to the game defaults for every aura?", message: `Overrides on ${what} go, guild-declared families included. The solver, the cards and the board read the defaults from the next run on; nothing already locked is re-solved.`, confirmLabel: "Reset all", color: "red" }))) return;
    api.post<{ message: string }>("/api/admin/aura/reset", {}).then((r) => { ok(r.message); load(); }).catch(fail);
  }
  /** One buff or family back to the game defaults (its own overrides only). */
  async function resetOne(kind: "buff" | "family", id: string, name: string, overridden: string[], onDone: () => void) {
    if (!(await ask({ title: `${name} back to the game defaults?`, message: `Only this ${kind}'s overrides go: ${overridden.join(", ")}. Other ${kind === "buff" ? "buffs and the families" : "families and the buffs"} keep theirs.`, confirmLabel: "Reset", color: "red" }))) return;
    api.post<{ message: string }>("/api/admin/aura/reset", { id }).then((r) => { ok(r.message); onDone(); }).catch(fail);
  }
  return (
    <Stack gap="lg">
      <PageTitle title="Auras" intro="What we know about Forever's buffs. A stacking family says which buffs don't stack (the strongest present one counts) and who benefits from it; each buff says who casts it, whether it reaches the party or the whole raid, how strong it is, and how sure we are. Amber marks a guild override of the game defaults. The solver, the cards and the board all read this." />

      <Card>
        <CardHeader title="Buffs" hint={data.owner ? "click a row to edit" : "the owner edits these"} />
        <Table.ScrollContainer minWidth={820}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Buff</Table.Th><Table.Th>Cast by</Table.Th><Table.Th>Scope</Table.Th><Table.Th>Family</Table.Th><Table.Th w={90}>Strength</Table.Th><Table.Th w={110}>Status</Table.Th><Table.Th>Note</Table.Th><Table.Th w={40} /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.buffs.map((b) => editing === `b:${b.id}`
                ? <BuffEdit key={b.id} b={b} data={data} onDone={() => { setEditing(null); load(); }} onReset={() => resetOne("buff", b.id, b.name, b.overridden, () => { setEditing(null); load(); })} />
                : (
                  <Table.Tr key={b.id} style={{ cursor: data.owner ? "pointer" : undefined }} onClick={() => data.owner && setEditing(`b:${b.id}`)}>
                    <Table.Td><Group gap="sm" wrap="nowrap">{b.art ? <Box component="img" src={`/img/icon/${b.art}.jpg`} alt="" style={{ width: 26, height: 26, borderRadius: 5, border: "1px solid var(--mantine-color-slate-5)" }} /> : <Box style={{ width: 26, height: 26, borderRadius: 5, background: b.colour }} />}<Box><Text size="sm" fw={600}>{b.name}</Text><Text size="xs" c="dimmed">{b.id}{b.slot ? ` · slot ${b.slot.replace("shaman_", "")}` : ""}</Text></Box></Group></Table.Td>
                    <Table.Td><Group gap={4}>{b.providers.map((p) => <Text key={p} size="xs" c={CLASS_COLOURS[p.split(":")[0]]}>{p.replace(":*", "")}</Text>)}</Group></Table.Td>
                    <Table.Td><Badge variant="light" color={b.overridden.includes("scope") ? "yellow" : b.scope === "raid" ? "teal" : "gray"}>{b.scope}</Badge></Table.Td>
                    <Table.Td><Text size="sm" c={b.overridden.includes("family") ? "yellow" : undefined}>{famName(b.family)}</Text>{b.family !== b.id && <Text size="xs" c="dimmed">doesn't stack with {data.buffs.filter((x) => x.family === b.family && x.id !== b.id).map((x) => x.name).join(", ") || "—"}</Text>}</Table.Td>
                    <Table.Td><Text size="sm" c={b.overridden.includes("strength") ? "yellow" : undefined}>×{b.strength}</Text></Table.Td>
                    <Table.Td><Badge size="xs" variant="outline" color={b.overridden.includes("status") ? "yellow" : STATUS_COLOUR[b.status]}>{b.status}</Badge></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed" lineClamp={2}>{b.note}</Text></Table.Td>
                    <Table.Td>{data.owner && <IconPencil size={14} opacity={0.5} />}</Table.Td>
                  </Table.Tr>
                ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>

      <Card>
        <CardHeader title="Stacking families · who benefits" hint="points per beneficiary; a buff gives its family's points × its strength" action={data.owner ? <Button size="xs" variant="default" onClick={() => setEditing("f:new")}>New family</Button> : null} />
        <Table.ScrollContainer minWidth={820}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Family</Table.Th><Table.Th>Buffs</Table.Th><Table.Th>Benefits</Table.Th><Table.Th w={110}>Status</Table.Th><Table.Th>Note</Table.Th><Table.Th w={40} /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {editing === "f:new" && <FamilyEdit f={null} data={data} onDone={() => { setEditing(null); load(); }} />}
              {data.families.filter((f) => f.buffs.some((b) => known.has(b)) || f.declared).map((f) => editing === `f:${f.id}`
                ? <FamilyEdit key={f.id} f={f} data={data} onDone={() => { setEditing(null); load(); }} onReset={() => resetOne("family", f.id, f.name, f.overridden, () => { setEditing(null); load(); })} />
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
        {data.owner && (overBuffs.length > 0 || overFams.length > 0) && (
          <Group p="sm" px="md" justify="flex-end" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
            <Button size="xs" variant="subtle" color="red" onClick={resetAll}>Reset all to game defaults</Button>
          </Group>
        )}
      </Card>
      <Text size="xs" c="dimmed">Also in plain text, in the ops channel or with /gm change: "fortitude and blood pact don't stack", "sanctity aura is raid-wide", "only mana users benefit from intellect", "windfury is confirmed". Or /gm config aura.</Text>
      {confirmDialog}
    </Stack>
  );
}

function BuffEdit({ b, data, onDone, onReset }: { b: Buff; data: Auras; onDone: () => void; onReset: () => void }) {
  const [d, setD] = useState({ scope: b.scope, family: b.family, strength: b.strength as number | string, status: b.status, note: b.note });
  const [busy, setBusy] = useState(false);
  const fams = data.families.map((f) => ({ value: f.id, label: `${f.name} (${f.id})` }));
  async function save() {
    setBusy(true);
    try { const r = await api.post<{ message: string }>("/api/admin/aura", { id: b.id, ...d }); ok(r.message); onDone(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  return (
    <Table.Tr style={{ background: "var(--mantine-color-slate-6)" }}>
      <Table.Td><Text size="sm" fw={600}>{b.name}</Text><Text size="xs" c="dimmed">{b.id}</Text></Table.Td>
      <Table.Td><Text size="xs" c="dimmed">{b.providers.join(", ")}</Text></Table.Td>
      <Table.Td><Select size="xs" w={100} data={data.scopes} value={d.scope} onChange={(v) => setD({ ...d, scope: v || d.scope })} /></Table.Td>
      <Table.Td><Select size="xs" data={fams} value={d.family} onChange={(v) => setD({ ...d, family: v || d.family })} searchable /></Table.Td>
      <Table.Td><NumberInput size="xs" min={0} step={0.5} value={d.strength} onChange={(v) => setD({ ...d, strength: v })} /></Table.Td>
      <Table.Td><Select size="xs" data={data.statuses} value={d.status} onChange={(v) => setD({ ...d, status: v || d.status })} /></Table.Td>
      <Table.Td><TextInput size="xs" value={d.note} onChange={(e) => setD({ ...d, note: e.currentTarget.value })} placeholder="source, date, what was tested" /></Table.Td>
      <Table.Td><Group gap={2} wrap="nowrap"><ActionIcon size="sm" color="teal" variant="light" loading={busy} onClick={save}><IconCheck size={14} /></ActionIcon><ActionIcon size="sm" variant="subtle" color="gray" onClick={onDone}><IconX size={14} /></ActionIcon>{b.overridden.length > 0 && <Tooltip label="back to the game defaults for this buff"><ActionIcon size="sm" variant="subtle" color="red" onClick={onReset}>↺</ActionIcon></Tooltip>}</Group></Table.Td>
    </Table.Tr>
  );
}

function FamilyEdit({ f, data, onDone, onReset }: { f: Family | null; data: Auras; onDone: () => void; onReset?: () => void }) {
  const [id, setId] = useState(f?.id || "");
  const [d, setD] = useState({ name: f?.name || "", status: f?.status || "assumed", note: f?.note || "", value: { ...(f?.value || {}) } as Record<string, number | string> });
  const [busy, setBusy] = useState(false);
  const byClass = useMemo(() => Object.entries(data.specs.reduce((m, s) => { (m[s.cls] ||= []).push(s); return m; }, {} as Record<string, Auras["specs"]>)), [data.specs]);
  const setVal = (k: string, v: number | string) => setD({ ...d, value: { ...d.value, [k]: v } });
  async function save() {
    if (!id.trim()) { fail(new Error("a family needs an id, e.g. stamina")); return; }
    setBusy(true);
    try { const r = await api.post<{ message: string }>("/api/admin/family", { id: id.trim().toLowerCase().replace(/\s+/g, "_"), ...d }); ok(r.message); onDone(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const cell = (k: string, label: string) => <NumberInput key={k} size="xs" w={78} min={0} step={1} value={d.value[k] ?? ""} onChange={(v) => setVal(k, v)} placeholder="0" label={label} styles={{ label: { fontSize: 11 } }} />;
  return (
    <Table.Tr style={{ background: "var(--mantine-color-slate-6)" }}>
      <Table.Td colSpan={6}>
        <Stack gap="sm">
          <Group gap="sm" align="flex-end" wrap="wrap">
            {f ? <Box><Text size="sm" fw={600}>{f.name}</Text><Text size="xs" c="dimmed">{f.id}</Text></Box> : <TextInput size="xs" label="id" placeholder="stamina" value={id} onChange={(e) => setId(e.currentTarget.value)} w={150} />}
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
            <Button size="xs" loading={busy} onClick={save}>Save family</Button>
          </Group>
        </Stack>
      </Table.Td>
    </Table.Tr>
  );
}
