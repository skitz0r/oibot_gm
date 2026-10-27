import { useEffect, useState } from "react";
import { ActionIcon, Button, Group, Loader, Menu, Modal, Select, Stack, Text } from "@mantine/core";
import { IconDotsVertical, IconPin, IconPinnedOff, IconUserOff, IconUserPlus } from "@tabler/icons-react";
import { api, type MemberAction, type MemberActions, type Person } from "../../api";
import { fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";

/** Officers record anything a member can do on a run, for them (design B5) — the SAME filtered list the Discord SetWizard
 *  shows (GET /api/admin/actions → rc.member_actions): their open Confirm ask, their fill ask, Record a callout… (with a note),
 *  Join "as <alt>", Bench, No thanks, the pins. Each pick shows the server's consequence sentence (who is told and how — a
 *  notice, their open DM card edited; never a new DM) and writes through POST /api/admin/action with the list's `rev`: a
 *  member who changed meanwhile is a 409 naming what they changed to, nothing written. */

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

const DANGER = new Set(["callout", "confirm_no", "fill_no", "no_thanks"]);
const ICON: Record<string, React.ReactNode> = { pin_in: <IconPin size={14} />, pin_out: <IconUserOff size={14} />, unpin: <IconPinnedOff size={14} />, join: <IconUserPlus size={14} /> };

/** Pick → the consequence (+ the note box for a callout) → the write. Shared by the row menu and "Answer for someone…". */
function useRecord(run: string, onDone: () => Promise<void> | void) {
  const [ask, dialog] = useConfirm();
  async function record(uid: string, name: string, list: MemberActions, it: Item) {
    const consequence = (it.character && it.a.by_character?.[it.character]) || it.a.consequence;
    const r = await ask({ title: `${it.label} — ${name}?`, message: consequence, confirmLabel: it.a.key === "callout" ? "Record the callout" : it.label.replace(/…$/, ""),
      color: DANGER.has(it.a.key) ? "red" : undefined, note: it.a.needs === "note" ? { label: "Note for the officers (optional — never on the public sheet)", maxLength: 300 } : undefined });
    if (!r) return;
    try {
      const x = await api.post<{ message: string }>("/api/admin/action", { run, member: uid, action: it.a.key, character: it.character, note: typeof r === "object" ? r.note : null, rev: list.rev });
      ok(x.message);
    } catch (e) { fail(e); } finally { await onDone(); }
  }
  return [record, dialog] as const;
}

/** The row ⋮ menu on a sheet (joined, Bench, No thanks chips and the not-answered rows): fetched when opened, so it is
 *  always the member's state now. */
export function MemberMenu({ run, uid, name, onDone }: { run: string; uid: string; name: string; onDone: () => Promise<void> | void }) {
  const [list, setList] = useState<MemberActions | null>(null);
  const [record, dialog] = useRecord(run, onDone);
  const load = () => { setList(null); api.get<MemberActions>(`/api/admin/actions?run=${encodeURIComponent(run)}&member=${encodeURIComponent(uid)}`).then(setList).catch(fail); };
  return (
    <>
      <Menu shadow="md" width={240} position="bottom-end" onOpen={load}>
        <Menu.Target><ActionIcon variant="subtle" color="gray" size="sm" aria-label={`actions for ${name}`}><IconDotsVertical size={14} /></ActionIcon></Menu.Target>
        <Menu.Dropdown>
          <Menu.Label>{name}{list ? ` · ${list.now}` : ""}</Menu.Label>
          {!list && <Group justify="center" p="xs"><Loader size="xs" /></Group>}
          {list && list.actions.length === 0 && <Menu.Item disabled>Nothing applies on this run</Menu.Item>}
          {list && items(list.actions).map((it) => (
            <Menu.Item key={`${it.a.key}:${it.character || ""}`} leftSection={ICON[it.a.key]} color={DANGER.has(it.a.key) ? "red" : undefined}
              onClick={() => record(uid, name, list, it)}>{it.label}</Menu.Item>
          ))}
        </Menu.Dropdown>
      </Menu>
      {dialog}
    </>
  );
}

/** "Answer for someone…" beside the joiners: a member who isn't on the sheet (or anyone) → the same filtered list as buttons. */
export function SomeoneElse({ run, people, onDone }: { run: string; people: Person[]; onDone: () => Promise<void> | void }) {
  const [open, setOpen] = useState(false);
  const [uid, setUid] = useState<string | null>(null);
  const [list, setList] = useState<MemberActions | null>(null);
  const close = () => { setOpen(false); setUid(null); setList(null); };
  const [record, dialog] = useRecord(run, async () => { close(); await onDone(); });
  useEffect(() => {
    if (!uid) { setList(null); return; }
    api.get<MemberActions>(`/api/admin/actions?run=${encodeURIComponent(run)}&member=${encodeURIComponent(uid)}`).then(setList).catch(fail);
  }, [uid, run]);
  const person = people.find((p) => p.uid === uid);
  return (
    <>
      <Button size="xs" variant="default" onClick={() => setOpen(true)}>Answer for someone…</Button>
      <Modal opened={open} onClose={close} title="Answer for someone" centered>
        <Stack gap="sm">
          <Select label="Member" placeholder="Pick a member" searchable value={uid} onChange={setUid}
            data={people.map((p) => ({ value: p.uid, label: p.display_name }))} nothingFoundMessage="nobody by that name" />
          {uid && !list && <Loader size="xs" />}
          {list && <Text size="xs" c="dimmed">Now: {list.now}</Text>}
          {list && list.actions.length === 0 && <Text size="sm" c="dimmed">Nothing applies to them on this run.</Text>}
          {list && (
            <Group gap="xs">
              {items(list.actions).map((it) => (
                <Button key={`${it.a.key}:${it.character || ""}`} size="xs" variant={DANGER.has(it.a.key) ? "light" : "default"} color={DANGER.has(it.a.key) ? "red" : undefined}
                  onClick={() => record(uid!, person?.display_name || list.display_name, list, it)}>{it.label}</Button>
              ))}
            </Group>
          )}
        </Stack>
      </Modal>
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
        <Text size="sm">The live sheet moves to this channel: its buttons and later updates follow it, and the old message says where it went.</Text>
        <Select label="Channel" placeholder={chans ? "Pick a channel" : "Loading…"} searchable data={chans || []} value={cid} onChange={setCid} />
        <Group justify="flex-end"><Button variant="default" size="sm" onClick={onClose}>Back</Button><Button size="sm" disabled={!cid} loading={busy} onClick={go}>Re-post here</Button></Group>
      </Stack>
    </Modal>
  );
}

