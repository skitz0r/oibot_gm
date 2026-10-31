import { useEffect, useRef, useState } from "react";
import { Badge, Button, Card, Group, Modal, Progress, Select, Stack, Tabs, Text, TextInput } from "@mantine/core";
import { IconSearch } from "@tabler/icons-react";
import { api, ApiError, type CatalogRecipe, type Crafter, type Meta, type ProfessionsData } from "../api";
import { GameIcon } from "../components/Icons";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { useLive } from "../hooks/useLive";
import { classColour } from "../theme";
import css from "./professions.module.css";

type Fix = { verb: "rename" | "merge"; recipe: CatalogRecipe };

/** A crafter on one line: class icon, the character in its class colour, the member dimmed (a name — nobody is pinged). */
function Who({ meta, c }: { meta: Meta; c: Crafter }) {
  return (
    <Group gap={6} wrap="nowrap" className={css.who}>
      {c.cls && <GameIcon meta={meta} kind="class" id={c.cls} size={20} title={c.cls} />}
      <Text size="sm" fw={600} c={c.cls ? classColour(meta, c.cls) : undefined} className={css.name}>{c.label}</Text>
      {c.member !== c.label && <Text size="xs" c="dimmed" className={css.name}>{c.member}</Text>}
    </Group>
  );
}

/** The Professions page (design §5.60), for every member: the finder — a typed recipe name and who makes it, highest
 *  skill first — then a tab per profession listing its crafters with a skill bar and their notable recipes. Officers
 *  rename a misspelt recipe or merge two spellings; the server's sentences show before anything is written. */
export function ProfessionsPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<ProfessionsData | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<CatalogRecipe[] | null>(null);
  const [tab, setTab] = useState<string | null>(null);
  const [fix, setFix] = useState<Fix | null>(null);
  const qRef = useRef("");
  qRef.current = q;
  const cap = meta.professions?.skill_cap || 300;
  const [confirm, confirmDialog] = useConfirm();

  const find = (text: string) => {
    if (!text.trim()) { setHits(null); return; }
    api.get<{ q: string; recipes: CatalogRecipe[] }>(`/api/professions/find?q=${encodeURIComponent(text.trim())}`)
      .then((d) => { if (d.q.trim() === qRef.current.trim()) setHits(d.recipes); }).catch(fail);
  };
  const load = () => {
    api.get<ProfessionsData>("/api/professions").then(setData).catch(fail);
    if (qRef.current.trim()) find(qRef.current);
  };
  useEffect(() => { load(); }, []);
  useLive(load, ["member", "config"]);
  useEffect(() => { const t = setTimeout(() => find(q), 250); return () => clearTimeout(t); }, [q]);

  const profs = data?.professions || [];
  const makes = (id: string) => !!meta.professions?.list.find((d) => d.id === id)?.makes;  // Fishing and the gathering trades hold none
  const pname = (id: string) => profs.find((p) => p.id === id)?.name || meta.professions?.list.find((p) => p.id === id)?.name || id;
  const shown = tab && profs.some((p) => p.id === tab) ? tab : (profs.find((p) => p.crafters.length) || profs[0])?.id || null;
  const recipesOf = (c: Crafter) => (data?.recipes || []).filter((r) => r.profession === c.profession && r.crafters.some((x) => x.uid === c.uid && x.label === c.label));
  const byCount = (n: number) => `registered by ${n} crafter${n === 1 ? "" : "s"}`;

  return (
    <Stack gap="lg" className={css.touch}>
      <PageTitle title="Professions" intro="Who can make what. Search a recipe, or open a profession to see its crafters. Add your own on the Me page or with /me professions." />
      <TextInput leftSection={<IconSearch size={16} />} placeholder="a recipe's name, like Mooncloth Bag" value={q} aria-label="Search a recipe"
        onChange={(e) => setQ(e.currentTarget.value)} maw={520} />
      <Card>
        <CardHeader title={hits === null ? "Type a recipe's name to see who makes it" : `${hits.length} recipe${hits.length === 1 ? "" : "s"} match "${q.trim()}"`} />
        {hits !== null && hits.length === 0 && (
          <Text size="sm" c="dimmed" px="md" py="sm">No registered recipe matches "{q.trim()}". A crafter adds a recipe on the Me page, with /me professions, or by saying so in the professions channel.</Text>
        )}
        {hits !== null && hits.length > 0 && (
          <div className={css.hits}>
            {hits.map((r) => (
              <div key={r.key} className={css.hit}>
                <Group justify="space-between" wrap="wrap" gap="xs">
                  <div>
                    <Text fw={700}>{r.name}</Text>
                    <Text size="xs" c="dimmed">{pname(r.profession)} · {byCount(r.crafters.length)}</Text>
                  </div>
                  {data?.officer && (
                    <Group gap="xs">
                      <Button size="xs" variant="default" onClick={() => setFix({ verb: "rename", recipe: r })}>Rename…</Button>
                      <Button size="xs" variant="default" onClick={() => setFix({ verb: "merge", recipe: r })}>Merge…</Button>
                    </Group>
                  )}
                </Group>
                <Stack gap={4} mt={6}>
                  {[...r.crafters].sort((a, b) => b.skill - a.skill).map((c) => (
                    <Group key={c.uid + c.label} justify="space-between" wrap="nowrap" gap="sm" className={css.crafter}>
                      <Who meta={meta} c={c} />
                      <Group gap={5} wrap="nowrap">
                        <GameIcon meta={meta} kind="profession" id={c.profession} size={20} title={pname(c.profession)} />
                        <Text fw={700} size="sm" c={c.skill >= cap ? "teal.3" : undefined}>{c.skill}</Text>
                      </Group>
                    </Group>
                  ))}
                </Stack>
              </div>
            ))}
          </div>
        )}
      </Card>

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
              <Card>
                {p.crafters.length === 0 ? (
                  <Text size="sm" c="dimmed" px="md" py="sm">Nobody has registered {p.name} yet. A crafter adds it on the Me page, with /me professions, or by saying so in the professions channel.</Text>
                ) : (
                  <div className={css.crafters}>
                    {p.crafters.map((c) => {
                      const mine = recipesOf(c);
                      return (
                        <div key={c.uid + c.label} className={css.craftRow}>
                          <Who meta={meta} c={c} />
                          <Group gap={8} wrap="nowrap" className={css.skill}>
                            <Progress value={Math.min(100, (c.skill / cap) * 100)} color={c.skill >= cap ? "teal" : "teal.7"} size="sm" style={{ flex: 1 }} aria-label={`${c.skill} of ${cap}`} />
                            <Text fw={700} size="sm" c={c.skill >= cap ? "teal.3" : undefined} w={32} ta="right">{c.skill}</Text>
                          </Group>
                          <div className={css.recipes}>
                            {mine.length
                              ? <Group gap={6} wrap="wrap">{mine.map((r) => <Badge key={r.key} variant="outline" color="gray" radius="xl" tt="none" fw={500}>{r.name}</Badge>)}</Group>
                              : makes(p.id) && <Text size="xs" c="dimmed">no notable recipes registered — the crafter adds them on the Me page</Text>}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>
            </Tabs.Panel>
          ))}
        </Tabs>
      )}

      {fix && <FixModal fix={fix} recipes={data?.recipes || []} confirm={confirm} onClose={() => setFix(null)} onDone={() => { setFix(null); load(); }} />}
      {confirmDialog}
    </Stack>
  );
}

