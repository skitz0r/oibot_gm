import { useState } from "react";
import { Box, Group, Tabs, Text, Tooltip } from "@mantine/core";
import type { EventPerson, EventView, Meta, Person } from "../../api";
import { ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { MemberMenu, SomeoneElse, useAnswerBox } from "../board/MemberMenu";
import css from "../../pages/listpage.module.css";

type Row = "in" | "sub" | "out" | "none";
const TABS: { row: Row; label: string; empty: string; n: (s: EventView["signups"]) => number }[] = [
  { row: "in", label: "Joined", empty: "Nobody has joined yet.", n: (s) => s.joined },
  { row: "sub", label: "Bench", empty: "Nobody is on the bench.", n: (s) => s.bench },
  { row: "out", label: "No thanks", empty: "Nobody said No thanks.", n: (s) => s.no_thanks },
  { row: "none", label: "No answer yet", empty: "Everyone has answered.", n: (s) => s.no_answer },
];

/** The list's members by class, in the game's class order (the signup sheet's layout: a class per column); members
 *  with no character yet come last, under no icon. */
function byClass(people: EventPerson[], meta: Meta): { cls: string; people: EventPerson[] }[] {
  const order = Object.keys(meta.classes);
  const groups = new Map<string, EventPerson[]>();
  for (const p of people) groups.set(p.cls || "", [...(groups.get(p.cls || "") || []), p]);
  const rank = (c: string) => (c === "" ? order.length + 1 : order.indexOf(c) < 0 ? order.length : order.indexOf(c));
  return [...groups.entries()].sort((x, y) => rank(x[0]) - rank(y[0]) || x[0].localeCompare(y[0])).map(([cls, people]) => ({ cls, people }));
}

/** One member on a line: the spec icon alone (it says role and spec — the Iconography rule), the name, the character
 *  when it is another name, and the row ⋮ (the on-behalf list, the member's state now) while the event is live. Its
 *  Join / Bench / No thanks go to `onAnswer` (the server's answer box); its callouts and pins keep the menu's own confirm. */
function PersonLine({ p, meta, run, live, onDone, onAnswer }: {
  p: EventPerson; meta: Meta; run: string; live: boolean; onDone: () => void; onAnswer: (action: string, character: string | null) => void;
}) {
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
      </Group>
      {live && <MemberMenu run={run} uid={p.uid} name={p.display_name} onDone={onDone} onAnswer={onAnswer} />}
    </Group>
  );
}

/** Signups: four tabs — Joined (shown first), Bench, No thanks, No answer yet — each with the server's count. A tab lists
 *  its members grouped by class, a class per column under its class icon and count (the Discord sheet's layout), one
 *  member per line with the spec icon and the row ⋮; an empty tab says so in a sentence. Answer for someone (a member
 *  with no answer yet) at the right while the event is live — kept mounted (greyed with nobody left), so a box it has
 *  open survives a reload and its own refresh brings the server's refusal. The rows' answer box lives HERE, not on a
 *  row: a member who moves to another list while their box is open keeps it. */
export function SignupsSection({ ev, meta, onDone }: { ev: EventView; meta: Meta; onDone: () => void }) {
  const [tab, setTab] = useState<Row>("in");
  const [answer, answerDialog] = useAnswerBox(ev.key, onDone);
  const live = ev.state === "open" || ev.state === "locked";
  /** A row's answer through the server's box, the member fixed: their own character and spec unless the item names one. */
  const answerFor = (p: EventPerson) => (action: string, character: string | null) => {
    const person: Person = { uid: p.uid, display_name: p.display_name,
      characters: ev.characters[p.uid] || [{ label: p.character, cls: p.cls, spec: p.spec, role: "", is_main: true }] };
    const ch = character ? person.characters.find((c) => c.label === character) : undefined;
    answer([person], { person, action, character: character || p.character, spec: ch ? ch.spec : p.spec,
      home: p.character ? { character: p.character, spec: p.spec } : undefined });
  };
  return (
    <ViewSection title="Signups" action={live ? <SomeoneElse run={ev.key} people={ev.people} onDone={onDone} /> : undefined}>
      <Tabs value={tab} onChange={(v) => v && setTab(v as Row)} keepMounted={false}>
        <Tabs.List>
          {TABS.map((t) => (
            <Tabs.Tab key={t.row} value={t.row} rightSection={<Text span size="sm" fw={700} c={t.n(ev.signups) ? undefined : "dimmed"}>{t.n(ev.signups)}</Text>}>{t.label}</Tabs.Tab>
          ))}
        </Tabs.List>
        {TABS.map((t) => {
          const groups = byClass(ev.signups.rows[t.row], meta);
          return (
            <Tabs.Panel key={t.row} value={t.row}>
              {groups.length === 0 ? <Text size="sm" c="dimmed" mt="sm">{t.empty}</Text> : (
                <div className={css.classes}>
                  {groups.map((g) => (
                    <div key={g.cls} className={css.classGroup}>
                      <Group gap={6} wrap="nowrap" mih={22}>
                        {g.cls && <GameIcon meta={meta} kind="class" id={g.cls} size={22} title={g.cls} />}
                        <Text size="sm" fw={700} c="dimmed">{g.people.length}</Text>
                      </Group>
                      {g.people.map((p) => <PersonLine key={`${p.uid}:${p.character}`} p={p} meta={meta} run={ev.key} live={live} onDone={onDone} onAnswer={answerFor(p)} />)}
                    </div>
                  ))}
                </div>
              )}
            </Tabs.Panel>
          );
        })}
      </Tabs>
      {answerDialog}
    </ViewSection>
  );
}
