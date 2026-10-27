import { useCallback, useState } from "react";
import { Button, Group, Modal, Stack, Text, Textarea } from "@mantine/core";

/** `note`: the dialog also takes an optional note (a cancel reason, a note to an applicant) — prose, never parsed.
 *  With `note` set the promise resolves to `{ note }` (note may be ""), without it to `true`; `false` is always Cancel. */
export type ConfirmOpts = { title?: string; message: React.ReactNode; confirmLabel?: string; cancelLabel?: string; color?: string; note?: { label: string; maxLength: number; placeholder?: string } };
export type ConfirmResult = false | true | { note: string };
type Pending = ConfirmOpts & { resolve: (r: ConfirmResult) => void };

/** A promise-flavoured confirm dialog (Mantine Modal): `const [ask, dialog] = useConfirm(); if (!(await ask({ message }))) return;` — render `dialog` once.
 *  The message is the consequence sentence the SERVER composed (e.g. `sheet.consequences.lock`); a page never writes its own promise of what the bot will do. */
export function useConfirm(): [(o: ConfirmOpts) => Promise<ConfirmResult>, React.ReactNode] {
  const [pending, setPending] = useState<Pending | null>(null);
  const [note, setNote] = useState("");
  const ask = useCallback((o: ConfirmOpts) => new Promise<ConfirmResult>((resolve) => { setNote(""); setPending({ ...o, resolve }); }), []);
  const close = (yes: boolean) => { pending?.resolve(yes ? (pending.note ? { note: note.trim() } : true) : false); setPending(null); };
  const dialog = (
    <Modal opened={pending !== null} onClose={() => close(false)} title={pending?.title || "Are you sure?"} centered>
      <Stack gap="md">
        <Text size="sm" style={{ whiteSpace: "pre-line" }}>{pending?.message}</Text>
        {pending?.note && (
          <Textarea label={pending.note.label} placeholder={pending.note.placeholder} value={note} maxLength={pending.note.maxLength} autosize minRows={2}
            onChange={(e) => setNote(e.currentTarget.value)} description={`${note.length}/${pending.note.maxLength}`} />
        )}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={() => close(false)}>{pending?.cancelLabel || "Cancel"}</Button>
          <Button size="sm" color={pending?.color} onClick={() => close(true)}>{pending?.confirmLabel || "Confirm"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
  return [ask, dialog];
}
