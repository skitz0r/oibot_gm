import { useEffect, useState } from "react";
import { ActionIcon, Alert, Anchor, Badge, Box, Button, Card, Group, MultiSelect, Paper, Select, Stack, Switch, Text, TextInput, Tooltip } from "@mantine/core";
import { IconArrowDown, IconArrowUp, IconLink, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { api, type Question, type Recruitment as RecruitmentData } from "../api";
import { CardHeader, fail, ok } from "./Page";

const LABEL_MAX = 45, HINT_MAX = 100, NOTE_MAX = 200;

type Draft = { open: boolean; note: string; invite_url: string; member_role_ids: string[]; questions: Question[] };

/** A character counter for the right edge of an input: red once over the limit (Discord's modal caps). */
const counter = (n: number, max: number) => <Text size="xs" c={n > max ? "red" : "dimmed"} style={{ whiteSpace: "nowrap" }}>{n}/{max}</Text>;

/** Config page · Recruitment. Applications happen in Discord (/apply and the registration card); this is what they ask,
 *  whether they are open, the homepage's invite and who registers without applying. Officers read; the owner edits
 *  (one Save → POST /api/admin/recruitment, refusals shown here). */
export function Recruitment({ channels }: { channels: { id: string; name: string; category: string | null }[] }) {
  const [data, setData] = useState<RecruitmentData | null>(null);
  const [d, setD] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inviteChannel, setInviteChannel] = useState<string | null>(null);  // R-06: the invite is created AT Save, never before
  const load = () => api.get<RecruitmentData>("/api/recruitment").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  if (!data) return null;
  const editing = d !== null;
  const roleName = (id: string) => data.guild_roles.find((r) => r.id === id)?.name;
  const known = new Set(data.guild_roles.map((r) => r.id));
  const start = () => { setError(null); setInviteChannel(null); setD({ open: data.open, note: data.note, invite_url: data.invite_url, member_role_ids: [...data.member_role_ids], questions: data.questions.map((q) => ({ ...q, hint: q.hint ?? "" })) }); };
  const setQ = (i: number, patch: Partial<Question>) => setD({ ...d!, questions: d!.questions.map((q, j) => (j === i ? { ...q, ...patch } : q)) });
  const move = (i: number, by: number) => {
    const qs = [...d!.questions]; const j = i + by;
    if (j < 0 || j >= qs.length) return;
    [qs[i], qs[j]] = [qs[j], qs[i]]; setD({ ...d!, questions: qs });
  };

  async function save() {
    if (!d) return;
    setBusy(true); setError(null);
    try {
      const r = await api.post<{ message: string; invite_error: string | null } & RecruitmentData>("/api/admin/recruitment", {
        open: d.open, note: d.note, member_role_ids: d.member_role_ids,
        ...(inviteChannel ? { invite_create: inviteChannel } : { invite_url: d.invite_url }),
        questions: d.questions.map((q) => ({ ...(q.id ? { id: q.id } : {}), label: q.label, hint: q.hint || null, required: q.required })),
      });
      setData(r);
      if (r.invite_error) {  // "Saved. Invite not created: …" — the rest landed; only the channel stays in the draft
        setError(r.message);
        setD({ open: r.open, note: r.note, invite_url: r.invite_url, member_role_ids: [...r.member_role_ids], questions: r.questions.map((q) => ({ ...q, hint: q.hint ?? "" })) });
      } else { ok(r.message); setD(null); setInviteChannel(null); }
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  const channelOptions = channels.map((c) => ({ value: c.id, label: `#${c.name}${c.category ? ` · ${c.category}` : ""}` }));
  const roleOptions = [...data.guild_roles.map((r) => ({ value: r.id, label: r.name })), ...(d?.member_role_ids ?? data.member_role_ids).filter((id) => !known.has(id)).map((id) => ({ value: id, label: `role ${id} (deleted role)` }))];

  return (
    <Card>
      <CardHeader title="Recruitment" hint="applications happen in Discord: /apply and the registration card's Apply button"
        action={data.owner && !editing ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={start}>Edit recruitment</Button> : null} />
      {!editing ? (
        <Stack gap="sm" p="md">
          <Group gap="sm">
            <Badge variant="light" color={data.open ? "teal" : "gray"}>{data.open ? "Applications open" : "Applications closed"}</Badge>
            {data.note && <Text size="sm">{data.note}</Text>}
          </Group>
          <Text size="sm">Invite: {data.invite_url ? <Anchor href={data.invite_url} target="_blank" rel="noopener">{data.invite_url}</Anchor> : <Text span c="dimmed" size="sm">none, so the homepage shows no Join button</Text>}</Text>
          <Box>
            <Text size="sm">Register without applying:</Text>
            <Group gap={6} mt={4}>{data.member_role_ids.length ? data.member_role_ids.map((id) => <Badge key={id} variant="light" color="grape">@{roleName(id) || `role ${id}`}</Badge>) : <Text size="xs" c="dimmed">officers only (no member roles set)</Text>}</Group>
          </Box>
          <Box>
            <Text size="sm">Questions ({data.questions.length}/{data.questions_max})</Text>
            {data.questions.length === 0 && <Text size="xs" c="dimmed">None: applicants give their character, spec and schedules only.</Text>}
            <Stack gap={4} mt={4}>
              {data.questions.map((q, i) => (
                <Box key={q.id || i}>
                  <Text size="sm">{i + 1}. {q.label}{q.required && <Badge ml={8} size="xs" variant="outline" color="orange">required</Badge>}</Text>
                  {q.hint && <Text size="xs" c="dimmed" ml={18}>{q.hint}</Text>}
                </Box>
              ))}
            </Stack>
          </Box>
        </Stack>
      ) : (
        <Stack gap="md" p="md">
          <Switch label="Applications open" description="closed: /apply and the Apply button explain that instead of opening the form" checked={d!.open} onChange={(e) => setD({ ...d!, open: e.currentTarget.checked })} />
          <TextInput label="Recruitment note" description="one line on the Apply screen, e.g. looking for healers" value={d!.note} onChange={(e) => setD({ ...d!, note: e.currentTarget.value })}
            rightSection={counter(d!.note.length, NOTE_MAX)} rightSectionWidth={64} maw={620} />
          <Stack gap={6}>
            <TextInput label="Discord invite" description="the homepage's Join our Discord button; anyone can join, members apply inside" placeholder="https://discord.gg/…" value={d!.invite_url}
              onChange={(e) => setD({ ...d!, invite_url: e.currentTarget.value })} leftSection={<IconLink size={15} />} maw={620} disabled={!!inviteChannel} />
            <Group gap="sm" align="flex-end" wrap="wrap">
              <Select size="xs" placeholder="or create one: pick a channel" data={channelOptions} value={inviteChannel} onChange={setInviteChannel} searchable clearable w={260} aria-label="Invite channel" />
              <Text size="xs" c="dimmed">{inviteChannel ? "Save creates a permanent, unlimited-use invite to that channel and uses it (the bot needs Create Invite there)" : "a permanent, unlimited-use invite, created when you save"}</Text>
            </Group>
          </Stack>
          <MultiSelect label="Member roles" description="people with these roles register without applying (existing members); officers always can"
            data={roleOptions} value={d!.member_role_ids} onChange={(v) => setD({ ...d!, member_role_ids: v })} searchable clearable maw={620} />
          <Box>
            <Text size="sm" fw={500}>Application questions</Text>
            <Text size="xs" c="dimmed" mb="xs">asked in Discord's form after the character: a short label (Discord allows {LABEL_MAX} characters) and an optional hint ({HINT_MAX}); up to {data.questions_max}</Text>
            <Stack gap="xs">
              {d!.questions.map((q, i) => (
                <Paper key={i} withBorder p="xs" radius="md">
                  <Group gap="xs" wrap="wrap" align="flex-start">
                    <Text size="sm" c="dimmed" w={22} pt={6}>{i + 1}.</Text>
                    <TextInput size="xs" placeholder="Question" value={q.label} onChange={(e) => setQ(i, { label: e.currentTarget.value })} rightSection={counter(q.label.length, LABEL_MAX)} rightSectionWidth={52} style={{ flex: "2 1 220px" }} aria-label={`Question ${i + 1}`} />
                    <TextInput size="xs" placeholder="Hint (optional)" value={q.hint ?? ""} onChange={(e) => setQ(i, { hint: e.currentTarget.value })} rightSection={counter((q.hint ?? "").length, HINT_MAX)} rightSectionWidth={60} style={{ flex: "3 1 240px" }} aria-label={`Hint ${i + 1}`} />
                    <Group gap={4} wrap="nowrap" pt={4}>
                      <Switch size="xs" label="required" checked={q.required} onChange={(e) => setQ(i, { required: e.currentTarget.checked })} />
                      <Tooltip label="Move up"><ActionIcon variant="subtle" color="gray" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up"><IconArrowUp size={15} /></ActionIcon></Tooltip>
                      <Tooltip label="Move down"><ActionIcon variant="subtle" color="gray" disabled={i === d!.questions.length - 1} onClick={() => move(i, 1)} aria-label="Move down"><IconArrowDown size={15} /></ActionIcon></Tooltip>
                      <Tooltip label="Remove"><ActionIcon variant="subtle" color="red" onClick={() => setD({ ...d!, questions: d!.questions.filter((_, j) => j !== i) })} aria-label="Remove"><IconTrash size={15} /></ActionIcon></Tooltip>
                    </Group>
                  </Group>
                </Paper>
              ))}
              <Group>
                <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} disabled={d!.questions.length >= data.questions_max}
                  onClick={() => setD({ ...d!, questions: [...d!.questions, { label: "", hint: "", required: false }] })}>Add question</Button>
                {d!.questions.length >= data.questions_max && <Text size="xs" c="dimmed">{data.questions_max} is the most Discord's forms can ask</Text>}
              </Group>
            </Stack>
          </Box>
          {error && <Alert color="red" variant="light">{error}</Alert>}
          <Group gap="sm" justify="flex-end">
            <Button variant="default" size="sm" onClick={() => { setD(null); setError(null); }}>Cancel</Button>
            <Button size="sm" loading={busy} onClick={save}>Save recruitment</Button>
          </Group>
        </Stack>
      )}
    </Card>
  );
}
