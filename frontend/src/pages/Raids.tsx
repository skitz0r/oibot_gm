import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { useEffect, useRef, useState } from "react";
import { Box, Button, Card, Group, NumberInput, Select, Stack, Text, TextInput, Tooltip } from "@mantine/core";
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
import { Pair, Panel, SeatWeights, Section, WEIGHT_HINT, WEIGHT_WORDS, WhenItRuns } from "../components/RaidCard";

export function RaidsPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Raids | null>(null);
  const [slots, setSlots] = useState<CompSlot[] | null>(null);
  const editing = useRef(0);  // edit modes open on this page (rules, minimums, a schedule editor): live reloads wait for them
  const onEditing = (on: boolean) => { editing.current = Math.max(0, editing.current + (on ? 1 : -1)); };
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  useEffect(() => { load(); api.get<Readiness>("/api/readiness").then((r) => setSlots(r.slots)).catch(() => setSlots([])); }, []);
  useRefresh(load);
  useLive(() => { if (!editing.current) load(); }, ["config", "run"]);  // a change made elsewhere shows — never under a draft (design §2.4)
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const zone = meta.timezones?.flatMap((g) => g.items).find((i) => i.value === data.tz)?.label || data.tz;
  return (
    <Stack gap="lg">
      <PageTitle title="Raids" intro={`Each raid: when it runs, its rules — the group make-up and who gets a seat — and its schedules, each with its own week. Amber marks a value changed from the game's default. Times in ${zone}.`} />
      {data.raids.map((r) => <RuleCard key={r.id} r={r} meta={meta} slots={slots} weightKeys={data.weight_keys} policies={data.split_policies} maxRosters={data.max_rosters} others={data.raids.filter((x) => x.id !== r.id).map((x) => ({ id: x.id, name: x.name, duration: x.duration_hours }))} taken={data.raids.flatMap((x) => x.schedules.map((sc) => sc.id))} onSaved={load} onEditing={onEditing} />)}
    </Stack>
  );
}

/** The raid's own rules (sheet timing is each schedule's, D16 minimums are their own editor): what Edit rules drafts. */
type Draft = { split_policy: string; weights: Record<string, number | string>; first_open: string; lockout_days: number | string; duration_hours: number | string; notes: string };

/** A raid setting as the reset confirm names it (the reader's words, never the stored key). */
const SETTING_WORDS: Record<string, string> = {
  split_policy: "how one night's two groups are made", first_open: "when the first sheet opens", lockout_days: "the lockout",
  duration_hours: "how long a run lasts", notes: "the notes", size: "the size", slots: "the run times", schedules: "the schedules",
  comp_groups: "the group layout", name: "the name",
};
function settingWords(k: string): string {
  const w = /^weight_(.+)$/.exec(k);
  if (w) return `the ${WEIGHT_WORDS[w[1]] || w[1].replace(/_/g, " ")} weight`;
  return SETTING_WORDS[k] || k.replace(/_/g, " ");
}

/** One edit mode per card at a time (design §5.54): the rules, the minimums or a schedule — the other two's buttons wait. */
type Mode = "rules" | "minimums" | "schedule" | null;

