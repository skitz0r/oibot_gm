import { useEffect, useState, type ReactNode } from "react";
import { Accordion, ActionIcon, Alert, Anchor, Badge, Box, Button, Checkbox, Group, Menu, Modal, Stack, Text, Textarea, Tooltip } from "@mantine/core";
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
import { PinMark, RoleCounts, SeatLine, markOf, runName, type Act, type OnBoard } from "./shared";
import { RunStrip } from "../RaidCard";
import type { LockPreview, NightSheet } from "./types";
import css from "../../pages/rosters.module.css";

/** One live run's card (design §5.56), in the order a raid leader reads it: the time, its state and kind; the run's week
 *  as a strip with a now mark; one headcount with its owner (joined before the lock, rostered after) and the role counts;
 *  one short line (role icon + number + why, from the server); before the lock who joined by class, the other answers in
 *  one line and the board collapsed to one line until it's opened; after the lock the board alone — it is the roster.
 *  Raid-night state (dual interfaces phase 2) comes as words from the server: the lock failure, the fill state, asks
 *  waiting on the site, the D3 banner, the spec switches and regulars left out — nothing here composes a sentence the
 *  bot didn't. `badge`: an extra badge beside the state (the Rosters page's Test tab passes the test badge). */
export function SheetCard({ e: sheet, meta, busy, onAct, onBoard, onReload, badge, people }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void>; badge?: ReactNode; people?: Person[] }) {
  const e = sheet as unknown as NightSheet;
  const locked = e.state !== "open";
  const n = e.needs;
  const signups = e.signups || [];
  const joined = signups.filter((s) => s.status === "in");
  // the role counts: who joined before the lock; after it the roster itself — the board's groups, counted once
  const roles = Object.fromEntries(meta.roles.map((r) => [r, 0])) as Record<string, number>;
  (locked && e.board ? e.board.rosters.flatMap((r) => r.groups.flat()) : joined).forEach((s) => { roles[s.role] = (roles[s.role] || 0) + 1; });
  const byClass = Object.entries(joined.reduce((m, s) => { (m[s.cls] ||= []).push(s); return m; }, {} as Record<string, Signup[]>)).sort((a, b) => b[1].length - a[1].length);
  const conf = e.confirmations || [];
  // the Discord card's tally (✅ · ⏳ · ❌, no answer in time counted with can't): the marks and their words from meta.marks
  const counts = { yes: conf.filter((c) => c.answer === "yes").length, waiting: conf.filter((c) => c.answer === null).length, no: conf.filter((c) => c.answer === "no" || c.answer === "expired").length };
  const L = meta.run_labels, W = meta.run_words;
  const shortRoles = n ? Object.entries(n.roles).filter(([, k]) => k > 0) : [];
  // after the lock a freed seat the roles don't account for is a bare number (seats); before it the headcount is the "N / size joined" itself
  const extraSeats = locked && n ? Math.max(0, n.headcount - shortRoles.reduce((x, [, k]) => x + k, 0)) : 0;
  const isShort = shortRoles.length > 0 || extraSeats > 0;
  const anyone = locked || joined.length > 0;
  const [ask, confirmDialog] = useConfirm();
  const [fillOpen, setFillOpen] = useState(false);
  const [lockOpen, setLockOpen] = useState(false);
  const [lockPreview, setLockPreview] = useState(false);  // Preview lock opens the lock screen with the roster drawn
  const [cancelOpen, setCancelOpen] = useState(false);
  const [boardOpen, setBoardOpenState] = useState<boolean>(() => { try { return localStorage.getItem(`oibot.board.${e.key}`) === "1"; } catch { return false; } });
  const setBoardOpen = (v: boolean) => { setBoardOpenState(v); try { localStorage.setItem(`oibot.board.${e.key}`, v ? "1" : "0"); } catch { /* per-viewer convenience only */ } };
  const seatsN = (e.plan?.seats || []).length;  // seats the planner works on (before lock: the tank/healer minimums)
  const asksN = (e.plan?.seats || []).filter((st) => st.options.length).length;
  const name = runName(e as unknown as Sheet);
  const hints = e.health?.hints || [];  // who could cover the short roles: the server's lines ("not on the sheet" said once, with every role it covers)
  const placed = (e.board?.rosters || []).reduce((k, r) => k + r.groups.reduce((m, g) => m + g.length, 0), 0);
  const missing = e.health?.no_provider_buffs || [];
  const bench = signups.filter((s) => s.status === "sub"), outs = signups.filter((s) => s.status === "out"), away = e.absences || [];

  // the row ⋮ menu (design B5): the member's filtered on-behalf list from the server — the same one /raid set offers
  const menu = (s: Signup) => <MemberMenu run={e.key} uid={s.uid} name={s.display_name} onDone={onReload} />;
  const [repostOpen, setRepostOpen] = useState(false);
  const [tellOpen, setTellOpen] = useState(false);  // the officers' Tell the bot box, about this run (design §3.4 OFF-20)

  // Remind and End print ONLY the server's sentence (consequences.py "remind"; End = LootMixin.end_consequence, the
  // council's part + consequences "end" with Reopen's window from code) — the Discord decide screens show the same words
  async function remind() {
    if (!(await ask({ title: `${L.remind} on ${name}?`, message: e.consequences?.remind, confirmLabel: L.remind, cancelLabel: "Back" }))) return;
    await onAct(`remind${e.key}`, `/api/run/${e.key}/remind`);
  }
  async function endNow() {
    let preview = "";
    try { preview = (await api.post<{ preview: string }>(`/api/loot/${e.key}/end`, { preview: true })).preview; } catch (err) { fail(err); return; }
    if (!(await ask({ title: `End ${name}?`, message: preview, confirmLabel: L.end, cancelLabel: "Back", color: "red" }))) return;
    await onAct(`end${e.key}`, `/api/loot/${e.key}/end`);
  }
  const pause = (paused: boolean) => onAct(`pause${e.key}`, `/api/run/${e.key}/pause`, { paused });
  // one button in every state (design B7), named as on Discord: Suggest… before the lock (the planner's suggestions),
  // Fill seats… after it (the seats to fill)
  const fillTip = !locked
    ? (seatsN ? `${seatsN} seat${seatsN === 1 ? "" : "s"} short of the tank/healer minimums — ${asksN} suggestion${asksN === 1 ? "" : "s"}; nothing is sent until you press Send` : "the tank/healer minimums are covered by who joined and the regulars yet to answer")
    : (isShort ? `${(n?.headcount || 0)} short — ${asksN} suggestion${asksN === 1 ? "" : "s"}` : "every seat is taken");
  const facts = [e.kind === "adhoc" ? W.first_come : e.series_name, e.confirms === false ? W.no_confirmations : null, locked ? e.rel : e.timeline ? `locks ${e.timeline.lock}` : null].filter(Boolean).join(" · ");

  return (
    <Box id={`run-${e.key}`} p="md" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 10, scrollMarginTop: 16, background: "var(--mantine-color-slate-8)" }}>
      <Group justify="space-between" wrap="wrap" align="center" gap="xs">
        <Group gap="sm" wrap="wrap" align="center" style={{ minWidth: 0 }}>
          <Text size="lg" fw={800}>{e.when}</Text>
          {badge}
          <Badge variant="light" color={locked ? "gray" : "teal"} tt="none" leftSection={locked ? <IconLock size={12} /> : undefined}>{meta.run_states[e.state] || e.state}</Badge>
          {e.kind === "adhoc" && <Badge variant="light" color="gray" tt="none">ad hoc</Badge>}
          {e.early_warning && <Tooltip label="the regulars can't cover a tank or healer minimum — see the suggestions"><Badge variant="light" color="red" tt="none">early warning</Badge></Tooltip>}
          {e.autofill_paused && <Tooltip label={`${W.auto_asks_paused}${e.autofill_paused_by ? ` by ${e.autofill_paused_by}` : ""} — your ${L.fill} still sends`}><Badge variant="light" color="gray" tt="none">{W.auto_asks_paused.toLowerCase()}</Badge></Tooltip>}
          {facts && <Text size="sm" c="dimmed">{facts}</Text>}
        </Group>
        <Group gap={8} wrap="wrap">
          <RunLinks e={e} />
          {e.live && <Button size="compact-xs" variant="subtle" color="gray" leftSection={<IconMessageCircle size={12} />} onClick={() => setTellOpen((x) => !x)}>Tell the bot about this run</Button>}
          <RunMenu e={e} busy={busy} onRepost={() => setRepostOpen(true)} />
        </Group>
      </Group>

      {tellOpen && <Box mt="sm"><TellTheBot scope={`run:${e.key}`} guild={meta.guild_key} prefill={`${name}: `} placeholder={`Ask or tell the bot about ${name}…`} autoFocus /></Box>}
      {e.lock_error && <Alert color="red" variant="light" mt="sm" p="xs"><Text size="sm">{e.lock_error}</Text></Alert>}
      {e.live && e.week && <Box mt="md"><RunStrip steps={e.week} /></Box>}

      {/* one headcount with its owner, the role counts, one short line, Answer for someone… beside the joiners */}
      <Group gap="md" mt="sm" align="center" wrap="wrap">
        <Text fw={800} style={{ fontSize: 26, fontVariantNumeric: "tabular-nums", lineHeight: 1.2 }}>{locked ? e.rostered : joined.length}<Text span c="dimmed" fw={500} size="md"> / {e.size}{e.n_rosters > 1 ? ` × ${e.n_rosters}` : ""} {locked ? "rostered" : "joined"}</Text></Text>
        {anyone && <RoleCounts meta={meta} counts={roles} size={18} />}
        {anyone && isShort && (
          <Group gap={6} wrap="wrap" align="center">
            <Text size="sm" fw={700} c="red.5">short</Text>
            {extraSeats > 0 && <Tooltip label={`${extraSeats} open seat${extraSeats === 1 ? "" : "s"}`}><Text size="sm" fw={700} c="red.5" style={{ fontVariantNumeric: "tabular-nums" }}>{extraSeats}</Text></Tooltip>}
            {shortRoles.map(([role, k]) => (
              <Group key={role} gap={3} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={16} title={role} /><Text size="sm" fw={700} c="red.5" style={{ fontVariantNumeric: "tabular-nums" }}>{k}</Text></Group>
            ))}
            {e.short_why && <Text size="sm" c="dimmed">— {e.short_why}</Text>}
          </Group>
        )}
        {!anyone && <Text size="sm" c="dimmed">Nobody has joined yet — the sheet is in Discord, or answer for someone here.</Text>}
        {(e.double_booked || []).length > 0 && <Tooltip label={e.double_booked!.join(", ")}><Badge variant="light" color="gray" tt="none">double-booked {e.double_booked!.length}</Badge></Tooltip>}
        {!locked && joined.length > 0 && missing.length > 0 && (
          <Tooltip label={`Nobody in the run brings: ${missing.map((b) => b.name).join(", ")}`} multiline maw={320}>
            <Group gap={4} p={3} wrap="nowrap" aria-label="party buffs nobody in the run brings" style={{ border: "1px dashed var(--mantine-color-slate-4)", borderRadius: 7 }}>
              {missing.map((b) => <BuffMark key={b.id} b={b} />)}
            </Group>
          </Tooltip>
        )}
        <Box style={{ flex: "1 1 auto" }} />
        {e.live && people && <SomeoneElse run={e.key} people={people} onDone={onReload} />}
      </Group>
      {locked && e.confirms !== false && conf.length > 0 && (
        <Group gap="md" mt={6} align="center">
          {(["yes", "waiting", "no"] as const).map((k) => {
            const m = markOf(meta, k === "waiting" ? null : k);
            return <Tooltip key={k} label={k === "no" ? `${m.words}, or ${meta.marks.expired.words}` : m.words}><Text size="sm">{m.mark} {counts[k]} {m.words}</Text></Tooltip>;
          })}
          <Text size="xs" c="dimmed">the same marks as the Discord card</Text>
        </Group>
      )}
      {/* who could cover a short role (officers): the server's words, one line per short role, under the counts */}
      {!locked && hints.length > 0 && (
        <Stack gap={0} mt={4}>{hints.map((h) => <Group key={h.roles.join()} gap={6} wrap="nowrap">{h.roles.map((role) => <GameIcon key={role} meta={meta} kind="role" id={role} size={14} title={role} />)}<Text size="xs" c="dimmed">{h.hint}</Text></Group>)}</Stack>
      )}

      {locked && <NightBlock e={e} meta={meta} onFill={() => setFillOpen(true)} />}
      {!locked && <NotAnswered e={e} meta={meta} onDone={onReload} />}

      {/* before the lock: who joined by class (spec icon + name only); after it the board alone is the roster */}
      {!locked && byClass.length > 0 && (
        <Group gap="xl" mt="md" align="flex-start" wrap="wrap">
          {byClass.map(([cls, ss]) => (
            <Box key={cls} style={{ minWidth: 0 }}>
              <Group gap={6} mb={2}><GameIcon meta={meta} kind="class" id={cls} size={20} title={cls} /><Text size="sm" fw={600} style={{ fontVariantNumeric: "tabular-nums" }}>{ss.length}</Text></Group>
              {ss.map((s) => <SeatLine key={s.uid} meta={meta} s={s} right={<><PinMark meta={meta} pin={s.pin} />{menu(s)}</>} />)}
            </Box>
          ))}
        </Group>
      )}
      {/* the other answers in one line, a part only when someone is in it (after the lock the Bench answers sit in the board's Not rostered list) */}
      <OthersLine label={meta.labels.sub} items={locked ? [] : bench} menu={menu} outs={outs} outLabel={meta.labels.out} away={away} />

      {e.board && locked && <BoardView e={e as unknown as Sheet} meta={meta} onBoard={onBoard} onReload={onReload} />}
      {e.board && !locked && joined.length > 0 && !boardOpen && (
        <Group mt="md" gap="sm" wrap="wrap" justify="space-between" p="sm" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 10 }}>
          <Text size="sm"><b>Board</b> <Text span c="dimmed">— {joined.length} joined, {placed} placed: shape it before the lock, or let the lock place everyone</Text></Text>
          <Button size="xs" variant="default" onClick={() => setBoardOpen(true)}>Open the board</Button>
        </Group>
      )}
      {e.board && !locked && joined.length > 0 && boardOpen && <BoardView e={e as unknown as Sheet} meta={meta} onBoard={onBoard} onReload={onReload} onHide={() => setBoardOpen(false)} />}
      {/* Run actions live here in every state, under the board they act on: destructive far left, the step that
          commits the work last on the right. What does not apply yet is disabled with the reason in its label. */}
      <Group justify="space-between" wrap="wrap" gap="sm" mt="md" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
        <Group gap="xs" wrap="wrap">
          <Button size="xs" variant="subtle" color="red" loading={busy === `cancel${e.key}`} onClick={() => setCancelOpen(true)}>{L.cancel}</Button>
          {/* after the lock, the raid-night levers the Discord lock card's second row has — the same labels (meta.run_labels) */}
          {locked && e.live && <Tooltip label={e.end_note || ""} disabled={!e.end_note}><span><Button size="xs" variant="subtle" color="red" leftSection={<IconFlagCheck size={13} />} disabled={!e.may_end} loading={busy === `end${e.key}`} onClick={endNow}>{L.end}</Button></span></Tooltip>}
        </Group>
        <Group gap="xs" wrap="wrap">
          {locked && e.live && <Tooltip label={e.remind_note || ""} disabled={!e.remind_note}><span><Button size="xs" variant="default" leftSection={<IconBell size={13} />} disabled={!e.may_remind} loading={busy === `remind${e.key}`} onClick={remind}>{L.remind}</Button></span></Tooltip>}
          {locked && e.live && (e.autofill_paused
            ? <Button size="xs" variant="default" leftSection={<IconPlayerPlay size={13} />} loading={busy === `pause${e.key}`} onClick={() => pause(false)}>{L.resume}</Button>
            : <Button size="xs" variant="default" leftSection={<IconPlayerPause size={13} />} loading={busy === `pause${e.key}`} onClick={() => pause(true)}>{L.pause}</Button>)}
          <Tooltip label={fillTip}>
            <Button size="xs" variant={asksN ? "light" : "default"} color={asksN ? "teal" : undefined} leftSection={<IconUsersPlus size={13} />} onClick={() => setFillOpen(true)}>{locked ? L.fill : L.suggest}</Button>
          </Tooltip>
          {!locked && <Button size="xs" variant="default" onClick={() => { setLockPreview(true); setLockOpen(true); }}>{L.preview_lock}</Button>}
          <Button size="xs" leftSection={<IconLock size={13} />} disabled={locked} loading={busy === `lock${e.key}`} onClick={() => { setLockPreview(false); setLockOpen(true); }}>{locked ? `${L.locked}${e.timeline?.locked_at ? ` ${e.timeline.locked_at}` : ""}` : L.lock}</Button>
          {/* after lock, the next step is the loot council (design §3.1): open it here (the same route as Loot › Tonight), or go to it */}
          {e.loot?.may_open && (
            <Tooltip label={`opens the council: a thread in the officer channel${(e.n_rosters || 1) > 1 ? ` — one per roster (${e.n_rosters})` : ""}`}>
              <Button size="xs" variant="light" leftSection={<IconDiamond size={13} />} loading={busy === `loot${e.key}`} onClick={() => onAct(`loot${e.key}`, `/api/loot/${e.key}/open`)}>{L.loot}</Button>
            </Tooltip>
          )}
          {e.loot?.open && e.loot.bosses && <Text size="xs" c={e.loot.bosses.all_down ? "teal" : "dimmed"}>{e.loot.bosses.text}</Text>}
          {e.loot?.open && <Button size="xs" variant="light" leftSection={<IconDiamond size={13} />} component={Link} to="/loot">{L.loot}</Button>}
        </Group>
      </Group>
      <Detail e={e} meta={meta} onAct={onAct} busy={busy} />
      <FillModal e={e as unknown as Sheet} meta={meta} opened={fillOpen} onClose={() => setFillOpen(false)} onSent={onReload} />
      <LockModal e={e} meta={meta} opened={lockOpen} preview={lockPreview} onClose={() => setLockOpen(false)} onLock={() => onAct(`lock${e.key}`, `/api/run/${e.key}/lock`)} />
      <RepostSheetModal run={e.key} opened={repostOpen} onClose={() => setRepostOpen(false)} onDone={onReload} />
      <CancelModal e={e} opened={cancelOpen} onClose={() => setCancelOpen(false)} onCancel={(reason, notify) => onAct(`cancel${e.key}`, `/api/run/${e.key}/cancel`, { reason, notify })} />
      {confirmDialog}
    </Box>
  );
}

