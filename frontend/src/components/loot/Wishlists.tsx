import { useEffect, useState } from "react";
import { Badge, Button, Card, Group, Stack, Table, Text, Textarea } from "@mantine/core";
import { api, type Meta, type WishCharacter } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { useLive } from "../../hooks/useLive";
import { Who } from "./shared";

interface Wanted { key: string; name: string; item_id: number; boss: string | null; wants: { character: string; cls: string; spec: string; rank: number; offspec: boolean }[] }
interface Overview { people: { uid: string; display_name: string; test: boolean; characters: WishCharacter[] }[]; items: Wanted[]; max: number }
/** One character's part of a TMB import (Registry.import_tmb_wishlists per_character): item names. */
interface PerCharacter { character: string; added: string[]; kept: string[]; replaced: string[]; duplicates: string[]; over_cap: string[] }
interface ImportReport { rows: number; characters: number; added: number; replaced: number; duplicates: number; received: number; over_cap: number; unknown: string[]; lines: string[]; per_character?: PerCharacter[] }

/** Loot › Wishlists (officers): who wants each item most (main spec before offspec, by place on the list) — the same
 *  lists scoring reads — and the ThatsMyBIS import (D5: preview first — per character what is added and what is kept —
 *  then Confirm; an import never deletes an entry made on the site). */
export function Wishlists({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Overview | null>(null);
  const [csv, setCsv] = useState("");
  const [report, setReport] = useState<{ message: string; report: ImportReport; preview: boolean } | null>(null);
  const load = () => api.get<Overview>("/api/loot/wishlists").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["member", "loot:"]);
  async function run(preview: boolean) {
    try {
      const r = await api.post<{ message: string; report: ImportReport }>("/api/loot/wishlists/import", { csv, preview });
      setReport({ ...r, preview });
      if (!preview) { ok(r.message); setCsv(""); load(); }
    } catch (e) { fail(e); }
  }
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Card>
        <CardHeader title="Most wanted" hint="not yet received · main spec first, then the place on each list" />
        {/* no rows: no minimum width, so the empty line wraps on a phone instead of running out of sight */}
        <Table.ScrollContainer minWidth={data.items.length ? 560 : 0}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Item</Table.Th><Table.Th>Wanted by</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.items.length === 0 && <Table.Tr><Table.Td colSpan={2}><Text size="sm" c="dimmed">No wishlists yet — members build theirs on the Me page or with /me wishlist.</Text></Table.Td></Table.Tr>}
              {data.items.map((w) => (
                <Table.Tr key={w.key}>
                  <Table.Td><Text size="sm" fw={600}>{w.name}</Text>{w.boss && <Text size="xs" c="dimmed">{w.boss}</Text>}</Table.Td>
                  <Table.Td>
                    <Group gap="md" wrap="wrap">
                      {w.wants.map((x) => (
                        <Group key={x.character} gap={4} wrap="nowrap">
                          <Who meta={meta} name={x.character} cls={x.cls} spec={x.spec} dim={x.offspec} />
                          <Text size="xs" c="dimmed">#{x.rank}{x.offspec ? " OS" : ""}</Text>
                        </Group>
                      ))}
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      <Card>
        <CardHeader title="Import from ThatsMyBIS" hint="paste the wishlist export (CSV)" />
        <Stack p="md" gap="sm">
          <Textarea autosize minRows={3} maxRows={10} value={csv} onChange={(e) => { setCsv(e.currentTarget.value); setReport(null); }} placeholder="type,character_name,sort_order,item_name,item_id,is_offspec,note,received_at,…" />
          <Text size="xs" c="dimmed">Per character in TMB's order. An item already on someone's list stays as it is; a character's earlier TMB rows are replaced; entries made on the site are never deleted; past {data.max} the rest is skipped and counted.</Text>
          {report && (
            <Stack gap={4}>
              <Text size="sm">{report.message}</Text>
              {report.preview && (report.report.per_character || []).length > 0 && (
                <Table.ScrollContainer minWidth={560}>
                  <Table verticalSpacing={2}>
                    <Table.Thead><Table.Tr><Table.Th>Character</Table.Th><Table.Th>Added from TMB</Table.Th><Table.Th>Kept (made on the site)</Table.Th><Table.Th>Not added</Table.Th></Table.Tr></Table.Thead>
                    <Table.Tbody>
                      {(report.report.per_character || []).map((c) => (
                        <Table.Tr key={c.character}>
                          <Table.Td><Text size="sm" fw={600}>{c.character}</Text></Table.Td>
                          <Table.Td><Text size="xs">{c.added.join(" · ") || "—"}</Text>{c.replaced.length > 0 && <Text size="xs" c="dimmed">replaces the earlier TMB rows: {c.replaced.join(" · ")}</Text>}</Table.Td>
                          <Table.Td><Text size="xs">{c.kept.join(" · ") || "—"}</Text></Table.Td>
                          <Table.Td>
                            {c.duplicates.length > 0 && <Text size="xs" c="dimmed">already on the list: {c.duplicates.join(" · ")}</Text>}
                            {c.over_cap.length > 0 && <Text size="xs" c="yellow">past {data.max}: {c.over_cap.join(" · ")}</Text>}
                            {!c.duplicates.length && !c.over_cap.length && <Text size="xs" c="dimmed">—</Text>}
                          </Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Table.ScrollContainer>
              )}
              {report.preview && <Text size="xs" c="dimmed">Nothing is deleted: every entry made on the site stays, in its place.</Text>}
              {!report.preview && report.report.lines.map((l) => <Text key={l} size="xs" c="dimmed">{l}</Text>)}
            </Stack>
          )}
          <Group justify="flex-end" gap="sm">
            <Button variant="default" disabled={!csv.trim()} onClick={() => run(true)}>Preview</Button>
            <Button disabled={!csv.trim() || !report?.preview || !(report.report.added || report.report.replaced)} onClick={() => run(false)}>Confirm — import {report?.preview ? report.report.added : ""}</Button>
          </Group>
        </Stack>
      </Card>
      <Card>
        <CardHeader title="By member" />
        <Stack p="md" gap="sm">
          {data.people.length === 0 && <Text c="dimmed" size="sm">Nobody has a wishlist yet.</Text>}
          {data.people.map((p) => (
            <Stack key={p.uid} gap={2}>
              <Group gap="xs"><Text fw={600}>{p.display_name}</Text>{p.test && <Badge size="xs" color="violet" variant="light">test</Badge>}</Group>
              {p.characters.map((c) => (
                <Group key={c.label} gap="xs" wrap="wrap" pl="sm">
                  <Who meta={meta} name={c.label} cls={c.cls} spec={c.spec} />
                  <Text size="xs" c="dimmed">{c.entries.map((e) => `${e.rank}. ${e.name}${e.offspec ? " (OS)" : ""}${e.received ? " ✓" : ""}`).join(" · ")}</Text>
                </Group>
              ))}
            </Stack>
          ))}
        </Stack>
      </Card>
    </Stack>
  );
}
