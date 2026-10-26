import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { useEffect, useRef, useState } from "react";
import { Box, Button, Card, Group, NumberInput, Select, Stack, Switch, Text, TextInput, Tooltip } from "@mantine/core";
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
import { HOUR_HINT, HOUR_WORDS, Pair, Panel, RunWeek, SWITCH_HINT, SWITCH_WORDS, SeatWeights, Section, WEIGHT_HINT, WEIGHT_WORDS, WhenItRuns, type WeekRule } from "../components/RaidCard";

export function RaidsPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Raids | null>(null);
  const [slots, setSlots] = useState<CompSlot[] | null>(null);
  const editing = useRef(0);  // edit modes open on this page (rules, aims, a schedule editor): live reloads wait for them
  const onEditing = (on: boolean) => { editing.current = Math.max(0, editing.current + (on ? 1 : -1)); };
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  useEffect(() => { load(); api.get<Readiness>("/api/readiness").then((r) => setSlots(r.slots)).catch(() => setSlots([])); }, []);
  useRefresh(load);
  useLive(() => { if (!editing.current) load(); }, ["config", "run"]);  // a change made elsewhere shows — never under a draft (design §2.4)
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const zone = meta.timezones?.flatMap((g) => g.items).find((i) => i.value === data.tz)?.label || data.tz;
  return (
    <Stack gap="lg">
      <PageTitle title="Raids" intro={`Each raid: when it runs, its rules — a run's week, the group make-up, who gets a seat — and its schedules. Amber marks a value changed from the game's default. Times in ${zone}.`} />
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

/** One edit mode per card at a time (design §5.54): the rules, the aims or a schedule — the other two's buttons wait. */
type Mode = "rules" | "aims" | "schedule" | null;
const HOURS = ["signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours"] as const;
const SWITCHES = ["nudge", "confirm", "autofill", "open_dm"] as const;

function RuleCard({ r, meta, slots, owner, weightKeys, policies, maxRosters, onSaved, onEditing }: { r: RaidRule; meta: Meta; slots: CompSlot[] | null; owner: boolean; weightKeys: string[]; policies: string[]; maxRosters: number; onSaved: () => void; onEditing: (on: boolean) => void }) {
  const [mode, setModeState] = useState<Mode>(null);
  const editing = mode === "rules";
  const setMode = (m: Mode) => { if ((m !== null) !== (mode !== null)) onEditing(m !== null); setModeState(m); };
  const childEditing = (m: Mode) => (on: boolean) => setMode(on ? m : null);
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
      else if (x && !x.refused?.length) { setMode(null); onSaved(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function reset() {
    const what = r.overridden.map(settingWords).join(", ");
    const n = r.overridden.length;
    if (!(await ask({ title: `Reset ${r.name} to the game's defaults?`, message: `The guild's ${n} change${n === 1 ? "" : "s"} to this raid go back to the game's default${n === 1 ? "" : "s"}: ${what}.${r.overridden.includes("slots") || r.overridden.includes("schedules") ? " Its schedules go too: no sheet opens for this raid until you add one again." : ""}${r.live ? ` The ${r.live} live sheet${r.live === 1 ? "" : "s"} keep their times.` : ""}`, confirmLabel: "Reset", color: "red" }))) return;
    setBusy(true);
    try {
      const x = await saveOps([{ kind: "raid_reset", target: r.id }], { ...raidRevs(r), title: `Reset ${r.name}` });
      if (x?.applied?.length) { setMode(null); onSaved(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  // the header's facts: amber = changed from the game's default (the card's one convention; the tooltip says so)
  const metaParts: [string, boolean][] = [[`${r.size}-player`, over("size")], [`${r.lockout_days}-day lockout`, over("lockout_days")], [`${r.duration_hours} h`, over("duration_hours")]];
  const metaLine = (
    <>
      <Group component="span" gap={12} wrap="wrap" style={{ rowGap: 2 }}>
        {metaParts.map(([text, changed]) => (changed
          ? <Tooltip key={text} label="changed from the game's default"><Text span size="xs" c="yellow">{text}</Text></Tooltip>
          : <Text key={text} span size="xs" c="dimmed">{text}</Text>))}
      </Group>
      {r.notes && <Text span size="xs" c={over("notes") ? "yellow" : "dimmed"} style={{ display: "block", marginTop: 2 }}>{r.notes}</Text>}
    </>
  );
  const limits = Object.fromEntries(["tank", "healer", "dps"].filter((role) => r.comp[role]).map((role) => [role, { min: r.comp[role].min ?? 0, max: r.comp[role].max ?? null }]));
  const limitsChanged = ["tank", "healer", "dps"].filter((role) => over(`${role}_min`) || over(`${role}_max`));
  const split = (SPLIT_LABEL[r.split_policy] || r.split_policy).toLowerCase();
  const right = !owner ? <Text size="xs" c="dimmed">The owner edits the rules; officers set the aims.</Text>
    : mode === null ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={() => setMode("rules")}>Edit rules</Button> : null;

  // the editor's timeline moves with the draft; amber still marks what is stored as changed
  const num = (v: number | string) => (v === "" ? 0 : Number(v));
  const draftRule: WeekRule = { signup_lead_hours: num(d.signup_lead_hours), nudge_hours_before: num(d.nudge_hours_before), lock_hours_before: num(d.lock_hours_before), confirm_hours_before: num(d.confirm_hours_before), fill_ask_hours: num(d.fill_ask_hours),
    nudge: d.nudge, confirm: d.confirm, autofill: d.autofill, open_dm: d.open_dm, overridden: r.overridden, slots: r.slots, slot_labels: r.slot_labels, schedules: r.schedules };
  const amber = (...ks: string[]) => (ks.some(over) ? { label: { color: "var(--mantine-color-yellow-5)" } } : undefined);

  return (
    <Card id={`raid-${r.id}`} style={{ scrollMarginTop: 16 }}>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={right} />
      <Box p="md">
        {!editing ? (
          <Stack gap="lg">
            <Section title="When it runs"><WhenItRuns r={r} owner={owner} /></Section>
            <Section title="A run's week" hint="counted back from the raid">
              <RunWeek r={r} />
            </Section>
            <Pair>
              <Section title="Group make-up" hint="how many of each role a run takes">
                <Panel>
                  <CompTargets meta={meta} rid={r.id} size={r.size} targets={r.comp_targets} bounds={limits} boundsChanged={limitsChanged} slots={slots} rev={r.rev} editable={mode === null || mode === "aims"} onSaved={onSaved} onEditing={childEditing("aims")} />
                </Panel>
                {r.comp_groups.length > 0 && <Text size="xs" c="dimmed">Group layout: {r.comp_groups.join(", ")} (set in plain text in the analytics channel)</Text>}
              </Section>
              <Section title="Who gets a seat" hint="when more people join a recurring run than there are seats; an ad hoc run is first come">
                <Panel>
                  <SeatWeights r={r} keys={weightKeys} />
                  <Text size="xs" c={over("split_policy") ? "yellow" : "dimmed"} mt={4}>When one night fills two groups: {split} — {SPLIT_BLURB[r.split_policy] || split}.</Text>
                </Panel>
              </Section>
            </Pair>
          </Stack>
        ) : (
          <Stack gap="lg">
            <Section title="A run's week" hint="hours before the raid; every schedule follows it unless it sets its own">
              <RunWeek r={draftRule} sentence={false} />
              <Group gap="md" wrap="wrap" align="flex-start">
                {HOURS.map((k) => (
                  <NumberInput key={k} label={k === "fill_ask_hours" ? "a fill ask waits (h)" : `${HOUR_WORDS[k]} (h before)`} description={HOUR_HINT[k]} min={k === "signup_lead_hours" ? 1 : 0} w={190} styles={amber(k)}
                    value={d[k]} onChange={(v) => setD({ ...d, [k]: v })} disabled={k === "nudge_hours_before" && !d.nudge} />
                ))}
              </Group>
              <Group gap="lg" wrap="wrap" align="flex-start">
                {SWITCHES.map((k) => (
                  <Switch key={k} label={SWITCH_WORDS[k]} description={SWITCH_HINT[k]} checked={d[k]} onChange={(e) => setD({ ...d, [k]: e.currentTarget.checked })} styles={amber(k)} />
                ))}
              </Group>
            </Section>
            <Pair>
              <Section title="Group make-up" hint="the limits: the fewest and most of each role a run takes — officers set the aims within them">
                <Group gap="md" wrap="wrap" align="flex-end">
                  {(["tank", "healer", "dps"] as const).map((role) => (
                    <Group key={role} gap={6} align="flex-end" wrap="nowrap">
                      <NumberInput label={`${role} at least`} min={0} value={d.comp[role].min} onChange={(v) => setD({ ...d, comp: { ...d.comp, [role]: { ...d.comp[role], min: v } } })} w={110} styles={amber(`${role}_min`)} />
                      <NumberInput label="at most" min={0} value={d.comp[role].max} onChange={(v) => setD({ ...d, comp: { ...d.comp, [role]: { ...d.comp[role], max: v } } })} w={100} styles={amber(`${role}_max`)} />
                    </Group>
                  ))}
                </Group>
              </Section>
              <Section title="Who gets a seat" hint="a higher number counts more">
                <Group gap="md" wrap="wrap" align="flex-start">
                  {weightKeys.map((k) => <NumberInput key={k} label={WEIGHT_WORDS[k] || k.replace(/_/g, " ")} description={WEIGHT_HINT[k]} min={0} value={d.weights[k]} onChange={(v) => setD({ ...d, weights: { ...d.weights, [k]: v } })} w={170} styles={amber(`weight_${k}`)} />)}
                  <Select label="when one night fills two groups" description="how the two groups are made" data={policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))} value={d.split_policy} onChange={(v) => setD({ ...d, split_policy: v || d.split_policy })} w={200} styles={amber("split_policy")} />
                </Group>
              </Section>
            </Pair>
            <Section title="The raid">
              <Group gap="md" wrap="wrap" align="flex-end">
                <DateTimePicker label="the first sheet opens" description="guild time; the lockout is counted from it" value={pickerValue(d.first_open)} onChange={(v) => setD({ ...d, first_open: fromPicker(v, d.first_open) })}
                  valueFormat="ddd D MMM YYYY h:mm A" timePickerProps={{ format: "12h", withDropdown: true, popoverProps: { withinPortal: false } }} popoverProps={{ withinPortal: true }} w={240} styles={amber("first_open")} />
                <NumberInput label="lockout (days)" min={1} step={1} value={d.lockout_days} onChange={(v) => setD({ ...d, lockout_days: v })} w={130} styles={amber("lockout_days")} />
                <NumberInput label="a run lasts (h)" min={0.5} step={0.5} value={d.duration_hours} onChange={(v) => setD({ ...d, duration_hours: v })} w={130} styles={amber("duration_hours")} />
                <TextInput label="notes" value={d.notes} onChange={(e) => setD({ ...d, notes: e.currentTarget.value })} styles={amber("notes")} style={{ flex: "1 1 240px" }} />
              </Group>
            </Section>
            <Group gap="sm" justify="flex-end">
              {r.overridden.length > 0 && <Button variant="subtle" color="red" size="sm" onClick={reset} disabled={busy} mr="auto">Reset to defaults</Button>}
              <Button variant="default" size="sm" onClick={() => { setD(toDraft(r)); setMode(null); }}>Cancel</Button>
              <Button size="sm" loading={busy} onClick={save}>Save rules</Button>
            </Group>
          </Stack>
        )}
      </Box>
      <Schedules r={r} meta={meta} owner={owner && (mode === null || mode === "schedule")} policies={policies} maxRosters={maxRosters} onSaved={onSaved} onEditing={childEditing("schedule")} />
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
