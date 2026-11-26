import { useState } from "react";
import { ActionIcon, Popover, Text } from "@mantine/core";
import { IconHelpCircle } from "@tabler/icons-react";

/** The (?) beside a setting: the server's sentence on what the setting does (PAGE_WORDS schedule_field_help) — ONE
 *  component for the editors' switches (SettingsFields) and the view pages' Details rows that name the same setting
 *  (Facts, from a Fact's `help`). Opens on hover, on keyboard focus and on a tap (a phone has no hover), closes on
 *  leaving, Escape or a tap elsewhere. A 44 px target; the bubble keeps inside a 400 px screen. `inline` (a Details
 *  label) keeps the 44 px target but takes only the icon's room in the line, so the grid does not move, and draws no
 *  hover disc (it would cover the label's last letters). */
export function FieldHelp({ label, text, inline }: { label: string; text: string; inline?: boolean }) {
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const opened = hover || pinned;
  return (
    <Popover opened={opened} onChange={(o) => { if (!o) { setHover(false); setPinned(false); } }} width={300} position="top" withArrow
      shadow="md" withinPortal closeOnEscape closeOnClickOutside>
      <Popover.Target>
        <ActionIcon variant={inline ? "transparent" : "subtle"} color="gray" size={44} radius="xl" aria-label={label} aria-expanded={opened}
          style={inline ? { margin: "-15px -15px -15px -11px", verticalAlign: "middle" } : undefined}
          onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)} onFocus={() => setHover(true)}
          onBlur={() => { setHover(false); setPinned(false); }} onClick={() => setPinned((p) => !p)}
          onKeyDown={(e) => { if (e.key === "Escape") { setHover(false); setPinned(false); } }}>
          <IconHelpCircle size={inline ? 14 : 18} />
        </ActionIcon>
      </Popover.Target>
      <Popover.Dropdown role="tooltip" maw="calc(100vw - 32px)"><Text size="sm">{text}</Text></Popover.Dropdown>
    </Popover>
  );
}
