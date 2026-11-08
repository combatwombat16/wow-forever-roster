# Refactor Design — Pipeline ↔ Data ↔ Modular UI (rev 1, for sign-off)

Status: DRAFT awaiting approval. Nothing here is implemented yet.

## 0. Goals

1. Single coherent pipeline: `tools/*.py` → `data/*.json` → modular static UI on GitHub Pages.
2. Character lifecycle: **draft → sync discovers → user validates → read-only**.
3. On-demand Battle.net refresh from the browser via PAT-triggered `workflow_dispatch` (secrets never client-side).
4. Deployment: GitHub Actions Pages build (Tailwind generated at deploy time), still native Pages.

---

## 1. Target repository layout

```
index.html                  # thin shell: markup skeleton + module entry
css/
  app.css                   # custom classes (wow-border, cinzel, gradients) — committed
  tailwind.css              # GENERATED at deploy (gitignored)
js/
  main.js                   # bootstrap: fetch data/, wire state, first render
  state.js                  # characters + filters + search = single source of truth; render dispatcher
  data.js                   # sidecar loading (character-data, battle-net-map), join by id, staleness
  github.js                 # PAT settings, Contents API pull/push, workflow_dispatch + commit polling
  roster.js                 # roster grid, badges, status dots
  review.js                 # validation modal: candidates, confirm, claim-slug, report-not-found
  editchar.js               # create/edit form (draft-state characters only)
  party.js                  # 5-man party simulator
  professions.js            # profession matrix
  stats.js                  # race + class distribution bars
  util.js                   # HTML escaping (single shared helper), formatting
tailwind.config.js          # content: index.html + js/**, theme extension (class colors etc.)
data/
  characters.json           # canonical roster (schema v2, see §2)
  character-data.json       # Blizzard-sourced display data (schema v2, see §3)
  battle-net-map.json       # roster-id → Battle.net resolution incl. candidates (schema v2, see §3)
tools/
  bnet/                     # sync/probe modularized (see §5)
  migrate_v2.py             # one-shot: root JSONs → data/, schema v1 → v2
docs/ …                     # unchanged + amended notes
armory-tasks.md             # amended to record constraint changes (§7)
```

- All page-internal paths **relative** (site serves under `/wow-forever-roster/`). No root-absolute URLs.
- `characters.json` fallback chain simplifies: fetch `data/characters.json`; **`DEFAULT_CHARACTERS` and the `wow_forever_roster` localStorage key are removed** (drafts live in memory until pushed; the PAT settings modal keeps its own existing storage).

---

## 2. `data/characters.json` — schema v2

Top level becomes an **object** (was: bare array):

```json
{
  "schemaVersion": 2,
  "characters": [ { …10 original fields…, "validation": { … } } ]
}
```

Original per-character fields (`id, player, name, track, role, roleSec, race, class, prof1, prof2`) are **unchanged**. New optional block:

```json
"validation": {
  "state": "validated",            // only stored value today; absence == "draft"
  "confirmedSlug": "shiftybeef-stroganoff",  // user's chosen/reconfirmed slug (optional; sync's pick if absent)
  "reviewedBy": "<github-login>",
  "reviewedAt": "2026-11-08T05:05:48Z"
}
```

### Lifecycle state machine (UI-derived, mostly NOT stored)

| Stored `validation` | Map status (from `battle-net-map.json`) | Derived UI state | Editable? | Badge |
|---|---|---|---|---|
| absent | no entry | `awaiting_sync` | ✅ draft | grey "unvalidated — awaiting sync" |
| absent | `ok` | `awaiting_confirmation` | ✅ draft (bnet preview shown read-only) | amber "confirm Battle.net match" |
| absent | `ambiguous` | `needs_resolution` | ✅ draft | amber "pick candidate" |
| absent | `not_found` | `not_found` | ✅ draft | red "no Battle.net match" |
| absent | `fetch_error` | `fetch_error` | ✅ draft | red "sync error" |
| `validated` | any | `validated` | ❌ read-only | green "validated by X" |

Rules:
- **User-authored fields editable only while draft.** Once `validated`, the row locks; re-opening requires an explicit "re-open for editing" action (new commit, audit trail via git).
- Battle.net-sourced data (level/gear/progress/…) is **display-only in every state**.
- "Not my character" in review modal → user edits the name or presses "report mismatch" → a commit appends `note` (free text) and sets map review via the sync tool's `needs_review` on next run; the char stays draft.
- `confirmedSlug` (set during review) is authoritative: sync never re-resolves a character that has one unless `--force` + user has re-opened it.

## 3. Sidecar schemas v2 (sync outputs)

`data/battle-net-map.json` (schemaVersion 2): same entry shape **plus**
```json
"candidates": [ { "slug": "…", "name": "…", "surname": "…", "matchedForm": "…", "href": "…" } ]
```
- `status` gains **`ambiguous`** (multiple plausible resolutions; `candidates` populated, `slug: null`). Auto-pick behavior is removed: previously-ok auto-picks become `ambiguous` unless exactly one hit.
- Entries whose roster `validation.state == "validated"` or that carry `confirmedSlug`: sync trusts the slug (re-queries data, never re-resolves the name).

`data/character-data.json` (schemaVersion 2): shape unchanged; adds top-level
`"triggeredBy": "schedule" | "<github-login>"` (from `github.actor`) for provenance, and per-character `status` semantics unchanged.

