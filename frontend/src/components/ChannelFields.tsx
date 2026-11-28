import { Accordion, Group, Select, Stack } from "@mantine/core";
import type { ChannelSection } from "../api";
import { FieldHelp } from "./FieldHelp";

/** A select's empty choice: the channel the place falls back to (Mantine needs a value; the draft holds null). */
const FALLBACK = "fallback";

/** The Channels section of New schedule, Edit schedule, New event and Edit event (design 2026-11-27-event-channels):
 *  collapsed under the timing settings (open when one is set), one select per place — Signup sheet · Officers' cards ·
 *  Loot log — listing the server's text channels as the Config page's pickers do. Every word is the server's
 *  (event_view.channel_section): the section's title, each label and (?), and the empty choice naming the channel it
 *  falls back to ("#raid-signups (the guild's)", "(the schedule's)"). A value the list no longer holds (a channel
 *  deleted, or Discord unreachable) stays selectable under `gone`'s words. Pickers only: nothing is typed. */
export function ChannelFields({ section, values, onChange, about, gone }: {
  section: ChannelSection; values: Record<string, string | null>; onChange: (key: string, value: string | null) => void;
  /** the (?)'s name for a screen reader, "What {name} does" (PAGE_WORDS help_about) */
  about: string;
  /** a selected channel the server's list doesn't hold (PAGE_WORDS channel_gone) */
  gone: string;
}) {
  const open = section.fields.some((f) => values[f.key]);
  return (
    <Accordion variant="contained" chevronPosition="left" defaultValue={open ? "channels" : null}>
      <Accordion.Item value="channels">
        <Accordion.Control>{section.title}</Accordion.Control>
        <Accordion.Panel>
          <Stack gap="md">
            {section.fields.map((f) => {
              const v = values[f.key] ?? null;
              const known = section.options.some((o) => o.value === v);
              const data = [{ value: FALLBACK, label: f.empty }, ...(v && !known ? [{ value: v, label: gone }] : []),
                ...section.options.map((o) => ({ value: o.value, label: o.label }))];
              return (
                <Group key={f.key} gap={2} wrap="nowrap" align="flex-end">
                  <Select label={f.label} w={320} maw="100%" searchable allowDeselect={false} comboboxProps={{ withinPortal: true }}
                    value={v ?? FALLBACK} data={data} onChange={(x) => onChange(f.key, x && x !== FALLBACK ? x : null)} />
                  {f.help && <FieldHelp label={about.replace("{name}", f.label)} text={f.help.text} />}
                </Group>
              );
            })}
          </Stack>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
