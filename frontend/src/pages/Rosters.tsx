import { useEffect, useState } from "react";
import { DateTimePicker } from "@mantine/dates";
import { ActionIcon, Badge, Box, Button, Card, Group, Select, Stack, Tabs, Text, Tooltip } from "@mantine/core";
import { IconSparkles, IconUserPlus, IconX } from "@tabler/icons-react";
import { api, type Board, type Meta, type Person, type RaidRuns, type Rosters, type Series, type Sheet } from "../api";
import { GameIcon } from "../components/Icons";
import { useConfirm } from "../components/ConfirmModal";
import { classColour } from "../theme";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { SheetCard } from "../components/board/SheetCard";
import { RoleCounts, runName, type Act, type OnBoard } from "../components/board/shared";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

export function RostersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Rosters | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = (): Promise<void> => api.get<Rosters>("/api/rosters").then(setData).catch((e) => { fail(e); });
  useEffect(() => { load(); }, []);
  usePoll(load);
  useLive(load, ["run:", "member"]);  // a save anywhere (another officer, Discord, the scheduler) refreshes this page within a second
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

/** The picker hands back "YYYY-MM-DD HH:mm:ss"; show it the way every other time on the site is shown. */
function fmt12(v: string): string {
  const d = new Date(v.replace(" ", "T"));
  return isNaN(d.getTime()) ? v : d.toLocaleString(undefined, { weekday: "short", day: "2-digit", month: "short", hour: "numeric", minute: "2-digit", hour12: true });
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
 *  timestamp. Ad hoc: first come, no regulars, no reminder, no confirmations by default. */
function StartAdhoc({ r, busy, onAct }: { r: RaidRuns; busy: string | null; onAct: Act }) {
  const [tpl, setTpl] = useState<string>("adhoc");  // "adhoc" = a one-off
  const [when, setWhen] = useState<string | null>(null);
  const templates = (r.schedules || []).filter((s) => s.active && (s.class === "adhoc" || s.kind === "pickup"));
  const picked = (when || "").trim();
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
      <Tooltip label={picked ? `Starts ${fmt12(picked)} — first come, first rostered` : "Pick when it starts"} withinPortal>
        <Button size="sm" variant="default" loading={busy === `adhoc${r.id}`} disabled={!picked}
          onClick={() => onAct(`adhoc${r.id}`, `/api/raid/${r.id}/open`, { when: picked, schedule: tpl }).then(() => setWhen(null))}>Start ad hoc run</Button>
      </Tooltip>
    </Group>
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
  const metaLine = [scheds.length ? scheds.map((s) => (scheds.length > 1 ? `${s.name}: ${s.label}` : s.label)).join(" · ") : "no schedule set", r.opened ? null : r.first_open ? `opens ${r.first_open}` : "no opening date", `${r.open.length} open · ${r.locked.length} locked`].filter(Boolean).join(" · ");
  const target = window.location.hash.startsWith("#run-") ? window.location.hash.slice(5) : "";
  const first = r.locked.some((e) => e.key === target) ? "locked" : r.open.length ? "open" : r.locked.length ? "locked" : "upcoming";
  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={<Stack gap={6} align="flex-end"><OpenNext r={r} busy={busy} onAct={onAct} /><StartAdhoc r={r} busy={busy} onAct={onAct} /></Stack>} />
      <Tabs defaultValue={first} px="md" pb="md">
        <Tabs.List>
          <Tabs.Tab value="open" rightSection={r.open.length ? <Badge size="xs" color="teal" variant="light">{r.open.length}</Badge> : null}>Open for signup</Tabs.Tab>
          <Tabs.Tab value="locked" rightSection={r.locked.length ? <Badge size="xs" color="yellow" variant="light">{r.locked.length}</Badge> : null}>Locked</Tabs.Tab>
          <Tabs.Tab value="upcoming">Upcoming</Tabs.Tab>
          <Tabs.Tab value="regulars" rightSection={(r.series || []).length ? <Badge size="xs" color="gray" variant="light">{(r.series || []).reduce((n, s) => n + s.regulars.length, 0)}</Badge> : null}>Regulars</Tabs.Tab>
          <Tabs.Tab value="history">History</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="open" pt="md">
          {r.open.length === 0 && <Text size="sm" c="dimmed">No sheet open. The next slot opens on its own {r.upcoming[0] ? `${r.upcoming[0].opens}` : "once a schedule is set on Raids"}, or open one now.</Text>}
          <Stack gap="md">{r.open.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="locked" pt="md">
          {r.locked.length === 0 && <Text size="sm" c="dimmed">Nothing locked.</Text>}
          <Stack gap="md">{r.locked.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="upcoming" pt="md">
          {r.upcoming.length === 0 && <Text size="sm" c="dimmed">No upcoming runs{scheds.length ? " before the raid opens" : " — add a schedule on Raids"}.</Text>}
          <Stack gap={4}>{r.upcoming.map((u) => <Text key={u.start} size="sm"><b>{u.start}</b> <Text span c="dimmed">· {u.slot} · sheet opens {u.opens}</Text></Text>)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="regulars" pt="md">
          <Regulars r={r} meta={meta} people={people} onAct={onAct} busy={busy} />
        </Tabs.Panel>
        <Tabs.Panel value="history" pt="md">
          {r.past.length === 0 && <Text size="sm" c="dimmed">No past runs.</Text>}
          <Stack gap={4}>{r.past.map((e) => <Text key={e.key} size="sm"><Badge size="xs" variant="outline" color={e.state === "done" ? "teal" : "gray"} mr={6}>{e.state}</Badge>{e.when} · {e.counts.in} joined · {e.rostered} rostered{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>)}</Stack>
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}
