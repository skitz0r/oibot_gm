import { useEffect, useState } from "react";
import { Anchor, Box, Button, Card, Group, Image, Stack, Text, Title } from "@mantine/core";
import { IconBrandDiscord } from "@tabler/icons-react";
import { api, type PublicInfo } from "../api";

/** A logged-in Discord account that isn't in the guild's server (the API answered 403 not_member): the same message
 *  as the server's not-a-member page, with the Join button when an invite is set. Anyone can join. */
export function NotMember() {
  const [info, setInfo] = useState<PublicInfo | null>(null);
  useEffect(() => { api.get<PublicInfo>("/api/public").then(setInfo).catch(() => setInfo({ name: "the guild", about: null, invite_url: null, recruiting: false, icon: null })); }, []);
  if (!info) return <Box p="xl"><Text c="dimmed">Loading…</Text></Box>;
  return (
    <Box mih="100vh" p="md" style={{ display: "grid", placeItems: "center", background: "var(--mantine-color-slate-9)" }}>
      <Card p="xl" maw={440} w="100%">
        <Stack gap="md">
          <Group gap="sm" wrap="nowrap">
            {info.icon && <Image src={info.icon} w={56} h={56} radius="md" alt="" />}
            <Title order={1} size="h2" style={{ overflowWrap: "anywhere" }}>{info.name}</Title>
          </Group>
          <Text c="dimmed">
            You're not in the {info.name} Discord yet. The site is for members of the server.{" "}
            {info.invite_url ? "Join the server, then come back and log in again." : "Ask a member of the guild for an invite, then come back and log in again."}
          </Text>
          {info.invite_url && (
            <Group><Button component="a" href={info.invite_url} rel="noopener" color="#5865F2" leftSection={<IconBrandDiscord size={18} />}>Join our Discord</Button></Group>
          )}
          <Anchor href="/auth/logout" size="sm" c="dimmed">Log out</Anchor>
        </Stack>
      </Card>
    </Box>
  );
}
