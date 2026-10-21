import { useEffect, useState } from "react";
import { Anchor, Badge, Box, Button, Card, Code, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import { api } from "../api";
import { useLive } from "../hooks/useLive";
import type { SetupData } from "../setupApi";
import { CardHeader, fail, ok } from "./Page";

const MARK: Record<string, { c: string; t: string }> = { ok: { c: "teal", t: "✓" }, warn: { c: "yellow", t: "!" }, bad: { c: "red", t: "✗" } };

/** The MCP status the Agents API reports (design §3.5 M-01: configured, local-only, token last used) — rendered as given. */
type McpStatus = Record<string, unknown> | null;

/** Config › Setup (design §3.5 O-01, S-05): the checklist — each row's ✓ computed by the bot with the test it runs at
 *  runtime (hover names it): the owner, every channel (perms.missing_for), officer roles (none = amber "Manage Server
 *  only"), the server-wide permissions, the optional ones, the error alerts — plus the invite's state and "Open in
 *  Discord". For a host superuser, the Host card: the bot's invite link, servers it is in that aren't guilds yet, parked
 *  guilds (the bot was removed from their server; nothing deleted) and the MCP status from the Agents API. */
export function SetupCard() {
  const [data, setData] = useState<SetupData | null>(null);
  const [mcp, setMcp] = useState<McpStatus>(null);
  const load = () => api.get<SetupData>("/api/admin/setup").then((d) => {
    setData(d);
    if (d.host) api.get<{ mcp?: McpStatus }>("/api/agents").then((a) => setMcp(a.mcp ?? null)).catch(() => setMcp(null));
  }).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["config", "ops"]);
  if (!data) return null;
  const bad = data.checklist.filter((r) => r.state === "bad").length;
  const inviteColour = { ok: "teal", dead: "red", none: "gray", unknown: "gray" }[data.invite.state];
  return (
    <>
      <Card>
        <CardHeader title="Setup" hint="each check is the one the bot runs before it posts"
          action={<Group gap="xs">
            {bad > 0 ? <Badge color="red" variant="light">{bad} to fix</Badge> : <Badge color="teal" variant="light">ready</Badge>}
            {data.links.server && <Anchor size="xs" href={data.links.server} target="_blank" rel="noreferrer">Open in Discord</Anchor>}
          </Group>} />
        <Table verticalSpacing={4}>
          <Table.Tbody>
            {data.checklist.map((r) => (
              <Table.Tr key={r.key}>
                <Table.Td w={28}><Tooltip label={`checked: ${r.test}`}><Text fw={700} c={MARK[r.state].c} aria-label={r.state}>{MARK[r.state].t}</Text></Tooltip></Table.Td>
                <Table.Td w={190}><Text size="sm">{r.label}</Text></Table.Td>
                <Table.Td><Text size="sm" c={r.state === "ok" ? undefined : MARK[r.state].c}>{r.text}</Text></Table.Td>
              </Table.Tr>
            ))}
            <Table.Tr>
              <Table.Td><Text fw={700} c={inviteColour}>{data.invite.state === "ok" ? "✓" : data.invite.state === "dead" ? "✗" : "·"}</Text></Table.Td>
              <Table.Td><Text size="sm">Homepage invite</Text></Table.Td>
              <Table.Td><Text size="sm" c={data.invite.state === "dead" ? "red" : undefined}>{data.invite.text}</Text></Table.Td>
            </Table.Tr>
          </Table.Tbody>
        </Table>
        <Text size="xs" c="dimmed" px="md" py="xs">
          Error alerts go to {data.errors.words}: one DM per distinct error per 30 minutes
          {data.errors.suppressed > 0 ? ` · ${data.errors.suppressed} more folded into earlier alerts since the bot started` : ""}
          {data.errors.ops_failing > 0 ? ` · the last ${data.errors.ops_failing} posts to the ops channel failed` : ""}. Change who with <Code>/gm config errors-to</Code>.
        </Text>
      </Card>
      {data.host && (
        <Card>
          <CardHeader title="Host" hint="superusers only: the bot itself" />
          <Stack p="md" gap="sm">
            {data.host.invite_link ? (
              <Group gap="xs" wrap="nowrap">
                <Text size="sm" style={{ flex: "none" }}>Invite link</Text>
                <Code style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{data.host.invite_link}</Code>
                <Button size="compact-xs" variant="default" onClick={() => { navigator.clipboard?.writeText(data.host!.invite_link!); ok("Copied"); }}>Copy</Button>
              </Group>
            ) : <Text size="sm" c="dimmed">No invite link (DISCORD_CLIENT_ID isn't set on the host).</Text>}
            <Box>
              <Text size="sm" fw={600}>Servers that aren't guilds yet</Text>
              {data.host.unclaimed.length === 0 ? <Text size="sm" c="dimmed">None — every server the bot is in is a guild.</Text>
                : data.host.unclaimed.map((g) => <Text key={g.id} size="sm">{g.name} — <Code>/gm config owner</Code> there claims it{g.missing.length ? ` · missing ${g.missing.join(", ")}` : ""}</Text>)}
            </Box>
            <Box>
              <Text size="sm" fw={600}>Parked guilds</Text>
              {data.host.detached.length === 0 ? <Text size="sm" c="dimmed">None.</Text>
                : data.host.detached.map((g) => <Text key={g.key} size="sm">{g.name} — the bot was removed from its server{g.detached ? ` (${g.detached})` : ""}; invite it back and it is served again (nothing was deleted)</Text>)}
            </Box>
            {mcp && (
              <Box>
                <Text size="sm" fw={600}>MCP</Text>
                {Object.entries(mcp).map(([k, v]) => <Text key={k} size="sm"><Text span c="dimmed">{k.replace(/_/g, " ")}: </Text>{typeof v === "boolean" ? (v ? "yes" : "no") : String(v ?? "—")}</Text>)}
              </Box>
            )}
          </Stack>
        </Card>
      )}
    </>
  );
}
