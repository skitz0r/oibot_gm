import { useCallback, useState } from "react";
import { Button, Group, Modal, Stack, Text } from "@mantine/core";

export type ConfirmOpts = { title?: string; message: React.ReactNode; confirmLabel?: string; color?: string };
type Pending = ConfirmOpts & { resolve: (yes: boolean) => void };

/** A promise-flavoured confirm dialog (Mantine Modal): `const [ask, dialog] = useConfirm(); if (!(await ask({ message }))) return;` — render `dialog` once. */
export function useConfirm(): [(o: ConfirmOpts) => Promise<boolean>, React.ReactNode] {
  const [pending, setPending] = useState<Pending | null>(null);
  const ask = useCallback((o: ConfirmOpts) => new Promise<boolean>((resolve) => setPending({ ...o, resolve })), []);
  const close = (yes: boolean) => { pending?.resolve(yes); setPending(null); };
  const dialog = (
    <Modal opened={pending !== null} onClose={() => close(false)} title={pending?.title || "Are you sure?"} centered>
      <Stack gap="md">
        <Text size="sm">{pending?.message}</Text>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={() => close(false)}>Cancel</Button>
          <Button size="sm" color={pending?.color} onClick={() => close(true)}>{pending?.confirmLabel || "Confirm"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
  return [ask, dialog];
}
