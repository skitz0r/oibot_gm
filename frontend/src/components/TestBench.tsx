import { useEffect, useMemo, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Alert, Box, Button, Card, Group, SegmentedControl, Select, Stack, Switch, Text, Tooltip } from "@mantine/core";
import { IconFlask, IconMinus, IconPlus, IconTrash } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { api, type MemberRow, type Meta } from "../api";
import { GameIcon } from "./Icons";
import { CardHeader, Eyebrow, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { TestFeed } from "./TestFeed";
import { useLive } from "../hooks/useLive";

type Key = string;  // "Class:Spec"
interface TestRun { key: string; raid: string; title: string; state: string; when: string; joined: number; rostered: number; posted: boolean; asks: number }
interface Tempo { id: string; label: string; start_in: number; nudge_in: number; lock_in: number; confirm_in: number }
export interface TestState {
  members: number; max: number; roster: number; seed: { count: number; label: string }[]; runs: TestRun[];
  signup_channel: { set: boolean; name: string | null }; open_refusal: string | null; tempos: Tempo[]; default_tempo: string; clear_consequence: string[];
}
const COUNTS = ["0", "1", "2", "3", "5", "8", "10", "15", "20"];  // preset counts: nobody types a number
type Reply = { message: string; key?: string };

/** The test bench (officers): both halves of /gm test on the site (dual interfaces phase 2, B9). Puppets by composition —
 *  each spec is its icon and a count stepped up or down, one Save adds or removes the difference — or Seed 5/10/20/all;
 *  Open a test run (raid, tempo, DM-on-open; refused without a signup channel, named); Answer as puppets (a random mix,
 *  or one puppet → status; once locked, their Confirm / Can't asks); Try a comp (the sandbox run's board); and the live
 *  feed of every card, puppet DM and step. Every write is the /api/test/* route the Discord wizards' verbs share. */
export function TestBench({ meta, rows, onChanged }: { meta: Meta; rows: MemberRow[]; onChanged: () => Promise<unknown> }) {
  const puppets = rows.filter((r) => r.privilege === "test");
  const now = useMemo(() => {
    const out: Record<Key, string[]> = {};  // spec → puppet uids, newest last (removals take the newest)
    for (const r of [...puppets].sort((a, b) => (BigInt(a.uid) < BigInt(b.uid) ? -1 : 1))) {
      const c = r.characters.find((x) => x.is_main) || r.characters[0];
      if (c) (out[`${c.cls}:${c.spec}`] ||= []).push(r.uid);
    }
    return out;
  }, [rows]);  // eslint-disable-line react-hooks/exhaustive-deps
  const [state, setState] = useState<TestState | null>(null);
  const loadState = () => api.get<TestState>("/api/test").then(setState).catch(() => setState(null));
  useEffect(() => { loadState(); }, [rows]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(loadState, ["test:", "run:", "config"]);
  useEffect(() => {  // Config's Test bench card links here: /members#test-bench
    if (window.location.hash !== "#test-bench") return;
    const t = setTimeout(() => document.getElementById("test-bench")?.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
    return () => clearTimeout(t);
  }, []);
  const MAX = state?.max ?? 80;  // Registry.TEST_MAX, from the server
  const [draft, setDraft] = useState<Record<Key, number> | null>(null);
  useDirty("test-bench", draft !== null);
  const [cls, setCls] = useState<string | null>(null);
  const [spec, setSpec] = useState<string | null>(null);
  const [raid, setRaid] = useState<string | null>(() => (meta.raids.find((r) => r.size === 20) || meta.raids[0])?.id ?? null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, confirmDialog] = useConfirm();
  const navigate = useNavigate();

  const counts: Record<Key, number> = draft ?? Object.fromEntries(Object.entries(now).map(([k, v]) => [k, v.length]));
  const keys = Object.keys(counts).sort((a, b) => roleOrder(meta, a) - roleOrder(meta, b) || a.localeCompare(b));
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const byRole = meta.roles.map((role) => [role, keys.filter((k) => roleOf(meta, k) === role).reduce((a, k) => a + counts[k], 0)] as const);
  const add = keys.flatMap((k) => (counts[k] > (now[k]?.length ?? 0) ? [{ cls: k.split(":")[0], spec: k.split(":")[1], count: counts[k] - (now[k]?.length ?? 0) }] : []));
  const remove = keys.flatMap((k) => (now[k] ?? []).slice(counts[k]));
  const dirty = add.length > 0 || remove.length > 0;

  const step = (k: Key, by: number) => setDraft({ ...counts, [k]: Math.max(0, Math.min(counts[k] + by, counts[k] + (MAX - total))) });
  function addSpec() {
    if (!cls || !spec) return;
    const k = `${cls}:${spec}`;
    setDraft({ ...counts, [k]: (counts[k] ?? 0) + (total < MAX ? 1 : 0) });
    setSpec(null);
  }
  async function post(busyKey: string, path: string, body: unknown, after?: (r: Reply) => void) {
    setBusy(busyKey);
    try {
      const r = await api.post<Reply>(path, body);
      ok(r.message); setDraft(null); await onChanged(); await loadState(); after?.(r);
    } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function clearAll() {
    let lines: string[] = state?.clear_consequence || [];
    try { lines = (await api.post<{ consequence: string[] }>("/api/test/clear", { preview: true })).consequence; } catch (e) { fail(e); return; }
    if (!(await ask({ title: "Clear the test bench?", message: lines.join("\n"), confirmLabel: "Clear the test bench", color: "red" }))) return;
    await post("clear", "/api/test/clear", {});
  }

  return (
    <Card id="test-bench">
      <CardHeader title="Test bench" hint={`puppet members for rehearsing · ${total} of ${MAX}`} />
      <Stack gap="md" p="md">
        <Group gap="lg" wrap="wrap">
          {byRole.map(([role, n]) => (
            <Tooltip key={role} label={role}><Group gap={4}><GameIcon meta={meta} kind="role" id={role} size={20} title={role} /><Text fw={700}>{n}</Text></Group></Tooltip>
          ))}
          <Group gap={6} ml="auto" wrap="wrap">
            <Text size="xs" c="dimmed">Seed</Text>
            {(state?.seed || []).map((s) => (
              <Button key={s.label} size="compact-xs" variant="default" disabled={dirty} loading={busy === `seed${s.count}`}
                onClick={() => post(`seed${s.count}`, "/api/test/seed", { count: s.count })}>{s.label}</Button>
            ))}
          </Group>
        </Group>
        <Group gap="sm" wrap="wrap">
          {keys.map((k) => {
            const [c, s] = k.split(":");
            const changed = counts[k] !== (now[k]?.length ?? 0);
            return (
              <Group key={k} gap={2} wrap="nowrap" px={6} py={4} style={{ border: `1px solid var(--mantine-color-${changed ? "yellow-7" : "slate-5"})`, borderRadius: 8, opacity: counts[k] ? 1 : 0.45 }}>
                <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`one fewer ${s} ${c}`} onClick={() => step(k, -1)} disabled={!counts[k]}><IconMinus size={14} /></ActionIcon>
                <Tooltip label={`${s} ${c} · ${roleOf(meta, k)}`}><Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="spec" id={k} size={26} title={`${s} ${c}`} /></Box></Tooltip>
                <Text fw={700} w={22} ta="center">{counts[k]}</Text>
                <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`one more ${s} ${c}`} onClick={() => step(k, 1)} disabled={total >= MAX}><IconPlus size={14} /></ActionIcon>
              </Group>
            );
          })}
          {keys.length === 0 && <Text size="sm" c="dimmed">No test members. Seed some above, or add any mix of specs below.</Text>}
        </Group>
        <Group gap="xs" wrap="wrap" align="flex-end">
          <Select size="sm" w={160} placeholder="Class" data={Object.keys(meta.classes)} value={cls} onChange={(v) => { setCls(v); setSpec(null); }} />
          <Select size="sm" w={200} placeholder="Spec" disabled={!cls} data={Object.entries(meta.classes[cls || ""] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }))} value={spec} onChange={setSpec} />
          <Button size="sm" variant="default" leftSection={<IconPlus size={14} />} disabled={!cls || !spec || total >= MAX} onClick={addSpec}>Add</Button>
        </Group>
        <Group gap="sm" wrap="wrap" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
          <Button size="sm" color="red" variant="subtle" leftSection={<IconTrash size={15} />} disabled={(!puppets.length && !(state?.runs.length)) || dirty} loading={busy === "clear"} onClick={clearAll}>Clear the test bench</Button>
          <Group gap="sm" ml="auto" wrap="wrap">
            {dirty ? (
              <>
                <Text size="sm" c="dimmed">{add.reduce((a, r) => a + r.count, 0)} to add · {remove.length} to remove</Text>
                <Button size="sm" variant="default" onClick={() => setDraft(null)}>Cancel</Button>
                <Button size="sm" loading={busy === "save"} onClick={() => post("save", "/api/test/compose", { add, remove })}>Save test members</Button>
              </>
            ) : (
              <>
                <Select size="sm" w={200} aria-label="raid for the comp sandbox" data={meta.raids.map((r) => ({ value: r.id, label: `${r.name} (${r.size})` }))} value={raid} onChange={setRaid} allowDeselect={false} />
                <Tooltip label="Opens a test run two weeks out with every test member joined (never posted in Discord); Propose roster on its board builds the best comp from them">
                  <Button size="sm" leftSection={<IconFlask size={15} />} disabled={!puppets.length || !raid} loading={busy === "comp"}
                    onClick={() => post("comp", "/api/test/comp", { raid }, (r) => { if (r.key) navigate(`/rosters#run-${r.key}`); })}>Try a comp</Button>
                </Tooltip>
              </>
            )}
          </Group>
        </Group>
        {state && !dirty && <OpenTestRun meta={meta} state={state} puppets={puppets.length} busy={busy} onPost={post} />}
        {state && !dirty && state.runs.length > 0 && <AnswerAsPuppets state={state} puppets={puppets} busy={busy} onPost={post} />}
        <Box pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}><TestFeed /></Box>
      </Stack>
      {confirmDialog}
    </Card>
  );
}

