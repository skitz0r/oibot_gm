import { useEffect, useState } from "react";
import { Button, Card, Group, Stack, Text } from "@mantine/core";
import { api } from "../api";
import { useConfirm } from "./ConfirmModal";
import { useLive } from "../hooks/useLive";
import type { LeftMember } from "../setupApi";
import { CardHeader, fail, ok } from "./Page";

/** Members who left the Discord server (Member.left_at, set when Discord says so): officers may Remove record — the
 *  confirm prints the server's sentence (what goes, what stays). Members never delete their own record (D29). Hidden when
 *  nobody left. */
export function LeftServer() {
  const [rows, setRows] = useState<LeftMember[] | null>(null);
  const [ask, dialog] = useConfirm();
  const load = () => api.get<{ left: LeftMember[] }>("/api/admin/left").then((d) => setRows(d.left)).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["member"]);
  if (!rows || rows.length === 0) return null;
  async function remove(m: LeftMember) {
    if (!(await ask({ title: `Remove ${m.name}'s record?`, message: m.consequence, confirmLabel: "Remove record", color: "red" }))) return;
    try { const r = await api.post<{ message: string }>("/api/admin/member/remove", { uid: m.uid }); ok(r.message); } catch (e) { fail(e); } finally { load(); }
  }
  return (
    <Card>
      <CardHeader title="Left the server" hint="their records stay until an officer removes them" />
      <Stack p="md" gap="xs">
        {rows.map((m) => (
          <Group key={m.uid} justify="space-between" wrap="nowrap">
            <Text size="sm" truncate>{m.name} <Text span size="xs" c="dimmed">· left {m.left}{m.characters.length ? ` · ${m.characters.join(", ")}` : ""}</Text></Text>
            <Button size="xs" variant="subtle" color="red" onClick={() => remove(m)}>Remove record…</Button>
          </Group>
        ))}
      </Stack>
      {dialog}
    </Card>
  );
}
