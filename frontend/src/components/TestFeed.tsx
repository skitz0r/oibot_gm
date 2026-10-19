import { useEffect, useState } from "react";
import { Badge, Box, Group, ScrollArea, Stack, Text } from "@mantine/core";
import { api } from "../api";
import { useLive } from "../hooks/useLive";
import { Eyebrow } from "./Page";

export interface FeedEvent { seq: number; at: string; at_label: string; kind: "card" | "dm" | "step" | "answer"; text: string; actor: string | null; run: string }

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
  const load = () => api.get<{ events: FeedEvent[] }>(`/api/test/feed${run ? `?run=${encodeURIComponent(run)}` : ""}`).then((r) => setEvents(r.events)).catch(() => setEvents([]));
  useEffect(() => { load(); }, [run]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["test:"]);
  const rows = [...(events || [])].reverse();
  const many = new Set(rows.map((e) => e.run)).size > 1;
  return (
    <Box>
      <Eyebrow>Test feed</Eyebrow>
      {events === null ? <Text size="sm" c="dimmed">Loading…</Text> : rows.length === 0 ? (
        <Text size="sm" c="dimmed">Nothing yet. Open a test run: its cards, the puppets' DMs and every step show up here as they happen.</Text>
      ) : (
        <ScrollArea.Autosize mah={height} type="auto" mt={4}>
          <Stack gap={4}>
            {rows.map((e) => (
              <Group key={e.seq} gap={8} wrap="nowrap" align="flex-start">
                <Text size="xs" c="dimmed" w={150} style={{ flexShrink: 0, fontVariantNumeric: "tabular-nums" }}>{e.at_label}</Text>
                <Badge size="xs" variant="light" color={KIND[e.kind]?.color || "gray"} w={78} style={{ flexShrink: 0 }}>{KIND[e.kind]?.label || e.kind}</Badge>
                <Text size="sm" style={{ whiteSpace: "pre-line", minWidth: 0 }}>{many && <Text span c="dimmed" size="xs">{e.run} · </Text>}{e.text}</Text>
              </Group>
            ))}
          </Stack>
        </ScrollArea.Autosize>
      )}
    </Box>
  );
}
