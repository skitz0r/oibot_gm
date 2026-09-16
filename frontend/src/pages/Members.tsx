import { Fragment, useEffect, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, Radio, Select, Stack, Table, Text, TextInput, Tooltip, UnstyledButton } from "@mantine/core";
import { IconCrown, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, type Character, type MemberRow, type Members as MembersData, type Meta, type WeekBlock } from "../api";
import { GameIcon } from "../components/Icons";
import { HeatMap } from "../components/HeatMap";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { WeekGrid } from "../components/WeekGrid";
import { CLASS_COLOURS } from "../theme";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PREF = "#2E9E6B", AVAIL = "#B8892A", NONE = "var(--mantine-color-slate-6)";
const PRIV: Record<string, string> = { owner: "yellow", officer: "teal", member: "gray", outside: "red" };

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; main: boolean; named: boolean; isNew?: boolean };
type MemberDraft = { characters: Draft[]; deletes: string[]; week?: WeekBlock[] };

function daySummary(week: WeekBlock[]) {
  const days = Array.from({ length: 7 }, () => "" as "" | "preferred" | "available");
  week.forEach((b) => { if (b.level === "preferred" || !days[b.day]) days[b.day] = b.level; });
  return { days, hours: week.reduce((s, b) => s + (b.end - b.start), 0) / 60 };
}

