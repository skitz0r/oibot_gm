import { Progress, Select, Text } from "@mantine/core";
import type { RaidPage } from "../../api";
import { Facts, ViewSection } from "../Facts";
import type { RaidDraft } from "./RulesView";
import css from "../../pages/listpage.module.css";

const WEIGHTS = Array.from({ length: 7 }, (_, i) => String(i));

/** The Raids page's Priority view (design §7a): when the priority is used and that one-off events ignore it (the
 *  server's words), then the five weights as bars, highest first — the bar is the weight against the largest, the number
 *  beside it. In the edit mode each weight is a select from 0 to 6 (the draft shared with Rules). */
export function PriorityView({ page, draft, setDraft }: { page: RaidPage; draft: RaidDraft | null; setDraft: (d: RaidDraft) => void }) {
  const max = Math.max(1, ...page.priority.map((p) => p.value));
  return (
    <ViewSection title="Priority">
      <Facts items={[{ label: "Used when", value: page.used_when }, { label: "One-off events", value: page.one_off_rule }]} />
      <div style={{ marginTop: 14 }}>
        {page.priority.map((p) => (
          <div key={p.key} className={[css.bar, draft ? css.barEdit : ""].join(" ")}>
            <Text size="sm">{p.label}</Text>
            {draft ? (
              <Select aria-label={`${p.label}: priority`} w={100} data={WEIGHTS} allowDeselect={false} value={draft.weights[p.key] ?? String(p.value)}
                onChange={(v) => v !== null && setDraft({ ...draft, weights: { ...draft.weights, [p.key]: v } })} />
            ) : (
              <>
                <Progress value={(p.value / max) * 100} size="sm" color="teal" aria-label={p.label} />
                <Text size="sm" c="dimmed" style={{ fontVariantNumeric: "tabular-nums" }}>{p.value}</Text>
              </>
            )}
          </div>
        ))}
      </div>
    </ViewSection>
  );
}
