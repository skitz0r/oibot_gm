import { useEffect, useState } from "react";
import { ActionIcon, Button, Group, Loader, Menu, Modal, Select, Stack, Text, Textarea } from "@mantine/core";
import { IconArrowsMove, IconDotsVertical, IconPin, IconPinnedOff, IconUserOff, IconUserPlus } from "@tabler/icons-react";
import { api, type Box, type MemberAction, type MemberActions, type Person } from "../../api";
import { fail, ok } from "../Page";
import { pageWords, useConfirmBox } from "../ConfirmBox";
import { boxOf } from "../events/shared";
import css from "./board.module.css";

/** Officers record anything a member can do on a run, for them (design B5) — the SAME filtered list the Discord SetWizard
 *  shows (GET /api/admin/actions → rc.member_actions): their open Confirm ask, their fill ask, Record a callout… (with a note),
 *  Join "as <alt>", Bench, No thanks, the pins. Each pick prints the SERVER's box (who is told and how — a notice, their
 *  open DM card edited; never a new DM) and writes through POST /api/admin/action with the box's fp as `seen`: a member
 *  who changed meanwhile brings the fresh box back, nothing written. */

type Item = { a: MemberAction; character: string | null; label: string };

function items(acts: MemberAction[]): Item[] {
  const out: Item[] = [];
  for (const a of acts) {
    const chars = a.characters || [];
    if (a.needs === "character" && chars.length > 1) chars.forEach((c) => out.push({ a, character: c, label: `${a.label} as ${c}` }));
    else out.push({ a, character: a.needs === "character" && chars.length === 1 ? chars[0] : null, label: a.label });
  }
  return out;
}

/** The switches go after Join (and Join as…) and before Bench, No thanks and the pins; at the end when none of those is offered. */
function withSwitches(list: Item[], switches: SwitchItem[]): (Item | SwitchItem)[] {
  if (!switches.length) return list;
  const at = list.findIndex((it) => AFTER_SWITCH.has(it.a.key));
  return at < 0 ? [...list, ...switches] : [...list.slice(0, at), ...switches, ...list.slice(at)];
}

const DANGER = new Set(["callout", "confirm_no", "fill_no", "no_thanks"]);
const ICON: Record<string, React.ReactNode> = { pin_in: <IconPin size={14} />, pin_out: <IconUserOff size={14} />, unpin: <IconPinnedOff size={14} />, join: <IconUserPlus size={14} /> };

/** A callout's note, inside the box: the officers' own words, never on the public sheet. `into` keeps what is typed. */
function NoteField({ into }: { into: { note: string } }) {
  const [note, setNote] = useState(into.note);
  return <Textarea label="Note for the officers (optional)" maxLength={300} autosize minRows={2} value={note}
    onChange={(e) => { into.note = e.currentTarget.value; setNote(e.currentTarget.value); }} />;
}

/** Pick → the SERVER's box (consequences.member_action_box through POST /api/admin/action `preview`; a callout adds
 *  its note field) → the write with the fp on screen as `seen`: a member who changed meanwhile brings the fresh box
 *  back (409), a pick that no longer applies is the refusal box (400). The live topics refresh an open box. */
