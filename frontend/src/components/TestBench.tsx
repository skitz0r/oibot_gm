import { useMemo, useState } from "react";
import { ActionIcon, Box, Button, Card, Group, Select, Stack, Text, Tooltip } from "@mantine/core";
import { IconFlask, IconMinus, IconPlus, IconTrash } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { api, type MemberRow, type Meta } from "../api";
import { GameIcon } from "./Icons";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";

const MAX = 80;  // Registry.TEST_MAX
type Key = string;  // "Class:Spec"

/** The test bench: puppet members by composition. Each spec is its icon and a count you step up or down; one Save
 *  adds or removes the difference. "Try a comp" opens a sandbox run with every puppet joined (never posted), and the
 *  board's Propose roster builds the best comp from them. Officers only. */
export function TestBench({ meta, rows, onChanged }: { meta: Meta; rows: MemberRow[]; onChanged: () => Promise<unknown> }) {
  const puppets = rows.filter((r) => r.privilege === "test");
  const now = useMemo(() => {
    const out: Record<Key, string[]> = {};  // spec → puppet uids, newest last (removals take the newest)
    for (const r of [...puppets].sort((a, b) => (BigInt(a.uid) < BigInt(b.uid) ? -1 : 1))) {
      const c = r.characters.find((x) => x.is_main) || r.characters[0];
      if (c) (out[`${c.cls}:${c.spec}`] ||= []).push(r.uid);
    }
    return out;
  }, [rows]);
  const [draft, setDraft] = useState<Record<Key, number> | null>(null);
  const [cls, setCls] = useState<string | null>(null);
  const [spec, setSpec] = useState<string | null>(null);
  const [raid, setRaid] = useState<string | null>(() => (meta.raids.find((r) => r.size === 20) || meta.raids[0])?.id ?? null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, confirmDialog] = useConfirm();
  const navigate = useNavigate();

  const counts: Record<Key, number> = draft ?? Object.fromEntries(Object.entries(now).map(([k, v]) => [k, v.length]));
  const keys = Object.keys(counts).sort((a, b) => roleOrder(meta, a) - roleOrder(meta, b) || a.localeCompare(b));
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const byRole = meta.roles.map((role) => [role, keys.filter((k) => roleOf(meta, k) === role).reduce((a, k) => a + counts[k], 0)] as const);
  const add = keys.flatMap((k) => (counts[k] > (now[k]?.length ?? 0) ? [{ cls: k.split(":")[0], spec: k.split(":")[1], count: counts[k] - (now[k]?.length ?? 0) }] : []));
  const remove = keys.flatMap((k) => (now[k] ?? []).slice(counts[k]));
  const dirty = add.length > 0 || remove.length > 0;

  const step = (k: Key, by: number) => setDraft({ ...counts, [k]: Math.max(0, Math.min(counts[k] + by, counts[k] + (MAX - total))) });
  function addSpec() {
    if (!cls || !spec) return;
    const k = `${cls}:${spec}`;
    setDraft({ ...counts, [k]: (counts[k] ?? 0) + (total < MAX ? 1 : 0) });
    setSpec(null);
  }
  async function post(key: string, body: unknown, after?: (r: { message: string; key?: string }) => void) {
    setBusy(key);
    try {
      const r = await api.post<{ message: string; key?: string }>("/api/admin/test", body);
      ok(r.message); setDraft(null); await onChanged(); after?.(r);
    } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function clearAll() {
    if (!(await ask({ title: `Delete all ${puppets.length} test members?`, message: "Every test run is cancelled and every puppet goes, with their answers and placements. Real members are never touched.", confirmLabel: "Delete all", color: "red" }))) return;
    await post("clear", { action: "clear" });
  }

  return (
    <Card>
      <CardHeader title="Test bench" hint={`puppet members for rehearsing · ${total} of ${MAX}`} />
      <Stack gap="md" p="md">
        <Group gap="lg" wrap="wrap">
          {byRole.map(([role, n]) => (
            <Tooltip key={role} label={role}><Group gap={4}><GameIcon meta={meta} kind="role" id={role} size={20} title={role} /><Text fw={700}>{n}</Text></Group></Tooltip>
          ))}
        </Group>
        <Group gap="sm" wrap="wrap">
          {keys.map((k) => {
            const [c, s] = k.split(":");
            const changed = counts[k] !== (now[k]?.length ?? 0);
            return (
              <Group key={k} gap={2} wrap="nowrap" px={6} py={4} style={{ border: `1px solid var(--mantine-color-${changed ? "yellow-7" : "slate-5"})`, borderRadius: 8, opacity: counts[k] ? 1 : 0.45 }}>
                <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`one fewer ${s} ${c}`} onClick={() => step(k, -1)} disabled={!counts[k]}><IconMinus size={14} /></ActionIcon>
                <Tooltip label={`${s} ${c} · ${roleOf(meta, k)}`}><Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="spec" id={k} size={26} title={`${s} ${c}`} /></Box></Tooltip>
                <Text fw={700} w={22} ta="center">{counts[k]}</Text>
                <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`one more ${s} ${c}`} onClick={() => step(k, 1)} disabled={total >= MAX}><IconPlus size={14} /></ActionIcon>
              </Group>
            );
          })}
          {keys.length === 0 && <Text size="sm" c="dimmed">No test members. Add any mix of specs below.</Text>}
        </Group>
        <Group gap="xs" wrap="wrap" align="flex-end">
          <Select size="sm" w={160} placeholder="Class" data={Object.keys(meta.classes)} value={cls} onChange={(v) => { setCls(v); setSpec(null); }} />
          <Select size="sm" w={200} placeholder="Spec" disabled={!cls} data={Object.entries(meta.classes[cls || ""] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` }))} value={spec} onChange={setSpec} />
          <Button size="sm" variant="default" leftSection={<IconPlus size={14} />} disabled={!cls || !spec || total >= MAX} onClick={addSpec}>Add</Button>
        </Group>
        <Group gap="sm" wrap="wrap" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
          <Button size="sm" color="red" variant="subtle" leftSection={<IconTrash size={15} />} disabled={!puppets.length || dirty} loading={busy === "clear"} onClick={clearAll}>Delete all test members</Button>
          <Group gap="sm" ml="auto" wrap="wrap">
            {dirty ? (
              <>
                <Text size="sm" c="dimmed">{add.reduce((a, r) => a + r.count, 0)} to add · {remove.length} to remove</Text>
                <Button size="sm" variant="default" onClick={() => setDraft(null)}>Cancel</Button>
                <Button size="sm" loading={busy === "save"} onClick={() => post("save", { action: "compose", add, remove })}>Save test members</Button>
              </>
            ) : (
              <>
                <Select size="sm" w={200} aria-label="raid for the comp sandbox" data={meta.raids.map((r) => ({ value: r.id, label: `${r.name} (${r.size})` }))} value={raid} onChange={setRaid} allowDeselect={false} />
                <Tooltip label="Opens a test run two weeks out with every test member joined (never posted in Discord); Propose roster on its board builds the best comp from them">
                  <Button size="sm" leftSection={<IconFlask size={15} />} disabled={!puppets.length || !raid} loading={busy === "comp"}
                    onClick={() => post("comp", { action: "comp", raid }, (r) => { if (r.key) navigate(`/rosters#run-${r.key}`); })}>Try a comp</Button>
                </Tooltip>
              </>
            )}
          </Group>
        </Group>
      </Stack>
      {confirmDialog}
    </Card>
  );
}

function roleOf(meta: Meta, k: Key): string {
  const [c, s] = k.split(":");
  return meta.classes[c]?.[s] ?? "";
}
function roleOrder(meta: Meta, k: Key): number {
  const i = meta.roles.indexOf(roleOf(meta, k));
  return i < 0 ? 99 : i;
}
