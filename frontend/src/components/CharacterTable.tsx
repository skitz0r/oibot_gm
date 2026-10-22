import { useEffect, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Badge, Button, Card, Group, Menu, MultiSelect, Select, Stack, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconCrown, IconDots, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, ApiError, type Character, type Me, type Meta, type TableSaved } from "../api";
import { GameIcon } from "./Icons";
import { CharacterCell, SpecCell } from "./Cells";
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

/** The Me page's characters card (design §3.3 cluster A): view with a row ⋮ menu (Make main · Retire · Delete) and a Flex
 *  column; Edit for the whole table. Every write is POST /api/me/characters — THE character verb, one HTTP face — with
 *  the table's base `rev` (a change made elsewhere meanwhile is refused, 409, and the draft is kept). A row action asks
 *  the server first (`preview`): its ripple sentences (a seat handed back, an answer cleared) are what the confirm says;
 *  with no ripple it just saves. `onEditing` tells the page not to reload under an edit. */
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
  async function save() {
    setBusy("save");
    try {
      const r = await api.post<TableSaved>("/api/me/characters", { rows: drafts, rev: me.characters_rev });
      ok(r.message); setEditing(false); onChanged();
    } catch (e) { if (!stale(e)) fail(e); } finally { setBusy(null); }
  }
  /** One row through the verb: preview → confirm when it ripples (always for a delete) → save. */
  async function act(c: Character, row: Record<string, unknown>, title: string, confirmLabel: string, color?: string, always = false, intro = "") {
    setBusy(c.label);
    try {
      const p = await api.post<TableSaved>("/api/me/characters", { rows: [row], rev: me.characters_rev, preview: true });
      if (always || p.ripple.length) {
        const message = [intro, ...p.ripple].filter(Boolean).join("\n") || "Nothing else changes.";
        if (!(await ask({ title, message, confirmLabel, color }))) return;
      }
      const r = await api.post<TableSaved>("/api/me/characters", { rows: [row], rev: me.characters_rev });
      ok(r.message); onChanged();
    } catch (e) { if (stale(e)) onChanged(); else fail(e); } finally { setBusy(null); }
  }
  const makeMain = (c: Character) => act(c, { label: c.label, main: true }, `Make ${c.label} your main?`, "Make it my main", undefined, false);
  const retire = (c: Character) => act(c, { label: c.label, retire: true }, `Retire ${c.label}?`, "Retire", "orange", true, `${c.cls} ${c.spec}${c.is_main ? " — your main; your next character becomes main" : ""}. It leaves sheets and rosters; its history is kept.`);
  const del = (c: Character) => act(c, { label: c.label, delete: true }, `Delete ${c.label}?`, "Delete", "red", true, `${c.cls} ${c.spec}${c.is_main ? " — your main" : ""}. Deleting removes it outright; prefer Retire if you may come back to it.`);

  return (
    <Card>
      <CardHeader title="Characters" hint={`${meta.game} names are first + last · the crown marks your main${me.rank ? ` · rank ${me.rank}` : ""}`}
        action={!editing && !me.must_apply ? <Button size="xs" variant="default" leftSection={<IconPencil size={13} />} onClick={startEdit}>Edit</Button> : undefined} />
      <Table.ScrollContainer minWidth={680}>
        <Table style={{ tableLayout: "fixed" }}>
          <Table.Thead><Table.Tr><Table.Th w={editing ? 84 : 44} /><Table.Th>Character</Table.Th><Table.Th w={editing ? "22%" : "14%"}>Spec</Table.Th><Table.Th w={editing ? "22%" : "14%"}>Offspec</Table.Th><Table.Th w={editing ? "20%" : "14%"}>Flex</Table.Th>{!editing && <Table.Th w="16%">Status</Table.Th>}<Table.Th w={48} /></Table.Tr></Table.Thead>
          <Table.Tbody>
            {!editing && me.characters.map((c) => (
              <Table.Tr key={c.label}>
                <Table.Td>{c.is_main ? <Tooltip label="your main"><IconCrown size={18} color="var(--mantine-color-yellow-5)" /></Tooltip> : <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: ".06em" }}>alt</Text>}</Table.Td>
                <Table.Td><CharacterCell meta={meta} cls={c.cls} label={c.label} size={34} /></Table.Td>
                <Table.Td><SpecCell meta={meta} cls={c.cls} spec={c.spec} role={c.role} /></Table.Td>
                <Table.Td>{c.offspec ? <SpecCell meta={meta} cls={c.cls} spec={c.offspec} role={c.off_role || ""} /> : <Text c="dimmed">—</Text>}</Table.Td>
                <Table.Td><FlexIcons meta={meta} flex={c.flex} /></Table.Td>
                <Table.Td>{!c.name ? <Badge variant="outline" color="gray">planned</Badge> : c.confirmed ? <Badge variant="light" color="teal">confirmed</Badge> : <Badge variant="light" color="yellow">unconfirmed</Badge>}</Table.Td>
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
                <Table.Td>{d.isNew ? <Select size="xs" w={68} value={d.slot} data={[{ value: "main", label: "main" }, { value: "alt", label: "alt" }]} onChange={(v) => upd(i, { slot: (v as "main" | "alt") || "alt" })} /> : d.slot === "main" ? <IconCrown size={18} color="var(--mantine-color-yellow-5)" /> : null}</Table.Td>
                <Table.Td>
                  <Stack gap={6}>
                    {d.isNew
                      ? <Select size="sm" placeholder="Class" value={d.cls || null} data={Object.keys(meta.classes)} onChange={(v) => upd(i, { cls: v || "", spec: v ? Object.keys(meta.classes[v])[0] : "", offspec: null, flex: [] })} />
                      : <Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={d.cls} size={34} title={d.cls} /><Text fw={600} c={classColour(meta, d.cls)} truncate>{d.label}</Text></Group>}
                    {(d.isNew || !me.characters.find((c) => c.label === d.label)?.name) && (
                      <Group gap={6} wrap="nowrap" grow><TextInput size="sm" placeholder="First" maxLength={12} value={d.name} onChange={(e) => upd(i, { name: e.currentTarget.value })} /><TextInput size="sm" placeholder="Last" maxLength={12} value={d.surname} onChange={(e) => upd(i, { surname: e.currentTarget.value })} /></Group>
                    )}
                    {d.isNew && <Text size="xs" c="dimmed">{d.name ? "an officer confirms it after save" : "No name yet = planned. Name it before the first raid so an officer can confirm it."}</Text>}
                  </Stack>
                </Table.Td>
                <Table.Td><Select size="sm" data={specsOf(d.cls)} value={d.spec || null} onChange={(v) => upd(i, { spec: v || d.spec })} disabled={!d.cls} /></Table.Td>
                <Table.Td><Select size="sm" placeholder="none" data={specsOf(d.cls)} value={d.offspec} onChange={(v) => upd(i, { offspec: v || null })} disabled={!d.cls} clearable /></Table.Td>
                <Table.Td><MultiSelect size="sm" placeholder="none" data={d.cls ? flexChoices(meta, d.cls, d.spec, d.offspec) : []} value={d.flex.filter((r) => flexChoices(meta, d.cls, d.spec, d.offspec).includes(r))} onChange={(v) => upd(i, { flex: v })} disabled={!d.cls} /></Table.Td>
                <Table.Td>{d.isNew && <ActionIcon variant="subtle" color="red" onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}><IconTrash size={17} /></ActionIcon>}</Table.Td>
              </Table.Tr>
            ))}
            {editing && (
              <Table.Tr><Table.Td /><Table.Td colSpan={5}><Button size="xs" variant="subtle" leftSection={<IconPlus size={14} />} onClick={() => setDrafts([...drafts, { label: null, cls: "", spec: "", offspec: null, name: "", surname: "", slot: me.characters.length ? "alt" : "main", flex: [], isNew: true }])}>Add a character</Button></Table.Td></Table.Tr>
            )}
            {!editing && me.characters.length === 0 && <Table.Tr><Table.Td colSpan={7}><Text c="dimmed">No characters yet — press <b>Edit</b> and add your main.</Text></Table.Td></Table.Tr>}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {editing && (
        <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
          <Text size="sm" c="dimmed" mr="auto">Edits apply on save · adding with names blank keeps the character planned</Text>
          <Button variant="default" size="sm" onClick={() => { setEditing(false); onChanged(); }}>Cancel</Button>
          <Button size="sm" loading={busy === "save"} onClick={save}>Save changes</Button>
        </Group>
      )}
      {dialog}
    </Card>
  );
}
