import { useEffect, useRef, useState } from "react";
import { ActionIcon, Button, Card, Group, SegmentedControl, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconSearch } from "@tabler/icons-react";
import { api, ApiError, type Bargains, type Meta, type Prep, type MoverRow, type Movers, type PriceFind, type PriceHistory, type PriceRow, type WatchList } from "../api";
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
 *  now against the item's usual price over the window, each row opening to the same chart, with what recipes use it. The server sends
 *  every mover; the card picks among them (2026-11-15): a group (the server's `groups`, each with how many it holds), the order
 *  (by percent or by gold), the smallest move, and Show more past the first `page` rows. The card's second tab, Bargains
 *  (GET /api/prices/bargains, where the version has reference prices — `hint_words` not empty): materials far cheaper here than most,
 *  against another economy's prices; the server's rows and sentences, opening to the same chart. The third, Prep (GET
 *  /api/prices/prep): levelling-zone materials that end up in high-level consumables, then what is listed below a vendor's price. Every sentence, price and time is the server's words; the page composes
 *  none. A press waits for the server and takes the list it answers; a refusal prints the server's sentence. With prices
 *  hidden the list shows names alone and opens nothing. The `prices` topic (a new table) and `member` (a watch from another
 *  tab) reload it. */
export function PricesPage({ meta }: { meta: Meta }) {
  const [list, setList] = useState<WatchList | null>(null);
  const [words, setWords] = useState(meta.prices?.words || "");
  const [q, setQ] = useState("");
  const [found, setFound] = useState<PriceFind | null>(null);
  type Where = "search" | "list" | "dips" | "rising" | "bargains" | "prep" | "below";
  const [openAt, setOpenAt] = useState<{ id: number; at: Where } | null>(null);  // the one open row, and in which card
  const open = openAt ? openAt.id : null;
  const setOpen = (id: number | null, at: Where = "list") => setOpenAt(id === null ? null : { id, at });
  const [movers, setMovers] = useState<Movers | null>(null);
  const [win, setWin] = useState("24");
  // Movers' choices (2026-11-15): the group (null = the server's first), the order, the smallest move, the pages shown per side
  const [group, setGroup] = useState<string | null>(null);
  const [sort, setSort] = useState("pct");
  const [step, setStep] = useState("10");
  const [pages, setPages] = useState({ dips: 1, rising: 1 });
  const choose = (set: () => void) => { set(); setPages({ dips: 1, rising: 1 }); };
  // the card's second tab (2026-11-15): materials far cheaper here than most, against another economy's prices
  const [tab, setTab] = useState("movers");
  const [bargains, setBargains] = useState<Bargains | null>(null);
  const [bargainPages, setBargainPages] = useState(1);
  // the third tab (2026-11-15): levelling-zone materials that end up in high-level consumables, and what is listed below a vendor's price
  const [prep, setPrep] = useState<Prep | null>(null);
  const [prepPages, setPrepPages] = useState({ rows: 1, below: 1 });
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
  const loadBargains = () => api.get<Bargains>("/api/prices/bargains").then(setBargains).catch(fail);
  const loadPrep = () => api.get<Prep>("/api/prices/prep").then(setPrep).catch(fail);
  const load = () => {
    loadList();
    loadMovers(winRef.current);
    loadBargains();
    loadPrep();
    if (want.current) search(want.current);
    if (view.current.open !== null) loadHistory(view.current.open, view.current.span);
  };
  useEffect(() => { loadList(); loadMovers(winRef.current); loadBargains(); loadPrep(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
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
  const moverGroup = group ?? movers?.group ?? "all";
  const bigEnough = (r: MoverRow) => Math.abs(r.pct) >= Number(step);
  const moverCount = (id: string) => movers === null ? 0
    : [...movers.dips, ...movers.rising].filter((r) => bigEnough(r) && (id === "all" || r.group === id)).length;
  const moversSide = (title: string, at: "dips" | "rising", all: MoverRow[], none: string) => {
    const chosen = all.filter((r) => bigEnough(r) && (moverGroup === "all" || r.group === moverGroup));
    if (sort === "gold") chosen.sort((a, b) => Math.abs(b.diff) - Math.abs(a.diff));  // the server's order is by percent
    const side = chosen.slice(0, (movers?.page ?? 15) * pages[at]);
    return (
    <div>
      <Text size="xs" fw={700} c="dimmed" tt="uppercase" px="md" pt="sm">{title}</Text>
      {side.length === 0 ? <Text size="sm" c="dimmed" px="md" py="sm">{all.length === 0 ? none : movers?.none_words}</Text> : (
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
                <div className={css.change}>
                  <Text size="sm" fw={700} c={at === "dips" ? "red.4" : "teal.4"}>{sort === "gold" ? r.diff_words : r.change_words}</Text>
                  <Text size="xs" c="dimmed">{sort === "gold" ? r.change_words : r.diff_words}</Text>
                </div>
              </div>
              {isOpen(r.id, at) && panel(r, mine.has(r.id))}
            </div>
          ))}
          {chosen.length > side.length && (
            <Group px="md" py="xs">
              <Button size="compact-sm" variant="default" onClick={() => setPages({ ...pages, [at]: pages[at] + 1 })}>
                Show more ({chosen.length - side.length} left)
              </Button>
            </Group>
          )}
        </div>
      )}
    </div>
    );
  };

  /** Bargains: a row per material — its price in the other economy → the price here, how far under most it sits. */
  const bargainRows = (b: Bargains) => {
    const side = b.rows.slice(0, b.page * bargainPages);
    return (
      <>
        <Text size="xs" c="dimmed" px="md" pt="sm">{b.typical_words} {b.source_words}</Text>
        {b.rows.length === 0 ? <Text size="sm" c="dimmed" px="md" py="sm">{b.none_words}</Text> : (
          <div className={css.list}>
            {side.map((r) => (
              <div key={r.id} className={css.item}>
                <div className={`${css.mover} ${css.opens}`} onClick={() => toggle(r.id, "bargains")}>
                  <Group gap={6} wrap="nowrap" className={css.name}>
                    {chevron(r.id, "bargains")}
                    <div className={css.name}>
                      <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                      {r.uses_words && <Text size="xs" c="dimmed">{r.uses_words}</Text>}
                    </div>
                  </Group>
                  <div className={css.figures}>
                    <Text size="sm">{r.ref_words} → {r.now_words}</Text>
                    <Text size="xs" c="dimmed">{r.qty} listed</Text>
                  </div>
                  <Text size="sm" fw={700} c="teal.4" className={css.change}>{r.gap_words}</Text>
                </div>
                {isOpen(r.id, "bargains") && panel(r, mine.has(r.id))}
              </div>
            ))}
            {b.rows.length > side.length && (
              <Group px="md" py="xs">
                <Button size="compact-sm" variant="default" onClick={() => setBargainPages(bargainPages + 1)}>
                  Show more ({b.rows.length - side.length} left)
                </Button>
              </Group>
            )}
          </div>
        )}
      </>
    );
  };

  /** Prep: a row per material — what it goes into, its price here and the vendor's, Bargains' gap when it has one —
   *  then what is listed below a vendor's price. */
  const more = (left: number, press: () => void) => left > 0 && (
    <Group px="md" py="xs">
      <Button size="compact-sm" variant="default" onClick={press}>Show more ({left} left)</Button>
    </Group>
  );
  const prepRows = (p: Prep) => {
    const side = p.rows.slice(0, p.page * prepPages.rows);
    const below = p.below_rows.slice(0, p.page * prepPages.below);
    return (
      <>
        {p.rows.length === 0 ? <Text size="sm" c="dimmed" px="md" py="sm">{p.none_words}</Text> : (
          <div className={css.list}>
            {side.map((r) => (
              <div key={r.id} className={css.item}>
                <div className={`${css.mover} ${css.opens}`} onClick={() => toggle(r.id, "prep")}>
                  <Group gap={6} wrap="nowrap" className={css.name}>
                    {chevron(r.id, "prep")}
                    <div className={css.name}>
                      <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                      <Text size="xs" c="dimmed" className={css.name}>{r.uses_words}</Text>
                    </div>
                  </Group>
                  <div className={css.figures}>
                    <Text size="sm">{r.now_words}</Text>
                    <Text size="xs" c="dimmed">{r.qty} listed</Text>
                  </div>
                  <div className={css.change}>
                    {r.gap_words && <Text size="sm" fw={700} c="teal.4">{r.gap_words}</Text>}
                    {r.vendor_words && <Text size="xs" c="dimmed">{r.vendor_words}</Text>}
                  </div>
                </div>
                {isOpen(r.id, "prep") && panel(r, mine.has(r.id))}
              </div>
            ))}
            {more(p.rows.length - side.length, () => setPrepPages({ ...prepPages, rows: prepPages.rows + 1 }))}
          </div>
        )}
        <Text size="xs" fw={700} c="dimmed" tt="uppercase" px="md" pt="md">Below vendor price</Text>
        <Text size="xs" c="dimmed" px="md">{p.below_words}</Text>
        {p.below_rows.length === 0 ? <Text size="sm" c="dimmed" px="md" py="sm">{p.below_none}</Text> : (
          <div className={css.list}>
            {below.map((r) => (
              <div key={r.id} className={css.item}>
                <div className={`${css.mover} ${css.opens}`} onClick={() => toggle(r.id, "below")}>
                  <Group gap={6} wrap="nowrap" className={css.name}>
                    {chevron(r.id, "below")}
                    <Text size="sm" fw={600} className={css.name}><ItemName id={r.id} name={r.name} /></Text>
                  </Group>
                  <div className={css.figures}>
                    <Text size="sm">{r.now_words} → {r.vendor_words}</Text>
                    <Text size="xs" c="dimmed">{r.qty} listed</Text>
                  </div>
                  <Text size="sm" fw={700} c="teal.4" className={css.change}>{r.profit_words}</Text>
                </div>
                {isOpen(r.id, "below") && panel(r, mine.has(r.id))}
              </div>
            ))}
            {more(p.below_rows.length - below.length, () => setPrepPages({ ...prepPages, below: prepPages.below + 1 }))}
          </div>
        )}
      </>
    );
  };

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
          <CardHeader title="Movers" hint={tab === "bargains" && bargains ? bargains.hint_words : tab === "prep" && prep ? prep.hint_words
            : `the price now against the item's usual price over the window; ${movers.floor_words}`} />
          {(bargains?.hint_words || prep?.hint_words) && (
            <Group px="md" pt="sm">
              <SegmentedControl size="sm" value={tab} onChange={setTab} aria-label="What to look at"
                data={[{ value: "movers", label: "Movers" }, ...(bargains?.hint_words ? [{ value: "bargains", label: "Bargains" }] : []),
                  ...(prep?.hint_words ? [{ value: "prep", label: "Prep" }] : [])]} />
            </Group>
          )}
          {tab === "prep" && prep !== null && prep.ready && prepRows(prep)}
          {tab === "bargains" && bargains !== null && (
            bargains.ready ? bargainRows(bargains) : (bargains.words && <Text size="sm" c="dimmed" px="md" py="sm">{bargains.words}</Text>)
          )}
          {tab === "movers" && (<>
          <Group px="md" pt="sm">
            <SegmentedControl size="xs" value={win} onChange={changeWindow} disabled={win !== movers.window} aria-label="Over how long"
              data={movers.windows.map((w) => ({ value: w.id, label: w.label }))} />
            {movers.ready && (
              <>
                <SegmentedControl size="xs" value={sort} onChange={(v) => choose(() => setSort(v))} aria-label="Biggest move first"
                  data={movers.sorts.map((s) => ({ value: s.id, label: s.label }))} />
                <SegmentedControl size="xs" value={step} onChange={(v) => choose(() => setStep(v))} aria-label="Moved at least"
                  data={movers.steps.map((s) => ({ value: s.id, label: s.label }))} />
              </>
            )}
          </Group>
          {movers.ready && (
            <Group px="md" pt="xs" gap={6}>
              {movers.groups.map((g) => (
                <Button key={g.id} size="compact-xs" radius="xl" variant={moverGroup === g.id ? "filled" : "default"}
                  aria-pressed={moverGroup === g.id} onClick={() => choose(() => setGroup(g.id))}>
                  {g.name} · {moverCount(g.id)}
                </Button>
              ))}
            </Group>
          )}
          {movers.ready && movers.words && <Text size="xs" c="dimmed" px="md" pt="sm">{movers.words}</Text>}
          {!movers.ready ? (movers.words && <Text size="sm" c="dimmed" px="md" py="sm">{movers.words}</Text>) : (
            <SimpleGrid cols={{ base: 1, md: 2 }} spacing={0}>
              {moversSide("Dips", "dips", movers.dips, movers.dips_words)}
              {moversSide("Rising", "rising", movers.rising, movers.rising_words)}
            </SimpleGrid>
          )}
          </>)}
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
