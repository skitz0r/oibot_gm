import { useState } from "react";
import { ActionIcon, Badge, Button, Group, MultiSelect, Radio, Select, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconArchive, IconArrowBackUp, IconPlus, IconX } from "@tabler/icons-react";
import { useDirty } from "../../hooks/useDirty";
import { api, ApiError, type MemberRow, type Meta, type TableSaved } from "../../api";
import { GameIcon } from "../Icons";
import { flexChoices } from "../CharacterTable";
import { fail, ok } from "../Page";
import type { ConfirmOpts, ConfirmResult } from "../ConfirmModal";
import { classColour } from "../../theme";
import css from "../../pages/members.module.css";

type Draft = { label: string | null; cls: string; spec: string; offspec: string | null; name: string; surname: string; main: boolean; named: boolean; flex: string[]; retire?: boolean; isNew?: boolean };

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

/** Edit one member's characters in place (design §5.58, finding 18): the member's row becomes inputs with Cancel and
 *  Save <name>'s characters beside them; nothing else on the page changes. Save sends only the rows that changed, with the
 *  member's base rev, through POST /api/members/save — previewed first: the server's sentences (consequences.
 *  save_consequence, one per fate in the live runs) are the confirm; none = the first press saves. A member changed
 *  elsewhere meanwhile is refused (409) and the draft is kept. Retire marks a row to retire on save; Delete is on the
 *  character's ⋮ (it asks on its own). Rank changes from the member's ⋮, never here. */
