import { useEffect, useMemo, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { ActionIcon, Box, Button, Card, Group, SegmentedControl, Select, Stack, Switch, Text, Title, Tooltip } from "@mantine/core";
import { IconFlask, IconInfoCircle, IconMinus, IconPlus } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { api, type MemberRow, type Meta } from "../api";
import type { WeekStep } from "./board/types";
import { GameIcon } from "./Icons";
import { Eyebrow, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { RunStrip } from "./RaidCard";
import { TestFeed } from "./TestFeed";
import { useLive } from "../hooks/useLive";

type Key = string;  // "Class:Spec"
interface TestRun { key: string; raid: string; title: string; state: string; when: string; joined: number; rostered: number; posted: boolean; asks: number }
/** A tempo (presets.TEMPOS) with its week in the Rosters strip's shape — words from the server, never assembled here. */
interface Tempo { id: string; label: string; start_in: number; nudge_in: number; lock_in: number; confirm_in: number; week?: WeekStep[] }
export interface TestState {
  members: number; max: number; roster: number; seed: { count: number; label: string }[]; runs: TestRun[];
  signup_channel: { set: boolean; name: string | null }; open_refusal: string | null; tempos: Tempo[]; default_tempo: string; clear_consequence: string[];
}
const COUNTS = ["0", "1", "2", "3", "5", "8", "10", "15", "20"];  // preset counts: nobody types a number
type Reply = { message: string; key?: string };
const RAID_KEY = "oibot.bench.raid";  // the last raid rehearsed, per browser (a convenience: the server's first raid otherwise)

function lastRaid(meta: Meta): string | null {
  let saved: string | null = null;
  try { saved = window.localStorage.getItem(`${RAID_KEY}.${meta.guild_key}`); } catch { /* private window: no memory */ }
  return meta.raids.find((r) => r.id === saved)?.id ?? meta.raids[0]?.id ?? null;
}

/** One line under a row of buttons: why they are greyed, or where the result goes. */
function Why({ children }: { children: React.ReactNode }) {
  return (
    <Group gap={6} wrap="nowrap" align="flex-start">
      <IconInfoCircle size={15} style={{ flexShrink: 0, marginTop: 2 }} color="var(--mantine-color-slate-2)" />
      <Text size="sm" c="dimmed">{children}</Text>
    </Group>
  );
}

/** The Test bench tab (officers, design §5.58 phase 3): both halves of /gm test on the site. Puppets: the role counts
 *  (icon + number), Seed in the server's words, the composition (each spec its icon and a count stepped up or down, one
 *  Save adds or removes the difference) with Class / Spec → Add one puppet, and Clear the test bench (the server's
 *  sentences). ONE Rehearse with raid picker for Try a comp and the test run alike (the server's first raid, else the
 *  last one used); the tempo drawn as the run's week (RunStrip over /api/test `tempos[].week`); a why line under every
 *  greyed button (the server's open_refusal first); Answer as puppets; the live feed. Every write is the /api/test/*
 *  route the Discord wizards' verbs share. */
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
  const MAX = state?.max ?? 80;  // Registry.TEST_MAX, from the server
  const [draft, setDraft] = useState<Record<Key, number> | null>(null);
  useDirty("test-bench", draft !== null);
  const [cls, setCls] = useState<string | null>(null);
  const [spec, setSpec] = useState<string | null>(null);
  const [raid, setRaidState] = useState<string | null>(() => lastRaid(meta));
  const setRaid = (id: string | null) => {
    setRaidState(id);
    try { if (id) window.localStorage.setItem(`${RAID_KEY}.${meta.guild_key}`, id); } catch { /* not remembered */ }
  };
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
  const raidName = meta.raids.find((r) => r.id === raid)?.name ?? "";

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
      <Stack gap="md" p="md">
        <Group gap="sm" align="baseline" wrap="wrap">
          <Title order={2} size="h4">Test bench</Title>
          <Text size="sm" c="dimmed">puppet members to rehearse a run with, which real members never see</Text>
        </Group>

        <Stack gap="sm">
          <Tooltip label={`The bench holds up to ${MAX} puppets.`}><Box style={{ alignSelf: "flex-start" }}><Eyebrow plain>Puppets · {total} of up to {MAX}</Eyebrow></Box></Tooltip>
          <Group gap="lg" wrap="wrap" align="center">
            {byRole.map(([role, n]) => (
              <Group key={role} gap={4} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={20} title={role} /><Text fw={700}>{n}</Text></Group>
            ))}
            <Group gap={6} ml="auto" wrap="wrap" align="center">
              <Text size="sm" c="dimmed">Seed</Text>
              {(state?.seed || []).map((s) => (
                <Button key={s.label} size="xs" variant="default" disabled={dirty} loading={busy === `seed${s.count}`}
                  onClick={() => post(`seed${s.count}`, "/api/test/seed", { count: s.count })}>{s.label}</Button>
              ))}
            </Group>
          </Group>
          {keys.length > 0 && (
            <Group gap="sm" wrap="wrap">
              {keys.map((k) => {
                const [c, s] = k.split(":");
                const changed = counts[k] !== (now[k]?.length ?? 0);
                return (
                  <Group key={k} gap={2} wrap="nowrap" px={6} py={4} style={{ border: `1px solid var(--mantine-color-${changed ? "yellow-7" : "slate-5"})`, borderRadius: 8, opacity: counts[k] ? 1 : 0.45 }}>
                    <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`one fewer ${s} ${c}`} onClick={() => step(k, -1)} disabled={!counts[k]}><IconMinus size={14} /></ActionIcon>
                    <GameIcon meta={meta} kind="spec" id={k} size={26} title={`${s} ${c} · ${roleOf(meta, k)}`} />
                    <Text fw={700} w={22} ta="center">{counts[k]}</Text>
                    <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`one more ${s} ${c}`} onClick={() => step(k, 1)} disabled={total >= MAX}><IconPlus size={14} /></ActionIcon>
                  </Group>
                );
              })}
            </Group>
          )}
          <Group gap="xs" wrap="wrap" align="center">
            <Select size="sm" w={150} aria-label="Class" placeholder="Class" data={Object.keys(meta.classes)} value={cls} onChange={(v) => { setCls(v); setSpec(null); }} />
            <Select size="sm" w={170} aria-label="Spec" placeholder="Spec" disabled={!cls} data={Object.entries(meta.classes[cls || ""] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }))} value={spec} onChange={setSpec} />
            <Button size="sm" variant="default" leftSection={<IconPlus size={14} />} disabled={!cls || !spec || total >= MAX} onClick={addSpec}>Add one puppet</Button>
            <Group gap="sm" ml="auto" wrap="wrap">
              {dirty ? (
                <>
                  <Text size="sm" c="dimmed">{add.reduce((a, r) => a + r.count, 0)} to add · {remove.length} to remove</Text>
                  <Button size="sm" variant="default" onClick={() => setDraft(null)}>Cancel</Button>
                  <Button size="sm" loading={busy === "save"} onClick={() => post("save", "/api/test/compose", { add, remove })}>Save test members</Button>
                </>
              ) : (
                <Button size="sm" color="red" variant="subtle" disabled={!puppets.length && !(state?.runs.length)} loading={busy === "clear"} onClick={clearAll}>Clear the test bench</Button>
              )}
            </Group>
          </Group>
          {dirty ? <Why>Save or cancel the change to the puppets first. Seeding and the test runs wait until then.</Why>
            : !puppets.length && <Why>No puppets yet. Seed some or add one by class and spec, and the raid's analytics cards pause while they exist.</Why>}
        </Stack>

        {state && !dirty && (
          <Rehearse meta={meta} state={state} raid={raid} raidName={raidName} setRaid={setRaid} puppets={puppets.length} busy={busy} onPost={post}
            onComp={() => post("comp", "/api/test/comp", { raid }, (r) => { if (r.key) navigate(`/rosters#run-${r.key}`); })} />
        )}
        {state && !dirty && state.runs.length > 0 && <AnswerAsPuppets meta={meta} state={state} puppets={puppets} busy={busy} onPost={post} />}
        <Box pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}><TestFeed /></Box>
      </Stack>
      {confirmDialog}
    </Card>
  );
}

