import { useEffect, useState } from "react";
import { DateTimePicker } from "@mantine/dates";
import { ActionIcon, Badge, Box, Button, Card, Drawer, Group, Select, Stack, Tabs, Text, Tooltip } from "@mantine/core";
import { IconFlask, IconSparkles, IconUserPlus, IconX } from "@tabler/icons-react";
import { api, type Board, type Meta, type Person, type RaidRuns, type Rosters, type Series, type Sheet } from "../api";
import { GameIcon } from "../components/Icons";
import { useConfirm } from "../components/ConfirmModal";
import { classColour } from "../theme";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { BoardPreview } from "../components/board/BoardPreview";
import { Detail, SheetCard } from "../components/board/SheetCard";
import { TestFeed } from "../components/TestFeed";
import type { NightSheet, OpenPreview, RunsPage, UpcomingRow } from "../components/board/types";
import { RoleCounts, runName, type Act, type OnBoard } from "../components/board/shared";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

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
  return (
    <Stack gap="lg">
      <PageTitle title="Rosters" intro={`Recurring schedules open a sheet per run on their own and have regulars — the people expected each week (nothing is answered for them; you see who hasn't answered). Ad hoc runs are opened here at a time you pick: first come, first rostered. Shape the roster on the board before it locks; after lock, the board is the roster. Times are ${data.tz}; the page refreshes itself while open.`} />
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} people={data.people || []} busy={busy} onAct={act} onBoard={patchBoard} onReload={load} />)}
      {data.orphans.length > 0 && (
        <Card><Box p="md"><Eyebrow>Sheets not tied to a raid</Eyebrow>{data.orphans.map((e) => <Text key={e.key} size="sm">{runName(e)} · {e.state}</Text>)}</Box></Card>
      )}
    </Stack>
  );
}