export function MembersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<MembersData | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [drafts, setDrafts] = useState<Record<string, MemberDraft> | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<MembersData>("/api/members").then(setData).catch(fail);
  useEffect(() => { load(); }, []);

  const specsOf = (cls: string) => Object.entries(meta.classes[cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }));
  const toggle = (uid: string) => setOpen((o) => { const n = new Set(o); if (n.has(uid)) n.delete(uid); else n.add(uid); return n; });

  function startEdit() {
    if (!data) return;
    setDrafts(Object.fromEntries(data.rows.map((r) => [r.uid, { characters: r.characters.map((c) => ({ label: c.label, cls: c.cls, spec: c.spec, offspec: c.offspec, name: c.name || "", surname: c.surname || "", main: c.is_main, named: !!c.name })), deletes: [] }])));
  }
  function upd(uid: string, patch: Partial<MemberDraft>) { drafts && setDrafts({ ...drafts, [uid]: { ...drafts[uid], ...patch } }); }
  function updChar(uid: string, i: number, patch: Partial<Draft>) {
    if (!drafts) return;
    let chars = drafts[uid].characters.map((c, j) => (j === i ? { ...c, ...patch } : c));
    if (patch.main) chars = chars.map((c, j) => ({ ...c, main: j === i }));
    upd(uid, { characters: chars });
  }
  function removeChar(uid: string, i: number) {
    if (!drafts) return;
    const c = drafts[uid].characters[i];
    upd(uid, { characters: drafts[uid].characters.filter((_, j) => j !== i), deletes: c.label ? [...drafts[uid].deletes, c.label] : drafts[uid].deletes });
  }
  async function save() {
    if (!drafts) return;
    setBusy("save");
    try {
      const rows = Object.entries(drafts).map(([uid, d]) => ({ uid, characters: d.characters, deletes: d.deletes, week: d.week ?? null }));
      const r = await api.post<{ message: string }>("/api/members/save", { rows });
      ok(r.message); setDrafts(null); await load();
    } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function act(key: string, path: string, payload: unknown) {
    setBusy(key);
    try { const r = await api.post<{ message: string }>(path, payload); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function del(r: MemberRow, c: Character) {
    if (!confirm(`Delete ${c.label} (${r.display_name})? It is removed from every roster.`)) return;
    await act(`d${c.label}`, "/api/members/save", { rows: [{ uid: r.uid, characters: [], deletes: [c.label] }] });
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  const editing = drafts !== null;
  return (
    <Stack gap="lg">
      <PageTitle title="Members" intro={`${data.rows.length} members with characters · ${data.members} in the registry · owner and officer badges come from Discord roles`}
        right={!editing ? <Button variant="default" leftSection={<IconPencil size={15} />} onClick={startEdit}>Edit members</Button> : null} />
      <Card>
        <Table.ScrollContainer minWidth={900}>
          <Table style={{ tableLayout: "fixed" }}>
            <Table.Thead>
              <Table.Tr><Table.Th w={210}>Member</Table.Th><Table.Th w={editing ? 56 : 44} /><Table.Th>Character</Table.Th><Table.Th w="20%">Spec</Table.Th><Table.Th w="20%">Offspec</Table.Th><Table.Th w={130}>{editing ? "" : "Confirmed"}</Table.Th><Table.Th w={48} /></Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.rows.map((r) => {
                const d = drafts?.[r.uid];
                const week = d?.week ?? r.week;
                const { days, hours } = daySummary(week);
                const chars = d ? d.characters : r.characters;
                const span = chars.length + (editing ? 1 : 0) + (open.has(r.uid) ? 1 : 0);
                const memberCell = (
                  <Table.Td rowSpan={span} style={{ verticalAlign: "top", borderRight: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-7)" }}>
                    <Group gap={8} wrap="nowrap"><Text fw={700} truncate>{r.display_name}</Text><Badge size="xs" variant="outline" color={PRIV[r.privilege] || "gray"}>{r.privilege}</Badge></Group>
                    <Text size="xs" c="dimmed">{r.verification}</Text>
                    {r.asks.map((a) => <Badge key={a.roster} size="xs" variant="light" color="yellow" mt={4}>confirm {a.roster}?</Badge>)}
                    <Tooltip label={open.has(r.uid) ? "hide the week" : "show the full week"}>
                      <UnstyledButton aria-label={`${open.has(r.uid) ? "hide" : "show"} the week of ${r.display_name}`} onClick={() => toggle(r.uid)} mt={8} px={4} py={3} style={{ display: "flex", gap: 3, alignItems: "center", borderRadius: 6, border: `1px solid ${open.has(r.uid) ? "var(--mantine-color-slate-5)" : "transparent"}`, background: open.has(r.uid) ? "var(--mantine-color-slate-6)" : undefined, whiteSpace: "nowrap" }}>
                        {days.map((lv, i) => <Box key={i} title={DAYS[i]} style={{ width: 13, height: 13, borderRadius: 3, background: lv === "preferred" ? PREF : lv === "available" ? AVAIL : NONE }} />)}
                        <Text size="xs" c="dimmed" ml={6}>{hours ? `${hours} h` : "no grid yet"}{d?.week ? " · edited" : ""}</Text>
                      </UnstyledButton>
                    </Tooltip>
                  </Table.Td>
                );
                return (
                  <Fragment key={r.uid}>
                    {(chars.length ? chars : [null]).map((c, i) => (
                      <Table.Tr key={c ? (c as Draft | Character).label ?? `new${i}` : "empty"} style={{ borderTop: i === 0 ? "1px solid var(--mantine-color-slate-5)" : undefined, background: editing ? "var(--mantine-color-slate-6)" : undefined }}>
                        {i === 0 && memberCell}
                        {c === null ? <Table.Td colSpan={6}><Text size="sm" c="dimmed">no characters</Text></Table.Td>
                          : !editing ? <ViewRow meta={meta} c={c as Character} busy={busy} onConfirm={() => act(`c${(c as Character).label}`, "/api/admin/confirm", { label: (c as Character).label })} onDelete={() => del(r, c as Character)} />
                          : <EditRow meta={meta} d={c as Draft} uid={r.uid} specsOf={specsOf} onChange={(p) => updChar(r.uid, i, p)} onRemove={() => removeChar(r.uid, i)} />}
                      </Table.Tr>
                    ))}
                    {editing && (
                      <Table.Tr style={{ background: "var(--mantine-color-slate-6)" }}>
                        {chars.length === 0 && memberCell}
                        <Table.Td /><Table.Td colSpan={5}><Button size="xs" variant="subtle" leftSection={<IconPlus size={14} />} onClick={() => upd(r.uid, { characters: [...(d?.characters ?? []), { label: null, cls: "", spec: "", offspec: null, name: "", surname: "", main: chars.length === 0, named: false, isNew: true }] })}>Add a character for {r.display_name}</Button></Table.Td>
                      </Table.Tr>
                    )}
                    {open.has(r.uid) && (
                      <Table.Tr>
                        <Table.Td colSpan={6} style={{ background: "var(--mantine-color-slate-7)" }}>
                          <Group gap="md" mb="xs" wrap="wrap"><Text size="sm" fw={600}>{r.display_name} · when they can raid</Text>{!editing && <Text size="xs" c="dimmed">read-only · press Edit members to change it</Text>}</Group>
                          <WeekGrid value={week} onChange={editing ? (w) => upd(r.uid, { week: w }) : undefined} readOnly={!editing} compact landscape raidWindows={data.raid_windows} tz={data.tz} />
                        </Table.Td>
                      </Table.Tr>
                    )}
                  </Fragment>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {editing && (
          <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
            <Text size="sm" c="dimmed" mr="auto">Names, specs, mains, adds, deletes and availability apply on save · click a member's day squares to edit their week</Text>
            <Button variant="default" size="sm" onClick={() => setDrafts(null)}>Cancel</Button>
            <Button size="sm" loading={busy === "save"} onClick={save}>Save changes</Button>
          </Group>
        )}
      </Card>
      <Card>
        <CardHeader title="Everyone's availability" hint={`${data.grid_members} of ${data.rows.length} mains have a grid · ${data.tz} · darker = more people, green = mostly preferred`} />
        <Box p="md"><HeatMap heat={data.week_heat} n={data.grid_members} /></Box>
      </Card>
    </Stack>
  );
}

function ViewRow({ meta, c, busy, onConfirm, onDelete }: { meta: Meta; c: Character; busy: string | null; onConfirm: () => void; onDelete: () => void }) {
  return (
    <>
      <Table.Td style={{ textAlign: "center" }}>{c.is_main ? <Tooltip label="main"><IconCrown size={18} color="var(--mantine-color-yellow-5)" /></Tooltip> : <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: ".06em" }}>alt</Text>}</Table.Td>
      <Table.Td><Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={c.cls} size={30} /><Box style={{ minWidth: 0 }}><Text fw={600} c={CLASS_COLOURS[c.cls]} truncate>{c.label}</Text><Text size="xs" c="dimmed">{c.cls}</Text></Box></Group></Table.Td>
      <Table.Td><SpecCell meta={meta} cls={c.cls} spec={c.spec} role={c.role} /></Table.Td>
      <Table.Td>{c.offspec ? <SpecCell meta={meta} cls={c.cls} spec={c.offspec} role={c.off_role || ""} /> : <Text c="dimmed">—</Text>}</Table.Td>
      <Table.Td>{!c.name ? <Badge variant="outline" color="gray">planned</Badge> : c.confirmed ? <Badge variant="light" color="teal">confirmed</Badge> : <Button size="xs" loading={busy === `c${c.label}`} onClick={onConfirm}>Confirm</Button>}</Table.Td>
      <Table.Td><Tooltip label={`delete ${c.label}`}><ActionIcon variant="subtle" color="red" loading={busy === `d${c.label}`} onClick={onDelete}><IconTrash size={17} /></ActionIcon></Tooltip></Table.Td>
    </>
  );
}

function EditRow({ meta, d, uid, specsOf, onChange, onRemove }: { meta: Meta; d: Draft; uid: string; specsOf: (cls: string) => { value: string; label: string }[]; onChange: (p: Partial<Draft>) => void; onRemove: () => void }) {
  return (
    <>
      <Table.Td style={{ textAlign: "center" }}><Tooltip label="main"><Radio name={`main-${uid}`} checked={d.main} onChange={() => onChange({ main: true })} disabled={!d.cls} /></Tooltip></Table.Td>
      <Table.Td>
        <Stack gap={6}>
          {d.isNew
            ? <Select size="sm" placeholder="Class" value={d.cls || null} data={Object.keys(meta.classes)} onChange={(v) => onChange({ cls: v || "", spec: v ? Object.keys(meta.classes[v])[0] : "", offspec: null })} />
            : <Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={d.cls} size={30} /><Text fw={600} c={CLASS_COLOURS[d.cls]} truncate>{d.label}</Text></Group>}
          {(d.isNew || !d.named) && (
            <Group gap={6} wrap="nowrap" grow><TextInput size="sm" placeholder="First" maxLength={12} value={d.name} onChange={(e) => onChange({ name: e.currentTarget.value })} /><TextInput size="sm" placeholder="Last" maxLength={12} value={d.surname} onChange={(e) => onChange({ surname: e.currentTarget.value })} /></Group>
          )}
        </Stack>
      </Table.Td>
      <Table.Td><Select size="sm" data={specsOf(d.cls)} value={d.spec || null} onChange={(v) => onChange({ spec: v || d.spec })} disabled={!d.cls} /></Table.Td>
      <Table.Td><Select size="sm" placeholder="none" data={specsOf(d.cls)} value={d.offspec} onChange={(v) => onChange({ offspec: v || null })} disabled={!d.cls} clearable /></Table.Td>
      <Table.Td><Text size="xs" c="dimmed">{d.isNew ? (d.name ? "confirm after save" : "planned until named") : ""}</Text></Table.Td>
      <Table.Td><Tooltip label={d.isNew ? "discard" : "delete on save"}><ActionIcon variant="subtle" color="red" onClick={onRemove}><IconTrash size={17} /></ActionIcon></Tooltip></Table.Td>
    </>
  );
}

function SpecCell({ meta, cls, spec, role }: { meta: Meta; cls: string; spec: string; role: string }) {
  return <Group gap={8} wrap="nowrap"><GameIcon meta={meta} kind="spec" id={`${cls}:${spec}`} title={`${spec} (${role})`} /><Text size="sm">{spec}</Text><Text size="xs" c="dimmed">{role}</Text></Group>;
}