UI joins the three files by roster `id` at load; each file independently optional with graceful degradation (sidecar missing → site renders exactly as the pre-refactor behavior for that panel).

## 4. Workflows

```
sync-bnet.yml  (modified)   cron 06:00 UTC + workflow_dispatch{lookup_name, force}
  → python3 -m bnet sync (new module entrypoint) → verify_sync_artifacts.py → commit data/
  → commit message keeps [skip ci]  (deploy handled by workflow_run below)

pages.yml  (new)            on: push(main) + workflow_run(sync-bnet: completed, main) + workflow_dispatch
  → setup node → npx tailwindcss -c tailwind.config.js -o css/tailwind.css --minify
  → upload static bundle → actions/deploy-pages
```
- Repo settings one-time flip: Pages `build_type: legacy → workflow` (done via `gh api` at phase ④, with your OK in the moment).
- `workflow_run` chain guarantees nightly data is redeployed (the artifact-snapshot staleness trap).
- Security posture of sync-bnet.yml unchanged: zero PR triggers, `contents: write`, bot identity, redaction, host allowlist, no-redirect, mass-flip floor.

**On-demand refresh (browser):** `POST /repos/{owner}/{repo}/actions/workflows/sync-bnet.yml/dispatches` with the user's PAT and `inputs.lookup_name = <character>`, `client_payload.actor = <login>`; UI then polls `…/actions/runs?created=>` until completion and refetches `data/` (cache-busted). PAT scope additions documented: `actions: write` (existing flows keep `contents: write`).

## 5. Python modularization

`tools/bnet/` package (stdlib-only preserved, security invariants carried over verbatim):
```
tools/bnet/__init__.py
  config.py       # URLs, namespaces, budgets, paths (data/ lives here, nothing else hardcodes paths)
  http.py         # token flow, redactor, allowlist, no-redirect handler, conditional requests
  roster.py       # characters.json v1/v2 read; validation-state awareness helpers
  resolve.py      # name→slug; candidate enumeration; ambiguous verdicts; confirmedSlug trust
  fetch.py        # profile/gear/progress fetching, href/iconUrl validators (H2)
  artifacts.py    # map/data JSON writers (schemas of record), mass-flip floor (H4)
  sync.py         # Sync orchestrator — CLI: python3 -m bnet sync [--lookup NAME] [--force] [--dry-run]
  probe.py        # probe orchestrator — CLI: python3 -m bnet probe
tests/ …          # optional pytest for pure functions (resolve, artifacts) — nice-to-have
```
Behavior changes (lifecycle-aware):
- reads v1 array OR v2 object roster transparently (migration script flips the repo);
- skips name-resolution for `validated`/`confirmedSlug` entries (still refreshes their character data);
- emits `candidates` + `ambiguous` instead of auto-picking;
- records `triggeredBy`.

`verify_bnet_probe.py` gets path updates only; probe itself behavior-unchanged.

## 6. Verification (phase ⑤)

- `verify_stats.mjs` → multi-file: parse all `js/*.js` (no dynamic `innerHTML` of unescaped sidecar strings; inline `on*` handlers resolve to functions exposed on `window` from modules — modules assign `window.fn = fn` deliberately, so the old invariant is kept in a new place).
- `verify_sync_artifacts.py` → `data/` paths, v2 schemas (exact key sets incl. `candidates`, `triggeredBy`), ambiguity rule, confirmedSlug trust rule.
- new `verify_armory.mjs` (task 4.1): three-file parse+schema cross-check (map covers roster ids exactly; data keys ⊆ map ok/ambiguous ids), escaper coverage scan, secret-pattern `rg` scan, `characters.json` v2 round-trip (v1 backup byte-identical check during migration commit).
- reviewer subagent pass (4.2) over the whole diff surface: tools + workflows + github.js.
- E2E (4.3): dispatch refresh via UI on one char, verify commit + redeploy + badge state transitions incl. needs_resolution.

## 7. armory-tasks.md amendments to record

- Phase 3 header: single-file constraint lifted → "modular static, no CI build step except Pages-deploy Tailwind" (your decision).
- New Phase 3bis: validation lifecycle (badges per §2, review modal, on-demand dispatch).
- "Deferred" list: OAuth stays deferred; note that on-demand sync supersedes the interim "nightly only" UX.
- Trust-boundary note extended: PATs now also carry `actions: write`.

## 8. Migration & rollback

- Phase ② lands one migration commit: `git mv` JSONs → `data/`, v1→v2 roster conversion (`tools/migrate_v2.py`, idempotent), workflow paths updated in lockstep. Old root files removed in the same commit.
- Loader tolerates v1 array shape during the transition window only; verify script forbids v1 after the migration commit.
- Rollback story: revert the commit; Pages Actions deploy makes rollback = redeploy previous artifact.

## 9. Open decisions needing your explicit sign-off

1. **`characters.json` becomes an object** `{schemaVersion, characters:[…]}` instead of a bare array (breaking for any forgotten consumer; only sync tool + UI consume it today). Agree?
2. **Stored states minimal**: only `validated` is ever stored; everything else derived from map status (§2 table). Alternative: store every state explicitly (more audit, more drift risk). Agree with derived?
3. **Auto-picks demoted to `ambiguous`**: existing map entries that were auto-resolved with one hit stay `ok`, but any future multi-hit is never auto-picked. Existing 4 `ok` entries therefore enter `awaiting_confirmation` on first load after refactor. Agree?
