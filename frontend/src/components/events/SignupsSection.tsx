import { useState } from "react";
import { Box, Button, Group, Text, Tooltip } from "@mantine/core";
import type { EventPerson, EventView, Meta } from "../../api";
import { ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { MemberMenu, SomeoneElse } from "../board/MemberMenu";
import css from "../../pages/listpage.module.css";

type Row = "in" | "sub" | "out" | "none";
const COUNTS: { row: Row; label: string; n: (s: EventView["signups"]) => number }[] = [
  { row: "in", label: "Joined", n: (s) => s.joined },
  { row: "sub", label: "Bench", n: (s) => s.bench },
  { row: "out", label: "No thanks", n: (s) => s.no_thanks },
  { row: "none", label: "No answer yet", n: (s) => s.no_answer },
];

/** One member on a line: the spec icon alone (it says role and spec — the Iconography rule), the name, the character
 *  when it is another name, and the row ⋮ (the on-behalf list, the member's state now) while the event is live. */
function PersonLine({ p, meta, run, live, onDone }: { p: EventPerson; meta: Meta; run: string; live: boolean; onDone: () => void }) {
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
      {live && <MemberMenu run={run} uid={p.uid} name={p.display_name} onDone={onDone} />}
    </Group>
  );
}

/** Signups: the server's four counts (Joined, Bench, No thanks, No answer yet); each count is a button that unfolds
 *  its names, one member per line with the row ⋮. Answer for someone (a member with no answer yet) at the right while
 *  the event is live. */
export function SignupsSection({ ev, meta, onDone }: { ev: EventView; meta: Meta; onDone: () => void }) {
  const [open, setOpen] = useState<Row | null>(null);
  const live = ev.state === "open" || ev.state === "locked";
  const shown = open ? ev.signups.rows[open] : [];
  return (
    <ViewSection title="Signups" action={live && ev.people.length > 0 ? <SomeoneElse run={ev.key} people={ev.people} meta={meta} onDone={onDone} /> : undefined}>
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
          {shown.map((p) => <PersonLine key={`${p.uid}:${p.character}`} p={p} meta={meta} run={ev.key} live={live} onDone={onDone} />)}
        </div>
      )}
    </ViewSection>
  );
}
