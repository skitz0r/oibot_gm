import { useState } from "react";
import { Link } from "react-router-dom";
import { ActionIcon, Menu, Text, Tooltip } from "@mantine/core";
import { IconBell, IconDiamond, IconDots, IconEraser, IconFlagCheck, IconLockOpen, IconPlayerPause, IconPlayerPlay, IconPlayerTrackNext, IconRotate, IconSend, IconTrash, IconUsersPlus } from "@tabler/icons-react";
import { api, type Box, type EventView, type Meta, type Sheet } from "../../api";
import { useConfirmBox } from "../ConfirmBox";
import { boxOf as boxOf_ } from "./shared";
import { fail, ok } from "../Page";
import { FillModal } from "../board/FillModal";
import { RepostSheetModal } from "../board/MemberMenu";
import type { NightSheet } from "../board/types";
import css from "../board/board.module.css";

/** The event's More menu (phase 4, Task 10 — the capability inventory): the raid-night levers the old run card
 *  had and the approved mockup does not draw, kept on the event so none is lost — per raid, from the run's own JSON
 *  (`part.sheet`, ev_json): Fill seats… after the lock / Suggest… before it (the fill screen), Remind the unanswered,
 *  Pause / Resume Automated Fill Requests (once, for the whole event), Re-post the sheet… (the first raid's: it holds the sheet), the loot council, End the
 *  run and, on a finished event, Reopen an end made by mistake; before the lock, Clear
 *  the board (every open raid's groups back to Not placed, the server's box — consequences.clear_board_box). Each raid's split
 *  moved to Edit event (the owner's tweaks, B) with the event's other own settings. Each verb is named as
 *  on Discord (meta.run_labels); what can't be pressed is greyed with the server's reason under it; Remind and End ask
 *  first with the server's box (remind_box, end_box); Unlock and Start now (issue 14) once for the whole event, with unlock_box and
 *  start_now_box — Start now sits right under the first raid's greyed End whose note says it hasn't started; the fill and
 *  re-post screens are the board's own. The asks out and the callouts are the event's Bot activity tab (BotActivity), no
 *  longer an item here. Nothing here composes a sentence. */
