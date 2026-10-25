import { useEffect, useRef, useState } from "react";
import { Alert, Badge, Box, Button, Group, Modal, MultiSelect, NumberInput, SegmentedControl, Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { TimePicker } from "@mantine/dates";
import { IconPencil, IconPlayerPause, IconPlayerPlay, IconPlus, IconTrash } from "@tabler/icons-react";
import { opsApi, type Meta, type OpsReply, type RaidRule, type Schedule } from "../api";
import { useConfirm } from "./ConfirmModal";
import { useOpsSave } from "./ReviewModal";
import { useDirty } from "../hooks/useDirty";
import { Eyebrow, fail, ok } from "./Page";
import { SlotEditor } from "./SlotEditor";

const HOURS: [string, string][] = [["signup_lead_hours", "sheet opens (h before)"], ["nudge_hours_before", "reminder (h before)"], ["lock_hours_before", "locks (h before)"],
  ["confirm_hours_before", "confirm by (h before)"], ["fill_ask_hours", "fill asks (h to answer)"]];
const BOOLS: [string, string][] = [["nudge", "remind the unanswered"], ["autofill", "fill seats automatically"], ["open_dm", "DM the regulars on open"], ["confirm", "ask the rostered to confirm"]];
/** Ad hoc (pickup) runs open on demand and remind nobody: these fields mean nothing there, so the editor hides them. */
const ADHOC_HIDDEN = new Set(["signup_lead_hours", "nudge_hours_before", "nudge", "open_dm"]);
const WEIGHTS: [string, string][] = [["regular", "being a regular"], ["rank", "rank"], ["main", "main over alt"], ["sat_out", "sat out last lockout"], ["signup_order", "signed up early"]];
const SOURCE_WORDS: Record<string, string> = { raid: "raid", adhoc: "ad hoc default", own: "own" };
const SPLIT_LABEL: Record<string, string> = { balanced: "Balanced", first: "Raid one first", rotation: "Rotation" };
const KIND_LABEL: Record<string, string> = { weekly: "Weekly nights", lockout: "Days of each lockout", pickup: "Pickup template" };
const OWN_WORDS: Record<string, string> = { signup_lead_hours: "opens", nudge_hours_before: "reminder", lock_hours_before: "locks", confirm_hours_before: "confirm by", fill_ask_hours: "fill asks",
  nudge: "reminder", autofill: "autofill", open_dm: "DM on open", confirm: "confirmations", split_policy: "two groups:" };

/** What a schedule sets itself, in words ("locks 12 h before · two groups: Rotation"); empty = all the raid's. */
function ownWords(s: Schedule): string {
  return Object.entries(s.own).map(([k, v]) => `${OWN_WORDS[k] || k} ${typeof v === "boolean" ? (v ? "on" : "off") : k === "split_policy" ? SPLIT_LABEL[String(v)] || v : `${v} h${k === "fill_ask_hours" ? "" : " before"}`}`).join(" · ");
}

/** A raid's schedules: WHEN it runs. Each is weekly nights, day N of each lockout, or a pickup template officers open on
 *  demand, with the rosters a run expects and any cadence of its own (the rest follows the raid's rules above). */
/** The raid section rev the page loaded: every schedule write carries it, so a write against settings someone changed
 *  meanwhile is refused (409) instead of landing on top of theirs (design §2.4). */
export function raidRevs(r: RaidRule): { revs?: Record<string, string> } {
  return r.rev ? { revs: { [`config:raids:${r.id}`]: r.rev } } : {};
}

export function Schedules({ r, meta, owner, policies, maxRosters, onSaved, onEditing }: { r: RaidRule; meta?: Meta; owner: boolean; policies: string[]; maxRosters: number; onSaved: () => void; onEditing?: (on: boolean) => void }) {
  const [edit, setEditState] = useState<Schedule | "new" | null>(null);
  const setEdit = (v: Schedule | "new" | null) => { if ((v === null) !== (edit === null)) onEditing?.(v !== null); setEditState(v); };
  const [ask, confirmDialog] = useConfirm();
  const [saveOps, review] = useOpsSave();
  /** Remove and Pause are warn-class (D51): the ReviewModal is their confirm, with the server's sentences (the regulars by
   *  name, the runs already open; what pausing stops) — one review step, never a second dialog before it. */
  async function remove(s: Schedule) {
    try { await saveOps([{ kind: "schedule_remove", target: `${r.id}:${s.id}` }], { ...raidRevs(r), title: `Remove ${s.name}?` }); } catch (e) { fail(e); }
    onSaved();
  }
  /** Pause / Resume beside Edit (design RD-10): one field — never hidden behind the editor's Save. Pausing is reviewed;
   *  resuming is routine, asked here with the server's sentence. */
  async function pause(s: Schedule, on: boolean) {
    if (!on && !(await ask({ title: `Resume ${s.name}?`, message: s.resume_sentence || "It opens sheets again.", confirmLabel: "Resume" }))) return;
    try { await saveOps([{ kind: "schedule", target: `${r.id}:${s.id}`, fields: { active: !on } }], { ...raidRevs(r), title: `${on ? "Pause" : "Resume"} ${s.name}?` }); } catch (e) { fail(e); }
    onSaved();
  }
  return (
    <Box px="md" pb="md">
      <Group justify="space-between" mb={6} wrap="wrap">
        <Eyebrow>Schedules</Eyebrow>
        {owner && <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} onClick={() => setEdit("new")}>Add a schedule</Button>}
      </Group>
      {r.schedules.length === 0 && <Text size="sm" c="dimmed">No schedules: no sheet opens for this raid on its own. Add one, or open a run from the Rosters page.</Text>}
      <Stack gap="xs">
        {r.schedules.map((s) => (
          <Group key={s.id} justify="space-between" align="flex-start" wrap="wrap" p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
            <Box style={{ minWidth: 0 }}>
              <Group gap={6} wrap="wrap">
                <Text fw={600} size="sm">{s.name}</Text>
                <Badge size="xs" color={s.class === "adhoc" ? "blue" : "teal"} variant="light">{s.class_label || (s.kind === "pickup" ? "Ad hoc" : "Recurring")}</Badge>
                {!s.active && <Badge size="xs" color="gray" variant="light">paused</Badge>}
                {s.kind === "pickup" && <Badge size="xs" color="blue" variant="outline">template</Badge>}
                {s.class === "recurring" && <Text size="xs" c="dimmed">{s.regulars || 0} regular{s.regulars === 1 ? "" : "s"}</Text>}
              </Group>
              <Text size="sm">{s.label}</Text>
              {s.kind !== "pickup" && (s.active
                ? <Text size="xs" c="dimmed">{s.next.length ? `next: ${s.next.join(" · ")}` : "no run coming up"}</Text>
                : <Text size="xs" c="yellow">{(s.next_if_active || []).length ? `Paused — would next run ${s.next_if_active![0]} · nothing opens` : "Paused · nothing opens"}</Text>)}
              <Text size="xs" c={Object.keys(s.own).length ? "yellow" : "dimmed"}>{Object.keys(s.own).length ? `its own timing: ${ownWords(s)}` : s.class === "adhoc" ? "the ad hoc defaults: first come, no reminder, no confirmations, locks 2 h before" : "follows the raid's timing"}</Text>
            </Box>
            {owner && (
              <Group gap={4} wrap="nowrap">
                {s.kind !== "pickup" && (s.active
                  ? <Button size="xs" variant="subtle" color="yellow" leftSection={<IconPlayerPause size={14} />} onClick={() => pause(s, true)}>Pause</Button>
                  : <Button size="xs" variant="subtle" color="teal" leftSection={<IconPlayerPlay size={14} />} onClick={() => pause(s, false)}>Resume</Button>)}
                <Button size="xs" variant="subtle" leftSection={<IconPencil size={14} />} onClick={() => setEdit(s)}>Edit</Button>
                <Button size="xs" variant="subtle" color="red" onClick={() => remove(s)} aria-label={`remove ${s.name}`}><IconTrash size={14} /></Button>
              </Group>
            )}
          </Group>
        ))}
      </Stack>
      {edit && <ScheduleEditor r={r} meta={meta} s={edit === "new" ? null : edit} policies={policies} maxRosters={maxRosters} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); onSaved(); }} onStale={onSaved} />}
      {confirmDialog}
      {review}
    </Box>
  );
}

