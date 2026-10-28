import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ActionIcon, Anchor, Badge, Box, Button, Card, Collapse, Group, Menu, Stack, Tabs, Text, TextInput, Tooltip, UnstyledButton } from "@mantine/core";
import { IconCalendarOff, IconChecks, IconChevronDown, IconChevronRight, IconCrown, IconDotsVertical, IconHourglass, IconMessageOff, IconSearch } from "@tabler/icons-react";
import { useRefresh } from "../hooks/usePoll";
import { useDirty } from "../hooks/useDirty";
import { useLive } from "../hooks/useLive";
import { api, type Character, type MemberRow, type Members as MembersData, type Meta, type PeoplePreview } from "../api";
import { GameIcon } from "../components/Icons";
import { PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { TestBench } from "../components/TestBench";
import { AbsencesModal } from "../components/AbsencesModal";
import { AbsencesTable } from "../components/AbsencesTable";
import { MemberEdit } from "../components/members/MemberEdit";
import { ConfirmWalk, PickModal, type Pick, type WalkItem } from "../components/members/PeopleDialogs";
import { classColour } from "../theme";
import css from "./members.module.css";

type Filter = "away" | "confirm" | "left" | "bare" | null;

/** A character waiting for an officer's confirm: named, active, nobody confirmed it yet. */
const waits = (c: Character) => c.status === "active" && !!c.name && !c.confirmed;
const BADGE = new Set(["owner", "officer"]);

/** One ⋮ item: greyed with its reason under the label when it doesn't apply (design §5.58, as on Rosters). */
function Item({ label, why, onClick, color }: { label: string; why?: string | null; onClick?: () => void; color?: string }) {
  return (
    <Menu.Item disabled={!!why} color={why ? undefined : color} onClick={why ? undefined : onClick}>
      <Text size="sm" inherit>{label}</Text>
      {why && <Text size="xs" c="dimmed">{why}</Text>}
    </Menu.Item>
  );
}

/** The Members page (officers; design §5.58): find a person, fix one thing on their record, confirm their characters.
 *  A header of counts that are also filters, a search over member and character names and Confirm characters… (a walk).
 *  One row per member — characters as icon lines, rank as a word, a standing line only when there is one — with a
 *  member ⋮ (Rank…, Record an absence…, DMs, Edit characters, Remove record…) and a character ⋮ (Spec…, Extra roles…,
 *  Make main, Confirm, Retire…, Delete…). Every press previews through POST /api/members/set (or /dm, /save) and the
 *  confirm prints ONLY the server's sentences; the page writes none. Editing is one member in place. Not in the server,
 *  no character yet and the test bench's puppets fold at the foot. Tabs: Members · Absences · Test bench. Below the sm
 *  breakpoint every member is a card (members.module.css) and every control is 44 px. */
export function MembersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<MembersData | null>(null);
  const [tab, setTab] = useState<string | null>(window.location.hash === "#test-bench" ? "test" : "members");
  const [filter, setFilter] = useState<Filter>(null);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  useDirty("members", editing !== null);
  const editRef = useRef<string | null>(null);
  editRef.current = editing;
  const [busy, setBusy] = useState<string | null>(null);
  const [absFor, setAbsFor] = useState<MemberRow | null>(null);
  const [absKey, setAbsKey] = useState(0);
  const [pick, setPick] = useState<Pick | null>(null);
  const [walk, setWalk] = useState<{ queue: WalkItem[]; skipped: string[] } | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<MembersData>("/api/members").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useRefresh(load);
  useLive(() => { if (editRef.current === null) load(); }, ["member", "run:"]);  // never under an edit in progress
  const changed = () => { load(); setAbsKey((k) => k + 1); };

  const rows = data?.rows || [];
  const me = meta.viewer.uid;
  const own = data?.own || {};
  const counts = useMemo(() => ({
    inServer: rows.filter((r) => !r.left && !r.test).length,
    away: rows.filter((r) => r.away_now && !r.test).length,
    left: rows.filter((r) => r.left).length,
    bare: rows.filter((r) => !r.left && !r.test && !r.characters.length).length,
    absences: rows.reduce((n, r) => n + r.absences.length, 0),
  }), [rows]);

  /** One press on a ⋮: the server's preview → its sentences in the confirm → the write with the rev the preview returned. */
  async function press(r: MemberRow, body: Record<string, unknown>, title: string, confirmLabel: string, color?: string) {
    setBusy(r.uid);
    try {
      const p = await api.post<PeoplePreview>("/api/members/set", { uid: r.uid, rev: r.characters_rev, ...body, preview: true });
      if (!(await ask({ title, message: p.sentences.join("\n"), confirmLabel, color }))) return;
      const x = await api.post<{ message: string }>("/api/members/set", { uid: r.uid, rev: p.rev || r.characters_rev, ...body });
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); load(); }
  }
  async function setDm(r: MemberRow, on: boolean) {
    setBusy(r.uid);
    try {
      const p = await api.post<PeoplePreview>("/api/members/dm", { uid: r.uid, on, preview: true });
      if (!(await ask({ title: `Turn DMs ${on ? "on" : "off"} for ${r.display_name}?`, message: p.sentences.join("\n"), confirmLabel: on ? "Turn DMs on" : "Turn DMs off" }))) return;
      const x = await api.post<{ message: string }>("/api/members/dm", { uid: r.uid, on });
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); load(); }
  }
  async function removeRecord(r: MemberRow) {
    if (!r.remove_consequence) return;
    if (!(await ask({ title: `Remove ${r.display_name}'s record?`, message: r.remove_consequence, confirmLabel: "Remove record", color: "red" }))) return;
    setBusy(r.uid);
    try { const x = await api.post<{ message: string }>("/api/admin/member/remove", { uid: r.uid }); ok(x.message); } catch (e) { fail(e); } finally { setBusy(null); load(); }
  }
  function startWalk() {
    const queue: WalkItem[] = [];
    const skipped: string[] = [];
    for (const r of rows) {
      if (r.left || r.test) continue;
      for (const c of r.characters.filter(waits)) {
        if (r.uid === me && own.confirm) skipped.push(c.label); else queue.push({ r, c });
      }
    }
    setWalk({ queue, skipped });
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;

  const needle = q.trim().toLowerCase();
  const matches = (r: MemberRow) => !needle || r.display_name.toLowerCase().includes(needle) || r.characters.some((c) => c.label.toLowerCase().includes(needle));
  const pred: Record<Exclude<Filter, null>, (r: MemberRow) => boolean> = {
    away: (r) => !!r.away_now && !r.test,
    confirm: (r) => !r.left && !r.test && r.characters.some(waits),
    left: (r) => !!r.left,
    bare: (r) => !r.left && !r.test && !r.characters.length,
  };
  const flat = filter !== null || !!needle;
  const shown = flat ? rows.filter((r) => matches(r) && (!filter || pred[filter](r))) : rows.filter((r) => !r.left && !r.test && r.characters.length);
  const folds = flat ? [] : [
    { key: "left", title: `Not in the Discord server · ${counts.left}`, hint: "their records stay until an officer removes them: Remove record… is on each one's ⋮", rows: rows.filter((r) => r.left) },
    { key: "bare", title: `No character yet · ${counts.bare}`, hint: "add one from their ⋮, or they register with /register; absences and DMs still apply", rows: rows.filter(pred.bare) },
    { key: "test", title: `Test bench puppets · ${rows.filter((r) => r.test).length}`, hint: "they answer through the test bench; seed and clear them on the Test bench tab", rows: rows.filter((r) => r.test) },
  ].filter((f) => f.rows.length);
  const standing = (r: MemberRow) => !!(r.away || r.asks.length || r.test || r.dm === false);
  const withStanding = [...shown, ...folds.flatMap((f) => f.rows)].some(standing);
  const cols = `minmax(150px, 1.1fr) minmax(0, 2.4fr) 76px ${withStanding ? "minmax(0, 1.6fr) " : ""}44px`;

  const memberMenu = (r: MemberRow) => (
    <Menu shadow="md" width={260} position="bottom-end" classNames={{ item: css.menuItem }}>
      <Menu.Target><ActionIcon variant="subtle" color="gray" loading={busy === r.uid} aria-label={`${r.display_name}'s record`}><IconDotsVertical size={17} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{r.display_name}</Menu.Label>
        <Item label="Rank…" why={r.uid === me && own.rank ? own.rank : !r.characters.length ? "rank lives on the main: add a character first" : null} onClick={() => setPick({ kind: "rank", r })} />
        <Item label="Record an absence…" onClick={() => setAbsFor(r)} />
        <Item label={`Turn DMs ${r.dm === false ? "on" : "off"} for ${r.display_name}…`} why={r.test ? "a puppet answers through the test bench" : null} onClick={() => setDm(r, r.dm === false)} />
        <Item label="Edit characters" why={editing && editing !== r.uid ? "save or cancel the member you're editing first" : null} onClick={() => setEditing(r.uid)} />
        <Menu.Divider />
        <Item label="Remove record…" color="red" why={r.left ? null : "only once they leave the Discord server"} onClick={() => removeRecord(r)} />
      </Menu.Dropdown>
    </Menu>
  );

  const charMenu = (r: MemberRow, c: Character) => (
    <Menu shadow="md" width={260} position="bottom-end" classNames={{ item: css.menuItem }}>
      <Menu.Target><ActionIcon variant="subtle" color="gray" size="sm" aria-label={`${c.label} the character`}><IconDotsVertical size={15} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{c.label}</Menu.Label>
        <Item label="Spec…" onClick={() => setPick({ kind: "spec", r, c })} />
        <Item label="Extra roles…" onClick={() => setPick({ kind: "flex", r, c })} />
        <Item label="Make main" why={c.is_main ? "already the main" : null} onClick={() => press(r, { character: c.label, main: true }, `Make ${c.label} ${r.display_name}'s main?`, "Make it the main")} />
        <Item label="Confirm" why={c.confirmed ? "already confirmed" : !c.name ? "a planned character is confirmed once it has a name" : r.uid === me && own.confirm ? own.confirm : null}
          onClick={() => press(r, { character: c.label, confirm: true }, `Confirm ${c.label}?`, "Confirm")} />
        <Menu.Divider />
        <Item label="Retire…" onClick={() => press(r, { character: c.label, retire: true }, `Retire ${c.label}?`, "Retire")} />
        <Item label="Delete…" color="red" onClick={() => press(r, { character: c.label, delete: true }, `Delete ${c.label}?`, "Delete", "red")} />
      </Menu.Dropdown>
    </Menu>
  );

  const charLine = (r: MemberRow, c: Character) => (
    <Box key={c.label}>
      <div className={css.charLine}>
        <GameIcon meta={meta} kind="class" id={c.cls} size={24} title={c.cls} />
        <Text fw={600} c={classColour(meta, c.cls)} className={css.charName} title={c.label}>{c.label}</Text>
        {c.is_main && <Tooltip label="main"><IconCrown size={16} color="var(--mantine-color-gray-3)" aria-label="main" /></Tooltip>}
        <GameIcon meta={meta} kind="spec" id={`${c.cls}:${c.spec}`} size={22} title={`${c.spec} · ${c.role}`} />
        {c.offspec && <GameIcon meta={meta} kind="spec" id={`${c.cls}:${c.offspec}`} size={22} dim title={`offspec ${c.offspec} · ${c.off_role || ""}`} />}
        {(c.flex || []).map((x) => <span key={x} className={css.flexIcon}><GameIcon meta={meta} kind="role" id={x} size={18} title={`could also play ${x}`} /></span>)}
        {waits(c) && <Group gap={6} wrap="nowrap"><span className={css.unconfirmed} /><Text size="xs" c="dimmed">waits for a confirm</Text></Group>}
        {!c.name && <Text size="xs" c="dimmed">planned, no name yet</Text>}
        {charMenu(r, c)}
      </div>
      {!!c.regular_on?.length && <Anchor component={Link} to="/rosters" size="xs" c="dimmed" ml={32}>regular of {c.regular_on.join(", ")}</Anchor>}
    </Box>
  );

  const row = (r: MemberRow) => {
    if (editing === r.uid) {
      return <div key={r.uid} className={css.row}><MemberEdit meta={meta} r={r} ask={ask} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} /></div>;
    }
    return (
      <div key={r.uid} className={css.row}>
        <Box className={css.name}>
          <Group gap={6} wrap="wrap" style={{ minWidth: 0 }}>
            <Text fw={700} truncate title={r.display_name} style={{ minWidth: 0 }}>{r.display_name}</Text>
            {BADGE.has(r.privilege) && <Badge size="sm" variant="outline" color="teal" tt="none">{r.privilege}</Badge>}
            {r.test && <Badge size="sm" variant="outline" color="grape" tt="none">puppet</Badge>}
            {r.rank && r.characters.length > 0 && <Text size="sm" c="dimmed" className={css.rankInline}>· {r.rank}</Text>}
          </Group>
          {r.left_line && <Text size="xs" c="dimmed">{r.left_line}</Text>}
          {r.attendance && !r.left && <Text size="xs" c="dimmed">attended {r.attendance}</Text>}
          {r.left && r.remove_consequence && <Button size="compact-sm" variant="subtle" color="red" mt={4} px={0} onClick={() => removeRecord(r)}>Remove record…</Button>}
        </Box>
        <Stack gap={2} className={css.chars}>
          {r.characters.map((c) => charLine(r, c))}
          {!r.characters.length && <Text size="sm" c="dimmed">No character yet. Add one with Edit characters, or they register with /register.</Text>}
        </Stack>
        <Text size="sm" className={css.rank}>{r.characters.length ? r.rank || "" : ""}</Text>
        {withStanding && (
          <Stack gap={4} className={css.standing}>
            {r.away && <Group gap={6} wrap="nowrap"><IconCalendarOff size={15} color="var(--mantine-color-gray-5)" /><Text size="sm" c="dimmed">{r.away}</Text></Group>}
            {r.asks.map((a) => (
              <Tooltip key={a.roster} label={a.how_words}>
                <Group gap={6} wrap="nowrap" align="flex-start">
                  <IconHourglass size={15} style={{ flexShrink: 0, marginTop: 3 }} />
                  <Text size="sm">{a.key ? <Anchor component={Link} to={`/rosters#run-${a.key}`} size="sm">{a.words}</Anchor> : a.words}{a.line.slice(a.words.length)}</Text>
                </Group>
              </Tooltip>
            ))}
            {r.dm === false && !r.test && <Group gap={6} wrap="nowrap"><IconMessageOff size={15} color="var(--mantine-color-gray-5)" /><Text size="sm" c="dimmed">DMs off, so asks wait on their Me page</Text></Group>}
            {r.test && <Text size="sm" c="dimmed">answers through the test bench</Text>}
          </Stack>
        )}
        <Box className={css.menu}>{memberMenu(r)}</Box>
      </div>
    );
  };

  const chip = (f: Filter, n: number, words: string) => (
    <Button key={words} size="xs" radius="xl" variant={filter === f ? "light" : "default"} tt="none" aria-pressed={filter === f}
      onClick={() => setFilter(filter === f ? null : f)}><b>{n}</b>&nbsp;{words}</Button>
  );

  return (
    <Stack gap="lg" className={css.touch}>  {/* phone: every control 44 px (members.module.css) */}
      <PageTitle title="Members" intro="Everyone with a record: find a person, fix one thing on their record, confirm their characters." />
      <Group gap="xs" wrap="wrap">
        {chip(null, counts.inServer, "in the server")}
        {counts.away > 0 && chip("away", counts.away, "away now")}
        {(data.pending || 0) > 0 && chip("confirm", data.pending || 0, "characters wait for a confirm")}
        {counts.left > 0 && chip("left", counts.left, "not in the server")}
        {counts.bare > 0 && chip("bare", counts.bare, "no character yet")}
        <Box style={{ flex: "1 1 0" }} />
        <TextInput w={{ base: "100%", sm: 260 }} type="search" leftSection={<IconSearch size={15} />} placeholder="Find a member or a character" aria-label="Find a member or a character"
          value={q} onChange={(e) => setQ(e.currentTarget.value)} />
        {(data.pending || 0) > 0 && <Button variant="default" leftSection={<IconChecks size={15} />} onClick={startWalk}>Confirm characters…</Button>}
      </Group>
      <Tabs value={tab} onChange={(v) => { setTab(v); window.history.replaceState(null, "", v === "test" ? "#test-bench" : window.location.pathname + window.location.search); }}>
        <Tabs.List>
          <Tabs.Tab value="members">Members · {counts.inServer}</Tabs.Tab>
          <Tabs.Tab value="absences">{counts.absences ? `Absences · ${counts.absences}` : "Absences"}</Tabs.Tab>
          <Tabs.Tab value="test">Test bench</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="members" pt="md">
          <Card p={0}>
            <div className={css.list} style={{ ["--cols" as string]: cols }}>
              <div className={css.head}>
                <Text size="xs" c="dimmed">Member</Text><Text size="xs" c="dimmed">Characters</Text><Text size="xs" c="dimmed">Rank</Text>
                {withStanding && <Text size="xs" c="dimmed">Standing</Text>}<span />
              </div>
              {shown.map(row)}
              {flat && shown.length === 0 && (
                <Group p="md" gap="sm"><Text size="sm" c="dimmed">Nobody here matches. Clear the search or the filter to see everyone.</Text>
                  <Button size="xs" variant="default" onClick={() => { setQ(""); setFilter(null); }}>Show everyone</Button></Group>
              )}
              {folds.map((f) => (
                <Box key={f.key}>
                  <UnstyledButton className={css.fold} aria-expanded={!!open[f.key]} onClick={() => setOpen({ ...open, [f.key]: !open[f.key] })}>
                    {open[f.key] ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
                    <Text fw={600} size="sm">{f.title}</Text>
                    <Text size="xs" c="dimmed">{f.hint}</Text>
                  </UnstyledButton>
                  <Collapse expanded={!!open[f.key]}>{f.rows.map(row)}</Collapse>
                </Box>
              ))}
            </div>
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="absences" pt="md"><AbsencesTable reloadKey={absKey} /></Tabs.Panel>
        <Tabs.Panel value="test" pt="md"><TestBench meta={meta} rows={rows} onChanged={load} /></Tabs.Panel>
      </Tabs>
      <AbsencesModal meta={meta} opened={!!absFor} who={absFor ? { uid: absFor.uid, display_name: absFor.display_name } : null} onClose={() => setAbsFor(null)} onSaved={() => { setAbsFor(null); changed(); }} />
      <PickModal meta={meta} pick={pick} rankChoices={data.rank_choices || []} onClose={() => setPick(null)} onDone={load} />
      <ConfirmWalk meta={meta} queue={walk?.queue || []} skipped={walk?.skipped || []} skipReason={own.confirm} opened={walk !== null} onClose={() => { setWalk(null); load(); }} />
      {confirmDialog}
    </Stack>
  );
}
