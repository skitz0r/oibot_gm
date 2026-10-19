import { useEffect, useState } from "react";
import { Button, Group, Select, Text } from "@mantine/core";
import { api, type OwnerInfo } from "../api";
import { fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";

/** The Config page's owner line (design §3.5 O-03, D50): the owner by name and, for the owner (or a host superuser), a
 *  transfer — pick a registered member, the server's preview says what changes (Registry.owner_consequence; refused for a
 *  bot, yourself, someone outside the server), one confirm, POST /api/owner. Nobody is DMed. /gm config owner does the same
 *  in Discord; MCP never (the token is the owner). */
export function OwnerLine({ onChanged }: { onChanged?: () => void }) {
  const [info, setInfo] = useState<OwnerInfo | null>(null);
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ask, dialog] = useConfirm();
  const load = () => api.get<OwnerInfo>("/api/owner").then(setInfo).catch(() => setInfo(null));
  useEffect(() => { load(); }, []);
  async function transfer() {
    if (!pick) return;
    setBusy(true);
    try {
      const p = await api.post<{ message: string }>("/api/owner", { uid: pick, preview: true });
      if (!(await ask({ title: "Transfer ownership?", message: p.message, confirmLabel: "Transfer ownership", color: "red" }))) return;
      const r = await api.post<{ message: string }>("/api/owner", { uid: pick });
      ok(r.message); setPick(null); await load(); onChanged?.();
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  if (!info) return null;
  return (
    <Group gap="sm" align="flex-end" wrap="wrap">
      <Text size="sm">Owner: <b>{info.owner ? info.owner.name : "not claimed — /gm config owner"}</b></Text>
      {info.may_transfer && (
        <>
          <Select size="xs" placeholder="Hand it over to…" searchable clearable w={220} value={pick} onChange={setPick} data={(info.candidates || []).map((c) => ({ value: c.uid, label: c.name }))} />
          <Button size="xs" variant="default" color="red" disabled={!pick} loading={busy} onClick={transfer}>Transfer…</Button>
        </>
      )}
      {!info.may_transfer && <Text size="xs" c="dimmed">the owner hands it over here or with /gm config owner</Text>}
      {dialog}
    </Group>
  );
}
