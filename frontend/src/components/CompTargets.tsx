import { Fragment, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Box, Button, Group, NumberInput, Select, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import type { CompSlot, Meta, Minimum, MinimumEntry } from "../api";
import { GameIcon } from "./Icons";
import { useOpsSave } from "./ReviewModal";
import css from "./raidcard.module.css";

/** A minimum's slot icon alone — role, class or spec — with its words in the tooltip (the iconography rule). "dps" (not
 *  a game role) is drawn as the melee + ranged pair. */
export function SlotIcon({ meta, slot, label, size = 22 }: { meta: Meta; slot: string; label?: string; size?: number }) {
  if (slot === "dps") return <RoleIcon meta={meta} role="dps" size={size} />;
  const kind = meta.roles.includes(slot) ? "role" : slot.includes(":") ? "spec" : "class";
  return (
    <Tooltip label={label || slot}>
      <Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind={kind} id={slot} size={size} title={label || slot} /></Box>
    </Tooltip>
  );
}

/** A role's icon: tank and healer have one; dps is melee and ranged together (the profile has no dps icon). */
export function RoleIcon({ meta, role, size = 22 }: { meta: Meta; role: string; size?: number }) {
  if (role !== "dps") return <SlotIcon meta={meta} slot={role} size={size} />;
  return (
    <Tooltip label="dps">
      <Box style={{ display: "inline-flex" }}>
        <GameIcon meta={meta} kind="role" id="melee" size={size} title="dps" />
        <Box style={{ marginLeft: -Math.round(size / 3), display: "inline-flex" }}><GameIcon meta={meta} kind="role" id="ranged" size={size} title="dps" /></Box>
      </Box>
    </Tooltip>
  );
}

/** A stored minimum as {min, note} (registry.min_entry: the profile writes a bare number). */
const entry = (e: MinimumEntry): Minimum => (typeof e === "number" ? { min: e } : { min: Number(e.min || 0), note: e.note || null });

type Row = { slot: string; min: number | string; note: string };

/** Group make-up (D16): one minimum per role, class or spec — the fewest a run of this raid takes, with no upper number.
 *  Read: slot icon + number for each minimum a run reads (`minimum`, the game's tank and healer ones included), the
 *  reason in the tooltip (the iconography rule); amber = the guild's own. Set minimums / Edit minimums turns the guild's
 *  own (`minimums`, as stored) into a table — slot · at least · why — with one Save: a typed `comp` change on the
 *  structured path (only the slots that differ; a removed row = null) with the raid's section rev, so someone else's
 *  change meanwhile is a 409, not an overwrite. Used by the Raids page's raid card and the Readiness page. */
export function CompTargets({ meta, rid, size, minimums, minimum, slots, rev, editable, onSaved, onEditing }: {
  meta: Meta; rid: string; size: number;
  /** the guild's own, as stored */ minimums: Record<string, MinimumEntry>;
  /** what a run reads, one number per slot */ minimum: Record<string, number>;
  slots: CompSlot[] | null; rev?: string; editable: boolean; onSaved: () => void; onEditing?: (on: boolean) => void;
}) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [save, review] = useOpsSave();
  useDirty(`comp-targets:${rid}`, rows !== null);  // the guild switcher asks before dropping the draft
  const words = (slot: string) => slots?.find((x) => x.value === slot)?.label || slot;
  const own: Record<string, Minimum> = Object.fromEntries(Object.entries(minimums || {}).map(([slot, e]) => [slot, entry(e)]));
  const toRows = (): Row[] => Object.entries(own).map(([slot, m]) => ({ slot, min: m.min, note: m.note || "" }));
  const start = () => { setRows(toRows()); onEditing?.(true); };
  const stop = () => { setRows(null); setAdding(null); onEditing?.(false); };

  /** the draft as one `comp` change: changed or new slots as whole entries, removed slots null — nothing else */
  function fields(): Record<string, Minimum | null> {
    const out: Record<string, Minimum | null> = {};
    const want = new Map((rows || []).map((r) => [r.slot, r]));
    for (const [slot, r] of want) {
      const next: Minimum = { min: Number(r.min || 0), note: r.note.trim() || null };
      const have = own[slot];
      if (!have || have.min !== next.min || (have.note || null) !== next.note) out[slot] = next;
    }
    for (const slot of Object.keys(own)) if (!want.has(slot)) out[slot] = null;
    return out;
  }

  async function submit() {
    const f = fields();
    if (!Object.keys(f).length) { stop(); return; }
    setBusy(true);
    try {
      const r = await save([{ kind: "comp", target: rid, fields: f }], { revs: rev ? { [`config:raids:${rid}`]: rev } : undefined, title: "Save the minimums?" });
      if (r === null) return;  // Back to edits: the draft stays
      if (r.changed?.length) return;  // someone changed this raid meanwhile: the toast said so; the draft stays for a reload
      if (!r.refused?.length) { stop(); onSaved(); }
    } finally { setBusy(false); }
  }

  const reads = Object.entries(minimum || {});
  const why = (slot: string) => (own[slot] ? own[slot].note || "the guild's minimum" : "the game's minimum");
  const edit = editable && slots ? <Button size="xs" variant="default" onClick={start}>{Object.keys(own).length ? "Edit minimums" : "Set minimums"}</Button> : null;
  const taken = new Set((rows || []).map((r) => r.slot));
  return (
    <Stack gap="xs">
      {!rows ? (
        <>
          <div className={css.mrow}>
            {reads.map(([slot, n]) => (
              <span key={slot} className={[css.count, own[slot] ? css.changedCount : ""].join(" ")}>
                <SlotIcon meta={meta} slot={slot} label={`${words(slot)}: ${why(slot)}`} />{n}
              </span>
            ))}
            {Object.keys(own).length > 0 && edit}
          </div>
          {Object.keys(own).length === 0 && (
            <>
              <Text size="xs" c="dimmed">No minimums set. The lock uses the game's minimum for tanks and healers. Add one for a role or class below.</Text>
              {edit && <Group>{edit}</Group>}
            </>
          )}
        </>
      ) : (
        <Stack gap="xs">
          {rows.length > 0 && (
            <div className={css.aimgrid}>
              <span /><span className={css.aimhead}>At least</span><span className={[css.aimhead, css.aimWhy].join(" ")}>Why</span><span />
              {rows.map((r, i) => (
                <Fragment key={r.slot}>
                  <SlotIcon meta={meta} slot={r.slot} label={words(r.slot)} />
                  <NumberInput size="xs" min={0} max={size} value={r.min} aria-label={`${words(r.slot)}: at least`} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, min: v } : x)))} />
                  <TextInput size="xs" className={css.aimWhy} value={r.note} maxLength={200} placeholder="why — shown on the Readiness page" aria-label={`${words(r.slot)}: why`} onChange={(e) => { const v = e.currentTarget.value; setRows(rows.map((x, j) => (j === i ? { ...x, note: v } : x))); }} />
                  <Tooltip label={`Remove the ${words(r.slot)} minimum`}><ActionIcon variant="subtle" color="red" aria-label={`remove the ${words(r.slot)} minimum`} onClick={() => setRows(rows.filter((_, j) => j !== i))}><IconTrash size={14} /></ActionIcon></Tooltip>
                </Fragment>
              ))}
            </div>
          )}
          {rows.length === 0 && <Text size="sm" c="dimmed">No minimums of the guild's own in this draft. The lock uses the game's minimum for tanks and healers. Add one below, or save to clear them all.</Text>}
          <Group gap="xs" wrap="wrap">
            <Select size="xs" w={240} searchable placeholder="Add a minimum for…" value={adding} onChange={setAdding} nothingFoundMessage="No role, class or spec by that name"
              data={(slots || []).filter((x) => !taken.has(x.value)).map((x) => ({ value: x.value, label: x.label }))} />
            <Button size="xs" variant="default" leftSection={<IconPlus size={13} />} disabled={!adding} onClick={() => { if (adding) { setRows([...rows, { slot: adding, min: 1, note: "" }]); setAdding(null); } }}>Add</Button>
          </Group>
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={stop} disabled={busy}>Cancel</Button>
            <Button size="xs" loading={busy} onClick={submit}>Save the minimums</Button>
          </Group>
        </Stack>
      )}
      {review}
    </Stack>
  );
}
