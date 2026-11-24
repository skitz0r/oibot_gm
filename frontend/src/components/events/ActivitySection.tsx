import { useState } from "react";
import { Button, Group, Stack, Text } from "@mantine/core";
import { ViewSection } from "../Facts";
import { pageWords } from "../ConfirmBox";

/** Activity · {n} lines: the event's log (every raid's lines, newest first, `when` in the server's words), folded until
 *  asked for. `action` sits at the heading's right (the quiet Cancel event). */
export function ActivitySection({ activity, action }: { activity: { when: string; line: string }[]; action?: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const n = activity.length;
  return (
    <ViewSection title={`Activity · ${n} ${n === 1 ? "line" : "lines"}`} action={(
      <Group gap="xs">
        {n > 0 && (
          <Button variant="subtle" color="gray" size="xs" aria-expanded={open} aria-controls="event-activity" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide event history" : "View event history"}
          </Button>
        )}
        {action}
      </Group>
    )}>
      {n === 0 && <Text size="sm" c="dimmed">{pageWords().activity_empty}</Text>}
      {open && (
        <Stack id="event-activity" gap={4}>
          {activity.map((a, i) => (
            <Text key={i} size="sm" style={{ overflowWrap: "anywhere" }}><Text span c="dimmed" size="sm">{a.when}</Text> · {a.line}</Text>
          ))}
        </Stack>
      )}
    </ViewSection>
  );
}
