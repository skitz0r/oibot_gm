# Guide library: the guild's own leveling guides, issued per member — design

Date: 2026-11-09. Status: approved in chat (owner). Design home when built: `docs/design.md` §5.63.

## 1. What the owner asked for

> We'll be using our own tools to decrypt, edit, and then republish our internally built custom guides.
> 1. Author uploads guide with date (version) — encrypted with their battletag, which they supply — alongside
>    version and faction (A/H/Both).
> 2. Other users can see the latest version of each and get an encrypted version for themselves, to use the existing
>    guide addon with our custom route. The user supplies their battletag in a Discord request with the desired
>    version, and the bot DMs them a version encrypted for them.

What exists: `guides/rxp_decode.py` (decode / encode an import string keyed by a BattleTag), `guides/reissue.py`
(one member's string locked to their other account, nothing stored), Me › Leveling guide.

Decisions taken in chat (2026-11-09):

- **D1 Who uploads.** Any member of the guild.
- **D2 Versions.** A version is the upload's date and time plus its faction. No guide names, no typed version label.
  The uploader is metadata: recorded and shown, never part of the version.
- **D3 Shape.** One line per faction: Alliance, Horde, Both. "Latest" = the newest version of a line that is not
  withdrawn. A member picks a line, then latest or an older date.
- **D4 Discord.** Slash commands with wizards; the BattleTag is typed only in a private pop-up (modal).
- **D5 Site.** Me › Leveling guide lists the library, downloads for your tag, and uploads.
- **D6 Guard.** Every upload is kept. Officers can withdraw a version (and restore it).
- **D7 No BattleTag is stored.** The author's tag unlocks the upload once; the bot stores the unlocked guide texts and
  locks them to each requester's tag on request. Neither tag is written, logged, or put in an ops line. (The owner's
  ask said "which we store"; the design shown and approved stores the texts instead.)

## 2. The words

