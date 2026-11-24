import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMediaQuery } from "@mantine/hooks";
import { Anchor, Box as MBox, Button, Group, MultiSelect, SegmentedControl, Select, Stack, Switch, Text, Title } from "@mantine/core";
import { api, ApiError, type Box, type Fact, type Meta, type ScheduleEditorOptions, type ScheduleList, type ScheduleView } from "../api";
import { fail, ok } from "../components/Page";
import { Facts, ViewSection } from "../components/Facts";
import { BoxBody, BoxButtons, useConfirmBox } from "../components/ConfirmBox";
import { guardNav } from "../components/LeaveGuard";
import { QUARTER_HOURS, WhenPicker, wallTime } from "../components/WhenPicker";
import { SlotEditor } from "../components/SlotEditor";
import { useOpsSave } from "../components/ReviewModal";
import { SPLIT_LABEL } from "../components/board/shared";
import { NameFields } from "../components/schedules/NameFields";
import { LineupBuilder, type LineupEntry, type PaletteRaid } from "../components/lineup/LineupBuilder";
import { LineupPicture, type PictureStep } from "../components/lineup/LineupPicture";
import type { OpenPreview } from "../components/board/types";
import { clearDirty, useDirty } from "../hooks/useDirty";
import { useLive } from "../hooks/useLive";
import css from "./listpage.module.css";

/** GET /api/event-new (event_view.new_event_options): what the page picks from. `today` = the GUILD's date (the Day
 *  picker's lower bound); `lineup_max` = registry.LINEUP_MAX (the builder holds no copy). */
type Options = {
  today: string; raids: PaletteRaid[]; lineups: { id: string; name: string; lineup: LineupEntry[] }[];
  locks: { hours: number; words: string }[]; lineup_max: number; lineup_full: string;
};
/** POST /api/raid/{rid}/open/preview with the New event fields (event_view.new_event_view): "What this makes" in the
 *  server's words — facts, each step's start, notes, the refusal box (a night already open, a past time) and, for a
 *  schedule's night, the Open the sheet box. */
type Preview = OpenPreview & {
  facts: Fact[]; lineup: (PictureStep & { raid: string; starts: number })[]; notes: string[]; refusal_box: Box | null; box: Box | null;
};
type Opened = { message: string; key: string };

const boxOf = (e: unknown): Box | undefined => (e instanceof ApiError ? (e.body as { box?: Box } | undefined)?.box : undefined);
/** Where Back goes: the Events list. */
const EVENTS = "/events";

/** The lineup builder's page (design §7a): `mode` "new-event" = New event (NewEvent.dc.html), "edit-lineup" = an open
 *  event's Edit lineup (/events/:key/lineup — Confirms.dc.html box 4), "new-schedule" / "edit-schedule" = the schedule
 *  editor (/schedules/new, /schedules/:id/edit — Schedules.dc.html, boxes 13–14). */
export function LineupPage({ meta, mode }: { meta: Meta; mode: "new-event" | "edit-lineup" | "new-schedule" | "edit-schedule" }) {
  if (mode === "new-schedule" || mode === "edit-schedule") return <ScheduleEditor meta={meta} edit={mode === "edit-schedule"} />;
  if (mode === "edit-lineup") return <EditLineup />;
  return <NewEvent />;
}

/** The New event page: Start from (copy a schedule's lineup, or an empty one), the lineup builder, When (Day from a
 *  calendar, Starts from a select, Locks), and What this makes — the server's preview, asked again on every change
 *  (the latest answer wins). That section is the confirmation: Open the sheet opens a one-off at once; a time that is
 *  a schedule's night opens THAT schedule's event through the server's box (its lineup shown, the builder's not used).
 *  Nothing is written before Open the sheet; leaving with edits asks first (useDirty + the leave guard). */