type Post = (busyKey: string, path: string, body: unknown, after?: (r: Reply) => void) => Promise<void>;

/** Rehearse with: ONE raid picker for Try a comp and the test run alike; the tempo (the server's TEMPOS) as the run's
 *  week strip; DM every puppet when it opens; then a why line — the server's open_refusal, "Add puppets first", or
 *  where the run's card will live. The site has no channel of its own, so without a signup channel it is refused. */
function Rehearse({ meta, state, raid, raidName, setRaid, puppets, busy, onPost, onComp }: {
  meta: Meta; state: TestState; raid: string | null; raidName: string; setRaid: (id: string | null) => void; puppets: number; busy: string | null; onPost: Post; onComp: () => void;
}) {
  const [tempo, setTempo] = useState(state.default_tempo);
  const [dm, setDm] = useState(false);
  const t = state.tempos.find((x) => x.id === tempo);
  const where = raidName ? `The run's card then lives on Rosters under ${raidName} › Test, and its sheet refuses real members.` : "";
  return (
    <Stack gap="sm" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
      <Group gap="sm" wrap="wrap" align="center">
        <Eyebrow plain>Rehearse with</Eyebrow>
        <Select size="sm" w={240} aria-label="Rehearse with" data={meta.raids.map((r) => ({ value: r.id, label: `${r.name} · ${r.size}-player` }))} value={raid} onChange={setRaid} allowDeselect={false} />
        <Text size="sm" c="dimmed">the raid for Try a comp and the test run alike</Text>
      </Group>
      <Group gap="sm" wrap="wrap" align="center">
        <Text size="sm" fw={700}>Open a test run</Text>
        <SegmentedControl size="sm" value={tempo} onChange={setTempo} data={state.tempos.map((x) => ({ value: x.id, label: x.label }))} />
        <Switch size="sm" label="DM every puppet when it opens" checked={dm} onChange={(e) => setDm(e.currentTarget.checked)} />
        <Group gap="sm" ml="auto" wrap="wrap">
          <Tooltip label="Opens a test run two weeks out with every puppet joined, never posted in Discord. Propose roster on its board builds the best comp from them.">
            <Button size="sm" variant="default" leftSection={<IconFlask size={15} />} disabled={!puppets || !raid} loading={busy === "comp"} onClick={onComp}>Try a comp</Button>
          </Tooltip>
          <Button size="sm" disabled={!raid || !!state.open_refusal || !puppets} loading={busy === "run"}
            onClick={() => onPost("run", "/api/test/run", { raid, tempo, dm_open: dm })}>Open the test run</Button>
        </Group>
      </Group>
      {t?.week && <Box px="md"><RunStrip steps={t.week} still /></Box>}
      {state.open_refusal ? <Why>{state.open_refusal}</Why>
        : !puppets ? <Why>Add puppets first. {where}</Why>
        : <Why>{where}{state.signup_channel.name ? ` The sheet posts in #${state.signup_channel.name}.` : ""} The puppets' DMs reach you as one digest per batch, and the feed below shows each.</Why>}
    </Stack>
  );
}

/** Puppets answer a live test run: a random mix (Join / Bench / No thanks on an open sheet; Confirm / Can't make it on
 *  their open asks once locked), or one puppet → a status. The counts are presets. */
function AnswerAsPuppets({ meta, state, puppets, busy, onPost }: { meta: Meta; state: TestState; puppets: MemberRow[]; busy: string | null; onPost: Post }) {
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
        <Eyebrow plain>Answer as puppets</Eyebrow>
        {state.runs.length > 1 && <Select size="sm" w={260} aria-label="Which test run" value={r.key} onChange={setRun} allowDeselect={false} data={state.runs.map((x) => ({ value: x.key, label: `${x.title} · ${meta.run_states[x.state] || x.state}` }))} />}
      </Group>
      <Text size="sm" c="dimmed">{r.title} · {meta.run_states[r.state] || r.state} · {locked ? `${r.rostered} rostered${r.asks ? ` · ${r.asks} puppet ask${r.asks === 1 ? "" : "s"} open` : ""}` : `${r.joined} joined`}</Text>
      <Group gap="sm" wrap="wrap" align="flex-end">
        {fields.map(([k, label]) => (
          <Select key={k} size="sm" w={110} label={label} data={COUNTS} value={mix[k]} onChange={(v) => setMix({ ...mix, [k]: v || "0" })} allowDeselect={false} />
        ))}
        <Button size="sm" variant="default" loading={busy === "mix"} disabled={Object.values(body).every((n) => !n)}
          onClick={() => onPost("mix", "/api/test/answer", { run: r.key, ...body })}>Random mix</Button>
        <Select size="sm" w={180} label="One puppet" searchable placeholder="pick a puppet" value={who} onChange={setWho}
          data={puppets.map((p) => ({ value: p.uid, label: p.display_name }))} />
        <Select size="sm" w={200} label="Answers" data={statuses} value={status} onChange={(v) => setStatus(v || "in")} allowDeselect={false} />
        <Button size="sm" variant="default" disabled={!who} loading={busy === "one"} onClick={() => onPost("one", "/api/test/answer", { run: r.key, member: who, status })}>Answer</Button>
      </Group>
      {Object.values(body).every((n) => !n) && !who && (
        <Why>Pick how many answer {fields.map(([, l]) => l).join(", ")} for a random mix, or one puppet and their answer.</Why>
      )}
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
