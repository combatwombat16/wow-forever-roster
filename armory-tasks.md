# Armory Integration Tasks

Battle.net real-character-data integration for the WoW: Forever roster site.
Written 2026-11-08 from a verified recon session. Execute in a subsequent session,
or hand individual phases to worker agents. Follow `pi-warden.md` at all times.

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
- `characters.json` is **machine-owned** by the external tracker (commits with `[skip ci]`). Schema fixed: `id, player, name, track, role, roleSec, race, class, prof1, prof2`. **Never write or reformat it.** No realm field — that's why `battle-net-map.json` exists.
- Hosting: GitHub Pages, live at `https://combatwombat16.github.io/wow-forever-roster/`, served from `main` of **public** repo `combatwombat16/wow-forever-roster`.
- Existing localStorage keys (`wow_forever_roster`, `wow_gh_config`) are frozen. Do not add keys for this feature — fetch the sidecar per page load instead.
- Page must fully work when the sidecar is missing/stale/failing (existing rule).
- All strings from the sidecar are untrusted input: escape before `innerHTML`, same as existing roster strings.
- Existing GitHub PAT sync feature stays as is; do not entangle it with Blizzard data.

## Prerequisites (Phase 0 — human, one-time, ~10 min)

- [ ] 0.1 Battle.net account with authenticator (2FA) + Developer ToU accepted.
- [ ] 0.2 Create client at https://develop.battle.net/access/clients :
      name globally unique, Redirect URI exactly `https://combatwombat16.github.io/wow-forever-roster/` (registered now for the future login phase), Service URL = the same site, intended use = "display WoW: Forever character info for our guild roster".
- [ ] 0.3 Save repo secrets (Settings → Secrets and variables → Actions): `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`. Never commit values; never echo in logs.
- [ ] 0.4 Roster members: in-game privacy "Community Sites and Apps" = shared, profiles public. Characters must log in at least once post-launch.

## Phase 1 — Endpoint probe (script, run once with real creds)

Goal: freeze the forever-API contract before building anything on it.

- [ ] 1.1 Write `tools/probe_bnet.py` (or `.mjs`): reads creds from env, mints token, probes with a REAL character name supplied at invocation:
      - `profile/wow/character/{name}?namespace=profile-forever-us` (also try `{name}-{surname}`, lowercase/uppercase, URL-encoded accents)
      - `…/equipment`, `…/achievements`, `…/statistics`, `…/summary`, any `progression*`/`media` variants discoverable from links in responses
      - `dynamic-forever-us` realm/ruleset/region indexes
      - static ns discovery: read `playable_class` href from a character response → extract version-pinned ns (do not hardcode)
- [ ] 1.2 Record: which endpoints return 200, payload field inventory for level/guild/spec/ilvl/gear+icon URLs/raid progress/M+/PvP, whether names are `{name}` or `{name}-{surname}`, whether the `character` self `href` in responses is the canonical lookup form.
- [ ] 1.3 Commit findings to `docs/bnet-api-notes.md`. Every later phase reads its paths from here.
- [ ] 1.4 If a Tier-2 datum (raid/M+/PvP) does not exist yet: note it, design UI to omit that section cleanly, re-probe later.

Acceptance: `docs/bnet-api-notes.md` lists, per data field, the exact URL + JSON path; no guessed paths remain.

## Phase 2 — Nightly sync workflow

Files: `.github/workflows/sync-bnet.yml`, `tools/sync_bnet.py` (or `.mjs`), `battle-net-map.json`, `character-data.json`.

