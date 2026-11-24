import { Badge, Group, Title } from "@mantine/core";
import { ApiError, type Box } from "../../api";
import css from "../../pages/listpage.module.css";

/** The box a refused or stale write answers with (400 refusal / 409 "changed" — the route's `box`), else undefined. */
export const boxOf = (e: unknown): Box | undefined => (e instanceof ApiError ? (e.body as { box?: Box } | undefined)?.box : undefined);

/** An event's header section (Main.dc.html): its start in the server's words, the state chip (teal while open for
 *  signup, grey otherwise), the actions at the right. */
export function EventHead({ title, chip, open, tag, children }: { title: string; chip: string; open?: boolean; tag?: string | null; children?: React.ReactNode }) {
  return (
    <div className={css.sect}>
      <Group justify="space-between" align="center" wrap="wrap" gap="sm">
        <Group gap="sm" wrap="wrap" align="center">
          <Title order={2} size="h3">{title}</Title>
          <Badge variant="light" color={open ? "teal" : "gray"} tt="none" radius="xl">{chip}</Badge>
          {/* a rehearsal says so (event_json test_chip, the server's word) */}
          {tag && <Badge variant="light" color="grape" tt="none" radius="xl">{tag}</Badge>}
        </Group>
        {children}
      </Group>
    </div>
  );
}
