import type { EventPerson, EventView, Meta, Person } from "../../api";
import { ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { fail } from "../Page";
import { MemberMenu, SomeoneElse, useAnswerBox, type SwitchItem } from "../board/MemberMenu";
import { useSwitchPress } from "./RoleSwitches";
import { SignupTabs } from "./SignupTabs";

/** Signups: four tabs — Joined (shown first), Bench, No thanks, No answer yet — each with the server's count, drawn by
 *  the shared SignupTabs (grouped by class, the server's labels and empty sentences); each row ends in its ⋮ (the
 *  on-behalf list, the member's state now) while the event is live — its Join / Bench / No thanks go to the server's
 *  answer box, its callouts and pins keep the menu's own confirm. Answer for someone (a member
 *  with no answer yet) at the right while the event is live — kept mounted (greyed with nobody left), so a box it has
 *  open survives a reload and its own refresh brings the server's refusal. The rows' answer box lives HERE, not on a
 *  row: a member who moves to another list while their box is open keeps it. A member the Role switches section has a
 *  row for gets that row in their ⋮ too (after Join, before Bench): its spec icon and page_words.switch_item over the
 *  row's own button word and `to`, pressed through the section's own useSwitchPress — its box lives here as well. */
export function SignupsSection({ ev, meta, onDone }: { ev: EventView; meta: Meta; onDone: () => void }) {
  const [answer, answerDialog] = useAnswerBox(ev.key, onDone);
  const [switchPress, switchDialog] = useSwitchPress(ev, onDone);
  const live = ev.state === "open" || ev.state === "locked";
  /** A row's answer through the server's box, the member fixed: their own character and spec unless the item names one. */
  const answerFor = (p: EventPerson) => (action: string, character: string | null) => {
    const person: Person = { uid: p.uid, display_name: p.display_name,
      characters: ev.characters[p.uid] || [{ label: p.character, cls: p.cls, spec: p.spec, role: "", is_main: true }] };
    const ch = character ? person.characters.find((c) => c.label === character) : undefined;
    answer([person], { person, action, character: character || p.character, spec: ch ? ch.spec : p.spec,
      home: p.character ? { character: p.character, spec: p.spec } : undefined });
  };
  /** The member's Role switches rows as ⋮ items: the spec icon alone (the class from their character), the server's words. */
  const switchesFor = (p: EventPerson): SwitchItem[] => (ev.switches?.rows || []).filter((r) => r.uid === p.uid).map((r) => {
    const cls = (ev.characters[p.uid] || []).find((c) => c.label === r.character)?.cls || p.cls;
    return { key: `${r.character}:${r.spec}:${r.part}`,
      icon: cls ? <GameIcon meta={meta} kind="spec" id={`${cls}:${r.spec}`} size={16} title={`${r.spec} ${cls}`} /> : undefined,
      label: meta.page_words.switch_item.split("{button}").join(r.button).split("{to}").join(r.to),
      onPick: () => { switchPress(r, { scope: "event" }).catch(fail); } };  // a Signups row's switch is the whole event (issue 15)
  });
  return (
    <ViewSection title="Signups" action={live ? <SomeoneElse run={ev.key} people={ev.people} onDone={onDone} /> : undefined}>
      <SignupTabs tabs={["in", "sub", "out", "none"]} meta={meta} rows={ev.signups.rows}
        counts={{ in: ev.signups.joined, sub: ev.signups.bench, out: ev.signups.no_thanks, none: ev.signups.no_answer }}
        right={live ? (x) => { const p = x as EventPerson; return <MemberMenu run={ev.key} uid={p.uid} name={p.display_name} onDone={onDone} onAnswer={answerFor(p)} switches={switchesFor(p)} />; } : undefined} />
      {answerDialog}
      {switchDialog}
    </ViewSection>
  );
}
