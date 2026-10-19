import { useEffect, useMemo, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, Select, Stack, Switch, Table, Text, TextInput, Title, Tooltip, Modal } from "@mantine/core";
import css from "./me.module.css";
import { DateInput } from "@mantine/dates";
import { IconCrown, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, type Character, type Me, type Meta, type MyRegular, type MySheet } from "../api";
import { AskCard, NoticesCard } from "../components/AskCard";
import type { ConfirmOpts, ConfirmResult } from "../components/ConfirmModal";
import { GameIcon } from "../components/Icons";
import { CharacterCell, SpecCell } from "../components/Cells";
import { classColour } from "../theme";
import { CardHeader, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { MyLootCard, WishlistCard } from "../components/loot/MyLoot";

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; slot: "main" | "alt"; isNew?: boolean };


export function MePage({ meta }: { meta: Meta }) {
  const [me, setMe] = useState<Me | null>(null);
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [absOpen, setAbsOpen] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<Me>("/api/me").then(setMe).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(() => { if (!editing) load(); });
  useLive(() => { if (!editing) load(); }, ["run:", "member"]);

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
    const rosters = c.rosters.length ? `It comes off ${c.rosters.length === 1 ? "the " + c.rosters[0] + " roster" : `${c.rosters.length} rosters`}; sheets you answered with it lose that answer.` : "Sheets you answered with it lose that answer.";
    if (!(await ask({ title: `Delete ${c.label}?`, message: `${c.cls} ${c.spec}${c.is_main ? " — your main" : ""}. ${rosters} Officers keep the history.`, confirmLabel: "Delete", color: "red" }))) return;
    try { const r = await api.post<{ message: string }>("/api/me/character/delete", { label: c.label }); ok(r.message); load(); } catch (e) { fail(e); }
  }
  async function stepDown(r: MyRegular) {
    if (!(await ask({ title: `Step down from ${r.label}?`, message: `You stop being one of its regulars (${r.character}): the bot no longer expects your answer or reminds you. You can still Join any of its sheets. To come back, ask an officer.`, confirmLabel: "Step down", color: "red" }))) return;
    try { const x = await api.post<{ message: string }>("/api/me/regular/remove", { series: r.series }); ok(x.message); load(); } catch (e) { fail(e); }
  }
  async function setDm(on: boolean) { try { await api.post("/api/me/dm", { on }); ok(on ? "DMs on" : "DMs off"); load(); } catch (e) { fail(e); } }
  async function clearAbs(a: Me["absences"][number]) {
    const span = a.span || `${a.start}${a.end !== a.start ? ` → ${a.end}` : ""}`;  // the days in words from the server (the clock rule)
    if (!(await ask({ title: "Back after all?", message: `Your absence ${span} is cleared and the sheets on those days re-open for you — your No thanks on them goes; answer them again if you can come.`, confirmLabel: "I'm back" }))) return;
    try { const r = await api.post<{ message: string }>("/api/me/absence/clear", { start: a.start }); ok(r.message); load(); } catch (e) { fail(e); }
  }

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
          {!editing && !me.must_apply && <Button variant="default" leftSection={<IconPencil size={15} />} onClick={startEdit}>Edit characters</Button>}
        </Group>
      </Group>
      {me.must_apply && (
        <Card withBorder>
          <Text fw={700}>New here? Apply in Discord first.</Text>
          <Text size="sm" c="dimmed" mt={4}>Press <b>Apply to join</b> on the registration card, or use <code>/apply</code>. Once an officer accepts you, your character shows up here.</Text>
        </Card>
      )}

      {/* the inbox (design B2 / D26): every open ask wherever it waits, then the notices — outcomes are told here, never by DM */}
      {me.asks.map((a) => <AskCard key={`${a.kind}:${a.id}`} a={a} meta={meta} onDone={load} />)}
      <NoticesCard notices={me.notices || []} onDone={load} />

      <div className={css.layout}>
        <Stack gap="lg" style={{ minWidth: 0 }}>
          <Card>
            <CardHeader title="Characters" hint={`${meta.game} names are first + last · the crown marks your main`} />
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
          {!me.must_apply && me.characters.length > 0 && <WishlistCard meta={meta} />}
        </Stack>

        <Stack gap="lg" style={{ minWidth: 0 }}>
          {(me.regulars || []).length > 0 && (
            <Card>
              <CardHeader title="My regular raids" hint="you're expected on every sheet of these" />
              <Stack p="md" gap="xs">
                {me.regulars!.map((r) => (
                  <Group key={r.series} justify="space-between" wrap="nowrap">
                    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                      <GameIcon meta={meta} kind="spec" id={`${r.cls}:${r.spec}`} size={20} title={`${r.spec} · ${r.role}`} />
                      <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{r.label}</Text><Text size="xs" c={classColour(meta, r.cls)}>{r.character}</Text></Box>
                    </Group>
                    <Button size="xs" variant="subtle" color="red" onClick={() => stepDown(r)}>Step down</Button>
                  </Group>
                ))}
                <Text size="xs" c="dimmed">Nothing is answered for you: press Join or No thanks on each sheet (an absence marks you away). Want to become a regular somewhere? Ask an officer.</Text>
              </Stack>
            </Card>
          )}
          <Card>
            <CardHeader title="My sheets" />
            <Stack p="md" gap="xs">
              {me.sheets.length === 0 && <Text c="dimmed" size="sm">No live raids.</Text>}
              {me.sheets.map((s) => <SheetRow key={s.key} s={s} me={me} meta={meta} ask={ask} onDone={load} />)}
              {me.sheets.length > 0 && <Text size="xs" c="dimmed">Answer here or on the sheet in Discord — the same answer either way. Once the roster locks you confirm {me.dm ? "by DM or here" : "here — your DMs are off"}.</Text>}
            </Stack>
          </Card>
          {!me.must_apply && <MyLootCard meta={meta} />}
          <Card>
            <CardHeader title="Absences" action={<Button size="xs" variant="default" leftSection={<IconPlus size={13} />} onClick={() => setAbsOpen(true)}>Add</Button>} />
            <Stack p="md" gap="xs">
              {me.absences.length === 0 && <Text c="dimmed" size="sm">None upcoming.</Text>}
              {me.absences.map((a) => (
                <Group key={a.start} justify="space-between">
                  <Text size="sm">{a.span || `${a.start}${a.end !== a.start ? ` → ${a.end}` : ""}`}</Text>
                  <Group gap="xs">{a.reason && <Badge variant="outline" color="gray">{a.reason}</Badge>}<Tooltip label="I'm back after all"><ActionIcon variant="subtle" color="gray" size="sm" onClick={() => clearAbs(a)}><IconTrash size={14} /></ActionIcon></Tooltip></Group>
                </Group>
              ))}
            </Stack>
          </Card>
          <Card>
            <CardHeader title="Direct messages" action={<Switch checked={me.dm} onChange={(e) => setDm(e.currentTarget.checked)} />} />
            <Text p="md" size="sm" c="dimmed">{(meta.dm_sentences ? (me.dm ? meta.dm_sentences.on : meta.dm_sentences.off) : me.dm_sentence) || ""}</Text>
          </Card>
        </Stack>
      </div>

      <AbsenceModal opened={absOpen} onClose={() => setAbsOpen(false)} onSaved={() => { setAbsOpen(false); load(); }} />
      {confirmDialog}
    </Stack>
  );

  function upd(i: number, patch: Partial<Draft>) { setDrafts(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d))); }
}

