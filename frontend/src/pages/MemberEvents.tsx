import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Stack, Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { api, ApiError, type MemberEventList, type MemberEventView, type Meta } from "../api";
import { ListPage, type ListGroup } from "../components/ListPage";
import { fail } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { AskCard } from "../components/AskCard";
import { MyAnswer } from "../components/MyAnswer";
import { EventHead } from "../components/events/shared";
import { EventDetails } from "../components/events/EventDetails";
import { SignupTabs } from "../components/events/SignupTabs";
import { MemberRoster } from "../components/events/MemberRoster";
import { ViewSection } from "../components/Facts";
import { useLive } from "../hooks/useLive";
import { usePoll } from "../hooks/usePoll";
import css from "./listpage.module.css";

/** The members' Events page (design 2026-11-25 §2): what the Discord signup sheet shows everyone, plus the member's own
 *  answer. The list — Open (live events, each with the member's own answer) and Coming up (the next nights, plain text:
 *  no sheet yet, so no page) — beside one event: the header, Your answer (open asks, then the same answer row as the Me
 *  page), Details, Signups by answer and class, and after the lock each raid's roster. Read from the member routes alone
 *  (GET /api/me/events, /api/me/event/<key>); answers go through the Me page's own routes. Every Discord event link opens
 *  it for a member who is not an officer (App.tsx EventRoute). A key the page doesn't list prints the server's sentence
 *  above the list; a later raid's key moves the address to its event's own. */
export function MemberEventsPage({ meta }: { meta: Meta }) {
  const { key } = useParams();
  const navigate = useNavigate();
  const { search } = useLocation();  // ?g= stays on the address when a later raid's key moves to its event's
  const phone = useMediaQuery("(max-width: 48em)");
  const [list, setList] = useState<MemberEventList | null>(null);
  const [ev, setEv] = useState<MemberEventView | null>(null);
  const [gone, setGone] = useState<{ key: string; error: string } | null>(null);
  const [tick, setTick] = useState(0);
  const [ask, confirmDialog] = useConfirm();
  const seq = useRef(0);
  const loadList = () => api.get<MemberEventList>("/api/me/events").then(setList).catch(fail);
  const load = () => { loadList(); setTick((t) => t + 1); };
  useEffect(() => { loadList(); }, []);
  useLive(load, ["run:", "member"]);
  usePoll(load);

  // the detail: the route's key, else (desktop) the first open event; a phone shows the list alone until one is picked
  const pick = key || (phone || !list ? null : list.open[0]?.key || null);
  useEffect(() => {
    if (!pick) { setEv(null); return; }
    const n = ++seq.current;
    api.get<MemberEventView>(`/api/me/event/${encodeURIComponent(pick)}`).then((v) => {
      if (n !== seq.current) return;  // a newer GET was sent meanwhile: its answer is the one drawn
      setEv(v); setGone(null);
      if (key && v.key !== key) navigate({ pathname: `/events/${encodeURIComponent(v.key)}`, search }, { replace: true });
    }).catch((e) => {
      if (n !== seq.current) return;
      setEv(null);
      if (e instanceof ApiError && e.status === 404) setGone({ key: pick, error: (e.body as { error?: string } | undefined)?.error || e.message });
      else fail(e);
    });
  }, [pick, tick]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!list) return <Text c="dimmed">Loading…</Text>;
  const over = gone && gone.key === pick ? gone.error : null;
  const groups: ListGroup[] = [
    { label: "Open", items: list.open.map((r) => ({ id: r.key, title: r.title, subtitle: r.subtitle, chip: r.answer, to: `/events/${encodeURIComponent(r.key)}` })) },
    { label: "Coming up", items: list.coming.map((c, i) => ({ id: `coming:${i}`, title: c.title, subtitle: c.subtitle })) },
  ];
  const shown = over ? null : ev && (ev.key === pick || ev.part_keys.includes(pick || "")) ? ev : null;
  return (
    <ListPage title="Events" selected={over ? null : shown?.key || pick} back="/events" groups={groups} intro={list.tz_words}
      notice={over ? <Text size="sm">{over}</Text>
        /* nothing open yet, nights coming: the list would show only Coming up and say nothing about the sheet */
        : list.open.length === 0 && list.coming.length > 0 ? <Text size="sm" c="dimmed">{list.empty}</Text> : undefined}
      empty={<Text size="sm" c="dimmed">{list.empty}</Text>}>
      {pick && !over && (shown ? <MemberEvent ev={shown} meta={meta} ask={ask} onDone={load} /> : <Text c="dimmed">Loading…</Text>)}
      {confirmDialog}
    </ListPage>
  );
}

/** One live event as a member sees it. The section titles are the page's own (as the officers' Details and Signups). */
function MemberEvent({ ev, meta, ask, onDone }: {
  ev: MemberEventView; meta: Meta; ask: ReturnType<typeof useConfirm>[0]; onDone: () => void;
}) {
  return (
    <section className={css.view} aria-label={ev.title}>
      <EventHead title={ev.title} chip={ev.chip} open={ev.state === "open"} tag={ev.test_chip} />
      <ViewSection title="Your answer">
        <Stack gap="sm">
          {ev.asks.map((a) => <AskCard key={`${a.kind}:${a.id}`} a={a} meta={meta} onDone={onDone} />)}
          {ev.characters.length === 0
            ? <Text size="sm">{meta.page_words.answer_needs_character}</Text>
            : ev.answers.map((s) => <MyAnswer key={s.key} s={s} characters={ev.characters} meta={meta} ask={ask} onDone={onDone} />)}
        </Stack>
      </ViewSection>
      <EventDetails details={ev.details} />
      <ViewSection title="Signups">
        <SignupTabs tabs={["in", "sub", "out", "away"]} counts={ev.signups.counts} rows={ev.signups.rows} meta={meta} />
      </ViewSection>
      {ev.rosters.length > 0 && (
        <ViewSection title="Roster">
          <MemberRoster parts={ev.rosters} meta={meta} />
        </ViewSection>
      )}
    </section>
  );
}
