import { useEffect, useRef, useState } from "react";
import { ActionIcon, Button, Card, Group, SegmentedControl, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconSearch } from "@tabler/icons-react";
import { api, ApiError, type Meta, type MoverRow, type Movers, type PriceFind, type PriceHistory, type PriceRow, type WatchList } from "../api";
import { ItemName } from "../components/ItemName";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { PriceChart, Sparkline, trendOf } from "../components/PriceChart";
import { useLive } from "../hooks/useLive";
import css from "./prices.module.css";

/** The Prices tab (price watch §4.3; D1, D3, C5, C6, C12, C16), for every member where the version has auction houses:
 *  the search (GET /api/prices/find, the Professions box's search — D2) with Watch / Stop watching per row, then Your
 *  watch list (GET /api/prices/watching), each row with its small 24-hour line and the day's change beside the price (Sparkline, the row's `spark`). A row of
 *  either — watched or not (the owner's decision 2026-11-14) — opens in place to the item's chart and the server's
 *  history sentences (GET /api/prices/history) with Watch / Stop watching; one row is open at a time. Between the two,
 *  Movers (GET /api/prices/movers, an experiment): Dips and Rising over 6 hours / 12 hours / 24 hours / 7 days — the price
 *  now against the item's usual price over the window, each row opening to the same chart, with what recipes use it. Every sentence, price and time is the server's words; the page composes
 *  none. A press waits for the server and takes the list it answers; a refusal prints the server's sentence. With prices
 *  hidden the list shows names alone and opens nothing. The `prices` topic (a new table) and `member` (a watch from another
 *  tab) reload it. */
