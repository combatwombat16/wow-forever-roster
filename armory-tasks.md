# Armory Integration Tasks

Battle.net real-character-data integration for the WoW: Forever roster site.
Written 2026-11-08 from a verified recon session. Execute in a subsequent session,
or hand individual phases to worker agents. Follow `pi-warden.md` at all times.

**Deployed 2026-11-08:** pushed to `main`, Pages flipped to the Actions build system; first
`Deploy GitHub Pages` run green on the rebased tip and the site verified live (zero CDN scripts,
module entry + built `css/tailwind.css` + `data/` served). Hashes in this file are post-rebase.
**Status as of 2026-11-08, aligned to commit `14eab90`:** Phase 0 partial (0.4 open), Phase 1 ✅, Phase 2 ✅,
Phase 3 ✅ (offline-verified — see its acceptance note), Phase 3bis ✅, Phase 4: 4.1 ✅ and 4.2 ✅, **4.3 open**
(manual end-to-end needs a push plus an owner PAT). Amendments are marked inline as `NOTE` / `AMENDED` /
`SUPERSEDED`; the design-level record of those amendments is `docs/refactor-design.md`.

---

## Architecture decisions (already made — do not re-litigate)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Auth scope | Start read-only public data. Design so Battle.net OAuth login can be added later (redirect URI registered for it from day one). |
| 2 | Secret custody | **GitHub Actions only.** No Cloudflare Worker, no runtime server. `client_secret` lives exclusively in Actions repo secrets. |
| 3 | Roster→Battle.net mapping | Auto-resolved sidecar file (`battle-net-map.json`). Unique direct-GET hit → auto-map; ambiguous/missing → `needs_review`, never guessed. |
| 4 | Data depth | Tier 1 + 2 + 3: profile summary, raid/M+/PvP progress **where Forever exposes it**, full equipment with Blizzard item icons. |
| 5 | Search | (a) client-side roster search box over local+enriched data, AND (b) maintainer lookup via `workflow_dispatch` input. There is **no Blizzard character-search API** (verified 404) — lookup is direct-GET by name. |
| 6 | UI placement | Detail modal on card click + compact badges on roster rows. Keep current visual language. |
| 7 | Target game | **WoW: Forever**, US region. Namespaces `profile-forever-us`, `dynamic-forever-us`, version-pinned static ns. NOT retail `static-us`/`dynamic-us`. |

## Verified facts (probed live 2026-11-08 — re-verify in Phase 1, game is new and moving)

- All Blizzard API calls require an OAuth access token. Token-less → 404; bogus token → 401 (use 401-vs-404 to detect whether a path is routed).
- Token mint: `POST https://us.battle.net/oauth/token`, HTTP Basic `client_id:client_secret`, `grant_type=client_credentials`. Tokens last 24 h.
- Rate limit: 36,000 req/h, 100 req/s per client. Our load (< 500 req/day) is trivial.
- Character profile, **realmless**: `GET https://us.api.blizzard.com/profile/wow/character/{name}?namespace=profile-forever-us` → routed (401 unauth). Forever characters have a `surname` field and no realm; full name form may be `{name}-{surname}`.
- `…/character/{name}/equipment?namespace=profile-forever-us` → routed.
- `data/wow/{ruleset,region,realm}/index?namespace=dynamic-forever-us` → routed.
- Static namespace is version-pinned, e.g. `static-1.60.1_70399-forever-us` (plain `static-forever-us` → 403). Discover the current version at runtime from `playable_class`/`playable_race` hrefs in responses; never hardcode it.
- `character-search` endpoint: **does not exist** (404 on all namespaces probed).
- Retail `profile/wow/character/{realm}/{name}/achievements?namespace=profile-us` → routed; retail `progression/raid/131?namespace=dynamic-us` → not routed at probed path. **Forever progression/M+/PvP availability is UNKNOWN** — Phase 1 settles it.
- Character data updates when the character logs out (standard WoW profile behavior).
- Known instability: EU Forever character endpoints returned 404 as of Nov 6 (open forum report); US works per community. Endpoints undocumented → keep every path/namespace in one config block.
- Blizzard media/icon URLs (render hosts) hotlink fine from browsers and are embedded in API payloads as `href`s. User approved Blizzard image URLs as the only third-party image source (no new CDN scripts — pi-warden rule stands).

## Repo facts

- Single-file SPA `index.html` (vanilla JS + Tailwind Play CDN; global functions for inline handlers; no build step). Do not split, no modules, no new CDN scripts.
      **SUPERSEDED 2026-11-08** — see the Phase 3 header amendment below: the UI is modular ES modules
      (`index.html` shell + `js/*.js` + `css/app.css`), Tailwind is generated at Pages deploy rather than
      loaded from the Play CDN. The "no new CDN scripts" and "no bundler/build step in the repo" halves
      still stand and are enforced (`pages.yml`, `verify_sync_artifacts.py`).
