import { useEffect, useState } from "react";
import { Badge, Box, Group, ScrollArea, Stack, Text } from "@mantine/core";
import { api, ApiError } from "../api";
import { useLive } from "../hooks/useLive";
import { Eyebrow } from "./Page";

/** `title` = the run in words from the server ('<raid> · <start>'), never its key. */
export interface FeedEvent { seq: number; at: string; at_label: string; kind: "card" | "dm" | "step" | "answer"; text: string; actor: string | null; run: string; title?: string }

const KIND: Record<FeedEvent["kind"], { label: string; color: string }> = {
  card: { label: "card", color: "blue" },
  dm: { label: "puppet DM", color: "grape" },
  step: { label: "step", color: "gray" },
  answer: { label: "answer", color: "teal" },
};

/** The test feed (D20): every card, puppet DM, scheduler step and answer of the guild's test runs as one stream, newest
 *  first — a rehearsal followed on the site while its cards post in Discord and the puppets' DMs reach the tester as a
 *  digest. Live: the server publishes `test:<run>` on every new event. Nothing is stored; a restart empties it. */
export function TestFeed({ run, height = 320 }: { run?: string; height?: number }) {
  const [events, setEvents] = useState<FeedEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);  // the feed is officers' only (403): hide it, never pretend it is empty
  const load = () => api.get<{ events: FeedEvent[] }>(`/api/test/feed${run ? `?run=${encodeURIComponent(run)}` : ""}`)
    .then((r) => { setEvents(r.events); setError(null); })
    .catch((e) => { if (e instanceof ApiError && (e.status === 403 || e.status === 401)) setHidden(true); else setError(e instanceof Error ? e.message : String(e)); });
  useEffect(() => { load(); }, [run]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["test:"]);
  const rows = [...(events || [])].reverse();
  const many = new Set(rows.map((e) => e.run)).size > 1;
  if (hidden) return null;
  return (
    <Box>
      <Eyebrow plain>Test feed</Eyebrow>
      {error && events === null ? <Text size="sm" c="red">Couldn't load the feed: {error}</Text> : events === null ? <Text size="sm" c="dimmed">Loading…</Text> : rows.length === 0 ? (
        <Text size="sm" c="dimmed">Nothing yet. Open a test run: its cards, the puppets' DMs and every step show up here as they happen.</Text>
      ) : (
        <ScrollArea.Autosize mah={height} type="auto" mt={4}>
          <Stack gap={4}>
            {rows.map((e) => (
              // wraps on a phone: the time and the kind on one line, the text under them when the row is narrow
              <Group key={e.seq} gap="4px 8px" wrap="wrap" align="flex-start">
                <Group gap={8} wrap="nowrap" style={{ flexShrink: 0 }}>
                  <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>{e.at_label}</Text>
                  <Badge size="xs" variant="light" tt="none" color={KIND[e.kind]?.color || "gray"}>{KIND[e.kind]?.label || e.kind}</Badge>
                </Group>
                <Text size="sm" style={{ whiteSpace: "pre-line", minWidth: 0, flex: "1 1 16rem" }}>{many && e.title && <Text span c="dimmed" size="xs">{e.title} · </Text>}{e.text}</Text>
              </Group>
            ))}
          </Stack>
        </ScrollArea.Autosize>
      )}
    </Box>
  );
}