export function PricesPage({ meta }: { meta: Meta }) {
  const [list, setList] = useState<WatchList | null>(null);
  const [words, setWords] = useState(meta.prices?.words || "");
  const [q, setQ] = useState("");
  const [found, setFound] = useState<PriceFind | null>(null);
  type Where = "search" | "list" | "dips" | "rising";
  const [openAt, setOpenAt] = useState<{ id: number; at: Where } | null>(null);  // the one open row, and in which card
  const open = openAt ? openAt.id : null;
  const setOpen = (id: number | null, at: Where = "list") => setOpenAt(id === null ? null : { id, at });
  const [movers, setMovers] = useState<Movers | null>(null);
  const [win, setWin] = useState("24");
  const winRef = useRef(win);
  winRef.current = win;
  const [span, setSpan] = useState("1");  // 24 hours first: where a price moved today shows
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
      .catch((e) => { if (e instanceof ApiError && e.status === 400) setOpen(null); else fail(e); });  // an item the bot can't name any more
  };
  const loadList = () => api.get<WatchList>("/api/prices/watching").then((d) => { setList(d); setWords(d.words); }).catch(fail);
  const loadMovers = (w: string) => api.get<Movers>(`/api/prices/movers?window=${encodeURIComponent(w)}`)
    .then((d) => { if (winRef.current === d.window) setMovers(d); }).catch(fail);
  const changeWindow = (w: string) => { setWin(w); loadMovers(w); };
  const load = () => {
    loadList();
    loadMovers(winRef.current);
    if (want.current) search(want.current);
    if (view.current.open !== null) loadHistory(view.current.open, view.current.span);
  };
  useEffect(() => { loadList(); loadMovers(winRef.current); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
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
      if (!on && openAt?.id === id && openAt.at === "list") setOpen(null);  // its list row is gone
      ok(d.message);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) setRefusal(e.message);
      fail(e);
    } finally {
      setPressing((b) => { const n = new Set(b); n.delete(id); return n; });
    }
  };

  const watchButton = (r: { id: number }, on: boolean) => (
    <Button size="xs" variant={on ? "light" : "subtle"} color={on ? "teal" : "gray"} loading={pressing.has(r.id)} onClick={() => press(r.id, !on)}>
      {on ? "Stop watching" : "Watch"}
    </Button>
  );
  const shown = list?.shown ?? !!meta.prices?.on;  // prices show (fresh or stale): search and history; else names alone
  const rows = list?.rows || [];
  const mine = new Set(rows.map((r) => r.id));
  const isOpen = (id: number, at: Where) => shown && openAt?.id === id && openAt.at === at;
  const toggle = (id: number, at: Where) => setOpen(isOpen(id, at) ? null : id, at);
  const chevron = (id: number, at: Where) => (
    <ActionIcon variant="subtle" color="gray" size="sm" aria-expanded={isOpen(id, at)} aria-label={isOpen(id, at) ? "Hide its price over time" : "Show its price over time"}
      onClick={(e) => { e.stopPropagation(); toggle(id, at); }}>
      {isOpen(id, at) ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
    </ActionIcon>
  );
  /** An open row's panel: the chart, the server's sentences, and Watch / Stop watching. */
  const panel = (r: { id: number }, watching: boolean) => (
    <Stack gap="sm" className={css.panel}>
      {hist === null || hist.id !== r.id ? <Text size="sm" c="dimmed">Loading…</Text> : (
        <>
          <PriceChart data={hist} onSpan={changeSpan} span={span} busy={span !== hist.span} />
          {hist.sentences.map((s) => <Text key={s} size="sm">{s}</Text>)}
          {hist.used_in && <Text size="sm" c="dimmed">{hist.used_in}</Text>}
        </>
      )}
      <Group>{watchButton(r, watching)}</Group>
    </Stack>
  );
  /** One side of Movers: its rows (the item, its usual price → the price now, the change, how many are listed, how many
   *  recipes use it), or the server's sentence when nothing moved that way. */
  const moversSide = (title: string, at: "dips" | "rising", side: MoverRow[], none: string) => (
    <div>
      <Text size="xs" fw={700} c="dimmed" tt="uppercase" px="md" pt="sm">{title}</Text>
      {side.length === 0 ? <Text size="sm" c="dimmed" px="md" py="sm">{none}</Text> : (
        <div className={css.list}>
          {side.map((r) => (
            <div key={r.id} className={css.item}>
              <div className={`${css.mover} ${css.opens}`} onClick={() => toggle(r.id, at)}>
                <Group gap={6} wrap="nowrap" className={css.name}>
                  {chevron(r.id, at)}
                  <div className={css.name}>
                    <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                    {r.uses_words && <Text size="xs" c="dimmed">{r.uses_words}</Text>}
                  </div>
                </Group>
                <div className={css.figures}>
                  <Text size="sm">{r.usual_words} → {r.now_words}</Text>
                  <Text size="xs" c="dimmed">{r.qty} listed</Text>
                </div>
                <Text size="sm" fw={700} c={at === "dips" ? "red.4" : "teal.4"} className={css.change}>{r.change_words}</Text>
              </div>
              {isOpen(r.id, at) && panel(r, mine.has(r.id))}
            </div>
          ))}
        </div>
      )}
    </div>
  );

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
                    <div key={p.id} className={css.found}>
                      <div className={`${css.row} ${css.opens}`} onClick={() => toggle(p.id, "search")}>
                        <Group gap={6} wrap="nowrap" className={css.name}>
                          {chevron(p.id, "search")}
                          <Text size="sm" fw={600} className={css.name}><ItemName id={p.id} name={p.name} /></Text>
                        </Group>
                        <Text size="sm" c={p.qty ? undefined : "dimmed"}>{p.low_words}</Text>
                        <Text size="sm" c="dimmed">{p.qty ? `${p.qty} listed` : ""}</Text>
                        <Group justify="flex-end" className={css.actions} onClick={(e) => e.stopPropagation()}>{watchButton(p, mine.has(p.id) || !!p.watching)}</Group>
                      </div>
                      {isOpen(p.id, "search") && panel(p, mine.has(p.id) || !!p.watching)}
                    </div>
                  ))}
                </div>
                {found.more && <Text size="sm" c="dimmed">{found.more} Type more of the name to narrow it.</Text>}
              </>
            )}
          </Stack>
        </Card>
      )}

      {shown && movers !== null && (
        <Card>
          <CardHeader title="Movers" hint="the price now against the item's usual price over the window; at least 5 listed" />
          <Group px="md" pt="sm">
            <SegmentedControl size="xs" value={win} onChange={changeWindow} disabled={win !== movers.window} aria-label="Over how long"
              data={movers.windows.map((w) => ({ value: w.id, label: w.label }))} />
          </Group>
          {movers.ready && movers.words && <Text size="xs" c="dimmed" px="md" pt="sm">{movers.words}</Text>}
          {!movers.ready ? (movers.words && <Text size="sm" c="dimmed" px="md" py="sm">{movers.words}</Text>) : (
            <SimpleGrid cols={{ base: 1, md: 2 }} spacing={0}>
              {moversSide("Dips", "dips", movers.dips, movers.dips_words)}
              {moversSide("Rising", "rising", movers.rising, movers.rising_words)}
            </SimpleGrid>
          )}
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
              const opened = isOpen(r.id, "list");
              return (
                <div key={r.id} className={css.item}>
                  {shown ? (
                    <div className={`${css.watch} ${css.opens}`} onClick={() => toggle(r.id, "list")}>
                      <Group gap={6} wrap="nowrap" className={css.name}>
                        {chevron(r.id, "list")}
                        <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                      </Group>
                      <div className={css.spark}>{r.spark ? <Sparkline data={r.spark} label="Its lowest price over the last 24 hours" /> : null}</div>
                      <div className={css.figures}>
                        <Text size="sm" fw={600} c={r.qty ? undefined : "dimmed"}>{r.low_words}</Text>
                        {r.spark?.change_words && <Text size="xs" fw={700} style={{ color: trendOf(r.spark.up) }}>{r.spark.change_words}</Text>}
                      </div>
                      <Text size="sm" c="dimmed" className={css.listed}>{r.qty ? `${r.qty} listed` : ""}</Text>
                    </div>
                  ) : (
                    <div className={css.row}>
                      <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                      <Text size="sm" c="dimmed">{r.low_words}</Text>
                      <Text size="sm" c="dimmed" />
                      <Group justify="flex-end" className={css.actions}>{watchButton(r, true)}</Group>
                    </div>
                  )}
                  {opened && panel(r, true)}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </Stack>
  );
}
