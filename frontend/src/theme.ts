import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Design tokens from the redesign brief: slate surfaces, teal accent, class colours as the only loud colours.
const teal: MantineColorsTuple = ["#E3F5F2", "#C4EAE4", "#8FD6CA", "#5EC3B3", "#38B2A0", "#2E9A8A", "#25806F", "#1E6A5D", "#17554B", "#0F3F38"];
const slate: MantineColorsTuple = ["#EAEFF5", "#C2CAD6", "#8C97A8", "#5D687A", "#313B48", "#262E39", "#1E252E", "#181E26", "#141920", "#0F1318"];

export const CLASS_COLOURS: Record<string, string> = {
  Warrior: "#C79C6E", Paladin: "#F58CBA", Hunter: "#ABD473", Rogue: "#FFF569", Priest: "#FFFFFF", Shaman: "#3F7CEE", Mage: "#69CCF0", Warlock: "#9482C9", Druid: "#FF7D0A",
};
export const ROLE_COLOURS: Record<string, string> = { tank: "#6FA8DC", healer: "#7CCB8F", melee: "#E0A448", ranged: "#C9A4FF" };

export const theme = createTheme({
  primaryColor: "teal",
  primaryShade: 4,
  colors: { teal, slate, dark: slate },
  fontFamily: '"Source Sans 3", "Segoe UI", Helvetica, Arial, sans-serif',
  fontFamilyMonospace: '"JetBrains Mono", ui-monospace, Menlo, monospace',
  headings: { fontFamily: '"Manrope", "Helvetica Neue", Arial, sans-serif', fontWeight: "700" },
  defaultRadius: "md",
  fontSizes: { xs: "12px", sm: "13.5px", md: "15px", lg: "17px", xl: "20px" },
  components: {
    Card: { defaultProps: { withBorder: true, radius: "md", padding: 0 } },
    Table: { defaultProps: { verticalSpacing: "sm", horizontalSpacing: "md" } },
  },
});
