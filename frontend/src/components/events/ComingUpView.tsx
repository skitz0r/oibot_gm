import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Stack, Text } from "@mantine/core";
import { api, ApiError, type Box, type ComingView } from "../../api";
import { fail, ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { EventDetails } from "./EventDetails";
import { boxOf, EventHead } from "./shared";
import css from "../../pages/listpage.module.css";

/** A schedule's night with no sheet yet (GET /api/event/coming/{sid}/{occurrence}): its start, the chip "no sheet yet",
 *  Open the sheet now (greyed with the server's reason under it while it can't open) and Details. No Reschedule: the
 *  night is the schedule's. Open the sheet now prints the server's box and posts to the night's first raid with the
 *  box's fp as `seen`; done → the new event's page. A night that has an event now goes there; one off its schedule
 *  says so. `tick` = the page's live reload. */
export function ComingUpView({ sid, occurrence, tick }: { sid: string; occurrence: string; tick: number }) {
  const navigate = useNavigate();
  const [view, setView] = useState<ComingView | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const [ask, dialog] = useConfirmBox(["run:", "config"]);
  const url = `/api/event/coming/${encodeURIComponent(sid)}/${encodeURIComponent(occurrence)}`;
  const load = () => api.get<ComingView>(url).then((v) => { setView(v); setGone(null); return v; }).catch((e) => {
    const b = e instanceof ApiError && e.status === 404 ? (e.body as { error?: string; key?: string | null } | undefined) : undefined;
    if (b?.key) navigate(`/events/${encodeURIComponent(b.key)}`, { replace: true });  // opened since: its own page
    else if (b) setGone(b.error || null);
    else fail(e);
    return null;
  });
  useEffect(() => { load(); }, [url, tick]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function openNow() {
    if (!view?.box) return;
    const o = view.open;
    let key = "";
    let said = "";
    const done = await ask({
      box: view.box,
      refresh: async (): Promise<Box> => { const v = await load(); if (v?.box) return v.box; throw new Error("gone"); },
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
