import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Anchor, Button, Group, Stack, Text, Title } from "@mantine/core";
import { api, ApiError, type Box, type EventView, type Meta } from "../../api";
import { fail, ok } from "../Page";
import { ViewSection } from "../Facts";
import { useConfirmBox } from "../ConfirmBox";
import { guardNav } from "../LeaveGuard";
import { PRIORITY_KEYS, PriorityFields, TimingFields, type TimingDraft } from "../schedules/SettingsFields";
import { clearDirty, useDirty } from "../../hooks/useDirty";
import { boxOf } from "./shared";
import { ChannelFields } from "../ChannelFields";
import css from "../../pages/listpage.module.css";

/** What the editor holds: the sheet timing and switches by setting, the five weights as strings (a select's value),
 *  the seating, each raid's split by its key. */
type Draft = { timing: TimingDraft; weights: Record<string, string>; firstCome: boolean; splits: Record<string, string>;
  /** the event's own channels by place (null = the empty choice: its schedule's, else the guild's) */
  channels: Record<string, string | null> };

const TIMING = ["lock_hours_before", "auto_lock", "auto_lock_fill", "nudge", "nudge_hours_before", "confirm", "confirm_hours_before", "autofill", "fill_ask_hours"];

function seedOf(ev: EventView): Draft {
  const e = ev.edit!;
  const v = e.values;
  const w = (v.weights || {}) as Record<string, number>;
  return {
    timing: Object.fromEntries(TIMING.filter((k) => v[k] !== undefined && v[k] !== null).map((k) => [k, v[k] as number | boolean])),
    weights: Object.fromEntries(PRIORITY_KEYS.map((k) => [k, w[k] != null ? String(w[k]) : "0"])),
    firstCome: v.first_come === true,
    splits: Object.fromEntries(e.splits.map((s) => [s.key, s.value])),
    channels: Object.fromEntries(e.channels.fields.map((f) => [f.key, f.value])),
  };
}

/** The draft as reschedule's `timing`: only what differs from the seed and the editor may change now (the weights
 *  per key, the splits by raid key). Empty = nothing to save. */
function changesOf(d: Draft, seed: Draft, fields: string[]): Record<string, unknown> {
  const may = new Set(fields);
  const out: Record<string, unknown> = {};
  for (const k of TIMING) if (may.has(k) && d.timing[k] !== seed.timing[k]) out[k] = d.timing[k];
  if (may.has("first_come") && d.firstCome !== seed.firstCome) out.first_come = d.firstCome;
  if (may.has("weights")) {
    const w = Object.fromEntries(PRIORITY_KEYS.filter((k) => d.weights[k] !== seed.weights[k]).map((k) => [k, Number(d.weights[k])]));
    if (Object.keys(w).length) out.weights = w;
  }
  if (may.has("split_policy")) {
    const s = Object.fromEntries(Object.entries(d.splits).filter(([k, v]) => v !== seed.splits[k]));
    if (Object.keys(s).length) out.split_policy = s;
  }
  // the channels are not timing: they change until the event ends, after the lock and the start too ("" = the empty choice)
  for (const [k, v] of Object.entries(d.channels)) if (v !== seed.channels[k]) out[k] = v ?? "";
  return out;
}
/** how many edits the leave guard counts: each setting, each weight and each raid's split that differs from the seed */
const countOf = (c: Record<string, unknown>) => Object.entries(c).reduce((n, [k, v]) => n + (k === "weights" || k === "split_policy" ? Object.keys(v as object).length : 1), 0);

/** Edit event (the owner's tweaks, B — /events/<key>/edit): ONE editor for every setting the event's schedule sets for
 *  a night, kept on this event alone — the schedule editor's own Sheet timing and Priority pieces
 *  (components/schedules/SettingsFields), pickers only, labels from PAGE_WORDS schedule_fields. Before the lock every
 *  field; after it only what still means something (the server's `edit.fields`: automatic asks, an ask's wait) under the
 *  server's sentence. Save asks POST /api/run/{key}/reschedule `edit: true, preview: true` for the SERVER's box (the
 *  "label: old → new" lines, what changes, who is told, what can be undone) and presses with the box's fp as `seen`: an
 *  event changed meanwhile answers 409 with the fresh box, which waits again; a refusal is its box, Back alone. The
 *  draft is compared with the seed (useDirty: the leave guard asks before it is dropped). */
