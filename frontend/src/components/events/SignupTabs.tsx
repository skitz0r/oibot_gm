import { useState } from "react";
import { Box, Group, Tabs, Text, Tooltip } from "@mantine/core";
import type { EventPerson, MemberPerson, Meta, SignupTab } from "../../api";
import { GameIcon } from "../Icons";
import css from "../../pages/listpage.module.css";

type AnyPerson = EventPerson | MemberPerson;

/** The list's members by class, in the game's class order (the signup sheet's layout: a class per column); members
 *  with no character yet come last, under no icon. */
function byClass<P extends AnyPerson>(people: P[], meta: Meta): { cls: string; people: P[] }[] {
  const order = Object.keys(meta.classes);
  const groups = new Map<string, P[]>();
  for (const p of people) groups.set(p.cls || "", [...(groups.get(p.cls || "") || []), p]);
  const rank = (c: string) => (c === "" ? order.length + 1 : order.indexOf(c) < 0 ? order.length : order.indexOf(c));
  return [...groups.entries()].sort((x, y) => rank(x[0]) - rank(y[0]) || x[0].localeCompare(y[0])).map(([cls, people]) => ({ cls, people }));
}

/** One member on a line: the spec icon alone (it says role and spec — the Iconography rule), the name, the character
 *  when it is another name, the sheet's ⚑ on a No thanks the member was called out of (raid_views.OUT_MARK; the
 *  server's words in its tooltip), and whatever the page puts at the right (the officers' row ⋮). */
function PersonLine({ p, meta, right }: { p: AnyPerson; meta: Meta; right?: React.ReactNode }) {
  const callout = "callout" in p && p.callout;
  return (
    <Group gap="xs" wrap="nowrap" justify="space-between" style={{ minWidth: 0 }}>
      <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
        {p.cls && p.spec && (
          <Tooltip label={`${p.spec} ${p.cls}`}>
            <Box style={{ display: "inline-flex", flex: "none" }}><GameIcon meta={meta} kind="spec" id={`${p.cls}:${p.spec}`} size={20} title={`${p.spec} ${p.cls}`} /></Box>
          </Tooltip>
        )}
        <Text size="sm" fw={600} truncate>{p.display_name}</Text>
        {p.character && p.character !== p.display_name && <Text size="sm" c="dimmed" truncate>{p.character}</Text>}
        {callout && <Tooltip label={meta.page_words.called_out}><Text span size="sm" aria-label={meta.page_words.called_out}>⚑</Text></Tooltip>}
      </Group>
      {right}
    </Group>
  );
}

/** The Signups tabs: one tab per key in `tabs` (the first shown first), each with its label and the server's count; a
 *  tab lists its members grouped by class, a class per column under its class icon and count (the Discord sheet's
 *  layout), one member per line; an empty tab says so in the server's sentence. The labels and empty sentences are
 *  meta.page_words.signup_tabs. `right` draws the end of a row (the officers' ⋮; nothing on the members' page). */
export function SignupTabs({ tabs, counts, rows, meta, right }: {
  tabs: SignupTab[]; counts: Record<string, number>; rows: Record<string, MemberPerson[] | EventPerson[]>; meta: Meta;
  right?: (p: EventPerson | MemberPerson) => React.ReactNode;
}) {
  const [tab, setTab] = useState<SignupTab>(tabs[0]);
  const words = meta.page_words.signup_tabs;
  return (
    <Tabs value={tab} onChange={(v) => v && setTab(v as SignupTab)} keepMounted={false}>
      <Tabs.List>
        {tabs.map((t) => (
          <Tabs.Tab key={t} value={t} rightSection={<Text span size="sm" fw={700} c={counts[t] ? undefined : "dimmed"}>{counts[t] || 0}</Text>}>{words[t].label}</Tabs.Tab>
        ))}
      </Tabs.List>
      {tabs.map((t) => {
        const groups = byClass<AnyPerson>(rows[t] || [], meta);
        return (
          <Tabs.Panel key={t} value={t}>
            {groups.length === 0 ? <Text size="sm" c="dimmed" mt="sm">{words[t].empty}</Text> : (
              <div className={css.classes}>
                {groups.map((g) => (
                  <div key={g.cls} className={css.classGroup}>
                    <Group gap={6} wrap="nowrap" mih={22}>
                      {g.cls && <GameIcon meta={meta} kind="class" id={g.cls} size={22} title={g.cls} />}
                      <Text size="sm" fw={700} c="dimmed">{g.people.length}</Text>
                    </Group>
                    {g.people.map((p, i) => <PersonLine key={"uid" in p ? `${p.uid}:${p.character}` : `${p.display_name}:${p.character}:${i}`} p={p} meta={meta} right={right?.(p)} />)}
                  </div>
                ))}
              </div>
            )}
          </Tabs.Panel>
        );
      })}
    </Tabs>
  );
}
