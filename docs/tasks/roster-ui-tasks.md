# Roster UI Adjustments (Round 2) — Tasks & Goals

Status: **SHIPPED** (implemented 2026-11-09, Phases A–E, commits `e8a7432..2fd9ae4`); bespoke
silhouette art (F) also **SHIPPED 2026-11-09** as owner-authored **per-class** art (see Phase F
for the deviation from the original per-validated-character plan).

Outcome notes (what the implementation decided or changed relative to this plan):

- **Owner decisions during implementation:** (1) the gear-doll fallback art is exactly ONE
  generic `assets/silhouettes/generic.svg` (no per-race files) — its SVG header carries the
  measured art↔socket contract; when Phase F shipped it kept that one generic as the
  class-less fallback and added per-class art beside it (see Phase F).
  (2) The nightly sync is now an approved ADDITIVE writer of `data/characters.json` and
  `sync-bnet.yml` commits it; README documents the third writer + only-visible-members caveat.
  (3) The Pages bundle allowlist widened narrowly to `assets/silhouettes/*.svg` (gate widened
  in the same commit, `fb5d40b`).
- `validation.state` gained one stored value: `"pending"`, written ONLY by the auto-add;
  the gate vocabulary is `(absent | validated | pending)` and stays strict against anything else.
- Doll geometry: socket boxes are pinned 52×50 px with the empty-socket label as an in-box
  overlay; the supported layout floor is a **360px viewport (~294px doll box)**, declared in
  CSS as `--doll-supported-min-w` and DERIVED by the harness gate (sub-360px phones are
  documented out of scope for the zero-overlap guarantee).
- Two pre-existing defects were found and fixed alongside: a harness crash when zero rows need
  a human pick (`c0c89b2`), and a Phase C gate-indent regression that had silently deadened the
  `validation.note` ceiling and the §2 reported-id trust rule (fixed + pinned in `eeace6c`).
- Review trail: two fresh reviewer passes (CHANGES REQUESTED → all findings closed); gate
  battery at final head: 231 unit tests / 2252 sync-artifact checks / 522 UI-harness checks /
  199 stats / 259 writes, all green, with mutation-tested non-blindness on the doll gate.

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

## Phase F — Owner-authored SVG silhouettes (SHIPPED 2026-11-09)

**Deviation from the original plan (recorded deliberately):** this list was written as one
bespoke silhouette *per validated character* under `assets/silhouettes/char/`. What shipped is
owner-authored art **per class** — nine files covering **all nine roster classes**
(`assets/silhouettes/{druid,hunter,mage,paladin,priest,rogue,shaman,warlock,warrior}.svg`,
512×512, solid #0a0a0a figure on a warm layered backdrop) — plus the existing `generic.svg`
which **remains the fallback** for class-less rows (auto-added characters), unknown classes
and any hostile class string.

- Mapping: `js/detail.js` `dollClassToken()` folds `(sidecar.class.name) || row.class` and emits
  exactly one of nine allowlisted `is-<slug>` tokens on the figure element, or none. No path is
  ever built from data; the silhouette stays a CSS background layer (no `<img>` — the Phase B
  invariant, still gated in `verify_search_detail.mjs` [4f]/[4f-F]).
- Alignment: the nine canvases are square where `generic.svg` is 3:4, so five classes (druid,
  hunter, priest, rogue, warlock) would otherwise paint their Head socket over bare backdrop
  (raw deviation +8.6 to +22.7 points). The alignment is achieved **by CSS scale + position in
  `css/app.css`** (uniform `background-size` 110–132% with a bottom anchor; measured head lands
  at 6.5–7.0% against the Head-7 band) **over the owner art — the SVG files are intentionally
  unmodified** (owner decision: our drawing stays byte-identical, every adjustment is reversible
  in our stylesheet). A **viewBox bake-in remains a possible future consolidation** if the owner
  ever wants the socket contract in the files themselves.
- The phone-width residual (square art letterboxes at the 294px floor) is documented per class
  in the `css/app.css` rule comments and is not hidden; measured by
  `.pi/scripts/probe_doll_art.py` (chrome-headless-shell, exact-#0a0a0a figure pixels), which
  the harness gate also runs — assertions measure the committed stylesheet, never a table.
- Docs touch-up shipped alongside: `warrior.svg`'s stale `test-4.svg` path comment corrected
  (comment only, zero art changes).
