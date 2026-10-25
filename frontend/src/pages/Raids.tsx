import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { useEffect, useRef, useState } from "react";
import { Badge, Box, Button, Card, Group, NumberInput, Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import { IconPencil } from "@tabler/icons-react";
import { api, type CompSlot, type Meta, type RaidRule, type Raids, type Readiness } from "../api";
import { RaidHeader } from "../components/RaidHeader";
import { PageTitle, fail } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { Schedules, raidRevs } from "../components/ScheduleCard";
import { useOpsSave } from "../components/ReviewModal";
import { useDirty } from "../hooks/useDirty";
import { CompTargets } from "../components/CompTargets";
import { Pair, Panel, RunWeek, SeatWeights, Section, WEIGHT_HINT, WEIGHT_WORDS } from "../components/RaidCard";

export function RaidsPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Raids | null>(null);
  const [slots, setSlots] = useState<CompSlot[] | null>(null);
  const editing = useRef(0);  // edit modes open on this page (rules, aims, a schedule editor): live reloads wait for them
  const onEditing = (on: boolean) => { editing.current = Math.max(0, editing.current + (on ? 1 : -1)); };
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  useEffect(() => { load(); api.get<Readiness>("/api/readiness").then((r) => setSlots(r.slots)).catch(() => setSlots([])); }, []);
  useRefresh(load);
  useLive(() => { if (!editing.current) load(); }, ["config"]);  // a change made elsewhere shows — never under a draft (design §2.4)
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Raids" intro={`Each raid's rules: a run's week (when its sheet opens, locks and must be confirmed by), the group make-up and who gets a seat when more people join than there are seats — then its schedules, which say when it runs. Amber marks a value changed from the game's default. Times are ${data.tz}.`} />
      {data.raids.map((r) => <RuleCard key={r.id} r={r} meta={meta} slots={slots} owner={data.owner} weightKeys={data.weight_keys} policies={data.split_policies} maxRosters={data.max_rosters} onSaved={load} onEditing={onEditing} />)}
    </Stack>
  );
}

type Draft = { split_policy: string; nudge: boolean; autofill: boolean; open_dm: boolean; confirm: boolean; nudge_hours_before: number | string; signup_lead_hours: number | string; lock_hours_before: number | string; confirm_hours_before: number | string; fill_ask_hours: number | string; weights: Record<string, number | string>; first_open: string; lockout_days: number | string; duration_hours: number | string; notes: string; comp: Record<string, { min: number | string; max: number | string }> };

/** A raid setting as the reset confirm names it (the reader's words, never the stored key). */
const SETTING_WORDS: Record<string, string> = {
  signup_lead_hours: "when the sheet opens", nudge: "the reminder", nudge_hours_before: "when the reminder goes", lock_hours_before: "when it locks",
  confirm: "confirmations", confirm_hours_before: "the confirm-by time", fill_ask_hours: "how long a fill ask waits", autofill: "filling seats automatically",
  open_dm: "the DM when the sheet opens", split_policy: "how one night's two groups are made", first_open: "when the first sheet opens", lockout_days: "the lockout",
  duration_hours: "how long a run lasts", notes: "the notes", size: "the size", slots: "the run times", schedules: "the schedules", comp_targets: "the officers' aims",
  comp_groups: "the group layout", name: "the name",
};
function settingWords(k: string): string {
  const w = /^weight_(.+)$/.exec(k);
  if (w) return `the ${WEIGHT_WORDS[w[1]] || w[1].replace(/_/g, " ")} weight`;
  const c = /^(tank|healer|dps)_(min|max)$/.exec(k);
  if (c) return `the ${c[1]} ${c[2] === "min" ? "minimum" : "maximum"}`;
  return SETTING_WORDS[k] || k.replace(/_/g, " ");
}

