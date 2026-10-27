import { Badge, Tooltip } from "@mantine/core";
import type { Meta } from "../api";

/** the chip's colour per level: thin is slate, never amber (amber marks a value changed from the default, site-wide) */
const COLOUR: Record<string, string> = { green: "teal", amber: "gray", red: "red" };

/** A coverage level as a word — ready / thin / short (constants.LEVEL_WORDS through /api/meta.level_words) — with why in
 *  its tooltip. The one chip for Readiness (who could play against the raid) and the Rosters page's Regulars (the
 *  schedule's regulars against the minimums): design §5.56 phase 2. */
export function LevelChip({ meta, level, why, size }: { meta: Meta; level: string; why?: string; size?: "xs" | "sm" | "md" }) {
  return (
    <Tooltip label={why || meta.level_words[level] || level} disabled={!why}>
      <Badge size={size} variant="light" color={COLOUR[level] || "gray"} tt="none">{meta.level_words[level] || level}</Badge>
    </Tooltip>
  );
}