export function EventMore({ ev, meta, onChanged }: { ev: EventView; meta: Meta; onChanged: () => void }) {
  const [ask, dialog] = useConfirmBox(["run:", "loot"]);
  const [fill, setFill] = useState<NightSheet | null>(null);
  const [repost, setRepost] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);  // "more" while a press is being written
  const L = meta.run_labels;
  const live = ev.state === "open" || ev.state === "locked";
  const parts = ev.parts.filter((p) => p.sheet).map((p) => ({ p, s: p.sheet as unknown as NightSheet }));
  const several = parts.length > 1;

  async function post(path: string, payload: unknown = {}, what = "more") {
    setBusy(what);
    try { const r = await api.post<{ message: string }>(path, payload); if (r.message) ok(r.message); onChanged(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  /** Remind and End the run print the SERVER's box (§7b; consequences.remind_box, LootMixin.end_box — the run named as
   *  Discord names it): the preview asks the route, the press sends the box's fp as `seen`, a run or council that changed
   *  while the box was open comes back as the fresh box (409) and waits again; a refusal is its box, Back alone. */
  async function boxed(url: string) {
    const preview = async (): Promise<Box> => {
      try { return (await api.post<{ box: Box }>(url, { preview: true })).box; } catch (e) { const b = boxOf_(e); if (b) return b; throw e; }
    };
    let first: Box;
    try { first = await preview(); } catch (e) { fail(e); return; }
    const said = { text: "" };
    const done = await ask({
      box: first, refresh: preview,
      act: async (fp) => {
        try { said.text = (await api.post<{ message: string }>(url, { seen: fp })).message; return null; } catch (e) { const b = boxOf_(e); if (b) return b; throw e; }
      },
    });
    if (done) { if (said.text) ok(said.text); onChanged(); }
  }
  const remind = (s: NightSheet) => boxed(`/api/run/${encodeURIComponent(s.key)}/remind`);
  const clear = () => boxed(`/api/event/${encodeURIComponent(ev.key)}/clear`);
  const end = (s: NightSheet) => boxed(`/api/loot/${encodeURIComponent(s.key)}/end`);
  const unlock = (s: NightSheet) => boxed(`/api/run/${encodeURIComponent(s.key)}/unlock`);
  const start = (s: NightSheet) => boxed(`/api/run/${encodeURIComponent(s.key)}/start`);

  /** one raid's items */
  const items = ({ p, s }: (typeof parts)[number], i: number) => {
    const locked = s.state === "locked";
    const out: React.ReactNode[] = [];
    const why = (note?: string | null) => (note ? <Text size="xs" c="dimmed">{note}</Text> : null);
    // a raid that ended while a later raid still plays offers none of the live items (only Reopen, within the hour)
    if (live && s.state !== "done" && s.state !== "cancelled") {
      out.push(<Menu.Item key={`${p.key}:fill`} leftSection={<IconUsersPlus size={14} />} onClick={() => setFill(s)}>{locked ? L.fill : L.suggest}</Menu.Item>);
      if (locked) {
        out.push(<Menu.Item key={`${p.key}:remind`} leftSection={<IconBell size={14} />} disabled={!s.may_remind} onClick={() => remind(s)}>{L.remind}{why(s.may_remind ? null : s.remind_note)}</Menu.Item>);
      }
      if (i === 0) out.push(<Menu.Item key={`${p.key}:repost`} leftSection={<IconSend size={14} />} onClick={() => setRepost(s.key)}>Re-post the sheet…</Menu.Item>);
      if (s.loot?.may_open) out.push(<Menu.Item key={`${p.key}:loot`} leftSection={<IconDiamond size={14} />} onClick={() => post(`/api/loot/${encodeURIComponent(s.key)}/open`)}>{L.loot}</Menu.Item>);
      else if (s.loot?.open) out.push(<Menu.Item key={`${p.key}:loot`} leftSection={<IconDiamond size={14} />} component={Link} to="/loot">{L.loot}</Menu.Item>);
      if (locked) out.push(<Menu.Item key={`${p.key}:end`} color="red" leftSection={<IconFlagCheck size={14} />} disabled={!s.may_end} onClick={() => end(s)}>{L.end}{why(s.may_end ? null : s.end_note)}</Menu.Item>);
      // Start now acts on the whole event (RaidMixin.start_now): once, right under the first raid's greyed End whose note
      // says it hasn't started, while the server offers it (locked, before the start); the box is consequences.start_now_box
      if (i === 0 && s.may_start) out.push(<Menu.Item key="start" leftSection={<IconPlayerTrackNext size={14} />} onClick={() => start(s)}>{L.start}</Menu.Item>);
    } else if (s.state === "done" && (s.may_reopen || s.reopen_note)) {
      // an END by mistake goes back to locked within the hour (rc.reopen_refusal); a cancelled event never reopens
      out.push(<Menu.Item key={`${p.key}:reopen`} leftSection={<IconRotate size={14} />} disabled={!s.may_reopen} onClick={() => post(`/api/run/${encodeURIComponent(s.key)}/reopen`)}>{L.reopen}{why(s.may_reopen ? null : s.reopen_note)}</Menu.Item>);
    }
    if (!out.length) return [];
    return [...(several ? [<Menu.Label key={`${p.key}:label`}>{p.raid_name}</Menu.Label>] : []), ...out];
  };
  // Pause / Resume Automated Fill Requests acts on every raid of the event (RaidMixin.set_autofill_paused): once, above
  // the raids — Resume while any locked raid is paused, Pause otherwise
  const lockedParts = live ? parts.filter(({ s }) => s.state === "locked") : [];
  const anyPaused = lockedParts.some(({ s }) => s.autofill_paused);
  const all = [
    ...(lockedParts.length ? [anyPaused
      ? <Menu.Item key="pause" leftSection={<IconPlayerPlay size={14} />} onClick={() => post(`/api/run/${encodeURIComponent(lockedParts[0].s.key)}/pause`, { paused: false })}>{L.resume}</Menu.Item>
      : <Menu.Item key="pause" leftSection={<IconPlayerPause size={14} />} onClick={() => post(`/api/run/${encodeURIComponent(lockedParts[0].s.key)}/pause`, { paused: true })}>{L.pause}</Menu.Item>,
      ...(several ? [<Menu.Divider key="pause:div" />] : [])] : []),
    ...parts.flatMap(items),
  ];
  // Unlock acts on the whole event (RaidMixin.unlock_run): once, at the foot, while its first raid is locked — greyed with
  // the server's refusal (started, a loot council open) under it; the box is the server's (consequences.unlock_box)
  const head = parts[0]?.s;
  if (live && head && (head.may_unlock || head.unlock_note)) {
    all.push(<Menu.Divider key="unlock:div" />, <Menu.Item key="unlock" leftSection={<IconLockOpen size={14} />} disabled={!head.may_unlock} onClick={() => unlock(head)}>{L.unlock}{head.may_unlock ? null : <Text size="xs" c="dimmed">{head.unlock_note}</Text>}</Menu.Item>);
  }
  if (ev.board_post?.remove) {  // Post board's messages taken down and the updates stopped (the server's word)
    all.push(<Menu.Divider key="board:div" />, <Menu.Item key="board" leftSection={<IconTrash size={14} />} onClick={() => post(`/api/event/${encodeURIComponent(ev.key)}/board/remove`)}>{ev.board_post.remove}</Menu.Item>);
  }
  if (ev.actions.clear) {  // the whole event's board, once: every open raid's groups back to Not placed (pins stay)
    all.push(<Menu.Divider key="clear:div" />, <Menu.Item key="clear" leftSection={<IconEraser size={14} />} onClick={clear}>Clear the board…</Menu.Item>);
  }
  if (!all.length) return null;

  return (
    <>
      <Menu shadow="md" width={280} position="bottom-end" classNames={{ item: css.menuItem }} withinPortal>
        {/* compact, so the header keeps the mockup's one row: Reschedule · Edit lineup · Lock now, then this */}
        <Menu.Target><Tooltip label="More" withinPortal><ActionIcon variant="default" size={36} loading={busy === "more"} aria-label="More"><IconDots size={18} /></ActionIcon></Tooltip></Menu.Target>
        <Menu.Dropdown>{all}</Menu.Dropdown>
      </Menu>
      {fill && <FillModal e={fill as unknown as Sheet} meta={meta} opened onClose={() => setFill(null)} onSent={async () => { onChanged(); }} />}
      <RepostSheetModal run={repost || ""} opened={!!repost} onClose={() => setRepost(null)} onDone={onChanged} />
      {dialog}
    </>
  );
}
