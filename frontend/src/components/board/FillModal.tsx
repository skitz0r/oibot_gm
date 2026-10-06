import { useEffect, useState } from "react";
import { Badge, Box, Button, Group, Modal, Radio, Stack, Text } from "@mantine/core";
import { ApiError, api, type FillAskRow, type FillPreview, type Meta, type Plan, type Sheet } from "../../api";
import { GameIcon } from "../Icons";
import { classColour } from "../../theme";
import { Eyebrow, fail, ok } from "../Page";
import { runName } from "./shared";

const KIND: Record<string, string> = { sub: "from the bench", regular: "a regular, no answer", unanswered: "no answer", other: "not a regular", flex: "declared flex", offspec: "offspec", alt: "brings an alt" };

/** One ask: the spec icon they'd play + the character (class colour), the member in dim text, then how and why. */
function AskLine({ a, meta, why = false }: { a: FillAskRow; meta: Meta; why?: boolean }) {
  return (
    <Box style={{ minWidth: 0 }}>
      <Group gap={6} wrap="nowrap" style={{ lineHeight: 1.8, minWidth: 0 }}>
        {a.cls ? <GameIcon meta={meta} kind="spec" id={`${a.cls}:${a.spec}`} size={18} title={`${a.cls} ${a.spec} · ${a.role}`} /> : <GameIcon meta={meta} kind="role" id={a.role} size={18} title={a.role} />}
        <Text size="sm" fw={600} c={a.cls ? classColour(meta, a.cls) : undefined} truncate>{a.character}</Text>
        <Text size="xs" c="dimmed" truncate>{a.display_name}</Text>
        <Text size="xs" c="dimmed" style={{ flex: "none" }}>· {KIND[a.kind] || a.kind}</Text>
      </Group>
      {why && a.why && <Text size="xs" c="dimmed" ml={24}>{a.why}</Text>}
    </Box>
  );
}

/** Exactly what Send DMs (raidcycle.send_batch): each seat's picked option in seat order; after lock at most `room`
 *  asks (3 out at once), never a swap without its tied backfill. The server sends the same list for this rev + picks. */
function sendBatch(plan: Plan, picks: Record<number, number>): FillAskRow[] {
  const asks = plan.seats.flatMap((st, i) => (st.options.length ? st.options[picks[i] || 0] || st.options[0] : []));
  if (plan.phase === "prelock" || plan.room == null) return asks;
  const keep = asks.slice(0, plan.room);
  const ids = new Set(keep.map((a) => String(a.uid)));
  return keep.filter((a) => a.pair == null || ids.has(String(a.pair)));
}

/** The planner's plan — before lock the suggestions for the tank/healer minimums, after lock the seats to fill: per
 *  short seat the suggestion and up to two alternates to pick instead (no names typed), then the exact asks Send DMs
 *  and what stays short. Send carries the plan's `rev`: when the plan changed meanwhile the server refuses (409) with
 *  the current plan, shown here instead. Before lock a yes changes the member's signup; nobody is seated until the lock. */
