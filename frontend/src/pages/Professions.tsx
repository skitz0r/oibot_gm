import { useEffect, useRef, useState } from "react";
import { Badge, Button, Card, Group, Modal, Progress, Stack, Tabs, Text, TextInput } from "@mantine/core";
import { IconSearch } from "@tabler/icons-react";
import { api, type CatalogRecipe, type Crafter, type FindResult, type Meta, type PriceFind, type ProfessionsData, type RecipeCost, type UnlistedRecipe } from "../api";
import { ItemName } from "../components/ItemName";
import { GameIcon } from "../components/Icons";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { RecipePicker } from "../components/RecipePicker";
import { useLive } from "../hooks/useLive";
import { classColour } from "../theme";
import css from "./professions.module.css";

/** A crafter on one line: class icon, the character in its class colour, the member dimmed (a name — nobody is pinged). */
function Who({ meta, c }: { meta: Meta; c: Pick<Crafter, "label" | "member" | "cls"> }) {
  return (
    <Group gap={6} wrap="nowrap" className={css.who}>
      {c.cls && <GameIcon meta={meta} kind="class" id={c.cls} size={20} title={c.cls} />}
      <Text size="sm" fw={600} c={c.cls ? classColour(meta, c.cls) : undefined} className={css.name}>{c.label}</Text>
      {c.member !== c.label && <Text size="xs" c="dimmed" className={css.name}>{c.member}</Text>}
    </Group>
  );
}

/** Highest skill first; a skill not said yet last (the server's order too). */
const bySkill = (a: Crafter, b: Crafter) => (b.skill ?? -1) - (a.skill ?? -1);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
/** "Makes 1–4 " / "Makes " — the words before the game's own item (printed through ItemName, so it links). */
function makesWords(m: NonNullable<CatalogRecipe["makes"]>): string {
  const n = m.quantity_max && m.quantity_max !== m.quantity ? `${m.quantity}–${m.quantity_max} ` : m.quantity > 1 ? `${m.quantity} ` : "";
  return `Makes ${n}`;
}

/** A recipe's cost to make (auction prices §4.4), every number and sentence the server's: one line per reagent — the
 *  reagent, how many, its lowest price each and the line's total, or "none listed" — then the server's cost sentence and
 *  what a craft makes at its own lowest price. Nothing when the server sends no cost. */
function CostLines({ cost }: { cost: RecipeCost }) {
  return (
    <Stack gap={0} className={css.cost}>
      {cost.lines.map((x) => (
        <Text key={x.id} size="xs" className={css.costLine}>
          <ItemName id={x.id} name={x.name} /> × {x.quantity}
          <Text span size="xs" c={x.total_words ? undefined : "dimmed"}> · {x.total_words ? `${x.unit_words} each · ${x.total_words}` : x.unit_words}</Text>
        </Text>
      ))}
      <Text size="xs" fw={600} mt={2}>{cost.sentence}</Text>
      {cost.made_words && <Text size="xs" c="dimmed">{cost.made_words}</Text>}
    </Stack>
  );
}

/** The price box (auction prices §4.5): part of an item's name → the items it matches with their lowest price and how many
 *  are listed, from GET /api/prices/find; the empty answer, how recent the prices are and "N more" are the server's words.
 *  An answer for text no longer in the box is dropped. */
