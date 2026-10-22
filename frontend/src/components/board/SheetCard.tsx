import { useState, type ReactNode } from "react";
import { Accordion, ActionIcon, Alert, Anchor, Badge, Box, Button, Checkbox, Group, Menu, Modal, Stack, Switch, Text, Textarea, Tooltip } from "@mantine/core";
import { IconBell, IconDiamond, IconDotsVertical, IconFlagCheck, IconLock, IconMessageCircle, IconPlayerPause, IconPlayerPlay, IconSend, IconUsersPlus } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { api, type Meta, type Person, type Sheet, type Signup } from "../../api";
import { MemberMenu, RepostSheetModal, SomeoneElse } from "./MemberMenu";
import { classColour } from "../../theme";
import { GameIcon } from "../Icons";
import { Eyebrow, fail } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { BoardPreview } from "./BoardPreview";
import { BoardView } from "./BoardView";
import { FillModal } from "./FillModal";
import { TellTheBot } from "../TellTheBot";
import { Aura, RoleCounts, SeatLine, runName, type Act, type OnBoard } from "./shared";
import type { LockPreview, NightSheet } from "./types";

/** One sheet: header + timeline, who joined by class, bench / no-thanks rows, the board, the detail accordion. Raid-night
 *  state (dual interfaces phase 2) comes as words from the server: the lock failure, the fill state, asks waiting on the
 *  site, the D3 banner, the spec switches and regulars left out — nothing here composes a sentence the bot didn't. */