export function MemberEdit({ meta, r, ask, onCancel, onSaved }: { meta: Meta; r: MemberRow; ask: (o: ConfirmOpts) => Promise<ConfirmResult>; onCancel: () => void; onSaved: () => void }) {
  const [drafts, setDrafts] = useState<Draft[]>(() => r.characters.map((c) => ({ label: c.label, cls: c.cls, spec: c.spec, offspec: c.offspec, name: c.name || "", surname: c.surname || "", main: c.is_main, named: !!c.name, flex: [...(c.flex || [])] })));
  const [busy, setBusy] = useState(false);
  useDirty(`member-edit:${r.uid}`, true);  // mounted only while this member is edited
  const specsOf = (cls: string) => Object.entries(meta.classes[cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }));
  const upd = (i: number, patch: Partial<Draft>) => setDrafts((ds) => ds.map((d, j) => (j === i ? { ...d, ...patch } : patch.main ? { ...d, main: false } : d)));

  /** The rows that differ from the record: a retire, a new character, or a changed spec / offspec / extra roles / main / name. */
  function changed(): Record<string, unknown>[] {
    const out: Record<string, unknown>[] = [];
    for (const d of drafts) {
      const o = r.characters.find((c) => c.label === d.label);
      const row = { label: d.label, cls: d.cls, spec: d.spec, offspec: d.offspec, name: d.name, surname: d.surname, main: d.main, flex: d.flex };
      if (d.isNew) { if (d.cls && d.spec) out.push(row); continue; }
      if (!o) continue;
      if (d.retire) { out.push({ label: d.label, retire: true }); continue; }
      if (d.spec !== o.spec || (d.offspec || null) !== (o.offspec || null) || !sameSet(d.flex, o.flex || []) || (d.main && !o.is_main) || (!o.name && !!d.name)) out.push(row);
    }
    return out;
  }

  async function save() {
    const rows = changed();
    if (!rows.length) { onCancel(); return; }
    const body = (preview: boolean) => ({ rows: [{ uid: r.uid, rev: r.characters_rev, characters: rows }], preview });
    setBusy(true);
    try {
      const p = await api.post<TableSaved>("/api/members/save", body(true));
      if (p.refused?.length) throw new Error(p.refused[0].error);
      const said = ((p.sentences || []) as { uid: string; sentences: string[] }[]).find((x) => x.uid === r.uid)?.sentences || [];
      if (said.length && !(await ask({ title: `Save ${r.display_name}'s characters?`, message: said.join("\n"), confirmLabel: "Save" }))) return;
      const x = await api.post<TableSaved>("/api/members/save", body(false));
      if (x.refused?.length) throw new Error(x.refused[0].error);
      ok(x.message);
      onSaved();
    } catch (e) {
      fail(e instanceof ApiError && e.status === 409 ? new Error(`${e.message} Your edit is kept: Cancel to see the latest.`) : e);
    } finally { setBusy(false); }
  }

  const add = () => setDrafts((ds) => [...ds, { label: null, cls: "", spec: "", offspec: null, name: "", surname: "", main: ds.length === 0, named: false, flex: [], isNew: true }]);
  const lab = { label: css.phoneLabel };
  return (
    <Stack gap="sm" className={css.edit} py={4}>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Group gap={8} wrap="wrap" style={{ minWidth: 0 }}>
          <Text fw={700}>{r.display_name}</Text>
          {r.privilege === "owner" || r.privilege === "officer" ? <Badge size="sm" variant="outline" color="teal" tt="none">{r.privilege}</Badge> : null}
          {r.rank && <Text size="sm" c="dimmed">{r.rank}</Text>}
        </Group>
        <Group gap="xs" wrap="wrap">
          <Button variant="default" size="sm" onClick={onCancel}>Cancel</Button>
          <Button size="sm" loading={busy} onClick={save}>Save {r.display_name}'s characters</Button>
        </Group>
      </Group>
      <div className={css.editGrid}>
        <Text size="xs" c="dimmed" className={css.editHead}>Character</Text>
        <Text size="xs" c="dimmed" className={css.editHead}>Spec</Text>
        <Text size="xs" c="dimmed" className={css.editHead}>Offspec</Text>
        <Text size="xs" c="dimmed" className={css.editHead}>Extra roles</Text>
        <Text size="xs" c="dimmed" className={css.editHead}>Main</Text>
        <span className={css.editHead} />
        {drafts.map((d, i) => {
          const choices = d.cls ? flexChoices(meta, d.cls, d.spec, d.offspec) : [];
          const off = !!d.retire;
          return (
            <div key={d.label ?? `new${i}`} style={{ display: "contents" }}>
              <Stack gap={6} className={css.editRow} style={{ minWidth: 0 }}>
                {d.isNew
                  ? <Select size="sm" label="Class" classNames={lab} placeholder="Class" value={d.cls || null} data={Object.keys(meta.classes)} onChange={(v) => upd(i, { cls: v || "", spec: v ? Object.keys(meta.classes[v])[0] : "", offspec: null, flex: [] })} />
                  : <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}><GameIcon meta={meta} kind="class" id={d.cls} size={26} title={d.cls} /><Text fw={600} c={classColour(meta, d.cls)} truncate className={off ? css.retired : undefined}>{d.label}</Text></Group>}
                {(d.isNew || !d.named) && !off && (
                  <Group gap={6} wrap="nowrap" grow><TextInput size="sm" aria-label="first name" placeholder="First" maxLength={12} value={d.name} onChange={(e) => upd(i, { name: e.currentTarget.value })} /><TextInput size="sm" aria-label="last name" placeholder="Last" maxLength={12} value={d.surname} onChange={(e) => upd(i, { surname: e.currentTarget.value })} /></Group>
                )}
                {off && <Text size="xs" c="dimmed">retires when you save</Text>}
              </Stack>
              <Select size="sm" label="Spec" classNames={lab} aria-label="spec" data={specsOf(d.cls)} value={d.spec || null} onChange={(v) => upd(i, { spec: v || d.spec })} disabled={!d.cls || off} />
              <Select size="sm" label="Offspec" classNames={lab} aria-label="offspec" placeholder="No offspec" data={specsOf(d.cls)} value={d.offspec} onChange={(v) => upd(i, { offspec: v || null })} disabled={!d.cls || off} clearable />
              <MultiSelect size="sm" label="Extra roles" classNames={lab} aria-label="extra roles" placeholder="No extra roles" data={choices} value={d.flex.filter((x) => choices.includes(x))} onChange={(v) => upd(i, { flex: v })} disabled={!d.cls || off} />
              <Radio name={`main-${r.uid}`} label="main" checked={d.main} onChange={() => upd(i, { main: true })} disabled={!d.cls || off} />
              {d.isNew
                ? <Tooltip label="Discard this new character"><ActionIcon variant="subtle" color="gray" aria-label="Discard this new character" onClick={() => setDrafts((ds) => ds.filter((_, j) => j !== i))}><IconX size={17} /></ActionIcon></Tooltip>
                : <Tooltip label={off ? `Keep ${d.label}` : `Retire ${d.label}: it leaves rosters and keeps its history`}><ActionIcon variant="subtle" color="gray" aria-label={off ? `Keep ${d.label}` : `Retire ${d.label}`} onClick={() => upd(i, { retire: !off })}>{off ? <IconArrowBackUp size={17} /> : <IconArchive size={17} />}</ActionIcon></Tooltip>}
            </div>
          );
        })}
      </div>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Button size="sm" variant="subtle" leftSection={<IconPlus size={14} />} onClick={add}>Add a character for {r.display_name}</Button>
        <Text size="xs" c="dimmed">A new character waits for an officer's confirm. Retire keeps the history; Delete… is on the character's ⋮.</Text>
      </Group>
    </Stack>
  );
}
