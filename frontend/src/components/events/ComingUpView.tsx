import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Stack, Text } from "@mantine/core";
import { api, ApiError, type Box, type ComingView } from "../../api";
import { fail, ok } from "../Page";
import { boxWords, useAnyBoxOpen, useConfirmBox } from "../ConfirmBox";
import { EventDetails } from "./EventDetails";
import { boxOf, EventHead } from "./shared";
import css from "../../pages/listpage.module.css";

/** A schedule's night with no sheet yet (GET /api/event/coming/{sid}/{occurrence}): its start, the chip "no sheet yet",
 *  Open the sheet now (greyed with the server's reason under it while it can't open) and Details. No Reschedule: the
 *  night is the schedule's. Open the sheet now prints the server's box and posts to the night's first raid with the
 *  box's fp as `seen`; done → the new event's page. A night that has an event now goes there; one off its schedule
 *  says so. `tick` = the page's live reload; the latest GET wins (an older answer never overwrites a newer one). While a
 *  box is open the view stays (a night opened elsewhere never unmounts the box mid-ask): the box's own refresh brings the
 *  server's sentence, and the event's page follows once it is closed. */
export function ComingUpView({ sid, occurrence, tick }: { sid: string; occurrence: string; tick: number }) {
  const navigate = useNavigate();
  const [view, setView] = useState<ComingView | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const [openedAt, setOpenedAt] = useState<string | null>(null);  // the night has its event now: its page, once no box is open
  const [ask, dialog] = useConfirmBox(["run:", "config"]);
  const held = useAnyBoxOpen();
  const seq = useRef(0);
  const url = `/api/event/coming/${encodeURIComponent(sid)}/${encodeURIComponent(occurrence)}`;
  const goneOf = (e: unknown) => (e instanceof ApiError && e.status === 404 ? (e.body as { error?: string; key?: string | null } | undefined) : undefined);
  const load = () => {
    const n = ++seq.current;
    return api.get<ComingView>(url).then((v) => { if (n === seq.current) { setView(v); setGone(null); } return v; }).catch((e) => {
      if (n !== seq.current) return null;
      const b = goneOf(e);
      if (b?.key) setOpenedAt(b.key);  // opened since: its own page
      else if (b) setGone(b.error || null);
      else fail(e);
      return null;
    });
  };
  useEffect(() => { load(); }, [url, tick]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (openedAt && !held) navigate(`/events/${encodeURIComponent(openedAt)}`, { replace: true }); }, [openedAt, held]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function openNow() {
    if (!view?.box) return;
    const o = view.open;
    let key = "";
    let said = "";
    const done = await ask({
      box: view.box,
      // the night's box asked again; a night that has its event now (or left its schedule) answers with the server's
      // sentence, Back alone
      refresh: async (): Promise<Box> => {
        try { const v = await api.get<ComingView>(url); if (v.box) return v.box; } catch (e) {
          const b = goneOf(e);
          if (b?.error) return { title: boxWords().refused, sentences: [b.error], verb: null, danger: false, diff: [], fp: `gone:${b.key || ""}` };
          throw e;
        }
        throw new Error("gone");
      },
      act: async (fp) => {
        try {
          const r = await api.post<{ message: string; key: string }>(`/api/raid/${encodeURIComponent(view.open.raid)}/open`,
            { start: o.start_iso, schedule: o.schedule, occurrence: o.occurrence, seen: fp });
          key = r.key; said = r.message;
          return null;
        } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      },
    });
    if (done && key) { ok(said); navigate(`/events/${encodeURIComponent(key)}`); }
  }

  if (gone) return <Text c="dimmed">{gone}</Text>;
  if (!view) return <Text c="dimmed">Loading…</Text>;
  return (
    <section className={css.view} aria-label={view.title}>
      <EventHead title={view.title} chip={view.chip}>
        <Stack gap={2} align="flex-end">
          <Button disabled={!view.can_open} onClick={openNow}>Open the sheet now</Button>
          {!view.can_open && view.why && <Text size="xs" c="dimmed" maw={320} ta="right">{view.why}</Text>}
        </Stack>
      </EventHead>
      <EventDetails details={view.details} />
      {dialog}
    </section>
  );
}
