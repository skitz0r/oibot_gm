import { useEffect, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Button, Card, Group, Menu, MultiSelect, Select, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconCrown, IconDots, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, ApiError, type Character, type Me, type Meta, type TableSaved } from "../api";
import { GameIcon } from "./Icons";
import { CharacterCell, SpecCell, StatusBadge } from "./Cells";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { classColour } from "../theme";

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; slot: "main" | "alt"; flex: string[]; isNew?: boolean };

/** The roles a character could ALSO play: every role its spec and offspec don't already give (the server's set_flex rule). */
export function flexChoices(meta: Meta, cls: string, spec: string, offspec: string | null): string[] {
  const own = new Set([meta.classes[cls]?.[spec], offspec ? meta.classes[cls]?.[offspec] : undefined].filter(Boolean) as string[]);
  return (meta.roles || []).filter((r) => !own.has(r));
}

/** A character's extra roles: role icons alone (the iconography rule — the tooltip carries the name). */
export function FlexIcons({ meta, flex }: { meta: Meta; flex?: string[] }) {
  if (!flex || !flex.length) return <Text c="dimmed">—</Text>;
  return <Group gap={4} wrap="nowrap">{flex.map((r) => <GameIcon key={r} meta={meta} kind="role" id={r} size={20} title={`could also play ${r}`} />)}</Group>;
}

/** The server's confirm words from a /api/me/characters preview (flat on the Me page). */
function sentencesOf(p: TableSaved): string[] {
  return (p.sentences || []).filter((x): x is string => typeof x === "string");
}

/** The Me page's characters card (design §3.3 cluster A, §5.58 phase 3): view with a row ⋮ menu (Make main · Retire ·
 *  Delete) and a Flex column; Edit for the whole table. Every write is POST /api/me/characters — THE character verb, one
 *  HTTP face — with the table's base `rev` (a change made elsewhere meanwhile is refused, 409, and the draft is kept).
 *  Every press asks the server first (`preview`) and the confirm prints ONLY its `sentences`: a row verb's are the
 *  people confirm in "your" voice (consequences.people_consequence, never empty), a Save's the ripple into the runs
 *  (save_consequence) — `[]` = nothing beyond the record changes, and the first press saves. `onEditing` tells the page
 *  not to reload under an edit. */
