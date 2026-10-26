import { Fragment, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Anchor, Box, Button, Group, NumberInput, Select, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import type { CompSlot, CompTarget, Meta } from "../api";
import { GameIcon } from "./Icons";
import { useOpsSave } from "./ReviewModal";
import css from "./raidcard.module.css";

/** A comp slot's icon alone — role, class or spec — with its words in the tooltip (the iconography rule). "dps" (a limit's
 *  role, not a game role) is the melee + ranged pair. */
export function SlotIcon({ meta, slot, label, size = 22 }: { meta: Meta; slot: string; label?: string; size?: number }) {
  if (slot === "dps") return <RoleIcon meta={meta} role="dps" size={size} />;
  const kind = meta.roles.includes(slot) ? "role" : slot.includes(":") ? "spec" : "class";
  return (
    <Tooltip label={label || slot}>
      <Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind={kind} id={slot} size={size} title={label || slot} /></Box>
    </Tooltip>
  );
}

/** A limit's role icon: tank and healer have one; dps is melee and ranged together (the profile has no dps icon). */
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

const range = (min: number, max: number | null | undefined) => (max === null || max === undefined ? `${min}+` : `${min}–${max}`);

type Row = { slot: string; min: number | string; max: number | string; note: string };

/** Group make-up: how many of each role a run takes. Two rows — the owner's LIMITS (what the lock builds within; read-only
 *  here, the owner edits them under Edit rules) and the officers' AIMS for one raid ("we aim for" — what the Readiness page
 *  measures; design §3.5 RD-12): icon + range, the reason in the tooltip. Set aims / Edit aims turns the aims into a table
 *  of inputs with one Save: typed `comp` changes on the structured path (only the slots that differ; a removed row = null)
 *  with the raid's section rev, so someone else's change meanwhile is a 409, not an overwrite. A role aim outside the
 *  limits is refused by the server with the owner pointer. Used by the Raids page's raid card and the Readiness page. */
export function CompTargets({ meta, rid, size, targets, bounds, boundsChanged, slots, rev, editable, onSaved, onEditing, onReadiness }: {
  meta: Meta; rid: string; size: number; targets: Record<string, CompTarget>; bounds?: Record<string, { min: number; max?: number | null }>;
  /** roles whose limits differ from the game's default (shown amber) */ boundsChanged?: string[];
  slots: CompSlot[] | null; rev?: string; editable: boolean; onSaved: () => void; onEditing?: (on: boolean) => void;
  /** true on the Readiness page itself: its empty state says "this page" instead of linking to it */ onReadiness?: boolean;
}) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [save, review] = useOpsSave();
  useDirty(`comp-targets:${rid}`, rows !== null);  // the guild switcher asks before dropping the draft
  const words = (slot: string) => slots?.find((x) => x.value === slot)?.label || slot;
  const toRows = (): Row[] => Object.entries(targets).map(([slot, t]) => ({ slot, min: t.min ?? 0, max: t.max ?? "", note: t.note || "" }));
  const start = () => { setRows(toRows()); onEditing?.(true); };
  const stop = () => { setRows(null); setAdding(null); onEditing?.(false); };

  /** the draft as one `comp` change: changed or new slots as whole entries, removed slots null — nothing else */
  function fields(): Record<string, CompTarget | null> {
    const out: Record<string, CompTarget | null> = {};
    const want = new Map((rows || []).map((r) => [r.slot, r]));
    for (const [slot, r] of want) {
      const entry: CompTarget = { min: Number(r.min || 0), max: r.max === "" || r.max === null ? null : Number(r.max), note: r.note.trim() || null };
      const have = targets[slot];
      if (!have || have.min !== entry.min || (have.max ?? null) !== entry.max || (have.note || null) !== entry.note) out[slot] = entry;
    }
    for (const slot of Object.keys(targets)) if (!want.has(slot)) out[slot] = null;
    return out;
  }

  async function submit() {
    const f = fields();
    if (!Object.keys(f).length) { stop(); return; }
    setBusy(true);
    try {
      const r = await save([{ kind: "comp", target: rid, fields: f }], { revs: rev ? { [`config:raids:${rid}`]: rev } : undefined, title: "Save the aims?" });
      if (r === null) return;  // Back to edits: the draft stays
      if (r.changed?.length) return;  // someone changed this raid meanwhile: the toast said so; the draft stays for a reload
      if (!r.refused?.length) { stop(); onSaved(); }
    } finally { setBusy(false); }
  }

  const b = bounds || {};
  const roles = ["tank", "healer", "dps"].filter((role) => b[role]);
  const aims = Object.entries(targets);
  const setAims = editable && slots ? <Button size="xs" variant="default" onClick={start}>{aims.length ? "Edit aims" : "Set aims"}</Button> : null;
  const readiness = onReadiness ? <>this page</> : <Anchor component={Link} to="/readiness" inherit>Readiness</Anchor>;
  const taken = new Set((rows || []).map((r) => r.slot));
  return (
    <Stack gap="xs">
      {roles.length > 0 && (
        <div className={css.mrow}>
          <span className={css.mlabel}>limits <Text span inherit c="dimmed" opacity={0.75}>(owner)</Text></span>
          {roles.map((role) => (
            <span key={role} className={[css.count, boundsChanged?.includes(role) ? css.changedCount : ""].join(" ")}>
              <RoleIcon meta={meta} role={role} />{range(b[role].min, b[role].max)}
            </span>
          ))}
        </div>
      )}
      {!rows ? (
        <div className={css.mrow}>
          <span className={css.mlabel}>aims <Text span inherit c="dimmed" opacity={0.75}>(officers)</Text></span>
          {aims.length === 0
            ? <Text size="xs" c="dimmed" style={{ flex: "1 1 160px", minWidth: 0 }}>No aims yet: {readiness} measures against the limits and the raid buffs' defaults.</Text>
            : aims.map(([slot, t]) => (
              <span key={slot} className={css.count}>
                <SlotIcon meta={meta} slot={slot} label={`${words(slot)} — ${t.note || "no reason given"}`} />{range(t.min ?? 0, t.max)}
              </span>
            ))}
          {setAims}
        </div>
      ) : (
        <Stack gap="xs">
          {rows.length > 0 && (
            <div className={css.aimgrid}>
              <span /><span className={css.aimhead}>At least</span><span className={css.aimhead}>At most</span><span className={[css.aimhead, css.aimWhy].join(" ")}>Why</span><span />
              {rows.map((r, i) => (
                <Fragment key={r.slot}>
                  <SlotIcon meta={meta} slot={r.slot} label={words(r.slot)} />
                  <NumberInput size="xs" min={0} max={size} value={r.min} aria-label={`${words(r.slot)}: at least`} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, min: v } : x)))} />
                  <NumberInput size="xs" min={0} max={size} value={r.max} placeholder="no limit" aria-label={`${words(r.slot)}: at most`} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, max: v } : x)))} />
                  <TextInput size="xs" className={css.aimWhy} value={r.note} maxLength={200} placeholder="why — shown on the Readiness page" aria-label={`${words(r.slot)}: why`} onChange={(e) => { const v = e.currentTarget.value; setRows(rows.map((x, j) => (j === i ? { ...x, note: v } : x))); }} />
                  <Tooltip label={`Remove the ${words(r.slot)} aim`}><ActionIcon variant="subtle" color="red" aria-label={`remove the ${words(r.slot)} aim`} onClick={() => setRows(rows.filter((_, j) => j !== i))}><IconTrash size={14} /></ActionIcon></Tooltip>
                </Fragment>
              ))}
            </div>
          )}
          {rows.length === 0 && <Text size="sm" c="dimmed">No aims in this draft — add one for a role, class or spec below, or Save to clear them all.</Text>}
          <Group gap="xs" wrap="wrap">
            <Select size="xs" w={240} searchable placeholder="Add an aim for…" value={adding} onChange={setAdding} nothingFoundMessage="No role, class or spec by that name"
              data={(slots || []).filter((x) => !taken.has(x.value)).map((x) => ({ value: x.value, label: x.label }))} />
            <Button size="xs" variant="default" leftSection={<IconPlus size={13} />} disabled={!adding} onClick={() => { if (adding) { setRows([...rows, { slot: adding, min: 1, max: "", note: "" }]); setAdding(null); } }}>Add</Button>
          </Group>
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={stop} disabled={busy}>Cancel</Button>
            <Button size="xs" loading={busy} onClick={submit}>Save aims</Button>
          </Group>
        </Stack>
      )}
      {review}
    </Stack>
  );
}
