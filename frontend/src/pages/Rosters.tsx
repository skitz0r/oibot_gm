import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { DateTimePicker } from "@mantine/dates";
import { ActionIcon, Anchor, Badge, Box, Button, Card, Drawer, Group, Modal, Select, Stack, Tabs, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconCalendarPlus, IconFlask, IconPlus, IconUserPlus, IconX } from "@tabler/icons-react";
import { api, type Board, type Meta, type Person, type RaidRuns, type Rosters, type Series, type Sheet, type UpcomingRun } from "../api";
import { GameIcon } from "../components/Icons";
import { useConfirm } from "../components/ConfirmModal";
import { classColour } from "../theme";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { BoardPreview } from "../components/board/BoardPreview";
import { Detail, SheetCard } from "../components/board/SheetCard";
import { TestFeed } from "../components/TestFeed";
import type { NightSheet, OpenPreview, RunsPage } from "../components/board/types";
import { WhenItRuns, raidCardCss } from "../components/RaidCard";
import { RoleCounts, runName, type Act, type OnBoard } from "../components/board/shared";
import { LevelChip } from "../components/LevelChip";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import rosterCss from "./rosters.module.css";

export function RostersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Rosters | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = (): Promise<void> => api.get<Rosters>("/api/rosters").then(setData).catch((e) => { fail(e); });
  useEffect(() => { load(); }, []);
  usePoll(load);
  useLive(load, ["run:", "member", "loot:"]);  // (the Test tab's feed listens to "test:" itself)  // a save anywhere (another officer, Discord, the scheduler) refreshes this page within a second
  // deep link from Discord cards: /app/rosters#run-<key> scrolls to that run once it has rendered
  useEffect(() => {
    if (!data || !window.location.hash.startsWith("#run-")) return;
    const t = setTimeout(() => document.getElementById(window.location.hash.slice(1))?.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
    return () => clearTimeout(t);
  }, [data]);

  async function act(key: string, path: string, payload?: unknown) {
    setBusy(key);
    try { const r = await api.post<{ message: string }>(path, payload ?? {}); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  /** Board edits answer with the new board; patch it into the sheet without a full reload. */
  function patchBoard(key: string, board: Board, needs?: Sheet["needs"], rev?: string) {
    setData((d) => d && { ...d, raids: d.raids.map((r) => ({ ...r, open: r.open.map((e) => (e.key === key ? { ...e, board, needs: needs ?? e.needs, rev: rev ?? e.rev, has_layout: true } : e)), locked: r.locked.map((e) => (e.key === key ? { ...e, board, needs: needs ?? e.needs, rev: rev ?? e.rev } : e)) })) });
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  const zone = meta.timezones?.flatMap((g) => g.items).find((i) => i.value === data.tz)?.label || data.tz;  // the zone in words, as on Raids
  return (
    <Stack gap="lg" className={rosterCss.touch}>  {/* phone: every control 44 px (rosters.module.css) */}
      <PageTitle title="Rosters" intro={`Each raid's runs, soonest first: answer for someone, shape the board, lock, fill. Times in ${zone}.`} />
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} people={data.people || []} busy={busy} onAct={act} onBoard={patchBoard} onReload={load} />)}
      {data.orphans.length > 0 && (
        <Card><Box p="md"><Eyebrow plain>Sheets not tied to a raid</Eyebrow>{data.orphans.map((e) => <Text key={e.key} size="sm">{runName(e)} · {e.state_word || meta.run_states[e.state] || e.state}</Text>)}</Box></Card>
      )}
    </Stack>
  );
}

/** a person's own clock for the open confirms: day without a leading zero, 12-hour (the Clock rule) */
const yourClock = (iso: string) => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hour12: true });

/** Open the next run of a recurring schedule: one press (a schedule select when the raid has several) — the first
 *  upcoming run the server says can open. When none can, the reason is written under the button (a disabled button's
 *  tooltip never shows). */
