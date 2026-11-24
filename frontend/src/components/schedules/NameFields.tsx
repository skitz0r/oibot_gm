import { Group, TextInput } from "@mantine/core";

/** The schedule editor's Details: its Name (prose, typed) and a NEW schedule's Id (`id` null on an edit: an id never
 *  changes). The Id follows the name until the officer types one; letters, digits and dashes only — the server refuses
 *  one another schedule holds (ID_TAKEN) and the editor shows that sentence under its Save. Never a time (the Clock
 *  rule: times are picked). */
export function NameFields({ name, id, onName, onId }: { name: string; id: string | null; onName: (v: string) => void; onId: (v: string) => void }) {
  return (
    <Group gap="md" align="flex-end" wrap="wrap">
      <TextInput label="Name" w={260} maw="100%" maxLength={40} value={name} placeholder="Tuesday raid night" onChange={(e) => onName(e.currentTarget.value)} />
      {id !== null && (
        <TextInput label="Id" description="The schedule's address on this site" w={200} maw="100%" maxLength={24} value={id}
          onChange={(e) => onId(e.currentTarget.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))} />
      )}
    </Group>
  );
}
