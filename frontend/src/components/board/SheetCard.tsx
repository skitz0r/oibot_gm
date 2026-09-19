import { useState } from "react";
import { Accordion, ActionIcon, Badge, Box, Button, Group, Menu, Stack, Text, Tooltip } from "@mantine/core";
import { IconDotsVertical, IconLock, IconPin, IconPinnedOff, IconUserOff, IconUsersPlus } from "@tabler/icons-react";
import type { Meta, Sheet, Signup } from "../../api";
import { GameIcon } from "../Icons";
import { Eyebrow } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { BoardView } from "./BoardView";
import { FillModal } from "./FillModal";
import { RoleCounts, SeatLine, runName, type Act, type OnBoard } from "./shared";

/** One sheet: header + timeline, who joined by class, bench / no-thanks rows, the board, the detail accordion. */
export function SheetCard({ e, meta, busy, onAct, onBoard, onReload }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void> }) {
  const locked = e.state !== "open";
  const n = e.needs;
  const signups = e.signups || [];
  const joined = signups.filter((s) => s.status === "in");
  const roles = Object.fromEntries(meta.roles.map((r) => [r, 0])) as Record<string, number>;
  joined.forEach((s) => { roles[s.role] = (roles[s.role] || 0) + 1; });
  const byClass = Object.entries(joined.reduce((m, s) => { (m[s.cls] ||= []).push(s); return m; }, {} as Record<string, Signup[]>)).sort((a, b) => b[1].length - a[1].length);
  const conf = e.confirmations || [];
  const counts = { yes: conf.filter((c) => c.answer === "yes").length, pending: conf.filter((c) => c.answer === null).length, no: conf.filter((c) => c.answer === "no" || c.answer === "expired").length };
  const short = n ? [n.headcount ? `${n.headcount} seat${n.headcount === 1 ? "" : "s"}` : null, ...Object.entries(n.roles).map(([role, k]) => `${k} ${role}`)].filter(Boolean).join(", ") : "";
  const [ask, confirmDialog] = useConfirm();
  const [fillOpen, setFillOpen] = useState(false);
  const name = runName(e);

  const pin = (s: Signup, p: "in" | "out" | null) => onAct(`pin${s.uid}`, `/api/run/${e.key}/pin`, { uid: s.uid, pin: p });
  /** Officer sets an answer for someone. Before lock it only changes the sheet; after lock the same verb as the board applies:
   *  Join seats them and asks them to confirm, No thanks releases the seat and fills it, Bench keeps them as a fill candidate. */
  async function set(s: Signup, status: string) {
    const label = meta.labels[status] || status;
    const after = status === "in" ? `Join after lock seats ${s.display_name} (${s.character}) and asks them to confirm — by DM, or on their Me page if their DMs are off. If every seat is taken they join unrostered; move them in on the board.`
      : status === "out" ? `No thanks after lock releases ${s.display_name}'s seat and DMs them that it was released; the bench is asked to fill it.`
      : `Bench after lock is the answer only: ${s.display_name} keeps a seat they already hold until you set No thanks; off the roster, they are a fill candidate.`;
    const before = status === "in" ? `${s.display_name} counts as joined on ${s.character}; the board picks them up at lock.`
      : status === "out" ? `${s.display_name} is off the sheet for this run; they are told nothing — this is an officer answer, not theirs.`
      : `${s.display_name} sits on the bench for this run; the fill engine may ask them after lock.`;
    if (!(await ask({ title: `Set ${label} for ${s.display_name}?`, message: `${locked ? after : before} The log records that ${meta.viewer.name} set it.`, confirmLabel: `Set ${label}`, color: status === "out" ? "red" : undefined }))) return;
    await onAct(`set${s.uid}`, `/api/run/${e.key}/set`, { uid: s.uid, status });
  }
  const menu = (s: Signup) => (
    <Menu shadow="md" width={200} position="bottom-end">
      <Menu.Target><ActionIcon variant="subtle" color="gray" size="sm" aria-label={`actions for ${s.display_name}`}><IconDotsVertical size={14} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{s.display_name}</Menu.Label>
        {!locked && s.pin !== "in" && s.status === "in" && <Menu.Item leftSection={<IconPin size={14} />} onClick={() => pin(s, "in")}>Pin to roster</Menu.Item>}
        {!locked && s.pin !== "out" && s.status !== "out" && <Menu.Item leftSection={<IconUserOff size={14} />} onClick={() => pin(s, "out")}>Keep on bench</Menu.Item>}
        {!locked && s.pin && <Menu.Item leftSection={<IconPinnedOff size={14} />} onClick={() => pin(s, null)}>Unpin</Menu.Item>}
        {!locked && <Menu.Divider />}
        {meta.statuses.filter((st) => st !== s.status).map((st) => <Menu.Item key={st} onClick={() => set(s, st)}>Set {meta.labels[st] || st}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );

  async function lockNow() {
    if (!(await ask({ title: `Lock ${name} now?`, message: "The board becomes the roster and everyone rostered gets a confirmation DM.", confirmLabel: "Lock now" }))) return;
    await onAct(`lock${e.key}`, `/api/run/${e.key}/lock`);
  }
  async function cancelRun() {
    if (!(await ask({ title: `Cancel ${name}?`, message: `The sheet closes for everyone on it${locked ? `; the ${e.rostered} rostered seat${e.rostered === 1 ? "" : "s"} and any open confirmation asks are withdrawn` : ""}. Rostered members are not told automatically — say so in the channel.`, confirmLabel: "Cancel the run", color: "red" }))) return;
    await onAct(`cancel${e.key}`, `/api/run/${e.key}/cancel`);
  }

  return (
    <Box id={`run-${e.key}`} p="md" style={{ border: `1px solid ${locked ? "var(--mantine-color-yellow-5)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, scrollMarginTop: 16 }}>
      <Group justify="space-between" wrap="wrap" align="flex-start">
        <Box>
          <Group gap="sm"><Text size="lg" fw={700}>{e.when}</Text>{locked && <Badge variant="light" color="yellow">locked</Badge>}</Group>
          {e.timeline && (
            <Text size="xs" c="dimmed" mt={2}>
              🕒 {e.rel}{!locked && <> · ❗ nudges {e.timeline.nudge}</>} · 🔒 {locked ? "locked" : `locks ${e.timeline.lock}`} · ✓ confirm by {e.timeline.confirm}
            </Text>
          )}
        </Box>
      </Group>

      <Group gap="xs" mt="sm" align="baseline">
        <Text fw={700} style={{ fontSize: 22, fontVariantNumeric: "tabular-nums" }}>{locked ? e.rostered : joined.length}<Text span c="dimmed" fw={500} size="sm"> / {e.size}{e.n_rosters > 1 ? ` × ${e.n_rosters}` : ""}</Text></Text>
        {locked && <Group gap={6} ml="sm"><Badge variant="light" color="teal">✓ {counts.yes}</Badge><Badge variant="light" color="gray">⏳ {counts.pending}</Badge><Badge variant="light" color="red">✗ {counts.no}</Badge><Text size="xs" c="dimmed">fill {e.fill_state}</Text></Group>}
      </Group>
      <Group gap="md" mt={4}>
        <RoleCounts meta={meta} counts={roles} size={18} />
        {(e.double_booked || []).length > 0 && <Tooltip label={e.double_booked!.join(", ")}><Badge variant="light" color="yellow">double-booked {e.double_booked!.length}</Badge></Tooltip>}
      </Group>

      <Group gap="xl" mt="md" align="flex-start" wrap="wrap">
        {byClass.length === 0 && <Text size="sm" c="dimmed">Nobody has joined yet.</Text>}
        {byClass.map(([cls, ss]) => (
          <Box key={cls} style={{ minWidth: 0 }}>
            <Group gap={6} mb={2}><GameIcon meta={meta} kind="class" id={cls} size={20} title={cls} /><Text size="sm" fw={600} style={{ fontVariantNumeric: "tabular-nums" }}>{ss.length}</Text></Group>
            {ss.map((s) => <SeatLine key={s.uid} meta={meta} s={s} right={<>{s.pin && <Badge size="xs" variant="light" color={s.pin === "in" ? "teal" : "red"}>{s.pin === "in" ? "pinned" : "bench"}</Badge>}{menu(s)}</>} />)}
          </Box>
        ))}
      </Group>
      <ChipRow label={`${meta.labels.sub} (${signups.filter((s) => s.status === "sub").length})`} items={signups.filter((s) => s.status === "sub")} menu={menu} />
      <ChipRow label={`${meta.labels.out} (${signups.filter((s) => s.status === "out").length})`} items={signups.filter((s) => s.status === "out")} menu={menu} />
      <Group gap="xs" mt={8} justify="space-between" wrap="wrap">
        <Group gap="xs"><Eyebrow>Away that day</Eyebrow>{(e.absences || []).length === 0 ? <Text size="sm" c="dimmed">nobody</Text> : e.absences!.map((a) => <Text key={a.display_name} size="sm">{a.display_name}{a.reason ? <Text span c="dimmed"> ({a.reason})</Text> : null}</Text>)}</Group>
        {short ? <Badge variant="filled" color="red">short: {short}</Badge> : <Badge variant="light" color="teal">full</Badge>}
      </Group>

      {e.board && <BoardView e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} />}
      {/* Run actions live here in every state, under the board they act on: destructive far left, the step that
          commits the work last on the right. What does not apply yet is disabled with the reason, never removed. */}
      <Group justify="space-between" wrap="wrap" gap="sm" mt="md" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
        <Button size="xs" variant="subtle" color="red" loading={busy === `cancel${e.key}`} onClick={cancelRun}>Cancel run</Button>
        <Group gap="xs">
          <Tooltip label="after lock: asks the bench to fill freed seats" disabled={locked}>
            <Button size="xs" variant="default" leftSection={<IconUsersPlus size={13} />} disabled={!locked} onClick={() => setFillOpen(true)}>Fill seats</Button>
          </Tooltip>
          <Button size="xs" leftSection={<IconLock size={13} />} disabled={locked} loading={busy === `lock${e.key}`} onClick={lockNow}>{locked ? `Locked${e.timeline?.locked_at ? ` ${e.timeline.locked_at}` : ""}` : "Lock now"}</Button>
        </Group>
      </Group>
      <Detail e={e} />
      {locked && <FillModal e={e} meta={meta} opened={fillOpen} onClose={() => setFillOpen(false)} onSent={onReload} />}
      {confirmDialog}
    </Box>
  );
}

function ChipRow({ label, items, menu }: { label: string; items: Signup[]; menu: (s: Signup) => React.ReactNode }) {
  return (
    <Group gap="xs" mt={8} align="center">
      <Eyebrow>{label}</Eyebrow>
      {items.length === 0 && <Text size="sm" c="dimmed">—</Text>}
      {items.map((s) => (
        <Group key={s.uid} gap={4} wrap="nowrap" px={8} py={2} style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 7, background: "var(--mantine-color-slate-6)" }}>
          <Text size="sm">{s.display_name}</Text>{menu(s)}
        </Group>
      ))}
    </Group>
  );
}

function Detail({ e }: { e: Sheet }) {
  const fa = e.fill_asks || [], co = e.callouts || [], log = e.log || [];
  return (
    <Accordion variant="contained" mt="md" chevronPosition="left">
      <Accordion.Item value="detail">
        <Accordion.Control><Text size="xs" c="dimmed">fill asks ({fa.length}) · callouts ({co.length}) · log ({log.length})</Text></Accordion.Control>
        <Accordion.Panel>
          {fa.length > 0 && <Stack gap={2} mb="sm">{fa.map((a, i) => <Text key={i} size="xs">{a.display_name} · {a.kind} · {a.character} ({a.spec}, {a.role}) · {a.reason} · <Text span c={a.answer === "yes" ? "teal" : a.answer === "no" ? "red" : "yellow"}>{a.answer || "waiting"}</Text></Text>)}</Stack>}
          {co.length > 0 && <Text size="xs" c="dimmed" mb="sm">{co.map((c) => `${c.display_name} ${c.hours_before}h before${c.late ? " (after lock)" : ""}`).join(" · ")}</Text>}
          <Box component="pre" style={{ fontSize: 12, whiteSpace: "pre-wrap", margin: 0, fontFamily: "var(--mantine-font-family-monospace)", color: "var(--mantine-color-slate-2)" }}>{log.join("\n")}</Box>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
