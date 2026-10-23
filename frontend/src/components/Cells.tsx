import { Badge, Box, Group, Text, Tooltip } from "@mantine/core";
import type { Meta } from "../api";
import { GameIcon } from "./Icons";
import { classColour } from "../theme";

/** A member-table character cell: class icon + character name; the class name lives in the tooltip. */
export function CharacterCell({ meta, cls, label, size = 30 }: { meta: Meta; cls: string; label: string; size?: number }) {
  return (
    <Group gap="sm" wrap="nowrap">
      <Tooltip label={cls}><Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="class" id={cls} size={size} title={cls} /></Box></Tooltip>
      <Text fw={600} c={classColour(meta, cls)} truncate style={{ whiteSpace: "nowrap" }}>{label}</Text>
    </Group>
  );
}

/** A spec on its own: the icon denotes role + spec; the tooltip carries the names. */
export function SpecCell({ meta, cls, spec, role }: { meta: Meta; cls: string; spec: string; role: string }) {
  return (
    <Tooltip label={`${spec} · ${role}`}>
      <Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="spec" id={`${cls}:${spec}`} size={26} title={`${spec} · ${role}`} /></Box>
    </Tooltip>
  );
}

/** A character's status in a table (Me › Characters, Members): planned (no name yet) · unconfirmed · confirmed — the
 *  word in full, never an ellipsis: lower case and small, and the badge never shrinks below its word (its column gives
 *  it the room). */
export function StatusBadge({ named, confirmed }: { named: boolean; confirmed: boolean }) {
  const [word, color, variant] = !named ? ["planned", "gray", "outline"] : confirmed ? ["confirmed", "teal", "light"] : ["unconfirmed", "yellow", "light"];
  return <Badge size="sm" variant={variant} color={color} tt="none" style={{ flexShrink: 0, overflow: "visible" }} styles={{ label: { overflow: "visible", textOverflow: "clip" } }}>{word}</Badge>;
}
