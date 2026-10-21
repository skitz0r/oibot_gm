import { Card, Text } from "@mantine/core";
import type { MyApplicationState } from "../setupApi";
import { CardHeader } from "./Page";

/** The Me page's own application (design §3.3 cluster D): waiting → "Your application for X is waiting for an officer";
 *  decided → the decision in the bot's words (registry.application_state — /me view says the same). Shown while it is
 *  open, or once decided until they have a character. */
export function MyApplication({ application, registered }: { application?: MyApplicationState | null; registered: boolean }) {
  if (!application || (application.state !== "open" && registered)) return null;
  const colour = { open: "yellow", accepted: "teal", declined: "red", withdrawn: "gray" }[application.state];
  return (
    <Card withBorder style={{ borderColor: `var(--mantine-color-${colour}-6)` }}>
      <CardHeader title="Your application" hint={application.label} />
      <Text p="md" size="sm">{application.text}</Text>
    </Card>
  );
}
