import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { Button, Group, SegmentedControl, Text, Title } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { api, ApiError, opsApi, type Box, type CompSlot, type Meta, type OpsChange, type RaidRule, type Raids, type Readiness, type ReadinessRaid } from "../api";
import { ListPage } from "../components/ListPage";
import { fail, ok } from "../components/Page";
import { useConfirmBox } from "../components/ConfirmBox";
import { useOpsSave } from "../components/ReviewModal";
import { RulesView, type RaidDraft } from "../components/raids/RulesView";
import { PriorityView } from "../components/raids/PriorityView";
import { ReadinessView } from "../components/raids/ReadinessView";
import { useDirty } from "../hooks/useDirty";
import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import css from "./listpage.module.css";

type View = "rules" | "priority" | "readiness";
const VIEWS: View[] = ["rules", "priority", "readiness"];

/** The Raids page (design §7a, phase 4): a list of the game's raids beside one raid in three views — Rules (Details and
 *  Group make-up), Priority (who gets a seat when more join than fit) and Readiness (who could play, against the
 *  make-up). `/raids/<id>?view=rules|priority|readiness`; `/raids` = the first raid on a desktop, the list on a phone.
 *  Every word a raid's views print is the server's (/api/raids `page`, /api/readiness `view`); every officer edits. */
