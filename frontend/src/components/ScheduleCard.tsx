import { useEffect, useRef, useState } from "react";
import { Alert, Badge, Box, Button, Group, MultiSelect, NumberInput, SegmentedControl, Select, Stack, Switch, Text, TextInput, Tooltip } from "@mantine/core";
import { TimePicker } from "@mantine/dates";
import { IconPencil, IconPlayerPause, IconPlayerPlay, IconPlus, IconTrash } from "@tabler/icons-react";
import { opsApi, type Meta, type OpsReply, type RaidRule, type Schedule } from "../api";
import { useConfirm } from "./ConfirmModal";
import { useOpsSave } from "./ReviewModal";
import { useDirty } from "../hooks/useDirty";
import { Eyebrow, fail, ok } from "./Page";
import { SlotEditor, slotLabel } from "./SlotEditor";
import { HOUR_HINT, HOUR_WORDS, RunWeek, SWITCH_HINT, SWITCH_WORDS, WEIGHT_HINT, WEIGHT_WORDS, type WeekRule } from "./RaidCard";
import css from "./raidcard.module.css";

/** The steps a schedule may set for itself — the same words as the raid card and the rules editor (RaidCard). */
const HOURS = ["signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours"] as const;
const BOOLS = ["nudge", "autofill", "open_dm", "confirm"] as const;
/** Ad hoc (pickup) runs open on demand and remind nobody: these fields mean nothing there, so the editor hides them. */
const ADHOC_HIDDEN = new Set(["signup_lead_hours", "nudge_hours_before", "nudge", "open_dm"]);
const WEIGHTS = ["regular", "rank", "main", "sat_out", "signup_order"] as const;
const SOURCE_WORDS: Record<string, string> = { raid: "raid", adhoc: "ad hoc default", own: "own" };
const SPLIT_LABEL: Record<string, string> = { balanced: "Balanced", first: "Raid one first", rotation: "Rotation" };
const KIND_LABEL: Record<string, string> = { weekly: "Weekly nights", lockout: "Days of each lockout", pickup: "Pickup template" };
const OWN_WORDS: Record<string, string> = { ...HOUR_WORDS, nudge: "reminder", autofill: "fills automatically", open_dm: "DM when the sheet opens", confirm: "confirmations", split_policy: "two groups:" };

/** What a schedule sets itself, in words ("locks 12 h before · two groups: Rotation"); empty = all the raid's. */
function ownWords(s: Schedule): string {
  return Object.entries(s.own).map(([k, v]) => `${OWN_WORDS[k] || k} ${typeof v === "boolean" ? (v ? "on" : "off") : k === "split_policy" ? SPLIT_LABEL[String(v)] || v : `${v} h${k === "fill_ask_hours" ? "" : " before"}`}`).join(" · ");
}

/** The raid section rev the page loaded: every schedule write carries it, so a write against settings someone changed
 *  meanwhile is refused (409) instead of landing on top of theirs (design §2.4). */
export function raidRevs(r: RaidRule): { revs?: Record<string, string> } {
  return r.rev ? { revs: { [`config:raids:${r.id}`]: r.rev } } : {};
}

