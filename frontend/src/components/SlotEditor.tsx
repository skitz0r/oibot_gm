import { useState } from "react";
import { Box, Button, Group, Pill, Select, Text } from "@mantine/core";
import { TimePicker } from "@mantine/dates";
import { IconPlus } from "@tabler/icons-react";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** 'Tue 19:30' (the stored weekly slot) → 'Tue 7:30 PM' (what a person reads). Mirrors Registry.slot_label. */
export function slotLabel(slot: string): string {
  const m = /^(\w+)\s+(\d{1,2}):(\d{2})$/.exec(slot.trim());
  if (!m) return slot;
  const h = Number(m[2]);
  return `${m[1]} ${h % 12 || 12}:${m[3]} ${h < 12 ? "AM" : "PM"}`;
}

/** Sort key: weekday, then time of day. */
function slotKey(slot: string): number {
  const m = /^(\w+)\s+(\d{1,2}):(\d{2})$/.exec(slot.trim());
  if (!m) return 1e9;
  return DAYS.indexOf(m[1].slice(0, 3)) * 1440 + Number(m[2]) * 60 + Number(m[3]);
}

/** Weekly run slots, picked — never typed. The value is the machine form the API stores ('Tue 19:30'); every label is
 *  12-hour ('Tue 7:30 PM'). Chips carry an × to remove; the add row is a weekday and a 12-hour time picker. */
export function SlotEditor({ value, onChange, label = "run slots", description }: { value: string[]; onChange: (v: string[]) => void; label?: string; description?: string }) {
  const [day, setDay] = useState<string | null>("Tue");
  const [time, setTime] = useState<string>("19:30");
  const hm = /^(\d{1,2}):(\d{2})/.exec(time || "");
  const candidate = day && hm ? `${day} ${hm[1].padStart(2, "0")}:${hm[2]}` : null;
  const dup = candidate !== null && value.includes(candidate);
  const add = () => {
    if (!candidate || dup) return;
    onChange([...value, candidate].sort((a, b) => slotKey(a) - slotKey(b)));
  };
  return (
    <Box>
      <Text size="sm" fw={500}>{label}</Text>
      {description && <Text size="xs" c="dimmed" mb={6}>{description}</Text>}
      <Group gap={6} mb="xs" wrap="wrap">
        {value.length === 0 && <Text size="sm" c="dimmed">No slots — no sheets open for this raid.</Text>}
        {value.map((s) => (
          <Pill key={s} size="md" withRemoveButton onRemove={() => onChange(value.filter((x) => x !== s))} removeButtonProps={{ "aria-label": `remove ${slotLabel(s)}` }}>
            {slotLabel(s)}
          </Pill>
        ))}
      </Group>
      <Group gap="xs" align="flex-end" wrap="wrap">
        <Select aria-label="weekday" data={DAYS} value={day} onChange={setDay} allowDeselect={false} w={100} size="sm" />
        <TimePicker aria-label="time" format="12h" withDropdown minutesStep={5} value={time} onChange={setTime} w={140} size="sm" />
        <Button size="sm" variant="default" leftSection={<IconPlus size={15} />} onClick={add} disabled={!candidate || dup}>
          {dup ? "Already added" : "Add"}
        </Button>
      </Group>
    </Box>
  );
}
