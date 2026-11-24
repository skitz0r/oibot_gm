import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button, Group, Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { api, ApiError, opsApi, type Box, type Meta, type OpsChange, type ScheduleList, type ScheduleView } from "../api";
import { ListPage } from "../components/ListPage";
import { fail, ok } from "../components/Page";
import { useConfirmBox } from "../components/ConfirmBox";
import { guardNav } from "../components/LeaveGuard";
import { EventHead } from "../components/events/shared";
import { ScheduleDetails } from "../components/schedules/ScheduleDetails";
import { RegularsSection, RemovedRegulars } from "../components/schedules/RegularsSection";
import { LineupPicture } from "../components/lineup/LineupPicture";
import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import css from "./listpage.module.css";

/** The Schedules page (design §7a — Schedules.dc.html): the list (Active · Paused, each with when it repeats) beside
 *  one schedule in sections — its header (name, chip, Edit, Pause / Resume, Remove), Details, Lineup, Sheet timing,
 *  Next events and Regulars. `/schedules` = the first schedule on a desktop, the list on a phone; `/schedules/<id>` one
 *  schedule; New schedule and Edit open the lineup builder's page (pages/LineupPage). Every word is the server's
 *  (/api/schedules, /api/schedules/{id}); every officer edits. A schedule whose raid left the game is listed, says so,
 *  shows no next events and keeps its Remove. A removed schedule's leftover regulars are listed under the list. */
