import { useEffect, useState } from "react";
import { Box, Button, Group, Modal, Stack, Text, Tooltip } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { api, type Meta, type Sheet, type SplitPreview } from "../../api";
import { SPLIT_BLURB, SPLIT_LABEL } from "../../pages/Raids";
import { fail } from "../Page";
import { BoardPreview } from "./BoardPreview";
import { runName } from "./shared";

/** Propose: for one roster a fresh layout from the solver; for a split, pick a philosophy first. One preview in the builder's own layout, then seed the board. */
export function SplitModal({ e, meta, multi, opened, onClose, onUse }: { e: Sheet; meta: Meta; multi: boolean; opened: boolean; onClose: () => void; onUse: (layout: string[][], strategy: string) => Promise<void> }) {
  const [strategy, setStrategy] = useState(e.split?.strategy || meta.split_policies[0] || "balanced");
  const [preview, setPreview] = useState<SplitPreview | null>(null);
  const [seen, setSeen] = useState<string[][][]>([]);
  const [busy, setBusy] = useState(false);
  const [using, setUsing] = useState(false);
  async function run(st: string, avoid: string[][][]) {
    setBusy(true);
    try { const r = await api.post<SplitPreview>(`/api/run/${e.key}/split`, { strategy: st, avoid }); setPreview(r); setSeen([...avoid, r.layout]); } catch (err) { fail(err); } finally { setBusy(false); }
  }
  useEffect(() => { if (opened) { setPreview(null); setSeen([]); run(strategy, []); } /* eslint-disable-line react-hooks/exhaustive-deps */ }, [opened]);
  const pick = (st: string) => { setStrategy(st); setSeen([]); run(st, []); };
  return (
    <Modal opened={opened} onClose={onClose} title={`${multi ? "Propose splits" : "Propose roster"} · ${runName(e)}`} size="xl">
      <Stack gap="md">
        {multi && <Group gap="xs" wrap="wrap">
          {meta.split_policies.map((st) => (
            <Box key={st} onClick={() => !busy && pick(st)} p="sm" style={{ flex: "1 1 180px", cursor: "pointer", border: `1px solid ${strategy === st ? "var(--mantine-color-teal-4)" : "var(--mantine-color-slate-5)"}`, background: strategy === st ? "rgba(56,178,160,.08)" : undefined, borderRadius: 8 }}>
              <Text size="sm" fw={700}>{SPLIT_LABEL[st] || st}</Text><Text size="xs" c="dimmed">{SPLIT_BLURB[st] || ""}</Text>
            </Box>
          ))}
          <Tooltip label="needs wishlists and loot tables — not yet"><Box p="sm" style={{ flex: "1 1 180px", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, opacity: 0.4 }}><Text size="sm" fw={700}>Loot quality</Text><Text size="xs" c="dimmed">seat people where the drops they need go uncontested</Text></Box></Tooltip>
        </Group>}
        <Group justify="space-between" wrap="wrap">
          <Text size="xs" c="dimmed">{busy ? "solving…" : preview ? (multi ? <>Preview of <b>{SPLIT_LABEL[preview.strategy] || preview.strategy}</b> · total synergy {preview.total} · gap {preview.gap} · your own placements on the board are kept</> : <>Solver's pick · synergy {preview.total} · your own placements on the board are kept</>) : ""}</Text>
          {preview && !busy && <Button size="compact-xs" variant="subtle" leftSection={<IconRefresh size={12} />} onClick={() => run(strategy, seen)}>{multi ? "another split like this" : "another layout"}</Button>}
        </Group>
        {preview && !busy && <BoardPreview meta={meta} board={preview.board} />}
        {busy && <Text size="sm" c="dimmed" ta="center" py="xl">The solver is working on it…</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button disabled={!preview || busy} loading={using} onClick={async () => { if (!preview) return; setUsing(true); try { await onUse(preview.layout, preview.strategy); onClose(); } finally { setUsing(false); } }}>{multi ? "Use this split on the board" : "Use this layout on the board"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
