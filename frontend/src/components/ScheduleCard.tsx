import { useState } from "react";
import { Badge, Box, Button, Group, Modal, MultiSelect, NumberInput, SegmentedControl, Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { TimePicker } from "@mantine/dates";
import { IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, type RaidRule, type Schedule } from "../api";
import { useConfirm } from "./ConfirmModal";
import { Eyebrow, fail, ok } from "./Page";
import { SlotEditor } from "./SlotEditor";

const HOURS: [string, string][] = [["signup_lead_hours", "sheet opens (h before)"], ["nudge_hours_before", "nudge (h before)"], ["lock_hours_before", "locks (h before)"],
  ["confirm_hours_before", "confirm by (h before)"], ["fill_ask_hours", "fill asks (h to answer)"]];
const BOOLS: [string, string][] = [["nudge", "nudge the unanswered"], ["autofill", "fill seats automatically"], ["open_dm", "DM everyone on open"], ["confirm", "ask the rostered to confirm"]];
/** Ad hoc (pickup) runs open on demand and remind nobody: these fields mean nothing there, so the editor hides them. */
const ADHOC_HIDDEN = new Set(["signup_lead_hours", "nudge_hours_before", "nudge", "open_dm"]);
const WEIGHTS: [string, string][] = [["regular", "being a regular"], ["rank", "rank"], ["main", "main over alt"], ["sat_out", "sat out last time"], ["signup_order", "signed up early"]];
const SOURCE_WORDS: Record<string, string> = { raid: "raid", adhoc: "ad hoc default", own: "own" };
const SPLIT_LABEL: Record<string, string> = { balanced: "Balanced", first: "Raid one first", rotation: "Rotation" };
const KIND_LABEL: Record<string, string> = { weekly: "Weekly nights", lockout: "Days of each lockout", pickup: "Pickup template" };
const OWN_WORDS: Record<string, string> = { signup_lead_hours: "opens", nudge_hours_before: "nudge", lock_hours_before: "locks", confirm_hours_before: "confirm by", fill_ask_hours: "fill asks",
  nudge: "nudge", autofill: "autofill", open_dm: "DM on open", confirm: "confirmations", split_policy: "split" };

/** What a schedule sets itself, in words ("locks 12 h before · split Rotation"); empty = all the raid's. */
function ownWords(s: Schedule): string {
  return Object.entries(s.own).map(([k, v]) => `${OWN_WORDS[k] || k} ${typeof v === "boolean" ? (v ? "on" : "off") : k === "split_policy" ? SPLIT_LABEL[String(v)] || v : `${v} h${k === "fill_ask_hours" ? "" : " before"}`}`).join(" · ");
}

/** A raid's schedules: WHEN it runs. Each is weekly nights, day N of each lockout, or a pickup template officers open on
 *  demand, with the rosters a run expects and any cadence of its own (the rest follows the raid's rules above). */
export function Schedules({ r, owner, policies, maxRosters, onSaved }: { r: RaidRule; owner: boolean; policies: string[]; maxRosters: number; onSaved: () => void }) {
  const [edit, setEdit] = useState<Schedule | "new" | null>(null);
  const [ask, confirmDialog] = useConfirm();
  async function remove(s: Schedule) {
    const extra = s.is_default ? " These are the raid's own run times, so it is left with none." : "";
    if (!(await ask({ title: `Remove ${s.name}?`, message: `${s.label}.${extra} Runs already open keep their times.`, confirmLabel: "Remove", color: "red" }))) return;
    try { const x = await api.post<{ message: string }>("/api/admin/raid/schedule/remove", { instance: r.id, id: s.id }); ok(x.message); onSaved(); } catch (e) { fail(e); }
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
          <Group key={s.id} justify="space-between" align="flex-start" wrap="nowrap" p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
            <Box style={{ minWidth: 0 }}>
              <Group gap={6} wrap="wrap">
                <Text fw={600} size="sm">{s.name}</Text>
                <Badge size="xs" color={s.class === "adhoc" ? "blue" : "teal"} variant="light">{s.class_label || (s.kind === "pickup" ? "Ad hoc" : "Recurring")}</Badge>
                {!s.active && <Badge size="xs" color="gray" variant="light">paused</Badge>}
                {s.kind === "pickup" && <Badge size="xs" color="blue" variant="outline">template</Badge>}
                {s.class === "recurring" && <Text size="xs" c="dimmed">{s.regulars || 0} regular{s.regulars === 1 ? "" : "s"}</Text>}
              </Group>
              <Text size="sm">{s.label}</Text>
              {s.kind !== "pickup" && <Text size="xs" c="dimmed">{s.next.length ? `next: ${s.next.join(" · ")}` : "no run coming up"}</Text>}
              <Text size="xs" c={Object.keys(s.own).length ? "yellow" : "dimmed"}>{Object.keys(s.own).length ? `own cadence: ${ownWords(s)}` : s.class === "adhoc" ? "the ad hoc defaults: first come, no reminder, no confirmations, locks 2 h before" : "the raid's cadence"}</Text>
            </Box>
            {owner && (
              <Group gap={4} wrap="nowrap">
                <Button size="xs" variant="subtle" leftSection={<IconPencil size={14} />} onClick={() => setEdit(s)}>Edit</Button>
                <Button size="xs" variant="subtle" color="red" onClick={() => remove(s)} aria-label={`remove ${s.name}`}><IconTrash size={14} /></Button>
              </Group>
            )}
          </Group>
        ))}
      </Stack>
      {edit && <ScheduleEditor r={r} s={edit === "new" ? null : edit} policies={policies} maxRosters={maxRosters} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); onSaved(); }} />}
      {confirmDialog}
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
function ScheduleEditor({ r, s, policies, maxRosters, onClose, onSaved }: { r: RaidRule; s: Schedule | null; policies: string[]; maxRosters: number; onClose: () => void; onSaved: () => void }) {
  const [d, setD] = useState<Draft>(() => toDraft(s));
  const [busy, setBusy] = useState(false);
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

  async function save() {
    const fields: Record<string, unknown> = { name: d.name.trim() || null, active: d.active, rosters: Number(d.rosters) };
    if (!s) fields.kind = d.kind;
    if (d.kind === "weekly") fields.slots = d.slots;
    if (d.kind === "lockout") { fields.days = d.days.map(Number); fields.time = d.time; }
    for (const [k] of HOURS) fields[k] = d.cadence[k] === undefined || d.cadence[k] === "" ? null : Number(d.cadence[k]);
    for (const [k] of BOOLS) fields[k] = d.cadence[k] ? d.cadence[k] === "on" : null;
    fields.split_policy = d.cadence.split_policy || null;
    fields.weights = Object.fromEntries(WEIGHTS.map(([k]) => [k, d.weights[k] === undefined || d.weights[k] === "" ? null : Number(d.weights[k])]));
    if (adhoc) for (const k of ADHOC_HIDDEN) delete fields[k];
    if (s?.is_default) delete fields.kind;
    const id = s ? s.id : slug(d.name, d.kind, r.schedules.map((x) => x.id));
    setBusy(true);
    try { const x = await api.post<{ message: string }>("/api/admin/raid/schedule", { instance: r.id, id, fields }); ok(x.message); onSaved(); } catch (e) { fail(e); } finally { setBusy(false); }
  }

  const hm = /^(\d{1,2}):(\d{2})/.exec(d.time || "");
  return (
    <Modal opened onClose={onClose} title={s ? `Edit ${s.name}` : `Add a schedule to ${r.name}`} size="lg">
      <Stack gap="sm">
        <Group gap="md" align="flex-end" wrap="wrap">
          <TextInput label="name" placeholder={d.kind === "pickup" ? "Pickup" : "Main night, Alt run…"} value={d.name} onChange={(e) => setD({ ...d, name: e.currentTarget.value })} maxLength={40} w={220} />
          <Select label="rosters per run" description={`the sheet advertises this × ${r.size} seats`} data={Array.from({ length: maxRosters }, (_, i) => ({ value: String(i + 1), label: `${i + 1} roster${i ? "s" : ""}` }))}
            value={d.rosters} onChange={(v) => setD({ ...d, rosters: v || "1" })} allowDeselect={false} w={200} />
          <Switch label="active" description="paused: nothing opens from it" checked={d.active} onChange={(e) => setD({ ...d, active: e.currentTarget.checked })} />
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
        <Text size="sm" fw={500} mt="xs">{adhoc ? "Ad hoc" : "Recurring"} cadence <Text span size="xs" c="dimmed">— empty follows {adhoc ? "the ad hoc defaults (it takes only the raid's size, comp and lockout)" : "the raid's rules"}</Text></Text>
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
          <Select label="split a full run" w={200} allowDeselect={false} value={d.cadence.split_policy || ""} onChange={(v) => setC("split_policy", v || "")}
            data={[{ value: "", label: `the raid's (${SPLIT_LABEL[String(raidVal("split_policy"))] || raidVal("split_policy")})` }, ...policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))]} />
        </Group>
        <Text size="sm" fw={500} mt="xs">Seat order at lock <Text span size="xs" c="dimmed">— empty follows {adhoc ? "first come (signup order)" : "the raid's weights"}</Text></Text>
        <Group gap="md" align="flex-end" wrap="wrap">
          {WEIGHTS.map(([k, label]) => (
            <NumberInput key={k} label={label} min={0} w={150} placeholder={`${s?.weights ? String(s.weights[k]) : adhoc ? String(ADHOC_WEIGHT[k]) : String(r.weights[k] ?? "")}`}
              value={d.weights[k] ?? ""} onChange={(x) => setW(k, x === "" ? "" : String(x))} />
          ))}
        </Group>
        <Group gap="sm" justify="flex-end" mt="sm">
          <Button variant="default" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" loading={busy} onClick={save}>{s ? "Save schedule" : "Add schedule"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
