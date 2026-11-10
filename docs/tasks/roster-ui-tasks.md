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

## Phase G — One image in the doll, one guild in the roster (SHIPPED 2026-11-10)

Two owner adjustments, both of which turned out to be about *which source wins* rather than about
markup, so both are written up with the measurement that decided them.

### G1 — a real render replaces the silhouette (it no longer layers over it)

**What the owner saw:** "both are being used in some cases". **Why:** `media.fullRender` is the
armory's `-main-raw.png`, and the committed asset is **1600×1200 RGBA with a transparent ground**
(measured with a fetch of the committed URL: corner alpha 0). The doll painted the class silhouette
as a CSS background and the render as an `<img>` over it, so the silhouette's head read from behind
the character's head. Not a data bug and not a stale-cache bug — a layering contract that had never
been stated.

- **Rule now:** the doll shows one image. Where a safe `fullRender` exists, `js/detail.js` puts
  `has-render` on the doll box and `css/app.css` stops the silhouette background from painting
  (`.wow-doll.has-render .wow-doll-figure { background-image: none }`). Three classes, so it outranks
  the two-class `is-<slug>` art rules without touching them; `generic.svg` still backs every row with
  no render, exactly as in Phase B/F.
- **Degrades, not regresses:** the render's `onerror` is now the named `dollRenderFailed(this)`
  instead of an inline `display='none'` — because restoring the fallback art needs the doll box, not
  just the image. A URL that validated at sync time and died at read time hides itself *and* puts the
  class silhouette back, so the row never ends up as an empty frame.
- **Eager, not lazy** (review round): the `<img>` dropped `loading="lazy"`. The suppression ships with
  the markup, so a fetch the browser deferred to intersection showed gradient and sockets for the whole
  round trip — in a modal the user just opened, where that image is the whole point.
- **Chosen over the alternatives (owner decision):** `avatarSd`/`avatar` were considered as a
  fallback "real image" and rejected for the doll — they are an **84×84 opaque bust on a red
  backdrop** (measured), which stretched into the 420px doll is a blurry headshot that no longer lines
  up with the Head-7/Feet-92 bands. They stay exactly where they belong: the modal header avatar.
  Consequence stated rather than hidden: `media.fullRender` is on **0 of the 21 committed sidecar
  rows** (that file predates the writer that always sets it), so G1 becomes visible on the next
  nightly that writes it. Until then every doll is one silhouette, which is also correct.
- Gated in `verify_search_detail.mjs` [4f]: one `has-render` iff a render, the CSS rule's existence,
  the handler's two effects (image hidden, flag dropped) and its inertness on a missing element.

### G2 — the roster tracks one guild (input + select), and other guilds leave the list

Before: the guild chips listed every guild the sidecars happened to mention, so one Heghmoh member
(whose profile is what made that guild discoverable) sat in a Flowmasters roster forever.

- **Source of truth:** `data/guild-config.json` — `{ "schemaVersion": 1, "includedGuild":
  "Flowmasters" }`. Committed, hand-maintained, read by the site (`js/data.js` →
  `state.setGuildScopeDefault`) and by the nightly (`bnet.config.included_guild()`). No file, blank
  or unreadable ⇒ **unscoped**, i.e. the Phase C behaviour: a setting nobody configured must not hide
  rows.
- **Visibility, not a fourth predicate:** a row is in the roster only when its bucket is the included
  guild or a no-guild bucket. Every panel reads `getFilteredCharacters()`, so the grid, the party pool,
  the profession matrix and both distributions cannot disagree — the rule lives in one function
  (`getScopedGuildBucket`) and the raw four-step fact (`getGuildBucket`) is untouched under it.
- **The chips are then exactly All / Flowmasters / Guildless**, which is what the owner asked for.
  `Unknown` folds into `Guildless` (owner decision): "never looked up" is not a third guild, and 16
  hand-added rows had to stay reachable under something. The scoped guild is offered even at zero
  rows, so a chip row can never strand the current selection.
