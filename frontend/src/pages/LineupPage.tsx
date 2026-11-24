import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Anchor, Box as MBox, Button, Group, Select, Stack, Text, Title } from "@mantine/core";
import { api, ApiError, type Box, type Fact, type Meta } from "../api";
import { fail, ok } from "../components/Page";
import { Facts, ViewSection } from "../components/Facts";
import { BoxBody, BoxButtons, useConfirmBox } from "../components/ConfirmBox";
import { guardNav } from "../components/LeaveGuard";
import { WhenPicker, wallTime } from "../components/WhenPicker";
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
 *  event's Edit lineup (/events/:key/lineup — Confirms.dc.html box 4). The schedule editor reuses the builder later. */
export function LineupPage({ mode }: { meta: Meta; mode: "new-event" | "edit-lineup" }) {
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