function PriceBox({ onWords }: { onWords: (w: string) => void }) {
  const [q, setQ] = useState("");
  const [found, setFound] = useState<PriceFind | null>(null);
  const want = useRef("");
  want.current = q.trim();
  useEffect(() => {
    const text = q.trim();
    if (!text) { setFound(null); return; }
    const t = setTimeout(() => {
      api.get<PriceFind>(`/api/prices/find?q=${encodeURIComponent(text)}`)
        .then((d) => { if (want.current === d.q.trim()) { setFound(d); onWords(d.words); } }).catch(fail);
    }, 250);
    return () => clearTimeout(t);
  }, [q]);  // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Card>
      <CardHeader title="What does it go for?" hint="the lowest price listed on the guild's auction house" />
      <Stack gap="sm" px="md" py="sm">
        <TextInput leftSection={<IconSearch size={16} />} placeholder="part of an item's name, like arcanite bar" value={q} aria-label="Search an item's price"
          onChange={(e) => setQ(e.currentTarget.value)} maw={520} />
        {found !== null && found.rows.length === 0 && <Text size="sm" c="dimmed" style={{ whiteSpace: "pre-line" }}>{found.message}</Text>}
        {found !== null && found.rows.length > 0 && (
          <>
            {found.words && <Text size="xs" c="dimmed">{found.words}</Text>}
            <div className={css.prices}>
              {found.rows.map((p) => (
                <div key={p.id} className={css.priceRow}>
                  <Text size="sm" fw={600} className={css.name}><ItemName id={p.id} name={p.name} /></Text>
                  <Text size="sm" c={p.qty ? undefined : "dimmed"}>{p.low_words}</Text>
                  <Text size="sm" c="dimmed">{p.qty ? `${p.qty} listed` : ""}</Text>
                </div>
              ))}
            </div>
            {found.more && <Text size="sm" c="dimmed">{found.more} Type more of the name to narrow it.</Text>}
          </>
        )}
      </Stack>
    </Card>
  );
}
/** A recipe a price line is about: it has a cost, or reagents a cost would price (then the line says why none shows —
 *  prices not read yet, or more than a day old). */
const priced = (r: CatalogRecipe) => !!r.cost || !!r.reagents?.length;
const ADD_HOW = "A crafter adds it on the Me page or with /me professions.";
const NOT_LISTED_GROUP = "Not in the game's list";

/** The Professions page (design §5.60, §5.64), for every member: the finder — part of a recipe's name searches the game's
 *  whole list (with it), what somebody makes first; each hit shows its category, what it makes, its reagents and its
 *  crafters highest skill first, or the server's "Nobody in the guild makes … yet." Then a tab per profession: its
 *  crafters with a skill bar, and — with the game's list — what the guild makes, grouped by the game's category (without
 *  the list, each crafter's typed recipes as chips, as before). Officers see the recipes typed before the list that the
 *  list lacks, with Pick the recipe… / Remove; the server's sentences show before anything is written. */