/** Open the next run of a recurring schedule: one press (a schedule select when the raid has several). */
function OpenNext({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [sched, setSched] = useState<string>("");  // "" = the next run of any recurring schedule
  const recurring = (r.schedules || []).filter((s) => s.active && s.class !== "adhoc" && s.kind !== "pickup");
  const next = r.upcoming.find((u) => !sched || u.schedule === sched);
  return (
    <Group gap="xs" align="center" wrap="wrap">
      {recurring.length > 1 && (
        <Select size="sm" w={180} allowDeselect={false} aria-label="which recurring schedule" value={sched} onChange={(v) => setSched(v || "")}
          data={[{ value: "", label: "Any schedule" }, ...recurring.map((s) => ({ value: s.id, label: s.name }))]} />
      )}
      <Tooltip label={next ? `The next run: ${next.start}` : "No run coming up — add run times on the Raids page"} withinPortal>
        <Button size="sm" leftSection={<IconSparkles size={15} />} loading={busy === `open${r.id}`} disabled={!next}
          onClick={() => onAct(`open${r.id}`, `/api/raid/${r.id}/open`, { schedule: sched })}>Open next run</Button>
      </Tooltip>
    </Group>
  );
}

/** Start an ad hoc run: a pickup template (its own settings) or a one-off, at a date and time picked — nobody types a
 *  timestamp. Every change asks the server what Open would do (POST /api/raid/{rid}/open/preview, rc.open_preview): a
 *  time that IS an upcoming recurring run relabels the button "Open the … run — scheduled, with confirmations", and
 *  the confirm shows both clocks (guild time and yours), the run's rules, the DST note and where the sheet posts. Open
 *  carries the preview's exact instant and key back, so a stale match is refused rather than opening another run. */
function StartAdhoc({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [tpl, setTpl] = useState<string>("adhoc");  // "adhoc" = a one-off
  const [when, setWhen] = useState<string | null>(null);
  const [p, setP] = useState<OpenPreview | null>(null);
  const [ask, dialog] = useConfirm();
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
  const label = p && p.kind === "scheduled" ? `Open the ${p.schedule_name} run — scheduled, with confirmations` : "Start ad hoc run";
  async function start() {
    if (!p) return;
    const yours = new Date(p.start).toLocaleString(undefined, { weekday: "short", day: "2-digit", month: "short", hour: "numeric", minute: "2-digit", hour12: true });
    const message = [`${p.label} guild time · ${yours} your time`, p.rules, p.when_note, p.exists ? `A run for that time already exists (${p.exists}); Open points you at it instead of making a second.` : "", p.refusal || "",
      p.channel ? `The sheet posts in #${p.channel_name || "the signup channel"}.` : "No signup channel is set: the sheet isn't posted until one is (/raid sheet)."].filter(Boolean).join("\n");
    if (!(await ask({ title: p.kind === "scheduled" ? `Open the ${p.schedule_name} run?` : `Start an ad hoc ${r.name}?`, message, confirmLabel: p.kind === "scheduled" ? "Open" : "Start" }))) return;
    await onAct(`adhoc${r.id}`, `/api/raid/${r.id}/open`, { start: p.start, key: p.key,
      // a time that IS a scheduled run opens as that schedule's run (null = the raid's own run times → "default");
      // sending "adhoc" there would resolve to a one-off key and the server would refuse the stale preview (409)
      schedule: p.kind === "scheduled" ? (p.schedule || "default") : (p.schedule || (tpl === "adhoc" ? "adhoc" : tpl)) });
    setWhen(null);
  }
  return (
    <Group gap="xs" align="center" wrap="wrap">
      {templates.length > 0 && (
        <Select size="sm" w={180} allowDeselect={false} aria-label="template" value={tpl} onChange={(v) => setTpl(v || "adhoc")}
          data={[{ value: "adhoc", label: "One-off" }, ...templates.map((s) => ({ value: s.id, label: s.name }))]} />
      )}
      <DateTimePicker
        size="sm" w={230} clearable value={when} onChange={setWhen} placeholder="pick a date and time"
        minDate={new Date().toISOString().slice(0, 10)}
        valueFormat="ddd DD MMM h:mm A" timePickerProps={{ format: "12h", withDropdown: true, popoverProps: { withinPortal: false } }}
        popoverProps={{ withinPortal: true }} aria-label="when the ad hoc run starts"
      />
      <Tooltip label={p ? (p.refusal || `${p.label} — ${p.rules}`) : picked ? "Checking that time…" : "Pick when it starts"} withinPortal multiline w={320}>
        <Button size="sm" variant="default" loading={busy === `adhoc${r.id}`} disabled={!p || p.past || !!p.closed} onClick={start}>{label}</Button>
      </Tooltip>
      {dialog}
    </Group>
  );
}

/** The Upcoming tab: each run of a recurring schedule with its own Open (design B4) — one press, then the confirm with the
 *  server's preview; a row the scheduler can't open (no signup channel set) says so in red. */
function UpcomingRows({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [ask, dialog] = useConfirm();
  const rows = r.upcoming as UpcomingRow[];
  async function open(u: UpcomingRow) {
    let p: OpenPreview;
    try { p = await api.post<OpenPreview>(`/api/raid/${r.id}/open/preview`, { start: u.start_iso, schedule: u.schedule }); } catch (err) { fail(err); return; }
    const yours = new Date(p.start).toLocaleString(undefined, { weekday: "short", day: "2-digit", month: "short", hour: "numeric", minute: "2-digit", hour12: true });
    if (p.refusal) { fail(new Error(p.refusal)); return; }
    const message = [`${p.label} guild time · ${yours} your time`, p.rules, p.channel ? `The sheet posts in #${p.channel_name || "the signup channel"}.` : "No signup channel is set: the sheet isn't posted until one is (/raid sheet)."].join("\n");
    if (!(await ask({ title: `Open ${r.name} · ${u.slot}?`, message, confirmLabel: "Open" }))) return;
    await onAct(`up${u.key || u.start}`, `/api/raid/${r.id}/open`, { start: p.start, schedule: u.schedule, key: p.key });
  }
  return (
    <Stack gap={4}>
      {rows.map((u) => (
        <Group key={u.key || u.start} gap="sm" wrap="wrap">
          <Text size="sm"><b>{u.start}</b> <Text span c="dimmed">· {u.slot} · sheet opens {u.opens}</Text></Text>
          {u.needs_channel && <Text size="xs" c="red">needs a signup channel</Text>}
          {u.start_iso && <Button size="compact-xs" variant="light" loading={busy === `up${u.key || u.start}`} onClick={() => open(u)}>Open</Button>}
        </Group>
      ))}
      {dialog}
    </Stack>
  );
}

/** History: the newest runs first, paged (GET /api/runs beyond the first 8), each row opening a read-only drawer with
 *  the run in full (GET /api/run/{key}); `#run-<key>` deep links open it. */
function History({ r, meta }: { r: RaidRuns; meta: Meta }) {
  const [more, setMore] = useState<Sheet[]>([]);
  const [total, setTotal] = useState<number>((r as RaidRuns & { past_total?: number }).past_total ?? r.past.length);
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
      {rows.length === 0 && <Text size="sm" c="dimmed">No past runs.</Text>}
      {rows.map((e) => (
        <Text key={e.key} size="sm" style={{ cursor: "pointer" }} onClick={() => { setOpenKey(e.key); window.history.replaceState(null, "", `#run-${e.key}`); }}>
          <Badge size="xs" variant="outline" color={e.state === "done" ? "teal" : "gray"} mr={6}>{e.state}</Badge>{e.when} · {e.counts.in} joined · {e.rostered} rostered{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}
        </Text>
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
          <Group gap="sm"><Badge variant="outline" color={e.state === "done" ? "teal" : "gray"}>{e.state}</Badge>{e.closed_at && <Text size="xs" c="dimmed">closed {e.closed_at}</Text>}</Group>
          <Text size="sm">{e.counts.in} joined · {e.rostered} rostered{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>
          {e.board && e.rostered > 0 && <BoardPreview meta={meta} board={e.board} />}
          {e.may_reopen && <Button size="xs" variant="light" loading={busy} onClick={reopen} w="fit-content">Reopen — ended by mistake</Button>}
          {!e.may_reopen && e.reopen_note && e.state === "done" && <Text size="xs" c="dimmed">{e.reopen_note}</Text>}
          <Detail e={e} readOnly />
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
  const levelColour = { green: "teal", amber: "yellow", red: "red" }[level];
  const spare = s.no_spare || [];
  const levelWords = { green: "regular mains cover the tank and healer minimums, with a spare", amber: spare.length ? `no spare ${spare.join(" or ")} among regular mains — one absence leaves it short` : "covered only with offspecs, flex roles or alts", red: "regulars can't field the minimum" }[level];
  async function remove(uid: string, name: string, character: string) {
    if (!(await ask({ title: `Take ${name} off the regulars?`, message: `${character} stops being expected on ${r.name} · ${s.name}. They can still Join any sheet.`, confirmLabel: "Remove", color: "red" }))) return;
    await onAct(`reg${s.key}${uid}`, "/api/admin/raid/regulars", { raid: r.id, schedule: s.id, uid, on: false });
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
          <Text fw={700} size="sm">{s.name}{!s.active && <Badge ml={6} size="xs" color="gray" variant="light">paused</Badge>}</Text>
          <Text size="xs" c="dimmed">{s.label} · {s.regulars.length} regular{s.regulars.length === 1 ? "" : "s"} for {s.seats} seats</Text>
        </Box>
        <Tooltip label={levelWords}>
          <Box style={{ borderBottom: `2px solid var(--mantine-color-${levelColour}-5)` }}><RoleCounts meta={meta} counts={s.counts} need={s.need} size={16} /></Box>
        </Tooltip>
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
            <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`remove ${x.character}`} loading={busy === `reg${s.key}${x.uid}`} onClick={() => remove(x.uid, x.display_name, x.character)}><IconX size={13} /></ActionIcon>
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
        <Button size="xs" variant="default" leftSection={<IconUserPlus size={13} />} disabled={!person} loading={busy === `reg${s.key}add`} onClick={add}>Add</Button>
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
      <Eyebrow>Next 4 weeks</Eyebrow>
      <Stack gap={0}>
        {rows.map((f) => (
          <Tooltip key={f.start} label={f.away.length ? `away: ${f.away.join(", ")}` : `${f.expected} regulars expected`} withinPortal>
            <Group gap={8} wrap="nowrap" style={{ lineHeight: 1.7 }}>
              <Text size="xs" w={78} style={{ fontVariantNumeric: "tabular-nums" }}>{f.day}</Text>
              {Object.keys(f.short).length === 0 ? <Text size="xs" c="teal">✓</Text> : Object.entries(f.short).map(([role, k]) => (
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
              <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`clear ${o.character}`} onClick={() => onAct(`orph${o.key}${o.uid}`, "/api/admin/raid/regulars", { raid: r.id, schedule: o.key.split(":")[1], uid: o.uid, on: false })}><IconX size={13} /></ActionIcon>
            </Group>
          ))}
        </Box>
      )}
    </Stack>
  );
}

function RaidCard({ r, meta, people, busy, onAct, onBoard, onReload }: { r: RaidRuns; meta: Meta; people: Person[]; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void> }) {
  const scheds = (r.schedules || []).filter((s) => s.active && s.kind !== "pickup");
  // test runs (the test bench's rehearsals and comp sandboxes) live on their own Test tab, never among the real sheets (B9)
  const open = r.open.filter((e) => !e.test), locked = r.locked.filter((e) => !e.test);
  const tests = [...r.open, ...r.locked].filter((e) => e.test);
  const metaLine = [scheds.length ? scheds.map((s) => (scheds.length > 1 ? `${s.name}: ${s.label}` : s.label)).join(" · ") : "no schedule set", r.opened ? null : r.first_open ? `opens ${r.first_open}` : "no opening date", `${open.length} open · ${locked.length} locked`].filter(Boolean).join(" · ");
  const target = window.location.hash.startsWith("#run-") ? window.location.hash.slice(5) : "";
  const first = tests.some((e) => e.key === target) ? "test" : locked.some((e) => e.key === target) ? "locked" : r.past.some((e) => e.key === target) ? "history" : open.length ? "open" : locked.length ? "locked" : "upcoming";
  const testBadge = <Tooltip label="a test run: puppets and its tester only — its cards post where it was opened"><Badge variant="light" color="grape" leftSection={<IconFlask size={12} />}>test</Badge></Tooltip>;
  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={<Stack gap={6} align="flex-end"><OpenNext r={r} busy={busy} onAct={onAct} /><StartAdhoc r={r} busy={busy} onAct={onAct} /></Stack>} />
      <Tabs defaultValue={first} px="md" pb="md">
        <Tabs.List>
          <Tabs.Tab value="open" rightSection={open.length ? <Badge size="xs" color="teal" variant="light">{open.length}</Badge> : null}>Open for signup</Tabs.Tab>
          <Tabs.Tab value="locked" rightSection={locked.length ? <Badge size="xs" color="yellow" variant="light">{locked.length}</Badge> : null}>Locked</Tabs.Tab>
          <Tabs.Tab value="upcoming">Upcoming</Tabs.Tab>
          <Tabs.Tab value="regulars" rightSection={(r.series || []).length ? <Badge size="xs" color="gray" variant="light">{(r.series || []).reduce((n, s) => n + s.regulars.length, 0)}</Badge> : null}>Regulars</Tabs.Tab>
          <Tabs.Tab value="history">History</Tabs.Tab>
          {tests.length > 0 && <Tabs.Tab value="test" rightSection={<Badge size="xs" color="grape" variant="light">{tests.length}</Badge>}>Test</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="open" pt="md">
          {open.length === 0 && <Text size="sm" c="dimmed">No sheet open. The next slot opens on its own {r.upcoming[0] ? `${r.upcoming[0].opens}` : "once a schedule is set on Raids"}, or open one now.</Text>}
          <Stack gap="md">{open.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="locked" pt="md">
          {locked.length === 0 && <Text size="sm" c="dimmed">Nothing locked.</Text>}
          <Stack gap="md">{locked.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        {tests.length > 0 && (
          <Tabs.Panel value="test" pt="md">
            <Stack gap="md">
              {tests.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} badge={testBadge} />)}
              {meta.viewer.officer && <Card withBorder p="sm"><TestFeed run={tests.length === 1 ? tests[0].key : undefined} height={260} /></Card>}
            </Stack>
          </Tabs.Panel>
        )}
        <Tabs.Panel value="upcoming" pt="md">
          {r.upcoming.length === 0 && <Text size="sm" c="dimmed">No upcoming runs{scheds.length ? " before the raid opens" : " — add a schedule on Raids"}.</Text>}
          <UpcomingRows r={r} busy={busy} onAct={onAct} />
        </Tabs.Panel>
        <Tabs.Panel value="regulars" pt="md">
          <Regulars r={r} meta={meta} people={people} onAct={onAct} busy={busy} />
        </Tabs.Panel>
        <Tabs.Panel value="history" pt="md">
          <History r={r} meta={meta} />
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}
