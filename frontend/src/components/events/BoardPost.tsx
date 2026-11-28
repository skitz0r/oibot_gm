import { useState } from "react";
import { Group, Select, Button } from "@mantine/core";
import { api, type Box, type EventView } from "../../api";
import { useConfirmBox } from "../ConfirmBox";
import { fail, ok } from "../Page";
import { boxOf } from "./shared";

type Channel = NonNullable<Box["channel"]>;

/** The box's one select, "Post to" (the server's label and channels), opening on where the post goes now. Kept as the
 *  box's own field (as LockBox keeps its choice); the pick is read at the press. */
function ChannelPick({ channel, onPick }: { channel: Channel; onPick: (value: string | null) => void }) {
  const [value, setValue] = useState<string | null>(channel.value);
  return (
    <Select label={channel.label} data={channel.options} value={value} searchable allowDeselect={false}
      comboboxProps={{ withinPortal: true }} onChange={(v) => { setValue(v); onPick(v); }} />
  );
}

/** Post board (design 2026-11-27-post-board): the SERVER's box — its title (Post board, or Update board post while a
 *  post is up), the one select and Back · Post, no sentences (the owner's exception to the Confirm rule). The press sends
 *  the box's fp as `seen` and the picked channel: a box that changed comes back fresh (409) and waits again; a refusal
 *  is its one sentence with Back alone. Nothing here composes a word. */
export function useBoardPost(ev: EventView, onChanged: () => void): [() => Promise<void>, React.ReactNode] {
  const [ask, dialog] = useConfirmBox(["run:"]);
  const url = `/api/event/${encodeURIComponent(ev.key)}/board`;
  async function open() {
    const preview = async (): Promise<Box> => {
      try { return (await api.post<{ box: Box }>(url, { preview: true })).box; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
    };
    let first: Box;
    try { first = await preview(); } catch (e) { fail(e); return; }
    const pick = { value: first.channel?.value ?? null };
    const said = { text: "" };
    const done = await ask({
      box: first, refresh: preview,
      fields: first.channel ? <ChannelPick channel={first.channel} onPick={(v) => { pick.value = v; }} /> : undefined,
      act: async (fp) => {
        try { said.text = (await api.post<{ message: string }>(url, { seen: fp, channel: pick.value })).message; return null; }
        catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      },
    });
    if (done) { if (said.text) ok(said.text); onChanged(); }
  }
  return [open, dialog];
}

/** The button under the board (open or locked events): the server's word, Post board or Update board post. */
export function BoardPostButton({ ev, onChanged }: { ev: EventView; onChanged: () => void }) {
  const [open, dialog] = useBoardPost(ev, onChanged);
  if (!ev.board_post?.button) return null;
  return (
    <>
      <Group justify="flex-end" mt="xs" mb="md">
        <Button variant="default" onClick={open}>{ev.board_post.button}</Button>
      </Group>
      {dialog}
    </>
  );
}
