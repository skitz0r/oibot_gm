import { useEffect, useState } from "react";
import { Anchor, Badge, Box, Button, Card, Group, Stack, Text } from "@mantine/core";
import { api, type Meta } from "../api";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { GameIcon } from "../components/Icons";
import { useLive } from "../hooks/useLive";
import type { Application, ApplicationsData } from "../setupApi";

/** The officers' Applications page (owner decision D32; design §3.3 cluster D): the open applications oldest first with
 *  every answer in full (Discord's card cuts them under its embed limits), the earlier verdict on the same person
 *  ("Applied before: declined 12 Mar by Jonny — full on healers"), Accept / Decline through useConfirm with the
 *  consequence sentences FROM THE SERVER (the Discord decide screen prints the same) and an optional note to the
 *  applicant, then the last decided ones with who decided and what Accept did about the member role (D31). The application
 *  itself is made in Discord only (design §5.29: no web form). */
export function ApplicationsPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<ApplicationsData | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, dialog] = useConfirm();
  const load = () => api.get<ApplicationsData>("/api/applications").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["member", "applications"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;

  async function decide(a: Application, accept: boolean) {
    const lines = (accept ? a.consequence?.accept : a.consequence?.decline) || [];
    const r = await ask({ title: `${accept ? "Accept" : "Decline"} ${a.display_name}'s application?`, message: lines.join("\n"), confirmLabel: accept ? "Accept" : "Decline",
      color: accept ? "teal" : "red", note: { label: "Note to the applicant (optional — it goes into their DM)", maxLength: data?.note_max ?? 400 } });
    if (!r) return;
    setBusy(a.uid);
    try {
      const x = await api.post<{ message: string }>("/api/admin/application/decide", { uid: a.uid, accept, note: typeof r === "object" ? r.note : "" });
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); load(); }
  }

  return (
    <Stack gap="lg">
      <PageTitle title="Applications" intro={`People apply in Discord (/apply or the registration card's Apply to join); you decide here or on the card. ${data.recruiting ? "Applications are open." : "Applications are closed right now — open ones stay open."}`} />
      <Card>
        <CardHeader title="Waiting" hint="oldest first" action={<Badge variant="light" color={data.open.length ? "yellow" : "gray"}>{data.open.length}</Badge>} />
        <Stack p="md" gap="md">
          {data.open.length === 0 && <Text size="sm" c="dimmed">No open applications.</Text>}
          {data.open.map((a) => <ApplicationBlock key={a.uid} a={a} meta={meta}
            actions={<Group gap="xs"><Button size="xs" color="teal" loading={busy === a.uid} disabled={busy !== null} onClick={() => decide(a, true)}>Accept…</Button>
              <Button size="xs" variant="default" disabled={busy !== null} onClick={() => decide(a, false)}>Decline…</Button></Group>} />)}
        </Stack>
      </Card>
      <Card>
        <CardHeader title="Decided" hint="the last 20, newest first" />
        <Stack p="md" gap="md">
          {data.decided.length === 0 && <Text size="sm" c="dimmed">Nothing decided yet.</Text>}
          {data.decided.map((a) => <ApplicationBlock key={a.uid} a={a} meta={meta} compact />)}
        </Stack>
      </Card>
      {dialog}
    </Stack>
  );
}

function ApplicationBlock({ a, meta, actions, compact }: { a: Application; meta: Meta; actions?: React.ReactNode; compact?: boolean }) {
  const colour = { open: "yellow", accepted: "teal", declined: "red", withdrawn: "gray" }[a.status];
  return (
    <Box p="sm" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          <GameIcon meta={meta} kind="spec" id={`${a.cls}:${a.spec}`} size={24} title={`${a.cls} ${a.spec}${a.offspec ? ` / ${a.offspec}` : ""}`} />
          <Box style={{ minWidth: 0 }}>
            <Text fw={600} truncate>{a.label}</Text>
            <Text size="xs" c="dimmed">{a.display_name} · applied {a.created}{a.decided ? ` · ${a.status} ${a.decided}${a.decided_by ? ` by ${a.decided_by}` : ""}` : ""}</Text>
          </Box>
        </Group>
        {actions || <Badge variant="light" color={colour}>{a.status}</Badge>}
      </Group>
      {a.prior && <Text size="xs" c="orange" mt={4}>{a.prior}</Text>}
      {a.note && <Text size="sm" mt={4}><Text span c="dimmed">Note: </Text>{a.note}</Text>}
      {a.role_note && <Text size="xs" c="dimmed" mt={2}>Member role: {a.role_note}</Text>}
      {!compact && (
        <Stack gap={6} mt="sm">
          {a.schedules.length > 0 && <Text size="sm"><Text span c="dimmed">Can raid: </Text>{a.schedules.join(" · ")}</Text>}
          {a.availability && <Text size="sm"><Text span c="dimmed">When they can raid: </Text>{a.availability}</Text>}
          {a.logs_url && <Text size="sm"><Text span c="dimmed">Logs: </Text><Anchor href={a.logs_url} target="_blank" rel="noreferrer">{a.logs_url}</Anchor></Text>}
          {a.answers.map((x, i) => (
            <Box key={i}><Text size="xs" c="dimmed" fw={600}>{x.label}</Text><Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{x.answer}</Text></Box>
          ))}
          {a.about && <Box><Text size="xs" c="dimmed" fw={600}>Anything else</Text><Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{a.about}</Text></Box>}
          {a.history.length > 1 && <Text size="xs" c="dimmed">Earlier: {a.history.map((h) => `${h.status}${h.when ? ` ${h.when}` : ""}${h.by ? ` by ${h.by}` : ""}`).join(" · ")}</Text>}
        </Stack>
      )}
    </Box>
  );
}