type Draft = { name: string; kind: string; active: boolean; rosters: string; slots: string[]; days: string[]; time: string; cadence: Record<string, string>; weights: Record<string, string> };

function toDraft(s: Schedule | null): Draft {
  const cadence: Record<string, string> = {};
  for (const [k, v] of Object.entries(s?.own || {})) cadence[k] = typeof v === "boolean" ? (v ? "on" : "off") : String(v);
  const weights: Record<string, string> = {};
  for (const [k, v] of Object.entries(s?.own_weights || {})) weights[k] = String(v);
  return { name: s?.name || "", kind: s?.kind || "weekly", active: s ? s.active : true, rosters: String(s?.rosters || 1), slots: s?.slots || [], days: (s?.days || []).map(String),
    time: s?.time || "20:00", cadence, weights };
}

/** registry.ADHOC_DEFAULTS / ADHOC_WEIGHTS, for a NEW pickup's placeholders (an existing schedule reads them from the server). */
const ADHOC_DEFAULT: Record<string, unknown> = { signup_lead_hours: 168, nudge_hours_before: 3, lock_hours_before: 2, confirm_hours_before: 1, fill_ask_hours: 1, nudge: false, open_dm: false, confirm: false, autofill: true, split_policy: "balanced" };
const ADHOC_WEIGHT: Record<string, number> = { rank: 0, main: 1, sat_out: 0, signup_order: 6, regular: 0 };