- `characters.json` is **machine-owned** by the external tracker (commits with `[skip ci]`). Schema fixed: `id, player, name, track, role, roleSec, race, class, prof1, prof2`. **Never write or reformat it.** No realm field — that's why `battle-net-map.json` exists.
      **SUPERSEDED 2026-11-08** — the file moved to `data/characters.json` in the v2 envelope
      `{schemaVersion, characters:[…]}` (design §2, commit `ee1eb52`); the last tracker commit to the old
      root path is `c49af62`, and the root copy no longer exists (`verify_sync_artifacts.py` forbids one).
      The ten user-authored fields are still fixed and are still not edited by the sync tool — but the file
      is now also written by the site's own PAT write paths (`validation` blocks) and by the bulk-push
      path, so "never write" now means "never written by the Battle.net pipeline", not "never written".
- Hosting: GitHub Pages, live at `https://combatwombat16.github.io/wow-forever-roster/`, served from `main` of **public** repo `combatwombat16/wow-forever-roster`.
- Existing localStorage keys (`wow_forever_roster`, `wow_gh_config`) are frozen. Do not add keys for this feature — fetch the sidecar per page load instead.
- Page must fully work when the sidecar is missing/stale/failing (existing rule).
- All strings from the sidecar are untrusted input: escape before `innerHTML`, same as existing roster strings.
- Existing GitHub PAT sync feature stays as is; do not entangle it with Blizzard data.
      **AMENDED 2026-11-08** — the PAT path is now also the review-commit path and can dispatch a Battle.net
      refresh (`26c3dc1`, design §4). The entanglement that remains forbidden is credential entanglement: no
      Blizzard secret ever reaches the browser, and the dispatch carries only `lookup_name`/`force` — and
      can carry nothing else, because `workflow_dispatch` rejects any other body key (see the §4 amendment
      in `docs/refactor-design.md`: `client_payload` is rejected by the API, so it is not sent).

### Repo layout as built (2026-11-08, at `722c428`)

```
index.html                 # 389-line markup shell; only script is <script type="module" src="js/main.js">
css/app.css                # hand-written custom classes (loaded after css/tailwind.css)
css/tailwind.css           # generated by pages.yml at deploy; committed so local dev needs no build
tailwind.config.js         # the single Tailwind input (content: index.html + js/**)
js/main.js                 # bootstrap: imports modules, sidecars → roster → Contents pull
js/state.js                # characters + filter + query = single source of truth; renderAll dispatcher
js/data.js                 # sidecar loading, byRosterId join, 36 h freshness, header sync line
js/github.js               # PAT config, Contents API pull/push, workflow_dispatch + run polling
js/roster.js               # roster grid, status dot, sidecar chips, lifecycle badge
js/detail.js               # detail modal + the review actions (confirm / resolve / report / re-open)
js/search.js               # search box: debounced input feeding state.js's query
js/editchar.js  js/party.js  js/professions.js  js/stats.js  js/util.js  # edit form, party sim, matrix,
                            # distribution bars, escapeHtml + class colours
data/characters.json       # canonical roster (v2)
data/character-data.json   # Blizzard display data sidecar (sync output)
data/battle-net-map.json   # roster id → Battle.net resolution + candidates (sync output)
tools/bnet/                # sync + probe package (stdlib only)   tools/tests/  tools/migrate_v2.py
.github/workflows/sync-bnet.yml  # secrets live here (nightly + manual)
.github/workflows/pages.yml      # Tailwind build + Pages deploy, zero secrets
.pi/scripts/verify_*.mjs|py      # deterministic verification (see Phase 4)
docs/                      # bnet-api-notes.md, bnet-probe-raw.json, refactor-design.md
```

## Prerequisites (Phase 0 — human, one-time, ~10 min)

- [x] 0.1 Battle.net account with authenticator (2FA) + Developer ToU accepted.
- [x] 0.2 Create client at https://develop.battle.net/access/clients :
      name globally unique, Redirect URI exactly `https://combatwombat16.github.io/wow-forever-roster/` (registered now for the future login phase), Service URL = the same site, intended use = "display WoW: Forever character info for our guild roster".
- [x] 0.3 Save repo secrets (Settings and variables → Actions): `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`. Never commit values; never echo in logs. (Confirmed 2026-11-08: local client mints tokens; exact secret names confirmed by user. Actions-side existence gets proven by the first workflow run.)
- [ ] 0.4 Roster members: in-game privacy "Community Sites and Apps" = shared, profiles public. Characters must log in at least once post-launch. (OPEN — ongoing: only 4/20 roster entries resolve on the live API as of 2026-11-08; the rest 404 pending member opt-in/login.)

## Phase 1 — Endpoint probe (script, run once with real creds)

Goal: freeze the forever-API contract before building anything on it.

- [x] 1.1 Write `tools/probe_bnet.py` (or `.mjs`): reads creds from env, mints token, probes with a REAL character name supplied at invocation:
      REPLACED: the script reads names from `characters.json` itself (re-runnable after tracker renames — which happened mid-Phase-1) instead of taking a name at invocation.
      - `profile/wow/character/{name}?namespace=profile-forever-us` (also try `{name}-{surname}`, lowercase/uppercase, URL-encoded accents)
      - `…/equipment`, `…/achievements`, `…/statistics`, `…/summary`, any `progression*`/`media` variants discoverable from links in responses
      - `dynamic-forever-us` realm/ruleset/region indexes
      - static ns discovery: read `playable_class` href from a character response → extract version-pinned ns (do not hardcode)
