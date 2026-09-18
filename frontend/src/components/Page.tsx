import { Box, Group, Text, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";

/** Success toast. A string with newlines shows one line per line (the server's multi-line replies: what happened, then the ripple). */
export const ok = (message: React.ReactNode) => notifications.show({ message: typeof message === "string" && message.includes("\n") ? <span style={{ whiteSpace: "pre-line" }}>{message}</span> : message, color: "teal" });
export const fail = (e: unknown) => notifications.show({ message: (e as Error).message || "Something went wrong", color: "red" });

export function PageTitle({ title, intro, right }: { title: string; intro?: React.ReactNode; right?: React.ReactNode }) {
  return (
    <Group justify="space-between" align="flex-start" wrap="wrap">
      <Box style={{ minWidth: 0, flex: "1 1 320px" }}><Title order={1} size="h2">{title}</Title>{intro && <Text c="dimmed" size="sm" mt={4}>{intro}</Text>}</Box>
      {right}
    </Group>
  );
}

export function CardHeader({ title, hint, action }: { title: string; hint?: string; action?: React.ReactNode }) {
  return (
    <Group justify="space-between" px="md" py="sm" wrap="wrap" style={{ borderBottom: "1px solid var(--mantine-color-slate-5)" }}>
      <Group gap="sm"><Title order={2} size="h5">{title}</Title>{hint && <Text size="xs" c="dimmed">{hint}</Text>}</Group>
      {action}
    </Group>
  );
}

export function Eyebrow({ children }: { children: React.ReactNode }) {
  return <Text size="xs" fw={700} tt="uppercase" c="dimmed" style={{ letterSpacing: ".08em" }}>{children}</Text>;
}
