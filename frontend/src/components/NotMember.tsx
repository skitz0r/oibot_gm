import { useEffect, useState } from "react";
import { Anchor, Box, Button, Card, Group, Image, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { IconBrandDiscord } from "@tabler/icons-react";
import { api, publicGuilds, type PublicInfo } from "../api";

const FALLBACK: PublicInfo = { name: "the guild", about: null, invite_url: null, recruiting: false, icon: null };

function Head({ info }: { info: PublicInfo }) {
  return (
    <Group gap="sm" wrap="nowrap">
      {info.icon && <Image src={info.icon} w={56} h={56} radius="md" alt="" />}
      <Title order={1} size="h2" style={{ overflowWrap: "anywhere" }}>{info.name}</Title>
    </Group>
  );
}

const Join = ({ url }: { url: string }) => (
  <Group><Button component="a" href={url} rel="noopener" color="#5865F2" leftSection={<IconBrandDiscord size={18} />}>Join our Discord</Button></Group>
);

/** A logged-in Discord account that may open none of the guilds (the API answered 403 not_member with no other guild
 *  to switch to): the same message as the server's not-a-member page, every server with its own Join button (when an
 *  invite is set). Anyone can join. */
export function NotMember() {
  const [infos, setInfos] = useState<PublicInfo[] | null>(null);
  useEffect(() => { api.get<PublicInfo | { guilds: PublicInfo[] }>("/api/public").then((p) => setInfos(publicGuilds(p))).catch(() => setInfos([FALLBACK])); }, []);
  if (!infos) return <Box p="xl"><Text c="dimmed">Loading…</Text></Box>;
  const shell = (children: React.ReactNode) => <Box mih="100vh" p="md" style={{ display: "grid", placeItems: "center", background: "var(--mantine-color-slate-9)" }}>{children}</Box>;
  if (infos.length <= 1) {
    const info = infos[0] || FALLBACK;
    return shell(
      <Card p="xl" maw={440} w="100%">
        <Stack gap="md">
          <Head info={info} />
          <Text c="dimmed">
            You're not in the {info.name} Discord yet. The site is for members of the server.{" "}
            {info.invite_url ? "Join the server, then come back and log in again." : "Ask a member of the guild for an invite, then come back and log in again."}
          </Text>
          {info.invite_url && <Join url={info.invite_url} />}
          <Anchor href="/auth/logout" size="sm" c="dimmed">Log out</Anchor>
        </Stack>
      </Card>,
    );
  }
  return shell(
    <Stack gap="md" maw={920} w="100%">
      <Text c="dimmed" ta="center">You're not in any of these Discord servers yet. The site is for members of a server. Join one, then come back and log in again.</Text>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
        {infos.map((info) => (
          <Card key={info.key || info.name} p="xl">
            <Stack gap="md">
              <Head info={info} />
              {info.about && <Text style={{ whiteSpace: "pre-line" }}>{info.about}</Text>}
              {info.invite_url ? <Join url={info.invite_url} /> : <Text c="dimmed" size="sm">Ask a member of the guild for an invite.</Text>}
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      <Anchor href="/auth/logout" size="sm" c="dimmed" ta="center">Log out</Anchor>
    </Stack>,
  );
}