function NewEvent() {
  const navigate = useNavigate();
  const [opts, setOpts] = useState<Options | null>(null);
  const [from, setFrom] = useState<string>("");
  const [lineup, setLineup] = useState<LineupEntry[]>([]);
  const [day, setDay] = useState<string | null>(null);
  const [time, setTime] = useState<string | null>(null);
  const [lock, setLock] = useState<string>("2");
  const [changes, setChanges] = useState(0);
  const [p, setP] = useState<Preview | null>(null);
  const [refused, setRefused] = useState<Box | null>(null);  // the preview's own refusal (a lineup the server won't open)
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  const [ask, dialog] = useConfirmBox(["run:", "config"]);

  useEffect(() => {
    api.get<Options>("/api/event-new").then((o) => {
      setOpts(o);
      const first = o.lineups[0];  // the first schedule's lineup, else an empty one
      if (first) { setFrom(first.id); setLineup(first.lineup.map((e) => ({ ...e }))); }
      setLock(String(o.locks[0]?.hours ?? 2));
    }).catch(fail);
  }, []);
  useLive(() => setTick((t) => t + 1), ["run:", "config"]);  // an event opened elsewhere, a schedule edited: ask again
  useDirty("lineup:new", changes > 0, { n: changes, name: "New event" });
  const edit = () => setChanges((n) => n + 1);

  const later = lineup.slice(1).map((e) => ({ raid: e.raid, rosters: e.rosters, starts: e.starts }));
  const timing = { lock_hours_before: Number(lock) };
  const body = lineup.length && day && time ? { when: wallTime(day, time), schedule: "", rosters: lineup[0].rosters, lineup: later, timing } : null;
  const asked = JSON.stringify([lineup[0]?.raid, body]);
  useEffect(() => {
    const n = ++seq.current;
    if (!body) { setP(null); setRefused(null); setChecking(false); return; }
    setChecking(true);
    api.post<Preview>(`/api/raid/${lineup[0].raid}/open/preview`, body)
      .then((x) => { if (n === seq.current) { setP(x); setRefused(null); } })
      .catch((e) => {
        if (n !== seq.current) return;
        setP(null);
        const b = boxOf(e);
        if (b) setRefused(b); else fail(e);
      })
      .finally(() => { if (n === seq.current) setChecking(false); });
  }, [asked, tick]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!opts) return <Text c="dimmed">Loading…</Text>;
  const scheduled = p?.kind === "scheduled";
  const refusal = refused || (p ? p.refusal_box : null);
  const ready = !!p && !refusal && !checking;
  // the server's step times are the builder's only when nothing is refused and the preview's raids ARE the builder's:
  // a taken time answers with the LIVE event's lineup (the refusal box names it), never times to borrow
  const own = !!p && !refusal && !checking && p.lineup.map((s) => s.raid).join() === lineup.map((e) => e.raid).join();

  const opened = (r: Opened) => {
    clearDirty();
    setChanges(0);
    ok(r.message);
    navigate(`/events/${r.key}`);
  };
  async function open() {
    if (!p || !ready) return;
    const url = `/api/raid/${lineup[0].raid}/open`;
    const sent = { start: p.start, schedule: p.kind === "scheduled" ? p.schedule : (p.schedule || "adhoc"), occurrence: p.occurrence };
    if (scheduled && p.box) {  // a schedule's night: the Open the sheet box, with `seen` (a box whose facts changed comes back fresh)
      let got: Opened | null = null;
      const done = await ask({ box: p.box, act: async (fp) => {
        try { got = await api.post<Opened>(url, { ...sent, seen: fp }); return null; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      } });
      if (done && got) opened(got);
      return;
    }
    setBusy(true);
    try {
      opened(await api.post<Opened>(url, { ...sent, rosters: lineup[0].rosters, lineup: later, timing }));
    } catch (e) {
      const b = boxOf(e);
      if (b) await ask({ box: b }); else fail(e);
      setTick((t) => t + 1);
    } finally { setBusy(false); }
  }

  const fromData = [...opts.lineups.map((l) => ({ value: l.id, label: l.name })), { value: "", label: "An empty lineup" }];
  return (
    <Stack gap={0} className={css.touch}>
      <Group justify="space-between" align="center" wrap="wrap" gap="xs">
        <Title order={1} size="h2">New event</Title>
        <Anchor component="button" type="button" onClick={() => guardNav(EVENTS)}>Back to Events</Anchor>
      </Group>
      <div className={css.view} style={{ marginTop: 18 }}>
        <ViewSection title="Start from">
          <Select label="Copy a lineup" w={260} maw="100%" allowDeselect={false} value={from} data={fromData} comboboxProps={{ withinPortal: true }}
            onChange={(v) => {
              const id = v ?? "";
              setFrom(id);
              setLineup((opts.lineups.find((l) => l.id === id)?.lineup || []).map((e) => ({ ...e })));
              edit();
            }} />
        </ViewSection>
        <ViewSection title="Lineup">
          {scheduled && p ? <LineupPicture lineup={p.lineup} /> : (
            <LineupBuilder raids={opts.raids} value={lineup} max={opts.lineup_max} fullWords={opts.lineup_full} startClock={time}
              times={own ? p.lineup.map((s) => s.time_words) : undefined}
              onChange={(next) => { setLineup(next); edit(); }} />
          )}
        </ViewSection>
        <ViewSection title="When">
          <WhenPicker label="When the event starts" day={day} time={time} minDate={opts.today}
            onDay={(d) => { setDay(d); edit(); }} onTime={(t) => { setTime(t); edit(); }}>
            {!scheduled && (
              <Select label="Locks" w={180} allowDeselect={false} value={lock} comboboxProps={{ withinPortal: true }}
                data={opts.locks.map((l) => ({ value: String(l.hours), label: l.words }))} onChange={(v) => { if (v) { setLock(v); edit(); } }} />
            )}
          </WhenPicker>
        </ViewSection>
        <ViewSection title="What this makes">
          <Stack gap="sm">
            {!lineup.length && <Text size="sm" c="dimmed">Add a raid to the lineup to see what this event makes.</Text>}
            {lineup.length > 0 && !body && <Text size="sm" c="dimmed">Pick the day and the start time to see what this event makes.</Text>}
            {refusal && (
              <MBox role="alert" p="sm" style={{ border: "1px solid var(--mantine-color-red-7)", borderRadius: 8 }}>
                <Text size="sm" fw={700}>{refusal.title}</Text>
                {refusal.sentences.map((s) => <Text key={s} size="sm">{s}</Text>)}
              </MBox>
            )}
            {p && !refusal && <Facts items={p.facts} />}
            {p && !refusal && p.notes.map((s) => <Text key={s} size="sm" c="dimmed">{s}</Text>)}
            <Group justify="flex-end" gap="sm" mt="xs">
              <Button variant="default" onClick={() => guardNav(EVENTS)}>Back</Button>
              <Button disabled={!ready} loading={busy} onClick={open}>Open the sheet</Button>
            </Group>
          </Stack>
        </ViewSection>
      </div>
      {dialog}
    </Stack>
  );
}

/** GET /api/event/{key}/lineup (event_view.lineup_edit_json): the event's own lineup and clock, the palette, and on a
 *  locked or finished event the server's refusal box. */
type EditStart = {
  key: string; title: string; start_time: string; lineup: LineupEntry[]; raids: PaletteRaid[]; lineup_max: number; lineup_full: string;
  refusal_box: Box | null;
};
/** POST /api/event/{key}/lineup (event_view.lineup_draft_json): a draft's "What this makes" — the facts, each step's
 *  start in the server's words, the Save box (consequences.lineup_box) and `same` (nothing to save). Nothing written. */
type Draft = { facts: Fact[]; lineup: (PictureStep & { raid: string; starts: number })[]; box: Box; same: boolean };

/** Edit lineup (design §7a, Confirms.dc.html box 4): the ONE builder with the first raid fixed, seeded from the event's
 *  own lineup and started at its clock — a kept raid keeps its start unless the edit moves it (removing a middle raid
 *  moves the next one earlier, shown in its Starts before the Save) —, and What this makes from the server on every
 *  change (the latest answer wins; the step times are the server's only while its raids and starts ARE the builder's).
 *  Save asks with the server's lineup box — the new start times, what a removed raid takes with it, that moves on a
 *  removed roster are dropped — and presses with the fp of the box on screen: a box whose facts changed comes back
 *  fresh (409) and the next press goes through. A locked or finished event shows the server's refusal and only Back.
 *  The draft is registered (useDirty, with its count and the event's title), so leaving asks first. */
function EditLineup() {
  const { key = "" } = useParams();
  const navigate = useNavigate();
  const [edit, setEdit] = useState<EditStart | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const [lineup, setLineup] = useState<LineupEntry[]>([]);
  const [changes, setChanges] = useState(0);
  const [x, setX] = useState<Draft | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  const changed = useRef(0);
  changed.current = changes;
  const [ask, dialog] = useConfirmBox(["run:"]);
  const evKey = edit?.key || key;
  const back = `/events/${encodeURIComponent(evKey)}`;
  const dirtyKey = `lineup:${evKey}`;
  const dirtyName = edit?.title || "";
  useDirty(dirtyKey, changes > 0 && !edit?.refusal_box, { n: changes, name: dirtyName });

  /** the event's start: its lineup seeds the builder while no edit is made (a live reload never resets a draft) */
  const load = () => api.get<EditStart>(`/api/event/${encodeURIComponent(key)}/lineup`).then((e) => {
    setEdit(e); setGone(null);
    if (!changed.current) setLineup(e.lineup.map((s) => ({ raid: s.raid, rosters: s.rosters, starts: s.starts })));
    setTick((n) => n + 1);
  }).catch((e) => {
    if (e instanceof ApiError && e.status === 404) setGone((e.body as { error?: string } | undefined)?.error || e.message);
    else fail(e);
  });
  useEffect(() => { load(); }, [key]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["run:"]);  // locked, ended or edited elsewhere meanwhile: the refusal or the new start, the draft kept

  const asked = JSON.stringify(lineup);
  useEffect(() => {
    const n = ++seq.current;
    if (!edit || edit.refusal_box || !lineup.length) { setX(null); setChecking(false); return; }
    setChecking(true);
    api.post<Draft>(`/api/event/${encodeURIComponent(edit.key)}/lineup`, { lineup })
      .then((d) => { if (n === seq.current) setX(d); })
      .catch((e) => { if (n === seq.current) { setX(null); fail(e); } })
      .finally(() => { if (n === seq.current) setChecking(false); });
  }, [asked, tick]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (gone) return <Text c="dimmed">{gone}</Text>;
  if (!edit) return <Text c="dimmed">Loading…</Text>;
  // the server's step times are the builder's only while the answer's raids and starts ARE the builder's (never times
  // borrowed from a lineup the officer has since changed)
  const own = !!x && !checking && x.lineup.map((s) => `${s.raid}:${s.starts}`).join() === lineup.map((e) => `${e.raid}:${e.starts}`).join();
  const refusal = x && x.box.verb === null && !x.same ? x.box : null;
  const ready = !!x && !checking && !x.same && x.box.verb !== null && changes > 0;

  async function save() {
    const url = `/api/run/${encodeURIComponent(evKey)}/lineup`;
    const sent = lineup.map((e) => ({ raid: e.raid, rosters: e.rosters, starts: e.starts }));
    /** the server's lineup box for this draft — asked again while it is open (someone joined, the event moved) */
    const preview = async (): Promise<Box> => {
      try { return (await api.post<{ box: Box }>(url, { lineup: sent, preview: true })).box; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
    };
    let first: { box: Box };
    setBusy(true);
    try { first = { box: await preview() }; } catch (e) { fail(e); return; } finally { setBusy(false); }
    const said = { text: "" };
    const done = await ask({ box: first.box, refresh: preview, act: async (fp) => {
      try { said.text = (await api.post<{ message: string }>(url, { lineup: sent, seen: fp })).message; return null; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
    } });
    if (done && said.text) {
      clearDirty();
      setChanges(0);
      ok(said.text);
      navigate(`/events/${evKey}`);
    }
  }

  return (
    <Stack gap={0} className={css.touch}>
      <Group justify="space-between" align="center" wrap="wrap" gap="xs">
        <div>
          <Title order={1} size="h2">Edit lineup</Title>
          <Text size="sm" c="dimmed">{edit.title}</Text>
        </div>
        <Anchor component="button" type="button" onClick={() => guardNav(back)}>Back to the event</Anchor>
      </Group>
      <div className={css.view} style={{ marginTop: 18 }}>
        {edit.refusal_box ? (
          <ViewSection title={edit.refusal_box.title}>
            <Stack gap="md">
              <BoxBody box={edit.refusal_box} />
              <BoxButtons box={edit.refusal_box} onBack={() => guardNav(back)} onGo={() => undefined} />
            </Stack>
          </ViewSection>
        ) : (
          <>
            <ViewSection title="Lineup">
              <LineupBuilder raids={edit.raids} value={lineup} max={edit.lineup_max} fullWords={edit.lineup_full} startClock={edit.start_time} fixedFirst
                times={own ? x!.lineup.map((s) => s.time_words) : undefined}
                onChange={(next) => { setLineup(next); setChanges((n) => n + 1); }} />
            </ViewSection>
            <ViewSection title="What this makes">
              <Stack gap="sm">
                {refusal && (
                  <MBox role="alert" p="sm" style={{ border: "1px solid var(--mantine-color-red-7)", borderRadius: 8 }}>
                    <Text size="sm" fw={700}>{refusal.title}</Text>
                    {refusal.sentences.map((s) => <Text key={s} size="sm">{s}</Text>)}
                  </MBox>
                )}
                {x && !refusal && <Facts items={x.facts} />}
                <Group justify="flex-end" gap="sm" mt="xs">
                  <Button variant="default" onClick={() => guardNav(back)}>Back</Button>
                  <Button disabled={!ready} loading={busy} onClick={save}>Save</Button>
                </Group>
              </Stack>
            </ViewSection>
          </>
        )}
      </div>
      {dialog}
    </Stack>
  );
}

/** ---- The schedule editor (phase 4, Task 11 — Schedules.dc.html, Confirms.dc.html boxes 13–14): the builder's third use. */
type Opts = ScheduleEditorOptions;
/** A schedule's draft: what its fields hold now. Hours are numbers, switches booleans; `split` "" = the raid's split,
 *  a weight "" = the raid's weight. `id` = a NEW schedule's id (from its name until the officer types one). */
type SchedDraft = {
  name: string; id: string; idTyped: boolean; kind: string; slots: string[]; days: string[]; time: string;
  lineup: LineupEntry[]; timing: Record<string, number | boolean>; split: string; weights: Record<string, string>;
};
const TIMING_HOURS = ["signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours"] as const;
const TIMING_SWITCHES = ["nudge", "autofill", "open_dm", "confirm"] as const;
/** a pickup template opens when an officer opens it and reminds nobody: these mean nothing there */
const PICKUP_HIDDEN = new Set<string>(["signup_lead_hours", "nudge_hours_before", "nudge", "open_dm"]);
const KINDS = [{ value: "weekly", label: "Weekly" }, { value: "lockout", label: "Day N of each lockout" }, { value: "pickup", label: "Opened by an officer" }];
const SCHEDULES = "/schedules";
const SAME = "same";  // a select's "Use the raid's": the draft holds "" (nothing of the schedule's own)

/** A new schedule's id from its name: unique across the guild (`taken`), never one of the server's reserved ids. */
function slug(name: string, taken: string[], reserved: string[]): string {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 12) || "schedule";
  let id = base, n = 2;
  while (taken.includes(id) || reserved.includes(id)) id = `${base}-${n++}`;
  return id;
}

/** Where step `i` starts when nobody picked its start: when the step before it ends (the builder's chain, C12). */
function chained(list: LineupEntry[], opts: Opts, i: number): number {
  const prev = list[i - 1];
  return prev.starts + (Math.round((opts.raids.find((r) => r.id === prev.raid)?.hours || 0) * 60) || 60);
}

/** The draft's timing as the server's suggestion for its first raid (a pickup: the pickup's). */
const suggested = (opts: Opts, kind: string, raid?: string): Record<string, number | boolean> =>
  ({ ...(kind === "pickup" ? opts.adhoc.timing : opts.raids.find((r) => r.id === raid)?.timing || {}) });

function seedOf(opts: Opts, view: ScheduleView | null, from: string): SchedDraft {
  if (view) {
    const st = view.stored as Record<string, unknown> & { weights?: Record<string, number> | null };
    const weights = Object.fromEntries(Object.keys(PRIORITY_KEYS).map((k) => [k, st.weights?.[k] != null ? String(st.weights[k]) : ""]));
    return {
      name: String(st.name || view.name), id: view.id, idTyped: true, kind: String(st.kind || "weekly"), slots: [...((st.slots as string[]) || [])],
      days: ((st.days as number[]) || []).map(String), time: String(st.time || "20:00"),
      lineup: view.lineup.map((s) => ({ raid: s.raid, rosters: s.rosters, starts: s.starts })),
      timing: Object.fromEntries([...TIMING_HOURS, ...TIMING_SWITCHES].filter((k) => st[k] !== undefined && st[k] !== null).map((k) => [k, st[k] as number | boolean])),
      split: String(st.split_policy || ""), weights,
    };
  }
  const lineup = (opts.lineups.find((l) => l.id === from)?.lineup || []).map((e) => ({ ...e }));
  return { name: "", id: "", idTyped: false, kind: "weekly", slots: [], days: [], time: "20:00", lineup, timing: suggested(opts, "weekly", lineup[0]?.raid),
    split: "", weights: Object.fromEntries(Object.keys(PRIORITY_KEYS).map((k) => [k, ""])) };
}
/** the priority's five weights in the Priority view's order (constants.PAGE_WORDS["priority"]'s keys) */
const PRIORITY_KEYS = { regular: 0, rank: 0, main: 0, sat_out: 0, signup_order: 0 };

/** New schedule (`sid` undefined) and a schedule's Edit (`/schedules/<id>/edit`): Start from (new only: Copy a lineup),
 *  Details (Name; a new one's Id), Lineup (the ONE builder — its first raid fixed on an edit, as the server keeps it),
 *  Repeats (Weekly run times · Day N of each lockout + its start · opened by an officer; a kind is chosen once, when
 *  the schedule is made), Sheet timing (the four hours and a fill ask's wait from the server's choices, the four
 *  switches, a preset), Priority (the five weights 0 to the server's highest, the split; Use the raid's clears them).
 *  One Save through useOpsSave's TOKEN path: the server's box first (create 13 / save 14 — "keeps its timing" when an
 *  open event keeps it), the press applies that preview's token, never `confirmed`. An edit sends only what differs
 *  from the schedule as loaded (an untouched editor saves nothing) with `config:schedule:<id>`: a stale one is a 409,
 *  the draft stays and the server's sentence says so, with Reload (through the leave guard). A new one is
 *  create-shaped (its kind and lineup); an id another schedule holds answers the server's ID_TAKEN under the Save,
 *  the draft kept and the Id field there to change. Leaving with edits asks first (useDirty + the leave guard). */
function ScheduleEditor({ meta, edit }: { meta: Meta; edit: boolean }) {
  const navigate = useNavigate();
  const { id: param } = useParams();
  const sid = edit ? param : undefined;
  const phone = useMediaQuery("(max-width: 48em)");
  const [list, setList] = useState<ScheduleList | null>(null);
  const [view, setView] = useState<ScheduleView | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const [from, setFrom] = useState<string>("");
  const [d, setD] = useState<SchedDraft | null>(null);
  const seed = useRef<SchedDraft | null>(null);
  const [made, setMade] = useState(0);  // a new schedule's edits (an edit counts what differs from the schedule instead)
  const [timingTyped, setTimingTyped] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [save, review] = useOpsSave();

  useEffect(() => {
    Promise.all([api.get<ScheduleList>("/api/schedules"), sid ? api.get<ScheduleView>(`/api/schedules/${encodeURIComponent(sid)}`) : Promise.resolve(null)])
      .then(([l, v]) => {
        setList(l); setView(v);
        const first = v ? "" : l.editor.lineups[0]?.id || "";
        setFrom(first);
        const s = seedOf(l.editor, v, first);
        seed.current = s; setD(s);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 404) setGone((e.body as { error?: string } | undefined)?.error || e.message);
        else fail(e);
      });
  }, [sid]);

  const opts = list?.editor;
  const taken = list ? [...list.active, ...list.paused].map((s) => s.id) : [];
  const id = sid ?? (d ? (d.idTyped ? d.id : slug(d.name, taken, list?.reserved_ids || [])) : "");
  const fields = d && opts ? fieldsOf(d, opts) : {};
  const n = sid ? Object.entries(fields).reduce((a, [k, v]) => a + (k === "weights" ? Object.keys(v as object).length : 1), 0) : made;
  const name = sid ? view?.name || "" : d?.name.trim() || "New schedule";
  useDirty(`schedule:${sid ?? "new"}`, n > 0, { n, name });
  const back = sid ? `${SCHEDULES}/${encodeURIComponent(sid)}` : SCHEDULES;

  /** The draft as the `schedule` change's fields. An edit: what differs from the schedule as loaded (the weights per
   *  key). A new one: everything it is made with, its kind first. A later step's start is sent only where it isn't
   *  "when the step before ends" or the schedule already held one. */
  function fieldsOf(d: SchedDraft, o: Opts): Record<string, unknown> {
    const base = seed.current!;
    const held = new Set(sid ? ((view?.stored.lineup || []).filter((e) => e.starts != null).map((e) => e.raid)) : []);
    const lineupOut = d.lineup.map((e, i) => ({ raid: e.raid, rosters: e.rosters,
      ...(i > 0 && (held.has(e.raid) || e.starts !== chained(d.lineup, o, i)) ? { starts: e.starts } : {}) }));
    const shown = (k: string) => !(d.kind === "pickup" && PICKUP_HIDDEN.has(k));
    const fields: Record<string, unknown> = {};
    if (!sid) {
      Object.assign(fields, { kind: d.kind, ...(d.name.trim() ? { name: d.name.trim() } : {}) });
      if (d.kind === "weekly") fields.slots = d.slots;
      if (d.kind === "lockout") Object.assign(fields, { days: d.days.map(Number), time: d.time });
      fields.lineup = lineupOut;
      for (const k of [...TIMING_HOURS, ...TIMING_SWITCHES]) if (shown(k) && d.timing[k] !== undefined) fields[k] = d.timing[k];
      if (d.split) fields.split_policy = d.split;
      const w = Object.fromEntries(Object.entries(d.weights).filter(([, v]) => v !== "").map(([k, v]) => [k, Number(v)]));
      if (Object.keys(w).length) fields.weights = w;
      return fields;
    }
    if (d.name.trim() !== base.name) fields.name = d.name.trim() || null;
    if (d.kind === "weekly" && JSON.stringify(d.slots) !== JSON.stringify(base.slots)) fields.slots = d.slots;
    if (d.kind === "lockout" && JSON.stringify(d.days) !== JSON.stringify(base.days)) fields.days = d.days.map(Number);
    if (d.kind === "lockout" && d.time !== base.time) fields.time = d.time;
    if (JSON.stringify(d.lineup) !== JSON.stringify(base.lineup)) fields.lineup = lineupOut;
    for (const k of [...TIMING_HOURS, ...TIMING_SWITCHES]) if (shown(k) && d.timing[k] !== base.timing[k]) fields[k] = d.timing[k];
    if (d.split !== base.split) fields.split_policy = d.split || null;
    const w = Object.fromEntries(Object.entries(d.weights).filter(([k, v]) => v !== base.weights[k]).map(([k, v]) => [k, v === "" ? null : Number(v)]));
    if (Object.keys(w).length) fields.weights = w;
    return fields;
  }

  if (gone) return <Text c="dimmed">{gone}</Text>;
  if (!list || !opts || !d) return <Text c="dimmed">Loading…</Text>;
  if (view?.gone) return <Text c="dimmed">{view.gone}</Text>;

  const set = (patch: Partial<SchedDraft>) => { setD({ ...d, ...patch }); setMade((k) => k + 1); setRefusal(null); };
  const setTiming = (k: string, v: number | boolean) => { setTimingTyped(true); set({ timing: { ...d.timing, [k]: v } }); };
  const firstRaid = opts.raids.find((r) => r.id === d.lineup[0]?.raid);
  const startClock = d.kind === "weekly" ? (d.slots[0] ? d.slots[0].split(" ")[1] : null) : d.kind === "lockout" ? d.time : null;
  const pickup = d.kind === "pickup";
  const shown = (k: string) => !(pickup && PICKUP_HIDDEN.has(k));
  const hourSelect = (k: string, label: string) => (
    <Select key={k} label={label} w={200} maw="100%" allowDeselect={false} comboboxProps={{ withinPortal: true }} placeholder="Pick one"
      value={d.timing[k] !== undefined ? String(d.timing[k]) : null} data={(opts.hours[k] || []).map((c) => ({ value: String(c.value), label: c.words }))}
      onChange={(v) => v && setTiming(k, Number(v))} />
  );
  const toggle = (k: string, label: string) => (
    <Switch key={k} label={label} checked={d.timing[k] === true} onChange={(e) => setTiming(k, e.currentTarget.checked)} />
  );
  const preset = opts.presets.find((p) => Object.entries(p.values).every(([k, v]) => Number(d.timing[k]) === v));
  const raidWeights = pickup ? opts.adhoc.weights : firstRaid?.weights || {};
  const weightData = (k: string) => [{ value: SAME, label: `Use the raid's (${raidWeights[k] ?? 0})` },
    ...Array.from({ length: opts.max_weight + 1 }, (_, i) => ({ value: String(i), label: String(i) }))];

  async function saveSchedule() {
    if (!Object.keys(fields).length) return;
    setBusy(true); setRefusal(null);
    try {
      const x = await save([{ kind: "schedule", target: id, fields }], {
        always: true, verb: sid ? "Save" : "Create schedule", title: sid ? `Save ${name}?` : `Create ${name}?`,
        revs: sid && view ? { [`config:schedule:${sid}`]: view.rev } : undefined,
      });
      if (x?.changed?.length) setStale(x.error || "");  // someone changed it meanwhile: the draft stays, the server's words say so
      else if (x?.applied?.length) { clearDirty(); setMade(0); navigate(`${SCHEDULES}/${encodeURIComponent(id)}`); }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setStale(e.message);
      else if (e instanceof ApiError && e.status === 400) setRefusal(e.message);  // the server's refusal (an id another schedule holds)
      else fail(e);
    } finally { setBusy(false); }
  }

  const fromData = [...opts.lineups.map((l) => ({ value: l.id, label: l.name })), { value: "", label: "An empty lineup" }];
  const days = Array.from({ length: firstRaid?.lockout_days || 7 }, (_, i) => ({ value: String(i + 1), label: `Day ${i + 1}` }));
  return (
    <Stack gap={0} className={css.touch}>
      <Group justify="space-between" align="center" wrap="wrap" gap="xs">
        <div>
          <Title order={1} size="h2">{sid ? "Edit schedule" : "New schedule"}</Title>
          {sid && <Text size="sm" c="dimmed">{view?.name}</Text>}
        </div>
        <Anchor component="button" type="button" onClick={() => guardNav(back)}>{sid ? "Back to the schedule" : "Back to Schedules"}</Anchor>
      </Group>
      <div className={css.view} style={{ marginTop: 18 }}>
        {!sid && (
          <ViewSection title="Start from">
            <Select label="Copy a lineup" w={260} maw="100%" allowDeselect={false} value={from} data={fromData} comboboxProps={{ withinPortal: true }}
              onChange={(v) => {
                const lid = v ?? "";
                setFrom(lid);
                const lineup = (opts.lineups.find((l) => l.id === lid)?.lineup || []).map((e) => ({ ...e }));
                set({ lineup, ...(timingTyped ? {} : { timing: suggested(opts, d.kind, lineup[0]?.raid) }) });
              }} />
          </ViewSection>
        )}
        <ViewSection title="Details">
          <NameFields name={d.name} id={sid ? null : id} onName={(name) => set({ name })} onId={(v) => set({ id: v, idTyped: true })} />
        </ViewSection>
        <ViewSection title="Lineup">
          <LineupBuilder raids={opts.raids} value={d.lineup} max={opts.lineup_max} fullWords={opts.lineup_full} startClock={startClock} fixedFirst={!!sid}
            onChange={(next) => set({ lineup: next, ...(sid || timingTyped || next[0]?.raid === d.lineup[0]?.raid ? {} : { timing: suggested(opts, d.kind, next[0]?.raid) }) })} />
        </ViewSection>
        <ViewSection title="Repeats">
          <Stack gap="sm">
            {sid ? <Text size="sm" fw={600}>{KINDS.find((k) => k.value === d.kind)?.label}</Text> : (
              <SegmentedControl aria-label="Repeats" value={d.kind} data={KINDS} orientation={phone ? "vertical" : "horizontal"} fullWidth={!!phone}
                onChange={(v) => set({ kind: v, ...(timingTyped ? {} : { timing: suggested(opts, v, d.lineup[0]?.raid) }) })} />
            )}
            {d.kind === "weekly" && <SlotEditor label="Run times" value={d.slots} onChange={(slots) => set({ slots })} />}
            {d.kind === "lockout" && (
              <Group gap="md" align="flex-end" wrap="wrap">
                <MultiSelect label="Days of each lockout" w={260} maw="100%" data={days} value={d.days} comboboxProps={{ withinPortal: true }}
                  onChange={(v) => set({ days: [...v].sort((a, b) => Number(a) - Number(b)) })} />
                <Select label="Starts" w={140} allowDeselect={false} value={d.time} data={QUARTER_HOURS} maxDropdownHeight={260} comboboxProps={{ withinPortal: true }}
                  onChange={(v) => v && set({ time: v })} />
              </Group>
            )}
            {pickup && <Text size="sm" c="dimmed">{meta.page_words.by_an_officer}</Text>}
          </Stack>
        </ViewSection>
        <ViewSection title="Sheet timing">
          <Stack gap="md">
            {!pickup && (
              <Select label="Start from a preset" w={260} maw="100%" placeholder="Pick a preset" value={preset?.name ?? null} comboboxProps={{ withinPortal: true }}
                data={opts.presets.map((p) => p.name)} onChange={(v) => {
                  const p = opts.presets.find((x) => x.name === v);
                  if (p) { setTimingTyped(true); set({ timing: { ...d.timing, ...p.values } }); }
                }} />
            )}
            <Group gap="md" align="flex-end" wrap="wrap">
              {shown("signup_lead_hours") && hourSelect("signup_lead_hours", "Sheet opens")}
              {hourSelect("lock_hours_before", "Locks")}
            </Group>
            {shown("nudge") && (
              <Group gap="md" align="flex-end" wrap="wrap">
                {toggle("nudge", "Reminder")}
                {d.timing.nudge === true && hourSelect("nudge_hours_before", "Reminder")}
              </Group>
            )}
            <Group gap="md" align="flex-end" wrap="wrap">
              {toggle("confirm", "Ask the rostered to confirm")}
              {d.timing.confirm === true && hourSelect("confirm_hours_before", "Confirm by")}
            </Group>
            <Group gap="md" align="flex-end" wrap="wrap">
              {toggle("autofill", "The bot asks people for open seats")}
              {hourSelect("fill_ask_hours", "An ask waits")}
            </Group>
            {shown("open_dm") && toggle("open_dm", "DM the regulars when the sheet opens")}
          </Stack>
        </ViewSection>
        <ViewSection title="Priority" action={<Button variant="subtle" size="sm" onClick={() => set({ weights: Object.fromEntries(Object.keys(d.weights).map((k) => [k, ""])), split: "" })}>Use the raid's</Button>}>
          <Group gap="md" align="flex-end" wrap="wrap">
            {(Object.keys(PRIORITY_KEYS) as (keyof typeof PRIORITY_KEYS)[]).map((k) => (
              <Select key={k} label={meta.page_words.priority[k]} w={190} maw="100%" allowDeselect={false} comboboxProps={{ withinPortal: true }}
                value={d.weights[k] || SAME} data={weightData(k)} onChange={(v) => set({ weights: { ...d.weights, [k]: !v || v === SAME ? "" : v } })} />
            ))}
            <Select label="When one night fills two rosters" w={240} maw="100%" allowDeselect={false} comboboxProps={{ withinPortal: true }} value={d.split || SAME}
              data={[{ value: SAME, label: "Use the raid's" }, ...opts.split_policies.map((p) => ({ value: p, label: SPLIT_LABEL[p] || p }))]}
              onChange={(v) => set({ split: !v || v === SAME ? "" : v })} />
          </Group>
        </ViewSection>
        <div className={css.sect}>
          <Stack gap="sm">
            {stale && (
              <MBox role="alert" p="sm" style={{ border: "1px solid var(--mantine-color-yellow-7)", borderRadius: 8 }}>
                <Group justify="space-between" wrap="wrap" gap="sm">
                  <Text size="sm" style={{ flex: "1 1 240px" }}>{stale}</Text>
                  <Button variant="default" onClick={() => guardNav(`${SCHEDULES}/${encodeURIComponent(sid || id)}/edit`)}>Reload</Button>
                </Group>
              </MBox>
            )}
            {refusal && (
              <MBox role="alert" p="sm" style={{ border: "1px solid var(--mantine-color-red-7)", borderRadius: 8 }}>
                <Text size="sm">{refusal}</Text>
              </MBox>
            )}
            {!d.lineup.length && <Text size="sm" c="dimmed">Add a raid to the lineup to make the schedule.</Text>}
            <Group justify="flex-end" gap="sm">
              <Button variant="default" onClick={() => guardNav(back)}>Back</Button>
              <Button loading={busy} disabled={!Object.keys(fields).length || !d.lineup.length || !id} onClick={saveSchedule}>{sid ? "Save" : "Create schedule"}</Button>
            </Group>
          </Stack>
        </div>
      </div>
      {review}
    </Stack>
  );
}