function OpenNext({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [sched, setSched] = useState<string>("");  // "" = the next run of any recurring schedule
  const recurring = (r.schedules || []).filter((s) => s.active && s.class !== "adhoc" && s.kind !== "pickup");
  const rows = r.upcoming.filter((u) => !sched || u.schedule === sched);
  const next = rows.find((u) => u.can_open);
  const why = next ? null : rows[0]?.why || "Nothing to open: this schedule has no run coming — check it on the Raids page.";
  return (
    <Stack gap={4} align="flex-end" style={{ maxWidth: 340 }}>
      <Group gap="xs" align="center" wrap="wrap" justify="flex-end">
        {recurring.length > 1 && (
          <Select size="sm" w={180} allowDeselect={false} aria-label="which recurring schedule" value={sched} onChange={(v) => setSched(v || "")}
            data={[{ value: "", label: "Any schedule" }, ...recurring.map((s) => ({ value: s.id, label: s.name }))]} />
        )}
        <Tooltip label={next ? `Opens the sheet for ${next.start} now` : ""} disabled={!next} withinPortal>
          <Button size="sm" variant="default" leftSection={<IconPlus size={15} />} loading={busy === `open${r.id}`} disabled={!next}
            onClick={() => next && onAct(`open${r.id}`, `/api/raid/${r.id}/open`, { start: next.start_iso, schedule: next.schedule })}>Open next run</Button>
        </Tooltip>
      </Group>
      {why && <Text size="xs" c="dimmed" ta="right">{why}</Text>}
    </Stack>
  );
}

/** Start an ad hoc run: a button opening one screen — a pickup template (its own settings) or a one-off, at a date and
 *  time picked (nobody types a timestamp). Every change asks the server what Open would do (POST
 *  /api/raid/{rid}/open/preview, rc.open_preview): a time that IS an upcoming recurring run relabels the button "Open the
 *  … run — scheduled, with confirmations"; the screen shows both clocks (guild time and yours), the run's rules, the DST
 *  note and where the sheet posts, and why Start waits when it does. Start carries the preview's exact instant and key
 *  back, so a stale match is refused rather than opening another run. */
function StartAdhoc({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [opened, setOpened] = useState(false);
  const [tpl, setTpl] = useState<string>("adhoc");  // "adhoc" = a one-off
  const [when, setWhen] = useState<string | null>(null);
  const [p, setP] = useState<OpenPreview | null>(null);
  const templates = (r.schedules || []).filter((s) => s.active && (s.class === "adhoc" || s.kind === "pickup"));
  const picked = (when || "").trim();
  useEffect(() => {
    if (!picked) { setP(null); return; }
    let live = true;
    // a one-off sends no schedule: the server matches an upcoming recurring run at that minute (which_run)
    api.post<OpenPreview>(`/api/raid/${r.id}/open/preview`, { when: picked.slice(0, 16), schedule: tpl === "adhoc" ? "" : tpl })
      .then((x) => { if (live) setP(x); }).catch(() => { if (live) setP(null); });
    return () => { live = false; };
  }, [picked, tpl, r.id]);
  const scheduled = p?.kind === "scheduled";
  const label = scheduled ? `Open the ${p!.schedule_name} run — scheduled, with confirmations` : "Start the ad hoc run";
  const waits = !picked ? "Pick the day and time it starts." : !p ? "Checking that time…" : p.refusal ? p.refusal : p.past ? "That time has passed — pick a later one." : null;
  const close = () => { setOpened(false); setWhen(null); setP(null); };
  async function start() {
    if (!p || waits) return;
    close();
    await onAct(`adhoc${r.id}`, `/api/raid/${r.id}/open`, { start: p.start, key: p.key,
      // a time that IS a scheduled run opens as that schedule's run (null = the raid's own run times → "default");
      // sending "adhoc" there would resolve to a one-off key and the server would refuse the stale preview (409)
      schedule: p.kind === "scheduled" ? (p.schedule || "default") : (p.schedule || (tpl === "adhoc" ? "adhoc" : tpl)) });
  }
  return (
    <>
      <Button size="sm" variant="default" leftSection={<IconCalendarPlus size={15} />} loading={busy === `adhoc${r.id}`} onClick={() => setOpened(true)}>Start an ad hoc run…</Button>
      <Modal opened={opened} onClose={close} title={`Start an ad hoc ${r.name} run`} centered>
        <Stack gap="sm">
          {templates.length > 0 && (
            <Select label="Settings" allowDeselect={false} value={tpl} onChange={(v) => setTpl(v || "adhoc")}
              data={[{ value: "adhoc", label: "One-off" }, ...templates.map((s) => ({ value: s.id, label: s.name }))]} />
          )}
          <DateTimePicker
            label="When it starts" clearable value={when} onChange={setWhen} placeholder="pick a date and time"
            minDate={new Date().toISOString().slice(0, 10)}
            valueFormat="ddd D MMM YYYY h:mm A" timePickerProps={{ format: "12h", withDropdown: true, popoverProps: { withinPortal: false } }}
            popoverProps={{ withinPortal: true }} aria-label="when the ad hoc run starts"
          />
          {p && (
            <Stack gap={2}>
              <Text size="sm"><b>{p.label}</b> guild time · {yourClock(p.start)} your time</Text>
              <Text size="sm" c="dimmed">{p.rules}</Text>
              {p.when_note && <Text size="sm" c="dimmed">{p.when_note}</Text>}
              {p.exists && <Text size="sm" c="dimmed">A run for that time already exists ({p.exists}); Open points you at it instead of making a second.</Text>}
              <Text size="sm" c="dimmed">{p.posts}</Text>
            </Stack>
          )}
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={close}>Back</Button>
            <Button disabled={!!waits} onClick={start}>{label}</Button>
          </Group>
          {waits && <Text size="xs" c={p?.refusal ? "red" : "dimmed"} ta="right">{waits}</Text>}
        </Stack>
      </Modal>
    </>
  );
}

/** The raid's upcoming runs inside "When it runs" (design §5.56): each active schedule's next three runs, whatever the
 *  horizon — the soonest as "next run", the rest behind one toggle. A row the server says can open has Open now (one
 *  press, then the confirm with the server's preview); one that can't says why, in the server's words. */
function UpcomingRows({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [ask, dialog] = useConfirm();
  const [all, setAll] = useState(false);
  const rows = all ? r.upcoming : r.upcoming.slice(0, 1);
  async function open(u: UpcomingRun) {
    let p: OpenPreview;
    try { p = await api.post<OpenPreview>(`/api/raid/${r.id}/open/preview`, { start: u.start_iso, schedule: u.schedule }); } catch (err) { fail(err); return; }
    if (p.refusal) { fail(new Error(p.refusal)); return; }
    const message = [`${p.label} guild time · ${yourClock(p.start)} your time`, p.rules, p.posts].join("\n");  // the server's words; the browser adds only the reader's own clock
    if (!(await ask({ title: `Open ${r.name} · ${u.start} now?`, message, confirmLabel: "Open now" }))) return;
    await onAct(`up${u.key}`, `/api/raid/${r.id}/open`, { start: p.start, schedule: u.schedule, key: p.key });
  }
  const css = raidCardCss;
  return (
    <>
      {rows.map((u, i) => (
        <li key={u.key} className={css.run}>
          <span className={[css.runDot, css.runNext].join(" ")} />
          <span className={css.runWhen}>{u.start}</span>
          <span className={css.runWhat}>{i === 0 ? "next run · " : ""}{u.schedule_name} — its sheet opens {u.opens}</span>
          {u.can_open && <Button size="compact-xs" variant="light" loading={busy === `up${u.key}`} onClick={() => open(u)}>Open now</Button>}
          {u.needs_channel && <Text size="xs" c="red">needs a signup channel</Text>}
          {!u.can_open && u.why && <Text size="xs" c="dimmed" style={{ flexBasis: "100%", paddingLeft: 20 }}>{u.why}</Text>}
        </li>
      ))}
      {r.upcoming.length > 1 && (
        <li className={css.run}>
          <Anchor component="button" type="button" size="xs" onClick={() => setAll((x) => !x)} style={{ paddingLeft: 20 }}>
            {all ? "Show only the next run" : `Show the next ${r.upcoming.length} runs`}
          </Anchor>
        </li>
      )}
      {dialog}
    </>
  );
}

/** History: the newest runs first, paged (GET /api/runs beyond the first 8), each row opening a read-only drawer with
 *  the run in full (GET /api/run/{key}); `#run-<key>` deep links open it. */
function History({ r, meta }: { r: RaidRuns; meta: Meta }) {
  const [more, setMore] = useState<Sheet[]>([]);
  const [total, setTotal] = useState<number>(r.past_total ?? r.past.length);
  const [busy, setBusy] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const rows = [...r.past, ...more.filter((m) => !r.past.some((p) => p.key === m.key))];
  useEffect(() => {  // a #run-<key> deep link to a past run opens its drawer
    const h = window.location.hash.startsWith("#run-") ? window.location.hash.slice(5) : "";
    if (h && rows.some((e) => e.key === h)) setOpenKey(h);
  }, [r.past]);  // eslint-disable-line react-hooks/exhaustive-deps
  async function loadMore() {
    setBusy(true);
    try { const pg = await api.get<RunsPage>(`/api/runs?raid=${encodeURIComponent(r.id)}&offset=${rows.length}&limit=20`); setMore((m) => [...m, ...pg.runs]); setTotal(pg.total); } catch (err) { fail(err); } finally { setBusy(false); }
  }
  return (
    <Stack gap={4}>
      {rows.length === 0 && <Text size="sm" c="dimmed">No past runs yet — a run lands here once it ends or is cancelled.</Text>}
      {rows.map((e) => (
        <UnstyledButton key={e.key} aria-label={`open the run of ${e.when}`} onClick={() => { setOpenKey(e.key); window.history.replaceState(null, "", `#run-${e.key}`); }}
          style={{ borderRadius: 6, padding: "2px 4px", width: "fit-content", maxWidth: "100%" }}>
          <Text size="sm" component="span"><Badge size="xs" variant="outline" color={e.state === "done" ? "teal" : "gray"} tt="none" mr={6}>{e.state_word || meta.run_states[e.state] || e.state}</Badge>{e.when} · {e.counts.in} joined · {e.rostered} rostered{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>
        </UnstyledButton>
      ))}
      {rows.length < total && <Button size="compact-xs" variant="subtle" loading={busy} onClick={loadMore} w="fit-content">Show older runs ({total - rows.length} more)</Button>}
      <RunDrawer runKey={openKey} meta={meta} onClose={() => { setOpenKey(null); if (window.location.hash.startsWith("#run-")) window.history.replaceState(null, "", window.location.pathname + window.location.search); }} />
    </Stack>
  );
}

/** A past run, read-only: its summary, the roster as it was, and the detail (fill asks, callouts, the log in words). An
 *  end by mistake can be reopened from here within the hour (never a cancelled run). */
function RunDrawer({ runKey, meta, onClose }: { runKey: string | null; meta: Meta; onClose: () => void }) {
  const [e, setE] = useState<NightSheet | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!runKey) { setE(null); return; }
    api.get<NightSheet>(`/api/run/${runKey}`).then(setE).catch((err) => { fail(err); onClose(); });
  }, [runKey]);  // eslint-disable-line react-hooks/exhaustive-deps
  async function reopen() {
    if (!e) return;
    setBusy(true);
    try { const x = await api.post<{ message: string }>(`/api/run/${e.key}/reopen`, {}); ok(x.message); onClose(); } catch (err) { fail(err); } finally { setBusy(false); }
  }
  return (
    <Drawer opened={!!runKey} onClose={onClose} position="right" size="xl" title={e ? `${e.raid} · ${e.when}` : "Loading…"}>
      {e && (
        <Stack gap="sm">
          <Group gap="sm"><Badge variant="outline" color={e.state === "done" ? "teal" : "gray"} tt="none">{e.state_word || meta.run_states[e.state] || e.state}</Badge>{e.closed_at && <Text size="xs" c="dimmed">closed {e.closed_at}</Text>}</Group>
          <Text size="sm">{e.counts.in} joined · {e.rostered} rostered{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>
          {e.board && e.rostered > 0 && <BoardPreview meta={meta} board={e.board} />}
          {e.may_reopen && <Tooltip label="ended by mistake: the run goes back to locked, as it was"><Button size="xs" variant="light" loading={busy} onClick={reopen} w="fit-content">{meta.run_labels.reopen}</Button></Tooltip>}
          {!e.may_reopen && e.reopen_note && e.state === "done" && <Text size="xs" c="dimmed">{e.reopen_note}</Text>}
          <Detail e={e} meta={meta} readOnly />
        </Stack>
      )}
    </Drawer>
  );
}

/** One recurring schedule's regulars: role icon + count against the minimum, one member per line (spec icon + character;
 *  the member's name in the tooltip), add (member, then character) and remove. Officers edit; it is the same route as
 *  Discord's /roster add|remove, plain text and MCP. */
function RegularsCard({ r, s, meta, people, onAct, busy }: { r: RaidRuns; s: Series; meta: Meta; people: Person[]; onAct: Act; busy: string | null }) {
  const [who, setWho] = useState<string | null>(null);
  const [char, setChar] = useState<string | null>(null);
  const [ask, dialog] = useConfirm();
  const person = people.find((p) => p.uid === who);
  const on = new Set(s.regulars.map((x) => x.uid));
  const level = s.level || (Object.entries(s.need).some(([role, n]) => (s.counts[role] || 0) < n) ? "red" : "green");
  const spare = s.no_spare || [];
  const levelWords = { green: "regular mains cover the tank and healer minimums, with a spare", amber: spare.length ? `no spare ${spare.join(" or ")} among regular mains — one absence leaves it short` : "covered only with offspecs, flex roles or alts", red: "regulars can't field the minimum" }[level];
  // the sentence is the server's (consequences.regular_remove), the write carries the member record's rev (stale → 409)
  async function remove(x: Series["regulars"][number]) {
    if (!(await ask({ title: `Take ${x.display_name} off the regulars?`, message: x.remove, confirmLabel: "Remove", cancelLabel: "Back", color: "red" }))) return;
    await onAct(`reg${s.key}${x.uid}`, "/api/admin/raid/regulars", { raid: r.id, schedule: s.id, uid: x.uid, on: false, rev: x.rev });
  }
  async function add() {
    if (!person) return;
    await onAct(`reg${s.key}add`, "/api/admin/raid/regulars", { raid: r.id, schedule: s.id, uid: person.uid, character: char || person.characters[0]?.label, on: true });
    setWho(null); setChar(null);
  }
  return (
    <Box p="sm" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 260, flex: "1 1 300px" }}>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Box style={{ minWidth: 0 }}>
          <Text fw={700} size="sm">{s.name}{!s.active && <Badge ml={6} size="xs" color="gray" variant="light" tt="none">paused</Badge>}</Text>
          <Text size="xs" c="dimmed">{s.label} · {s.regulars.length} regular{s.regulars.length === 1 ? "" : "s"} for {s.seats} seats</Text>
        </Box>
        {/* the same ready / thin / short chip as Readiness (the word from meta.level_words), why in its tooltip */}
        <Group gap="sm" wrap="nowrap"><RoleCounts meta={meta} counts={s.counts} need={s.need} size={16} /><LevelChip meta={meta} level={level} why={levelWords} size="sm" /></Group>
      </Group>
      {s.regulars.length > 0 && level === "amber" && (s.cover || []).length > 0 && <Text size="xs" c="dimmed" mt={4}>could cover: {(s.cover || []).join(", ")}</Text>}
      {s.regulars.length > 0 && level === "red" && <Text size="xs" c="red" mt={4}>recruit{(s.recruit || []).length ? `: ${(s.recruit || []).join(", ")}` : " — nobody in the guild plays the missing role"}</Text>}
      <Forecast s={s} meta={meta} />
      <Stack gap={0} mt={6}>
        {s.regulars.length === 0 && <Text size="sm" c="dimmed">No regulars yet — every main counts as expected until you add some.</Text>}
        {s.regulars.map((x) => (
          <Group key={x.uid} gap={6} wrap="nowrap" style={{ lineHeight: 1.8 }}>
            <GameIcon meta={meta} kind="spec" id={`${x.cls}:${x.spec}`} size={18} title={`${x.spec} · ${x.role}`} />
            <Tooltip label={x.display_name}><Text size="sm" fw={600} c={classColour(meta, x.cls)} truncate style={{ flex: 1 }}>{x.character}</Text></Tooltip>
            <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`remove ${x.character}`} loading={busy === `reg${s.key}${x.uid}`} onClick={() => remove(x)}><IconX size={13} /></ActionIcon>
          </Group>
        ))}
      </Stack>
      <Group gap="xs" mt="xs" wrap="wrap">
        <Select size="xs" w={170} searchable placeholder="add a member" value={who} onChange={(v) => { setWho(v); setChar(null); }}
          data={people.filter((p) => !on.has(p.uid)).map((p) => ({ value: p.uid, label: p.display_name }))} nothingFoundMessage="nobody left" />
        {person && person.characters.length > 1 && (
          <Select size="xs" w={160} allowDeselect={false} value={char || person.characters[0].label} onChange={setChar} aria-label="which character"
            data={person.characters.map((c) => ({ value: c.label, label: c.label + (c.is_main ? " (main)" : "") }))} />
        )}
        <Button size="xs" variant="default" leftSection={<IconUserPlus size={13} />} disabled={!person} loading={busy === `reg${s.key}add`} onClick={add}>{person ? `Add ${char || person.characters[0]?.label || person.display_name}` : "Add"}</Button>
      </Group>
      {dialog}
    </Box>
  );
}