- [x] 1.2 Record: which endpoints return 200, payload field inventory for level/guild/spec/ilvl/gear+icon URLs/raid progress/M+/PvP, whether names are `{name}` or `{name}-{surname}`, whether the `character` self `href` in responses is the canonical lookup form.
- [x] 1.3 Commit findings to `docs/bnet-api-notes.md`. Every later phase reads its paths from here.
- [x] 1.4 If a Tier-2 datum (raid/M+/PvP) does not exist yet: note it, design UI to omit that section cleanly, re-probe later.
      DONE: raid/M+/PvP endpoints route but carry no data (or are 404) — see notes §4. Sidecar and UI OMIT these sections entirely; re-probe is a scheduled later action, not a stub.

Acceptance: `docs/bnet-api-notes.md` lists, per data field, the exact URL + JSON path; no guessed paths remain.
ACCEPTED: met by commits `d8409fb` + `c7477d2`. `.pi/scripts/verify_bnet_probe.py` (41 checks) enforces that every cited JSON path exists in captured payloads and that the notes cover the current roster.

### Phase 1 outcome & amendments carried into Phase 2 (2026-11-08, from live probes)

1. **Slug rule**: canonical lookup slug is `lower(name)-lower(surname)`. Routes are **lowercase-only** — any capital letter 404s in exactly the same shape as "character missing". Normalization order: lowercase hyphenated form first, then legacy variants (space-removed, %20, original case) as fallbacks; record the matched form per character.
2. **Key on roster id, never name.** `characters.json` currently holds the duplicate name `Tir Rheebeard` under two ids; both are `needs_review` by policy regardless of probe outcome.
3. **Caching**: there are **no ETags** anywhere (0/114 responses). Use `Last-Modified` + `If-Modified-Since` → 304 (proven). Task 2.4's "ETag-aware" reads as "Last-Modified-aware".
4. **Guild-roster-assisted mapping APPROVED by user** (was not in the original plan): every resolved character exposes `guild.slug`; `GET /data/wow/guild/{slug}/roster?namespace=profile-forever-us` lists members with `character.slug/name/surname/class` and is public (notes §7.2). Caveats from probes: guildless characters omit `guild` entirely (e.g. `morc-theorc`); guild rosters contain members NOT in our tracker (e.g. `healy-bones`) — the lever is one-directional (resolve tracker rows via guild membership; never assume tracker ⊇ guild); the guild doc's `member_count` is unreliable; roster is cached `max-age=10800`; guild slugs are only discoverable from an already-resolved member.
5. **Static namespace**: version-pinned (`static-<ver>-forever-us`), discovered at runtime from `playable_class` hrefs; plain `static-forever-us` → 403; `dynamic-forever-us` class/race indexes are dead — read those from static.
6. **Icons**: one extra request per item (`/data/wow/media/item/{id}` → `assets[].value`, `render.worldofwarcraft.com/forever-us/icons/56/...`). Locale strings are inline maps (`.en_US`), no indirection.
7. **Drift tripwire**: responses carry `battlenet-schema` / `battlenet-schema-revision` headers — sync should log them to catch Blizzard schema changes.
8. **Freshness**: 18/20 characters 404 pending owner opt-in (0.4 stays open); the nightly run must treat `not_found` as a normal state, and it can flip to `ok` without any change on our side.
9. **Roster churn**: the tracker renames/edits characters frequently (a wave of renames landed mid-Phase-1). Every sync run must re-resolve against the CURRENT `characters.json`; stale map entries for removed ids should be pruned or marked, never block the run.

## Phase 2 — Nightly sync workflow

Files: `.github/workflows/sync-bnet.yml`, `tools/sync_bnet.py` (or `.mjs`), `battle-net-map.json`, `character-data.json`.

- [x] 2.1 Workflow triggers: `schedule` (cron, nightly) + `workflow_dispatch` with inputs `lookup_name` (maintainer search) and `force` (refetch ignoring ETags). **No `push`/`pull_request` triggers.** `permissions: contents: write` only.
      NOTE: enforced by `verify_sync_artifacts.py`, which also pins the step order sync → verify → commit (security review H4: nothing is committed that the artifact checks reject).
- [x] 2.2 Token: mint client-credentials token at job start from secrets; keep in memory only; never log; refresh on 401.
      NOTE: the mint cap is 3/run and a mid-run 401 re-mints + replays once. Security review H3: the base64 `client_id:client_secret` pair and the URL-encoded secret are registered with the redactor, so a token endpoint that echoes its own request cannot leak them into the public Actions log.