function RuleCard({ r, meta, slots, owner, weightKeys, policies, maxRosters, onSaved, onEditing }: { r: RaidRule; meta: Meta; slots: CompSlot[] | null; owner: boolean; weightKeys: string[]; policies: string[]; maxRosters: number; onSaved: () => void; onEditing: (on: boolean) => void }) {
  const [editing, setEditingState] = useState(false);
  const setEditing = (on: boolean) => { if (on !== editing) onEditing(on); setEditingState(on); };
  const [d, setD] = useState<Draft>(() => toDraft(r));
  const [busy, setBusy] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const [saveOps, review] = useOpsSave();
  useDirty(`raid-rules:${r.id}`, editing);
  useEffect(() => setD(toDraft(r)), [r]);
  const over = (k: string) => r.overridden.includes(k);

  async function save() {
    setBusy(true);
    try {
      const x = await saveOps([{ kind: "raid", target: r.id, fields: d }], { ...raidRevs(r), title: `${r.name} rules` });
      if (x?.changed?.length) onSaved();  // changed meanwhile: reload under the draft, the next Save carries the new rev
      else if (x && !x.refused?.length) { setEditing(false); onSaved(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function reset() {
    const what = r.overridden.map(settingWords).join(", ");
    const n = r.overridden.length;
    if (!(await ask({ title: `Reset ${r.name} to the game's defaults?`, message: `The guild's ${n} change${n === 1 ? "" : "s"} to this raid go back to the game's default${n === 1 ? "" : "s"}: ${what}.${r.overridden.includes("slots") || r.overridden.includes("schedules") ? " Its schedules go too: no sheet opens for this raid until you add one again." : ""}${r.live ? ` The ${r.live} live sheet${r.live === 1 ? "" : "s"} keep their times.` : ""}`, confirmLabel: "Reset", color: "red" }))) return;
    setBusy(true);
    try {
      const x = await saveOps([{ kind: "raid_reset", target: r.id }], { ...raidRevs(r), title: `Reset ${r.name}` });
      if (x?.applied?.length) { setEditing(false); onSaved(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const chip = (on: boolean) => (on ? <Badge component="span" size="xs" color="yellow" variant="light" style={{ textTransform: "none" }}>changed</Badge> : null);
  const metaParts: [string, boolean][] = [
    [`${r.size}-player`, over("size")],
    [`${r.lockout_days}-day lockout`, over("lockout_days")],
    [`${r.duration_hours} h`, over("duration_hours")],
    [r.first_open_words ? `first opens ${r.first_open_words}` : "no first opening yet — the owner sets it under Edit rules", over("first_open")],
  ];
  const metaLine = (
    <Group component="span" gap={6} wrap="wrap" style={{ rowGap: 2 }}>
      {metaParts.map(([text, changed], i) => (
        <Group component="span" key={i} gap={6} wrap="nowrap">{i > 0 && <Text span size="xs" c="dimmed" opacity={0.6}>·</Text>}<Text span size="xs" c="dimmed">{text}</Text>{chip(changed)}</Group>
      ))}
    </Group>
  );
  const limits = Object.fromEntries(["tank", "healer", "dps"].filter((role) => r.comp[role]).map((role) => [role, { min: r.comp[role].min ?? 0, max: r.comp[role].max ?? null }]));
  const limitsChanged = ["tank", "healer", "dps"].filter((role) => over(`${role}_min`) || over(`${role}_max`));
  const split = (SPLIT_LABEL[r.split_policy] || r.split_policy).toLowerCase();

  return (
    <Card id={`raid-${r.id}`} style={{ scrollMarginTop: 16 }}>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={owner && !editing ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={() => setEditing(true)}>Edit rules</Button> : null} />
      <Box p="md">
        {!editing ? (
          <Stack gap="lg">
            <Section title="A run's week" hint="when each step happens, counted back from the raid · amber = changed from the game's default">
              <RunWeek r={r} />
            </Section>
            <Pair>
              <Section title="Group make-up" hint="how many of each role a run takes">
                <Panel>
                  <CompTargets meta={meta} rid={r.id} size={r.size} targets={r.comp_targets} bounds={limits} boundsChanged={limitsChanged} slots={slots} rev={r.rev} editable onSaved={onSaved} onEditing={onEditing} />
                </Panel>
                {r.comp_groups.length > 0 && <Text size="xs" c="dimmed">Group layout: {r.comp_groups.join(", ")} (set in plain text in the analytics channel)</Text>}
              </Section>
              <Section title="Who gets a seat" hint="when more people join a recurring run than there are seats, these count — a higher number counts more; an ad hoc run is first come">
                <Panel>
                  <SeatWeights r={r} keys={weightKeys} />
                  <Text size="xs" c={over("split_policy") ? "yellow" : "dimmed"} mt={4}>When one night fills two groups: {split} — {SPLIT_BLURB[r.split_policy] || split}.</Text>
                </Panel>
              </Section>
            </Pair>
            {r.notes && <Text size="sm" c="dimmed"><Text span inherit fw={600} c={over("notes") ? "yellow" : undefined}>Notes</Text>{"  "}{r.notes}</Text>}
            {!owner && <Text size="xs" c="dimmed">The owner edits the raid's rules; officers set the aims.</Text>}
          </Stack>
        ) : (
          <Stack gap="sm">
            <Text size="xs" c="dimmed">The raid's timing: every schedule below follows it unless the schedule sets its own. Run times are set per schedule.</Text>
            <Group gap="md" wrap="wrap" align="flex-end">
              <NumberInput label="signup opens (h before)" min={1} value={d.signup_lead_hours} onChange={(v) => setD({ ...d, signup_lead_hours: v })} w={190} />
              <NumberInput label="reminder (h before)" description="one DM to the regulars who haven't answered" min={0} value={d.nudge_hours_before} onChange={(v) => setD({ ...d, nudge_hours_before: v })} w={170} disabled={!d.nudge} />
              <Switch label="reminder on" checked={d.nudge} onChange={(e) => setD({ ...d, nudge: e.currentTarget.checked })} mb={6} />
              <Switch label="fill seats automatically after lock" checked={d.autofill} onChange={(e) => setD({ ...d, autofill: e.currentTarget.checked })} mb={6} />
              <Switch label="DM the regulars (every main while a schedule has none) when the sheet opens" checked={d.open_dm} onChange={(e) => setD({ ...d, open_dm: e.currentTarget.checked })} mb={6} />
              <Switch label="ask the rostered to confirm" checked={d.confirm} onChange={(e) => setD({ ...d, confirm: e.currentTarget.checked })} mb={6} />
              <NumberInput label="locks (h before)" min={0} value={d.lock_hours_before} onChange={(v) => setD({ ...d, lock_hours_before: v })} w={150} />
              <NumberInput label="confirm by (h before)" min={0} value={d.confirm_hours_before} onChange={(v) => setD({ ...d, confirm_hours_before: v })} w={170} />
              <NumberInput label="fill asks (h to answer)" description="no answer counts as no" min={0} value={d.fill_ask_hours} onChange={(v) => setD({ ...d, fill_ask_hours: v })} w={170} />
              <Select label="when one night fills two groups" description="how the two groups are made" data={policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))} value={d.split_policy} onChange={(v) => setD({ ...d, split_policy: v || d.split_policy })} w={200} />
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
      </Box>
      <Schedules r={r} meta={meta} owner={owner} policies={policies} maxRosters={maxRosters} onSaved={onSaved} onEditing={onEditing} />
      {confirmDialog}
      {review}
    </Card>
  );
}

export const SPLIT_LABEL: Record<string, string> = { balanced: "Balanced", first: "Raid one first", rotation: "Rotation" };
export const SPLIT_BLURB: Record<string, string> = { balanced: "both made equally strong", first: "raid one is made as strong as it can be; raid two takes the rest", rotation: "whoever joined last lockout but wasn't in raid one moves into raid one" };

function toDraft(r: RaidRule): Draft {
  return { split_policy: r.split_policy, nudge: r.nudge, autofill: r.autofill, open_dm: r.open_dm, confirm: r.confirm !== false, nudge_hours_before: r.nudge_hours_before, signup_lead_hours: r.signup_lead_hours, lock_hours_before: r.lock_hours_before, confirm_hours_before: r.confirm_hours_before, fill_ask_hours: r.fill_ask_hours, weights: { ...r.weights },
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
