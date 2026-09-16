import { useEffect, useState } from "react";
import { Badge, Box, Card, Code, Stack, Text } from "@mantine/core";
import { api, type Config as ConfigData } from "../api";
import { CardHeader, PageTitle, fail } from "../components/Page";

export function ConfigPage() {
  const [data, setData] = useState<ConfigData | null>(null);
  useEffect(() => { api.get<ConfigData>("/api/config").then(setData).catch(fail); }, []);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Configuration" intro={<>Read-only here. Change it with <Code>/gm config …</Code>, or in plain text via <Code>/gm change</Code> or an @mention in the ops channel.</>} />
      <Card>
        <CardHeader title="guild.yaml" />
        <Box style={{ overflowX: "auto" }}><Code block p="md" style={{ background: "transparent", fontSize: 12.5 }}>{data.yaml}</Code></Box>
      </Card>
      {Object.entries(data.docs).map(([name, d]) => (
        <Card key={name}>
          <CardHeader title={name} action={d.compiled ? <Badge variant="light" color="teal">compiled</Badge> : name !== "persona" ? <Badge variant="light" color="yellow">not compiled</Badge> : null} />
          <Box style={{ overflowX: "auto" }}><Code block p="md" style={{ background: "transparent", fontSize: 12.5, whiteSpace: "pre-wrap" }}>{d.text || "(empty)"}</Code></Box>
          {d.summary && <Text size="xs" c="dimmed" px="md" pb="md">Compiled reading: {d.summary}</Text>}
        </Card>
      ))}
    </Stack>
  );
}