export function SchedulesPage({ meta }: { meta: Meta }) {
  const { id } = useParams();
  const phone = useMediaQuery("(max-width: 48em)");
  const [list, setList] = useState<ScheduleList | null>(null);
  const listSeq = useRef(0);  // the latest GET wins: an older answer never overwrites a newer one
  const loadList = () => {
    const n = ++listSeq.current;
    return api.get<ScheduleList>("/api/schedules").then((l) => { if (n === listSeq.current) setList(l); return l; })
      .catch((e) => { if (n === listSeq.current) fail(e); return null; });
  };
  useEffect(() => { loadList(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useRefresh(loadList);
  useLive(loadList, ["config", "member"]);
  if (!list) return <Text c="dimmed">Loading…</Text>;
  const rows = [...list.active, ...list.paused];
  const selected = id ?? (phone ? null : rows[0]?.id ?? null);
  const item = (s: ScheduleList["active"][number]) => ({ id: s.id, title: s.name, subtitle: s.repeats, to: `/schedules/${encodeURIComponent(s.id)}` });
  const removed = list.orphans.length > 0 ? <RemovedRegulars orphans={list.orphans} meta={meta} onReload={loadList} /> : null;
  return (
    <ListPage title="Schedules" selected={selected} back="/schedules"
      action={<Button onClick={() => guardNav("/schedules/new")}>New schedule</Button>}
      groups={[{ label: "Active", items: list.active.map(item) }, { label: "Paused", items: list.paused.map(item) }]}
      more={removed}
      empty={<>
        <Text size="sm" c="dimmed" px={12}>{meta.page_words.schedules_empty}</Text>
        {removed}
      </>}>
      {selected && <ScheduleDetail key={selected} sid={selected} meta={meta} onChanged={loadList} />}
    </ListPage>
  );
}

/** One schedule: the read model, reloaded on `config` (a schedule edited), `member` (a regular) and `run:` (an event
 *  opened from it). Pause / Resume / Remove print the read model's box and press through the TOKEN path: a preview of
 *  the change whose box must be the one on screen (else the fresh box, said so, waits again), then its token. The rev
 *  sent is the one of the box on screen: a 409 brings the fresh box AND its rev, so the next press goes through. */
function ScheduleDetail({ sid, meta, onChanged }: { sid: string; meta: Meta; onChanged: () => void }) {
  const navigate = useNavigate();
  const [view, setView] = useState<ScheduleView | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const goneWords = useRef("");  // the route's 404 sentence when the schedule went while a box was open
  const [ask, dialog] = useConfirmBox(["config", "member", "run:"]);
  const seq = useRef(0);  // the latest GET wins: an older answer never overwrites a newer one
  const load = () => {
    const n = ++seq.current;
    return api.get<ScheduleView>(`/api/schedules/${encodeURIComponent(sid)}`).then((v) => { if (n === seq.current) { setView(v); setGone(null); } return v; }).catch((e) => {
      if (e instanceof ApiError && e.status === 404) {
        goneWords.current = (e.body as { error?: string } | undefined)?.error || e.message;
        if (n === seq.current) setGone(goneWords.current);
      } else if (n === seq.current) fail(e);
      return null;
    });
  };
  useEffect(() => { load(); }, [sid]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["config", "member", "run:"]);
  if (gone) return <Text c="dimmed">{gone}</Text>;
  if (!view) return <Text c="dimmed">Loading…</Text>;

  /** a press on one of the read model's boxes: `pick` reads the box AND the change it sends from a read model — the one
   *  on screen first, then a fresh one (another officer paused it meanwhile: the Resume box, and Resume is what goes).
   *  The schedule removed meanwhile: the route's own sentence, and the list is read again. */
  const press = async (pick: (v: ScheduleView) => { box: Box; changes: OpsChange[] }, after?: () => void) => {
    const first = pick(view);
    const shown = { rev: view.rev, changes: first.changes };
    const fresh = async (): Promise<Box> => {
      const v = await load();
      if (!v) { onChanged(); throw new Error(goneWords.current); }
      const p = pick(v);
      shown.rev = v.rev;
      shown.changes = p.changes;
      return p.box;
    };
    const done = await ask({
      box: first.box,
      refresh: fresh,
      act: async (fp) => {
        const revs = { [`config:schedule:${view.id}`]: shown.rev };
        try {
          const changes = shown.changes;
          const pv = await opsApi.preview(changes, revs);
          const b = pv.described?.box;
          if (b && b.fp !== fp) return b;  // it changed while the box was open: the fresh box, said so, waits again
          if (!pv.token) {  // a refusal, or nothing left to change (someone did it meanwhile): said, and the box closes
            if (pv.refused?.length) fail(new Error(pv.refused.join("\n"))); else ok(pv.summary || "");
            return null;
          }
          const r = await opsApi.apply(pv.token, changes, pv.fingerprint, revs);
          if (r.needs_review && r.described?.box) return r.described.box;  // its words moved at the press: the fresh box
          if (r.refused?.length) fail(new Error(r.refused.join("\n"))); else ok(r.summary || "");
          return null;
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) return fresh();
          throw e;
        }
      },
    });
    if (done) { onChanged(); after?.(); }
  };
  const pause = () => press((v) => ({ box: (v.active ? v.boxes.pause : v.boxes.resume)!,
    changes: [{ kind: "schedule", target: v.id, fields: { active: !v.active } }] }));
  const remove = () => press((v) => ({ box: v.boxes.remove, changes: [{ kind: "schedule_remove", target: v.id }] }),
    () => navigate("/schedules", { replace: true }));
  const edit = `/schedules/${encodeURIComponent(view.id)}/edit`;

  return (
    <section className={css.view} aria-label={view.name}>
      <EventHead title={view.name} chip={view.chip} open={view.active}>
        <Group gap="xs" wrap="wrap">
          {!view.gone && <Button variant="default" onClick={() => guardNav(edit)}>Edit</Button>}
          {(view.boxes.pause || view.boxes.resume) && <Button variant="default" onClick={pause}>{view.active ? "Pause" : "Resume"}</Button>}
          <Button variant="default" onClick={remove}>Remove</Button>
        </Group>
      </EventHead>
      {view.gone && <div className={css.sect}><Text size="sm" fw={600} c="red">{view.gone}</Text></div>}
      <ScheduleDetails view={view} lineup={<LineupPicture lineup={view.lineup} />} />
      {view.regulars && <RegularsSection view={view} meta={meta} onReload={load} />}
      {dialog}
    </section>
  );
}

