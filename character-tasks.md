# Character Modal Rework — Tasks & Goals

Status: **Phase 0 COMPLETE** (2026-11-08, user checkpoint passed). Phase 1–3 in execution. Execute phases in order.

**Phase 0 outcome amendments (user-approved 2026-11-08; supersede conflicting text below — see `docs/bnet-api-notes.md` §9 for evidence):**
1. Item stat numbers **AVAILABLE** inside `/equipment` (`equipped_items[].stats[]`); zero extra requests. Tooltips include stats.
2. **Gems/sockets do not exist** in this API → dropped from T1.1/T2.3.
3. `character-media` has no inset/headshot → T1.5 targets `avatar(-sd)`.
4. T2.11 is a **no-op**: all 633 probed URLs pass existing `isSafeIconUrl()`.
5. **Visual talent tree IN SCOPE** (was M1): live API exposes full tree geometry — decision #3 updated.

## Goal

Rework the character detail modal (`js/detail.js`, opened by clicking a roster card via `window.openCharacterDetail(id)`) into a tabbed interface with Blizzard-quality gear hover tooltips, using Blizzard/Battle.net graphics and icons wherever possible.

## Agreed Decisions (do not re-litigate without the user)

1. **Gear hover:** Rich Blizzard-sourced tooltip. Before building, run an API probe to check whether authoritative item **stat numbers** exist anywhere in the Blizzard API (`forever-us` item/bonus-list endpoints, and classic-era namespaces). If found → include stats. If not → tooltip = quality color, ilvl + display string, slot, item class/subclass, enchant display strings, gems/sockets, Blizzard icon. **No third-party item DB.**
2. **Skills tab: DROPPED.** No public endpoint for spellbook/skill lists. Five content tabs + Progress.
3. **Talent tab: visual tree (AMENDED 2026-11-08).** Originally list-only with M1 follow-up; Phase 0 proved the live API natively serves full spec talent-tree geometry (`/data/wow/talent-tree/{id}/playable-specialization/{spec}`), so the visual tree is in scope. Selection data (`talent_rank`, spell icons via `spell_tooltip.spell.id` @ static ns) lights up nodes.
4. **Layout:** Compact field grid stays in the modal header (under title). Tab bar: **Gear · Talents · Professions · Reputation · Stats · Progress**. Sync/review notes, ambiguous-candidates picker, and review action buttons stay pinned as a fixed footer — the review workflow is never hidden behind a tab.
5. **Missing data:** All tabs always visible; tabs without data show a clean empty-state message ("No reputation data synced yet"-style). Never hide or disable tabs.
6. **Landing:** Commit directly to `main`. Run the extended sync **locally** using `.env` (repo root, contains `BLIZZARD_CLIENT_ID` / `BLIZZARD_CLIENT_SECRET`) to populate new data for development and verification. Nightly GitHub Actions sync keeps refreshing afterwards.

## Verified Facts (baseline, 2026-11-08)

