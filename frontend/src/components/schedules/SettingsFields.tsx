import { Group, Input, SegmentedControl, Select, Stack, Switch } from "@mantine/core";
import type { PageWords } from "../../api";

/** One setting's choices from the server ({value, words}: "5 days before", an ask's wait "2 hours"). */
export type HourChoice = { value: number; words: string };
/** The sheet timing a draft holds: hours as numbers, switches as booleans (the setting names are the server's). */
export type TimingDraft = Record<string, number | boolean>;

const SAME = "same";  // a select's "the raid's" choice: the draft holds "" (nothing of its own)

/** The Sheet timing pieces the schedule editor and Edit event share (one component, two editors): an optional preset,
 *  then — as `shown(setting)` allows — when the sheet opens and locks, the reminder switch and its time, Ask the rostered
 *  to confirm and Confirm by, the automatic asks and how long an ask waits, and the open DM. Every label is the
 *  server's word for the setting (PAGE_WORDS schedule_fields), so the Save box's diff lines say the same; every choice
 *  is the server's (`hours`). Pickers only: nothing is typed. */
export function TimingFields({ words, values, hours, shown, onChange, presets, onPreset }: {
  words: PageWords; values: TimingDraft; hours: Record<string, HourChoice[]>; shown: (setting: string) => boolean;
  onChange: (setting: string, value: number | boolean) => void;
  /** the schedule editor's presets: a pick hands its values over at once (one draft change, never one per setting) */
  presets?: { name: string; values: Record<string, number> }[]; onPreset?: (values: Record<string, number>) => void;
}) {
  const F = words.schedule_fields as Record<string, string>;
  const hourSelect = (k: string) => (
    <Select key={k} label={F[k]} w={200} maw="100%" allowDeselect={false} comboboxProps={{ withinPortal: true }} placeholder="Pick one"
      value={values[k] !== undefined ? String(values[k]) : null} data={(hours[k] || []).map((c) => ({ value: String(c.value), label: c.words }))}
      onChange={(v) => v && onChange(k, Number(v))} />
  );
  const toggle = (k: string) => (
    <Switch key={k} label={F[k]} checked={values[k] === true} onChange={(e) => onChange(k, e.currentTarget.checked)} />
  );
  const preset = presets?.find((p) => Object.entries(p.values).every(([k, v]) => Number(values[k]) === v));
  const row = (...items: (React.ReactNode | false)[]) => items.some(Boolean) && <Group gap="md" align="flex-end" wrap="wrap">{items}</Group>;
  return (
    <Stack gap="md">
      {presets && (
        <Select label="Start from a preset" w={260} maw="100%" placeholder="Pick a preset" value={preset?.name ?? null} comboboxProps={{ withinPortal: true }}
          data={presets.map((p) => p.name)} onChange={(v) => {
            const p = presets.find((x) => x.name === v);
            if (p && onPreset) onPreset(p.values);
          }} />
      )}
      {row(shown("signup_lead_hours") && hourSelect("signup_lead_hours"), shown("lock_hours_before") && hourSelect("lock_hours_before"))}
      {row(shown("nudge") && toggle("nudge"), shown("nudge_hours_before") && values.nudge === true && hourSelect("nudge_hours_before"))}
      {row(shown("confirm") && toggle("confirm"), shown("confirm_hours_before") && values.confirm === true && hourSelect("confirm_hours_before"))}
      {row(shown("autofill") && toggle("autofill"), shown("fill_ask_hours") && hourSelect("fill_ask_hours"))}
      {shown("open_dm") && toggle("open_dm")}
    </Stack>
  );
}

/** the priority's five weights in the Priority view's order (constants.PAGE_WORDS["priority"]'s keys) */
export const PRIORITY_KEYS = ["regular", "rank", "main", "sat_out", "signup_order"] as const;

/** One split select: a raid's (Edit event: one per raid, named when there are several) or the schedule's. */
export type SplitPick = { key: string; label: string; value: string; same?: string; onChange: (v: string) => void };

/** The Priority pieces the schedule editor and Edit event share: (an event's) Who gets a seat — First to join or By
 *  priority —, the five weights from 0 to the server's highest (the schedule's own "" = the raid's, shown as `same(k)`),
 *  and the split select(s). Labels and value words are the server's (PAGE_WORDS priority / split / schedule_fields). */
export function PriorityFields({ words, weights, maxWeight, same, onWeight, splits, policies, firstCome }: {
  words: PageWords; weights: Record<string, string>; maxWeight: number; same?: (key: string) => string;
  onWeight: (key: string, value: string) => void; splits: SplitPick[]; policies: string[];
  firstCome?: { value: boolean; onChange: (v: boolean) => void };
}) {
  const F = words.schedule_fields;
  const data = (k: string) => [...(same ? [{ value: SAME, label: same(k) }] : []),
    ...Array.from({ length: maxWeight + 1 }, (_, i) => ({ value: String(i), label: String(i) }))];
  return (
    <Stack gap="md">
      {firstCome && (
        <Input.Wrapper label={F.first_come} maw={420}>
          <SegmentedControl aria-label={F.first_come} value={firstCome.value ? "first" : "priority"} fullWidth
            data={[{ value: "first", label: words.first_to_join }, { value: "priority", label: words.by_priority }]}
            onChange={(v) => firstCome.onChange(v === "first")} />
        </Input.Wrapper>
      )}
      <Group gap="md" align="flex-end" wrap="wrap">
        {PRIORITY_KEYS.map((k) => (
          <Select key={k} label={words.priority[k]} w={190} maw="100%" allowDeselect={false} comboboxProps={{ withinPortal: true }}
            value={weights[k] || (same ? SAME : null)} data={data(k)} onChange={(v) => onWeight(k, !v || v === SAME ? "" : v)} />
        ))}
        {splits.map((s) => (
          <Select key={s.key} label={s.label} w={240} maw="100%" allowDeselect={false} comboboxProps={{ withinPortal: true }} value={s.value || SAME}
            data={[...(s.same ? [{ value: SAME, label: s.same }] : []), ...policies.map((p) => ({ value: p, label: words.split[p] || p }))]}
            onChange={(v) => s.onChange(!v || v === SAME ? "" : v)} />
        ))}
      </Group>
    </Stack>
  );
}