export function CharacterTable({ meta, me, onChanged, onEditing }: { meta: Meta; me: Me; onChanged: () => void; onEditing?: (editing: boolean) => void }) {
  const [editing, setEditing] = useState(false);
  useDirty("characters", editing);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, dialog] = useConfirm();
  useEffect(() => { onEditing?.(editing); }, [editing]);  // eslint-disable-line react-hooks/exhaustive-deps
  const specsOf = (cls: string) => Object.entries(meta.classes[cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }));

  function startEdit() {
    setDrafts(me.characters.map((c) => ({ label: c.label, cls: c.cls, spec: c.spec, offspec: c.offspec, name: c.name || "", surname: c.surname || "", slot: c.is_main ? "main" : "alt", flex: [...(c.flex || [])] })));
    setEditing(true);
  }
  function upd(i: number, patch: Partial<Draft>) { setDrafts(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d))); }
  function stale(e: unknown): boolean {
    if (e instanceof ApiError && e.status === 409) { fail(new Error(`${e.message} Your edit is kept — Cancel to see the latest.`)); return true; }
    return false;
  }
  /** Save the edited table: the server's preview first — its sentences (the ripple into the runs) → the confirm; [] → save. */
  async function save() {
    setBusy("save");
    try {
      const p = await api.post<TableSaved>("/api/me/characters", { rows: drafts, rev: me.characters_rev, preview: true });
      const said = sentencesOf(p);
      if (said.length && !(await ask({ title: "Save your characters?", message: said.join("\n"), confirmLabel: "Save changes" }))) return;
      const r = await api.post<TableSaved>("/api/me/characters", { rows: drafts, rev: me.characters_rev });
      ok(r.message); setEditing(false); onChanged();
    } catch (e) { if (!stale(e)) fail(e); } finally { setBusy(null); }
  }
  /** One row through the verb: the server's preview → its sentences in the confirm → save. */
  async function act(c: Character, row: Record<string, unknown>, title: string, confirmLabel: string, color?: string) {
    setBusy(c.label);
    try {
      const p = await api.post<TableSaved>("/api/me/characters", { rows: [row], rev: me.characters_rev, preview: true });
      const said = sentencesOf(p);
      if (said.length && !(await ask({ title, message: said.join("\n"), confirmLabel, color }))) return;
      const r = await api.post<TableSaved>("/api/me/characters", { rows: [row], rev: me.characters_rev });
      ok(r.message); onChanged();
    } catch (e) { if (stale(e)) onChanged(); else fail(e); } finally { setBusy(null); }
  }
  const makeMain = (c: Character) => act(c, { label: c.label, main: true }, `Make ${c.label} your main?`, "Make it my main");
  const retire = (c: Character) => act(c, { label: c.label, retire: true }, `Retire ${c.label}?`, "Retire", "orange");
  const del = (c: Character) => act(c, { label: c.label, delete: true }, `Delete ${c.label}?`, "Delete", "red");

  return (
    <Card>
      <CardHeader title="Characters" hint={me.rank ? `your rank is ${me.rank}` : undefined}
        action={!editing && !me.must_apply ? <Button size="xs" variant="default" leftSection={<IconPencil size={13} />} onClick={startEdit}>Edit</Button> : undefined} />
      <Table.ScrollContainer minWidth={680}>
        <Table style={{ tableLayout: "fixed" }}>
          <Table.Thead><Table.Tr><Table.Th w={editing ? 84 : 44} /><Table.Th>Character</Table.Th><Table.Th w={editing ? "22%" : "14%"}>Spec</Table.Th><Table.Th w={editing ? "22%" : "14%"}>Offspec</Table.Th><Table.Th w={editing ? "20%" : "14%"}>Flex</Table.Th>{!editing && <Table.Th w={120}>Status</Table.Th>}<Table.Th w={48} /></Table.Tr></Table.Thead>
          <Table.Tbody>
            {!editing && me.characters.map((c) => (
              <Table.Tr key={c.label}>
                <Table.Td>{c.is_main && <Tooltip label="your main"><IconCrown size={18} color="var(--mantine-color-slate-2)" /></Tooltip>}</Table.Td>
                <Table.Td><CharacterCell meta={meta} cls={c.cls} label={c.label} size={34} /></Table.Td>
                <Table.Td><SpecCell meta={meta} cls={c.cls} spec={c.spec} role={c.role} /></Table.Td>
                <Table.Td>{c.offspec ? <SpecCell meta={meta} cls={c.cls} spec={c.offspec} role={c.off_role || ""} /> : <Text c="dimmed">—</Text>}</Table.Td>
                <Table.Td><FlexIcons meta={meta} flex={c.flex} /></Table.Td>
                <Table.Td><StatusBadge named={!!c.name} confirmed={!!c.confirmed} /></Table.Td>
                <Table.Td>
                  <Menu position="bottom-end" withinPortal>
                    <Menu.Target><ActionIcon variant="subtle" color="gray" loading={busy === c.label} aria-label={`${c.label} actions`}><IconDots size={17} /></ActionIcon></Menu.Target>
                    <Menu.Dropdown>
                      {!c.is_main && <Menu.Item leftSection={<IconCrown size={14} />} onClick={() => makeMain(c)}>Make main</Menu.Item>}
                      <Menu.Item onClick={() => retire(c)}>Retire</Menu.Item>
                      <Menu.Item color="red" leftSection={<IconTrash size={14} />} onClick={() => del(c)}>Delete</Menu.Item>
                    </Menu.Dropdown>
                  </Menu>
                </Table.Td>
              </Table.Tr>
            ))}
            {editing && drafts.map((d, i) => (
              <Table.Tr key={d.label ?? `new${i}`} style={{ background: "var(--mantine-color-slate-6)" }}>
                <Table.Td>{d.isNew ? <Select size="xs" w={68} value={d.slot} data={[{ value: "main", label: "main" }, { value: "alt", label: "alt" }]} onChange={(v) => upd(i, { slot: (v as "main" | "alt") || "alt" })} /> : d.slot === "main" ? <IconCrown size={18} color="var(--mantine-color-slate-2)" /> : null}</Table.Td>
                <Table.Td>
                  <Stack gap={6}>
                    {d.isNew
                      ? <Select size="sm" placeholder="Class" value={d.cls || null} data={Object.keys(meta.classes)} onChange={(v) => upd(i, { cls: v || "", spec: v ? Object.keys(meta.classes[v])[0] : "", offspec: null, flex: [] })} />
                      : <Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={d.cls} size={34} title={d.cls} /><Text fw={600} c={classColour(meta, d.cls)} truncate>{d.label}</Text></Group>}
                    {(d.isNew || !me.characters.find((c) => c.label === d.label)?.name) && (
                      <Group gap={6} wrap="nowrap" grow><TextInput size="sm" placeholder="First" maxLength={12} value={d.name} onChange={(e) => upd(i, { name: e.currentTarget.value })} /><TextInput size="sm" placeholder="Last" maxLength={12} value={d.surname} onChange={(e) => upd(i, { surname: e.currentTarget.value })} /></Group>
                    )}
                    {d.isNew && <Text size="xs" c="dimmed">{d.name ? "A new character waits for an officer's confirm." : "Without a name it stays planned. Name it before the first raid so an officer can confirm it."}</Text>}
                  </Stack>
                </Table.Td>
                <Table.Td><Select size="sm" data={specsOf(d.cls)} value={d.spec || null} onChange={(v) => upd(i, { spec: v || d.spec })} disabled={!d.cls} /></Table.Td>
                <Table.Td><Select size="sm" placeholder="No offspec" data={specsOf(d.cls)} value={d.offspec} onChange={(v) => upd(i, { offspec: v || null })} disabled={!d.cls} clearable /></Table.Td>
                <Table.Td><MultiSelect size="sm" placeholder="No extra roles" data={d.cls ? flexChoices(meta, d.cls, d.spec, d.offspec) : []} value={d.flex.filter((r) => flexChoices(meta, d.cls, d.spec, d.offspec).includes(r))} onChange={(v) => upd(i, { flex: v })} disabled={!d.cls} /></Table.Td>
                <Table.Td>{d.isNew && <ActionIcon variant="subtle" color="red" onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}><IconTrash size={17} /></ActionIcon>}</Table.Td>
              </Table.Tr>
            ))}
            {editing && (
              <Table.Tr><Table.Td /><Table.Td colSpan={5}><Button size="xs" variant="subtle" leftSection={<IconPlus size={14} />} onClick={() => setDrafts([...drafts, { label: null, cls: "", spec: "", offspec: null, name: "", surname: "", slot: me.characters.length ? "alt" : "main", flex: [], isNew: true }])}>Add a character</Button></Table.Td></Table.Tr>
            )}
            {!editing && me.characters.length === 0 && <Table.Tr><Table.Td colSpan={7}><Text c="dimmed">No character yet. Press <b>Edit</b> and add your main.</Text></Table.Td></Table.Tr>}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {editing && (
        <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
          <Text size="sm" c="dimmed" mr="auto">Nothing changes until you save, and the bot says first what it does to your runs.</Text>
          <Button variant="default" size="sm" onClick={() => { setEditing(false); onChanged(); }}>Cancel</Button>
          <Button size="sm" loading={busy === "save"} onClick={save}>Save changes</Button>
        </Group>
      )}
      {dialog}
    </Card>
  );
}