export function EditEvent({ evKey, meta }: { evKey: string; meta: Meta }) {
  const navigate = useNavigate();
  const [ev, setEv] = useState<EventView | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const [d, setD] = useState<Draft | null>(null);
  const seed = useRef<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [ask, dialog] = useConfirmBox(["run:", "member"]);

  useEffect(() => {
    api.get<EventView>(`/api/event/${encodeURIComponent(evKey)}`).then((v) => {
      setEv(v);
      if (v.edit) { const s = seedOf(v); seed.current = s; setD(s); }
    }).catch((e) => {
      if (e instanceof ApiError && e.status === 404) setGone((e.body as { error?: string } | undefined)?.error || e.message);
      else fail(e);
    });
  }, [evKey]);

  const W = meta.page_words, F = W.schedule_fields;
  const fields = ev?.edit?.fields || [];
  const changes = d && seed.current ? changesOf(d, seed.current, fields) : {};
  const n = countOf(changes);
  useDirty(`event:${evKey}`, n > 0, { n, name: ev?.title || "" });
  const back = `/events/${encodeURIComponent(ev?.key || evKey)}`;

  if (gone) return <Text c="dimmed">{gone}</Text>;
  if (!ev) return <Text c="dimmed">Loading…</Text>;
  const e = ev.edit;
  const may = (k: string) => fields.includes(k);
  const url = `/api/run/${encodeURIComponent(ev.key)}/reschedule`;

  async function save() {
    if (!Object.keys(changes).length) return;
    const body = { edit: true, timing: changes };
    const preview = async (): Promise<Box> => {
      try { return (await api.post<{ box: Box }>(url, { ...body, preview: true })).box; } catch (x) { const b = boxOf(x); if (b) return b; throw x; }
    };
    setBusy(true);
    let first: Box;
    try { first = await preview(); } catch (x) { fail(x); setBusy(false); return; } finally { setBusy(false); }
    let said = "";
    const done = await ask({
      box: first, refresh: preview,
      act: async (fp) => {  // the fp of the box on screen: the fresh one after a 409, so the next press goes through
        try { said = (await api.post<{ message: string }>(url, { ...body, seen: fp })).message; return null; }
        catch (x) { const b = boxOf(x); if (b) return b; throw x; }
      },
    });
    if (done) { ok(said); clearDirty(); navigate(back); }
  }

  const set = (patch: Partial<Draft>) => setD((x) => (x ? { ...x, ...patch } : x));
  return (
    <Stack gap={0} className={css.touch}>
      <Group justify="space-between" align="center" wrap="wrap" gap="xs">
        <div>
          <Title order={1} size="h2">Edit event</Title>
          <Text size="sm" c="dimmed">{ev.title}</Text>
        </div>
        <Anchor component="button" type="button" onClick={() => guardNav(back)}>Back to the event</Anchor>
      </Group>
      <div className={css.view} style={{ marginTop: 18 }}>
        {!e || !d ? (
          <div className={css.sect}><Text size="sm" c="dimmed">{W.edit_over}</Text></div>
        ) : (
          <>
            {e.note ? <div className={css.sect}><Text size="sm">{e.note}</Text></div>
              : ev.state !== "open" && <div className={css.sect}><Text size="sm">{W.edit_locked}</Text></div>}
            {fields.length > 0 && (
              <ViewSection title="Sheet timing">
                {/* the schedule editor's own component: the same labels, choices and order */}
                <TimingFields words={W} values={d.timing} hours={e.hours} shown={may}
                  onChange={(k, v) => set({ timing: { ...d.timing, [k]: v } })} />
              </ViewSection>
            )}
            <div className={css.sect}>
              <ChannelFields section={e.channels} values={d.channels} about={W.help_about} gone={W.channel_gone}
                onChange={(k, v) => set({ channels: { ...d.channels, [k]: v } })} />
            </div>
            {may("weights") && (
              <ViewSection title={F.weights}>
                <PriorityFields words={W} weights={d.weights} maxWeight={e.max_weight} policies={e.split_policies}
                  firstCome={may("first_come") ? { value: d.firstCome, onChange: (v) => set({ firstCome: v }) } : undefined}
                  onWeight={(k, v) => set({ weights: { ...d.weights, [k]: v } })}
                  splits={may("split_policy") ? e.splits.map((s) => ({
                    key: s.key, label: e.splits.length > 1 ? `${s.raid} · ${F.split_policy}` : F.split_policy, value: d.splits[s.key],
                    onChange: (v: string) => set({ splits: { ...d.splits, [s.key]: v } }),
                  })) : []} />
              </ViewSection>
            )}
            <div className={css.sect}>
              <Group justify="flex-end" gap="sm">
                <Button variant="default" onClick={() => guardNav(back)}>Back</Button>
                <Button loading={busy} disabled={n === 0} onClick={save}>Save</Button>
              </Group>
            </div>
          </>
        )}
      </div>
      {dialog}
    </Stack>
  );
}