- [x] 2.3 Mapping step: for each `characters.json` entry not confidently mapped, direct-GET `profile/wow/character/{normalized(name)}`. 200 → store the canonical `href` + exact name; 404 → `status: "not_found"`; any ambiguity signal → `needs_review`. Never auto-guess.
      NOTE: keyed by roster id (amendment 2) and extended with the guild-roster lever (amendment 4); a 304 counts as a hit. Stored `href` is validated against the frozen profile-URL shape before it is written (H2) — a payload self/key href that is not a Blizzard URL is dropped in favour of the URL we construct.
- [x] 2.4 Enrichment: for mapped characters, fetch profile/equipment/achievements/available-progress per Phase 1 notes. ETag-aware (store `etag`s, send `If-None-Match`), honor `retry-after`/429 with backoff, hard-cap requests per run.
      NOTE: Last-Modified not ETag per amendment 3 — no response has ever carried an ETag, so `If-Modified-Since` is the mechanism and `If-None-Match` is never sent. `retry-after` (seconds or HTTP-date) + 3 retries, hard cap `MAX_REQUESTS = 2500`. Sub-resource and icon GETs are reused from a profile 304 (shared §7.3 timestamp).
- [x] 2.5 Emit normalized sidecar `character-data.json` (do NOT dump raw Blizzard blobs; normalize so Blizzard schema drift can't break the UI):
      NOTE: every URL written to either artifact passes one shared validator — href must be a Blizzard profile URL, `iconUrl` must match `^https://render\.worldofwarcraft\.com/` (H2); refused non-Blizzard URLs are also withheld from notes and logs.
      ```json
      {
        "schemaVersion": 1,
        "syncedAt": "…ISO8601…",
        "characters": {
          "<roster id>": {
            "status": "ok|not_found|needs_review|fetch_error",
            "bnet": { "name": "…", "surname": "…", "href": "…" },
            "level": 0, "guild": "…", "activeSpec": "…", "itemLevel": 0,
            "gear": [ { "slot": "…", "name": "…", "ilvl": 0, "iconUrl": "…" } ],
            "progress": { "raid": [], "mythicPlus": null, "pvp": null },
            "achievementPoints": 0,
            "fetchedAt": "…"
          }
        }
      }
      ```
      Exact fields per Phase 1 reality; omit unavailable sections entirely rather than stub.
- [x] 2.6 Commit both files with `[skip ci]`, dedicated bot identity; skip commit when unchanged.
      NOTE: the commit step now runs only after `python3 .pi/scripts/verify_sync_artifacts.py` succeeds (no `if:`/`continue-on-error` on that gate).
- [x] 2.7 Maintainer lookup mode (`lookup_name` input): direct-GET the name, write the match + result into the run summary (workflow summary markdown), update map if unambiguous.
      NOTE: lookup writes the **map only** (never `character-data.json` — enrichment is a full-run concern), and only when exactly one roster row carries the normalized name.
- [x] 2.8 Failure policy: one character failing never aborts the run; per-character `fetch_error` + run annotation. If ALL fail (e.g. creds/quota), leave the previous sidecar untouched and fail the job loudly.
      NOTE: extended with the H4 mass-flip floor — `ok_prev >= 3 and ok_now * 2 < ok_prev` refuses BOTH artifact writes, prints FATAL + a `GITHUB_STEP_SUMMARY` line and exits 1. A Blizzard-wide 404 storm is a live upstream bug (amendment 8) and lands as `not_found`, which the all-`fetch_error` guard cannot see.

Acceptance: a successful manual run commits both JSON files; second run is cheap (ETags); `battle-net-map.json` flags every non-unique/missing character instead of guessing.

## Phase 3 — UI (~~index.html only, single file~~ modular static ES modules, existing style)

> **AMENDED 2026-11-08 — owner decision lifts the "index.html only, single file" half of this header**
> (drafted as `docs/refactor-design.md` §7, implemented in `8e04e54`, re-confirmed by `63bab1a`/`26c3dc1`/
> `722c428`). The UI is modular static: `index.html` is a 389-line markup shell whose only script is
> `<script type="module" src="js/main.js">`; the app is twelve ES modules under `js/` plus `css/app.css`.
> What survives of the original constraint, and is enforced: **no bundler, no build step in the repo, no new
> CDN scripts, existing visual language.** Tailwind is compiled at Pages deploy from `tailwind.config.js`
> (`pages.yml`, `722c428`) and `index.html` loads the committed `css/tailwind.css` in place of the Play CDN.
> Modules publish their inline handlers with explicit `window.fn = fn` assignments, so the old
> "every `on*` handler is global" invariant still holds — it just lives in the modules now.

- [x] 3.1 Load `character-data.json` at startup (fetch + timeout + graceful fallback: no sidecar → everything renders exactly as today). Do not persist it in localStorage.
      DONE (`f3cb9b6`): `js/data.js` — `loadSidecars()` fetches both sidecars in parallel through
      `loadSidecar(path)`; every failure path (non-2xx, truncated body, non-object, network error) returns
      `null` independently per file, `byRosterId(id)` is the null-safe join, and
      `SIDECAR_FRESH_HOURS = 36` is the freshness rule. Sidecars stay in module scope and are never written
      to localStorage (`verify_search_detail.mjs` §6 pins "no localStorage growth").
      DELTAS vs the original tasking: (a) a sidecar is now a plain **same-origin relative `fetch`** of the
      committed `data/*.json` — not a GitHub API call; the Contents API is used only for the roster pull and
      the PAT write paths; (b) `DEFAULT_CHARACTERS` (`js/state.js`) survived the refactor but is demoted to an
      **offline-only seed** — localStorage first, `DEFAULT_CHARACTERS` on empty/parse failure, then
      `data/characters.json` and finally the Contents pull overwriting it (`js/main.js` load order:
      sidecars → roster → pull); (c) **no explicit request timeout**: there is no `AbortController`/deadline,
      the guarantee is that a rejection or a bad body degrades to `null`, so a hung request holds its panel
      in the no-sidecar shape rather than blocking the page.
- [x] 3.2 Compact badges on existing roster rows: level, ilvl, guild (truncate long guilds) + status dot: green fresh / amber stale (>36 h) / grey unmapped / red error. Use existing panel classes (`wow-border bg-slate-900/70 …`, `CLASS_COLORS`, amber cinzel headings) — no inline-style colors.
      DONE (`f3cb9b6`): `js/roster.js` — `getSidecarDot()` maps the four dot states (`bg-emerald-400` fresh /
      `bg-amber-400` stale past 36 h / `bg-slate-600` unmapped-or-no-match-or-no-data-yet / `bg-red-500`
      fetch error) with a `title` explaining each; `getSidecarMeta()` renders the Lv / iLvl / guild chips;
      `getLifecycleBadge()` adds the always-on lifecycle badge (all six states of design §2, shown with or
      without a sidecar) and is clickable on `needs_resolution` rows to open the candidate list. Tailwind
      class strings only — no inline-style colors. Validated rows render their edit button disabled
      (`isLifecycleEditable`).
- [x] 3.3 Detail modal (new, same visual language): header (name, surname, guild, level, race/class, active spec), gear grid with Blizzard icon `<img>`s (icon URLs from sidecar; `loading="lazy"`, alt text), progress sections (omit sections absent from data), per-character sync timestamp.
      DONE (`63bab1a`, extended by `26c3dc1`): `js/detail.js` — `openCharacterDetail()`/`detailHtml()` on card
      click, `gearGrid()` with `loading="lazy"` + `alt` on every icon, gated by `isSafeIconUrl()` (exact
      `https` hosts `render.worldofwarcraft.com` / `us.api.blizzard.com` only; anything else renders a
      placeholder `<span>`), `progressSections()` omitting any section the data does not carry, per-character
      `fetchedAt` freshness line, ESC/backdrop close, zero localStorage writes. Same module also owns the
      review actions added in Phase 3bis (`confirmMatch`, `selectCandidate`/`confirmSelectedCandidate`,
      `reportMismatch`, `reopenForEditing`, `refreshFromBattleNet`).
- [x] 3.4 Roster search box next to the Squad Filter: instant filter over name, player, class, role, guild (and realm/lookup name). Must compose with the existing track filter (search AND filter) and drive all panels through `renderRoster()` per the "characters + currentFilter is the single source of truth" rule — extend, don't fork, the filtering path.
      DONE (`63bab1a`): `js/search.js` (`onSearchInput` debounced `SEARCH_DEBOUNCE_MS = 120`, `clearSearch`,
      `#search-input`/`#clear-search` in `index.html`) feeding `setQuery()` in `js/state.js`. The path was
      extended, not forked: `getFilteredCharacters()` is now track AND query and every panel (roster, race,
      class, professions, party pool/synergy) repaints through `renderAll()`. Guild and the resolved Battle.net
      lookup name are searchable via the `registerSearchProvider()` hook (`data.js` supplies it, so `state.js`
      stays a leaf module with no sidecar import).
- [x] 3.5 "Last synced …" line near the header repo-status widget; falls back to "No Battle.net data" when sidecar missing.
      DONE (`f3cb9b6`): `#bnet-sync-line` in `index.html` (next to `#sync-status`), written by
      `renderSyncLine()` in `js/data.js` — `formatSyncedAt(syncedAt)` plus `syncTriggerLabel(triggeredBy)`
      (nightly vs the dispatching GitHub login), and the literal `No Battle.net data` fallback when the
      sidecar is absent or carries no `syncedAt`. Registered as a render hook so repaints keep it consistent.
- [x] 3.6 All sidecar strings HTML-escaped (same helper as existing roster escaping).
      DONE: the one shared `escapeHtml()` in `js/util.js` is used by every sidecar-derived interpolation in
      `roster.js`, `detail.js`, `data.js` and `search.js`; no module defines a second escaper. Enforced by
      `verify_stats.mjs` §5 and by the XSS fixtures in `verify_search_detail.mjs` §5 (foreign-host, `javascript:`
      and look-alike-host payloads), which run against fixtures in `/tmp`, never against the repo's `data/`.
- [x] 3.7 New render entry points called from `renderRoster()`, including its empty-roster early-return path.
      DONE: `renderRoster()` is registered with `registerRenderHook()` (`js/roster.js:238`) and every new
      entry point is registered the same way, so the single dispatcher `renderAll()` (`js/state.js`) paints
      grid → panels → sync line → open detail modal. The empty path is inside `renderRoster()` before the
      early `return` (escaped "No characters match …" vs "No characters in this filter."), and the count
      header is written from the same `getFilteredCharacters()` result, so the grid can never show rows the
      panels are not counting.

Acceptance: with the sidecar deleted, the site behaves identically to today; with it, badges/modal/search work on Pages; no new localStorage keys; no new CDN scripts; every new `on*="fn("` handler is global and every `getElementById` target exists.
ACCEPTED for everything verifiable offline: `verify_stats.mjs` (197 checks) + `verify_search_detail.mjs` (97 checks)
prove the id/handler/escaping/no-new-localStorage keys and the search/detail behaviour under a DOM stub. The
"work on Pages" half is **not yet proven** — `722c428` and its six predecessors are still unpushed
(`git log origin/main..HEAD`), and the one-time Pages `build_type` flip has not been confirmed from the tree.

### Phase 3bis — validation lifecycle + on-demand refresh (added by the refactor design; done `26c3dc1`)

Not in the original Phase 3 list; tracked here so this file stays the roster of what the UI does.

- [x] Lifecycle badges + editability per design §2 (`deriveLifecycleState`/`isLifecycleEditable` in `js/state.js`;
      only `validated` is ever stored, the rest derived; unknown values fail safe to `awaiting_sync`).
- [x] Review write paths in the detail modal: CONFIRM MATCH, RESOLVE (candidate radios), REPORT MISMATCH
      (note required, ≤240 chars), RE-OPEN — each committing `data/characters.json` through
      `commitRosterFile()` with a fresh-sha GET before every PUT and a 409 → "pull first" answer. A failing
      commit never mutates local state; handler arguments are shape-checked so untrusted strings cannot reach
      `on*` attributes, and `confirmedSlug` must match `SLUG_RE`.
- [x] On-demand "Refresh from Battle.net": `workflow_dispatch` (`lookup_name`) with the user's PAT, bounded
      poll (10 s interval, 180 s cap), then a cache-busted sidecar reload; single-flight; 403 → "your PAT needs
      Actions: read/write" guidance.
- [x] Sync honours an open `validation.note` (`tools/bnet/resolve.py: reported_note_entry`) — a reported
      mismatch is forced back to `needs_review` on every run until a `validated` commit replaces it.
- [x] `editchar.js` refuses to open or save the edit form for a validated row and warns on delete.

## Phase 4 — Verification & review

- [x] 4.1 Extend `.pi/scripts/` checks (or add `verify_armory.mjs`): sidecar + map JSON parse and schema-validate; every `getElementById` id present & unique; every inline handler resolves to a global function; sidecar-derived interpolations all pass through the escaper; `rg` scan proving no token/secret patterns in the repo or workflow logs; `characters.json` byte-identical to before the change.
      DONE — deliberately **not** as one `verify_armory.mjs`: the coverage was split into five verifiers that
      each own a surface, so a failure names the surface. Counts below are the current totals — re-run at
      `f2d4edd` (post fix-wave: verify_writes 140→220, verify_stats 181→197, unit suite 35→44), then again
      after the **live bug-fix wave on top of `9822da1`** (the phantom "roster changed upstream" drift, the
      422-rejected dispatch body, and the `needs_review` row with no way out: verify_writes 220→256,
      verify_stats 197→199, unit suite 44→49; the other three verifiers unchanged):

      | Script | Checks | Owns |
      |---|---|---|
      | `.pi/scripts/verify_sync_artifacts.py` | 486 | map + data + roster parse and exact v2 key sets, ok-rows carry real identity, ambiguous rows carry candidates and no slug, whitelisted sidecar keys (no raw Blizzard blobs), href/iconUrl hosts, secret scan, no root-level JSON copies, sync-workflow trigger/permissions/step-order, **and the `pages.yml` shape (secret-free, minimally permitted, Tailwind-built, official Pages actions, allowlisted bundle)** |
      | `.pi/scripts/verify_stats.mjs` | 199 | static structure across `js/**` (every `getElementById` target exists, no duplicate ids, every inline handler resolves **and** is assigned to `window` by a module), colour-map agreement, rendered stats vs an independent tally of `data/characters.json`, empty-filter placeholder, escaping, sidecar wiring (3b), search + detail (3c), review-action wiring (3d) |
      | `.pi/scripts/verify_writes.mjs` | 256 | the browser write paths under a DOM stub: no-PAT gating, action set per lifecycle state (**all three `needs_review` shapes, and the pick-candidate badge**), fresh-sha GET before PUT + v2 envelope + design §2 payload shape, 409 → pull-first with nothing applied locally, **§4d: the baseline after a commit, run against a branch that keeps what was PUT plus GitHub's 60 s max-age cache**, RESOLVE/REPORT/RE-OPEN + the needs_review exits, refresh dispatch body (**only `ref` + the two declared inputs — the harness 422s anything else, as the API does**) + 403 guidance + bounded poll, editchar gating, and an origin allowlist of `api.github.com` + same-origin only |
      | `.pi/scripts/verify_search_detail.mjs` | 97 | search AND track composition, every panel repaints on query change, debounce/clear, modal open paths, gear host allowlisting (foreign host, `javascript:`, look-alike host), section omission, escaping, ESC/backdrop close, no localStorage growth |
      | `.pi/scripts/verify_bnet_probe.py` | 42 | Phase 1 contract: every JSON path cited in `docs/bnet-api-notes.md` exists in the captured probe payloads; workflows allowlist |
      | `python3 -m unittest discover -s tools -p "test_*"` | 49 tests | offline stdlib-only unit tests for the sync package (allowlist, redactor, H4 floor incl. the resolver-verdict/note ordering, artifact schemas, `validation.note` → `needs_review`, log hygiene, **the `--lookup` verdicts a Refresh depends on: multi-hit → `ambiguous` + every candidate, single hit → `ok`, and neither published without a human**) |

      Deltas against the original 4.1 wording: (a) the `characters.json` byte-identical clause moved into the
      migration — `tools/migrate_v2.py` compares bytes and rewrites nothing when the target is already v2
      (idempotent), the ten user-authored fields are unchanged in the v2 envelope, and
      `verify_sync_artifacts.py` pins `set(roster_doc) == {"schemaVersion", "characters"}` plus the absence of
      any root-level copy; (b) the secret scan reads `.env` values (length ≥ 6) out of every committed surface
      it covers — `tools/bnet/`, `tools/tests/`, both workflows, `tools/migrate_v2.py` and all three `data/`
      files — and additionally forbids the literal `Authorization` outside `tools/`. **It does not read `js/`**;
      the client-side guarantee is structural instead: `verify_writes.mjs` answers for exactly two origins
      (`https://api.github.com` and the same-origin `data/` fixture path) and fails the run on anything else,
      so no Blizzard credential can reach the browser at all.
- [x] 4.2 `reviewer` subagent pass on the workflow + sync script (the security-sensitive surface): secret handling, permissions block, fork-PR exposure, log hygiene.
      DONE — two passes, both fix-first (the findings were implemented before the work landed, not filed):
      1. **Sync surface** (`047f3a8`, "post security review"; the findings are annotated inline through Phase 2
         above and pinned by regression guards in `verify_sync_artifacts.py`):
         **H1** bearer-token host allowlist at the `Client.get` choke point + a no-redirect credentialed opener;
         **H2** strict URL validators at every artifact write site (profile-href + render-host icons) and
         pre-validation of response-derived hrefs;
         **H3** the redactor registers the base64 and URL-encoded credential forms, so a token endpoint that
         echoes its own request cannot leak them into the public Actions log;
         **H4** the mass-flip floor refuses to publish a wipe, and the commit step runs only after
         `verify_sync_artifacts.py` exits 0 (no `if:`/`continue-on-error` on that gate).
         Zero PR/push triggers, `permissions: contents: write` only, secrets via `env:` never interpolated.
      2. **Write-path review** (`26c3dc1`, the new surface: `js/github.js`, `js/detail.js`, `js/editchar.js`):
         PAT attached in exactly one place against a fixed origin; fresh-sha GET before every PUT with 409 →
         pull-first; failures never mutate local state; handler arguments shape-checked so untrusted strings
         never reach `on*` attributes and `confirmedSlug` must match `SLUG_RE`; the refresh dispatch carries
         only the two declared workflow inputs — a `client_payload` key is rejected by the API (422), so it is
         not sent at all, and provenance stays the runner's `GITHUB_ACTOR`. `verify_writes.mjs` (new in
         that commit; the harness enforces the permitted body keys the way the endpoint does) is the
         executable record of these guards.
      Not in scope for either pass: `pages.yml` arrived after them (`722c428`) and was gated by its shape
      checks in `verify_sync_artifacts.py` instead; the `workflow_run`/`head_sha` reasoning is documented in
      the workflow's own comment block.
