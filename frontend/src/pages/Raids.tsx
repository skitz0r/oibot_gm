import { useRefresh } from "../hooks/usePoll";
import { useEffect, useState } from "react";
import { Badge, Box, Button, Card, Group, NumberInput, Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import { IconPencil } from "@tabler/icons-react";
import { api, type RaidRule, type Raids } from "../api";
import { RaidHeader } from "../components/RaidHeader";
import { PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { Schedules } from "../components/ScheduleCard";

export function RaidsPage() {
  const [data, setData] = useState<Raids | null>(null);
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useRefresh(load);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Raids" intro={`Each raid's rules: cadence (signup opens, lock, confirmation deadline), first opening, lockout, desired comp, and how the solver weighs seats — then its schedules, which say when it runs. Amber marks an override of the game defaults. Times are ${data.tz}.`} />
      {data.raids.map((r) => <RuleCard key={r.id} r={r} owner={data.owner} weightKeys={data.weight_keys} policies={data.split_policies} maxRosters={data.max_rosters} onSaved={load} />)}
    </Stack>
  );
}

type Draft = { split_policy: string; nudge: boolean; autofill: boolean; open_dm: boolean; nudge_hours_before: number | string; signup_lead_hours: number | string; lock_hours_before: number | string; confirm_hours_before: number | string; fill_ask_hours: number | string; weights: Record<string, number | string>; first_open: string; lockout_days: number | string; duration_hours: number | string; notes: string; comp: Record<string, { min: number | string; max: number | string }> };

function RuleCard({ r, owner, weightKeys, policies, maxRosters, onSaved }: { r: RaidRule; owner: boolean; weightKeys: string[]; policies: string[]; maxRosters: number; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [d, setD] = useState<Draft>(() => toDraft(r));
  const [busy, setBusy] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  useEffect(() => setD(toDraft(r)), [r]);
  const over = (k: string) => r.overridden.includes(k);
  const oc = (k: string) => (over(k) ? "yellow" : undefined);

  async function save() {
    setBusy(true);
    try { const x = await api.post<{ message: string }>("/api/admin/raid", { instance: r.id, ...d }); ok(x.message); setEditing(false); onSaved(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function reset() {
    const what = r.overridden.map((k) => k.replace(/^weight_/, "weight ").replace(/_/g, " ")).join(", ");
    if (!(await ask({ title: `Reset ${r.name} to the game defaults?`, message: `${r.overridden.length} override${r.overridden.length === 1 ? "" : "s"} go: ${what}.${r.overridden.includes("slots") || r.overridden.includes("schedules") ? " Its schedules go too: no sheet opens for this raid until you add one again." : ""}${r.live ? ` The ${r.live} live sheet${r.live === 1 ? "" : "s"} keep their times.` : ""}`, confirmLabel: "Reset", color: "red" }))) return;
    setBusy(true);
    try { const x = await api.post<{ message: string }>("/api/admin/raid/reset", { instance: r.id }); ok(x.message); setEditing(false); onSaved(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const metaLine = [`${r.size}-player`, r.schedules.length ? `${r.schedules.length} schedule${r.schedules.length === 1 ? "" : "s"}` : "no schedules", r.live ? `${r.live} live` : null, r.opened ? `window ${r.window[0]} → ${r.window[1]}` : r.first_open_local ? `opens ${r.window[0]}` : "no opening date set"].filter(Boolean).join(" · ");

  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={owner && !editing ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={() => setEditing(true)}>Edit rules</Button> : null} />
      <Box p="md">
        {!editing ? (
          <Stack gap="md">
            <Group gap="xl" wrap="wrap">
              <Fact label="signup opens" value={`${r.signup_lead_hours} h before`} c={oc("signup_lead_hours")} />
              <Fact label="nudge" value={r.nudge ? `${r.nudge_hours_before} h before` : "off"} c={over("nudge") || over("nudge_hours_before") ? "yellow" : undefined} />
              <Fact label="fill after lock" value={r.autofill ? "automatic" : "by hand"} c={oc("autofill")} />
              <Fact label="DM on open" value={r.open_dm ? "on" : "off"} c={oc("open_dm")} />
              <Fact label="locks" value={`${r.lock_hours_before} h before`} c={oc("lock_hours_before")} />
              <Fact label="confirm by" value={`${r.confirm_hours_before} h before`} c={oc("confirm_hours_before")} />
              <Fact label="fill asks" value={`${r.fill_ask_hours} h to answer`} c={oc("fill_ask_hours")} />
              <Fact label="split policy" value={SPLIT_LABEL[r.split_policy] || r.split_policy} c={oc("split_policy")} />
            </Group>
            <Group gap="xl" wrap="wrap">
              <Fact label="first opens" value={r.first_open_local ? local12(r.first_open_local) : "—"} c={oc("first_open")} />
              <Fact label="lockout" value={`${r.lockout_days} days`} c={oc("lockout_days")} />
              <Fact label="duration" value={`${r.duration_hours} h`} c={oc("duration_hours")} />
              {(["tank", "healer", "dps"] as const).map((role) => <Fact key={role} label={role} value={`${r.comp[role]?.min ?? "?"}–${r.comp[role]?.max ?? "?"}`} c={over(`${role}_min`) || over(`${role}_max`) ? "yellow" : undefined} />)}
              <Fact label="seat weights" value={weightKeys.map((k) => `${k.replace("_", " ")} ${r.weights[k] ?? 0}`).join(" · ")} c={weightKeys.some((k) => over(`weight_${k}`)) ? "yellow" : undefined} />
              {r.notes && <Fact label="notes" value={r.notes} c={oc("notes")} />}
            </Group>
          </Stack>
        ) : (
          <Stack gap="sm">
            <Text size="xs" c="dimmed">The raid's cadence: every schedule below follows it unless the schedule sets its own. Run times are set per schedule.</Text>
            <Group gap="md" wrap="wrap" align="flex-end">
              <NumberInput label="signup opens (h before)" min={1} value={d.signup_lead_hours} onChange={(v) => setD({ ...d, signup_lead_hours: v })} w={190} />
              <NumberInput label="nudge (h before)" description="one DM to mains who haven't answered" min={0} value={d.nudge_hours_before} onChange={(v) => setD({ ...d, nudge_hours_before: v })} w={170} disabled={!d.nudge} />
              <Switch label="nudge on" checked={d.nudge} onChange={(e) => setD({ ...d, nudge: e.currentTarget.checked })} mb={6} />
              <Switch label="fill seats automatically after lock" checked={d.autofill} onChange={(e) => setD({ ...d, autofill: e.currentTarget.checked })} mb={6} />
              <Switch label="DM every raider when the sheet opens" checked={d.open_dm} onChange={(e) => setD({ ...d, open_dm: e.currentTarget.checked })} mb={6} />
              <NumberInput label="locks (h before)" min={0} value={d.lock_hours_before} onChange={(v) => setD({ ...d, lock_hours_before: v })} w={150} />
              <NumberInput label="confirm by (h before)" min={0} value={d.confirm_hours_before} onChange={(v) => setD({ ...d, confirm_hours_before: v })} w={170} />
              <NumberInput label="fill asks (h to answer)" description="no answer counts as no" min={0} value={d.fill_ask_hours} onChange={(v) => setD({ ...d, fill_ask_hours: v })} w={170} />
              <Select label="split policy" description="when more join than one run seats" data={policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))} value={d.split_policy} onChange={(v) => setD({ ...d, split_policy: v || d.split_policy })} w={200} />
            </Group>
            <Group gap="md" wrap="wrap" align="flex-end">
              <DateTimePicker label="first opens" description="guild time" value={pickerValue(d.first_open)} onChange={(v) => setD({ ...d, first_open: fromPicker(v, d.first_open) })}
                valueFormat="ddd DD MMM YYYY h:mm A" timePickerProps={{ format: "12h", withDropdown: true, popoverProps: { withinPortal: false } }} popoverProps={{ withinPortal: true }} w={240} />
              <NumberInput label="lockout days" min={1} step={1} value={d.lockout_days} onChange={(v) => setD({ ...d, lockout_days: v })} w={130} />
              <NumberInput label="duration h" min={0.5} step={0.5} value={d.duration_hours} onChange={(v) => setD({ ...d, duration_hours: v })} w={130} />
            </Group>
            <Group gap="md" wrap="wrap" align="flex-end">
              {(["tank", "healer", "dps"] as const).map((role) => (
                <Group key={role} gap={6} align="flex-end">
                  <NumberInput label={`${role} min`} min={0} value={d.comp[role].min} onChange={(v) => setD({ ...d, comp: { ...d.comp, [role]: { ...d.comp[role], min: v } } })} w={100} />
                  <NumberInput label="max" min={0} value={d.comp[role].max} onChange={(v) => setD({ ...d, comp: { ...d.comp, [role]: { ...d.comp[role], max: v } } })} w={100} />
                </Group>
              ))}
            </Group>
            <Group gap="md" wrap="wrap" align="flex-end">
              {weightKeys.map((k) => <NumberInput key={k} label={`weight: ${k.replace("_", " ")}`} description={WEIGHT_HINT[k]} min={0} value={d.weights[k]} onChange={(v) => setD({ ...d, weights: { ...d.weights, [k]: v } })} w={170} />)}
            </Group>
            <TextInput label="notes" value={d.notes} onChange={(e) => setD({ ...d, notes: e.currentTarget.value })} />
            <Group gap="sm" justify="flex-end">
              {r.overridden.length > 0 && <Button variant="subtle" color="red" size="sm" onClick={reset} disabled={busy} mr="auto">Reset to defaults</Button>}
              <Button variant="default" size="sm" onClick={() => { setD(toDraft(r)); setEditing(false); }}>Cancel</Button>
              <Button size="sm" loading={busy} onClick={save}>Save rules</Button>
            </Group>
          </Stack>
        )}
        {(Object.keys(r.comp_targets).length > 0 || r.comp_groups.length > 0) && (
          <Text size="xs" c="dimmed" mt="sm">Comp targets: {Object.keys(r.comp_targets).length ? JSON.stringify(r.comp_targets) : "—"} · group layout: {r.comp_groups.join(", ") || "default"} (set in plain text in the analytics channel)</Text>
        )}
        {!owner && <Text size="xs" c="dimmed" mt="sm">The owner edits raid rules.</Text>}
      </Box>
      <Schedules r={r} owner={owner} policies={policies} maxRosters={maxRosters} onSaved={onSaved} />
      {confirmDialog}
    </Card>
  );
}

export const SPLIT_LABEL: Record<string, string> = { balanced: "Balanced", first: "Raid one first", rotation: "Rotation" };
export const SPLIT_BLURB: Record<string, string> = { balanced: "both runs equal: synergy, tanks, healers and seat quality spread evenly", first: "roster 1 gets the best synergy and seat weights; roster 2 is the rest", rotation: "whoever sat out or was in the weaker run last window moves up" };
const WEIGHT_HINT: Record<string, string> = { rank: "core > raider > trial", main: "main over alt", sat_out: "benched last window", signup_order: "earlier signup" };

function toDraft(r: RaidRule): Draft {
  return { split_policy: r.split_policy, nudge: r.nudge, autofill: r.autofill, open_dm: r.open_dm, nudge_hours_before: r.nudge_hours_before, signup_lead_hours: r.signup_lead_hours, lock_hours_before: r.lock_hours_before, confirm_hours_before: r.confirm_hours_before, fill_ask_hours: r.fill_ask_hours, weights: { ...r.weights },
    first_open: r.first_open_local, lockout_days: r.lockout_days, duration_hours: r.duration_hours, notes: r.notes,
    comp: Object.fromEntries(["tank", "healer", "dps"].map((role) => [role, { min: r.comp[role]?.min ?? "", max: r.comp[role]?.max ?? "" }])) };
}

/** The draft keeps the API's machine form ('2026-12-09T15:00', guild time); the picker speaks 'YYYY-MM-DD HH:mm:ss'. */
function pickerValue(iso: string): string | null {
  return iso ? `${iso.slice(0, 16).replace("T", " ")}:00` : null;
}
function fromPicker(v: string | null, keep: string): string {
  return v ? v.slice(0, 16).replace(" ", "T") : keep;
}

/** '2026-12-09T15:00' (guild wall time, no zone) → 'Wed 9 Dec 2026 3:00 PM'. Read field by field: a Date would shift it
 *  into the viewer's zone, and this is guild time. */
function local12(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  const [y, mo, d, h, mi] = m.slice(1).map(Number);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(Date.UTC(y, mo - 1, d)).getUTCDay()];
  const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][mo - 1];
  return `${wd} ${d} ${mon} ${y} ${h % 12 || 12}:${String(mi).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

function Fact({ label, value, c }: { label: string; value: string; c?: string }) {
  return (
    <Box style={{ minWidth: 0, maxWidth: "100%" }}>
      <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: ".08em" }}>{label}{c && <Badge size="xs" color={c} variant="light" ml={6}>override</Badge>}</Text>
      <Text size="sm" fw={600} c={c} style={{ wordBreak: "break-word" }}>{value}</Text>
    </Box>
  );
}