/** One raid's card. Every officer on the page edits (D8): the route is officers-only. */
function RuleCard({ r, meta, slots, weightKeys, policies, maxRosters, others, taken, onSaved, onEditing }: { r: RaidRule; meta: Meta; slots: CompSlot[] | null; weightKeys: string[]; policies: string[]; maxRosters: number; others: { id: string; name: string; duration?: number }[]; taken: string[]; onSaved: () => void; onEditing: (on: boolean) => void }) {
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
      // the reset's own rev covers the schedules it removes: one added since the page loaded is a 409, never removed unseen
      const x = await saveOps([{ kind: "raid_reset", target: r.id }], { revs: { ...raidRevs(r).revs, ...(r.reset_rev ? { [`config:raid_all:${r.id}`]: r.reset_rev } : {}) }, title: `Reset ${r.name}` });
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
  const split = (SPLIT_LABEL[r.split_policy] || r.split_policy).toLowerCase();
  const right = mode === null ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={() => setMode("rules")}>Edit rules</Button> : null;
  const amber = (...ks: string[]) => (ks.some(over) ? { label: { color: "var(--mantine-color-yellow-5)" } } : undefined);

  return (
    <Card id={`raid-${r.id}`} style={{ scrollMarginTop: 16 }}>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={right} />
      <Box p="md">
        {!editing ? (
          <Stack gap="lg">
            <Section title="When it runs"><WhenItRuns r={r} owner /></Section>
            <Pair>
              <Section title="Group make-up" hint="the fewest of each role or class a run takes">
                <Panel>
                  <CompTargets meta={meta} rid={r.id} size={r.size} minimums={r.minimums} minimum={r.minimum} slots={slots} rev={r.rev} editable={mode === null || mode === "minimums"} onSaved={onSaved} onEditing={childEditing("minimums")} />
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
            <Section title="Who gets a seat" hint="a higher number counts more; a schedule may set its own">
              <Group gap="md" wrap="wrap" align="flex-start">
                {weightKeys.map((k) => <NumberInput key={k} label={WEIGHT_WORDS[k] || k.replace(/_/g, " ")} description={WEIGHT_HINT[k]} min={0} value={d.weights[k]} onChange={(v) => setD({ ...d, weights: { ...d.weights, [k]: v } })} w={170} styles={amber(`weight_${k}`)} />)}
                <Select label="when one night fills two groups" description="how the two groups are made" data={policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))} value={d.split_policy} onChange={(v) => setD({ ...d, split_policy: v || d.split_policy })} w={200} styles={amber("split_policy")} />
              </Group>
            </Section>
            <Section title="The raid">
              <Group gap="md" wrap="wrap" align="flex-end">
                <DateTimePicker label="the first sheet opens" description="guild time; the lockout is counted from it" value={pickerValue(d.first_open)} onChange={(v) => setD({ ...d, first_open: fromPicker(v, d.first_open) })}
                  valueFormat="ddd D MMM YYYY h:mm A" timePickerProps={{ format: "12h", withDropdown: true, popoverProps: { withinPortal: false } }} popoverProps={{ withinPortal: true }} w={240} styles={amber("first_open")} />
                <NumberInput label="lockout (days)" min={1} step={1} value={d.lockout_days} onChange={(v) => setD({ ...d, lockout_days: v })} w={130} styles={amber("lockout_days")} />
                <NumberInput label="a run lasts (h)" min={0.5} step={0.5} value={d.duration_hours} onChange={(v) => setD({ ...d, duration_hours: v })} w={130} styles={amber("duration_hours")} />
                <TextInput label="notes" value={d.notes} onChange={(e) => setD({ ...d, notes: e.currentTarget.value })} styles={amber("notes")} style={{ flex: "1 1 240px" }} />
              </Group>
            </Section>
            <Text size="xs" c="dimmed">A run's week is set on each schedule below, and the minimums under Group make-up.</Text>
            <Group gap="sm" justify="flex-end">
              {r.overridden.length > 0 && <Button variant="subtle" color="red" size="sm" onClick={reset} disabled={busy} mr="auto">Reset to defaults</Button>}
              <Button variant="default" size="sm" onClick={() => { setD(toDraft(r)); setMode(null); }}>Cancel</Button>
              <Button size="sm" loading={busy} onClick={save}>Save rules</Button>
            </Group>
          </Stack>
        )}
      </Box>
      <Schedules r={r} meta={meta} editable={mode === null || mode === "schedule"} policies={policies} maxRosters={maxRosters} others={others} taken={taken} onSaved={onSaved} onEditing={childEditing("schedule")} />
      {confirmDialog}
      {review}
    </Card>
  );
}

export const SPLIT_LABEL: Record<string, string> = { balanced: "Balanced", first: "Raid one first", rotation: "Rotation" };
export const SPLIT_BLURB: Record<string, string> = { balanced: "both made equally strong", first: "raid one is made as strong as it can be; raid two takes the rest", rotation: "whoever joined last lockout but wasn't in raid one moves into raid one" };

function toDraft(r: RaidRule): Draft {
  return { split_policy: r.split_policy, weights: { ...r.weights }, first_open: r.first_open_local, lockout_days: r.lockout_days, duration_hours: r.duration_hours, notes: r.notes };
}

/** The draft keeps the API's machine form ('2026-12-09T15:00', guild time); the picker speaks 'YYYY-MM-DD HH:mm:ss'. */
function pickerValue(iso: string): string | null {
  return iso ? `${iso.slice(0, 16).replace("T", " ")}:00` : null;
}
function fromPicker(v: string | null, keep: string): string {
  return v ? v.slice(0, 16).replace(" ", "T") : keep;
}
