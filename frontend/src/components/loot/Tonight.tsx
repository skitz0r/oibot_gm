import { Fragment, useEffect, useState } from "react";
import { ActionIcon, Alert, Badge, Box, Button, Card, Group, NumberInput, Stack, Switch, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconX } from "@tabler/icons-react";
import { api, type LootCouncil, type LootOverview, type LootProposal, type LootReply, type LootRun, type Meta } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { useLive } from "../../hooks/useLive";
import { usePoll } from "../../hooks/usePoll";
import { COMPANION_COLOUR, ReasonModal, STATE_MARK, SourceBadge, Who, WhoReasonModal, lootWrite } from "./shared";

/** Loot › Tonight: every locked run's council, live on `loot:<run>` — the same verbs the thread's buttons, plain text and
 *  MCP call (one verb, every surface a caller). Whole-batch writes carry `batch_rev`, per-item Confirm `expect_to`, so a
 *  press made against a picture that changed since is refused with what changed and the card redraws. */
export function Tonight({ meta, goItems }: { meta: Meta; goItems: () => void }) {
  const [data, setData] = useState<LootOverview | null>(null);
  const load = () => api.get<LootOverview>("/api/loot").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(load);
  useLive(load, ["loot:", "run:", "items"]);
  const setRun = (run: LootRun) => setData((d) => (d ? { ...d, runs: d.runs.map((r) => (r.key === run.key ? run : r)) } : d));
  const [owed, setOwed] = useState<{ kind: "reason" | "correct"; run: string; session: string; drop: string; item: string; recipient?: string } | null>(null);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const live = new Set(data.runs.flatMap((r) => r.councils.map((c) => c.id)));
  const ended = data.reasons_pending.filter((r) => !live.has(r.session));
  const endedConflicts = data.conflicts.filter((c) => c.ended);
  return (
    <Stack gap="lg">
      <Group gap="xs" wrap="wrap">
        {data.companions.length === 0 && <Badge variant="outline" color="gray">no companion connected</Badge>}
        {data.companions.map((c) => (
          <Tooltip key={c.character} label={`last event ${c.last_seen_label || "—"}${c.routes_to ? ` · feeds ${c.routes_to}` : ""}`}>
            <Badge variant="light" color={COMPANION_COLOUR[c.state] || "gray"}>{c.character} · {c.state}</Badge>
          </Tooltip>
        ))}
        <Text size="xs" c="dimmed" ml="auto">{data.log.text}</Text>
      </Group>
      {data.feed_waiting && (
        <Alert color="yellow" title={`${data.feed_waiting.count} drop(s) arriving with no run live`}>
          From {data.feed_waiting.characters.join(", ")}'s log — held for 10 minutes. Open the council on tonight's run (or lock it first) and they go in; anything still held after that is listed in the ops channel as not recorded.
        </Alert>
      )}
      {data.runs.length === 0 && <Text c="dimmed">No locked run right now. A council opens on a locked run — lock the roster on the Rosters page first.</Text>}
      {data.runs.map((run) => <RunCard key={run.key} meta={meta} run={run} onState={setRun} goItems={goItems} reload={load} />)}
      {(ended.length > 0 || endedConflicts.length > 0) && (
        <Card>
          <CardHeader title="Still owed from ended councils" hint="reasons and corrections outlive the council" />
          <Stack p="md" gap="xs">
            {ended.map((r) => (
              <Group key={r.import_id} justify="space-between" wrap="wrap">
                <Text size="sm"><b>{r.item}</b> → {r.recipient} in game (the bot picked {r.bot || "?"}) · {r.received_label}</Text>
                <Group gap="xs">
                  <Button size="xs" onClick={() => setOwed({ kind: "reason", run: r.run, session: r.session, drop: r.drop_id, item: r.item, recipient: r.recipient })}>Give the reason…</Button>
                  <Button size="xs" variant="default" onClick={() => setOwed({ kind: "correct", run: r.run, session: r.session, drop: r.drop_id, item: r.item })}>Mistake — correct…</Button>
                </Group>
              </Group>
            ))}
            {endedConflicts.map((c) => (
              <Group key={`${c.session}-${c.drop_id}`} justify="space-between" wrap="wrap">
                <Text size="sm"><b>{c.item}</b>: in game it went to {c.in_game}, recorded to {c.recorded}</Text>
                <Button size="xs" variant="default" onClick={() => setOwed({ kind: "correct", run: c.run, session: c.session, drop: c.drop_id, item: c.item })}>Correct…</Button>
              </Group>
            ))}
          </Stack>
        </Card>
      )}
      <ReasonModal opened={owed?.kind === "reason"} title={`Why ${owed?.recipient} got the ${owed?.item}`} label="Reason (becomes the precedent)" onClose={() => setOwed(null)}
        onSubmit={async (reason) => { const r = await lootWrite(() => api.post<LootReply>(`/api/loot/${owed!.run}/reason`, { session: owed!.session, drop_id: owed!.drop, reason }), () => load()); if (r) { ok(r.message); load(); } return !!r; }} />
      <WhoReasonModal opened={owed?.kind === "correct"} title={`Correct the ${owed?.item}`} people={[]} reasonLabel="What went wrong (kept on the voided record)" onClose={() => setOwed(null)}
        onSubmit={async (to, reason) => { const r = await lootWrite(() => api.post<LootReply>(`/api/loot/${owed!.run}/correct`, { session: owed!.session, drop_id: owed!.drop, to, reason }), () => load()); if (r) { ok(r.message); load(); } return !!r; }} />
    </Stack>
  );
}