**Guild guide** — a leveling guide the guild made itself, in the library. **Version** — one upload, named by its
faction and when it was uploaded ("Horde · Mon 9 Nov 3:00 PM"). **Latest** — the newest version not withdrawn.
**Withdraw / Restore** — the officers' verbs. Never "encrypted", "decrypt", "plaintext", "key" where a person reads:
a guide is *locked to* a BattleTag and *unlocked* by it (the re-issue's words).

## 3. The model

`src/oibot_gm/guides/library.py`, native guild state in the data repo through `store.py`:

```
<guild>/guides/index.yaml                      versions: [GuideVersion]
<guild>/guides/<version id>/guide-001.txt …    the unlocked guide texts, one file per guide, in order
```

`GuideVersion` (Pydantic): `id` (`<faction>-<YYYYMMDD>-<HHMMSS>` UTC, e.g. `horde-20261109-230012`; a clash adds
`-2`), `faction` (`alliance | horde | both`), `uploaded_at` (ISO, UTC), `by_id`, `by_name` (display name at upload),
`guides` (count), `sections` (the string's section count, for the re-encode), `size` (characters of text),
`withdrawn_at`, `withdrawn_by`, `withdrawn_by_id` (None while live).

Plain text files so git deltas one version against the next. One commit per upload / withdraw / restore.

Functions (all sync, callers run them in `asyncio.to_thread`):

- `FACTIONS`, `FACTION_WORDS` (`Alliance`, `Horde`, `Both factions`).
- `versions(reg, faction=None, live_only=False)` newest first; `latest(reg, faction)`; `get(reg, vid)`.
- `upload(reg, text, battletag, faction, by_id, by_name) -> GuideVersion`: validates like `reissue` (EMPTY_TEXT,
  TOO_BIG at `TEXT_MAX`, `validate_battletag`, `decode` under the shared `SLOTS` semaphore → BUSY), refuses an unknown
  faction, refuses past `UPLOADS_PER_DAY = 5` per member per guild (counted from the index, the guild's day), writes
  the texts and the index in one `store.batch` commit `"<key>: guild guide <id> uploaded by <name>"`. Under a
  per-guild lock.
- `issue(reg, vid, battletag) -> str`: reads the version's texts, `encode(texts, tag, sections=v.sections)` under
  `SLOTS`. Refuses a withdrawn version for a member (`WITHDRAWN`); nothing written.
- `withdraw(reg, vid, by, by_id)` / `restore(reg, vid, by, by_id)`: officers; refusal sentences when already so.
  Withdraw `notify`s the uploader (kind `guide`: "Your Horde guide of <when> was withdrawn by <officer>.") — a
  notice, never a DM.
- `issue_filename(v, battletag)`: `GuildGuide-<Faction>-<YYYYMMDD>-<Name>.txt`.
- Sentences (Confirm rule; rendered by `tests/test_confirm_readability.py`):
  - `upload_consequence(reg, faction)`: "This becomes the latest Horde guide for <guild>. Nobody gets a DM. An officer
    can withdraw it."
  - `withdraw_consequence(reg, v)`: "The Horde guide of <when> is withdrawn. Members get the one from <when> as the
    latest." (or "No Horde guide is left for members.") "You can restore it."
  - `restore_consequence(reg, v)`: "The Horde guide of <when> is back." + "It becomes the latest again." when it is.
  - `rows(reg, officer)`: what every surface lists — id, faction words, `when` (`Registry.local12`), uploader, guides
    count, `latest` flag, `withdrawn` (+ by whom for officers). Members never see withdrawn versions.

Gate: the library is on wherever the guide tool is (`/api/meta.guides`), otherwise every surface answers `NO_TOOL`.

Member text is data: `by_name` is escaped on Discord and never reaches a prompt outside a fenced block (`unfence`).

## 4. Discord

A new group `/guide` (members; `discord_guides.py` registers, `flows_guides.py` holds the wizards):

- `/guide list` — ephemeral: each faction's latest and up to four older versions, with uploader, plus the site link.
- `/guide get` — wizard: faction select (lines with a live version) → version select (latest first, newest 25) →
  **Enter your BattleTag…** modal (prose, validated by `validate_battletag`) → the bot DMs the .txt:
  "Your guild guide (Horde · <when>), locked to **Name#1234**: import the attached string with RestedXP." The member
  asked, so this DM is an act under the DM rule. A refused DM → the file is attached to the ephemeral reply instead.
  No decide screen: nothing is written.
- `/guide upload file:<attachment> faction:<Choice>` — the attachment and the Choice are widgets (Wizard rule) →
  BattleTag modal → the decide screen with `upload_consequence` → write → "Uploaded: Horde · <when>." The file is read
  only after the routing / membership check, at most `TEXT_MAX` bytes, `.txt` only.
- `/guide withdraw` (officers, rights checked live) — version select (live versions) → decide → `withdraw`.
  `/guide restore` likewise over withdrawn versions.

Every wizard response is ephemeral; the tag and the string never appear in a channel, an ops line or a log. Ops lines
through `ops.audited`: "guild guide uploaded: Horde · <when>" / "withdrawn" / "restored". No ops line for a get.

## 5. Site

`web/api_guides.py` (same install function):

- `GET /api/guides/library` — `{available, rows, factions, can_withdraw, upload_sentences: {faction: sentence}}`.
- `POST /api/guides/library/upload` `{text, battletag, faction}` — MCP refused (the string stays out of transcripts).
- `POST /api/guides/library/{id}/download` `{battletag}` — the string as a no-store attachment; MCP refused.
- `POST /api/guides/library/{id}/withdraw|restore` `{preview}` — officers; `preview` returns the sentences
  (a stale membership refuses like every officer write). MCP may call these with the actor (D8).

`frontend/src/components/GuideLibrary.tsx` on Me › Leveling guide, above the existing cards: one block per faction;
each version a row (when, uploader, a Latest badge), **Get it for my BattleTag** (one tag field kept for the page
visit only, never in localStorage), officers' ⋮ Withdraw… / Restore… via `useConfirm` with the server's sentences;
**Upload a guild guide** card (paste box, BattleTag, faction select, the server's sentence above the button). Cards
below `sm`, 44 px targets, no words beside icons, empty state: "No guild guide yet. Upload one below." Registers with
`useDirty` while the upload box has text.

MCP: `list_guild_guides` (read), `withdraw_guild_guide` / `restore_guild_guide` (actor). No upload, no issue tool.

## 6. Plain text and help

No plain-text op: a BattleTag and a string must never reach a model (`configops.BY_DESIGN_ONLY` entries
`guide_upload`, `guide_get` with their paths; withdraw / restore are officer verbs on Discord, the site and MCP —
`BY_DESIGN_EXEMPT` from plain text for the same reason of keeping one small surface). The help snapshot gains a
`guild guides` section (every member): each faction's latest version and uploader, and "Get it with /guide get or on
the Me page." The manual gets §"Guild guides" and the glossary words.

## 7. Out of scope

Guide names, typed version labels, storing any BattleTag, automatic checks that an upload is the guild's own work
(the upload sentence and the officers' withdraw are the guard), announcing a new version in a channel, a diff
between versions, building the explorer from a library version.

## 8. Tests

`tests/test_guide_library.py`: upload → issue round trip (decode the issued string with the requester's tag equals
the texts); wrong tag, empty, too big, bad faction, daily cap; latest skips withdrawn; withdraw / restore refusals
and the uploader's notice; the index and commit carry no BattleTag (grep the scratch repo); id clash; rows for a
member hide withdrawn. Routes: MCP refusals, officer gates, no-store headers. Wizards through
`tests/wizard_harness.py` (lint every screen; DM refusal fallback). Confirm sentences in the readability harness;
copy lint; parity matrix; `scripts/check_commands.py`.
