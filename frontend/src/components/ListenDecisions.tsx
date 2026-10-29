import { useEffect, useState } from "react";
import { Badge, Box, Card, Group, Progress, Text, Tooltip } from "@mantine/core";
import { api } from "../api";
import { CardHeader, fail } from "./Page";
import { useLive } from "../hooks/useLive";
import type { PlainPermissionsData } from "./PlainPermissions";
import css from "./plain.module.css";

/** What the bot did with a message it read, in words (discord_bot.ListenMixin's `did`). */
const DID: Record<string, string> = {
  replied: "Replied", asked: "Asked with 👀", quiet: "Stayed quiet",
  "would reply": "Would have replied", "would ask": "Would have asked", skipped: "Skipped",
};
const DID_WHY: Record<string, string> = { skipped: "over the hourly limit of decisions for this server" };
const LISTEN_COLOUR: Record<string, string> = { watch: "gray", reply: "teal" };

/** The Ops page's Listening tab (design §5.59): which channels the bot listens in, and Recent decisions — every untagged
 *  message it read there, newest first, how likely it was for the bot and what the bot did. Officers read; the channels
 *  are set on the Plain-text permissions tab. Reloads on the `listen` live topic. */
export function ListenDecisions() {
  const [data, setData] = useState<PlainPermissionsData | null>(null);
  const load = () => api.get<PlainPermissionsData>("/api/plain-permissions").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["listen", "config"]);
  if (!data) return null;
  const words = data.listen_words || { watch: "Watches", reply: "Replies" };
  const listening = Object.entries(data.listen || {}).map(([id, mode]) => ({ id, mode, name: data.channels.find((c) => c.id === id)?.name }));
  const recent = data.listen_recent || [];
  return (
    <Card id="listening">
      <CardHeader title="Listening" hint="what the bot read in the channels it listens in, and what it did" />
      <Box p="md" pt="sm">
        {listening.length === 0 ? (
          <Text size="sm" c="dimmed">The bot listens in no channel yet. On the Plain-text permissions tab, the owner sets a channel's Listening to Watches or Replies.</Text>
        ) : (
          <Group gap={6} align="center">
            <Text size="sm" c="dimmed">Listening in</Text>
            {listening.map((l) => <Badge key={l.id} variant="light" color={LISTEN_COLOUR[l.mode] || "gray"}>#{l.name || "a channel the bot can't see"} · {words[l.mode] || l.mode}</Badge>)}
            <Text size="xs" c="dimmed">Watches only notes what it would have answered. Set on the Plain-text permissions tab.</Text>
          </Group>
        )}
        {data.listen_ready === false && listening.length > 0 && <Text size="xs" c="dimmed" mt={4}>Listening needs the host's TypeSafe key.</Text>}
      </Box>
      <Box pb="md">
        <Text fw={600} size="sm" px="md">Recent decisions</Text>
        <Text size="xs" c="dimmed" px="md" mb="xs">The last messages the bot read in a listening channel, newest first, and what it did with each. Sure from 80%, unsure from 50%; the list keeps the last 50 and starts again when the bot restarts.</Text>
        {recent.length === 0 ? (
          <Text size="sm" c="dimmed" px="md">No messages read yet. Each untagged message in a listening channel shows here with what the bot did.</Text>
        ) : (
          <div>
            <div className={css.head}>
              <Text size="xs" c="dimmed">When</Text><Text size="xs" c="dimmed">Channel</Text><Text size="xs" c="dimmed">Who wrote</Text>
              <Text size="xs" c="dimmed">Message</Text><Text size="xs" c="dimmed">Likely for the bot</Text><Text size="xs" c="dimmed">What the bot did</Text>
            </div>
            {recent.map((r, i) => (
              <div key={`${r.when}-${i}`} className={css.row}>
                <div className={css.meta}>
                  <Text size="xs" c="dimmed">{r.when}</Text>
                  <Text size="xs">{r.channel_name ? `#${r.channel_name}` : "a channel the bot can't see"}</Text>
                  <Text size="xs" fw={600}>{r.author}</Text>
                </div>
                <Text size="sm" className={css.text}>{r.text}</Text>
                <div className={css.bar}>
                  {r.p === null ? <Text size="xs" c="dimmed">not judged</Text> : <>
                    <Progress value={Math.round(r.p * 100)} size="sm" color={r.p >= 0.8 ? "teal" : r.p >= 0.5 ? "blue" : "gray"} aria-label={`${Math.round(r.p * 100)}% likely for the bot`} />
                    <Text size="xs" w={34} ta="right">{Math.round(r.p * 100)}%</Text>
                  </>}
                </div>
                <div className={css.did}>
                  {DID_WHY[r.did]
                    ? <Tooltip label={DID_WHY[r.did]}><Text size="sm" span>{DID[r.did] || r.did}</Text></Tooltip>
                    : <Text size="sm">{DID[r.did] || r.did}</Text>}
                </div>
              </div>
            ))}
          </div>
        )}
      </Box>
    </Card>
  );
}
