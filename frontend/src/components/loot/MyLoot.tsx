import { useEffect, useRef, useState } from "react";
import { useDirty } from "../../hooks/useDirty";
import { ActionIcon, Autocomplete, Badge, Button, Card, Group, SegmentedControl, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconArrowDown, IconArrowUp, IconPencil, IconTrash } from "@tabler/icons-react";
import { api, ApiError, type LogRow, type Meta, type WishCharacter, type WishEntry } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { useLive } from "../../hooks/useLive";
import { Who } from "./shared";

/** Me › My loot: what my characters received, newest first (by my Discord id; rows recorded by name count too).
 *  Members never see scores, reasons or who recorded it here (D2). */
export function MyLootCard({ meta }: { meta: Meta }) {
  const [rows, setRows] = useState<LogRow[] | null>(null);
  const load = () => api.get<{ rows: LogRow[] }>("/api/me/loot?limit=20").then((d) => setRows(d.rows)).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["loot:"]);
  return (
    <Card>
      <CardHeader title="My loot" hint="newest first" />
      <Stack p="md" gap={4}>
        {rows === null && <Text size="sm" c="dimmed">Loading…</Text>}
        {rows?.length === 0 && <Text size="sm" c="dimmed">Nothing yet. Say what you want on your wishlist below.</Text>}
        {rows?.map((r, i) => (
          <Group key={i} gap="xs" wrap="nowrap" justify="space-between">
            <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
              <Who meta={meta} name={r.character} cls={r.cls} spec={r.spec} />
              <Text size="sm" truncate>{r.item}</Text>
            </Group>
            <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>{r.date_label} · {r.offspec ? "OS" : "MS"}</Text>
          </Group>
        ))}
      </Stack>
    </Card>
  );
}

type Row = { item_id: number; name: string; offspec: boolean; note: string; received?: string | null; received_label?: string | null; known?: boolean };

/** Me › Wishlist (D5): per character, the items in the order you want them, main spec or offspec, a note. Edit mode
 *  with one Save carrying the list's rev: a list changed meanwhile (another tab, /me wishlist) is refused with the
 *  current one — nothing is overwritten. A received item stays on the list, greyed ("received 9 Dec"); remove it when
 *  you're done with it. */