function slug(name: string, kind: string, taken: string[]): string {
  const base = (name || kind).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 12) || kind;
  let id = base, n = 2;
  while (taken.includes(id) || id === "default") id = `${base}-${n++}`;
  return id;
}

/** Add or edit one schedule. Every time is picked (weekday + 12-hour time picker, lockout day selects); cadence fields
 *  left empty follow the raid's. One POST, one registry write: a refusal comes back as a toast and nothing is saved. */
function ScheduleEditor({ r, meta, s, policies, maxRosters, onClose, onSaved, onStale }: { r: RaidRule; meta?: Meta; s: Schedule | null; policies: string[]; maxRosters: number; onClose: () => void; onSaved: () => void; onStale?: () => void }) {
  const [d, setD] = useState<Draft>(() => toDraft(s));
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<OpsReply | null>(null);
  const [saveOps, review] = useOpsSave();
  useDirty(`schedule:${r.id}:${s?.id ?? "new"}`, true);  // an open editor holds a draft
  const adhoc = d.kind === "pickup";
  const raidVal = (k: string) => (r as unknown as Record<string, unknown>)[k];
  /** what an empty field inherits, and from where: the raid's rules, or (ad hoc) the ad hoc defaults */
  const inherited = (k: string): [unknown, string] => {
    const src = s?.source?.[k];
    if (s && typeof src === "string" && src !== "own") return [s.effective[k], SOURCE_WORDS[src] || src];
    if (adhoc) return [ADHOC_DEFAULT[k], "ad hoc default"];
    return [raidVal(k), "raid"];
  };
  const setC = (k: string, v: string) => setD({ ...d, cadence: { ...d.cadence, [k]: v } });
  const setW = (k: string, v: string) => setD({ ...d, weights: { ...d.weights, [k]: v } });

  const id = s ? s.id : slug(d.name, d.kind, r.schedules.map((x) => x.id));
  /** The draft as schedule fields — for an existing schedule ONLY the fields that differ from what was loaded (design RD-10:
   *  a Save never re-sends `active: true` over a pause made elsewhere a moment ago, nor any setting nobody touched). */
  function fieldsOf(dd: Draft): Record<string, unknown> {
    const all = (x: Draft): Record<string, unknown> => {
      const f: Record<string, unknown> = { name: x.name.trim() || null, active: x.active, rosters: Number(x.rosters) };
      if (!s) f.kind = x.kind;
      if (x.kind === "weekly") f.slots = x.slots;
      if (x.kind === "lockout") { f.days = x.days.map(Number); f.time = x.time; }
      for (const [k] of HOURS) f[k] = x.cadence[k] === undefined || x.cadence[k] === "" ? null : Number(x.cadence[k]);
      for (const [k] of BOOLS) f[k] = x.cadence[k] ? x.cadence[k] === "on" : null;
      f.split_policy = x.cadence.split_policy || null;
      f.weights = Object.fromEntries(WEIGHTS.map(([k]) => [k, x.weights[k] === undefined || x.weights[k] === "" ? null : Number(x.weights[k])]));
      if (x.kind === "pickup") for (const k of ADHOC_HIDDEN) delete f[k];
      if (s?.is_default) delete f.kind;
      return f;
    };
    const want = all(dd);
    if (!s) return want;
    const base = all(toDraft(s));
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(want)) {
      if (k === "weights") {  // per key: only the weights that changed
        const bw = (base.weights || {}) as Record<string, unknown>, ww = v as Record<string, unknown>;
        const diff = Object.fromEntries(Object.entries(ww).filter(([wk, wv]) => JSON.stringify(wv) !== JSON.stringify(bw[wk])));
        if (Object.keys(diff).length) out.weights = diff;
      } else if (JSON.stringify(v) !== JSON.stringify(base[k])) out[k] = v;
    }
    return out;
  }

  // "the next three runs with these rules" (design RD-01): the server's own preview of the draft (rc.next_runs_preview on a
  // shadow, plus the refusal its Save would give) — the browser computes no schedule
  const seq = useRef(0);
  useEffect(() => {
    const f = fieldsOf(d);
    if (!Object.keys(f).length || (!s && !f.slots && !f.days && d.kind !== "pickup")) { setPreview(null); return; }
    const n = ++seq.current;
    const t = setTimeout(() => {
      opsApi.preview([{ kind: "schedule", target: `${r.id}:${id}`, fields: f }]).then((x) => { if (n === seq.current) setPreview(x); }).catch(() => { if (n === seq.current) setPreview(null); });
    }, 500);
    return () => clearTimeout(t);
  }, [JSON.stringify(d)]);  // eslint-disable-line react-hooks/exhaustive-deps
  const pv = preview?.described;
  const pvNotes = pv ? [...new Set(pv.lines.flatMap((l) => l.notes))] : [];
  const pvError = pv?.lines.find((l) => l.error)?.error || null;

  async function save() {
    const fields = fieldsOf(d);
    if (!Object.keys(fields).length) { ok("nothing changed"); onClose(); return; }
    setBusy(true);
    try {
      const x = await saveOps([{ kind: "schedule", target: `${r.id}:${id}`, fields }], { ...raidRevs(r), title: s ? `Edit ${s.name}` : `Add ${d.name || "a schedule"}` });
      if (!x) return;  // Back to edits: the draft stays
      // a 409 (changed meanwhile): the page reloads under the open editor, the draft stays, the next Save carries the new rev
      if (x.changed?.length) onStale?.();
      else if (x.applied?.length) onSaved();
    } catch (e) { fail(e); }
    finally { setBusy(false); }
  }

  const hm = /^(\d{1,2}):(\d{2})/.exec(d.time || "");
  return (
    <Modal opened onClose={onClose} title={s ? `Edit ${s.name}` : `Add a schedule to ${r.name}`} size="lg">
      <Stack gap="sm">
        {review}
        <Group gap="md" align="flex-end" wrap="wrap">
          <TextInput label="name" placeholder={d.kind === "pickup" ? "Pickup" : "Main night, Alt run…"} value={d.name} onChange={(e) => setD({ ...d, name: e.currentTarget.value })} maxLength={40} w={220} />
          <Select label="rosters per run" description={(s?.rosters_copy && s.rosters_copy[d.rosters]) || `the sheet advertises ${d.rosters} × ${r.size} seats`} data={Array.from({ length: maxRosters }, (_, i) => ({ value: String(i + 1), label: `${i + 1} roster${i ? "s" : ""}` }))}
            value={d.rosters} onChange={(v) => setD({ ...d, rosters: v || "1" })} allowDeselect={false} w={260} />
          {!s && <Switch label="active" description="paused: nothing opens from it" checked={d.active} onChange={(e) => setD({ ...d, active: e.currentTarget.checked })} />}
          {s && <Text size="xs" c="dimmed" maw={200}>{s.active ? "Active — pause it from the schedule row." : "Paused — resume it from the schedule row."}</Text>}
        </Group>
        {!s && <SegmentedControl data={Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))} value={d.kind} onChange={(v) => setD({ ...d, kind: v })} />}
        {d.kind === "weekly" && <SlotEditor label="run times" description="one sheet per run time per week, guild time" value={d.slots} onChange={(v) => setD({ ...d, slots: v })} />}
        {d.kind === "lockout" && (
          <Group gap="md" align="flex-end" wrap="wrap">
            <MultiSelect label="days of each lockout" description={`day 1 = the day the ${r.lockout_days}-day lockout resets`} w={260}
              data={Array.from({ length: r.lockout_days }, (_, i) => ({ value: String(i + 1), label: `Day ${i + 1}` }))} value={d.days} onChange={(v) => setD({ ...d, days: v })} />
            <TimePicker label="start time" description="guild time" format="12h" withDropdown minutesStep={5} value={hm ? `${hm[1].padStart(2, "0")}:${hm[2]}` : ""} onChange={(v) => setD({ ...d, time: v })} w={150} />
          </Group>
        )}
        {d.kind === "pickup" && <Text size="sm" c="dimmed">A template never opens by itself: the Rosters page and /raid open offer it, and the run starts at the day and time picked there.</Text>}
        <Text size="sm" fw={500} mt="xs">{adhoc ? "An ad hoc run's timing" : "A run's week"} <Text span size="xs" c="dimmed">— empty follows {adhoc ? "the ad hoc defaults (it takes only the raid's size, comp and lockout)" : "the raid's rules"}</Text></Text>
        {!adhoc && (meta?.cadence_presets || []).length > 0 && (
          <Group gap={6} wrap="wrap">
            <Text size="xs" c="dimmed">Presets:</Text>
            {meta!.cadence_presets!.map((p) => (
              <Button key={p.key} size="compact-xs" variant="default" onClick={() => setD({ ...d, cadence: { ...d.cadence, signup_lead_hours: String(p.signup_lead_hours), nudge_hours_before: String(p.nudge_hours_before), lock_hours_before: String(p.lock_hours_before), confirm_hours_before: String(p.confirm_hours_before) } })}>{p.name}</Button>
            ))}
          </Group>
        )}
        <Group gap="md" align="flex-end" wrap="wrap">
          {HOURS.filter(([k]) => !adhoc || !ADHOC_HIDDEN.has(k)).map(([k, label]) => {
            const [v, from] = inherited(k);
            return <NumberInput key={k} label={label} min={0} w={170} placeholder={`${from}: ${v}`} value={d.cadence[k] ?? ""} onChange={(x) => setC(k, x === "" ? "" : String(x))} />;
          })}
        </Group>
        <Group gap="md" align="flex-end" wrap="wrap">
          {BOOLS.filter(([k]) => !adhoc || !ADHOC_HIDDEN.has(k)).map(([k, label]) => {
            const [v, from] = inherited(k);
            return <Select key={k} label={label} w={200} allowDeselect={false} value={d.cadence[k] || ""} onChange={(x) => setC(k, x || "")}
              data={[{ value: "", label: `${from === "raid" ? "the raid's" : from} (${v ? "on" : "off"})` }, { value: "on", label: "on" }, { value: "off", label: "off" }]} />;
          })}
          <Select label="when one night fills two groups" w={200} allowDeselect={false} value={d.cadence.split_policy || ""} onChange={(v) => setC("split_policy", v || "")}
            data={[{ value: "", label: `the raid's (${SPLIT_LABEL[String(raidVal("split_policy"))] || raidVal("split_policy")})` }, ...policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))]} />
        </Group>
        <Text size="sm" fw={500} mt="xs">Seat order at lock <Text span size="xs" c="dimmed">— empty follows {adhoc ? "first come (signup order)" : "the raid's weights"}</Text></Text>
        <Group gap="md" align="flex-end" wrap="wrap">
          {WEIGHTS.map(([k, label]) => (
            <NumberInput key={k} label={label} min={0} w={150} placeholder={`${s?.weights ? String(s.weights[k]) : adhoc ? String(ADHOC_WEIGHT[k]) : String(r.weights[k] ?? "")}`}
              value={d.weights[k] ?? ""} onChange={(x) => setW(k, x === "" ? "" : String(x))} />
          ))}
        </Group>
        {(pvNotes.length > 0 || pvError) && (
          <Alert variant="light" color={pvError ? "red" : "teal"} p="xs" title={pvError ? "This Save would be refused" : "With these rules"}>
            {pvError && <Text size="sm">{pvError}</Text>}
            {pvNotes.map((n, i) => <Text key={i} size="sm">{n}</Text>)}
          </Alert>
        )}
        <Group gap="sm" justify="flex-end" mt="sm">
          <Button variant="default" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" loading={busy} onClick={save}>{s ? "Save schedule" : "Add schedule"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