- Static site: vanilla ES modules + Tailwind v3.4.19 (compiled `css/tailwind.css` committed; regenerate: `npx --yes tailwindcss@3.4.19 -c tailwind.config.js -o css/tailwind.css --minify`). Dev preview: `python3 -m http.server 8000`.
- Modal today: `js/detail.js` (~1045 lines), single scrollable layout rendered from one HTML string (`detailHtml()`, ~L740–816). **No tab component exists anywhere in the codebase** — build one fresh.
- Modal open/close: `hidden` class toggle on `#detail-modal` (`index.html` ~L238–244); `openCharacterDetail(id)` / `closeDetail()` on `window`; state in `js/state.js` with `registerRenderHook(fn)`.
- Sidecar `data/character-data.json` (21 characters): gear entries carry only `slot`, `name`, `ilvl`, `iconUrl`. Item id, quality, class/subclass, enchants exist in the API response but are **discarded** by `tools/bnet/fetch.py` (~L273–295).
- `/statistics` (combat stats) is **already GET by the sync but thrown away** as a schema tripwire (`fetch.py` ~L300; contract documented in `docs/bnet-api-notes.md` §3.5 — full classic stat list incl. rating triples).
- Talents confirmed available: `/specializations` → `specialization_groups[].specializations[].talents[].{talent_rank, talent, spell_tooltip}` (`docs/bnet-api-notes.md` §3.4). **No tree structure/prerequisites in any API.**
- `reputations.href` and `professions.href` are linked from profiles but **never probed** — response shapes unverified (`docs/bnet-api-notes.md` §3, "not probed" list).
- `character-media` endpoint returns 200 (probe cap) — source for header character render.
- `/data/wow/item/{id}`: 200 for some ids (127), **404 for others (10657)**; even where it answers it carries no stat numbers (§8 of notes).
- Icons: only `render.worldofwarcraft.com` + `us.api.blizzard.com` allowed by `isSafeIconUrl()` in `js/detail.js` (~L29–43). Keep the allowlist discipline if Phase 0 surfaces new Blizzard hosts.
- Class colors hardcoded in `js/util.js` (`CLASS_COLORS`, `CLASS_BAR`).
- `gh` CLI authenticated (combatwombat16); `.github/workflows/sync-bnet.yml` has `workflow_dispatch`.
- API namespaces: `profile-forever-us`, `dynamic-forever-us`, static ns pinned like `static-1.60.1_70399-forever-us` (`tools/bnet/config.py`).

---

## Phase 0 — Authoritative API probe (read-only)

Load creds the documented way: `set -a && . ./.env && set +a && python3 -m bnet probe` (see `tools/bnet/probe.py` ~L932). Never print secret values.

