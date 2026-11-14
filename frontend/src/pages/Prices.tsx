import { useEffect, useRef, useState } from "react";
import { ActionIcon, Button, Card, Group, Stack, Text, TextInput } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconSearch } from "@tabler/icons-react";
import { api, ApiError, type Meta, type PriceFind, type PriceHistory, type PriceRow, type WatchList } from "../api";
import { ItemName } from "../components/ItemName";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { PriceChart } from "../components/PriceChart";
import { useLive } from "../hooks/useLive";
import css from "./prices.module.css";

/** The Prices tab (price watch §4.3; D1, D3, C5, C6, C12, C16), for every member where the version has auction houses:
 *  the search (GET /api/prices/find, the Professions box's search — D2) with Watch / Stop watching per row, then Your
 *  watch list (GET /api/prices/watching), each item opened in place to its chart and the server's history sentences
 *  (GET /api/prices/history) with Stop watching. Every sentence, price and time is the server's words; the page composes
 *  none. A press waits for the server and takes the list it answers; a refusal prints the server's sentence. With prices
 *  hidden the list shows names alone and opens nothing. The `prices` topic (a new table) and `member` (a watch from another
 *  tab) reload it. */
export function PricesPage({ meta }: { meta: Meta }) {
  const [list, setList] = useState<WatchList | null>(null);
  const [words, setWords] = useState(meta.prices?.words || "");
  const [q, setQ] = useState("");
  const [found, setFound] = useState<PriceFind | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [span, setSpan] = useState("7");
  const [hist, setHist] = useState<PriceHistory | null>(null);
  const [pressing, setPressing] = useState<Set<number>>(new Set());
  const [refusal, setRefusal] = useState("");
  const want = useRef("");
  want.current = q.trim();
  const view = useRef({ open, span });
  view.current = { open, span };

  const search = (text: string) => {
    if (!text) { setFound(null); return; }
    api.get<PriceFind>(`/api/prices/find?q=${encodeURIComponent(text)}`)
      .then((d) => { if (want.current === d.q.trim()) { setFound(d); setWords(d.words); } }).catch(fail);
  };
  const loadHistory = (id: number, s: string) => {
    api.get<PriceHistory>(`/api/prices/history?id=${id}&span=${encodeURIComponent(s)}`)
      .then((d) => { if (view.current.open === d.id && view.current.span === d.span) setHist(d); })
      .catch((e) => { if (e instanceof ApiError && e.status === 403) setOpen(null); else fail(e); });  // off the list meanwhile
  };
  const loadList = () => api.get<WatchList>("/api/prices/watching").then((d) => { setList(d); setWords(d.words); }).catch(fail);
  const load = () => {
    loadList();
    if (want.current) search(want.current);
    if (view.current.open !== null) loadHistory(view.current.open, view.current.span);
  };
  useEffect(() => { loadList(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["prices", "member"], { every: 3000 });
  useEffect(() => { const t = setTimeout(() => search(q.trim()), 250); return () => clearTimeout(t); }, [q]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setHist(null); if (open !== null) loadHistory(open, span); }, [open]);  // eslint-disable-line react-hooks/exhaustive-deps

  const changeSpan = (s: string) => { setSpan(s); if (open !== null) loadHistory(open, s); };

  /** Watch / Stop watching: the server's sentence, its list, and the search rows marked from it. */
  const press = async (id: number, on: boolean) => {
    setPressing((b) => new Set(b).add(id));
    try {
      const d = await api.post<{ message: string; rows: PriceRow[] }>("/api/prices/watch", { id, on });
      const mine = new Set(d.rows.map((r) => r.id));
      setList((l) => (l ? { ...l, rows: d.rows } : l));
      setFound((f) => (f ? { ...f, rows: f.rows.map((r) => ({ ...r, watching: mine.has(r.id) })) } : f));
      setRefusal("");
      if (!on && open === id) setOpen(null);
      ok(d.message);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) setRefusal(e.message);
      fail(e);
    } finally {
      setPressing((b) => { const n = new Set(b); n.delete(id); return n; });
    }
  };

  const watchButton = (r: PriceRow, on: boolean) => (
    <Button size="xs" variant={on ? "light" : "subtle"} color={on ? "teal" : "gray"} loading={pressing.has(r.id)} onClick={() => press(r.id, !on)}>
      {on ? "Stop watching" : "Watch"}
    </Button>
  );
  const shown = list?.shown ?? !!meta.prices?.on;  // prices show (fresh or stale): search and history; else names alone
  const rows = list?.rows || [];

  return (
    <Stack gap="lg" className={css.touch}>
      <PageTitle title="Prices" intro="The lowest price each item is listed for on the guild's auction house, and the items you keep an eye on." />
      {words && <Text size="sm" c="dimmed" mt={-8}>{words}</Text>}

      {shown && (
        <Card>
          <CardHeader title="What does it go for?" hint="part of an item's name finds its lowest price" />
          <Stack gap="sm" px="md" py="sm">
            <TextInput leftSection={<IconSearch size={16} />} placeholder="part of an item's name, like arcanite bar" value={q} aria-label="Search an item's price"
              onChange={(e) => setQ(e.currentTarget.value)} maw={520} />
            {found !== null && found.rows.length === 0 && <Text size="sm" c="dimmed" style={{ whiteSpace: "pre-line" }}>{found.message}</Text>}
            {found !== null && found.rows.length > 0 && (
              <>
                <div className={css.rows}>
                  {found.rows.map((p) => (
                    <div key={p.id} className={css.row}>
                      <Text size="sm" fw={600} className={css.name}><ItemName id={p.id} name={p.name} /></Text>
                      <Text size="sm" c={p.qty ? undefined : "dimmed"}>{p.low_words}</Text>
                      <Text size="sm" c="dimmed">{p.qty ? `${p.qty} listed` : ""}</Text>
                      <Group justify="flex-end" className={css.actions}>{watchButton(p, !!p.watching)}</Group>
                    </div>
                  ))}
                </div>
                {found.more && <Text size="sm" c="dimmed">{found.more} Type more of the name to narrow it.</Text>}
              </>
            )}
          </Stack>
        </Card>
      )}

      <Card>
        <CardHeader title="Your watch list" hint="only you see it; nothing pings you when a price moves" />
        {list === null && <Text size="sm" c="dimmed" px="md" py="sm">Loading…</Text>}
        {list !== null && rows.length === 0 && (
          <Text size="sm" c="dimmed" px="md" py="sm">{!list.registered && refusal ? refusal : list.empty}</Text>
        )}
        {rows.length > 0 && (
          <div className={css.list}>
            {rows.map((r) => {
              const opened = shown && open === r.id;
              return (
                <div key={r.id} className={css.item}>
                  <div className={shown ? `${css.row} ${css.opens}` : css.row} onClick={shown ? () => setOpen(opened ? null : r.id) : undefined}>
                    <Group gap={6} wrap="nowrap" className={css.name}>
                      {shown && (
                        <ActionIcon variant="subtle" color="gray" size="sm" aria-expanded={opened} aria-label={opened ? "Hide its price over time" : "Show its price over time"}
                          onClick={(e) => { e.stopPropagation(); setOpen(opened ? null : r.id); }}>
                          {opened ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
                        </ActionIcon>
                      )}
                      <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                    </Group>
                    <Text size="sm" c={r.qty ? undefined : "dimmed"}>{r.low_words}</Text>
                    <Text size="sm" c="dimmed">{r.qty ? `${r.qty} listed` : ""}</Text>
                    <Group justify="flex-end" className={css.actions} onClick={(e) => e.stopPropagation()}>{!shown && watchButton(r, true)}</Group>
                  </div>
                  {opened && (
                    <Stack gap="sm" className={css.panel}>
                      {hist === null || hist.id !== r.id ? <Text size="sm" c="dimmed">Loading…</Text> : (
                        <>
                          <PriceChart data={hist} onSpan={changeSpan} span={span} busy={span !== hist.span} />
                          {hist.sentences.map((s) => <Text key={s} size="sm">{s}</Text>)}
                        </>
                      )}
                      <Group>{watchButton(r, true)}</Group>
                    </Stack>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </Stack>
  );
}
