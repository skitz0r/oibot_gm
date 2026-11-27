import { useState } from "react";
import { Anchor, Badge, Box, Button, Group, Select, Stack, Text } from "@mantine/core";
import { Link } from "react-router-dom";
import { api, type Meta, type MySheet } from "../api";
import type { ConfirmOpts, ConfirmResult } from "./ConfirmModal";
import { guardClick } from "./LeaveGuard";
import { fail, ok } from "./Page";

/** One live sheet, answerable here (design B1) — the Me page's My sheets row and the members' event page's Your answer
 *  (`link` makes the raid line a link to that event's page): Join · Bench · No thanks while the sheet is open (the current answer filled), a
 *  character select when you have more than one, and — on a locked run you joined or are rostered on — "Can't make it…" with an
 *  optional note for the officers. Rendered from the server's state only: every button is disabled while a press is in flight,
 *  nothing flips before the reply, the reply's receipt is the toast and the page reloads (two devices: the last press wins). An
 *  ad hoc run you joined asks once before a change away from Join (the queue). */
export function MyAnswer({ s, characters, meta, ask, onDone, flash, link }: {
  s: MySheet; characters: { label: string; is_main: boolean }[]; meta: Meta; ask: (o: ConfirmOpts) => Promise<ConfirmResult>; onDone: () => void; flash?: boolean; link?: boolean;
}) {
  const chars = characters;  // the active ones (planned by label), main first on the server
  const main = chars.find((c) => c.is_main) || chars[0];
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const character = pick || s.character || main?.label || null;
  const title = `${s.raid}${s.then ? `, then ${s.then}` : ""}${s.lead_raid ? ` · after ${s.lead_raid}` : ""}${s.kind === "adhoc" ? " · one-off" : ""}`;
  const href = `/events/${encodeURIComponent(s.key)}`;
  async function answer(status: string) {
    if (s.queue_note && status !== "in" && !(await ask({ title: `${meta.labels[status] || status} on ${s.raid}?`, message: s.queue_note, confirmLabel: meta.labels[status] || status }))) return;
    setBusy(status);
    try {
      const r = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/answer`, { status, character: chars.length > 1 ? character : null });
      ok(r.message);
    } catch (e) { fail(e); } finally { setBusy(null); setPick(null); onDone(); }
  }
  async function cant() {
    const r = await ask({ title: `Can't make ${s.raid} · ${s.when}?`, message: s.cant_consequence || "",
      confirmLabel: "Can't make it", color: "red", note: { label: "Note for the officers (optional — never on the public sheet)", maxLength: 300 } });
    if (!r) return;
    setBusy("cant");
    try {
      const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/out`, { note: typeof r === "object" ? r.note : "" });
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  // a follow-on (design §5.62): leave after the lead, or take it back
  async function leave() {
    const r = await ask({ title: `Leaving after ${s.lead_raid}?`, message: s.leave_consequence || "", confirmLabel: "Leaving early", color: "red",
      note: { label: "Note for the officers (optional — never on the public sheet)", maxLength: 300 } });
    if (!r) return;
    setBusy("leave");
    try { const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/out`, { note: typeof r === "object" ? r.note : "" }); ok(x.message); }
    catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  async function stay() {
    setBusy("stay");
    try { const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/stay`, {}); ok(x.message); }
    catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  return (
    <Stack id={`run-${s.key}`} gap={4} py={4} px={flash ? 6 : 0} style={{ borderBottom: "1px solid var(--mantine-color-slate-6)", scrollMarginTop: 72, borderRadius: 8, transition: "background 1.5s, box-shadow 1.5s",
      background: flash ? "var(--mantine-color-slate-7)" : undefined, boxShadow: flash ? "0 0 0 2px var(--mantine-color-teal-5)" : undefined }}>
      <Group justify="space-between" wrap="nowrap">
        <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{link ? <Anchor component={Link} to={href} onClick={guardClick(href)} inherit>{title}</Anchor> : title}</Text><Text size="xs" c="dimmed">{s.when}{s.state_word ? ` · ${s.state_word}` : ""}{s.character ? ` · ${s.character}` : ""}{s.regular && !s.status ? " · you're a regular: answer it" : ""}</Text></Box>
        {/* the badge's words are the server's (rc.answer_word): "not rostered" for a joiner the lock left out, never "Join" on a locked run */}
        <Badge variant="light" tt="none" style={{ flex: "none" }} color={s.rostered ? "teal" : s.state !== "open" && s.status === "in" ? "gray" : s.status === "in" ? "teal" : s.status === "out" ? "red" : s.status === "sub" ? "yellow" : "gray"}>{s.badge || s.label || "not answered"}</Badge>
      </Group>
      {s.may_answer && (
        <Group gap={6} wrap="wrap">
          {chars.length > 1 && (
            <Select size="xs" w={150} aria-label="as which character" allowDeselect={false} value={character} disabled={busy !== null}
              data={chars.map((c) => ({ value: c.label, label: `${c.label}${c.is_main ? " (main)" : ""}` }))} onChange={(v) => setPick(v)} />
          )}
          {meta.statuses.map((st) => (
            <Button key={st} size="compact-sm" variant={s.status === st && (!pick || pick === s.character) ? "filled" : "default"} color={st === "out" ? "red" : st === "sub" ? "yellow" : "teal"}
              loading={busy === st} disabled={busy !== null || (s.status === st && (!pick || pick === s.character))} onClick={() => answer(st)}>{meta.labels[st] || st}</Button>
          ))}
        </Group>
      )}
      {s.may_cant && <Group gap={6}><Button size="compact-sm" variant="subtle" color="red" loading={busy === "cant"} disabled={busy !== null} onClick={cant}>Can't make it…</Button></Group>}
      {s.may_leave && <Group gap={6}><Button size="compact-sm" variant="subtle" color="red" loading={busy === "leave"} disabled={busy !== null} onClick={leave}>Leaving after {s.lead_raid}…</Button></Group>}
      {s.may_stay && <Group gap={6}><Button size="compact-sm" variant="subtle" color="teal" loading={busy === "stay"} disabled={busy !== null} onClick={stay}>I'm staying</Button></Group>}
      {s.plays && <Text size="xs">{s.plays}</Text>}
      {s.receipt && s.status && <Text size="xs" c="dimmed">{s.receipt}</Text>}
    </Stack>
  );
}

