import { useEffect, useMemo, useState } from "react";
import { Anchor, Badge, Box, Card, Code, Group, NavLink, Select, Stack, Table, Text, Title } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { useLocation, useNavigate } from "react-router-dom";
import { api, type Manual, type ManualSection } from "../api";
import { CardHeader, PageTitle, fail } from "../components/Page";

const TIER: Record<string, string> = { officers: "officers", owner: "owner" };

/** Inline markdown the manual uses: `code`, **bold**, [text](url). Text stays text (no HTML is ever injected). */
function inline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("`")) out.push(<Code key={i++}>{t.slice(1, -1)}</Code>);
    else if (t.startsWith("**")) out.push(<b key={i++}>{t.slice(2, -2)}</b>);
    else {
      const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(t)!;
      out.push(mm[2].startsWith("http") ? <Anchor key={i++} href={mm[2]} target="_blank" rel="noreferrer">{mm[1]}</Anchor> : mm[1]);
    }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** The manual's own block shapes: ### headings, paragraphs, bullet lists, fenced code, tables (shown as written). */
function Body({ text }: { text: string }) {
  const blocks = useMemo(() => {
    const out: { kind: "h" | "p" | "ul" | "code" | "table"; lines: string[] }[] = [];
    const lines = text.split("\n");
    let k = 0;
    while (k < lines.length) {
      const l = lines[k];
      if (l.startsWith("```")) {
        const body: string[] = [];
        k++;
        while (k < lines.length && !lines[k].startsWith("```")) body.push(lines[k++]);
        k++;
        out.push({ kind: "code", lines: body });
      } else if (/^#{3,6}\s/.test(l)) { out.push({ kind: "h", lines: [l.replace(/^#+\s/, "")] }); k++; }
      else if (/^\s*[-*]\s/.test(l)) {
        const items: string[] = [];
        while (k < lines.length && (/^\s*[-*]\s/.test(lines[k]) || (/^\s{2,}\S/.test(lines[k]) && items.length))) {
          if (/^\s*[-*]\s/.test(lines[k])) items.push(lines[k].replace(/^\s*[-*]\s/, "")); else items[items.length - 1] += " " + lines[k].trim();
          k++;
        }
        out.push({ kind: "ul", lines: items });
      } else if (l.startsWith("|")) {
        const rows: string[] = [];
        while (k < lines.length && lines[k].startsWith("|")) rows.push(lines[k++]);
        out.push({ kind: "table", lines: rows });
      } else if (!l.trim()) k++;
      else {
        const para: string[] = [];
        while (k < lines.length && lines[k].trim() && !/^(#{3,6}\s|```|\s*[-*]\s|\|)/.test(lines[k])) para.push(lines[k++]);
        out.push({ kind: "p", lines: para });
      }
    }
    return out;
  }, [text]);
  return (
    <Stack gap="xs">
      {blocks.map((b, i) => {
        if (b.kind === "h") return <Title key={i} order={4} size="h6" mt="sm">{inline(b.lines[0])}</Title>;
        if (b.kind === "code") return <Code key={i} block>{b.lines.join("\n")}</Code>;
        if (b.kind === "ul") return <Box key={i} component="ul" m={0} pl="lg">{b.lines.map((t, j) => <li key={j}><Text size="sm" span>{inline(t)}</Text></li>)}</Box>;
        if (b.kind === "table") {
          const rows = b.lines.filter((r) => !/^\|\s*-/.test(r)).map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
          return (
            <Table.ScrollContainer key={i} minWidth={480}>
              <Table withTableBorder verticalSpacing={4}>
                <Table.Thead><Table.Tr>{rows[0].map((c, j) => <Table.Th key={j}><Text size="xs" fw={700}>{inline(c)}</Text></Table.Th>)}</Table.Tr></Table.Thead>
                <Table.Tbody>{rows.slice(1).map((r, j) => <Table.Tr key={j}>{r.map((c, x) => <Table.Td key={x}><Text size="xs">{inline(c)}</Text></Table.Td>)}</Table.Tr>)}</Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          );
        }
        return <Text key={i} size="sm">{inline(b.lines.join(" "))}</Text>;
      })}
    </Stack>
  );
}

/** Help (members and officers, design §3.4 M35): the manual by its own sections — officer and owner sections are
 *  tagged, never hidden (D38: the manual isn't secret, the data is) — and the commands this person may run. The section
 *  is the URL's #anchor (Discord's /help links here with ?g= before the #); phones pick it from a Select. */
export function HelpPage() {
  const [m, setM] = useState<Manual | null>(null);
  const loc = useLocation();
  const nav = useNavigate();
  const phone = useMediaQuery("(max-width: 48em)");
  useEffect(() => { api.get<Manual>("/api/manual").then(setM).catch(fail); }, []);
  if (!m) return <Text c="dimmed">Loading…</Text>;
  const want = decodeURIComponent(loc.hash.replace(/^#/, ""));
  const current: ManualSection | undefined = m.sections.find((s) => s.id === want) || m.sections[0];
  const pick = (id: string) => nav({ hash: id, search: loc.search }, { replace: false });
  const label = (s: ManualSection) => `${s.id}. ${s.title}`;
  return (
    <Stack gap="lg">
      <PageTitle title="Help" intro="How the bot works: the manual it answers from, section by section. Sections for officers and the owner are marked; everyone may read them." />
      <Group align="flex-start" gap="lg" wrap="nowrap">
        {!phone && (
          <Box w={250} style={{ flex: "none", position: "sticky", top: 16 }}>
            {m.sections.map((s) => (
              <NavLink key={s.id} label={<Group gap={6} wrap="nowrap"><Text size="sm" truncate>{label(s)}</Text>{s.tier && <Badge size="xs" variant="outline" color={s.tier === "owner" ? "grape" : "blue"}>{TIER[s.tier]}</Badge>}</Group>}
                active={current?.id === s.id} onClick={() => pick(s.id)} variant="light" color="teal" style={{ borderRadius: 7 }} />
            ))}
          </Box>
        )}
        <Stack gap="md" style={{ flex: 1, minWidth: 0 }}>
          {phone && <Select aria-label="Section" data={m.sections.map((s) => ({ value: s.id, label: label(s) + (s.tier ? ` · ${TIER[s.tier]}` : "") }))} value={current?.id || null} onChange={(v) => v && pick(v)} allowDeselect={false} />}
          {current && (
            <Card id={current.id}>
              <CardHeader title={label(current)} action={current.tier ? <Badge variant="light" color={current.tier === "owner" ? "grape" : "blue"}>for {TIER[current.tier]}</Badge> : undefined} />
              <Box p="md"><Body text={current.body} /></Box>
            </Card>
          )}
          <Card>
            <CardHeader title="Your commands" hint={m.tier === "member" ? "what members can run" : m.tier === "officer" ? "members' and officers' commands" : "every command"} />
            <Table.ScrollContainer minWidth={420}>
              <Table verticalSpacing={4}>
                <Table.Tbody>
                  {m.commands.length === 0 && <Table.Tr><Table.Td><Text size="sm" c="dimmed">The command list is shown when the bot is connected to Discord.</Text></Table.Td></Table.Tr>}
                  {m.commands.map((c) => (
                    <Table.Tr key={c.path}>
                      <Table.Td w={230}><Code>{c.path}</Code></Table.Td>
                      <Table.Td><Text size="sm">{c.description}</Text></Table.Td>
                      <Table.Td w={80}>{c.who !== "member" && <Badge size="xs" variant="outline" color={c.who === "owner" ? "grape" : "blue"}>{c.who}</Badge>}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Card>
        </Stack>
      </Group>
    </Stack>
  );
}