export function RaidsPage({ meta }: { meta: Meta }) {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const phone = useMediaQuery("(max-width: 48em)");
  const [data, setData] = useState<Raids | null>(null);
  const editing = useRef(false);  // the open raid's edit mode: a change made elsewhere never reloads under the draft
  const load = () => api.get<Raids>("/api/raids").then((d) => { setData(d); return d; });
  const reload = () => load().catch(fail);
  useEffect(() => { reload(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useRefresh(reload);
  useLive(() => { if (!editing.current) reload(); }, ["config", "run"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const view: View = VIEWS.includes(params.get("view") as View) ? (params.get("view") as View) : "rules";
  const at = (rid: string) => `/raids/${rid}${view === "rules" ? "" : `?view=${view}`}`;
  const selected = id ?? (phone ? null : data.raids[0]?.id ?? null);
  const raid = data.raids.find((r) => r.id === selected) || null;
  const size = (r: RaidRule) => r.page?.details.find((f) => f.label === "Size")?.value;
  return (
    <ListPage title="Raids" selected={raid ? raid.id : null} back="/raids"
      groups={[{ label: "Raids", items: data.raids.map((r) => ({ id: r.id, title: r.name, subtitle: size(r), to: at(r.id) })) }]}>
      {raid && raid.page && (
        <RaidDetail key={raid.id} r={raid} meta={meta} view={view} setView={(v) => setParams(v === "rules" ? {} : { view: v }, { replace: true })}
          onEditing={(on) => { editing.current = on; }} onSaved={reload}
          fetchRaid={async () => (await load()).raids.find((r) => r.id === raid.id)!} />
      )}
      {id && !raid && <Text c="dimmed">That raid isn't in this game. Pick one from the list.</Text>}
    </ListPage>
  );
}

/** The draft from the raid as it stands (the select strings). */
function toDraft(r: RaidRule): RaidDraft {
  return {
    lockout_days: String(r.lockout_days), duration_hours: String(r.duration_hours), first_open: r.first_open_local,
    weights: Object.fromEntries(Object.entries(r.weights).map(([k, v]) => [k, String(v)])),
    mins: (r.page?.make_up || []).map((m) => ({ slot: m.slot, label: m.label, min: String(m.min), note: m.note, floor: m.own ? 0 : m.min })),
  };
}

/** The draft as the typed changes one Save sends: the raid's settings that differ, and the minimums that differ — a
 *  minimum of 0 goes as a clear (null), never as a 0. `n` = how many edits (the leave guard's count). */
function changesOf(r: RaidRule, d: RaidDraft): { changes: OpsChange[]; n: number } {
  const base = toDraft(r);
  const fields: Record<string, unknown> = {};
  if (d.lockout_days !== base.lockout_days) fields.lockout_days = Number(d.lockout_days);
  if (Number(d.duration_hours) !== Number(base.duration_hours)) fields.duration_hours = Number(d.duration_hours);
  if (d.first_open && d.first_open.slice(0, 16) !== (base.first_open || "").slice(0, 16)) fields.first_open = d.first_open;
  const weights: Record<string, number> = {};
  for (const [k, v] of Object.entries(d.weights)) if (v !== base.weights[k]) weights[k] = Number(v);
  if (Object.keys(weights).length) fields.weights = weights;
  const comp: Record<string, { min: number; note: string | null } | null> = {};
  const was = new Map(base.mins.map((m) => [m.slot, m]));
  for (const m of d.mins) {
    const b = was.get(m.slot);
    if (b ? b.min === m.min : m.min === "0") continue;
    comp[m.slot] = m.min === "0" ? null : { min: Number(m.min), note: m.note };
  }
  const changes: OpsChange[] = [];
  if (Object.keys(fields).length) changes.push({ kind: "raid", target: r.id, fields });
  if (Object.keys(comp).length) changes.push({ kind: "comp", target: r.id, fields: comp });
  const n = Object.keys(fields).filter((k) => k !== "weights").length + Object.keys(weights).length + Object.keys(comp).length;
  return { changes, n };
}

function RaidDetail({ r, meta, view, setView, onEditing, onSaved, fetchRaid }: {
  r: RaidRule; meta: Meta; view: View; setView: (v: View) => void; onEditing: (on: boolean) => void; onSaved: () => void; fetchRaid: () => Promise<RaidRule>;
}) {
  const page = r.page!;
  const [ready, setReady] = useState<ReadinessRaid | null>(null);
  const [slots, setSlots] = useState<CompSlot[] | null>(null);
  const [draft, setDraft] = useState<RaidDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [save, review] = useOpsSave();
  const [ask, boxDialog] = useConfirmBox(["config"]);
  const editing = draft !== null;
  const { changes, n } = draft ? changesOf(r, draft) : { changes: [], n: 0 };
  useDirty(`raid:${r.id}`, editing, { n, name: r.name });
  useEffect(() => { onEditing(editing); return () => onEditing(false); }, [editing]);  // eslint-disable-line react-hooks/exhaustive-deps
  const loadReady = () => api.get<Readiness>(`/api/readiness?raid=${encodeURIComponent(r.id)}`)
    .then((x) => { setReady(x.raids[0] || null); setSlots(x.slots); }).catch(fail);
  useEffect(() => { loadReady(); }, [r]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(loadReady, ["member"]);

  async function onSave() {
    if (!draft) return;
    if (!changes.length) { setDraft(null); return; }
    setBusy(true);
    try {
      const x = await save(changes, { revs: r.rev ? { [`config:raids:${r.id}`]: r.rev } : undefined, always: true, verb: "Save", title: `Save ${r.name}?` });
      if (x?.changed?.length) onSaved();  // someone changed this raid meanwhile: the toast said so; the draft stays over the fresh raid
      else if (x?.applied?.length) { setDraft(null); onSaved(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }

  /** Reset: the server's box (page.boxes.reset, checked against config:raid_all). The rev sent is the one of the box on
   *  screen: a 409 or a live change brings the fresh box AND its rev, so the next press can go through. */
  async function onReset() {
    const shown = { rev: r.reset_rev };
    const resetBox = async (): Promise<Box> => {
      const fresh = await fetchRaid();
      shown.rev = fresh.reset_rev;
      return fresh.page!.boxes.reset;
    };
    await ask({
      box: page.boxes.reset,
      refresh: resetBox,
      act: async () => {
        try {
          const x = await opsApi.confirm([{ kind: "raid_reset", target: r.id }], shown.rev ? { [`config:raid_all:${r.id}`]: shown.rev } : undefined);
          if (x.refused?.length) fail(new Error(x.refused.join("\n")));
          else ok(x.summary || "saved");
          onSaved();
          return null;
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) return resetBox();
          throw e;
        }
      },
    });
  }

  const actions = view === "readiness" && !editing ? null : editing ? (
    <Group gap="xs">
      <Button variant="default" size="sm" onClick={() => setDraft(null)} disabled={busy}>{meta.confirm_words.discard}</Button>
      <Button size="sm" loading={busy} onClick={onSave}>Save</Button>
    </Group>
  ) : (
    <Group gap="xs">
      {page.boxes.reset.verb && <Button variant="subtle" color="red" size="sm" onClick={onReset}>Reset to defaults</Button>}
      <Button variant="default" size="sm" onClick={() => setDraft(toDraft(r))}>Edit</Button>
    </Group>
  );
  return (
    <section className={css.view} aria-label={r.name}>
      <div className={css.sect}>
        <Group justify="space-between" align="center" wrap="wrap" gap="sm">
          <Group gap="md" wrap="wrap" align="center">
            <Title order={2} size="h3">{r.name}</Title>
            <SegmentedControl aria-label="Which view" value={view} onChange={(v) => setView(v as View)}
              data={[{ value: "rules", label: "Rules" }, { value: "priority", label: "Priority" }, { value: "readiness", label: "Readiness" }]} />
          </Group>
          {actions}
        </Group>
      </div>
      {view === "rules" && <RulesView page={page} size={r.size} draft={draft} setDraft={setDraft} slots={slots} />}
      {view === "priority" && <PriorityView page={page} draft={draft} setDraft={setDraft} />}
      {view === "readiness" && <ReadinessView meta={meta} view={ready?.view || null} onEdit={() => { if (!draft) setDraft(toDraft(r)); setView("rules"); }} />}
      {review}
      {boxDialog}
    </section>
  );
}