/** `badge`: an extra badge beside the state (the Rosters page's Test tab passes the test badge). */
export function SheetCard({ e: sheet, meta, busy, onAct, onBoard, onReload, badge, people }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void>; badge?: ReactNode; people?: Person[] }) {
  const e = sheet as unknown as NightSheet;
  const locked = e.state !== "open";
  const n = e.needs;
  const signups = e.signups || [];
  const joined = signups.filter((s) => s.status === "in");
  const roles = Object.fromEntries(meta.roles.map((r) => [r, 0])) as Record<string, number>;
  joined.forEach((s) => { roles[s.role] = (roles[s.role] || 0) + 1; });
  const byClass = Object.entries(joined.reduce((m, s) => { (m[s.cls] ||= []).push(s); return m; }, {} as Record<string, Signup[]>)).sort((a, b) => b[1].length - a[1].length);
  const conf = e.confirmations || [];
  const counts = { yes: conf.filter((c) => c.answer === "yes").length, pending: conf.filter((c) => c.answer === null).length, no: conf.filter((c) => c.answer === "no" || c.answer === "expired").length };
  const shortRoles = n ? Object.entries(n.roles).filter(([, k]) => k > 0) : [];
  const isShort = !!n && (n.headcount > 0 || shortRoles.length > 0);
  const [ask, confirmDialog] = useConfirm();
  const [fillOpen, setFillOpen] = useState(false);
  const [lockOpen, setLockOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const seatsN = (e.plan?.seats || []).length;  // seats the planner works on (before lock: the tank/healer minimums)
  const asksN = (e.plan?.seats || []).filter((st) => st.options.length).length;
  const name = runName(e as unknown as Sheet);
  const hints = (e.health?.roles || []).filter((r) => r.need && r.have < r.need && r.hint);

  // the row ⋮ menu (design B5): the member's filtered on-behalf list from the server — the same one /raid set offers
  const menu = (s: Signup) => <MemberMenu run={e.key} uid={s.uid} name={s.display_name} onDone={onReload} />;
  const [repostOpen, setRepostOpen] = useState(false);
  const [tellOpen, setTellOpen] = useState(false);  // the officers' Tell the bot box, about this run (design §3.4 OFF-20)

  async function remind() {
    if (!(await ask({ title: `Remind the unconfirmed on ${name}?`, message: `Everyone rostered who hasn't answered gets their Confirm / Can't DM again — the same card, the same deadline. Once an hour per run.`, confirmLabel: "Remind" }))) return;
    await onAct(`remind${e.key}`, `/api/run/${e.key}/remind`);
  }
  async function endNow() {
    let preview = "";
    try { preview = (await api.post<{ preview: string }>(`/api/loot/${e.key}/end`, { preview: true })).preview; } catch (err) { fail(err); return; }
    if (!(await ask({ title: `End ${name} now?`, message: `${preview}\nThe sheet and the lock card close; no more fill asks go out. Reopen works for an hour after.`, confirmLabel: "End the run now", color: "red" }))) return;
    await onAct(`end${e.key}`, `/api/loot/${e.key}/end`);
  }
  const pause = (paused: boolean) => onAct(`pause${e.key}`, `/api/run/${e.key}/pause`, { paused });
  // one Fill button in every state (design B7): before lock the planner's suggestions, after lock the seats to fill
  const fillTip = !locked
    ? (seatsN ? `${seatsN} seat${seatsN === 1 ? "" : "s"} short of the tank/healer minimums — ${asksN} suggestion${asksN === 1 ? "" : "s"}; nothing is sent until you press Send` : "the tank/healer minimums are covered by who joined and the regulars yet to answer")
    : (isShort ? `${n!.headcount + shortRoles.reduce((x, [, k]) => x + k, 0)} short — ${asksN} suggestion${asksN === 1 ? "" : "s"}` : "every seat is taken");

  return (
    <Box id={`run-${e.key}`} p="md" style={{ border: `1px solid ${locked ? "var(--mantine-color-yellow-5)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, scrollMarginTop: 16 }}>
      <Group justify="space-between" wrap="wrap" align="flex-start">
        <Box>
          <Group gap="sm">
            <Text size="lg" fw={700}>{e.when}</Text>
            {badge}
            {locked && <Badge variant="light" color="yellow">locked</Badge>}
            {e.kind === "adhoc" && <Tooltip label="first come, first rostered · no regulars · no reminder"><Badge variant="light" color="blue">ad hoc</Badge></Tooltip>}
            {e.kind === "recurring" && e.series_name && <Text size="xs" c="dimmed">{e.series_name}</Text>}
            {e.early_warning && <Tooltip label="the regulars can't cover a tank or healer minimum — see the suggestions"><Badge variant="light" color="red">early warning</Badge></Tooltip>}
            {e.autofill_paused && <Tooltip label={`autofill paused${e.autofill_paused_by ? ` by ${e.autofill_paused_by}` : ""} — your Fill still sends`}><Badge variant="light" color="gray">autofill paused</Badge></Tooltip>}
          </Group>
          {e.timeline && (
            <Text size="xs" c="dimmed" mt={2}>
              🕒 {e.rel}{!locked && e.timeline.nudge && <> · ❗ nudges {e.timeline.nudge}</>} · 🔒 {locked ? "locked" : `locks ${e.timeline.lock}`}{e.timeline.confirm ? ` · ✓ confirm by ${e.timeline.confirm}` : " · no confirmations: Join is the commitment"}
            </Text>
          )}
          <RunLinks e={e} />
          {e.live && <Button size="compact-xs" variant="subtle" mt={2} leftSection={<IconMessageCircle size={12} />} onClick={() => setTellOpen((x) => !x)}>Tell the bot about this run</Button>}
        </Box>
        <Group gap={4}>
          {e.live && people && <SomeoneElse run={e.key} people={people} onDone={onReload} />}
          <RunMenu e={e} busy={busy} onRemind={remind} onPause={pause} onEnd={endNow} onRepost={() => setRepostOpen(true)} />
        </Group>
      </Group>

      {tellOpen && <Box mt="sm"><TellTheBot scope={`run:${e.key}`} guild={meta.guild_key} prefill={`${name}: `} placeholder={`Ask or tell the bot about ${name}…`} autoFocus /></Box>}
      {e.lock_error && <Alert color="red" variant="light" mt="sm" p="xs"><Text size="sm">{e.lock_error}</Text></Alert>}

      <Group gap="xs" mt="sm" align="baseline">
        <Text fw={700} style={{ fontSize: 22, fontVariantNumeric: "tabular-nums" }}>{locked ? e.rostered : joined.length}<Text span c="dimmed" fw={500} size="sm"> / {e.size}{e.n_rosters > 1 ? ` × ${e.n_rosters}` : ""}</Text></Text>
        {locked && e.confirms !== false && <Group gap={6} ml="sm"><Badge variant="light" color="teal">✓ {counts.yes}</Badge><Badge variant="light" color="gray">⏳ {counts.pending}</Badge><Badge variant="light" color="red">✗ {counts.no}</Badge></Group>}
      </Group>
      <Group gap="md" mt={4}>
        <RoleCounts meta={meta} counts={roles} size={18} />
        {(e.double_booked || []).length > 0 && <Tooltip label={e.double_booked!.join(", ")}><Badge variant="light" color="yellow">double-booked {e.double_booked!.length}</Badge></Tooltip>}
        {(e.health?.no_provider_buffs || []).length > 0 && (
          <Group gap={4} aria-label="party buffs nobody brings">{e.health!.no_provider_buffs!.map((b) => <Aura key={b.id} a={b} missing />)}</Group>
        )}
      </Group>
      {/* who could cover a short role (officers): the server's words, one line per short role, under the counts */}
      {!locked && hints.length > 0 && (
        <Stack gap={0} mt={4}>{hints.map((r) => <Group key={r.role} gap={6} wrap="nowrap"><GameIcon meta={meta} kind="role" id={r.role} size={14} title={r.role} /><Text size="xs" c="dimmed">{r.hint}</Text></Group>)}</Stack>
      )}

      {locked && <NightBlock e={e} meta={meta} onFill={() => setFillOpen(true)} />}
      {!locked && <NotAnswered e={e} meta={meta} onDone={onReload} />}

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
        <Group gap="xs">
          {/* the iconography rule: a shortfall is the word, then icon + number per role (never "2 tank"); a bare number = seats */}
          {isShort ? (
            <Group gap={6} wrap="nowrap">
              <Badge variant="filled" color="red">short</Badge>
              {n!.headcount > 0 && <Tooltip label={`${n!.headcount} seat${n!.headcount === 1 ? "" : "s"}`}><Text size="xs" fw={700} c="red" style={{ fontVariantNumeric: "tabular-nums" }}>{n!.headcount}</Text></Tooltip>}
              {shortRoles.map(([role, k]) => (
                <Group key={role} gap={3} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={16} title={role} /><Text size="xs" fw={700} c="red" style={{ fontVariantNumeric: "tabular-nums" }}>{k}</Text></Group>
              ))}
            </Group>
          ) : <Badge variant="light" color="teal">full</Badge>}
        </Group>
      </Group>

      {e.board && <BoardView e={e as unknown as Sheet} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} />}
      {/* Run actions live here in every state, under the board they act on: destructive far left, the step that
          commits the work last on the right. What does not apply yet is disabled with the reason, never removed. */}
      <Group justify="space-between" wrap="wrap" gap="sm" mt="md" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
        <Button size="xs" variant="subtle" color="red" loading={busy === `cancel${e.key}`} onClick={() => setCancelOpen(true)}>Cancel run</Button>
        <Group gap="xs">
          <Tooltip label={fillTip}>
            <Button size="xs" variant={asksN ? "light" : "default"} color={asksN ? "yellow" : undefined} leftSection={<IconUsersPlus size={13} />} onClick={() => setFillOpen(true)}>Fill</Button>
          </Tooltip>
          <Button size="xs" leftSection={<IconLock size={13} />} disabled={locked} loading={busy === `lock${e.key}`} onClick={() => setLockOpen(true)}>{locked ? `Locked${e.timeline?.locked_at ? ` ${e.timeline.locked_at}` : ""}` : "Lock now"}</Button>
          {/* after lock, the next step is the loot council (design §3.1): open it here (the same route as Loot › Tonight), or go to it */}
          {e.loot?.may_open && (
            <Tooltip label={`opens the council: a thread in the officer channel${(e.n_rosters || 1) > 1 ? ` — one per roster (${e.n_rosters})` : ""}`}>
              <Button size="xs" variant="light" leftSection={<IconDiamond size={13} />} loading={busy === `loot${e.key}`} onClick={() => onAct(`loot${e.key}`, `/api/loot/${e.key}/open`)}>Open loot council</Button>
            </Tooltip>
          )}
          {e.loot?.open && e.loot.bosses && <Text size="xs" c={e.loot.bosses.all_down ? "teal" : "dimmed"}>{e.loot.bosses.text}</Text>}
          {e.loot?.open && <Button size="xs" variant="light" leftSection={<IconDiamond size={13} />} component={Link} to="/loot">Loot council</Button>}
        </Group>
      </Group>
      <Detail e={e} onAct={onAct} busy={busy} />
      <FillModal e={e as unknown as Sheet} meta={meta} opened={fillOpen} onClose={() => setFillOpen(false)} onSent={onReload} />
      <LockModal e={e} meta={meta} opened={lockOpen} onClose={() => setLockOpen(false)} onLock={() => onAct(`lock${e.key}`, `/api/run/${e.key}/lock`)} />
      <RepostSheetModal run={e.key} opened={repostOpen} onClose={() => setRepostOpen(false)} onDone={onReload} />
      <CancelModal e={e} opened={cancelOpen} onClose={() => setCancelOpen(false)} onCancel={(reason, notify) => onAct(`cancel${e.key}`, `/api/run/${e.key}/cancel`, { reason, notify })} />
      {confirmDialog}
    </Box>
  );
}

/** Where the run lives in Discord: the sheet, the officer card (the raid-night hub), the updates thread, the loot thread. */
function RunLinks({ e }: { e: NightSheet }) {
  const l = e.links;
  if (!l) return null;
  const items = ([["sheet", l.sheet], ["card", l.card], ["thread", l.thread], ["loot", l.loot]] as [string, string | null | undefined][]).filter(([, u]) => u);
  if (!items.length) return null;
  return (
    <Group gap={8} mt={2}>
      {items.map(([k, u]) => <Anchor key={k} href={u!} target="_blank" rel="noreferrer" size="xs">{k === "sheet" ? "Sheet in Discord" : k === "card" ? "Officer card" : k === "thread" ? "Updates thread" : "Loot thread"}</Anchor>)}
    </Group>
  );
}

/** The header menu: the raid-night levers that aren't the board's — Remind the unconfirmed (once an hour), pause or resume
 *  autofill (D18), End the run now (after the start). Disabled items carry the server's reason. */
function RunMenu({ e, busy, onRemind, onPause, onEnd, onRepost }: { e: NightSheet; busy: string | null; onRemind: () => void; onPause: (paused: boolean) => void; onEnd: () => void; onRepost: () => void }) {
  if (!e.live) return null;
  const locked = e.state !== "open";
  return (
    <Menu shadow="md" width={260} position="bottom-end">
      <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label={`run actions for ${e.when}`} loading={busy === `remind${e.key}` || busy === `pause${e.key}` || busy === `end${e.key}`}><IconDotsVertical size={16} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Tooltip label={e.remind_note || ""} disabled={!e.remind_note} position="left" withinPortal>
          <div><Menu.Item leftSection={<IconBell size={14} />} disabled={!e.may_remind} onClick={onRemind}>Remind the unconfirmed</Menu.Item></div>
        </Tooltip>
        {e.autofill_paused
          ? <Menu.Item leftSection={<IconPlayerPlay size={14} />} onClick={() => onPause(false)}>Resume autofill</Menu.Item>
          : <Menu.Item leftSection={<IconPlayerPause size={14} />} disabled={!locked} onClick={() => onPause(true)}>Pause autofill</Menu.Item>}
        <Menu.Item leftSection={<IconSend size={14} />} onClick={onRepost}>Re-post sheet…</Menu.Item>
        <Menu.Divider />
        <Tooltip label={e.end_note || ""} disabled={!e.end_note} position="left" withinPortal>
          <div><Menu.Item leftSection={<IconFlagCheck size={14} />} color="red" disabled={!e.may_end} onClick={onEnd}>End the run now</Menu.Item></div>
        </Tooltip>
      </Menu.Dropdown>
    </Menu>
  );
}

/** A locked run's raid-night state, from the server: the fill state in words, the D3 banner (a role swap waits for an
 *  officer — with the Fill button), asks waiting on the site, an unfinished confirmation fan-out, and what the lock
 *  decided — who plays an offspec and which regulars were left out (spec icon alone, one member per line). */
function NightBlock({ e, meta, onFill }: { e: NightSheet; meta: Meta; onFill: () => void }) {
  const switches = e.spec_switches || [], left = e.regulars_not_rostered || [];
  return (
    <Stack gap={6} mt="sm">
      {e.fill_state === "suggested" && (
        <Alert color="yellow" variant="light" p="xs">
          <Group justify="space-between" wrap="wrap" gap="xs">
            <Text size="sm">Waiting on you: {e.plan?.lines?.[0] || "a role swap would cover a short seat"}</Text>
            <Button size="compact-sm" variant="light" color="yellow" onClick={onFill}>Fill</Button>
          </Group>
        </Alert>
      )}
      <Text size="xs" c="dimmed">fill: {e.fill_state_text || e.fill_state}{e.asks_on_site ? ` · ${e.asks_on_site} ask${e.asks_on_site === 1 ? "" : "s"} wait on the site` : ""}{e.reminded_at ? ` · reminded ${e.reminded_at}${e.reminded_by ? ` by ${e.reminded_by}` : ""}` : ""}</Text>
      {e.confirmations_pending && <Text size="xs" c="yellow">Confirmations not yet sent — the bot resumes them within a minute.</Text>}
      {(switches.length > 0 || left.length > 0) && (
        <Group gap="xl" align="flex-start" wrap="wrap">
          {switches.length > 0 && (
            <Box>
              <Eyebrow>Playing an offspec</Eyebrow>
              {switches.map((s) => (
                <Group key={`${s.roster}${s.display_name}`} gap={6} wrap="nowrap" style={{ lineHeight: 1.8 }}>
                  <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={18} title={`${s.spec}${s.signed_spec ? ` (signed up as ${s.signed_spec})` : ""}`} />
                  <Tooltip label={s.display_name}><Text size="sm" fw={600} c={classColour(meta, s.cls)}>{s.character}</Text></Tooltip>
                </Group>
              ))}
            </Box>
          )}
          {left.length > 0 && (
            <Box>
              <Eyebrow>Regulars not rostered</Eyebrow>
              {left.map((s) => (
                <Group key={s.uid} gap={6} wrap="nowrap" style={{ lineHeight: 1.8 }}>
                  <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={18} title={s.spec} />
                  <Tooltip label={s.display_name}><Text size="sm" fw={600} c={classColour(meta, s.cls)}>{s.character}</Text></Tooltip>
                </Group>
              ))}
            </Box>
          )}
        </Group>
      )}
    </Stack>
  );
}

/** Lock now: the server's consequence sentence, and a Preview of the roster(s) the lock would build now
 *  (POST /api/run/{key}/lock/preview — nothing saved); Lock now sends the lock. */
function LockModal({ e, meta, opened, onClose, onLock }: { e: NightSheet; meta: Meta; opened: boolean; onClose: () => void; onLock: () => Promise<void> }) {
  const [p, setP] = useState<LockPreview | null>(null);
  const [busy, setBusy] = useState(false);
  async function preview() {
    setBusy(true);
    try { setP(await api.post<LockPreview>(`/api/run/${e.key}/lock/preview`, {})); } catch (err) { fail(err); } finally { setBusy(false); }
  }
  const close = () => { setP(null); onClose(); };
  return (
    <Modal opened={opened} onClose={close} title={`Lock ${runName(e as unknown as Sheet)} now?`} centered size={p ? "xl" : "md"}>
      <Stack gap="md">
        <Text size="sm" style={{ whiteSpace: "pre-line" }}>{p?.consequence || e.consequences?.lock || "The board becomes the roster."}</Text>
        {p && <BoardPreview meta={meta} board={p.board} />}
        {p && <Text size="xs" c="dimmed">Nothing is saved — Lock now builds the roster again from the sheet as it is then.</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={close}>Back</Button>
          <Button variant="light" size="sm" loading={busy} onClick={preview}>{p ? "Preview again" : "Preview"}</Button>
          <Button size="sm" onClick={async () => { close(); await onLock(); }}>Lock now</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** Cancel: one screen on every surface — the server's consequence, the optional reason (officers only: the run's thread
 *  and the ops feed), and whether a fixed line goes to the signup channel. Nobody is DMed (the DM rule). */
function CancelModal({ e, opened, onClose, onCancel }: { e: NightSheet; opened: boolean; onClose: () => void; onCancel: (reason: string, notify: boolean) => Promise<void> }) {
  const [reason, setReason] = useState("");
  const [notify, setNotify] = useState(true);
  const close = () => { setReason(""); setNotify(true); onClose(); };
  return (
    <Modal opened={opened} onClose={close} title={`Cancel ${runName(e as unknown as Sheet)}?`} centered>
      <Stack gap="md">
        <Text size="sm" style={{ whiteSpace: "pre-line" }}>{e.consequences?.cancel || "The sheet closes."}</Text>
        <Textarea label="Why? (optional)" description={`officers only: the run's thread and the ops feed — never the signup channel · ${reason.length}/300`} value={reason} maxLength={300} autosize minRows={2} onChange={(ev) => setReason(ev.currentTarget.value)} />
        <Checkbox label="Post a line in the signup channel (fixed text, no reason)" checked={notify} onChange={(ev) => setNotify(ev.currentTarget.checked)} />
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={close}>Back</Button>
          <Button color="red" size="sm" onClick={async () => { const r = reason.trim(), nf = notify; close(); await onCancel(r, nf); }}>Cancel the run</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** Officer-facing: who the bot expects an answer from and hasn't heard from — the series' regulars (every main while it
 *  has none), one member per line (spec icon + character, the name in the tooltip), and how the rest of the regulars
 *  answered as icon + number. Ad hoc runs expect nobody, so nothing shows. */
function NotAnswered({ e, meta, onDone }: { e: NightSheet; meta: Meta; onDone: () => Promise<void> }) {
  const waiting = e.not_answered || [];
  if (!e.expected_from || e.expected_from === "none") return null;
  const regs = e.regulars || [];
  const tally = (st: string) => regs.filter((r) => r.status === st).length;
  const title = e.expected_from === "regulars" ? `Regulars who haven't answered (${waiting.length} of ${regs.length})`
    : e.expected_from === "test" ? `Test bench, not answered (${waiting.length})` : `Not answered (${waiting.length}) — no regulars set, everyone counts`;
  return (
    <Box mt="sm" p="sm" style={{ border: `1px solid ${waiting.length ? "var(--mantine-color-yellow-6)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8 }}>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Eyebrow>{title}</Eyebrow>
        {e.expected_from === "regulars" && (
          <Group gap="sm">
            <Tooltip label={meta.labels.in}><Badge variant="light" color="teal">✓ {tally("in")}</Badge></Tooltip>
            <Tooltip label={meta.labels.sub}><Badge variant="light" color="yellow">{meta.labels.sub} {tally("sub")}</Badge></Tooltip>
            <Tooltip label={meta.labels.out}><Badge variant="light" color="red">✗ {tally("out")}</Badge></Tooltip>
          </Group>
        )}
      </Group>
      {waiting.length === 0 ? <Text size="sm" c="dimmed" mt={4}>Everyone has answered.</Text> : (
        <Stack gap={0} mt={4}>
          {waiting.map((s) => (
            <Group key={s.uid || s.display_name} gap={6} wrap="nowrap" style={{ lineHeight: 1.8 }}>
              <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={18} title={`${s.spec} · ${s.role}`} />
              <Tooltip label={s.display_name}><Text size="sm" fw={600} c={classColour(meta, s.cls)} truncate>{s.character}</Text></Tooltip>
              {s.uid && <MemberMenu run={e.key} uid={s.uid} name={s.display_name} onDone={onDone} />}
            </Group>
          ))}
        </Stack>
      )}
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
          {/* a callout's note (officers only — never the public sheet) rides on the chip's tooltip */}
          <Tooltip label={s.note || ""} disabled={!s.note} multiline maw={280}><Text size="sm" td={s.note ? "underline dotted" : undefined}>{s.display_name}</Text></Tooltip>{menu(s)}
        </Group>
      ))}
    </Group>
  );
}

/** The detail accordion: fill asks (an ask still out can be withdrawn), callouts with their notes, and the log — each line
 *  with its stamp in words, officer-only lines marked; the raw lines behind "machine form". `readOnly`: the history drawer. */
export function Detail({ e, onAct, busy, readOnly = false }: { e: NightSheet; onAct?: Act; busy?: string | null; readOnly?: boolean }) {
  const fa = e.fill_asks || [], co = e.callouts || [], rows = e.log_rows || (e.log || []).map((t) => ({ at: "", when: "", text: t, officer: false }));
  const [raw, setRaw] = useState(false);
  return (
    <Accordion variant="contained" mt="md" chevronPosition="left">
      <Accordion.Item value="detail">
        <Accordion.Control><Text size="xs" c="dimmed">fill asks ({fa.length}) · callouts ({co.length}) · log ({rows.length})</Text></Accordion.Control>
        <Accordion.Panel>
          {fa.length > 0 && (
            <Stack gap={2} mb="sm">
              {fa.map((a, i) => (
                <Group key={i} gap={6} wrap="wrap">
                  <Text size="xs">{a.display_name} · {a.kind}{a.phase === "prelock" ? " (before lock)" : ""} · {a.character} ({a.spec}, {a.role}) · {a.reason}{a.approved_by ? ` · by ${a.approved_by}` : ""}{a.channel === "web" ? " · on the site" : ""}{a.deadline && a.answer === null ? ` · by ${a.deadline}` : ""} · <Text span c={a.answer === "yes" ? "teal" : a.answer === "no" ? "red" : "yellow"}>{a.answer || "waiting"}{a.answered_by ? ` (${a.answered_by})` : ""}</Text></Text>
                  {!readOnly && onAct && a.pending && a.uid && (
                    <Button size="compact-xs" variant="subtle" color="gray" loading={busy === `withdraw${e.key}${a.uid}`} onClick={() => onAct(`withdraw${e.key}${a.uid}`, `/api/run/${e.key}/withdraw`, { uid: a.uid })}>Withdraw</Button>
                  )}
                </Group>
              ))}
            </Stack>
          )}
          {co.length > 0 && <Stack gap={0} mb="sm">{co.map((c, i) => <Text key={i} size="xs" c="dimmed">{c.display_name} {c.hours_before}h before{c.late ? " (after lock)" : ""}{c.note ? ` — ${c.note}` : ""}</Text>)}</Stack>}
          <Group justify="space-between" mb={4}><Eyebrow>Log</Eyebrow><Switch size="xs" label="machine form" checked={raw} onChange={(ev) => setRaw(ev.currentTarget.checked)} /></Group>
          {raw ? (
            <Box component="pre" style={{ fontSize: 12, whiteSpace: "pre-wrap", margin: 0, fontFamily: "var(--mantine-font-family-monospace)", color: "var(--mantine-color-slate-2)" }}>{(e.log || []).join("\n")}</Box>
          ) : (
            <Stack gap={0}>
              {rows.map((r, i) => (
                <Group key={i} gap={8} wrap="nowrap" align="flex-start">
                  <Text size="xs" c="dimmed" w={150} style={{ flex: "none", fontVariantNumeric: "tabular-nums" }}>{r.when || "—"}</Text>
                  <Text size="xs" style={{ minWidth: 0 }}>{r.text}{r.officer && <Badge ml={6} size="xs" variant="outline" color="gray">officers</Badge>}</Text>
                </Group>
              ))}
            </Stack>
          )}
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}

