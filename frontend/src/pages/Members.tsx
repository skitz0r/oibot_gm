import { useRefresh } from "../hooks/usePoll";
import { useDirty } from "../hooks/useDirty";
import { useRef, Fragment, useEffect, useState } from "react";
import { ActionIcon, Badge, Button, Card, Collapse, Group, Menu, MultiSelect, Radio, Select, Stack, Switch, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconCrown, IconDots, IconMessage, IconMessageOff, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { useLive } from "../hooks/useLive";
import { api, ApiError, type Character, type MemberRow, type Members as MembersData, type Meta, type TableSaved } from "../api";
import { GameIcon } from "../components/Icons";
import { CharacterCell, SpecCell, StatusBadge } from "../components/Cells";
import { PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { TestBench } from "../components/TestBench";
import { AbsencesModal } from "../components/AbsencesModal";
import { AbsencesTable } from "../components/AbsencesTable";
import { LeftServer } from "../components/LeftServer";
import { useImBack } from "../components/AbsencesCard";
import { FlexIcons, flexChoices } from "../components/CharacterTable";
import { classColour } from "../theme";

const PRIV: Record<string, string> = { owner: "yellow", officer: "teal", member: "gray", outside: "red", test: "violet" };
const DM_OFF = "With DMs off, Confirm and fill asks wait on their Me page and the sheet; nothing is sent anywhere else.";
/** The member cell stays put while the table scrolls sideways (design §3.3 Web — Members). */
const STICKY = { position: "sticky" as const, left: 0, zIndex: 1, verticalAlign: "top" as const, borderRight: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-7)" };

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; main: boolean; named: boolean; flex: string[]; rank?: string; isNew?: boolean };
type MemberDraft = { characters: Draft[]; deletes: string[] };

/** The Members page (officers): every member's characters, rank (D25: the member's, on the main), DMs and absences.
 *  Every character write is POST /api/members/save — THE character verb (a seat on a retired / deleted character is
 *  handed back and the bench asked; the server's preview says so before the confirm) — with each member's base rev: a
 *  member changed elsewhere meanwhile is refused alone and stays drafted. */
export function MembersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<MembersData | null>(null);
  const [absFor, setAbsFor] = useState<MemberRow | null>(null);
  const [drafts, setDrafts] = useState<Record<string, MemberDraft> | null>(null);
  useDirty("members", drafts !== null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showTest, setShowTest] = useState(false);
  const [showBare, setShowBare] = useState(false);
  const [stale, setStale] = useState<Record<string, string>>({});
  const [absKey, setAbsKey] = useState(0);
  const [ask, confirmDialog] = useConfirm();
  const draftsRef = useRef<unknown>(null);
  // design §3.5 M43: "att 5/6" per member (officers; GET /api/members/attendance/all — the view's count, bench counts)
  const [att, setAtt] = useState<Record<string, { text: string | null; loot: string | null }>>({});
  const loadAtt = () => { if (meta.viewer.officer) api.get<{ window_days: number; rows: Record<string, { text: string | null; loot: string | null }> }>("/api/members/attendance/all").then((r) => setAtt(r.rows)).catch(() => setAtt({})); };
  const load = () => { loadAtt(); return api.get<MembersData>("/api/members").then(setData).catch(fail); };
  useEffect(() => { load(); }, []);
  useRefresh(load);
  useLive(() => { if (draftsRef.current === null) load(); }, ["member", "run:"]);  // never under an edit in progress
  const changed = () => { load(); setAbsKey((k) => k + 1); };
  const [back, backDialog, backBusy] = useImBack(changed);

  const specsOf = (cls: string) => Object.entries(meta.classes[cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }));
  const ranks = meta.ranks?.length ? meta.ranks : ["trial", "raider", "core", "alt", "social"];

  function startEdit() {
    if (!data) return;
    setStale({});
    setDrafts(Object.fromEntries(data.rows.map((r) => [r.uid, { characters: r.characters.map((c) => ({ label: c.label, cls: c.cls, spec: c.spec, offspec: c.offspec, name: c.name || "", surname: c.surname || "", main: c.is_main, named: !!c.name, flex: [...(c.flex || [])], rank: c.is_main ? (r.rank || c.rank) : undefined })), deletes: [] }])));
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
    if (!drafts || !data) return;
    setBusy("save");
    const revs = Object.fromEntries(data.rows.map((r) => [r.uid, r.characters_rev]));
    try {
      const rows = Object.entries(drafts).map(([uid, d]) => ({ uid, rev: revs[uid], deletes: d.deletes, characters: d.characters.map(({ rank, ...c }) => (c.main && rank ? { ...c, rank } : c)) }));
      const r = await api.post<TableSaved>("/api/members/save", { rows });
      const refused = r.refused || [];
      if (refused.length) {  // keep only the refused members drafted, marked; the rest saved
        ok(r.message);
        setStale(Object.fromEntries(refused.map((x) => [x.uid, x.error])));
        setDrafts(Object.fromEntries(Object.entries(drafts).filter(([uid]) => refused.some((x) => x.uid === uid))));
      } else { ok(r.message); setDrafts(null); setStale({}); }
      await load();
    } catch (e) {
      const refused = ((e as ApiError).body as TableSaved | undefined)?.refused;
      if (refused?.length) setStale(Object.fromEntries(refused.map((x) => [x.uid, x.error])));
      fail(e);
    } finally { setBusy(null); }
  }
  /** One member, one row through the verb: preview → confirm when it ripples (always for retire / delete) → save. */
  async function act(r: MemberRow, c: Character, row: Record<string, unknown>, title: string, confirmLabel: string, color?: string, always = false, intro = "") {
    setBusy(`${r.uid}${c.label}`);
    const body = (preview: boolean) => ({ rows: [{ uid: r.uid, rev: r.characters_rev, characters: [row] }], preview });
    try {
      const p = await api.post<TableSaved>("/api/members/save", body(true));
      if (p.refused?.length) throw new Error(p.refused[0].error);
      if (always || p.ripple.length) {
        if (!(await ask({ title, message: [intro, ...p.ripple.map((x) => x.replace(`${r.display_name}: `, ""))].filter(Boolean).join("\n") || "Nothing else changes.", confirmLabel, color }))) return;
      }
      const x = await api.post<TableSaved>("/api/members/save", body(false));
      if (x.refused?.length) throw new Error(x.refused[0].error);
      ok(x.message); await load();
    } catch (e) { fail(e); await load(); } finally { setBusy(null); }
  }
  const makeMain = (r: MemberRow, c: Character) => act(r, c, { label: c.label, main: true }, `Make ${c.label} ${r.display_name}'s main?`, "Make it the main", undefined, true, `The rank (${r.rank || "trial"}) moves with it.`);
  const confirmChar = (r: MemberRow, c: Character) => act(r, c, { label: c.label, confirm: true }, `Confirm ${c.label}?`, "Confirm");
  const retire = (r: MemberRow, c: Character) => act(r, c, { label: c.label, retire: true }, `Retire ${c.label}?`, "Retire", "orange", true, `${r.display_name}'s ${c.cls} ${c.spec}${c.is_main ? " — their main; their next character becomes main" : ""}. It leaves sheets and rosters; its history is kept.`);
  const del = (r: MemberRow, c: Character) => act(r, c, { label: c.label, delete: true }, `Delete ${c.label}?`, "Delete", "red", true, `${r.display_name}'s ${c.cls} ${c.spec}${c.is_main ? " — their main" : ""}. Deleting removes it outright; prefer Retire if they may come back to it.`);
  async function setRank(r: MemberRow, rank: string | null) {
    if (!rank || rank === r.rank) return;
    if (!(await ask({ title: `${r.display_name}'s rank`, message: `${r.rank || "—"} → ${rank}. Rank belongs to the player and lives on the main; alts always read alt.`, confirmLabel: "Set the rank" }))) return;
    setBusy(`rank${r.uid}`);
    try { const x = await api.post<{ message: string }>("/api/members/set", { uid: r.uid, rank }); ok(x.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function setDm(r: MemberRow, on: boolean) {
    if (!(await ask({ title: `DMs ${on ? "on" : "off"} for ${r.display_name}?`, message: on ? "The bot DMs them when they must act: a confirmation, a fill or pre-lock ask." : DM_OFF, confirmLabel: on ? "Turn DMs on" : "Turn DMs off" }))) return;
    setBusy(`dm${r.uid}`);
    try { const x = await api.post<{ message: string }>("/api/members/dm", { uid: r.uid, on }); ok(x.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  const editing = drafts !== null;
  draftsRef.current = drafts;
  const nTest = data.rows.filter((r) => r.privilege === "test").length;
  const visible = showTest || editing ? data.rows : data.rows.filter((r) => r.privilege !== "test");
  const rows = editing ? visible : visible.filter((r) => r.characters.length);  // editing: a characterless member can get one
  const bare = visible.filter((r) => !r.characters.length);

  const memberCell = (r: MemberRow, span: number) => (
    <Table.Td rowSpan={span} style={STICKY}>
      {/* the name has the first line to itself (a long one truncates, whole on hover); the privilege badge sits by the
          verification line below, whole — never "OFFI…" squeezed beside the name */}
      <Group gap={8} wrap="nowrap" justify="space-between">
        <Text fw={700} truncate title={r.display_name} style={{ minWidth: 0 }}>{r.display_name}</Text>
        {r.privilege !== "test" && (
          <Tooltip label={r.dm === false ? "DMs off — asks wait on their Me page" : "DMs on"}>
            <ActionIcon size="sm" variant="subtle" color={r.dm === false ? "red" : "gray"} loading={busy === `dm${r.uid}`} onClick={() => setDm(r, r.dm === false)} aria-label="DMs">
              {r.dm === false ? <IconMessageOff size={15} /> : <IconMessage size={15} />}
            </ActionIcon>
          </Tooltip>
        )}
      </Group>
      <Group gap={6} wrap="wrap"><Badge size="xs" variant="outline" color={PRIV[r.privilege] || "gray"} style={{ flexShrink: 0 }}>{r.privilege}</Badge><Text size="xs" c="dimmed">{r.verification}</Text></Group>
      {att[r.uid]?.text && <Tooltip label={att[r.uid].loot ? `${att[r.uid].loot} (rostered and confirmed: the loot factor's)` : "finished runs in the loot policy's window; the bench counts here"}><Text size="xs" c="dimmed">{att[r.uid].text}</Text></Tooltip>}
      {stale[r.uid] && <Text size="xs" c="red">{stale[r.uid]}</Text>}
      {r.asks.map((a) => <Badge key={a.roster} size="xs" variant="light" color="yellow" mt={4}>confirm {a.raid}{a.when ? ` · ${a.when}` : ""}?</Badge>)}
      <Group gap={4} mt={6} wrap="wrap">
        {r.absences.map((a) => (
          <Tooltip key={a.start} label={(a.reason ? `${a.reason} · ` : "") + "I'm back: clear it"}>
            <Badge size="xs" variant="outline" color="gray" style={{ cursor: "pointer", opacity: backBusy === `${r.uid}${a.start}` ? 0.5 : 1 }} onClick={() => back(a, { uid: r.uid, display_name: r.display_name })}>away {a.span || a.start} ×</Badge>
          </Tooltip>
        ))}
        <Button size="compact-xs" variant="subtle" color="gray" onClick={() => setAbsFor(r)}>+ away</Button>
      </Group>
    </Table.Td>
  );

  return (
    <Stack gap="lg">
      <PageTitle title="Members" intro={`${data.rows.length - nTest} members${nTest ? ` (and ${nTest} test members)` : ""} · ${data.members} in the registry · owner and officer badges come from Discord roles · rank lives on the main · someone away on a run's day is listed under Away on its sheet`}
        right={!editing ? <Group gap="md">{nTest > 0 && <Switch size="sm" label={`show ${nTest} test members`} checked={showTest} onChange={(e) => setShowTest(e.currentTarget.checked)} />}<Button variant="default" leftSection={<IconPencil size={15} />} onClick={startEdit}>Edit members</Button></Group> : null} />
      <Card>
        {/* viewing: icon columns sized to their icons so the table fits the 1360px page with the Character column
            readable; editing: the selects need the room, and the table scrolls sideways under the sticky member cell */}
        <Table.ScrollContainer minWidth={editing ? 1220 : 1040}>
          <Table style={{ tableLayout: "fixed" }}>
            <Table.Thead>
              <Table.Tr><Table.Th w={editing ? 230 : 220} style={{ ...STICKY, verticalAlign: undefined }}>Member</Table.Th><Table.Th w={editing ? 56 : 44} /><Table.Th>Character</Table.Th><Table.Th w={editing ? "11%" : 64}>Spec</Table.Th><Table.Th w={editing ? "11%" : 72}>Offspec</Table.Th><Table.Th w={editing ? "12%" : 96}>Flex</Table.Th><Table.Th w={editing ? 110 : 104}>Rank</Table.Th><Table.Th w={editing ? "15%" : 140}>Regular on</Table.Th><Table.Th w={editing ? 120 : 112}>{editing ? "" : "Status"}</Table.Th><Table.Th w={editing ? 48 : 44} /></Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((r) => {
                const d = drafts?.[r.uid];
                const chars = d ? d.characters : r.characters;
                const span = chars.length + (editing ? 1 : 0);
                return (
                  <Fragment key={r.uid}>
                    {chars.map((c, i) => (
                      <Table.Tr key={(c as Draft | Character).label ?? `new${i}`} style={{ borderTop: i === 0 ? "1px solid var(--mantine-color-slate-5)" : undefined, background: editing ? "var(--mantine-color-slate-6)" : undefined }}>
                        {i === 0 && memberCell(r, span)}
                        {!editing
                          ? <ViewRow meta={meta} r={r} c={c as Character} busy={busy} ranks={ranks} onRank={(v) => setRank(r, v)} onMain={() => makeMain(r, c as Character)} onConfirm={() => confirmChar(r, c as Character)} onRetire={() => retire(r, c as Character)} onDelete={() => del(r, c as Character)} />
                          : <EditRow meta={meta} d={c as Draft} uid={r.uid} ranks={ranks} specsOf={specsOf} onChange={(p) => updChar(r.uid, i, p)} onRemove={() => removeChar(r.uid, i)} regularOn={r.characters.find((x) => x.label === (c as Draft).label)?.regular_on} />}
                      </Table.Tr>
                    ))}
                    {editing && (
                      <Table.Tr style={{ background: "var(--mantine-color-slate-6)" }}>
                        {chars.length === 0 && memberCell(r, 1)}
                        <Table.Td /><Table.Td colSpan={8}><Button size="xs" variant="subtle" leftSection={<IconPlus size={14} />} onClick={() => upd(r.uid, { characters: [...(d?.characters ?? []), { label: null, cls: "", spec: "", offspec: null, name: "", surname: "", main: chars.length === 0, named: false, flex: [], isNew: true }] })}>Add a character for {r.display_name}</Button></Table.Td>
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
            <Text size="sm" c="dimmed" mr="auto">Names, specs, extra roles, mains, the rank (on the main), adds and deletes apply on save · a seat on a deleted character is handed back</Text>
            <Button variant="default" size="sm" onClick={() => { setDrafts(null); setStale({}); }}>Cancel</Button>
            <Button size="sm" loading={busy === "save"} onClick={save}>Save changes</Button>
          </Group>
        )}
      </Card>
      {bare.length > 0 && !editing && (
        <Card>
          <Group p="sm" px="md" gap="xs" style={{ cursor: "pointer" }} onClick={() => setShowBare(!showBare)}>
            {showBare ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
            <Text fw={600} size="sm">No characters ({bare.length})</Text>
            <Text size="xs" c="dimmed">on file without a character — absences and DMs still apply</Text>
          </Group>
          <Collapse expanded={showBare}>
            <Table><Table.Tbody>{bare.map((r) => <Table.Tr key={r.uid}>{memberCell(r, 1)}<Table.Td><Text size="sm" c="dimmed">no characters</Text></Table.Td></Table.Tr>)}</Table.Tbody></Table>
          </Collapse>
        </Card>
      )}
      {meta.viewer.officer && !editing && <AbsencesTable reloadKey={absKey} />}
      {meta.viewer.officer && !editing && <LeftServer />}
      {meta.viewer.officer && !editing && <TestBench meta={meta} rows={data.rows} onChanged={load} />}
      <AbsencesModal meta={meta} opened={!!absFor} who={absFor ? { uid: absFor.uid, display_name: absFor.display_name } : null} onClose={() => setAbsFor(null)} onSaved={() => { setAbsFor(null); changed(); }} />
      {confirmDialog}
      {backDialog}
    </Stack>
  );
}

function ViewRow({ meta, r, c, busy, ranks, onRank, onMain, onConfirm, onRetire, onDelete }: { meta: Meta; r: MemberRow; c: Character; busy: string | null; ranks: string[]; onRank: (v: string | null) => void; onMain: () => void; onConfirm: () => void; onRetire: () => void; onDelete: () => void }) {
  return (
    <>
      <Table.Td style={{ textAlign: "center" }}>{c.is_main ? <Tooltip label="main"><IconCrown size={18} color="var(--mantine-color-yellow-5)" /></Tooltip> : <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: ".06em" }}>alt</Text>}</Table.Td>
      <Table.Td><CharacterCell meta={meta} cls={c.cls} label={c.label} /></Table.Td>
      <Table.Td><SpecCell meta={meta} cls={c.cls} spec={c.spec} role={c.role} /></Table.Td>
      <Table.Td>{c.offspec ? <SpecCell meta={meta} cls={c.cls} spec={c.offspec} role={c.off_role || ""} /> : <Text c="dimmed">—</Text>}</Table.Td>
      <Table.Td><FlexIcons meta={meta} flex={c.flex} /></Table.Td>
      <Table.Td>{c.is_main
        ? <Select size="xs" data={ranks} value={r.rank || c.rank} onChange={onRank} allowDeselect={false} disabled={busy === `rank${r.uid}` || r.privilege === "test"} aria-label="rank" />
        : <Tooltip label="rank lives on the main"><Text size="xs" c="dimmed">alt</Text></Tooltip>}</Table.Td>
      <Table.Td><RegularOnCell on={c.regular_on} /></Table.Td>
      <Table.Td><StatusBadge named={!!c.name} confirmed={!!c.confirmed} /></Table.Td>
      <Table.Td>
        <Menu position="bottom-end" withinPortal>
          <Menu.Target><ActionIcon variant="subtle" color="gray" loading={busy === `${r.uid}${c.label}`} aria-label={`${c.label} actions`}><IconDots size={17} /></ActionIcon></Menu.Target>
          <Menu.Dropdown>
            {!c.is_main && <Menu.Item leftSection={<IconCrown size={14} />} onClick={onMain}>Make main</Menu.Item>}
            {c.name && !c.confirmed && <Menu.Item onClick={onConfirm}>Confirm</Menu.Item>}
            <Menu.Item onClick={onRetire}>Retire</Menu.Item>
            <Menu.Item color="red" leftSection={<IconTrash size={14} />} onClick={onDelete}>Delete</Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </Table.Td>
    </>
  );
}

/** The "Regular on" cell: the recurring schedules this character is a regular of, in words; officers change them on the Rosters page (or /roster add|remove). A bare dash under the column header when none. */
function RegularOnCell({ on }: { on?: string[] }) {
  if (!on || !on.length) return <Text c="dimmed">—</Text>;
  return <Group gap={4} wrap="wrap">{on.map((l) => <Badge key={l} size="sm" variant="light" color="teal" tt="none" style={{ maxWidth: "100%" }}><Text span size="xs" truncate>{l}</Text></Badge>)}</Group>;
}

function EditRow({ meta, d, uid, ranks, specsOf, onChange, onRemove, regularOn }: { meta: Meta; d: Draft; uid: string; ranks: string[]; specsOf: (cls: string) => { value: string; label: string }[]; onChange: (p: Partial<Draft>) => void; onRemove: () => void; regularOn?: string[] }) {
  const choices = d.cls ? flexChoices(meta, d.cls, d.spec, d.offspec) : [];
  return (
    <>
      <Table.Td style={{ textAlign: "center" }}><Tooltip label="main"><Radio name={`main-${uid}`} checked={d.main} onChange={() => onChange({ main: true })} disabled={!d.cls} /></Tooltip></Table.Td>
      <Table.Td>
        <Stack gap={6}>
          {d.isNew
            ? <Select size="sm" placeholder="Class" value={d.cls || null} data={Object.keys(meta.classes)} onChange={(v) => onChange({ cls: v || "", spec: v ? Object.keys(meta.classes[v])[0] : "", offspec: null, flex: [] })} />
            : <Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={d.cls} size={30} title={d.cls} /><Text fw={600} c={classColour(meta, d.cls)} truncate>{d.label}</Text></Group>}
          {(d.isNew || !d.named) && (
            <Group gap={6} wrap="nowrap" grow><TextInput size="sm" placeholder="First" maxLength={12} value={d.name} onChange={(e) => onChange({ name: e.currentTarget.value })} /><TextInput size="sm" placeholder="Last" maxLength={12} value={d.surname} onChange={(e) => onChange({ surname: e.currentTarget.value })} /></Group>
          )}
        </Stack>
      </Table.Td>
      <Table.Td><Select size="sm" data={specsOf(d.cls)} value={d.spec || null} onChange={(v) => onChange({ spec: v || d.spec })} disabled={!d.cls} /></Table.Td>
      <Table.Td><Select size="sm" placeholder="No offspec" data={specsOf(d.cls)} value={d.offspec} onChange={(v) => onChange({ offspec: v || null })} disabled={!d.cls} clearable /></Table.Td>
      <Table.Td><MultiSelect size="sm" placeholder="No extra roles" data={choices} value={d.flex.filter((r) => choices.includes(r))} onChange={(v) => onChange({ flex: v })} disabled={!d.cls} /></Table.Td>
      <Table.Td>{d.main ? <Select size="sm" data={ranks} value={d.rank || "trial"} onChange={(v) => onChange({ rank: v || undefined })} allowDeselect={false} /> : <Text size="xs" c="dimmed">alt</Text>}</Table.Td>
      <Table.Td><RegularOnCell on={regularOn} /></Table.Td>
      <Table.Td><Text size="xs" c="dimmed">{d.isNew ? (d.name ? "confirm after save" : "planned until named") : ""}</Text></Table.Td>
      <Table.Td><Tooltip label={d.isNew ? "discard" : "delete on save"}><ActionIcon variant="subtle" color="red" onClick={onRemove}><IconTrash size={17} /></ActionIcon></Tooltip></Table.Td>
    </>
  );
}
