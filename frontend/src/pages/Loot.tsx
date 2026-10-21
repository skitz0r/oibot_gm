import { Stack, Tabs } from "@mantine/core";
import { useSearchParams } from "react-router-dom";
import type { Meta } from "../api";
import { PageTitle } from "../components/Page";
import { Tonight } from "../components/loot/Tonight";
import { History, Precedents } from "../components/loot/History";
import { Items } from "../components/loot/Items";
import { Wishlists } from "../components/loot/Wishlists";
import { Companions } from "../components/loot/Companions";

const TABS = ["tonight", "history", "precedents", "items", "wishlists", "companions"] as const;

/** Loot (officers): the council's web surface — Tonight (live, the same verbs as the thread), History, Precedents,
 *  Items (the untiered list, the tier grid, Claude drafts, CSV), Wishlists and Companions (presence; feed-token names for the owner). The thread in Discord is the other surface; both act on
 *  one council and see each other's changes at once. */
export function LootPage({ meta }: { meta: Meta }) {
  const [params, setParams] = useSearchParams();
  const tab = (TABS as readonly string[]).includes(params.get("tab") || "") ? (params.get("tab") as string) : "tonight";
  const go = (t: string) => setParams((p) => { const n = new URLSearchParams(p); n.set("tab", t); return n; }, { replace: true });
  return (
    <Stack gap="lg">
      <PageTitle title="Loot" intro="The council on the site: tick drops, distribute, confirm or override — the same council as the thread in Discord, live on both. Members see their own loot and the award log; scores and reasons stay here." />
      <Tabs value={tab} onChange={(v) => v && go(v)} keepMounted={false}>
        <Tabs.List mb="md">
          <Tabs.Tab value="tonight">Tonight</Tabs.Tab>
          <Tabs.Tab value="history">History</Tabs.Tab>
          <Tabs.Tab value="precedents">Precedents</Tabs.Tab>
          <Tabs.Tab value="items">Items</Tabs.Tab>
          <Tabs.Tab value="wishlists">Wishlists</Tabs.Tab>
          <Tabs.Tab value="companions">Companions</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="tonight"><Tonight meta={meta} goItems={() => go("items")} /></Tabs.Panel>
        <Tabs.Panel value="history"><History /></Tabs.Panel>
        <Tabs.Panel value="precedents"><Precedents /></Tabs.Panel>
        <Tabs.Panel value="items"><Items meta={meta} /></Tabs.Panel>
        <Tabs.Panel value="wishlists"><Wishlists meta={meta} /></Tabs.Panel>
        <Tabs.Panel value="companions"><Companions /></Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