- [ ] 4.3 Manual end-to-end: dispatch the workflow, watch first commit, hard-refresh the Pages site, check badges/modal/search/status-dot states incl. `needs_review` and `fetch_error` characters.
      **OPEN — intentionally unticked.** Every commit from `ee1eb52` (Phase 2's data layout) through `722c428`
      is still unpushed (`git log origin/main..HEAD` = 7 commits), so nothing new has ever run on Actions:
      the Actions-side existence of `BLIZZARD_CLIENT_ID`/`BLIZZARD_CLIENT_SECRET` has never been proven by a
      run, the one-time Pages `build_type: legacy → workflow` flip is unconfirmed, and the browser write paths
      need a real owner PAT (`contents: read/write` + `actions: read/write`) exercised against the live repo.
      Blocked on: push `main`, confirm/perform the Pages setting flip, then dispatch once from the UI and once
      from Actions and read the badge/lifecycle transitions on a `needs_review` and a `fetch_error` row.

## Known residual nits (verification review of `f2d4edd`, all accepted 2026-11-08)

- Confirm/candidate commits intentionally erase an upstream mismatch `note` posted after page load
  (a completed validation supersedes the objection it answers); `reportMismatch` merges instead.
  Documented at `js/detail.js` `commitValidation`.
- `deleteCharacter` clears the party slot before the push settles: a refused push leaves the slot
  empty while the row survives. UI-only; next edit restores it.
- Lookup-mode `floor_entries` snapshot has nightly + write-pair coverage but no dedicated unit test.
- The GET→PUT window on roster commits (≈200 ms) stays optimistic-concurrency: 409 → "pull first".

## Standing security scope (binding for every phase)

- `client_id`/`client_secret`: only ever in Actions secrets; never in the repo, the sidecar, logs, commit messages, or client JS.
- Workflow has zero PR triggers; only default-branch scheduled/manual runs can touch secrets. Repo write token scoped to `contents: write`.
- Browser talks only to same-origin GitHub Pages + `api.github.com` + Blizzard media image URLs. No Blizzard API calls from the browser. No Blizzard OAuth tokens anywhere (login phase deferred by design decision #1). The `api.github.com` origin is the new one added by the review-commit and refresh-dispatch paths (`26c3dc1`) and is the only non-image third-party origin the client may contact; `verify_writes.mjs` fails the run on any other origin.
- **PAT scopes (amended 2026-11-08):** a maintainer PAT needs `contents: read/write` for roster pull/commit,
  and — new since Phase 3d — **Actions read/write** to dispatch a `workflow_dispatch` refresh
  (`POST /repos/{owner}/{repo}/actions/workflows/sync-bnet.yml/dispatches`). A fine-grained token additionally
  has to be granted the repository in question; the client surfaces 401/403 as "your PAT needs Actions:
  read/write" rather than retrying. The token is attached in exactly one place in `js/github.js` against a
  fixed `https://api.github.com` origin, is kept in the existing `wow_gh_config` localStorage entry, and is
  never written into any file in this repo.
- **Trust boundary extended (amended 2026-11-08, was: "anyone with repo write access can add a workflow that
  reads the secrets"):** in addition to that standing risk, **anyone holding a `contents: write` PAT can commit
  a roster validation block** — i.e. mark a row `validated`, set `confirmedSlug`, or file a `validation.note`.
  That is by design: git history is the audit trail, and sync treats a `validated`/`confirmedSlug` row as
  trusted and stops re-resolving it (so a malicious slug can be pinned by a writer — the same authorisation
  level as editing the roster by hand). What this does **not** grant: the workflow secrets are unchanged and
  remain readable only by `sync-bnet.yml` on default-branch scheduled/manual runs; a roster commit cannot
  mint a Battle.net token, and the refresh dispatch is rate-scoped to runs that publish data through the same
  verified commit gate. Still acceptable for this solo/small-team repo; documented in README.

## Known unknowns (cannot be pre-verified — do not build on assumptions)

1. Exact Forever payload shapes and which Tier-2 fields exist (probe in Phase 1).
2. Name vs `{name}-{surname}` canonical form and collision behavior across hidden realms.
3. EU availability (open bug as of Nov 6); we target US only this iteration.
4. Whether the version-pinned static namespace changes on game patches — sync must re-discover it, never hardcode.
5. Data freshness: character data updates on logout only — surface that in the modal wording.

## Deferred (explicitly out of scope)

- Battle.net OAuth login / protected-character data (design hook: redirect URI already registered; a future `profile-forever` user-token phase). **Stays deferred 2026-11-08:** no user-token code exists in the tree; the browser holds only a GitHub PAT, and no OAuth flow was added by Phase 3d's refresh work.
- Any runtime proxy/Worker; any additional hosting.
- Changes to `characters.json`, the tracker, or existing localStorage keys.
  **Amended 2026-11-08:** the roster file moved to `data/characters.json` in the v2 envelope and is now written
  by the site's own PAT path (see the Repo-facts amendment above); the *localStorage keys* rule still holds
  exactly — `wow_forever_roster` and `wow_gh_config` are the only two keys ever written, and
  `verify_search_detail.mjs` §6 fails the build if a third appears.
- **On-demand sync note:** the "nightly only" interim UX is superseded by the in-page **Refresh from
  Battle.net** button (`26c3dc1`, design §4): a maintainer dispatches the same `sync-bnet.yml` from the browser
  with their own PAT, the UI polls the run to completion (10 s interval, 180 s cap) and reloads the sidecars
  past the HTTP cache. Nightly `schedule` remains the default data path and the only path that refreshes every
  character; dispatch with `lookup_name` refreshes one. Deferred inside this area: honouring a client-supplied
  actor for provenance — and it is not merely ignored: a `client_payload` key on the dispatch body **is rejected
  by the API, so it is not sent at all** (see `docs/refactor-design.md` §4 amendment), automatic
  re-resolve of a user-confirmed slug (design §2, stricter than drafted), and a `pages.yml` deploy that pins
  the sync run's `head_sha` (deliberately not done — see the workflow's comment block).
