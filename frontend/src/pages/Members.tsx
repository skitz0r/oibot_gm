import { Fragment, useEffect, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, Modal, Radio, Select, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { DateInput } from "@mantine/dates";
import { IconCrown, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, type Character, type MemberRow, type Members as MembersData, type Meta } from "../api";
import { GameIcon } from "../components/Icons";
import { PageTitle, fail, ok } from "../components/Page";
import { CLASS_COLOURS } from "../theme";

const PRIV: Record<string, string> = { owner: "yellow", officer: "teal", member: "gray", outside: "red" };

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; main: boolean; named: boolean; isNew?: boolean };
type MemberDraft = { characters: Draft[]; deletes: string[] };

export function MembersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<MembersData | null>(null);
  const [absFor, setAbsFor] = useState<MemberRow | null>(null);
  const [drafts, setDrafts] = useState<Record<string, MemberDraft> | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<MembersData>("/api/members").then(setData).catch(fail);
  useEffect(() => { load(); }, []);

  const specsOf = (cls: string) => Object.entries(meta.classes[cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }));

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
      const rows = Object.entries(drafts).map(([uid, d]) => ({ uid, characters: d.characters, deletes: d.deletes }));
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
      <PageTitle title="Members" intro={`${data.rows.length} members with characters · ${data.members} in the registry · owner and officer badges come from Discord roles · absences pre-fill No thanks on the sheets they overlap`}
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
                const chars = d ? d.characters : r.characters;
                const span = chars.length + (editing ? 1 : 0);
                const memberCell = (
                  <Table.Td rowSpan={span} style={{ verticalAlign: "top", borderRight: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-7)" }}>
                    <Group gap={8} wrap="nowrap"><Text fw={700} truncate>{r.display_name}</Text><Badge size="xs" variant="outline" color={PRIV[r.privilege] || "gray"}>{r.privilege}</Badge></Group>
                    <Text size="xs" c="dimmed">{r.verification}</Text>
                    {r.asks.map((a) => <Badge key={a.roster} size="xs" variant="light" color="yellow" mt={4}>confirm {a.roster}?</Badge>)}
                    <Group gap={4} mt={6} wrap="wrap">
                      {r.absences.map((a) => (
                        <Tooltip key={a.start} label={a.reason ? `${a.reason} · click to clear` : "click to clear"}>
                          <Badge size="xs" variant="outline" color="gray" style={{ cursor: "pointer" }} onClick={() => confirm(`Clear ${r.display_name}'s absence from ${a.start}?`) && act(`ca${r.uid}${a.start}`, "/api/members/absence/clear", { uid: r.uid, start: a.start })}>away {a.start}{a.end !== a.start ? ` → ${a.end}` : ""} ×</Badge>
                        </Tooltip>
                      ))}
                      <Button size="compact-xs" variant="subtle" color="gray" onClick={() => setAbsFor(r)}>+ away</Button>
                    </Group>
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
                  </Fragment>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {editing && (
          <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
            <Text size="sm" c="dimmed" mr="auto">Names, specs, mains, adds and deletes apply on save</Text>
            <Button variant="default" size="sm" onClick={() => setDrafts(null)}>Cancel</Button>
            <Button size="sm" loading={busy === "save"} onClick={save}>Save changes</Button>
          </Group>
        )}
      </Card>
      <AbsenceModal r={absFor} onClose={() => setAbsFor(null)} onSaved={() => { setAbsFor(null); load(); }} />
    </Stack>
  );
}

function AbsenceModal({ r, onClose, onSaved }: { r: MemberRow | null; onClose: () => void; onSaved: () => void }) {
  const [start, setStart] = useState<Date | null>(null);
  const [end, setEnd] = useState<Date | null>(null);
  const [reason, setReason] = useState("");
  const iso = (d: Date | null) => (d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` : null);
  return (
    <Modal opened={!!r} onClose={onClose} title={r ? `${r.display_name} is away` : ""} centered>
      <Stack>
        <DateInput label="From" value={start} onChange={(v) => setStart(v ? new Date(v) : null)} required />
        <DateInput label="To" value={end} onChange={(v) => setEnd(v ? new Date(v) : null)} description="leave empty for a single day" />
        <TextInput label="Reason" description="officers only" value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Text size="xs" c="dimmed">Sheets on those days get No thanks for them; if they're already seated the seat is handed back and the bench is asked.</Text>
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Button disabled={!start} onClick={() => r && api.post<{ message: string }>("/api/members/absence", { uid: r.uid, start: iso(start), end: iso(end), reason }).then((x) => { ok(x.message); onSaved(); setStart(null); setEnd(null); setReason(""); }).catch(fail)}>Add</Button></Group>
      </Stack>
    </Modal>
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