/** The series' 4-week forecast: each upcoming start with what the regulars minus recorded absences leave short — role
 *  icon + number (icons stand alone), who is away in the tooltip. Officers see a hole a week ahead. */
function Forecast({ s, meta }: { s: Series; meta: Meta }) {
  const rows = s.forecast || [];
  if (!rows.length) return null;
  return (
    <Box mt={6}>
      <Eyebrow plain>Next 4 weeks</Eyebrow>
      <Stack gap={0}>
        {rows.map((f) => (
          <Tooltip key={f.start} label={f.away.length ? `away: ${f.away.join(", ")}` : `${f.expected} regulars expected`} withinPortal>
            <Group gap={8} wrap="nowrap" style={{ lineHeight: 1.7 }}>
              <Text size="xs" w={78} style={{ fontVariantNumeric: "tabular-nums" }}>{f.day}</Text>
              {Object.keys(f.short).length === 0 ? <Text size="xs" c="teal">covered</Text> : Object.entries(f.short).map(([role, k]) => (
                <Group key={role} gap={2} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={14} title={role} /><Text size="xs" c="red" style={{ fontVariantNumeric: "tabular-nums" }}>−{k}</Text></Group>
              ))}
              {f.away.length > 0 && <Text size="xs" c="dimmed">{f.away.length} away</Text>}
            </Group>
          </Tooltip>
        ))}
      </Stack>
    </Box>
  );
}

