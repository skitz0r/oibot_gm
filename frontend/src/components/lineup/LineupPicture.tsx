import { Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { Link, StepNodes, nodesHeight, nodesPad } from "./LineupBuilder";
import css from "./lineup.module.css";

/** One step as the server describes it: the raid's name, its rosters, its start in words ('9:30 PM'), one roster's seats. */
export type PictureStep = { name: string; rosters: number; time_words: string; seats: number };

const HEAD = 48;  // the picture's head: Step n and its start, no controls

/** The lineup builder's picture, read-only (the Schedules page, a schedule's night on New event): each step's head
 *  (Step n and its start in the server's words), one node per roster, the same connectors. */
export function LineupPicture({ lineup }: { lineup: PictureStep[] }) {
  const phone = !!useMediaQuery("(max-width: 48em)");
  const height = nodesHeight(lineup.map((s) => s.rosters));
  return (
    <div className={css.track} style={{ "--head-h": `${HEAD}px` } as React.CSSProperties}>
      <div className={css.steps}>
        {lineup.map((s, i) => (
          <div key={`${s.name}${i}`} style={{ display: phone ? "block" : "flex" }}>
            {i > 0 && <Link from={lineup[i - 1].rosters} to={s.rosters} height={height} phone={phone} head={HEAD} />}
            <div className={css.step} role="group" aria-label={`Step ${i + 1}: ${s.name}`}>
              <div className={css.head}>
                <b>Step {i + 1}</b>
                {s.time_words && <Text size="sm"><Text span size="sm" c="dimmed">Starts </Text><span className={css.time}>{s.time_words}</span></Text>}
              </div>
              <StepNodes name={s.name} rosters={s.rosters} seats={s.seats} pad={phone ? 0 : nodesPad(s.rosters, height)} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