- [x] **P0.1** Probe `…/{slug}/reputations` on sample characters; record payload shape (faction names, standing values, max values, category grouping).
- [x] **P0.2** Probe `…/{slug}/professions`; record shape (primary/secondary, skill level, recipes if present).
- [x] **P0.3** Probe `/specializations` full payload; confirm `talents[]` fields and whether spell icons are resolvable (spell id → Blizzard CDN icon on `forever-us`). Check `class_definition`/tree-grouping hints if present.
- [x] **P0.4** Item-stat hunt (user's explicit ask): probe equipment `item.id` docs, `item/bonus-list`, and **classic-era namespaces** (`classic-era-1-us` etc.) for any authoritative equipment stat numbers. Record definitively: available / unavailable.
- [x] **P0.5** Probe `/statistics` and `character-media` (inset/headshot URLs); verify icon URLs pass the safe-host policy.
- [x] **P0.6** Append findings to `docs/bnet-api-notes.md` following existing table/provenance convention.

**Gate:** P0.4 decides tooltip stat content; P0.1/P0.2 decide Professions/Reputation renderers.

## Phase 1 — Sync extension (`tools/bnet/`)

- [ ] **T1.1** Gear: capture per-item tooltip data in `fetch.py` (item id, quality, class/subclass, enchant `display_string`s, **`stats[]` (value, display_string, color, is_equip_bonus)** — gem/socket info DROPPED per Phase 0: does not exist in API), keeping the `safe_artifact_url` validation discipline.
- [ ] **T1.2** Stats: store `/statistics` combat-stat payload in the sidecar (currently fetched & discarded).
- [ ] **T1.3** Talents: store selected talents (rank, spell name, tooltip/description, spec grouping, spell icon URL resolved via `spell_tooltip.spell.id` @ static ns — NOT `talent.id`). **Plus (amended):** fetch spec talent-tree definitions (nodes: display_row/col, raw_position_x/y, unlocks, locked_by, ranks[].tooltip) for the specs actually in use and store ONCE in a separate `data/talent-trees.json` (version-pinned to the static ns string), keyed by spec id — do NOT duplicate tree geometry per character in `character-data.json`.
- [ ] **T1.4** Professions + reputations: fetch and store per P0.1/P0.2 shapes; treat whole blocks as optional (mirror how guild/progress optionality is handled).
- [ ] **T1.5** Character media: store `avatar`/`avatar-sd` render URLs for the modal header (inset/headshot do not exist in this API).
- [ ] **T1.6** Python unit tests: `python3 -m unittest discover -s tools -p "test_*"` green with new extractors.
- [ ] **T1.7** Run `python3 -m bnet sync` locally with `.env`; commit enriched `data/character-data.json` (follow the "Sync Battle.net character data [skip ci]" commit convention).

## Phase 2 — Modal UI (`js/detail.js`, `css/app.css`, maybe new `js/` module for tabs/tooltip)

- [ ] **T2.1** Tab component (new pattern — none exists): tab bar Gear · Talents · Professions · Reputation · Stats · Progress; WoW-styled (see `wow-border` classes, Cinzel font); accessible (roles, arrow-key nav, aria-selected); state kept per-open, reset on character switch.
- [ ] **T2.2** Header: Blizzard character render (if synced), faction/class-colored title, existing field grid (level, ilvl, guild, active spec, achievement points, bnet slug, synced timestamp).
- [ ] **T2.3** Gear tab: 15-slot layout with Blizzard icons + **hover tooltip** — quality-colored name, ilvl, slot, class/subclass, enchant display strings, **stat lines (P0.4: available — value/display_string/color)**. Gem lines dropped (no API source). Tooltip positioned to stay in viewport, no flicker between adjacent slots.
- [ ] **T2.4** Talents tab: **visual talent tree** (amended) — render spec tree geometry from `data/talent-trees.json`, light nodes from selection data, rank shown, spell tooltip on hover; a compact selected-talents list may remain as a summary. Group can hold multiple specs (e.g. Arms+Protection); "no spec chosen" handled (Hunter case, notes §3.4).
- [ ] **T2.5** Professions tab per T1.4 data.
- [ ] **T2.6** Reputation tab per T1.4 data (bar/standing styling if values support it).
- [ ] **T2.7** Stats tab: classic combat stats from `/statistics` (base/effective, rating triples, resistances) — grouped sensibly (Attributes / Combat / Modifiers / Resistances).
- [ ] **T2.8** Progress tab: move existing raid/M+/PvP `progressSections()` here unchanged in behavior.
- [ ] **T2.9** Fixed footer: sync note, review note, candidates picker, review action buttons — always visible under the tab panel.
- [ ] **T2.10** Empty states in every tab when data is absent (stable tab bar, decision #5).
- [x] **T2.11** NO-OP (Phase 0): all 633 probed URLs pass the existing `isSafeIconUrl()` allowlist; no extension needed.
- [ ] **T2.12** Regenerate Tailwind if new utility classes are used; custom visuals into `css/app.css`.

## Phase 3 — Verification & landing

- [ ] **V.1** Extend `node .pi/scripts/verify_search_detail.mjs` to cover tab structure, tab switching, tooltip data wiring, empty states.
- [ ] **V.2** Extend `python3 .pi/scripts/verify_sync_artifacts.py` for new sidecar fields (schemas, hosts, secret scan).
- [ ] **V.3** Full suite green: `verify_stats.mjs`, `verify_writes.mjs`, `verify_search_detail.mjs`, `verify_bnet_probe.py`, `verify_sync_artifacts.py`, `python3 -m unittest discover -s tools -p "test_*"`.
- [ ] **V.4** Manual pass on `python3 -m http.server 8000` (user eyeballs; no local browser automation).
- [ ] **V.5** Commit to `main`; confirm Pages deploy + that nightly Actions sync preserves the new fields.

## Follow-up Milestones (out of scope this round)

- ~~**M1 — Visual talent tree**~~: **pulled into scope 2026-11-08** — live API serves native tree geometry (notes §9); no community data needed. Remaining follow-up candidates: class-tree (non-spec) display, and per-node unlock animation.

## Known Risks

- Reputations/professions payload shapes unverified until P0.1/P0.2 — renderers designed after probing, not before.
- Full item stat numbers very likely absent from the public API; P0.4 settles this honestly.
- Sparse sidecars (some entries are status-only) — empty states carry most characters today.
- Game patches can move endpoints; all contracts live in `tools/bnet/config.py` + `docs/bnet-api-notes.md` (keep that true).
