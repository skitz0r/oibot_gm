import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Alert, Button, Group, Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { api, ApiError, type Box, type EventList, type EventView, type Meta } from "../api";
import { ListPage, type ListGroup } from "../components/ListPage";
import { fail, ok } from "../components/Page";
import { useAnyBoxOpen, useConfirmBox } from "../components/ConfirmBox";
import { EventDetails } from "../components/events/EventDetails";
import { SignupsSection } from "../components/events/SignupsSection";
import { ActivitySection } from "../components/events/ActivitySection";
import { ComingUpView } from "../components/events/ComingUpView";
import { HistoryView } from "../components/events/HistoryView";
import { RoleSwitches } from "../components/events/RoleSwitches";
import { EventBoard } from "../components/events/EventBoard";
import { useAfterDrag } from "../components/board/shared";
import { useRescheduleBox } from "../components/events/RescheduleBox";
import { boxOf, EventHead } from "../components/events/shared";
import { EventMore } from "../components/events/EventMore";
import { guardNav } from "../components/LeaveGuard";
import type { NightSheet } from "../components/board/types";
import { useLive } from "../hooks/useLive";
import { usePoll } from "../hooks/usePoll";
import css from "./listpage.module.css";

const PAGE = 20;  // history rows per page (event_view.HISTORY_PAGE)
const comingId = (sid: string, occ: string) => `coming:${sid}:${occ}`;
const comingTo = (sid: string, occ: string) => `/events/coming/${encodeURIComponent(sid)}/${encodeURIComponent(occ)}`;

/** The Events page (design §7a, Main.dc.html): the list — Open · Coming up · History (Show older pages history) — beside
 *  one event in sections. `/events` = the first open event on a desktop (else the first coming-up night), the list alone
 *  on a phone; `/events/<key>` one event; `/events/coming/<schedule>/<night>` a schedule's night with no sheet yet.
 *  Every word and every box is the server's (/api/event-list, /api/event/…); the page lives on run: / member / config and
 *  a reload keeps the list's scroll, the selection and any box that is open. */
export function EventsPage({ meta }: { meta: Meta }) {
  const { key, sid, occurrence } = useParams();
  const phone = useMediaQuery("(max-width: 48em)");
  const [list, setList] = useState<EventList | null>(null);
  const [pages, setPages] = useState(1);
  const [tick, setTick] = useState(0);
  const loadList = async (n: number) => {
    const all = await Promise.all(Array.from({ length: n }, (_, i) => api.get<EventList>(`/api/event-list?history_offset=${i * PAGE}`)));
    setList({ ...all[0], history: all.flatMap((x) => x.history), history_more: all[all.length - 1].history_more });
  };
  const pagesRef = useRef(pages);
  pagesRef.current = pages;
  const load = () => { loadList(pagesRef.current).catch(fail); setTick((t) => t + 1); };
  useEffect(() => { loadList(1).catch(fail); }, []);
  useLive(load, ["run:", "member", "config"]);
  usePoll(load);
  if (!list) return <Text c="dimmed">Loading…</Text>;

  const groups: ListGroup[] = [
    { label: "Open", items: list.open.map((r) => ({ id: r.key, title: r.title, subtitle: r.subtitle, to: `/events/${encodeURIComponent(r.key)}` })) },
    { label: "Coming up", items: list.coming.map((c) => ({ id: comingId(c.schedule, c.occurrence), title: c.title, subtitle: c.subtitle, to: comingTo(c.schedule, c.occurrence) })) },
    { label: "History", items: list.history.map((r) => ({ id: r.key, title: r.title, subtitle: r.subtitle, to: `/events/${encodeURIComponent(r.key)}` })) },
  ];
  // the detail: the route's, else (desktop) the first open event, else the first coming-up night
  const pick = key ? { key } : sid && occurrence ? { sid, occurrence } : phone ? null
    : list.open[0] ? { key: list.open[0].key } : list.coming[0] ? { sid: list.coming[0].schedule, occurrence: list.coming[0].occurrence } : null;
  const selected = !pick ? null : "key" in pick ? pick.key! : comingId(pick.sid!, pick.occurrence!);
  const more = list.history_more ? (
    <Button variant="subtle" size="sm" mt={4} onClick={() => { const n = pages + 1; setPages(n); loadList(n).catch(fail); }}>Show older</Button>
  ) : null;
  return (
    <ListPage title="Events" selected={selected} back="/events" groups={groups} more={more} intro={list.tz_words}
      action={<Button component={Link} to="/events/new">New event</Button>}
      empty={<Text size="sm" c="dimmed">{meta.page_words.events_empty}</Text>}>
      {pick && "key" in pick && <EventPane key={pick.key} evKey={pick.key!} meta={meta} tick={tick} onChanged={load} />}
      {pick && "sid" in pick && <ComingUpView key={selected!} sid={pick.sid!} occurrence={pick.occurrence!} tick={tick} />}
    </ListPage>
  );
}

/** One event by its key: open or locked → its sections and actions; finished or cancelled → HistoryView. A key of a
 *  later raid answers as its event: the address becomes the event's own. A reload keeps what is drawn; the latest GET
 *  wins (a slow earlier answer never overwrites a newer one, even one held back by a drag). While a box is open the
 *  live view stays mounted with its last answer, so an event that ended underneath never takes the box away: the box's
 *  own refresh brings the server's refusal, and History follows once it is closed. */