- [ ] 2.1 Workflow triggers: `schedule` (cron, nightly) + `workflow_dispatch` with inputs `lookup_name` (maintainer search) and `force` (refetch ignoring ETags). **No `push`/`pull_request` triggers.** `permissions: contents: write` only.
- [ ] 2.2 Token: mint client-credentials token at job start from secrets; keep in memory only; never log; refresh on 401.
- [ ] 2.3 Mapping step: for each `characters.json` entry not confidently mapped, direct-GET `profile/wow/character/{normalized(name)}`. 200 → store the canonical `href` + exact name; 404 → `status: "not_found"`; any ambiguity signal → `needs_review`. Never auto-guess.
- [ ] 2.4 Enrichment: for mapped characters, fetch profile/equipment/achievements/available-progress per Phase 1 notes. ETag-aware (store `etag`s, send `If-None-Match`), honor `retry-after`/429 with backoff, hard-cap requests per run.
- [ ] 2.5 Emit normalized sidecar `character-data.json` (do NOT dump raw Blizzard blobs; normalize so Blizzard schema drift can't break the UI):
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
- [ ] 2.6 Commit both files with `[skip ci]`, dedicated bot identity; skip commit when unchanged.
- [ ] 2.7 Maintainer lookup mode (`lookup_name` input): direct-GET the name, write the match + result into the run summary (workflow summary markdown), update map if unambiguous.
- [ ] 2.8 Failure policy: one character failing never aborts the run; per-character `fetch_error` + run annotation. If ALL fail (e.g. creds/quota), leave the previous sidecar untouched and fail the job loudly.

Acceptance: a successful manual run commits both JSON files; second run is cheap (ETags); `battle-net-map.json` flags every non-unique/missing character instead of guessing.

## Phase 3 — UI (index.html only, single file, existing style)

- [ ] 3.1 Load `character-data.json` at startup (fetch + timeout + graceful fallback: no sidecar → everything renders exactly as today). Do not persist it in localStorage.
- [ ] 3.2 Compact badges on existing roster rows: level, ilvl, guild (truncate long guilds) + status dot: green fresh / amber stale (>36 h) / grey unmapped / red error. Use existing panel classes (`wow-border bg-slate-900/70 …`, `CLASS_COLORS`, amber cinzel headings) — no inline-style colors.
- [ ] 3.3 Detail modal (new, same visual language): header (name, surname, guild, level, race/class, active spec), gear grid with Blizzard icon `<img>`s (icon URLs from sidecar; `loading="lazy"`, alt text), progress sections (omit sections absent from data), per-character sync timestamp.
- [ ] 3.4 Roster search box next to the Squad Filter: instant filter over name, player, class, role, guild (and realm/lookup name). Must compose with the existing track filter (search AND filter) and drive all panels through `renderRoster()` per the "characters + currentFilter is the single source of truth" rule — extend, don't fork, the filtering path.
- [ ] 3.5 "Last synced …" line near the header repo-status widget; falls back to "No Battle.net data" when sidecar missing.
- [ ] 3.6 All sidecar strings HTML-escaped (same helper as existing roster escaping).
- [ ] 3.7 New render entry points called from `renderRoster()`, including its empty-roster early-return path.

Acceptance: with the sidecar deleted, the site behaves identically to today; with it, badges/modal/search work on Pages; no new localStorage keys; no new CDN scripts; every new `on*="fn("` handler is global and every `getElementById` target exists.

## Phase 4 — Verification & review

- [ ] 4.1 Extend `.pi/scripts/` checks (or add `verify_armory.mjs`): sidecar + map JSON parse and schema-validate; every `getElementById` id present & unique; every inline handler resolves to a global function; sidecar-derived interpolations all pass through the escaper; `rg` scan proving no token/secret patterns in the repo or workflow logs; `characters.json` byte-identical to before the change.
- [ ] 4.2 `reviewer` subagent pass on the workflow + sync script (the security-sensitive surface): secret handling, permissions block, fork-PR exposure, log hygiene.
- [ ] 4.3 Manual end-to-end: dispatch the workflow, watch first commit, hard-refresh the Pages site, check badges/modal/search/status-dot states incl. `needs_review` and `fetch_error` characters.

## Standing security scope (binding for every phase)

- `client_id`/`client_secret`: only ever in Actions secrets; never in the repo, the sidecar, logs, commit messages, or client JS.
- Workflow has zero PR triggers; only default-branch scheduled/manual runs can touch secrets. Repo write token scoped to `contents: write`.
- Browser talks only to same-origin GitHub Pages + Blizzard media image URLs. No Blizzard API calls from the browser. No Blizzard OAuth tokens anywhere (login phase deferred by design decision #1).
- Trust boundary to document in README: anyone with repo write access can add a workflow that reads the secrets. Acceptable for this solo/small-team repo.

## Known unknowns (cannot be pre-verified — do not build on assumptions)

1. Exact Forever payload shapes and which Tier-2 fields exist (probe in Phase 1).
2. Name vs `{name}-{surname}` canonical form and collision behavior across hidden realms.
3. EU availability (open bug as of Nov 6); we target US only this iteration.
4. Whether the version-pinned static namespace changes on game patches — sync must re-discover it, never hardcode.
5. Data freshness: character data updates on logout only — surface that in the modal wording.

## Deferred (explicitly out of scope)

- Battle.net OAuth login / protected-character data (design hook: redirect URI already registered; a future `profile-forever` user-token phase).
- Any runtime proxy/Worker; any additional hosting.
- Changes to `characters.json`, the tracker, or existing localStorage keys.