function useRecord(run: string, onDone: () => Promise<void> | void) {
  const [ask, dialog] = useConfirmBox(["run:", "member"]);
  async function record(uid: string, it: Item) {
    const body = { run, member: uid, action: it.a.key, character: it.character };
    const preview = async (): Promise<Box> => {
      try { return (await api.post<{ box: Box }>("/api/admin/action", { ...body, preview: true })).box; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
    };
    let reload = false;
    try {
      const typed = { note: "" };
      let said = "";
      const done = await ask({
        box: await preview(),
        fields: it.a.key === "callout" ? <NoteField into={typed} /> : undefined,
        refresh: preview,
        act: async (fp) => {
          try { said = (await api.post<{ message: string }>("/api/admin/action", { ...body, note: typed.note.trim() || null, seen: fp })).message; return null; }
          catch (e) { const b = boxOf(e); if (b) return b; throw e; }
        },
      });
      if (done) ok(said);
      reload = done;
    } catch (e) { fail(e); reload = true; }  // a refusal the server sent without a box: the board reloads as after a press
    finally { if (reload) await onDone(); }
  }
  return [record, dialog] as const;
}

/** One "Move to…" destination in a board chip's menu (BoardView): a group, or Not placed / Not rostered. `onPick` is the
 *  board's own move — the same POST /api/run/{key}/move a drag sends, after the server's confirm sentence. */
export type MoveTarget = { key: string; label: string; disabled?: boolean; hint?: string; aria?: string; onPick: () => void };

/** One role switch in a Signups row's ⋮ (the Events page, SignupsSection): that member's Role switches row — its spec
 *  icon, the server's words for it and the section's own press (useSwitchPress). */
export type SwitchItem = { key: string; icon: React.ReactNode; label: string; onPick: () => void };
/** The menu's items a switch goes before: the member's answers after Join and the pins. */
const AFTER_SWITCH = new Set(["bench", "no_thanks", "pin_in", "pin_out", "unpin"]);

/** The row ⋮ menu on a sheet (joined, Bench, No thanks chips and the not-answered rows) and on a board chip: fetched when
 *  opened, so it is always the member's state now. `moves` (the board's chips): "Move to…" first — every group with room
 *  and Not placed / Not rostered — the board's tap-and-keyboard path (a drag needs a mouse; design §5.56 phase 3).
 *  `onAnswer` (the Events page): Join / Join as… / Bench / No thanks open the SERVER's answer box (useAnswerBox, the member
 *  fixed) with its fields. Callouts, pins and the confirm / fill answers ask in the server's box too (useRecord), and so
 *  does every pick on the Test bench, which passes no `onAnswer`. */
export function MemberMenu({ run, uid, name, onDone, moves, onAnswer, switches }: {
  run: string; uid: string | null | undefined; name: string; onDone: () => Promise<void> | void; moves?: MoveTarget[];
  onAnswer?: (action: string, character: string | null) => void; switches?: SwitchItem[];
}) {
  const [list, setList] = useState<MemberActions | null>(null);
  const [record, dialog] = useRecord(run, onDone);
  const load = () => { setList(null); if (uid) api.get<MemberActions>(`/api/admin/actions?run=${encodeURIComponent(run)}&member=${encodeURIComponent(uid)}`).then(setList).catch(fail); };
  return (
    <>
      <Menu shadow="md" width={240} position="bottom-end" onOpen={load} classNames={{ item: css.menuItem }}>
        <Menu.Target><ActionIcon variant="subtle" color="gray" size="sm" aria-label={`actions for ${name}`}><IconDotsVertical size={14} /></ActionIcon></Menu.Target>
        <Menu.Dropdown>
          {moves && moves.length > 0 && (
            <>
              <Menu.Label>Move {name} to…</Menu.Label>
              {moves.map((m) => (
                <Menu.Item key={m.key} leftSection={<IconArrowsMove size={14} />} disabled={m.disabled} aria-label={m.aria} rightSection={m.hint ? <Text size="xs" c="dimmed">{m.hint}</Text> : undefined} onClick={m.onPick}>{m.label}</Menu.Item>
              ))}
              {uid && <Menu.Divider />}
            </>
          )}
          {uid && <Menu.Label>{name}{list ? ` · ${list.now}` : ""}</Menu.Label>}
          {uid && !list && <Group justify="center" p="xs"><Loader size="xs" /></Group>}
          {uid && list && list.actions.length === 0 && !switches?.length && <Menu.Item disabled>{pageWords().nothing_applies}</Menu.Item>}
          {uid && list && withSwitches(items(list.actions), switches || []).map((it) => ("onPick" in it ? (
            <Menu.Item key={`switch:${it.key}`} leftSection={it.icon} onClick={it.onPick}>{it.label}</Menu.Item>
          ) : (
            <Menu.Item key={`${it.a.key}:${it.character || ""}`} leftSection={ICON[it.a.key]} color={DANGER.has(it.a.key) ? "red" : undefined}
              onClick={() => (onAnswer && ANSWERS.has(it.a.key) ? onAnswer(it.a.key, it.character) : record(uid, it))}>{it.label}</Menu.Item>
          )))}
        </Menu.Dropdown>
      </Menu>
      {dialog}
    </>
  );
}

/** Answer for someone (box 8, design §7b): a member with NO answer on the sheet yet (the owner, 2026-11-22: someone who
 *  answered is changed from their row's ⋮, never offered here — SheetCard filters `people`). The SERVER's box
 *  (consequences.answer_box through POST /api/admin/action `preview`) with its fields — Member, Answer, Character (a
 *  character and the spec it plays) —; each pick asks the server again and the box takes its answer; Record answer
 *  sends the fp of the box on screen as `seen`, so a member who answered meanwhile brings the fresh box back. */
type AnswerPick = { uid: string; action: string; character: string | null; spec: string | null };
const ANSWERS = new Set(["join", "bench", "no_thanks"]);
const answersOf = (list: MemberActions | null) => (list?.actions || []).filter((a) => ANSWERS.has(a.key));
const actionsOf = (run: string, uid: string) => api.get<MemberActions>(`/api/admin/actions?run=${encodeURIComponent(run)}&member=${encodeURIComponent(uid)}`);

/** The character choices for one answer: each character the server allows with each spec it may answer as — the
 *  server's `plays` (its spec, offspec and flex roles; else its spec alone) — "Tavi, Holy". */
function playsOf(person: Person | undefined, a: MemberAction | undefined) {
  if (!person || !a || a.key === "no_thanks") return [];
  const allowed = new Set(a.characters && a.characters.length ? a.characters : person.characters.map((c) => c.label));
  return person.characters.filter((c) => allowed.has(c.label)).flatMap((c) =>
    (c.plays && c.plays.length ? c.plays : [c.spec]).map((sp) => ({ value: `${c.label}|${sp}`, label: `${c.label}, ${sp}` })));
}

/** The pick a box opens on: Join (else the first answer) as `home` — a row's own character and spec — else the main. */
function defaultPick(person: Person, list: MemberActions, home?: { character: string; spec: string }): AnswerPick {
  const a = answersOf(list).find((x) => x.key === "join") || answersOf(list)[0];
  const main = person.characters.find((c) => c.is_main) || person.characters[0];
  const own = home || (main ? { character: main.label, spec: main.spec } : null);
  const plays = a && a.key !== "no_thanks" && own ? own : { character: null, spec: null };
  return { uid: person.uid, action: a?.key || "", ...plays };
}

function AnswerFields({ run, people, first, firstList, onPick, home }: {
  run: string; people: Person[]; first: AnswerPick; firstList: MemberActions; onPick: (p: AnswerPick) => void;
  home?: { character: string; spec: string };
}) {
  const [pick, setPick] = useState(first);
  const [list, setList] = useState<MemberActions | null>(firstList);
  const person = people.find((p) => p.uid === pick.uid);
  const answer = answersOf(list).find((a) => a.key === pick.action);
  const plays = playsOf(person, answer);
  const set = (p: AnswerPick) => { setPick(p); onPick(p); };
  async function member(uid: string | null) {
    const who = people.find((p) => p.uid === uid);
    if (!who) return;
    setList(null);
    try {
      const l = await actionsOf(run, who.uid);
      setList(l);
      set(defaultPick(who, l));
    } catch (e) { fail(e); }
  }
  return (
    <Stack gap="xs">
      <Select label="Member" searchable allowDeselect={false} disabled={people.length < 2} value={pick.uid} onChange={member} comboboxProps={{ withinPortal: true }}
        data={people.map((p) => ({ value: p.uid, label: p.display_name }))} nothingFoundMessage="Nobody without an answer has that name." />
      <Select label="Answer" allowDeselect={false} disabled={!list} value={pick.action || null} comboboxProps={{ withinPortal: true }}
        data={answersOf(list).map((a) => ({ value: a.key, label: a.label }))}
        onChange={(k) => {
          if (!k || !person || !list) return;
          const a = answersOf(list).find((x) => x.key === k);
          const keep = playsOf(person, a).some((o) => o.value === `${pick.character}|${pick.spec}`);
          const base = defaultPick(person, list, home);
          set({ ...pick, action: k, ...(k === "no_thanks" ? { character: null, spec: null } : keep ? {} : { character: base.character, spec: base.spec }) });
        }} />
      {plays.length > 0 && (
        <Select label="Character" allowDeselect={false} value={`${pick.character}|${pick.spec}`} data={plays} comboboxProps={{ withinPortal: true }}
          onChange={(v) => { if (v) { const [character, spec] = v.split("|"); set({ ...pick, character, spec }); } }} />
      )}
    </Stack>
  );
}

/** Where an answer box starts: whose (`person`), which answer (`action`, else Join) and which character and spec (else
 *  the main, as it plays). `home` = the row's own character and spec (an answered member keeps theirs). */
export type AnswerStart = { person: Person; action?: string; character?: string | null; spec?: string | null; home?: { character: string; spec: string } };

/** THE answer box (box 8): `open(people, start)` previews the server's box for `start` and asks with its fields; every
 *  pick asks again (`replace`), the live topics refresh it, Record answer sends the fp on screen as `seen` (a 409 = the
 *  fresh box). `people` = whom the Member field offers (one = the member is fixed: a row's ⋮). The hook's owner stays
 *  mounted while the box is open, so a live reload never takes the box away. `busy` ends as the box opens. */
export function useAnswerBox(run: string, onDone: () => Promise<void> | void): [(people: Person[], start: AnswerStart) => Promise<void>, React.ReactNode, boolean] {
  const [ask, dialog, replace] = useConfirmBox(["run:", "member"]);
  const [busy, setBusy] = useState(false);
  async function open(people: Person[], start: AnswerStart) {
    const person = start.person;
    setBusy(true);
    try {
      const list = await actionsOf(run, person.uid);
      const base = defaultPick(person, list, start.home);
      const now: AnswerPick = {  // the fields' current pick: the preview, the refresh and the write read it
        ...base, ...(start.action ? { action: start.action } : {}),
        ...(start.action === "no_thanks" ? { character: null, spec: null } : start.character ? { character: start.character, spec: start.spec ?? base.spec } : {}),
      };
      let seq = 0;
      const bodyOf = (p: AnswerPick) => ({ run, member: p.uid, action: p.action, character: p.character, ...(p.spec && p.action !== "no_thanks" ? { spec: p.spec } : {}) });
      const preview = async (p: AnswerPick): Promise<Box> => {
        try { return (await api.post<{ box: Box }>("/api/admin/action", { ...bodyOf(p), preview: true })).box; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      };
      const first = await preview(now);
      setBusy(false);  // the box is open now: the button behind it is not busy
      let said = "";
      const done = await ask({
        box: first,
        fields: <AnswerFields run={run} people={people} first={now} firstList={list} home={start.home}
          onPick={(p) => { Object.assign(now, p); const n = ++seq; preview(p).then((b) => { if (n === seq) replace(b); }).catch(fail); }} />,
        refresh: () => preview(now),
        act: async (fp) => {
          try { said = (await api.post<{ message: string }>("/api/admin/action", { ...bodyOf(now), seen: fp })).message; return null; }
          catch (e) { const b = boxOf(e); if (b) return b; throw e; }
        },
      });
      if (done) { ok(said); await onDone(); }
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  return [open, dialog, busy];
}

export function SomeoneElse({ run, people, onDone }: { run: string; people: Person[]; onDone: () => Promise<void> | void }) {
  const [open, dialog, busy] = useAnswerBox(run, onDone);
  return (
    <>
      <Button size="xs" variant="default" loading={busy} disabled={!people.length}
        onClick={() => { if (people[0]) open(people, { person: people[0] }); }}>Answer for someone</Button>
      {dialog}
    </>
  );
}

/** "Re-post sheet…" (D21, officers; the header menu's item opens it): the live sheet MOVES to the picked channel — its buttons
 *  and later refreshes follow it, the old message says where it went. Channels whose plain-text mode is ignore are not offered
 *  (the bot keeps quiet there; the server refuses them too). */
export function RepostSheetModal({ run, opened, onClose, onDone }: { run: string; opened: boolean; onClose: () => void; onDone: () => Promise<void> | void }) {
  const [chans, setChans] = useState<{ value: string; label: string }[] | null>(null);
  const [cid, setCid] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!opened || chans) return;
    api.get<{ channels: { id: string; name: string; mode: string }[] }>("/api/plain-permissions")
      .then((d) => setChans(d.channels.filter((c) => c.mode !== "ignore").map((c) => ({ value: c.id, label: `#${c.name}` })))).catch(fail);
  }, [opened, chans]);
  async function go() {
    setBusy(true);
    try { const r = await api.post<{ message: string }>(`/api/run/${encodeURIComponent(run)}/sheet`, { channel: cid }); ok(r.message); onClose(); await onDone(); }
    catch (e) { fail(e); } finally { setBusy(false); }
  }
  return (
    <Modal opened={opened} onClose={onClose} title="Re-post the sheet" centered>
      <Stack gap="sm">
        <Text size="sm">{pageWords().repost_sheet}</Text>
        <Select label="Channel" placeholder={chans ? "Pick a channel" : "Loading…"} searchable data={chans || []} value={cid} onChange={setCid} />
        <Group justify="flex-end"><Button variant="default" size="sm" onClick={onClose}>Back</Button><Button size="sm" disabled={!cid} loading={busy} onClick={go}>Re-post here</Button></Group>
      </Stack>
    </Modal>
  );
}

