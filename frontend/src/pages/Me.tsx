import { useEffect, useMemo, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, Select, Stack, Switch, Table, Text, TextInput, Title, Tooltip, Modal } from "@mantine/core";
import css from "./me.module.css";
import { DateInput } from "@mantine/dates";
import { IconCrown, IconPencil, IconPlus, IconRefresh, IconTrash } from "@tabler/icons-react";
import { api, type Character, type Me, type Meta } from "../api";
import { GameIcon } from "../components/Icons";
import { CharacterCell, SpecCell } from "../components/Cells";
import { classColour } from "../theme";
import { CardHeader, fail, ok } from "../components/Page";
import { usePoll } from "../hooks/usePoll";

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; slot: "main" | "alt"; isNew?: boolean };


export function MePage({ meta }: { meta: Meta }) {
  const [me, setMe] = useState<Me | null>(null);
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [absOpen, setAbsOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const load = () => api.get<Me>("/api/me").then(setMe).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(() => { if (!editing) load(); });
  async function refresh() { setRefreshing(true); try { await load(); } finally { setRefreshing(false); } }

  const specsOf = (cls: string) => Object.entries(meta.classes[cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }));

  function startEdit() {
    if (!me) return;
    setDrafts(me.characters.map((c) => ({ label: c.label, cls: c.cls, spec: c.spec, offspec: c.offspec, name: c.name || "", surname: c.surname || "", slot: c.is_main ? "main" : "alt" })));
    setEditing(true);
  }
  async function save() {
    try {
      const r = await api.post<{ message: string }>("/api/me/characters", { rows: drafts });
      ok(r.message); setEditing(false); load();
    } catch (e) { fail(e); }
  }
  async function makeMain(c: Character) { try { const r = await api.post<{ message: string }>("/api/me/main", { label: c.label }); ok(r.message); load(); } catch (e) { fail(e); } }
  async function del(c: Character) {
    if (!confirm(`Delete ${c.label}? It is removed from every roster.`)) return;
    try { const r = await api.post<{ message: string }>("/api/me/character/delete", { label: c.label }); ok(r.message); load(); } catch (e) { fail(e); }
  }
  async function setDm(on: boolean) { try { await api.post("/api/me/dm", { on }); ok(on ? "DMs on" : "DMs off"); load(); } catch (e) { fail(e); } }
  async function clearAbs(start: string) { try { await api.post("/api/me/absence/clear", { start }); ok("absence cleared"); load(); } catch (e) { fail(e); } }

  const summary = useMemo(() => {
    if (!me) return "";
    const n = me.characters.length;
    const sheet = me.sheets.find((s) => s.rostered) || me.sheets.find((s) => s.status === "in");
    return [`${n} character${n === 1 ? "" : "s"}`, me.roles.primary || null, sheet ? `${sheet.raid} · ${sheet.when} — ${sheet.rostered ? "you're rostered" : "joined"}` : null].filter(Boolean).join(" · ");
  }, [me]);

  if (!me) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start">
        <Box><Title order={1} size="h2">{me.display_name}</Title><Text c="dimmed" size="sm" mt={4}>{summary}</Text></Box>
        <Group gap="xs">
          {!editing && <Button variant="default" leftSection={<IconPencil size={15} />} onClick={startEdit}>Edit characters</Button>}
          <Tooltip label="Refresh"><ActionIcon variant="default" size="lg" aria-label="refresh" loading={refreshing} onClick={refresh}><IconRefresh size={16} /></ActionIcon></Tooltip>
        </Group>
      </Group>

      {me.asks.map((a) => (
        <Card key={a.roster} style={{ borderColor: "var(--mantine-color-teal-4)" }}>
          <Group p="md" justify="space-between">
            <Text>You're rostered for <b>{a.raid}{a.when ? ` · ${a.when}` : ""}</b> as <b>{a.character}</b> — confirm to keep the seat, or hand it back.</Text>
            <Group gap="xs">
              <Button size="xs" onClick={() => api.post("/api/me/placement", { roster: a.roster, answer: "yes" }).then(load).catch(fail)}>Confirm</Button>
              <Button size="xs" variant="default" onClick={() => api.post("/api/me/placement", { roster: a.roster, answer: "no" }).then(load).catch(fail)}>Can't make it</Button>
            </Group>
          </Group>
        </Card>
      ))}

      <div className={css.layout}>
        <Stack gap="lg" style={{ minWidth: 0 }}>
          <Card>
            <CardHeader title="Characters" hint="Forever names are first + last · the crown marks your main" />
            <Table.ScrollContainer minWidth={640}>
              <Table style={{ tableLayout: "fixed" }}>
                <Table.Thead><Table.Tr><Table.Th w={editing ? 84 : 44} /><Table.Th>Character</Table.Th><Table.Th w={editing ? "27%" : "22%"}>Spec</Table.Th><Table.Th w={editing ? "27%" : "22%"}>Offspec</Table.Th>{!editing && <Table.Th w="18%">Status</Table.Th>}<Table.Th w={48} /></Table.Tr></Table.Thead>
                <Table.Tbody>
                  {!editing && me.characters.map((c) => (
                    <Table.Tr key={c.label}>
                      <Table.Td>
                        {c.is_main
                          ? <Tooltip label="your main"><IconCrown size={18} color="var(--mantine-color-yellow-5)" /></Tooltip>
                          : <Tooltip label={`make ${c.label} your main`}><ActionIcon variant="subtle" color="gray" onClick={() => makeMain(c)}><IconCrown size={18} opacity={0.35} /></ActionIcon></Tooltip>}
                      </Table.Td>
                      <Table.Td><CharacterCell meta={meta} cls={c.cls} label={c.label} size={34} /></Table.Td>
                      <Table.Td><SpecCell meta={meta} cls={c.cls} spec={c.spec} role={c.role} /></Table.Td>
                      <Table.Td>{c.offspec ? <SpecCell meta={meta} cls={c.cls} spec={c.offspec} role={c.off_role || ""} /> : <Text c="dimmed">—</Text>}</Table.Td>
                      <Table.Td>{!c.name ? <Badge variant="outline" color="gray">planned</Badge> : c.confirmed ? <Badge variant="light" color="teal">confirmed</Badge> : <Badge variant="light" color="yellow">unconfirmed</Badge>}</Table.Td>
                      <Table.Td><Tooltip label={`delete ${c.label}`}><ActionIcon variant="subtle" color="red" onClick={() => del(c)}><IconTrash size={17} /></ActionIcon></Tooltip></Table.Td>
                    </Table.Tr>
                  ))}
                  {editing && drafts.map((d, i) => (
                    <Table.Tr key={d.label ?? `new${i}`} style={{ background: "var(--mantine-color-slate-6)" }}>
                      <Table.Td>{d.isNew ? <Select size="xs" w={68} value={d.slot} data={[{ value: "main", label: "main" }, { value: "alt", label: "alt" }]} onChange={(v) => upd(i, { slot: (v as "main" | "alt") || "alt" })} /> : d.slot === "main" ? <IconCrown size={18} color="var(--mantine-color-yellow-5)" /> : null}</Table.Td>
                      <Table.Td>
                        <Stack gap={6}>
                          {d.isNew
                            ? <Select size="sm" placeholder="Class" value={d.cls || null} data={Object.keys(meta.classes)} onChange={(v) => upd(i, { cls: v || "", spec: v ? Object.keys(meta.classes[v])[0] : "", offspec: null })} />
                            : <Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={d.cls} size={34} title={d.cls} /><Text fw={600} c={classColour(meta, d.cls)}>{d.label}</Text></Group>}
                          {(d.isNew || !me.characters.find((c) => c.label === d.label)?.name) && (
                            <Group gap={6} wrap="nowrap" grow><TextInput size="sm" placeholder="First" maxLength={12} value={d.name} onChange={(e) => upd(i, { name: e.currentTarget.value })} /><TextInput size="sm" placeholder="Last" maxLength={12} value={d.surname} onChange={(e) => upd(i, { surname: e.currentTarget.value })} /></Group>
                          )}
                          {d.isNew && <Text size="xs" c="dimmed">{d.name ? "an officer confirms it after save" : "no name yet = planned"}</Text>}
                        </Stack>
                      </Table.Td>
                      <Table.Td><Select size="sm" data={specsOf(d.cls)} value={d.spec || null} onChange={(v) => upd(i, { spec: v || d.spec })} disabled={!d.cls} /></Table.Td>
                      <Table.Td><Select size="sm" placeholder="none" data={specsOf(d.cls)} value={d.offspec} onChange={(v) => upd(i, { offspec: v || null })} disabled={!d.cls} clearable /></Table.Td>
                      <Table.Td>{d.isNew && <ActionIcon variant="subtle" color="red" onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}><IconTrash size={17} /></ActionIcon>}</Table.Td>
                    </Table.Tr>
                  ))}
                  {editing && (
                    <Table.Tr><Table.Td /><Table.Td colSpan={4}><Button size="xs" variant="subtle" leftSection={<IconPlus size={14} />} onClick={() => setDrafts([...drafts, { label: null, cls: "", spec: "", offspec: null, name: "", surname: "", slot: me.characters.length ? "alt" : "main", isNew: true }])}>Add a character</Button></Table.Td></Table.Tr>
                  )}
                  {!editing && me.characters.length === 0 && <Table.Tr><Table.Td colSpan={6}><Text c="dimmed">No characters yet — press <b>Edit characters</b> and add your main.</Text></Table.Td></Table.Tr>}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            {editing && (
              <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
                <Text size="sm" c="dimmed" mr="auto">Edits apply on save · adding with names blank keeps the character planned</Text>
                <Button variant="default" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
                <Button size="sm" onClick={save}>Save changes</Button>
              </Group>
            )}
          </Card>

        </Stack>

        <Stack gap="lg" style={{ minWidth: 0 }}>
          <Card>
            <CardHeader title="My sheets" />
            <Stack p="md" gap="xs">
              {me.sheets.length === 0 && <Text c="dimmed" size="sm">No live raids.</Text>}
              {me.sheets.map((s) => (
                <Group key={s.key} justify="space-between" wrap="nowrap">
                  <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{s.raid}</Text><Text size="xs" c="dimmed">{s.when}{s.character ? ` · ${s.character}` : ""}</Text></Box>
                  <Badge variant="light" color={s.rostered ? "teal" : s.status === "in" ? "teal" : s.status === "out" ? "red" : s.status === "sub" ? "yellow" : "gray"}>{s.rostered ? `rostered${s.roster && s.roster > 1 ? ` · roster ${s.roster}` : ""}` : s.label || "not answered"}</Badge>
                </Group>
              ))}
              {me.sheets.length > 0 && <Text size="xs" c="dimmed">Answer on the sheet in Discord — Join / Bench / No thanks. Once the roster locks you confirm by DM.</Text>}
            </Stack>
          </Card>
          <Card>
            <CardHeader title="Absences" action={<Button size="xs" variant="default" leftSection={<IconPlus size={13} />} onClick={() => setAbsOpen(true)}>Add</Button>} />
            <Stack p="md" gap="xs">
              {me.absences.length === 0 && <Text c="dimmed" size="sm">None upcoming.</Text>}
              {me.absences.map((a) => (
                <Group key={a.start} justify="space-between">
                  <Text size="sm" ff="monospace">{a.start}{a.end !== a.start ? ` → ${a.end}` : ""}</Text>
                  <Group gap="xs">{a.reason && <Badge variant="outline" color="gray">{a.reason}</Badge>}<ActionIcon variant="subtle" color="gray" size="sm" onClick={() => clearAbs(a.start)}><IconTrash size={14} /></ActionIcon></Group>
                </Group>
              ))}
            </Stack>
          </Card>
          <Card>
            <CardHeader title="Direct messages" action={<Switch checked={me.dm} onChange={(e) => setDm(e.currentTarget.checked)} />} />
            <Text p="md" size="sm" c="dimmed">Sheets, nudges and fill requests arrive as DMs from the bot. Off, and officers reach you in the channel instead.</Text>
          </Card>
        </Stack>
      </div>

      <AbsenceModal opened={absOpen} onClose={() => setAbsOpen(false)} onSaved={() => { setAbsOpen(false); load(); }} />
    </Stack>
  );

  function upd(i: number, patch: Partial<Draft>) { setDrafts(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d))); }
}

function AbsenceModal({ opened, onClose, onSaved }: { opened: boolean; onClose: () => void; onSaved: () => void }) {
  const [start, setStart] = useState<Date | null>(null);
  const [end, setEnd] = useState<Date | null>(null);
  const [reason, setReason] = useState("");
  const iso = (d: Date | null) => (d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` : null);
  return (
    <Modal opened={opened} onClose={onClose} title="Add an absence" centered>
      <Stack>
        <DateInput label="From" value={start} onChange={(v) => setStart(v ? new Date(v) : null)} required />
        <DateInput label="To" value={end} onChange={(v) => setEnd(v ? new Date(v) : null)} description="leave empty for a single day" />
        <TextInput label="Reason" description="officers only" value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Button disabled={!start} onClick={() => api.post("/api/me/absence", { start: iso(start), end: iso(end), reason }).then(() => { ok("absence added"); onSaved(); setStart(null); setEnd(null); setReason(""); }).catch(fail)}>Add</Button></Group>
      </Stack>
    </Modal>
  );
}