function RunCard({ meta, run, onState, goItems, reload }: { meta: Meta; run: LootRun; onState: (r: LootRun) => void; goItems: () => void; reload: () => void }) {
  const [ask, dialog] = useConfirm();
  const [busy, setBusy] = useState(false);
  async function open() {
    setBusy(true);
    const r = await lootWrite(() => api.post<LootReply>(`/api/loot/${run.key}/open`, {}), onState);
    setBusy(false);
    if (r) ok(r.message);
  }
  async function end() {
    if (!(await ask({ title: `End ${run.title}?`, message: run.end_consequence, confirmLabel: "End", color: "red" }))) return;
    try { const r = await api.post<{ message: string }>(`/api/loot/${run.key}/end`, {}); ok(r.message); reload(); } catch (e) { fail(e); }
  }
  return (
    <Card>
      <CardHeader title={run.title} hint={run.when} action={
        <Group gap="xs">
          {run.test && <Badge color="violet" variant="light">rehearsal — nothing reaches the ledger</Badge>}
          {run.councils.length === 0 && run.state === "locked" && <Button size="xs" loading={busy} onClick={open}>Open loot council</Button>}
          {run.councils.length > 0 && run.state === "locked" && <Button size="xs" variant="light" color="red" onClick={end}>End council</Button>}
          {run.state !== "locked" && <Badge variant="outline" color="gray">{run.state}</Badge>}
        </Group>} />
      {run.councils.length === 0 && run.state === "locked" && <Text size="sm" c="dimmed" p="md">No council yet. Opening one makes a thread in the officer channel{run.rosters > 1 ? ` — one per roster (${run.rosters})` : ""}.</Text>}
      <Stack gap={0}>
        {run.councils.map((c) => <CouncilBlock key={c.id} meta={meta} run={run} c={c} onState={onState} goItems={goItems} many={run.councils.length > 1} />)}
      </Stack>
      {dialog}
    </Card>
  );
}

