import { useState } from "react";
import { Link } from "react-router-dom";
import { ActionIcon, Menu, Modal, Text, Tooltip } from "@mantine/core";
import { IconBell, IconDiamond, IconDots, IconFlagCheck, IconListCheck, IconPlayerPause, IconPlayerPlay, IconRotate, IconSend, IconUsersPlus } from "@tabler/icons-react";
import { api, type Box, type EventView, type Meta, type Sheet } from "../../api";
import { useConfirmBox } from "../ConfirmBox";
import { fail, ok } from "../Page";
import { FillModal } from "../board/FillModal";
import { RepostSheetModal } from "../board/MemberMenu";
import { Detail } from "../board/SheetCard";
import type { NightSheet } from "../board/types";
import css from "../board/board.module.css";

/** The event's More menu (phase 4, Task 10 — the capability inventory): the raid-night levers the Rosters page's card
 *  had and the approved mockup does not draw, kept on the event so none is lost — per raid, from the run's own JSON
 *  (`part.sheet`, ev_json): Fill seats… after the lock / Suggest… before it (the fill screen), Remind the unanswered,
 *  Pause / Resume automatic asks, Re-post the sheet… (the first raid's: it holds the sheet), the loot council, End the
 *  run, the asks out (each with Withdraw) and, on a finished event, Reopen an end made by mistake. Each verb is named as
 *  on Discord (meta.run_labels); what can't be pressed is greyed with the server's reason under it; Remind and End ask
 *  first with the server's sentence (consequences "remind", End's preview), as the Rosters card did; the fill and
 *  re-post screens are the board's own. Nothing here composes a sentence. */