/** One live sheet, answerable here (design B1): Join · Bench · No thanks while the sheet is open (the current answer filled), a
 *  character select when you have more than one, and — on a locked run you joined or are rostered on — "Can't make it…" with an
 *  optional note for the officers. Rendered from the server's state only: every button is disabled while a press is in flight,
 *  nothing flips before the reply, the reply's receipt is the toast and the page reloads (two devices: the last press wins). An
 *  ad hoc run you joined asks once before a change away from Join (the queue). */
function SheetRow({ s, me, meta, ask, onDone }: { s: MySheet; me: Me; meta: Meta; ask: (o: ConfirmOpts) => Promise<ConfirmResult>; onDone: () => void }) {
  const chars = me.characters;  // the active ones (planned by label), main first on the server
  const main = chars.find((c) => c.is_main) || chars[0];
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const character = pick || s.character || main?.label || null;
  async function answer(status: string) {
    if (s.queue_note && status !== "in" && !(await ask({ title: `${meta.labels[status] || status} on ${s.raid}?`, message: s.queue_note, confirmLabel: meta.labels[status] || status }))) return;
    setBusy(status);
    try {
      const r = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/answer`, { status, character: chars.length > 1 ? character : null });
      ok(r.message);
    } catch (e) { fail(e); } finally { setBusy(null); setPick(null); onDone(); }
  }
  async function cant() {
    const r = await ask({ title: `Can't make ${s.raid} · ${s.when}?`, message: s.rostered ? "Your seat is freed and the bench is asked; the officers see it as a callout." : "You go to No thanks and the officers see it as a callout.",
      confirmLabel: "Can't make it", color: "red", note: { label: "Note for the officers (optional — never on the public sheet)", maxLength: 300 } });
    if (!r) return;
    setBusy("cant");
    try {
      const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/out`, { note: typeof r === "object" ? r.note : "" });
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  return (
    <Stack gap={4} py={4} style={{ borderBottom: "1px solid var(--mantine-color-slate-6)" }}>
      <Group justify="space-between" wrap="nowrap">
        <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{s.raid}{s.kind === "adhoc" ? " · ad hoc" : ""}</Text><Text size="xs" c="dimmed">{s.when}{s.character ? ` · ${s.character}` : ""}{s.regular && !s.status ? " · you're a regular: answer it" : ""}</Text></Box>
        <Badge variant="light" color={s.rostered ? "teal" : s.status === "in" ? "teal" : s.status === "out" ? "red" : s.status === "sub" ? "yellow" : "gray"}>{s.rostered ? `rostered${s.roster && s.roster > 1 ? ` · roster ${s.roster}` : ""}` : s.label || "not answered"}</Badge>
      </Group>
      {s.may_answer && (
        <Group gap={6} wrap="wrap">
          {chars.length > 1 && (
            <Select size="xs" w={150} aria-label="as which character" allowDeselect={false} value={character} disabled={busy !== null}
              data={chars.map((c) => ({ value: c.label, label: `${c.label}${c.is_main ? " (main)" : ""}` }))} onChange={(v) => setPick(v)} />
          )}
          {meta.statuses.map((st) => (
            <Button key={st} size="compact-sm" variant={s.status === st && (!pick || pick === s.character) ? "filled" : "default"} color={st === "out" ? "red" : st === "sub" ? "yellow" : "teal"}
              loading={busy === st} disabled={busy !== null || (s.status === st && (!pick || pick === s.character))} onClick={() => answer(st)}>{meta.labels[st] || st}</Button>
          ))}
        </Group>
      )}
      {s.may_cant && <Group gap={6}><Button size="compact-sm" variant="subtle" color="red" loading={busy === "cant"} disabled={busy !== null} onClick={cant}>Can't make it…</Button></Group>}
      {s.receipt && s.status && <Text size="xs" c="dimmed">{s.receipt}</Text>}
    </Stack>
  );
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
        <TextInput label="Reason" description="only you and the officers see it" value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Button disabled={!start} onClick={() => api.post("/api/me/absence", { start: iso(start), end: iso(end), reason }).then(() => { ok("absence added"); onSaved(); setStart(null); setEnd(null); setReason(""); }).catch(fail)}>Add</Button></Group>
      </Stack>
    </Modal>
  );
}