function Regulars({ r, meta, people, onAct, busy }: { r: RaidRuns; meta: Meta; people: Person[]; onAct: Act; busy: string | null }) {
  const series = r.series || [];
  const orphans = r.orphan_regulars || [];
  const [ask, dialog] = useConfirm();
  /** Clear a removed schedule's regular: asks first with the server's sentence, carries the member record's rev, and the
   *  row's button spins while it writes. */
  async function clear(o: NonNullable<RaidRuns["orphan_regulars"]>[number]) {
    if (!(await ask({ title: `Clear ${o.character} from ${o.label}?`, message: o.clear, confirmLabel: "Clear", cancelLabel: "Back", color: "red" }))) return;
    await onAct(`orph${o.key}${o.uid}`, "/api/admin/raid/regulars", { raid: r.id, schedule: o.key.split(":")[1], uid: o.uid, on: false, rev: o.rev });
  }
  if (!series.length && !orphans.length) return <Text size="sm" c="dimmed">No recurring schedule yet — add weekly run times on the Raids page; ad hoc runs have no regulars.</Text>;
  return (
    <Stack gap="sm">
      <Group gap="sm" align="stretch" wrap="wrap">{series.map((s) => <RegularsCard key={s.key} r={r} s={s} meta={meta} people={people} onAct={onAct} busy={busy} />)}</Group>
      {orphans.length > 0 && (
        <Box>
          <Text size="xs" c="dimmed">Regulars of a removed schedule — clear them:</Text>
          {orphans.map((o) => (
            <Group key={o.key + o.uid} gap={6} wrap="nowrap">
              <GameIcon meta={meta} kind="spec" id={`${o.cls}:${o.spec}`} size={16} title={o.spec} />
              <Tooltip label={o.display_name}><Text size="sm">{o.character}</Text></Tooltip>
              <Text size="xs" c="dimmed">{o.label}</Text>
              <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`clear ${o.character}`} loading={busy === `orph${o.key}${o.uid}`} disabled={!!busy?.startsWith("orph")} onClick={() => clear(o)}><IconX size={13} /></ActionIcon>
            </Group>
          ))}
        </Box>
      )}
      {dialog}
    </Stack>
  );
}