function CouncilBlock({ meta, run, c, onState, goItems, many }: { meta: Meta; run: LootRun; c: LootCouncil; onState: (r: LootRun) => void; goItems: () => void; many: boolean }) {
  const [ask, dialog] = useConfirm();
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [override, setOverride] = useState<LootProposal | null>(null);
  const [correct, setCorrect] = useState<{ drop: string; item: string; current: string } | null>(null);
  const [reason, setReason] = useState<{ drop: string; item: string; recipient: string } | null>(null);
  const [add, setAdd] = useState({ name: "", boss: c.bosses[c.bosses.length - 1]?.boss || "", copies: 1 });
  const [busy, setBusy] = useState<string | null>(null);
  const url = (verb: string) => `/api/loot/${run.key}/${verb}`;
  const body = (b: Record<string, unknown>) => ({ ...b, ...(many ? { session: c.id } : {}) });
  const go = async (key: string, verb: string, b: Record<string, unknown>) => {
    setBusy(key);
    const r = await lootWrite(() => api.post<LootReply>(url(verb), body(b)), onState);
    setBusy(null);
    if (r) ok(r.message);
    return !!r;
  };
  const roster = c.roster.map((p) => ({ value: p.character, label: `${p.character} · ${p.spec} ${p.cls}`, group: "Rostered tonight" }));
  const peopleFor = (p: LootProposal | null) => {
    if (!p) return roster;
    const top = p.candidates.slice(0, 7).map((x) => ({ value: x.character, label: `${x.character} · ${x.tier} · ${x.base.toFixed(2)}`, group: "In the table" }));
    const inTop = new Set(top.map((x) => x.value));
    return [...top, ...roster.filter((x) => !inTop.has(x.value))];
  };
  const clsOf = (name: string) => c.roster.find((p) => p.character === name);
  const undistributed = c.pending.undistributed;
  async function discardAll() {
    try {
      const pv = await api.post<{ preview: string; batch_rev: string }>(url("discard"), body({ preview: true }));
      if (!(await ask({ title: "Discard the batch?", message: pv.preview, confirmLabel: "Discard", color: "red" }))) return;
      await go("discard", "discard", { batch_rev: pv.batch_rev });
    } catch (e) { fail(e); }
  }
  async function confirmRest() {
    try {
      const pv = await api.post<{ preview: string; batch_rev: string }>(url("confirm"), body({ preview: true }));
      if (pv.batch_rev !== c.batch_rev) { fail(new Error("The picks changed since this card was drawn — look again.")); return; }
      if (!(await ask({ title: `Confirm the rest (${c.proposals.length})`, message: pv.preview, confirmLabel: "Record" }))) return;
      await go("confirm-all", "confirm", { batch_rev: c.batch_rev });
    } catch (e) { fail(e); }
  }
  return (
    <Box style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
      <Group px="md" py="xs" gap="xs" wrap="wrap">
        {many && <Text fw={700}>{c.label}</Text>}
        {c.thread_pending && <Badge variant="light" color="yellow">thread pending — made when Discord answers</Badge>}
        {c.test && <Badge variant="light" color="violet">rehearsal</Badge>}
        <Text size="xs" c="dimmed">opened by {c.opened_by || "?"} · {c.opened_label}{c.last_change ? ` · last: ${c.last_change}` : ""}</Text>
        <Switch ml="auto" size="xs" label="Scores only" checked={!!c.scores_only} onChange={(e) => go("scores", "scores-only", { on: e.currentTarget.checked })} disabled={busy === "scores"} />
      </Group>
      <Stack px="md" pb="md" gap="sm">
        {c.distributing && <Alert color="blue" py={6}>Judging {c.distributing.drops} drop(s) — {c.distributing.by} pressed Distribute at {c.distributing.since_label}. Confirm the rest waits until it's done.</Alert>}
        {c.interrupted && <Alert color="orange" py={6}>Distribute was interrupted at {c.interrupted.since_label} — press Distribute to judge the {c.interrupted.drops} drop(s) again.</Alert>}
        {c.scores_only && <Alert color="gray" py={6}>Scores only (set by {c.scores_only.by} at {c.scores_only.at_label}): picks are the top score, no model call.</Alert>}
        {c.untiered > 0 && <Alert color="yellow" py={6}>{c.untiered} untiered drop(s) tonight: everyone rostered is a candidate, ranked by attendance and recent loot only. <Button size="compact-xs" variant="subtle" onClick={goItems}>Tier them on Items</Button></Alert>}
        {c.departed.length > 0 && <Text size="xs" c="dimmed">Left the roster, still candidates: {c.departed.map((d) => (
          <Button key={d} size="compact-xs" variant="subtle" color="red" onClick={() => go(`rm-${d}`, "candidate/remove", { who: d })}>{d} ✕</Button>))}</Text>}

        <Box>
          <Text size="xs" fw={700} c="dimmed" tt="uppercase" mb={4}>Drops</Text>
          {c.bosses.length === 0 && <Text size="sm" c="dimmed">Waiting for drops — tick them here, in the thread, or let the companion do it.</Text>}
          {c.bosses.map((b) => (
            <Box key={b.boss} mb={6}>
              <Text size="sm" fw={600}>{b.boss} <Text span size="xs" c="dimmed">▫️ {b.counts.ticked || 0} · 🟡 {b.counts.proposed || 0} · ✅ {b.counts.awarded || 0}</Text></Text>
              {b.drops.map((d) => (
                <Group key={d.drop_id} gap={6} wrap="nowrap" pl="sm">
                  <Text size="sm">{STATE_MARK[d.state]}</Text>
                  <Text size="sm" style={{ minWidth: 0 }} truncate>{d.item}</Text>
                  {!d.tiered && <Badge size="xs" variant="outline" color="yellow">untiered</Badge>}
                  {d.held && <Badge size="xs" variant="outline" color="gray">held</Badge>}
                  {d.to && <><Text size="xs" c="dimmed">→</Text><Who meta={meta} name={d.to} cls={clsOf(d.to)?.cls} spec={clsOf(d.to)?.spec} /></>}
                  {d.state !== "awarded" && <Tooltip label="un-tick (a false drop)"><ActionIcon size="sm" variant="subtle" color="gray" onClick={() => go(`rm-${d.drop_id}`, "tick", { remove: [d.drop_id] })}><IconX size={13} /></ActionIcon></Tooltip>}
                </Group>
              ))}
            </Box>
          ))}
          <Group gap="xs" align="flex-end" wrap="wrap" mt={4}>
            <TextInput size="xs" label="Item" placeholder="what dropped (a name is enough)" value={add.name} onChange={(e) => setAdd({ ...add, name: e.currentTarget.value })} w={240} />
            <TextInput size="xs" label="Boss" value={add.boss} onChange={(e) => setAdd({ ...add, boss: e.currentTarget.value })} w={170} />
            <NumberInput size="xs" label="Copies" min={1} max={5} value={add.copies} onChange={(v) => setAdd({ ...add, copies: Number(v) || 1 })} w={80} />
            <Button size="xs" variant="default" disabled={!add.name.trim()} loading={busy === "tick"} onClick={async () => { if (await go("tick", "tick", { add: [{ name: add.name.trim(), boss: add.boss.trim() || null, copies: add.copies }] })) setAdd({ ...add, name: "", copies: 1 }); }}>Tick</Button>
            <Button size="xs" disabled={!undistributed || !!c.distributing} loading={busy === "dist"} onClick={() => go("dist", "distribute", { drops_rev: c.drops_rev })}>Distribute {undistributed || ""} drop{undistributed === 1 ? "" : "s"}</Button>
          </Group>
        </Box>

        {c.proposals.length > 0 && (
          <Box>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase" mb={4}>Picks waiting for Confirm ({c.proposals.length})</Text>
            <Table.ScrollContainer minWidth={640}>
              <Table verticalSpacing={4}>
                <Table.Tbody>
                  {c.proposals.map((p) => {
                    const who = clsOf(p.to) || p.candidates.find((x) => x.character === p.to);
                    return (
                      <Fragment key={p.drop_id}>
                        <Table.Tr>
                          <Table.Td w={28}><ActionIcon size="sm" variant="subtle" color="gray" onClick={() => setOpen({ ...open, [p.drop_id]: !open[p.drop_id] })}>{open[p.drop_id] ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}</ActionIcon></Table.Td>
                          <Table.Td><Text size="sm" fw={600}>#{p.order} {p.item}</Text><Text size="xs" c="dimmed">{p.boss}</Text></Table.Td>
                          <Table.Td><Who meta={meta} name={p.to} cls={who?.cls} spec={who?.spec} /></Table.Td>
                          <Table.Td>
                            <Group gap={4}>
                              {p.override && <Tooltip label={`${p.override.by} at ${p.override.at_label}: “${p.override.reason}” (the bot picked ${p.bot})`}><Badge size="xs" color="orange" variant="light">⚠ override pending</Badge></Tooltip>}
                              {p.close_call && !p.override && <Badge size="xs" variant="light" color="yellow">≈ close call</Badge>}
                              {p.order_sensitive && <Badge size="xs" variant="light" color="blue">⇄ order</Badge>}
                              {p.judged_by === "deterministic" && <Badge size="xs" variant="outline" color="gray">✎ scores only</Badge>}
                              {!p.tiered && <Badge size="xs" variant="outline" color="yellow">untiered</Badge>}
                            </Group>
                          </Table.Td>
                          <Table.Td w={260}>
                            <Group gap={4} justify="flex-end" wrap="nowrap">
                              <Button size="compact-xs" loading={busy === `c-${p.drop_id}`} onClick={() => go(`c-${p.drop_id}`, "confirm", { only: [p.drop_id], expect_to: p.to })}>Confirm</Button>
                              <Button size="compact-xs" variant="default" onClick={() => setOverride(p)}>Override…</Button>
                              <Button size="compact-xs" variant="subtle" color="gray" onClick={() => go(`h-${p.drop_id}`, "discard", { only: [p.drop_id] })}>Decide later</Button>
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                        <Table.Tr style={{ display: open[p.drop_id] ? undefined : "none" }}>
                          <Table.Td />
                          <Table.Td colSpan={4}>
                            <Box>
                              {!p.tiered && <Text size="xs" c="yellow" mb={4}>No tier data for this item — everyone rostered is a candidate, ranked by attendance and recent loot only.</Text>}
                              <Text size="xs" mb={4}>{p.justification}</Text>
                              {p.warnings.map((w) => <Text key={w} size="xs" c="orange">• {w}</Text>)}
                              {p.alternates.length > 0 && <Text size="xs" c="dimmed">Alternates: {p.alternates.join(", ")}</Text>}
                              <Table verticalSpacing={2} fz="xs" mt={4}>
                                <Table.Thead><Table.Tr><Table.Th>Candidate</Table.Th><Table.Th>Tier</Table.Th><Table.Th>Upgrade</Table.Th><Table.Th>Attendance</Table.Th><Table.Th>Rank</Table.Th><Table.Th>Wishlist</Table.Th><Table.Th>14d loot</Table.Th><Table.Th>Score</Table.Th></Table.Tr></Table.Thead>
                                <Table.Tbody>
                                  {p.candidates.map((x) => (
                                    <Table.Tr key={x.character} style={{ opacity: x.already_has ? 0.5 : 1 }}>
                                      <Table.Td><Who meta={meta} name={x.character} cls={x.cls} spec={x.spec} /></Table.Td>
                                      <Table.Td>{x.tier}{x.offspec ? " (OS)" : ""}</Table.Td><Table.Td>{x.upgrade.toFixed(2)}</Table.Td>
                                      <Table.Td>{x.attendance_known ? x.attendance : "not tracked yet"}</Table.Td><Table.Td>{x.rank}</Table.Td>
                                      <Table.Td>{x.wishlist_rank ? `#${x.wishlist_rank}` : "—"}</Table.Td><Table.Td>{x.recent_power.toFixed(2)}</Table.Td>
                                      <Table.Td fw={700}>{x.already_has ? "has it" : x.base.toFixed(2)}</Table.Td>
                                    </Table.Tr>
                                  ))}
                                </Table.Tbody>
                              </Table>
                            </Box>
                          </Table.Td>
                        </Table.Tr>
                      </Fragment>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            <Group justify="flex-end" gap="xs" mt={6}>
              <Button size="xs" variant="default" color="red" onClick={discardAll}>Discard batch</Button>
              <Button size="xs" disabled={!!c.distributing} loading={busy === "confirm-all"} onClick={confirmRest}>{c.distributing ? `Re-scoring ${c.distributing.drops} item(s)…` : `Confirm the rest (${c.proposals.length})`}</Button>
            </Group>
          </Box>
        )}

        {(c.pending_reasons.length > 0 || c.conflicts.length > 0) && (
          <Box>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase" mb={4}>Waiting for a reason or a correction</Text>
            {c.pending_reasons.map((r) => (
              <Group key={r.drop_id} justify="space-between" wrap="wrap">
                <Text size="sm"><b>{r.item}</b> → {r.recipient} in game; the bot picked {r.bot}</Text>
                <Group gap={4}>
                  <Button size="compact-xs" onClick={() => setReason({ drop: r.drop_id, item: r.item, recipient: r.recipient || "" })}>Give the reason…</Button>
                  <Button size="compact-xs" variant="default" onClick={() => setCorrect({ drop: r.drop_id, item: r.item, current: r.recipient || "" })}>Mistake — correct…</Button>
                </Group>
              </Group>
            ))}
            {c.conflicts.map((r) => (
              <Group key={`cf-${r.drop_id}`} justify="space-between" wrap="wrap">
                <Text size="sm"><b>{r.item}</b>: in game it went to {r.in_game}, recorded to {r.recorded}</Text>
                <Button size="compact-xs" variant="default" onClick={() => setCorrect({ drop: r.drop_id, item: r.item, current: r.recorded || "" })}>Correct…</Button>
              </Group>
            ))}
          </Box>
        )}

        {c.awards.length > 0 && (
          <Box>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase" mb={4}>Recorded ({c.awards.length})</Text>
            {c.awards.map((a) => (
              <Group key={`${a.drop_id}-${a.import_id}`} gap={6} wrap="nowrap">
                <Text size="sm">✅ {a.item}</Text><Text size="xs" c="dimmed">→</Text>
                <Who meta={meta} name={a.to} cls={clsOf(a.to)?.cls} spec={clsOf(a.to)?.spec} />
                <SourceBadge source={a.source} />
                <Text size="xs" c="dimmed" truncate>{a.by} · {a.at_label}</Text>
                {a.drop_id && <Button ml="auto" size="compact-xs" variant="subtle" color="gray" onClick={() => setCorrect({ drop: a.drop_id!, item: a.item, current: a.to })}>Correct…</Button>}
              </Group>
            ))}
          </Box>
        )}
      </Stack>
      <WhoReasonModal opened={!!override} title={override ? `Award ${override.item} instead of ${override.to}` : ""} people={peopleFor(override)} current={override?.to} reasonLabel="Reason (required — becomes the precedent at Confirm)"
        onClose={() => setOverride(null)}
        onSubmit={async (to, reason) => {
          const p = override!;
          setBusy(`o-${p.drop_id}`);
          let r = await lootWrite(() => api.post<LootReply>(url("override"), body({ drop_id: p.drop_id, to, reason, batch_rev: c.batch_rev })), onState);
          if (!r && p.override && (await ask({ title: "Replace the pending override?", message: `${p.override.by} already set this to ${p.override.to} (“${p.override.reason}”).`, confirmLabel: "Replace" }))) {
            r = await lootWrite(() => api.post<LootReply>(url("override"), body({ drop_id: p.drop_id, to, reason, replace: true })), onState);
          }
          setBusy(null);
          if (r) ok(`${r.message} — recorded at Confirm`);
          return !!r;
        }} />
      <WhoReasonModal opened={!!correct} title={correct ? `Correct the ${correct.item} (recorded to ${correct.current})` : ""} people={roster} current={correct?.current} reasonLabel="What went wrong (kept on the voided record; no precedent)"
        onClose={() => setCorrect(null)} onSubmit={async (to, why) => go(`fix-${correct!.drop}`, "correct", { drop_id: correct!.drop, to, reason: why })} />
      <ReasonModal opened={!!reason} title={reason ? `Why ${reason.recipient} got the ${reason.item}` : ""} label="Reason (becomes the precedent)" onClose={() => setReason(null)}
        onSubmit={async (why) => {
          let r = await lootWrite(() => api.post<LootReply>(url("reason"), body({ drop_id: reason!.drop, reason: why })), onState);
          if (!r && (await ask({ title: "Replace the reason?", message: "Someone already gave a reason for this award.", confirmLabel: "Replace" }))) {
            r = await lootWrite(() => api.post<LootReply>(url("reason"), body({ drop_id: reason!.drop, reason: why, replace: true })), onState);
          }
          if (r) ok(r.message);
          return !!r;
        }} />
      {dialog}
    </Box>
  );
}