export function EventMore({ ev, meta, onChanged }: { ev: EventView; meta: Meta; onChanged: () => void }) {
  const [ask, dialog] = useConfirmBox();
  const [fill, setFill] = useState<NightSheet | null>(null);
  const [repost, setRepost] = useState<string | null>(null);
  const [asks, setAsks] = useState<NightSheet | null>(null);
  const [busy, setBusy] = useState(false);
  const L = meta.run_labels;
  const live = ev.state === "open" || ev.state === "locked";
  const parts = ev.parts.filter((p) => p.sheet).map((p) => ({ p, s: p.sheet as unknown as NightSheet }));
  const several = parts.length > 1;

  async function post(path: string, payload: unknown = {}) {
    setBusy(true);
    try { const r = await api.post<{ message: string }>(path, payload); if (r.message) ok(r.message); onChanged(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  // Remind and End print ONLY the server's sentence (consequences.py "remind"; End = LootMixin.end_consequence: the
  // council's part + "end" with Reopen's window from code) — the Discord decide screens show the same words — in the
  // event page's box (Back, then the verb named as on Discord); neither has a server box of its own yet
  const boxOf = (title: string, text: string | undefined, verb: string, danger = false): Box =>
    ({ title, sentences: (text || "").split("\n").filter(Boolean), verb, danger, diff: [], fp: "" });
  async function remind(s: NightSheet, name: string) {
    if (!(await ask({ box: boxOf(`${L.remind} on ${name}?`, s.consequences?.remind, L.remind) }))) return;
    await post(`/api/run/${s.key}/remind`);
  }
  async function end(s: NightSheet, name: string) {
    let preview = "";
    try { preview = (await api.post<{ preview: string }>(`/api/loot/${s.key}/end`, { preview: true })).preview; } catch (err) { fail(err); return; }
    if (!(await ask({ box: boxOf(`End ${name}?`, preview, L.end, true) }))) return;
    await post(`/api/loot/${s.key}/end`);
  }

  /** one raid's items; `name` = how its confirms name it (the raid on a several-raid evening, else the event) */
  const items = ({ p, s }: (typeof parts)[number], i: number) => {
    const name = several ? `${p.raid_name} on ${ev.title}` : ev.title;
    const locked = s.state === "locked";
    const out: React.ReactNode[] = [];
    const why = (note?: string | null) => (note ? <Text size="xs" c="dimmed">{note}</Text> : null);
    if (live) {
      out.push(<Menu.Item key={`${p.key}:fill`} leftSection={<IconUsersPlus size={14} />} onClick={() => setFill(s)}>{locked ? L.fill : L.suggest}</Menu.Item>);
      if (locked) {
        out.push(<Menu.Item key={`${p.key}:remind`} leftSection={<IconBell size={14} />} disabled={!s.may_remind} onClick={() => remind(s, name)}>{L.remind}{why(s.may_remind ? null : s.remind_note)}</Menu.Item>);
        out.push(s.autofill_paused
          ? <Menu.Item key={`${p.key}:pause`} leftSection={<IconPlayerPlay size={14} />} onClick={() => post(`/api/run/${s.key}/pause`, { paused: false })}>{L.resume}</Menu.Item>
          : <Menu.Item key={`${p.key}:pause`} leftSection={<IconPlayerPause size={14} />} onClick={() => post(`/api/run/${s.key}/pause`, { paused: true })}>{L.pause}</Menu.Item>);
      }
      if (i === 0) out.push(<Menu.Item key={`${p.key}:repost`} leftSection={<IconSend size={14} />} onClick={() => setRepost(s.key)}>Re-post the sheet…</Menu.Item>);
      if (s.loot?.may_open) out.push(<Menu.Item key={`${p.key}:loot`} leftSection={<IconDiamond size={14} />} onClick={() => post(`/api/loot/${s.key}/open`)}>{L.loot}</Menu.Item>);
      else if (s.loot?.open) out.push(<Menu.Item key={`${p.key}:loot`} leftSection={<IconDiamond size={14} />} component={Link} to="/loot">{L.loot}</Menu.Item>);
      if ((s.fill_asks || []).length + (s.callouts || []).length > 0) {
        out.push(<Menu.Item key={`${p.key}:asks`} leftSection={<IconListCheck size={14} />} onClick={() => setAsks(s)}>Asks and callouts…</Menu.Item>);
      }
      if (locked) out.push(<Menu.Item key={`${p.key}:end`} color="red" leftSection={<IconFlagCheck size={14} />} disabled={!s.may_end} onClick={() => end(s, name)}>{L.end}{why(s.may_end ? null : s.end_note)}</Menu.Item>);
    } else if (s.state === "done" && (s.may_reopen || s.reopen_note)) {
      // an END by mistake goes back to locked within the hour (rc.reopen_refusal); a cancelled event never reopens
      out.push(<Menu.Item key={`${p.key}:reopen`} leftSection={<IconRotate size={14} />} disabled={!s.may_reopen} onClick={() => post(`/api/run/${s.key}/reopen`)}>{L.reopen}{why(s.may_reopen ? null : s.reopen_note)}</Menu.Item>);
    }
    if (!out.length) return [];
    return [...(several ? [<Menu.Label key={`${p.key}:label`}>{p.raid_name}</Menu.Label>] : []), ...out];
  };
  const all = parts.flatMap(items);
  if (!all.length) return null;

  return (
    <>
      <Menu shadow="md" width={280} position="bottom-end" classNames={{ item: css.menuItem }} withinPortal>
        {/* compact, so the header keeps the mockup's one row: Reschedule · Edit lineup · Lock now, then this */}
        <Menu.Target><Tooltip label="More" withinPortal><ActionIcon variant="default" size={36} loading={busy} aria-label="More"><IconDots size={18} /></ActionIcon></Tooltip></Menu.Target>
        <Menu.Dropdown>{all}</Menu.Dropdown>
      </Menu>
      {fill && <FillModal e={fill as unknown as Sheet} meta={meta} opened onClose={() => setFill(null)} onSent={async () => { onChanged(); }} />}
      <RepostSheetModal run={repost || ""} opened={!!repost} onClose={() => setRepost(null)} onDone={onChanged} />
      <Modal opened={!!asks} onClose={() => setAsks(null)} title={asks ? `Asks and callouts · ${several ? ev.parts.find((p) => p.key === asks.key)?.raid_name || ev.title : ev.title}` : ""} centered size="lg">
        {asks && <Detail e={{ ...asks, log_rows: [], log: [] }} meta={meta} busy={busy ? "withdraw" : null} open
          onAct={async (_k, path, payload) => { await post(path, payload); setAsks(null); }} />}
      </Modal>
      {dialog}
    </>
  );
}
