# Roster UI Adjustments (Round 2) — Tasks & Goals

Status: **PLANNED** (agreed 2026-11-09). Four site adjustments (A–D) + verification (E);
bespoke silhouette art (F) is a queued, separate task list that does not block A–E.

Verified facts this plan rests on (checked 2026-11-09, do not re-litigate):

- Modal height is content-driven (`index.html` `#detail-modal` box: `max-h-[90vh]` flex
  column); width is already fixed at `max-w-2xl`. Tab panels scroll internally already.
- Battle.net exposes **no account/Battletag identity** on any probed endpoint
  (`docs/bnet-api-notes.md`); the roster's hand-maintained `player` field is the only
  account grouping key. It already groups alts (pandamancer ×5, ninjapirate ×3, …).
- Battle.net `character-media` yields only **84×84 avatars**; but the armory page loads a
  **full-body render** at `render.worldofwarcraft.com/forever-us/character/<g>/<id>-main-raw.png`
  (verified 1600×1200 RGBA, ~660 KB) — derivable from the stored avatar URL by swapping
  the `-avatar.jpg` suffix for `-main-raw.png`.
- The guild roster endpoint (`/data/wow/guild/{slug}/roster`, Tier-2 lever in
  `resolve.py`) is already fetched nightly; the sync **discovers** guild slugs from
  resolved members' profiles but never **adds** members to `characters.json`.
  Flowmasters guild reports `member_count = 4`; only opted-in characters are ever visible.
- Current data: 21 roster rows; 6 sidecar `ok` (4 `validation.state: validated`);
  guild strings exist only on sidecar rows (Flowmasters ×4, Heghmoh ×1, null ×1).

## Phase A — Fixed-size character modal

- The modal box gets a **fixed height** (`h-[90vh]` alongside the existing `max-h-[90vh]`,
  or equivalent): it opens at full size and **never resizes when cycling tabs**. Short
  tabs leave empty space; long tabs scroll inside `#detail-content` (mechanism exists).
- Width unchanged (`max-w-2xl`). Tab bar and review footer stay pinned.
- No per-tab sizing logic; the box shape must be data-independent (same rule as the
  modal-rework "bar shape never depends on data richness" convention).

## Phase B — Gear tab: paper doll around the character render

- **Sync side** (`tools/bnet/`): for each resolved character, derive the full-render URL
  from `media.avatar` and **HEAD-validate** it; record the outcome in the sidecar as
  `media.fullRender` (URL or null). A failed/absent validation is null, never a guess.
  Extend `verify_sync_artifacts.py` schema gating in the same commit.
- **UI side** (`detail.js` gear panel): replace the current two-grid paper doll with a
  portrait-centered layout — the validated render (or fallback, below) centered, gear
  slots absolutely/regionally positioned around the figure by Blizzard slot id
  (helm→head, boots→feet, shoulders over the shoulders, ring/trinket→hand area, chest/
  legs mid-body, etc.). Main hand / off hand / ranged / ammo flank the lower sides.
  Unmatched items stay an "Unslotted" pill row below.
- Image rules: hotlink only hosts on the existing `ICON_HOSTS` allowlist (render.
  worldofwarcraft.com is already on it); `loading="lazy"`; on error or absent
  `fullRender`, fall back to a **generic race silhouette** (`assets/silhouettes/<race>.svg`
  interim art — one simple humanoid per race, replaced later for validated characters
  by Phase F bespoke art). Render is the default; no per-character toggle in this phase.
- Slot hit areas keep tooltips + ilvl behavior; layout must not shift when the image is
  absent (fixed doll container size inside the now-fixed modal).

## Phase C — Guild filter + complete guild coverage

- **UI**: a guild filter (chips or dropdown, styled like the track filter) that combines
  with the existing track filter and search. Buckets: every distinct known guild name,
  **Guildless** (sidecar present, `guild` null/empty), and **Unknown** (no sidecar data).
  Guild for each row is joined at render time from the sidecar `guild` string, or from
  the `guild` field carried by auto-added rows (below).
- **Sync auto-add**: for every discovered guild roster, any member whose normalized
  (name, surname) is absent from `characters.json` is committed as a new row:
  fresh tracker-style `id`, `name`, `player` = the member's name (placeholder alias),
  `track: "alts"`, `guild: <guild name>`, empty role/professions, `validation.state:
  "pending"`. Additive only — never mutate or remove existing rows. Curation afterwards
  is manual via the site's edit UI.
- Schema: `characters.json` rows gain the optional `guild` field (schemaVersion stays 2;
  additive-optional per existing convention). Document in README's data-flow section.
- Caveat recorded, not hidden: only Battle.net-visible (opted-in) members can ever be
  auto-added; "all characters" means all *visible* ones.

## Phase D — Account grouping chip (from `player`)

- Compute the set of `player` values owning ≥2 roster rows. For those rows only, show an
  **account chip** on the roster card and a matching field/callout in the modal header.
  Single-character players show nothing (no empty chip, no layout shift).
- No new roster field, no click-to-filter, grid order unchanged. Chip is display-only
  identification (case-insensitive `player` matching, trimmed).

## Phase E — Verification & review (gates for A–D)

- `verify_search_detail.mjs`: extend for fixed-modal invariants (box size constant across
  all six tabs), gear-doll slot positioning structure, guild filter combination logic,
  account-chip presence/absence rules.
- `verify_sync_artifacts.py`: schema-gate `media.fullRender` and auto-added roster rows
  (additive-only invariant, guild field) in the same commit as the sync change.
- `tools/tests/`: unit tests for full-render URL derivation + HEAD validation, guild
  auto-add (match/miss/preserve-existing), account-group computation.
- Regenerate `css/tailwind.css` with the deploy's exact command; reviewer pass
  (`subagent: reviewer`) on the full diff before push.

## Phase F — Bespoke SVG silhouettes per validated character (QUEUED, separate list)

- One bespoke stylized silhouette per character with `validation.state: "validated"`
  (currently 4: see `data/characters.json`), committed under `assets/silhouettes/char/`.
- Used as the gear-doll fallback art (replaces the generic race silhouette for those
  characters when the Battle.net render is absent/fails).
- Does not block A–E: Phase B ships with generic race silhouettes; F swaps in bespoke
  art per character as it lands.