- **Input and select:** the filter row gained a text field that datalists every guild the roster
  knows *raw* — hidden ones included, which is how a guild comes back — with **Set** / Enter and a ↺
  that returns to the committed default. A typed choice is this browser's (`localStorage
  wow_forever_guild_scope`, third and last key the app writes); the empty string is an answer
  (unscope), and an absent key means "use the file", so the two states stay distinguishable. Nothing
  in the UI commits — changing the committed scope is a git change.
- **The nightly is scoped too:** `Sync.discover_guilds` filters what it discovered before returning
  it, so an out-of-scope guild's roster is never fetched (requests conserved) and never planned.
  Resolution is deliberately *not* scoped: the guild-lever pass still resolves characters already on
  the roster however they were found. Scope decides who may **arrive**; it never decides who gets
  refreshed, and it never removes a row. The summary line now says `guild scope: Flowmasters
  (auto-add reads only that guild's roster; skipped: Heghmoh)`.
- **One name rule, one table.** "Does this name select the included guild?" is answered twice per
  change — in Python by `bnet.sync.same_guild` (which rosters to fetch and auto-add) and in JS by
  `state.guildNameEquals` (which rows the scope admits, and which chip they land on). Both are pinned
  to `tools/tests/fixtures/guild-name-cases.json` (16 pairs, each with the reason), and so is the
  gate's own copy of the rule, which is what found the first divergence: an exact-compare shortcut
  made `---` equal to itself in Python while JS called it nameless. Trim, case-fold, drop everything
  outside letters/digits, compare — one comparison, so the answer cannot depend on which form of the
  question was asked first. Neither reader folds composed vs decomposed accents (a pair like
  `Rösche` NFC/NFD stays distinct on both sides), and `casefold`/`toLowerCase` differ for `ß`; both
  are written into the table's comment as limits rather than left as surprises.
- Gated in `verify_search_detail.mjs` [1] (scope honoured on the committed data, hidden rows are a
  *named* foreign guild and never a guildless one, the hidden set is non-empty so an inert scope cannot
  pass, chips limited, input/datalist/Set/↺ behaviour, swap-not-union, both spellings of "no guild"
  survive, the datalist names each hidden guild rather than counting options) plus the shared table on
  the JS side, and in `tools/tests/test_sidecar_phase_c.py` (`GuildScopeLimitsTheAutoAdd`: config
  parsing, name↔slug matching, unscoped default, summary line; `GuildNameRuleIsTheSharedTable`: the
  same table, plus symmetry and idempotence — the chip row dedupes on this relation, so it has to be
  one).
- **Known consequence:** the site now hides rows by default. If a character looks deleted, the first
  thing to check is the guild note under the chips ("1 other-guild row(s) hidden"), not the sync.

### G3 — what an independent review found after every gate was green

Five defects, all shipped past a green suite, which is the reason they are written down. Each is now
gated where it was measured, not where it was convenient.

1. **↺ was unreachable.** `index.html` hid it with the boolean `hidden` attribute; `renderGuildScope`
   shows it with `classList.toggle('hidden', …)`. The committed Tailwind preflight enforces
   `[hidden]{display:none}` at author level, so the class change could never win: pressed **Set** once
   and the browser was pinned to the typed value with no control to hand the decision back — including
   after a deploy changed `includedGuild`. Every other hidden element in the file uses the class. The
   harness missed it because its element stubs read `class="…"` and drop attributes; the gate now
   parses the button's **attribute names** (values blanked) and refuses the boolean form, and asserts
   the ↺ is visible exactly while an override exists. Re-measured in headless Chromium against the
   committed stylesheets: `none → inline-block → none`.
2. **A failed re-read unscoped the session.** `loadSidecar` returns `null` for 404 and for 503 alike,
   and `reloadSidecars()` runs after every Battle.net refresh — exactly when a blip is likely — so one
   503 collapsed the scope to `''` and the note said "No guild selected", which reads like a choice.
   Now: the **boot** read is the one that answers "does this deployment have a scope" (at boot the
   value it overwrites is `''` anyway, so failing is free); a failed **refresh** keeps the last value
   the page actually read. A document that is present but blank still really does clear — that is a
   config, not a failure. Gated by injecting a 503 and then a blank-`includedGuild` 200.
3. **Unscoped, the chip row folded case and the predicate did not.** `guildBuckets()` dedupes
   spellings, `matchesGuild()` compared exactly outside a scope, so the losing spelling had **no chip
   that returned it** — reachable by pressing Set with an empty field, i.e. the documented way to
   unscope. One folded comparison in both modes now, and a fixture row spelled `flowmasters` next to
   `Flowmasters` proves one chip returns both.
4. **The two readers disagreed about who is in the guild** (fixed by the table above). The failure mode
   was the worst kind: the nightly admitted members the site then never listed, so the roster grew rows
   nobody could see and no log said so.
5. **Five gates asserted less than their messages.** `scopedOut.every(…)` passed on an empty hidden
   set; "offers the guilds the scope hides" passed when the only extra option was a guild already
   visible; "no sync module writes the config" was a same-line substring test that would have passed
   `json.dump(doc, open(config.GUILD_CONFIG_FILE, "w"))` and a two-line `p = …; p.write_text(...)`;
   "never commits the scope" would have passed `git add -A`; and a "no gear content persisted" gate
   passed only because an unrelated section happened to clear the scope key. Each now tests the thing
   its message claims — naming what must be present, scanning who may *name the path at all*, and
   staging explicit files only.

Two minor items, fixed: the scope field no longer overwrites un-applied typing on an unrelated repaint
(clicking the search box used to revert it — focus alone is not "unsaved", so the field now tracks what
the last paint wrote), and the render is eager (G1). One, accepted: the first paint runs before
`guild-config.json` resolves, so a first-ever visit shows unscoped for a frame and then settles — a
browser that has ever pressed Set reads its value synchronously from `localStorage` and never sees it.

### G4 — the nightly moved the data under the harnesses (same day, after the push)

`3015804 Sync Battle.net character data` landed while Phase G was being reviewed, and it was the
first run to write data the harnesses had never seen: **11 auto-added Heghmoh members with no
`race`/`class`** (correct — discovery gives you a name, resolution gives you the rest), **14 sidecar
rows with a `media.fullRender`** (so G1 became visible immediately rather than after "the next
nightly", which is what this document said earlier), and a re-fetch that left the Warrior the Talents
gates were reading with **one** spec group instead of two.

Two gates were wrong about the app, and one whole section was wrong about what a fixture is:

- **`verify_stats.mjs`** tallied every row's `race`/`class`, so the rows without them appeared as a
  phantom bucket and the bars "should" have summed to 100. `stats.js` is the authority and it does
  two different things on purpose: `tallyBy` skips a falsy key (`stats.js:19`) while
  `renderDistribution` is handed `chars.length` as the denominator (`stats.js:88`) — a row nobody has
  resolved is counted in *N Chars* and absent from the bars. The harness now mirrors that, derives
  the expected bar-width sum from the **covered share** instead of assuming 100, and asserts the
  uncovered rows leak no stringified `undefined` label into the UI. Same reasoning for the printed
  percentages.
- **`verify_search_detail.mjs`** crashed on `c.class.toLowerCase()`. A row with no class cannot match
  a class query and is not a row in the bars; both rules are now written down where the query
  composition is asserted.
- **The Talents section, and the doll's "no render" baseline, were rented from live data.** Eleven
  gates about `detail.js` markup went red because a random player respec'd, and the doll baseline
  broke because the same character was given a `-main-raw.png` under it. Both are now built by the
  harness: a two-spec fixture row assembled out of the node grid of the committed
  `data/talent-trees.json` (the same document every geometry assertion re-derives from, so the
  checks bind to real coordinates and a real selection), and explicit `dollNoRender` /
  `dollWithRender` variants of one row. A UI behaviour is pinned to data the repo controls; live data
  is used for the gates that are *about* live data — the sidecar shape, the guild buckets, the counts.

The rule this leaves behind: **machine-owned files are inputs, never fixtures.** `data/` changes on a
schedule nobody reviews, so a gate that needs a shape builds that shape, and a gate that needs the
committed content asserts the content and fails loudly when the shape it assumed is gone.
