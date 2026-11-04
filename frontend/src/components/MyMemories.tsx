import { Box, Button, Card, Group, Stack, Text } from "@mantine/core";
import { api, type MemoryPage, type MemoryRow } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";

/** Me: what the bot remembers THIS member saying (design §5.61), newest first, with Forget on each — the server's
 *  sentence first, then the write. Shown only when a channel of the guild remembers (the page passes memory_on). */
export function MyMemories({ page, onChanged }: { page: MemoryPage; onChanged: () => void }) {
  const [ask, confirmDialog] = useConfirm();
  async function forget(r: MemoryRow) {
    try {
      const p = await api.post<{ sentences: string[] }>(`/api/me/memories/${r.id}/forget`, { preview: true });
      if (!(await ask({ title: "Forget this?", message: p.sentences.join(" "), confirmLabel: "Forget", color: "red" }))) return;
      const x = await api.post<{ message: string }>(`/api/me/memories/${r.id}/forget`, {});
      ok(x.message); onChanged();
    } catch (e) { fail(e); }
  }
  return (
    <Card>
      {confirmDialog}
      <CardHeader title="What the bot remembers you saying" hint="your words, repeated to whoever asks, with the day you said them" />
      <Stack p="md" gap="xs">
        {page.rows.length === 0 ? (
          <Text size="sm" c="dimmed">Nothing remembered yet. In a channel where the bot remembers, what you say that others could ask about later shows here, and you can make it forget any of it.</Text>
        ) : page.rows.map((r) => (
          <Group key={r.id} justify="space-between" wrap="nowrap" align="flex-start">
            <Box style={{ minWidth: 0 }}>
              <Text size="xs" c="dimmed">{r.when} · #{r.channel_name}</Text>
              <Text size="sm" style={{ overflowWrap: "anywhere" }}>{r.text}</Text>
            </Box>
            <Button size="xs" variant="subtle" color="red" onClick={() => forget(r)}>Forget</Button>
          </Group>
        ))}
        {page.more && <Text size="xs" c="dimmed">The newest {page.rows.length} of {page.total}. Officers see the whole list on the Ops page.</Text>}
      </Stack>
    </Card>
  );
}