/** A buff nobody in the run brings: its icon alone (the cluster's one tooltip names them all). */
function BuffMark({ b }: { b: { abbr: string; colour: string; art: string | null; name: string } }) {
  return b.art
    ? <Box component="img" src={`/img/icon/${b.art}.jpg`} alt={b.name} style={{ width: 22, height: 22, borderRadius: 5, opacity: 0.45, filter: "grayscale(1)" }} />
    : <Box style={{ minWidth: 26, textAlign: "center", padding: "1px 4px", borderRadius: 5, fontSize: 11, fontWeight: 700, color: "var(--mantine-color-slate-2)" }}>{b.abbr}</Box>;
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

/** The header menu: what isn't a raid-night lever — Re-post sheet… (Remind the unanswered, Pause automatic asks and End
 *  the run are buttons on a locked card, as on the Discord lock card's second row). */
function RunMenu({ e, busy, onRepost }: { e: NightSheet; busy: string | null; onRepost: () => void }) {
  if (!e.live) return null;
  return (
    <Menu shadow="md" width={260} position="bottom-end" classNames={{ item: css.menuItem }}>
      <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label={`run actions for ${e.when}`} loading={busy === `repost${e.key}`}><IconDotsVertical size={16} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Item leftSection={<IconSend size={14} />} onClick={onRepost}>Re-post sheet…</Menu.Item>
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
        <Alert color="teal" variant="light" p="xs">
          <Group justify="space-between" wrap="wrap" gap="xs">
            <Text size="sm">Waiting on you: {e.plan?.lines?.[0] || "a role swap would cover a short seat"}</Text>
            <Button size="compact-sm" variant="light" color="teal" onClick={onFill}>{meta.run_labels.fill}</Button>
          </Group>
        </Alert>
      )}
      <Text size="xs" c="dimmed">{e.fill_state_text || e.fill_state}{e.asks_on_site ? ` · ${e.asks_on_site} ask${e.asks_on_site === 1 ? "" : "s"} wait on the site` : ""}{e.reminded_at ? ` · reminded ${e.reminded_at}${e.reminded_by ? ` by ${e.reminded_by}` : ""}` : ""}</Text>
      {e.confirmations_pending && <Text size="xs" c="dimmed">Confirmations not yet sent — the bot resumes them within a minute.</Text>}
      {(switches.length > 0 || left.length > 0) && (
        <Group gap="xl" align="flex-start" wrap="wrap">
          {switches.length > 0 && (
            <Box>
              <Eyebrow plain>Playing an offspec</Eyebrow>
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
              <Eyebrow plain>Regulars not rostered</Eyebrow>
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
function LockModal({ e, meta, opened, preview: startWithPreview = false, onClose, onLock }: { e: NightSheet; meta: Meta; opened: boolean; preview?: boolean; onClose: () => void; onLock: () => Promise<void> }) {
  const [p, setP] = useState<LockPreview | null>(null);
  const [busy, setBusy] = useState(false);
  async function preview() {
    setBusy(true);
    try { setP(await api.post<LockPreview>(`/api/run/${e.key}/lock/preview`, {})); } catch (err) { fail(err); } finally { setBusy(false); }
  }
  useEffect(() => { if (opened && startWithPreview) preview(); }, [opened]);  // eslint-disable-line react-hooks/exhaustive-deps
  const close = () => { setP(null); onClose(); };
  return (
    <Modal opened={opened} onClose={close} title={`Lock ${runName(e as unknown as Sheet)} now?`} centered size={p ? "xl" : "md"}>
      <Stack gap="md">
        <Text size="sm" style={{ whiteSpace: "pre-line" }}>{p?.consequence || e.consequences?.lock}</Text>
        {p && <BoardPreview meta={meta} board={p.board} />}
        {p && <Text size="xs" c="dimmed">Nothing is saved — {meta.run_labels.lock} builds the roster again from the sheet as it is then.</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={close}>Back</Button>
          <Button variant="light" size="sm" loading={busy} onClick={preview}>{p ? "Preview again" : meta.run_labels.preview_lock}</Button>
          <Button size="sm" onClick={async () => { close(); await onLock(); }}>{meta.run_labels.lock}</Button>
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
        <Text size="sm" style={{ whiteSpace: "pre-line" }}>{e.consequences?.cancel}</Text>
        <Textarea label="Why? (optional)" description={`officers only: the run's thread and the ops feed — never the signup channel · ${reason.length}/300`} value={reason} maxLength={300} autosize minRows={2} onChange={(ev) => setReason(ev.currentTarget.value)} />
        <Checkbox label="Tell the channel — a fixed line, no reason" checked={notify} onChange={(ev) => setNotify(ev.currentTarget.checked)} />
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
  if (!e.expected_from || e.expected_from === "none" || waiting.length === 0) return null;  // nobody to wait on: no box
  const regs = e.regulars || [];
  const tally = (st: string) => regs.filter((r) => r.status === st).length;
  // the title is the server's words for who the bot waits on (meta.run_words.not_answered); the counts are numbers
  const title = `${meta.run_words.not_answered[e.expected_from] || meta.run_words.not_answered.everyone} · ${waiting.length}${e.expected_from === "regulars" ? ` of ${regs.length}` : ""}`;
  return (
    <Box mt="sm" p="sm" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Eyebrow plain>{title}</Eyebrow>
        {e.expected_from === "regulars" && (
          <Group gap="sm">{/* how the other regulars answered: the sheet's own answer words + a number, no glyphs */}
            {meta.statuses.map((st) => <Badge key={st} variant="light" color={st === "in" ? "teal" : st === "out" ? "red" : "gray"} tt="none">{meta.labels[st]} {tally(st)}</Badge>)}
          </Group>
        )}
      </Group>
      {(
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

/** The answers that aren't Join, in one line: Bench (before the lock), No thanks and Away that day — a part only when
 *  someone is in it, nothing at all when all three are empty. A callout's note (officers only — never the public sheet)
 *  rides on the name's tooltip; each name keeps its ⋮ menu. */
function OthersLine({ label, items, outLabel, outs, away, menu }: { label: string; items: Signup[]; outLabel: string; outs: Signup[]; away: { display_name: string; reason?: string | null }[]; menu: (s: Signup) => React.ReactNode }) {
  if (!items.length && !outs.length && !away.length) return null;
  const chip = (s: Signup) => (
    <Group key={s.uid} gap={2} wrap="nowrap">
      <Tooltip label={s.note || ""} disabled={!s.note} multiline maw={280}><Text size="sm" fw={600} td={s.note ? "underline dotted" : undefined}>{s.display_name}</Text></Tooltip>{menu(s)}
    </Group>
  );
  return (
    <Group gap="xs" mt="sm" align="center" wrap="wrap">
      {items.length > 0 && <><Text size="sm" c="dimmed">{label} ·</Text>{items.map(chip)}</>}
      {outs.length > 0 && <><Text size="sm" c="dimmed">{items.length ? "· " : ""}{outLabel} ·</Text>{outs.map(chip)}</>}
      {away.length > 0 && <><Text size="sm" c="dimmed">{items.length || outs.length ? "· " : ""}Away that day ·</Text>{away.map((a) => <Text key={a.display_name} size="sm" fw={600}>{a.display_name}{a.reason ? <Text span c="dimmed" fw={400}> ({a.reason})</Text> : null}</Text>)}</>}
    </Group>
  );
}

/** The detail accordion, "What happened · N lines": fill asks (an ask still out can be withdrawn), callouts with their
 *  notes, and the log — each line with its stamp in words, officer-only lines marked. Nothing to show = no accordion.
 *  `readOnly`: the history drawer. */
export function Detail({ e, meta, onAct, busy, readOnly = false }: { e: NightSheet; meta?: Meta; onAct?: Act; busy?: string | null; readOnly?: boolean }) {
  const fa = e.fill_asks || [], co = e.callouts || [], rows = e.log_rows || (e.log || []).map((t) => ({ at: "", when: "", text: t, officer: false }));
  const lines = fa.length + co.length + rows.length;
  if (!lines) return null;
  return (
    <Accordion variant="contained" mt="md" chevronPosition="left">
      <Accordion.Item value="detail">
        <Accordion.Control><Text size="sm" c="dimmed">What happened · {lines} line{lines === 1 ? "" : "s"}</Text></Accordion.Control>
        <Accordion.Panel>
          {fa.length > 0 && (
            <Stack gap={2} mb="sm">
              {fa.map((a, i) => (
                <Group key={i} gap={6} wrap="wrap">
                  <Text size="xs">{a.display_name} · {meta?.ask_kinds[a.kind] || a.kind}{a.phase === "prelock" ? " (before lock)" : ""} · {a.character} ({a.spec}) · {a.reason}{a.approved_by ? ` · by ${a.approved_by}` : ""}{a.channel === "web" ? " · on the site" : ""}{a.deadline && a.answer === null ? ` · by ${a.deadline}` : ""} · <Text span c={a.answer === "yes" ? "teal" : a.answer === "no" ? "red" : "dimmed"}>{meta?.ask_answers[a.answer || "waiting"] || a.answer || "waiting"}{a.answered_by ? ` (${a.answered_by})` : ""}</Text></Text>
                  {!readOnly && onAct && a.pending && a.uid && (
                    <Button size="compact-xs" variant="subtle" color="gray" loading={busy === `withdraw${e.key}${a.uid}`} onClick={() => onAct(`withdraw${e.key}${a.uid}`, `/api/run/${e.key}/withdraw`, { uid: a.uid })}>Withdraw</Button>
                  )}
                </Group>
              ))}
            </Stack>
          )}
          {co.length > 0 && <Stack gap={0} mb="sm">{co.map((c, i) => <Text key={i} size="xs" c="dimmed">{c.display_name} {c.hours_before}h before{c.late ? " (after lock)" : ""}{c.note ? ` — ${c.note}` : ""}</Text>)}</Stack>}
          {rows.length > 0 && <Box mb={4}><Eyebrow plain>Log</Eyebrow></Box>}
          <Stack gap={0}>
            {rows.map((r, i) => (
              <Group key={i} gap={8} wrap="nowrap" align="flex-start">
                <Text size="xs" c="dimmed" w={150} style={{ flex: "none", fontVariantNumeric: "tabular-nums" }}>{r.when || ""}</Text>
                <Text size="xs" style={{ minWidth: 0 }}>{r.text}{r.officer && <Badge ml={6} size="xs" variant="outline" color="gray" tt="none">officers</Badge>}</Text>
              </Group>
            ))}
          </Stack>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