export function ProfessionsPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<ProfessionsData | null>(null);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<FindResult | null>(null);
  const [tab, setTab] = useState<string | null>(null);
  const [pick, setPick] = useState<UnlistedRecipe | null>(null);
  const qRef = useRef("");
  qRef.current = q;
  const cap = meta.professions?.skill_cap || 300;
  const fromList = !!meta.professions?.recipe_list;
  const [confirm, confirmDialog] = useConfirm();
  /** auction prices (§4.4): prices show once the owner picked the auction house; `priceWords` = how recent they are (the
   *  server's sentence: /api/meta's, then each price answer's — a page left open hears the hourly check) */
  const pricesOn = !!meta.prices?.on;
  const [priceWords, setPriceWords] = useState(pricesOn ? meta.prices?.words || "" : "");  // off: the owner's one line is printed once, above

  const find = (text: string) => {
    if (!text.trim()) { setFound(null); return; }
    api.get<FindResult>(`/api/professions/find?q=${encodeURIComponent(text.trim())}`)
      .then((d) => { if (d.q.trim() === qRef.current.trim()) setFound(d); }).catch(fail);
  };
  const load = () => {
    api.get<ProfessionsData>("/api/professions").then(setData).catch(fail);
    if (qRef.current.trim()) find(qRef.current);
    if (pricesOn) api.get<PriceFind>("/api/prices/find?q=").then((d) => setPriceWords(d.words)).catch(() => undefined);
  };
  useEffect(() => { load(); }, []);
  useLive(load, ["member", "config", "prices"]);
  useEffect(() => { const t = setTimeout(() => find(q), 250); return () => clearTimeout(t); }, [q]);

  const profs = data?.professions || [];
  const makes = (id: string) => !!meta.professions?.list.find((d) => d.id === id)?.makes;  // the server says which hold recipes (R7)
  const pname = (id: string) => profs.find((p) => p.id === id)?.name || meta.professions?.list.find((p) => p.id === id)?.name || id;
  const shown = tab && profs.some((p) => p.id === tab) ? tab : (profs.find((p) => p.crafters.length) || profs[0])?.id || null;
  const recipesOf = (c: Crafter) => (data?.recipes || []).filter((r) => r.profession === c.profession && r.crafters.some((x) => x.uid === c.uid && x.label === c.label));
  const clsOf = (uid: string, label: string) => profs.flatMap((p) => p.crafters).find((c) => c.uid === uid && c.label === label)?.cls ?? null;
  const unlisted = data?.officer ? data.unmatched || [] : [];

  /** An officer's fix of one recipe the game's list lacks: the server's preview sentences in the confirm, then the write. */
  const fix = async (row: UnlistedRecipe, recipe: CatalogRecipe | null) => {
    const payload = { uid: row.uid, label: row.label, profession: row.profession, name: row.name, recipe_id: recipe ? recipe.id : null };
    try {
      const p = await api.post<{ sentences: string[]; message: string }>("/api/professions/unmatched", { ...payload, preview: true });
      const yes = await confirm({ title: recipe ? `Pick ${recipe.name}` : `Remove ${row.name}`, message: p.sentences.join("\n"),
        confirmLabel: recipe ? "Pick this recipe" : "Remove", color: recipe ? undefined : "red" });
      if (!yes) return;
      const d = await api.post<{ sentences: string[]; message: string }>("/api/professions/unmatched", payload);
      ok(d.message);
      load();
    } catch (e) { fail(e); }
  };

  return (
    <Stack gap="lg" className={css.touch}>
      <PageTitle title="Professions" intro={fromList
        ? "Who can make what. Search the game's recipes, or open a profession to see its crafters. Add your own on the Me page or with /me professions."
        : "Who can make what. Search a recipe, or open a profession to see its crafters. Add your own on the Me page or with /me professions."} />
      <TextInput leftSection={<IconSearch size={16} />} placeholder="part of a recipe's name, like ironfeather" value={q} aria-label="Search a recipe"
        onChange={(e) => setQ(e.currentTarget.value)} maw={520} />
      {!pricesOn && meta.prices?.words && <Text size="sm" c="dimmed">{meta.prices.words}</Text>}
      <Card>
        <CardHeader title={found === null ? "Type a recipe's name to see who makes it" : `${plural(found.total, "recipe matches", "recipes match")} "${found.q.trim()}"`} />
        {found !== null && found.recipes.length === 0 && (
          <Text size="sm" c="dimmed" px="md" py="sm">{found.message}{fromList ? "" : ` ${ADD_HOW}`}</Text>
        )}
        {found !== null && found.recipes.some(priced) && priceWords && (
          <Text size="xs" c="dimmed" px="md" pt="sm">{priceWords}</Text>
        )}
        {found !== null && found.recipes.length > 0 && (
          <div className={css.hits}>
            {found.recipes.map((r) => (
              <div key={r.id ?? `${r.profession}:${r.key}`} className={css.hit}>
                <Group wrap="nowrap" align="flex-start" gap="sm">
                  <GameIcon meta={meta} kind="profession" id={r.profession} size={28} title={pname(r.profession)} />
                  <Stack gap={2} className={css.what}>
                    <Text fw={700}><ItemName id={r.makes?.id} name={r.name} /></Text>
                    {r.category && <Text size="xs" c="dimmed">{r.category}</Text>}
                    {fromList && r.listed === false && <Text size="xs" c="yellow.4">{NOT_LISTED_GROUP}</Text>}
                    {r.makes && <Text size="xs">{makesWords(r.makes)}<ItemName id={r.makes.id} name={r.makes.name} /></Text>}
                    {!!r.reagents?.length && !r.cost && (
                      <Text size="xs" c="dimmed">Reagents: {r.reagents.map((x, i) => <span key={i}>{i ? ", " : ""}{x.quantity} × <ItemName id={x.id} name={x.name} /></span>)}</Text>
                    )}
                    {r.cost && <CostLines cost={r.cost} />}
                  </Stack>
                </Group>
                {r.crafters.length ? (
                  <Stack gap={4} mt={8}>
                    {[...r.crafters].sort(bySkill).map((c) => (
                      <Group key={c.uid + c.label} justify="space-between" wrap="nowrap" gap="sm" className={css.crafter}>
                        <Who meta={meta} c={c} />
                        {c.skill != null && <Text fw={700} size="sm" c={c.skill >= cap ? "teal.3" : undefined}>{c.skill}</Text>}
                      </Group>
                    ))}
                  </Stack>
                ) : <Text size="sm" c="dimmed" mt={8}>{r.nobody} {ADD_HOW}</Text>}
              </div>
            ))}
            {found.more && <Text size="sm" c="dimmed" px="md" py="sm">{found.more} Type more of the name to narrow it.</Text>}
          </div>
        )}
      </Card>

      {unlisted.length > 0 && (
        <Card>
          <CardHeader title={`${plural(unlisted.length, "recipe is", "recipes are")} not in the game's list`}
            hint="Typed before the bot knew the game's list. Pick the recipe each one meant, or remove it." />
          <div className={css.fixes}>
            {unlisted.map((row) => (
              <div key={`${row.uid}:${row.label}:${row.profession}:${row.name}`} className={css.fixRow}>
                <Who meta={meta} c={{ label: row.label, member: row.member, cls: clsOf(row.uid, row.label) }} />
                <Group gap={8} wrap="nowrap" className={css.what}>
                  <GameIcon meta={meta} kind="profession" id={row.profession} size={20} title={pname(row.profession)} />
                  <Text size="sm" className={css.name}>{row.name}</Text>
                </Group>
                <Group gap="xs" wrap="nowrap" className={css.fixActions}>
                  <Button size="xs" variant="default" onClick={() => setPick(row)}>Pick the recipe…</Button>
                  <Button size="xs" variant="subtle" color="red" onClick={() => fix(row, null)}>Remove</Button>
                </Group>
              </div>
            ))}
          </div>
        </Card>
      )}

      {pricesOn && <PriceBox onWords={setPriceWords} />}

      {data === null && <Text size="sm" c="dimmed">Loading…</Text>}
      {profs.length > 0 && (
        <Tabs value={shown} onChange={setTab}>
          <Tabs.List className={css.tabs} style={{ overflowX: "auto", flexWrap: "nowrap" }}>
            {profs.map((p) => (
              <Tabs.Tab key={p.id} value={p.id} leftSection={<GameIcon meta={meta} kind="profession" id={p.id} size={18} title={p.name} dim={!p.crafters.length} />}>
                <Text span size="sm" c={p.crafters.length ? undefined : "dimmed"} style={{ whiteSpace: "nowrap" }}>{p.name} · {p.crafters.length}</Text>
              </Tabs.Tab>
            ))}
          </Tabs.List>
          {profs.map((p) => (
            <Tabs.Panel key={p.id} value={p.id} pt="md">
              <Stack gap="md">
                <Card>
                  {p.crafters.length === 0 ? (
                    <Text size="sm" c="dimmed" px="md" py="sm">Nobody has registered {p.name} yet. A crafter adds it on the Me page, with /me professions, or by saying so in the professions channel.</Text>
                  ) : (
                    <div className={css.crafters}>
                      {p.crafters.map((c) => {
                        const mine = fromList ? [] : recipesOf(c);
                        return (
                          <div key={c.uid + c.label} className={fromList ? `${css.craftRow} ${css.craftRowShort}` : css.craftRow}>
                            <Group gap={8} wrap="nowrap"><Who meta={meta} c={c} />{c.specialization && <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>{c.specialization}</Text>}</Group>
                            {c.skill != null ? (
                              <Group gap={8} wrap="nowrap" className={css.skill}>
                                <Progress value={Math.min(100, (c.skill / cap) * 100)} color={c.skill >= cap ? "teal" : "teal.7"} size="sm" style={{ flex: 1 }} aria-label={`${c.skill} of ${cap}`} />
                                <Text fw={700} size="sm" c={c.skill >= cap ? "teal.3" : undefined} w={32} ta="right">{c.skill}</Text>
                              </Group>
                            ) : <Text size="xs" c="dimmed" className={css.skill}>skill not said yet</Text>}
                            {!fromList && (
                              <div className={css.recipes}>
                                {mine.length
                                  ? <Group gap={6} wrap="wrap">{mine.map((r) => <Badge key={r.key} variant="outline" color="gray" radius="xl" tt="none" fw={500}>{r.name}</Badge>)}</Group>
                                  : makes(p.id) && <Text size="xs" c="dimmed">no recipes on record — the crafter adds them on the Me page</Text>}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </Card>
                {fromList && p.crafters.length > 0 && makes(p.id) && (
                  <GuildMakes meta={meta} name={p.name} priceWords={priceWords} recipes={(data?.recipes || []).filter((r) => r.profession === p.id)} />
                )}
              </Stack>
            </Tabs.Panel>
          ))}
        </Tabs>
      )}

      {pick && (
        <Modal opened onClose={() => setPick(null)} title={`Pick the recipe ${pick.name} meant`} centered>
          <Stack gap="md">
            <Text size="sm">{pick.label}'s {pname(pick.profession)} lists {pick.name}, which is not in the game's list. Search the game's {pname(pick.profession)} recipes and pick the one it meant.</Text>
            <RecipePicker profession={pick.profession} initial={pick.name} ariaLabel={`Search the game's ${pname(pick.profession)} recipes`}
              onPick={(r) => { const row = pick; setPick(null); fix(row, r); }} />
            <Group justify="flex-end"><Button variant="default" size="sm" onClick={() => setPick(null)}>Cancel</Button></Group>
          </Stack>
        </Modal>
      )}
      {confirmDialog}
    </Stack>
  );
}

/** A profession's tab, with the game's list: what the guild makes, grouped by the game's category (the categories in
 *  order, a recipe typed before the list that the list lacks under its own heading last), each recipe with its crafters. */
function GuildMakes({ meta, name, recipes, priceWords }: { meta: Meta; name: string; recipes: CatalogRecipe[]; priceWords: string }) {
  const groups = new Map<string, CatalogRecipe[]>();
  for (const r of recipes) {
    const g = r.listed === false ? NOT_LISTED_GROUP : r.category || NOT_LISTED_GROUP;
    groups.set(g, [...(groups.get(g) || []), r]);
  }
  const order = [...groups.keys()].sort((a, b) => (a === NOT_LISTED_GROUP ? 1 : b === NOT_LISTED_GROUP ? -1 : a.localeCompare(b)));
  return (
    <Card>
      <CardHeader title="What the guild makes" />
      {recipes.some(priced) && priceWords && <Text size="xs" c="dimmed" px="md" pt="sm">{priceWords}</Text>}
      {recipes.length === 0 ? (
        <Text size="sm" c="dimmed" px="md" py="sm">Nobody has added a {name} recipe yet. A crafter adds theirs on the Me page or with /me professions.</Text>
      ) : (
        <div className={css.crafters}>
          {order.map((g) => (
            <div key={g} className={css.group}>
              <Text size="xs" fw={700} c={g === NOT_LISTED_GROUP ? "yellow.4" : "dimmed"} tt="uppercase" className={css.groupHead}>{g}</Text>
              {groups.get(g)!.sort((a, b) => a.name.localeCompare(b.name)).map((r) => (
                <div key={r.id ?? r.key} className={css.makeRow}>
                  <div className={css.what}>
                    <Text size="sm" fw={600} className={css.name}><ItemName id={r.makes?.id} name={r.name} /></Text>
                    {r.cost && <Text size="xs">{r.cost.sentence}</Text>}
                    {r.cost?.made_words && <Text size="xs" c="dimmed">{r.cost.made_words}</Text>}
                  </div>
                  <Group gap={12} wrap="wrap" className={css.what}>
                    {r.crafters.map((c) => <Who key={c.uid + c.label} meta={meta} c={c} />)}
                  </Group>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