/** An officer's Rename… / Merge… of one recipe: one input (the new spelling, or the recipe to merge into, picked from
 *  the same profession), then the server's preview sentences in the confirm, then the write. A refusal is its sentence. */
function FixModal({ fix, recipes, confirm, onClose, onDone }: {
  fix: Fix; recipes: CatalogRecipe[]; onClose: () => void; onDone: () => void;
  confirm: ReturnType<typeof useConfirm>[0];
}) {
  const r = fix.recipe;
  const rename = fix.verb === "rename";
  const [value, setValue] = useState<string>(rename ? r.name : "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const others = recipes.filter((x) => x.profession === r.profession && x.key !== r.key).map((x) => ({ value: x.key, label: x.name }));
  const url = rename ? "/api/professions/rename" : "/api/professions/merge";
  const payload = rename ? { key: r.key, name: value } : { key: r.key, into: value };
  const go = async () => {
    setError(null); setBusy(true);
    try {
      const p = await api.post<{ sentences: string[]; message: string }>(url, { ...payload, preview: true });
      const yes = await confirm({ title: rename ? `Rename ${r.name}` : `Merge ${r.name}`, message: p.sentences.join("\n"), confirmLabel: rename ? "Rename" : "Merge" });
      if (!yes) return;
      const d = await api.post<{ sentences: string[]; message: string }>(url, payload);
      ok(d.message);
      onDone();
    } catch (e) {
      if (e instanceof ApiError) setError(e.message); else fail(e);
    } finally { setBusy(false); }
  };
  return (
    <Modal opened onClose={onClose} title={rename ? `Rename ${r.name}` : `Merge ${r.name}`} centered>
      <Stack gap="md">
        {rename
          ? <TextInput label="The spelling every record will read" value={value} onChange={(e) => setValue(e.currentTarget.value)} data-autofocus />
          : others.length
            ? <Select label="The recipe it becomes" data={others} value={value || null} onChange={(v) => setValue(v || "")} searchable />
            : <Text size="sm" c="dimmed">No other recipe of this profession is registered to merge into. Rename it instead.</Text>}
        {error && <Text size="sm" c="red.4">{error}</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" loading={busy} disabled={!value.trim()} onClick={go}>{rename ? "Rename…" : "Merge…"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