function EventPane({ evKey, meta, tick, onChanged }: { evKey: string; meta: Meta; tick: number; onChanged: () => void }) {
  const navigate = useNavigate();
  const [ev, setEv] = useState<EventView | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const seq = useRef(0);
  const lastLive = useRef<EventView | null>(null);
  const held = useAnyBoxOpen();
  const afterDrag = useAfterDrag();  // one pending answer while a drag is on: the newest is drawn after the drop
  useEffect(() => {
    const n = ++seq.current;
    api.get<EventView>(`/api/event/${encodeURIComponent(evKey)}`).then((v) => afterDrag(() => {  // never redrawn under a drag
      if (n !== seq.current) return;  // a newer GET was sent meanwhile: its answer is the one drawn
      setEv(v); setGone(null);
      if (v.key !== evKey) navigate(`/events/${encodeURIComponent(v.key)}`, { replace: true });
    })).catch((e) => {
      if (n !== seq.current) return;
      if (e instanceof ApiError && e.status === 404) setGone((e.body as { error?: string } | undefined)?.error || e.message);
      else fail(e);
    });
  }, [evKey, tick]);  // eslint-disable-line react-hooks/exhaustive-deps
  const closed = !!ev && (ev.state === "done" || ev.state === "cancelled");
  if (ev && !closed) lastLive.current = ev;
  if (held && lastLive.current && (gone || closed)) return <LiveEvent ev={lastLive.current} meta={meta} onChanged={onChanged} />;
  if (gone) return <Text c="dimmed">{gone}</Text>;
  if (!ev) return <Text c="dimmed">Loading…</Text>;
  if (closed) return <HistoryView ev={ev} meta={meta} onChanged={onChanged} />;
  return <LiveEvent ev={ev} meta={meta} onChanged={onChanged} />;
}

/** An open or locked event (Main.dc.html): the header (Reschedule, Lock now), Details, Signups, then Activity with the
 *  quiet Cancel event at its right. Lock now and Cancel event print the server's box; the press sends the fp of the box
 *  on screen as `seen`, so a box whose facts changed comes back fresh (409) and waits again. */
function LiveEvent({ ev, meta, onChanged }: { ev: EventView; meta: Meta; onChanged: () => void }) {
  const [ask, dialog] = useConfirmBox(["run:", "member"]);
  const [reschedule, rescheduleDialog] = useRescheduleBox(ev, onChanged);
  const run = `/api/run/${encodeURIComponent(ev.key)}`;

  /** The box for `verb` asked again (its `preview`): the open box's live refresh. A refusal comes back as its box. */
  const fresh = (verb: "lock" | "cancel") => async (): Promise<Box> => {
    try { return (await api.post<{ box: Box }>(`${run}/${verb}`, { preview: true })).box; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
  };
  /** One write with the fp of the box on screen: done → the server's line; a stale or refused box → that box. */
  const press = (path: string, body: (fp: string) => object, said: { text: string }) => async (fp: string): Promise<Box | null> => {
    try { said.text = (await api.post<{ message: string }>(path, body(fp))).message; return null; }
    catch (e) { const b = boxOf(e); if (b) return b; throw e; }
  };

  async function lock() {
    if (!ev.boxes.lock) return;
    const said = { text: "" };
    if (await ask({ box: ev.boxes.lock, refresh: fresh("lock"), act: press(`/api/run/${encodeURIComponent(ev.key)}/lock`, (fp) => ({ seen: fp }), said) })) {
      ok(said.text); onChanged();
    }
  }
  async function cancel() {
    if (!ev.boxes.cancel) return;
    const said = { text: "" };
    if (await ask({ box: ev.boxes.cancel, refresh: fresh("cancel"), act: press(`${run}/cancel`, (fp) => ({ seen: fp, notify: false }), said) })) {
      ok(said.text); onChanged();
    }
  }

  return (
    <section className={css.view} aria-label={ev.title}>
      <EventHead title={ev.title} chip={ev.chip} open={ev.state === "open"} tag={ev.test_chip}>
        <Group gap="xs" wrap="wrap">
          {ev.actions.reschedule && ev.boxes.reschedule && <Button variant="default" onClick={reschedule}>Reschedule</Button>}
          {/* Edit lineup: the builder with the first raid fixed (LineupPage mode="edit-lineup"); gone once the roster locks */}
          {ev.actions.edit_lineup && <Button variant="default" onClick={() => guardNav(`/events/${encodeURIComponent(ev.key)}/lineup`)}>Edit lineup</Button>}
          {ev.actions.lock && ev.boxes.lock && <Button onClick={lock}>Lock now</Button>}
          <EventMore ev={ev} meta={meta} onChanged={onChanged} />
        </Group>
      </EventHead>
      {/* a scheduled lock that failed (the run stays open and retries): the server's sentence, per raid */}
      {ev.parts.map((p) => (p.sheet as unknown as NightSheet | null)?.lock_error ? (
        <div key={p.key} className={css.sect}><Alert color="red" variant="light" p="xs"><Text size="sm">{(p.sheet as unknown as NightSheet).lock_error}</Text></Alert></div>
      ) : null)}
      <EventDetails details={ev.details} />
      <SignupsSection ev={ev} meta={meta} onDone={onChanged} />
      <RoleSwitches meta={meta} view={ev} onReload={onChanged} />
      <EventBoard meta={meta} view={ev} onReload={onChanged} />
      <ActivitySection activity={ev.activity}
        action={ev.actions.cancel && ev.boxes.cancel ? <Button variant="subtle" color="gray" size="xs" onClick={cancel}>Cancel event</Button> : undefined} />
      {dialog}
      {rescheduleDialog}
    </section>
  );
}