/** A raid's schedules: WHEN it runs. Each is weekly nights, day N of each lockout, or a pickup template officers open on
 *  demand, with the rosters a run expects and any step of a run's week it sets itself (the rest follows the raid's rules
 *  above). The editor opens IN PLACE of the row it edits (a new one under the list) — the card's one edit mode at a time
 *  (design §5.54); `owner` is false while another edit mode is open, so the buttons wait. */
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
  const editor = (s: Schedule | null) => <ScheduleEditor key={s ? s.id : "new"} r={r} meta={meta} s={s} policies={policies} maxRosters={maxRosters} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); onSaved(); }} onStale={onSaved} />;
  return (
    <Box px="md" pb="md">
      <Group justify="space-between" mb={6} wrap="wrap">
        <Eyebrow>Schedules</Eyebrow>
        {owner && !edit && <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} onClick={() => setEdit("new")}>Add a schedule</Button>}
      </Group>
      {r.schedules.length === 0 && edit !== "new" && <Text size="sm" c="dimmed">No schedules: no sheet opens for this raid on its own. {owner ? "Add one, or open a run from the Rosters page." : "The owner adds one; an officer can open a run from the Rosters page."}</Text>}
      <Stack gap="xs">
        {r.schedules.map((s) => (edit !== null && edit !== "new" && edit.id === s.id ? editor(s) : (
          <div key={s.id} className={[css.sched, s.active ? "" : css.paused].join(" ")}>
            <Box style={{ minWidth: 0, flex: "1 1 240px" }}>
              <Group gap={6} wrap="wrap">
                <Text fw={600} size="sm">{s.name}</Text>
                <Badge size="xs" color={s.class === "adhoc" ? "blue" : "teal"} variant="light" style={{ textTransform: "none" }}>{s.class_label || (s.kind === "pickup" ? "Ad hoc" : "Recurring")}</Badge>
                {s.kind === "pickup" && <Badge size="xs" color="blue" variant="outline" style={{ textTransform: "none" }}>template</Badge>}
                {!s.active && <Badge size="xs" color="yellow" variant="light" style={{ textTransform: "none" }}>paused</Badge>}
                {s.class === "recurring" && (s.regulars
                  ? <Text size="xs" c="dimmed">{s.regulars} regular{s.regulars === 1 ? "" : "s"}</Text>
                  : <Text size="xs" c="dimmed">no regulars yet — name them on the Rosters page</Text>)}
              </Group>
              <Text size="sm">
                {s.label}
                {s.kind !== "pickup" && s.active && s.next.length > 0 && <Text span size="xs" c="dimmed"> · next {s.next.map((n, i) => <span key={n}>{i > 0 && " · "}<span className={css.nowrap}>{n}</span></span>)}</Text>}
                {s.kind !== "pickup" && s.active && s.next.length === 0 && <Text span size="xs" c="dimmed"> · no run coming up</Text>}
              </Text>
              {s.kind !== "pickup" && !s.active && <Text size="sm" c="yellow">Paused — nothing opens{(s.next_if_active || []).length ? `; it would next run ${s.next_if_active![0]}` : ""}. Resume to open sheets again.</Text>}
              <Text size="xs" c={Object.keys(s.own).length ? "yellow" : "dimmed"}>{Object.keys(s.own).length ? `its own: ${ownWords(s)}` : s.class === "adhoc" ? "the ad hoc defaults: first come, no reminder, no confirmations, locks 2 h before" : "follows the raid's week"}</Text>
            </Box>
            {owner && (
              <Group gap={2} wrap="nowrap">
                {s.kind !== "pickup" && (s.active
                  ? <Button size="sm" variant="subtle" color="yellow" leftSection={<IconPlayerPause size={14} />} onClick={() => pause(s, true)}>Pause</Button>
                  : <Button size="sm" variant="subtle" color="teal" leftSection={<IconPlayerPlay size={14} />} onClick={() => pause(s, false)}>Resume</Button>)}
                <Button size="sm" variant="subtle" leftSection={<IconPencil size={14} />} onClick={() => setEdit(s)}>Edit</Button>
                <Tooltip label="Remove this schedule"><Button size="sm" variant="subtle" color="red" onClick={() => remove(s)} aria-label={`remove ${s.name}`} px={8}><IconTrash size={15} /></Button></Tooltip>
              </Group>
            )}
          </div>
        )))}
        {edit === "new" && editor(null)}
      </Stack>
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
const PRESET_KEYS = ["signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before"] as const;

function slug(name: string, kind: string, taken: string[]): string {
  const base = (name || kind).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 12) || kind;
  let id = base, n = 2;
  while (taken.includes(id) || id === "default") id = `${base}-${n++}`;
  return id;
}