export function WishlistCard({ meta }: { meta: Meta }) {
  const [chars, setChars] = useState<WishCharacter[] | null>(null);
  const [max, setMax] = useState(10);
  const [edit, setEdit] = useState<{ label: string; rev: string; rows: Row[] } | null>(null);
  useDirty("wishlist", edit !== null);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<{ key: string; id: number; name: string; boss: string | null }[]>([]);
  const editing = useRef(false);
  editing.current = edit !== null;
  const load = () => api.get<{ characters: WishCharacter[]; max: number }>("/api/me/wishlist").then((d) => { setChars(d.characters); setMax(d.max); }).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(() => { if (!editing.current) load(); }, ["member", "loot:"]);  // never reload under an edit in progress
  useEffect(() => {
    const t = setTimeout(() => {
      if (q.trim().length < 2) { setFound([]); return; }
      api.get<{ items: { key: string; id: number; name: string; boss: string | null }[] }>(`/api/loot/items/search?q=${encodeURIComponent(q.trim())}`).then((d) => setFound(d.items)).catch(() => setFound([]));
    }, 250);
    return () => clearTimeout(t);
  }, [q]);
  const start = (c: WishCharacter) => setEdit({ label: c.label, rev: c.rev, rows: c.entries.map((e: WishEntry) => ({ item_id: e.item_id, name: e.name, offspec: e.offspec, note: e.note || "", received: e.received, received_label: e.received_label, known: e.known })) });
  const upd = (i: number, patch: Partial<Row>) => edit && setEdit({ ...edit, rows: edit.rows.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const move = (i: number, d: -1 | 1) => {
    if (!edit) return;
    const rows = [...edit.rows];
    const j = i + d;
    if (j < 0 || j >= rows.length) return;
    [rows[i], rows[j]] = [rows[j], rows[i]];
    setEdit({ ...edit, rows });
  };
  function addItem(name: string) {
    if (!edit || !name.trim()) return;
    if (edit.rows.length >= max) { fail(new Error(`A wishlist holds up to ${max} items per character — drop one first.`)); return; }
    const hit = found.find((f) => f.name.toLowerCase() === name.trim().toLowerCase());
    if (edit.rows.some((r) => r.name.toLowerCase() === (hit?.name || name.trim()).toLowerCase())) { fail(new Error("That item is on the list already.")); return; }
    setEdit({ ...edit, rows: [...edit.rows, { item_id: hit?.id || 0, name: hit?.name || name.trim(), offspec: false, note: "", known: !!hit }] });
    setQ("");
  }
  async function save() {
    if (!edit) return;
    try {
      const r = await api.post<{ message: string; characters: WishCharacter[] }>("/api/me/wishlist", { character: edit.label, rev: edit.rev, rows: edit.rows.map((x) => ({ item_id: x.item_id || undefined, name: x.name, offspec: x.offspec, note: x.note || undefined })) });
      ok(r.message); setChars(r.characters); setEdit(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const body = e.body as { characters?: WishCharacter[] };
        if (body.characters) setChars(body.characters);
        fail(new Error(`${e.message} Your edits are still here — Cancel to see the current list.`));
        return;
      }
      fail(e);
    }
  }
  if (!chars) return null;
  return (
    <Card>
      <CardHeader title="Wishlist" hint={`up to ${max} per character · the council sees it when it scores a drop`} />
      <Stack p="md" gap="md">
        {chars.length === 0 && <Text size="sm" c="dimmed">Register a character first — a wishlist belongs to a character.</Text>}
        {chars.map((c) => {
          const mine = edit?.label === c.label;
          return (
            <Stack key={c.label} gap={4}>
              <Group justify="space-between">
                <Who meta={meta} name={c.label} cls={c.cls} spec={c.spec} />
                {!mine && <Button size="compact-xs" variant="default" leftSection={<IconPencil size={12} />} disabled={!!edit} onClick={() => start(c)}>Edit</Button>}
              </Group>
              {!mine && c.entries.length === 0 && <Text size="xs" c="dimmed">Nothing yet.</Text>}
              {!mine && c.entries.map((e) => (
                <Group key={e.key} gap={6} wrap="nowrap" style={{ opacity: e.received ? 0.5 : 1 }}>
                  <Text size="sm" w={22} c="dimmed">{e.rank}.</Text>
                  <Text size="sm" truncate style={{ flex: 1, minWidth: 0 }}>{e.name}</Text>
                  {e.received && <Badge size="xs" variant="light" color="teal">received {e.received_label}</Badge>}
                  {!e.known && <Tooltip label="not on the tables yet — matched by name once it drops"><Badge size="xs" variant="outline" color="gray">by name</Badge></Tooltip>}
                  <Badge size="xs" variant="light" color={e.offspec ? "gray" : "teal"}>{e.offspec ? "OS" : "MS"}</Badge>
                </Group>
              ))}
              {mine && edit && (
                <Stack gap={6} p="xs" style={{ background: "var(--mantine-color-slate-6)", borderRadius: 7 }}>
                  {edit.rows.map((r, i) => (
                    <Group key={`${r.name}-${i}`} gap={6} wrap="wrap" style={{ opacity: r.received ? 0.6 : 1 }}>
                      <Text size="sm" w={22}>{i + 1}.</Text>
                      <Text size="sm" style={{ flex: "1 1 160px", minWidth: 0 }} truncate>{r.name}{r.received ? ` · received ${r.received_label}` : ""}</Text>
                      <SegmentedControl size="xs" value={r.offspec ? "os" : "ms"} data={[{ value: "ms", label: "MS" }, { value: "os", label: "OS" }]} onChange={(v) => upd(i, { offspec: v === "os" })} />
                      <TextInput size="xs" placeholder="note" maxLength={120} value={r.note} onChange={(e) => upd(i, { note: e.currentTarget.value })} w={150} />
                      <ActionIcon size="sm" variant="subtle" color="gray" onClick={() => move(i, -1)} disabled={i === 0}><IconArrowUp size={14} /></ActionIcon>
                      <ActionIcon size="sm" variant="subtle" color="gray" onClick={() => move(i, 1)} disabled={i === edit.rows.length - 1}><IconArrowDown size={14} /></ActionIcon>
                      <ActionIcon size="sm" variant="subtle" color="red" onClick={() => setEdit({ ...edit, rows: edit.rows.filter((_, j) => j !== i) })}><IconTrash size={14} /></ActionIcon>
                    </Group>
                  ))}
                  <Group gap="xs" wrap="nowrap">
                    <Autocomplete size="xs" style={{ flex: 1 }} placeholder={edit.rows.length >= max ? `full (${max})` : "add an item — type its name"} disabled={edit.rows.length >= max}
                      value={q} onChange={setQ} data={found.map((f) => ({ value: f.name, label: f.boss ? `${f.name} · ${f.boss}` : f.name }))}
                      onOptionSubmit={(v) => addItem(v)} onKeyDown={(e) => { if (e.key === "Enter") addItem(q); }} />
                    <Button size="xs" variant="default" disabled={!q.trim()} onClick={() => addItem(q)}>Add</Button>
                  </Group>
                  <Text size="xs" c="dimmed">An item the tables don't know yet is kept by name. Order = priority (1 = most wanted).</Text>
                  <Group justify="flex-end" gap="xs">
                    <Button size="xs" variant="default" onClick={() => { setEdit(null); load(); }}>Cancel</Button>
                    <Button size="xs" onClick={save}>Save</Button>
                  </Group>
                </Stack>
              )}
            </Stack>
          );
        })}
      </Stack>
    </Card>
  );
}