type Post = (busyKey: string, path: string, body: unknown, after?: (r: Reply) => void) => Promise<void>;

/** Open a test run now: raid, tempo (the server's TEMPOS), DM every puppet when it opens. The site has no channel of its
 *  own, so without a signup channel it is refused — the card says so and names the setting before anyone presses. */
function OpenTestRun({ meta, state, puppets, busy, onPost }: { meta: Meta; state: TestState; puppets: number; busy: string | null; onPost: Post }) {
  const [raid, setRaid] = useState<string | null>(() => (meta.raids.find((r) => r.size === 20) || meta.raids[0])?.id ?? null);
  const [tempo, setTempo] = useState(state.default_tempo);
  const [dm, setDm] = useState(false);
  const t = state.tempos.find((x) => x.id === tempo);
  return (
    <Stack gap={6} pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
      <Eyebrow>Open a test run</Eyebrow>
      {state.open_refusal && <Alert color="yellow" variant="light" p="xs"><Text size="sm">{state.open_refusal}</Text></Alert>}
      <Group gap="sm" wrap="wrap" align="center">
        <Select size="sm" w={200} aria-label="raid for the test run" data={meta.raids.map((r) => ({ value: r.id, label: `${r.name} (${r.size})` }))} value={raid} onChange={setRaid} allowDeselect={false} />
        <SegmentedControl size="sm" value={tempo} onChange={setTempo} data={state.tempos.map((x) => ({ value: x.id, label: x.label }))} />
        <Switch size="sm" label="DM every puppet when it opens" checked={dm} onChange={(e) => setDm(e.currentTarget.checked)} />
        <Button size="sm" ml="auto" disabled={!raid || !!state.open_refusal || !puppets} loading={busy === "run"}
          onClick={() => onPost("run", "/api/test/run", { raid, tempo, dm_open: dm })}>Open the test run</Button>
      </Group>
      {t && <Text size="xs" c="dimmed">Starts in {t.start_in} min · nudge {t.nudge_in} · lock {t.lock_in} · confirm by {t.confirm_in} min before{state.signup_channel.name ? ` · the sheet posts in #${state.signup_channel.name}` : ""}. Puppets' DMs reach you as one digest per batch; the feed below shows each.</Text>}
    </Stack>
  );
}

/** Puppets answer a live test run: a random mix (Join / Bench / No thanks on an open sheet; Confirm / Can't make it on
 *  their open asks once locked), or one puppet → a status. The counts are presets. */
function AnswerAsPuppets({ state, puppets, busy, onPost }: { state: TestState; puppets: MemberRow[]; busy: string | null; onPost: Post }) {
  const [run, setRun] = useState<string | null>(state.runs[0]?.key ?? null);
  const r = state.runs.find((x) => x.key === run) || state.runs[0];
  const locked = !!r && r.state !== "open";
  const [mix, setMix] = useState<Record<string, string>>({ join: "0", bench: "0", out: "0", yes: "0", no: "0" });
  const [who, setWho] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("in");
  const fields = locked ? [["yes", "Confirm"], ["no", "Can't make it"]] : [["join", "Join"], ["bench", "Bench"], ["out", "No thanks"]];
  const statuses = [{ value: "in", label: "Join" }, { value: "sub", label: "Bench" }, { value: "out", label: locked ? "No thanks (a callout when rostered)" : "No thanks" },
    ...(locked ? [{ value: "yes", label: "Confirm their ask" }, { value: "no", label: "Can't make it (their ask)" }] : [])];
  if (!r) return null;
  const body = Object.fromEntries(fields.map(([k]) => [k, Number(mix[k] || 0)]));
  return (
    <Stack gap={6} pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
      <Group gap="sm" justify="space-between" wrap="wrap">
        <Eyebrow>Answer as puppets</Eyebrow>
        {state.runs.length > 1 && <Select size="xs" w={260} aria-label="which test run" value={r.key} onChange={setRun} allowDeselect={false} data={state.runs.map((x) => ({ value: x.key, label: `${x.title} · ${x.state}` }))} />}
      </Group>
      <Text size="xs" c="dimmed">{r.title} · {r.state} · {locked ? `${r.rostered} rostered${r.asks ? ` · ${r.asks} puppet ask${r.asks === 1 ? "" : "s"} open` : ""}` : `${r.joined} joined`}</Text>
      <Group gap="sm" wrap="wrap" align="flex-end">
        {fields.map(([k, label]) => (
          <Select key={k} size="xs" w={110} label={label} data={COUNTS} value={mix[k]} onChange={(v) => setMix({ ...mix, [k]: v || "0" })} allowDeselect={false} />
        ))}
        <Button size="xs" variant="default" loading={busy === "mix"} disabled={Object.values(body).every((n) => !n)}
          onClick={() => onPost("mix", "/api/test/answer", { run: r.key, ...body })}>Random mix</Button>
        <Select size="xs" w={180} label="One puppet" searchable placeholder="pick a puppet" value={who} onChange={setWho}
          data={puppets.map((p) => ({ value: p.uid, label: p.display_name }))} />
        <Select size="xs" w={200} label="answers" data={statuses} value={status} onChange={(v) => setStatus(v || "in")} allowDeselect={false} />
        <Button size="xs" variant="default" disabled={!who} loading={busy === "one"} onClick={() => onPost("one", "/api/test/answer", { run: r.key, member: who, status })}>Answer</Button>
      </Group>
    </Stack>
  );
}

function roleOf(meta: Meta, k: Key): string {
  const [c, s] = k.split(":");
  return meta.classes[c]?.[s] ?? "";
}
function roleOrder(meta: Meta, k: Key): number {
  const i = meta.roles.indexOf(roleOf(meta, k));
  return i < 0 ? 99 : i;
}