/** Add or edit one schedule, in place. Every time is picked (weekday + 12-hour time picker, lockout day selects); a step
 *  left empty follows the raid's. The timeline above the fields is the draft's week as it stands (what an empty field
 *  inherits included) and a preset lights up while the hours match it. One POST, one registry write: a refusal comes
 *  back as a toast and nothing is saved. */
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
  /** the draft's week as it stands: a typed value, else what the empty field inherits */
  const effNum = (k: string): number => (d.cadence[k] !== undefined && d.cadence[k] !== "" ? Number(d.cadence[k]) : Number(inherited(k)[0] ?? 0));
  const effBool = (k: string): boolean => (d.cadence[k] ? d.cadence[k] === "on" : Boolean(inherited(k)[0]));
  const week: WeekRule = { signup_lead_hours: effNum("signup_lead_hours"), nudge_hours_before: effNum("nudge_hours_before"), lock_hours_before: effNum("lock_hours_before"), confirm_hours_before: effNum("confirm_hours_before"), fill_ask_hours: effNum("fill_ask_hours"),
    nudge: !adhoc && effBool("nudge"), confirm: effBool("confirm"), autofill: effBool("autofill"), open_dm: !adhoc && effBool("open_dm"), overridden: [] };
  const timeWords = (hm: string) => slotLabel(`Mon ${hm}`).replace(/^Mon /, "");
  const start = adhoc ? "when an officer opens it" : d.kind === "lockout" ? (d.days.length ? `day${d.days.length > 1 ? "s" : ""} ${d.days.join(", ")} · ${timeWords(d.time)}` : "pick the lockout days") : d.slots.length === 1 ? slotLabel(d.slots[0]) : d.slots.length ? `${d.slots.length} run times a week` : "pick the run times";
  const startWhy = adhoc ? "a pickup template opens nothing by itself" : d.kind === "lockout" ? `day 1 is the day the ${r.lockout_days}-day lockout resets` : d.slots.map(slotLabel).join(" · ") || "one sheet per run time per week";
  const presetOn = (p: Record<string, unknown>) => PRESET_KEYS.every((k) => effNum(k) === Number(p[k]));

  const id = s ? s.id : slug(d.name, d.kind, r.schedules.map((x) => x.id));
  /** The draft as schedule fields — for an existing schedule ONLY the fields that differ from what was loaded (design RD-10:
   *  a Save never re-sends `active: true` over a pause made elsewhere a moment ago, nor any setting nobody touched). */
  function fieldsOf(dd: Draft): Record<string, unknown> {
    const all = (x: Draft): Record<string, unknown> => {
      const f: Record<string, unknown> = { name: x.name.trim() || null, active: x.active, rosters: Number(x.rosters) };
      if (!s) f.kind = x.kind;
      if (x.kind === "weekly") f.slots = x.slots;
      if (x.kind === "lockout") { f.days = x.days.map(Number); f.time = x.time; }
      for (const k of HOURS) f[k] = x.cadence[k] === undefined || x.cadence[k] === "" ? null : Number(x.cadence[k]);
      for (const k of BOOLS) f[k] = x.cadence[k] ? x.cadence[k] === "on" : null;
      f.split_policy = x.cadence.split_policy || null;
      f.weights = Object.fromEntries(WEIGHTS.map((k) => [k, x.weights[k] === undefined || x.weights[k] === "" ? null : Number(x.weights[k])]));
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
    <div className={css.editor}>
      <Stack gap="sm">
        <Text fw={600} size="sm">{s ? `Edit ${s.name}` : `Add a schedule to ${r.name}`}</Text>
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
        <Text size="sm" fw={500} mt="xs">{adhoc ? "An ad hoc run's week" : "A run's week"} <Text span size="xs" c="dimmed">— empty follows {adhoc ? "the ad hoc defaults (it takes only the raid's size, make-up and lockout)" : "the raid's"}</Text></Text>
        <RunWeek r={week} start={start} startWhy={startWhy} />
        {!adhoc && (meta?.cadence_presets || []).length > 0 && (
          <Group gap={6} wrap="wrap">
            <Text size="xs" c="dimmed">Presets:</Text>
            {meta!.cadence_presets!.map((p) => {
              const on = presetOn(p as unknown as Record<string, unknown>);
              return (
                <Tooltip key={p.key} label={`sheet opens ${p.signup_lead_hours} h · reminder ${p.nudge_hours_before} h · locks ${p.lock_hours_before} h · confirm by ${p.confirm_hours_before} h before`}>
                  <Button size="compact-xs" variant={on ? "light" : "default"} color={on ? "teal" : undefined} onClick={() => setD({ ...d, cadence: { ...d.cadence, signup_lead_hours: String(p.signup_lead_hours), nudge_hours_before: String(p.nudge_hours_before), lock_hours_before: String(p.lock_hours_before), confirm_hours_before: String(p.confirm_hours_before) } })}>{p.name}</Button>
                </Tooltip>
              );
            })}
          </Group>
        )}
        <Group gap="md" align="flex-start" wrap="wrap">
          {HOURS.filter((k) => !adhoc || !ADHOC_HIDDEN.has(k)).map((k) => {
            const [v, from] = inherited(k);
            return <NumberInput key={k} label={k === "fill_ask_hours" ? "a fill ask waits (h)" : `${HOUR_WORDS[k]} (h before)`} description={HOUR_HINT[k]} min={0} w={190} placeholder={`${from === "raid" ? "the raid's" : from}: ${v}`} value={d.cadence[k] ?? ""} onChange={(x) => setC(k, x === "" ? "" : String(x))} />;
          })}
        </Group>
        <Group gap="md" align="flex-start" wrap="wrap">
          {BOOLS.filter((k) => !adhoc || !ADHOC_HIDDEN.has(k)).map((k) => {
            const [v, from] = inherited(k);
            return <Select key={k} label={SWITCH_WORDS[k]} description={SWITCH_HINT[k]} w={230} allowDeselect={false} value={d.cadence[k] || ""} onChange={(x) => setC(k, x || "")}
              data={[{ value: "", label: `${from === "raid" ? "the raid's" : from} (${v ? "on" : "off"})` }, { value: "on", label: "on" }, { value: "off", label: "off" }]} />;
          })}
          <Select label="when one night fills two groups" description="how the two groups are made" w={230} allowDeselect={false} value={d.cadence.split_policy || ""} onChange={(v) => setC("split_policy", v || "")}
            data={[{ value: "", label: `the raid's (${SPLIT_LABEL[String(raidVal("split_policy"))] || raidVal("split_policy")})` }, ...policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))]} />
        </Group>
        <Text size="sm" fw={500} mt="xs">Who gets a seat <Text span size="xs" c="dimmed">— empty follows {adhoc ? "first come (signup order)" : "the raid's"}</Text></Text>
        <Group gap="md" align="flex-start" wrap="wrap">
          {WEIGHTS.map((k) => (
            <NumberInput key={k} label={WEIGHT_WORDS[k]} description={WEIGHT_HINT[k]} min={0} w={170} placeholder={`${s?.weights ? String(s.weights[k]) : adhoc ? String(ADHOC_WEIGHT[k]) : String(r.weights[k] ?? "")}`}
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
    </div>
  );
}