function Suggestions({ plan, meta, picks, setPick, short }: { plan: Plan; meta: Meta; picks: Record<number, number>; setPick: (seat: number, opt: number) => void; short?: FillPreview["short"] }) {
  const expected = Object.entries(plan.need).filter(([r, n]) => n && r !== "dps");
  const batch = sendBatch(plan, picks);
  const names = new Map(batch.map((a) => [String(a.uid), a.display_name]));
  const held = plan.seats.reduce((n, st, i) => n + (st.options.length ? (st.options[picks[i] || 0] || st.options[0]).length : 0), 0) - batch.length;
  return (
    <Stack gap="sm">
      {plan.phase === "prelock" ? (
        <Group gap="md" align="center" wrap="wrap">
          <Eyebrow>Expected</Eyebrow>
          {expected.map(([role, n]) => (
            <Group key={role} gap={4} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={18} title={role} /><Text size="sm" c={(plan.have[role] || 0) < n ? "red" : undefined} style={{ fontVariantNumeric: "tabular-nums" }}>{plan.have[role] || 0}/{n}</Text></Group>
          ))}
          {plan.expected > 0 && <Text size="xs" c="dimmed">joined + {plan.expected} regular{plan.expected === 1 ? "" : "s"} yet to answer</Text>}
          {plan.full && <Badge variant="light" color="gray">every seat taken: swaps first</Badge>}
        </Group>
      ) : short && (
        <Group gap="md" align="center">
          <Eyebrow>Short</Eyebrow>
          {!short.headcount && !Object.keys(short.roles).length ? <Badge variant="light" color="teal">full</Badge> : (
            <Group gap="md">
              {short.headcount > 0 && <Text size="sm" fw={600} style={{ fontVariantNumeric: "tabular-nums" }}>{short.headcount} seat{short.headcount === 1 ? "" : "s"}</Text>}
              {Object.entries(short.roles).map(([role, k]) => <Group key={role} gap={4} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={18} title={role} /><Text size="sm" style={{ fontVariantNumeric: "tabular-nums" }}>{k}</Text></Group>)}
            </Group>
          )}
        </Group>
      )}
      {(plan.waiting || []).length > 0 && <Text size="xs" c="dimmed">Asked, waiting on {(plan.waiting || []).join(", ")} — their seats count as covered until they answer.</Text>}
      {plan.seats.length === 0 && <Text size="sm" c="dimmed">{(plan.waiting || []).length ? "Nothing more to suggest while those asks are out." : "Nothing is short — the minimums are covered."}</Text>}
      {plan.seats.map((st, i) => (
        <Box key={i} p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
          <Group gap={6} mb={4}>{st.role ? <GameIcon meta={meta} kind="role" id={st.role} size={18} title={st.role} /> : <Text size="xs" c="dimmed">seat</Text>}</Group>
          {st.options.length === 0 ? <Text size="sm" c="red">Nobody can cover it → recruit</Text> : (
            <Radio.Group value={String(picks[i] || 0)} onChange={(v) => setPick(i, Number(v))}>
              <Stack gap={4}>
                {st.options.map((opt, k) => (
                  <Radio key={k} value={String(k)} styles={{ body: { alignItems: "flex-start" } }}
                    label={<Stack gap={0}>{opt.map((a, j) => <AskLine key={j} a={a} meta={meta} why={j === 0} />)}{k > 0 && <Text size="xs" c="dimmed">alternate</Text>}</Stack>} />
                ))}
              </Stack>
            </Radio.Group>
          )}
        </Box>
      ))}
      {plan.seats.some((st) => st.options.length) && (
        <Stack gap={2}>
          <Eyebrow>Send DMs exactly these</Eyebrow>
          {batch.length === 0 && <Text size="sm" c="dimmed">Nothing can go out now — at most 3 asks out at once; ask again once some are answered.</Text>}
          {batch.map((a, i) => (
            <Group key={i} gap={6} wrap="nowrap" align="flex-start">
              <AskLine a={a} meta={meta} />
              {["flex", "offspec", "alt"].includes(a.kind) && <Badge size="xs" variant="light" color="orange" style={{ flex: "none" }}>swap</Badge>}
              {a.pair != null && <Text size="xs" c="dimmed" style={{ flex: "none" }}>{a.vacates ? `backfill: ${names.get(String(a.pair)) || "?"}` : `backfills ${names.get(String(a.pair)) || "?"}`}</Text>}
            </Group>
          ))}
          {held > 0 && <Text size="xs" c="dimmed">{held} more wait — at most 3 asks out at once.</Text>}
        </Stack>
      )}
      <Text size="sm">Still short if everyone says yes: <b>{plan.still_short}</b></Text>
    </Stack>
  );
}

/** Fill seats (after lock) or Suggest (before lock): the planner's plan with alternates and the exact asks Send DMs.
 *  Nothing is sent before Send, and Send sends only what is shown (the plan's rev rides with it). */
export function FillModal({ e, meta, opened, onClose, onSent }: { e: Sheet; meta: Meta; opened: boolean; onClose: () => void; onSent: () => Promise<void> }) {
  const [p, setP] = useState<FillPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [picks, setPicks] = useState<Record<number, number>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const suggest = e.state === "open";
  useEffect(() => {
    if (!opened) return;
    setP(null); setBusy(true); setPicks({}); setNotice(null);
    api.post<FillPreview>(`/api/run/${e.key}/fill/preview`, {}).then(setP).catch((err) => { fail(err); onClose(); }).finally(() => setBusy(false));
  }, [opened, e.key]); // eslint-disable-line react-hooks/exhaustive-deps
  const plan = p?.plan;
  const count = plan ? sendBatch(plan, picks).length : 0;
  async function send() {
    setSending(true);
    try {
      const r = await api.post<{ message: string }>(`/api/run/${e.key}/fill`, { rev: plan?.rev, picks });
      ok(r.message); await onSent(); onClose();
    } catch (err) {
      const fresh = err instanceof ApiError && err.status === 409 ? (err.body as { plan?: Plan })?.plan : undefined;
      if (fresh && p) { setP({ ...p, plan: fresh, short: fresh.short }); setPicks({}); setNotice((err as Error).message); } else fail(err);
    } finally { setSending(false); }
  }
  return (
    <Modal opened={opened} onClose={onClose} title={`${suggest ? "Suggestions" : "Fill seats"} · ${runName(e)}`} centered size="lg">
      <Stack gap="md">
        {busy && <Text size="sm" c="dimmed">Working out who to ask…</Text>}
        {notice && <Text size="sm" c="yellow">{notice}</Text>}
        {p && plan && <Suggestions plan={plan} meta={meta} picks={picks} short={p.short} setPick={(i, k) => setPicks((x) => ({ ...x, [i]: k }))} />}
        <Text size="xs" c="dimmed">{suggest
          ? "Each ask is a DM with Yes / No. A yes changes their signup (spec or character); the lock still builds the roster. One ask per member per run; unanswered asks close at lock."
          : "Each ask is a DM with Yes / No; no answer by the deadline counts as no. Tied pairs (a swap and its backfill) go out together. Your Send sends exactly the asks listed, role swaps included."}</Text>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button disabled={!p || count === 0} loading={sending} onClick={send}>Send {count} ask{count === 1 ? "" : "s"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
