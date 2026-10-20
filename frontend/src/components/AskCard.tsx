import { useState } from "react";
import { Box, Button, Card, Group, Stack, Text, Tooltip } from "@mantine/core";
import { api, type Meta, type MyAsk, type Notice } from "../api";
import { classColour } from "../theme";
import { GameIcon } from "./Icons";
import { CardHeader, fail, ok } from "./Page";

/** One open ask on the Me page (design B2): what it is, the run, "as Holy" (the spec icon alone, its tooltip says it), the group it would put you in (spec icon alone, one
 *  member per line), the deadline in words, and the member's own two buttons. Rendered from the server's state only: both buttons
 *  are disabled while a press is in flight and nothing flips before the answer comes back (no optimistic state) — a refusal
 *  ("Already answered (yes, by Kestrel at 5:12 PM).", "the seat was released") is shown as the server wrote it and the page reloads. */
export function AskCard({ a, meta, onDone }: { a: MyAsk; meta: Meta; onDone: () => void }) {
  const [busy, setBusy] = useState<"yes" | "no" | null>(null);
  const confirm = a.kind === "confirm";
  const yesLabel = confirm ? "Confirm" : a.kind === "swap" ? "Yes, switch" : "Yes, I'll come";
  const noLabel = confirm ? "Can't make it" : "No thanks";
  async function answer(yes: boolean) {
    setBusy(yes ? "yes" : "no");
    try {
      const r = confirm
        ? await api.post<{ message: string }>(`/api/me/ask/${encodeURIComponent(a.id)}`, { answer: yes ? "yes" : "no" })
        : await api.post<{ message: string }>(`/api/me/fill/${encodeURIComponent(a.run)}`, { answer: yes ? "yes" : "no" });
      ok(r.message);
    } catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  return (
    <Card style={{ borderColor: "var(--mantine-color-teal-4)" }}>
      <Group p="md" justify="space-between" align="flex-start" wrap="wrap" gap="md">
        <Box style={{ minWidth: 0, flex: "1 1 320px" }}>
          <Text fw={700}>{a.title}</Text>
          <Group gap={6} mt={2} wrap="nowrap">
            {a.cls && a.spec && <GameIcon meta={meta} kind="spec" id={`${a.cls}:${a.spec}`} size={20} title={`${a.as_label || a.spec}${a.role ? ` · ${a.role}` : ""}`} />}
            {/* the spec icon alone says "as Holy" (iconography rule: no text label beside it; the tooltip carries the words);
                the words show only when there is no icon to carry them */}
            <Text size="sm"><b>{a.raid}</b> · {a.when} · <Text span c={a.cls ? classColour(meta, a.cls) : undefined} fw={600}>{a.character}</Text>{!(a.cls && a.spec) && a.as_label ? ` ${a.as_label}` : ""}</Text>
          </Group>
          {a.reason && !confirm && <Text size="xs" c="dimmed" mt={4}>{a.reason}</Text>}
          {a.group && a.group.members.length > 0 && (
            <Box mt={6}>
              <Text size="xs" c="dimmed">{a.group.rosters > 1 ? `Roster ${a.group.roster} · ` : ""}Group {a.group.group}</Text>
              {a.group.members.map((p) => (
                <Group key={p.character} gap={6} wrap="nowrap" style={{ lineHeight: 1.7 }}>
                  <GameIcon meta={meta} kind="spec" id={`${p.cls}:${p.spec}`} size={16} title={`${p.spec} · ${p.role}`} />
                  <Text size="sm" fw={p.me ? 700 : 500} c={classColour(meta, p.cls)}>{p.character}</Text>
                </Group>
              ))}
            </Box>
          )}
          {a.held
            ? <Text size="xs" c="yellow" mt={4}>You said yes — waiting for {a.waiting_for || "the other half"}'s answer; nothing changes until both say yes.</Text>
            : <Text size="xs" c="dimmed" mt={4}>{a.deadline ? `Answer by ${a.deadline}` : "Answer when you can"}{a.channel === "web" ? " · this ask waits here (no DM)" : " · also in your DMs"}</Text>}
        </Box>
        {!a.held && (
          <Group gap="xs">
            <Button size="xs" loading={busy === "yes"} disabled={busy !== null} onClick={() => answer(true)}>{yesLabel}</Button>
            <Button size="xs" variant="default" loading={busy === "no"} disabled={busy !== null} onClick={() => answer(false)}>{noLabel}</Button>
          </Group>
        )}
      </Group>
    </Card>
  );
}

/** The inbox's notices (D26): outcomes that are never a DM — not rostered, a seat released, answered for you, a run cancelled,
 *  a schedule removed, an application decision, a loot award. Unread ones are bold; "Mark read" marks them all (or one). */
export function NoticesCard({ notices, onDone }: { notices: Notice[]; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  if (!notices.length) return null;
  const unread = notices.filter((n) => !n.read);
  async function mark(ids?: string[]) {
    setBusy(true);
    try { const r = await api.post<{ message: string }>("/api/me/notices/read", ids ? { ids } : {}); ok(r.message); } catch (e) { fail(e); } finally { setBusy(false); onDone(); }
  }
  return (
    <Card>
      <CardHeader title="Notices" hint={unread.length ? `${unread.length} new` : "nothing new"}
        action={unread.length > 0 ? <Button size="xs" variant="subtle" loading={busy} onClick={() => mark()}>Mark all read</Button> : null} />
      <Stack p="md" gap={6}>
        {notices.map((n) => (
          <Group key={n.id} justify="space-between" wrap="nowrap" align="flex-start" gap="sm">
            <Box style={{ minWidth: 0 }}>
              <Text size="sm" fw={n.read ? 400 : 600} c={n.read ? "dimmed" : undefined}>{n.text}</Text>
              <Text size="xs" c="dimmed">{n.when}</Text>
            </Box>
            {!n.read && <Tooltip label="mark read"><Button size="compact-xs" variant="subtle" color="gray" disabled={busy} onClick={() => mark([n.id])}>Read</Button></Tooltip>}
          </Group>
        ))}
      </Stack>
    </Card>
  );
}
