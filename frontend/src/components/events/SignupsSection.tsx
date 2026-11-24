import { useState } from "react";
import { Box, Button, Group, Text, Tooltip } from "@mantine/core";
import type { EventPerson, EventView, Meta, Person } from "../../api";
import { ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { MemberMenu, SomeoneElse, useAnswerBox } from "../board/MemberMenu";
import css from "../../pages/listpage.module.css";

type Row = "in" | "sub" | "out" | "none";
const COUNTS: { row: Row; label: string; n: (s: EventView["signups"]) => number }[] = [
  { row: "in", label: "Joined", n: (s) => s.joined },
  { row: "sub", label: "Bench", n: (s) => s.bench },
  { row: "out", label: "No thanks", n: (s) => s.no_thanks },
  { row: "none", label: "No answer yet", n: (s) => s.no_answer },
];

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

/** Signups: the server's four counts (Joined, Bench, No thanks, No answer yet); a count above 0 is a button that
 *  unfolds its names, one member per line with the row ⋮; a 0 is plain text. Answer for someone (a member with no answer
 *  yet) at the right while the event is live — kept mounted (greyed with nobody left), so a box it has open survives a
 *  reload and its own refresh brings the server's refusal. The rows' answer box lives HERE, not on a row: a member
 *  who moves to another list while their box is open keeps it. */
export function SignupsSection({ ev, meta, onDone }: { ev: EventView; meta: Meta; onDone: () => void }) {
  const [open, setOpen] = useState<Row | null>(null);
  const [answer, answerDialog] = useAnswerBox(ev.key, onDone);
  const live = ev.state === "open" || ev.state === "locked";
  const shown = open ? ev.signups.rows[open] : [];
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
      <dl className={css.facts}>
        {COUNTS.map((c) => {
          const n = c.n(ev.signups);
          return (
            <div key={c.row}>
              <dt>{c.label}</dt>
              <dd>
                {n === 0 ? <Text fw={700} px={6}>{n}</Text> : (
                  <Button variant={open === c.row ? "light" : "subtle"} size="compact-md" px={6} fw={700}
                    aria-expanded={open === c.row} aria-controls="event-signups" aria-label={`${c.label}: ${n}`}
                    onClick={() => setOpen((o) => (o === c.row ? null : c.row))}>{n}</Button>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
      {open && (
        <div id="event-signups" className={css.people} aria-label={COUNTS.find((c) => c.row === open)!.label}>
          {shown.map((p) => <PersonLine key={`${p.uid}:${p.character}`} p={p} meta={meta} run={ev.key} live={live} onDone={onDone} onAnswer={answerFor(p)} />)}
        </div>
      )}
      {answerDialog}
    </ViewSection>
  );
}
