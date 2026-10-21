import { useEffect, useState } from "react";
import { Anchor, Badge, Box, Button, Card, Code, Group, MultiSelect, SegmentedControl, Stack, Table, Text, Tooltip } from "@mantine/core";
import { api } from "../api";
import { useLive } from "../hooks/useLive";
import type { SetupData } from "../setupApi";
import { CardHeader, fail, ok } from "./Page";
import { useOpsSave } from "./ReviewModal";

type AlertMode = "owner" | "people" | "nobody";

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
  // OPS-01: the owner's edit of who gets the error alerts (null = not editing) — saved through the structured ops path
  const [alerts, setAlerts] = useState<{ mode: AlertMode; ids: string[] } | null>(null);
  const [saving, setSaving] = useState(false);
  const [save, review] = useOpsSave();
  const load = () => api.get<SetupData>("/api/admin/setup").then((d) => {
    setData(d);
    if (d.host) api.get<{ mcp?: McpStatus }>("/api/agents").then((a) => setMcp(a.mcp ?? null)).catch(() => setMcp(null));
  }).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["config", "ops"]);
  if (!data) return null;
  const editAlerts = () => {
    const ids = data.errors.ids;
    setAlerts(ids === null ? { mode: "owner", ids: [] } : ids.length ? { mode: "people", ids: [...ids] } : { mode: "nobody", ids: [] });
  };
  async function saveAlerts() {
    if (!alerts || !data) return;
    const value = alerts.mode === "owner" ? null : alerts.mode === "nobody" ? [] : alerts.ids;
    setSaving(true);
    try {
      const r = await save([{ kind: "settings", fields: { error_dm_ids: value } }], { revs: { "config:settings": data.rev }, title: "Who gets the error alerts" });
      if (r === null) return;
      if (r.changed?.length) { fail(new Error("Someone changed the settings since you opened the page — reload and check.")); return; }
      setAlerts(null); load();
    } catch (e) { fail(e); } finally { setSaving(false); }
  }
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
        <Group gap="xs" px="md" py="xs" wrap="wrap" align="center">
          <Text size="xs" c="dimmed" style={{ flex: "1 1 260px" }}>
            Error alerts go to {data.errors.words}: one DM per distinct error per 30 minutes
            {data.errors.suppressed > 0 ? ` · ${data.errors.suppressed} more folded into earlier alerts since the bot started` : ""}
            {data.errors.ops_failing > 0 ? ` · the last ${data.errors.ops_failing} posts to the ops channel failed` : ""}. {data.owner ? "" : <>The owner changes who (<Code>/gm config errors-to</Code>).</>}
          </Text>
          {data.owner && alerts === null && <Button size="compact-xs" variant="default" onClick={editAlerts}>Change who</Button>}
        </Group>
        {alerts !== null && (
          <Stack gap="xs" px="md" pb="md">
            <SegmentedControl size="xs" value={alerts.mode} onChange={(m) => setAlerts({ ...alerts, mode: m as AlertMode })}
              data={[{ value: "owner", label: "The owner" }, { value: "people", label: "These people" }, { value: "nobody", label: "Nobody" }]} />
            {alerts.mode === "people" && (
              <MultiSelect size="xs" label="Who gets them" placeholder="pick people" searchable maw={520}
                data={data.errors.choices.map((c) => ({ value: c.id, label: c.name }))} value={alerts.ids} onChange={(ids) => setAlerts({ ...alerts, ids })} />
            )}
            {alerts.mode === "nobody" && <Text size="xs" c="dimmed">Errors stay in the ops channel only.</Text>}
            <Group gap="xs" justify="flex-end">
              <Button size="xs" variant="default" onClick={() => setAlerts(null)} disabled={saving}>Discard</Button>
              <Button size="xs" onClick={saveAlerts} loading={saving} disabled={alerts.mode === "people" && alerts.ids.length === 0}>Save</Button>
            </Group>
          </Stack>
        )}
        {review}
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