/** One raid: its header (the schedules in words, Open next run, Start an ad hoc run…), "When it runs" (the Raids card's
 *  block from the same server fields, with the upcoming runs and their Open), then every live run as a card in time
 *  order — open or locked, soonest first, never a test run — and the tabs for what isn't live: Regulars · History ·
 *  Test (only once a test run exists). Design §5.56. */
function RaidCard({ r, meta, people, busy, onAct, onBoard, onReload }: { r: RaidRuns; meta: Meta; people: Person[]; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void> }) {
  const scheds = (r.schedules || []).filter((s) => s.active && s.kind !== "pickup");
  const recurring = scheds.filter((s) => s.class !== "adhoc");
  // test runs (the test bench's rehearsals and comp sandboxes) live on their own Test tab, never among the real sheets (B9)
  const runs = [...r.open, ...r.locked].filter((e) => !e.test).sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at));
  const tests = [...r.open, ...r.locked].filter((e) => e.test);
  const metaLine = [`${r.size}-player`, scheds.length ? scheds.map((s) => `${s.name}, ${s.label}`).join(" · ") : "no schedule yet — add one on the Raids page"].join(" · ");
  const target = window.location.hash.startsWith("#run-") ? window.location.hash.slice(5) : "";
  const first = tests.some((e) => e.key === target) ? "test" : r.past.some((e) => e.key === target) ? "history" : "regulars";
  const regulars = (r.series || []).reduce((n, s) => n + s.regulars.length, 0);
  const testBadge = <Tooltip label="a test run: puppets and its tester only — its cards post where it was opened"><Badge variant="light" color="grape" tt="none" leftSection={<IconFlask size={12} />}>test</Badge></Tooltip>;
  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={
        <Group gap="xs" align="flex-start" wrap="wrap" justify="flex-end">
          <StartAdhoc r={r} busy={busy} onAct={onAct} />
          {recurring.length > 0 && <OpenNext r={r} busy={busy} onAct={onAct} />}
        </Group>} />
      <Stack gap="md" p="md">
        <Box>
          <Box mb={6}><Eyebrow plain>When it runs</Eyebrow></Box>
          <WhenItRuns r={r} owner={false} here upcoming={r.upcoming.length ? <UpcomingRows r={r} busy={busy} onAct={onAct} /> : undefined}
            nothing={<>No run coming up: no schedule opens a sheet for this raid. Add one on the <Anchor component={Link} to="/raids" size="sm">Raids page</Anchor>, or start an ad hoc run here.</>} />
        </Box>
        {runs.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} people={people} />)}
        <Tabs defaultValue={first}>
          <Tabs.List>
            <Tabs.Tab value="regulars">Regulars · {regulars}</Tabs.Tab>
            <Tabs.Tab value="history">History</Tabs.Tab>
            {tests.length > 0 && <Tabs.Tab value="test">Test · {tests.length}</Tabs.Tab>}
          </Tabs.List>
          {tests.length > 0 && (
            <Tabs.Panel value="test" pt="md">
              <Stack gap="md">
                {tests.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} badge={testBadge} people={people} />)}
                {meta.viewer.officer && <Card withBorder p="sm"><TestFeed run={tests.length === 1 ? tests[0].key : undefined} height={260} /></Card>}
              </Stack>
            </Tabs.Panel>
          )}
          <Tabs.Panel value="regulars" pt="md">
            <Regulars r={r} meta={meta} people={people} onAct={onAct} busy={busy} />
          </Tabs.Panel>
          <Tabs.Panel value="history" pt="md">
            <History r={r} meta={meta} />
          </Tabs.Panel>
        </Tabs>
      </Stack>
    </Card>
  );
}
