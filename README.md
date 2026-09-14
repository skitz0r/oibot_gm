# oibot_GM

Guild Master assistant for WoW raiding guilds. **Offline prototype**: no Discord
yet. It takes a Raid-Helper signup, builds a roster + party layout with a CP-SAT
solver against a game-version buff matrix, then scores a set of drops and
recommends recipients with justification (Claude, with a deterministic fallback).

Design and research: [docs/design.md](docs/design.md), [docs/research.md](docs/research.md).

## Run

```bash
uv sync
uv run oibot demo --no-llm          # deterministic: roster + loot → out/report.md
uv run oibot roster                 # roster only (Claude narrative if key present)
uv run oibot loot                   # loot only, uses out/roster.json if present
```

Native guild state (characters, events, awards, precedents, policy) is a
git-backed store: a private repo checked out as a sibling `../oibot_gm-data`
(or `OIBOT_DATA_DIR`). Without one, the CLI falls back to `fixtures/demo`.

Put `ANTHROPIC_API_KEY=...` (and `WCL_CLIENT_ID/SECRET`) in `.env` to enable the
Claude paths. Model routing per workload is in `src/oibot_gm/llm/provider.py`
(`OIBOT_MODEL_LOOT_RECOMMEND=claude-sonnet-5` etc. overrides).

## Layout

```
profiles/tbc/            GameProfile: classes, buffs (aura matrix), comp_rules, raids, items, loot_defaults
fixtures/demo/           anonymized shadow-guild data (fixtures/25bg = real, local only): signup (transcribed), characters + name map, WCL attendance,
                         BisCouncil loot export (real), wishlists (MOCK), drops (MOCK), policy.md (MOCK)
src/oibot_gm/
  profiles.py            YAML loader; Buff.benefit(spec) is the value function the solver uses
  importers/             biscouncil.py, wcl.py, signup.py (Raid-Helper + registry mapping)
  roster/solver.py       CP-SAT selection + grouping;   roster/explain.py deterministic advisories
  loot/scoring.py        candidates + base score;       loot/recommend.py Claude / fallback
  llm/provider.py        provider boundary + routing table
  cli.py                 typer CLI
out/                     generated reports
```

## What's real vs mock in the fixtures

Real: WCL roster/specs and attendance (6 T6 reports), BisCouncil received-loot
ledger (106 awards), the Raid-Helper signup (transcribed from a screenshot).
Mock: wishlists, the drop list, per-item spec tiers, the loot policy, ranks,
and the Discord-name → character mapping (each row carries a confidence).
