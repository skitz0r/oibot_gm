import { useEffect, useMemo, useState } from "react";
import { Badge, Box, Button, Card, Group, Stack, Switch, Text, Title } from "@mantine/core";
import css from "./me.module.css";
import { api, type Me, type Meta, type MyRegular } from "../api";
import { GameIcon } from "../components/Icons";
import { classColour } from "../theme";
import { CardHeader, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { MyLootCard, WishlistCard } from "../components/loot/MyLoot";
import { CharacterTable } from "../components/CharacterTable";
import { AbsencesCard } from "../components/AbsencesCard";


export function MePage({ meta }: { meta: Meta }) {
  const [me, setMe] = useState<Me | null>(null);
  const [editing, setEditing] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<Me>("/api/me").then(setMe).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(() => { if (!editing) load(); });
  useLive(() => { if (!editing) load(); }, ["run:", "member"]);

  async function stepDown(r: MyRegular) {
    if (!(await ask({ title: `Step down from ${r.label}?`, message: `You stop being one of its regulars (${r.character}): the bot no longer expects your answer or reminds you. You can still Join any of its sheets. To come back, ask an officer.`, confirmLabel: "Step down", color: "red" }))) return;
    try { const x = await api.post<{ message: string }>("/api/me/regular/remove", { series: r.series }); ok(x.message); load(); } catch (e) { fail(e); }
  }
  async function setDm(on: boolean) { try { await api.post("/api/me/dm", { on }); ok(on ? "DMs on" : "DMs off"); load(); } catch (e) { fail(e); } }
  const summary = useMemo(() => {
    if (!me) return "";
    const n = me.characters.length;
    const sheet = me.sheets.find((s) => s.rostered) || me.sheets.find((s) => s.status === "in");
    return [`${n} character${n === 1 ? "" : "s"}`, me.roles.primary || null, sheet ? `${sheet.raid} · ${sheet.when} — ${sheet.rostered ? "you're rostered" : "joined"}` : null].filter(Boolean).join(" · ");
  }, [me]);

  if (!me) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start">
        <Box><Title order={1} size="h2">{me.display_name}</Title><Text c="dimmed" size="sm" mt={4}>{summary}</Text></Box>
      </Group>
      {me.must_apply && (
        <Card withBorder>
          <Text fw={700}>New here? Apply in Discord first.</Text>
          <Text size="sm" c="dimmed" mt={4}>Press <b>Apply to join</b> on the registration card, or use <code>/apply</code>. Once an officer accepts you, your character shows up here.</Text>
        </Card>
      )}

      {me.asks.map((a) => (
        <Card key={a.roster} style={{ borderColor: "var(--mantine-color-teal-4)" }}>
          <Group p="md" justify="space-between">
            <Box style={{ minWidth: 0, flex: "1 1 320px" }}>
              <Text>You're rostered for <b>{a.raid}{a.when ? ` · ${a.when}` : ""}</b> as <b>{a.character}</b> — confirm to keep the seat, or hand it back.</Text>
              {(a.channel === "web" || !me.dm) && <Text size="xs" c="dimmed" mt={4}>DMs are off, so this is the only place to answer.</Text>}
            </Box>
            <Group gap="xs">
              <Button size="xs" onClick={() => api.post<{ message: string }>("/api/me/placement", { roster: a.roster, answer: "yes" }).then((r) => { ok(r.message); load(); }).catch(fail)}>Confirm</Button>
              <Button size="xs" variant="default" onClick={() => api.post<{ message: string }>("/api/me/placement", { roster: a.roster, answer: "no" }).then((r) => { ok(r.message); load(); }).catch(fail)}>Can't make it</Button>
            </Group>
          </Group>
        </Card>
      ))}

      <div className={css.layout}>
        <Stack gap="lg" style={{ minWidth: 0 }}>
          <CharacterTable meta={meta} me={me} onChanged={load} onEditing={setEditing} />
          {!me.must_apply && me.characters.length > 0 && <WishlistCard meta={meta} />}
        </Stack>

        <Stack gap="lg" style={{ minWidth: 0 }}>
          {(me.regulars || []).length > 0 && (
            <Card>
              <CardHeader title="My regular raids" hint="you're expected on every sheet of these" />
              <Stack p="md" gap="xs">
                {me.regulars!.map((r) => (
                  <Group key={r.series} justify="space-between" wrap="nowrap">
                    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                      <GameIcon meta={meta} kind="spec" id={`${r.cls}:${r.spec}`} size={20} title={`${r.spec} · ${r.role}`} />
                      <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{r.label}</Text><Text size="xs" c={classColour(meta, r.cls)}>{r.character}</Text></Box>
                    </Group>
                    <Button size="xs" variant="subtle" color="red" onClick={() => stepDown(r)}>Step down</Button>
                  </Group>
                ))}
                <Text size="xs" c="dimmed">Nothing is answered for you: press Join or No thanks on each sheet (an absence marks you away). Want to become a regular somewhere? Ask an officer.</Text>
              </Stack>
            </Card>
          )}
          <Card>
            <CardHeader title="My sheets" />
            <Stack p="md" gap="xs">
              {me.sheets.length === 0 && <Text c="dimmed" size="sm">No live raids.</Text>}
              {me.sheets.map((s) => (
                <Group key={s.key} justify="space-between" wrap="nowrap">
                  <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{s.raid}{s.kind === "adhoc" ? " · ad hoc" : ""}</Text><Text size="xs" c="dimmed">{s.when}{s.character ? ` · ${s.character}` : ""}{s.regular && !s.status ? " · you're a regular: answer it" : ""}</Text></Box>
                  <Badge variant="light" color={s.rostered ? "teal" : s.status === "in" ? "teal" : s.status === "out" ? "red" : s.status === "sub" ? "yellow" : "gray"}>{s.rostered ? `rostered${s.roster && s.roster > 1 ? ` · roster ${s.roster}` : ""}` : s.label || "not answered"}</Badge>
                </Group>
              ))}
              {me.sheets.length > 0 && <Text size="xs" c="dimmed">Answer on the sheet in Discord — Join / Bench / No thanks. Once the roster locks you confirm {me.dm ? "by DM (or here)" : "here — your DMs are off"}.</Text>}
            </Stack>
          </Card>
          {!me.must_apply && <MyLootCard meta={meta} />}
          <AbsencesCard meta={meta} absences={me.absences} onChanged={load} />
          <Card>
            <CardHeader title="Direct messages" action={<Switch checked={me.dm} onChange={(e) => setDm(e.currentTarget.checked)} />} />
            <Text p="md" size="sm" c="dimmed">Sheets, nudges and fill requests arrive as DMs from the bot. Off, and officers reach you in the channel instead.</Text>
          </Card>
        </Stack>
      </div>

      {confirmDialog}
    </Stack>
  );

}
