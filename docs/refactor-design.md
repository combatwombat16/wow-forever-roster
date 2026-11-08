# Refactor Design — Pipeline ↔ Data ↔ Modular UI (rev 2 — as built)

Status: **IMPLEMENTED (rev 2)**, 2026-11-08. Rev 1 was signed off in `cab0c06` and landed as
`1280185` (Phase 2: `tools/bnet` package + `data/` migration) → `e7e53f6` (3a: ES modules) → `404c70f` (3b:
sidecar layer) → `8e593c7` (3c: search + detail modal) → `c3815b7` (3d: review write paths + on-demand
refresh) → `b378b29` (Phase 4: Pages via Actions build). Sections below describe the design; **where the
implementation differs, the divergence is recorded in §10 and is authoritative.** Rev 2 changed no decision —
it records what was built, plus one design correction in §4 (client-supplied provenance is not honoured).

Unverified by the tree, and therefore still open: the manual end-to-end run (armory-tasks 4.3) — all of
`1280185..b378b29` is unpushed, so neither workflow has executed on Actions and the Pages `build_type` flip
is unconfirmed.

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
- `confirmedSlug` (set during review) is authoritative: sync never re-resolves a character that has one. **Re-resolution of a user-confirmed slug is NOT implemented** — the `--force` + "user has re-opened it" escape hatch drafted here does not exist in the code, and there is no plan to add it to the sync: `--force` only bypasses the HTTP `Last-Modified` cache, never the trust rule. This is stricter than drafted; a re-resolve may be added later behind an explicit UI action (a review-modal button that rewrites `confirmedSlug` in `characters.json`), never as a sync flag.

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
- ~~Repo settings one-time flip: Pages `build_type: legacy → workflow` (done via `gh api` at phase ④, with your OK in the moment).~~ **Still an operator action, not a repo change.** `pages.yml` is committed and is the
  deploy path; the flip itself is a repository setting that cannot be observed from the tree, so no commit here
  claims it happened. Until it is flipped, Pages keeps serving `main` directly and `pages.yml` never runs —
  which is also why `css/tailwind.css` is committed (§10). `pages.yml` declares `workflow_dispatch` precisely so
  a manual redeploy is one click after the flip.
- `workflow_run` chain guarantees nightly data is redeployed (the artifact-snapshot staleness trap).
- Security posture of sync-bnet.yml unchanged: zero PR triggers, `contents: write`, bot identity, redaction, host allowlist, no-redirect, mass-flip floor.

**On-demand refresh (browser):** `POST /repos/{owner}/{repo}/actions/workflows/sync-bnet.yml/dispatches` with the user's PAT and `inputs.lookup_name = <character>`, ~~`client_payload.actor = <login>`~~; UI then polls `…/actions/runs?created=>` until completion and refetches `data/` (cache-busted). PAT scope additions documented: `actions: write` (existing flows keep `contents: write`).

> **AMENDED 2026-11-08 — the `client_payload` actor is NOT honoured, and cannot be.**
> `client_payload` is a **`repository_dispatch`** field; a `workflow_dispatch` run does not read it at all, so the
> drafted `client_payload.actor` would simply have been ignored by `sync-bnet.yml`. The implementation therefore
> takes provenance from the runner environment alone: `tools/bnet/artifacts.py::triggered_by()` reads
> `GITHUB_EVENT_NAME` first (so a cron run records `schedule` even though GitHub also sets `GITHUB_ACTOR`) and
> otherwise `GITHUB_ACTOR`, falling back to `manual` at a shell. **This direction is strictly safer than
> drafted:** `GITHUB_ACTOR` is set by GitHub from the authenticated caller, so a client cannot forge who caused a
> run — a browser-supplied actor string in the recorded `triggeredBy` would have been an untrusted value written
> into a committed artifact. Consequences: `js/github.js::dispatchSyncWorkflow()` still sends
> `client_payload.actor` (inert, kept only so the call shape matches this document and so a future
> `repository_dispatch` variant would have the field), sends exactly the two declared workflow inputs
> (`lookup_name`, `force: 'false'` — a refresh never bypasses the `Last-Modified` cache), and
> `verify_writes.mjs` §8 pins that body shape plus the 403 → "PAT needs Actions: write" guidance and the bounded
> poll. Nothing in `tools/` reads `client_payload`.

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

> Resolved at sign-off (`cab0c06`); all three landed as drafted — see §10 for the details that changed around them.

1. **`characters.json` becomes an object** `{schemaVersion, characters:[…]}` instead of a bare array (breaking for any forgotten consumer; only sync tool + UI consume it today). Agree?
2. **Stored states minimal**: only `validated` is ever stored; everything else derived from map status (§2 table). Alternative: store every state explicitly (more audit, more drift risk). Agree with derived?
3. **Auto-picks demoted to `ambiguous`**: existing map entries that were auto-resolved with one hit stay `ok`, but any future multi-hit is never auto-picked. Existing 4 `ok` entries therefore enter `awaiting_confirmation` on first load after refactor. Agree?

## 10. Implementation deltas (what differs from rev 1 as written)

Checked against the working tree at `263cb99` on 2026-11-08. Nothing here reverses a signed-off decision; each
entry is either a naming/plumbing change or a place where the build is stricter or lazier than drafted.

1. **Roster write path: read-modify-write for review mutations, compare-and-replace for bulk push** — shipped
   in the fix wave (`263cb99`) after the write-path review flagged whole-file overwrite as MAJOR.
   - *Review commits* — `commitValidationOnFresh(message, id, mutateFn)` → one commit core
     (`commitRosterFile`): fresh GET of `data/characters.json`, `setValidationOnFresh` re-applies the mutation
     (set/clear validation block, candidate choice, mismatch note) **onto the freshly fetched document**
     (id must appear exactly once with a usable name, else abort with no PUT), then PUTs that array with that
     sha. Concurrent validations by other reviewers survive; a row deleted or renamed upstream aborts the
     commit instead of resurrecting it; the in-memory roster adopts the rebased result.
   - *Bulk push* — `pushToGitHub()` delegates to the same core with `{compareBaseline: true}`: if the upstream
     content semantically differs from what this browser loaded (baseline captured at load/pull/successful
     commit), the push is refused with "roster changed upstream — pull first" and **nothing** is written
     locally (the old failure-time localStorage write was removed). Byte-different but semantically identical
     upstream content is not treated as drift.
   Mitigations unchanged: sync never commits the roster; every write goes through the v2 envelope
   (`ensureRosterEnvelope()`); failed writes never mutate local state.
2. **`pages.yml` is the deploy path; the "settings flip TBD" of §4 is retired by it** rather than by the flip.
   `push(main)` + `workflow_run(sync-bnet: completed, main)` + `workflow_dispatch`, zero secrets, least
   privilege per job, a size gate that refuses a degenerate stylesheet, an allowlisted `_site` bundle (repo
   docs, tools and `.pi/` are deliberately not published), and a documented decision **not** to deploy the
   triggering `head_sha` — `workflow_run` checks out the default-branch head, which already contains the sync's
   commit, so pinning the triggering sha would publish a stale UI over fresh data.
3. **Tailwind is pinned exactly: `tailwindcss@3.4.19`.** No `package.json`/lockfile exists, so `npx --yes`
   fetches the CLI; the exact pin is what makes the committed stub and the deployed stylesheet the product of
   the same compiler. It is 3.4.x because `tailwind.config.js` is v3-style CommonJS — the v4 CLI would need
   CSS-first config and would emit nothing.
4. **`css/tailwind.css` is committed, contradicting §1's "GENERATED at deploy (gitignored)".** It is still
   generated at deploy (and the deploy overwrites it), but the generated file is tracked on purpose: `index.html`
   no longer loads the Play CDN, so a committed copy is what makes `python3 -m http.server` render without a
   build step. Its header comment says it is generated and must not be hand-edited;
   `verify_sync_artifacts.py` checks the workflow's build → size-gate → stage → upload → deploy order.
5. **No `js/review.js`.** The §1 module list named one; the validation lifecycle shipped inside
   `js/detail.js` (review block, candidates, `confirmMatch` / `selectCandidate` / `confirmSelectedCandidate` /
   `reportMismatch` / `reopenForEditing` / `refreshFromBattleNet`), because those actions are only reachable from
   the modal that renders their state. `js/detail.js` is therefore larger than the design implied and
   `js/github.js` is the sole owner of transport.
6. **`DEFAULT_CHARACTERS` and the `wow_forever_roster` key were not removed** (§1 proposed removing both).
   They survive with a demoted role: localStorage first, `DEFAULT_CHARACTERS` on empty/parse failure, then
   `data/characters.json`, then the Contents pull. That keeps the standing rule "the page must fully work with no
   network" true without a build step. The sidecars are still never persisted, and no third localStorage key
   exists (`verify_search_detail.mjs` §6).
7. **No `verify_armory.mjs`** (§6). The 4.1 coverage was split per surface instead, so a failure names the
   surface: `verify_sync_artifacts.py` (486), `verify_stats.mjs` (197), `verify_writes.mjs` (220, new in
   `c3815b7`), `verify_search_detail.mjs` (97, new in `8e593c7`), `verify_bnet_probe.py` (42) and 44 unit tests
   in `tools/tests/`. `verify_sync_artifacts.py` additionally absorbed the `pages.yml` shape checks
   (secret-free, permissions, Tailwind build, official Pages actions, allowlisted bundle,
   build → size-gate → stage → upload → deploy order); the two-workflows-only allowlist for
   `.github/workflows/` lives in `verify_bnet_probe.py`.
8. **v1 tolerance is loader-side only.** §8 expected the loader to tolerate a v1 array during the transition and
   the verifier to forbid it afterwards: `rosterCharactersFrom()` still accepts either shape (harmless, and it
   is what lets a stale browser cache render), while `verify_sync_artifacts.py` pins the committed file to the
   exact v2 key set and fails if any root-level copy of the three JSON files reappears in the worktree.
   `tools/migrate_v2.py` is byte-comparing and idempotent, so its second run rewrites nothing.
9. **The rest of §2, §3 and §5 landed as named:** the `tools/bnet/` module split
   (`config/http/roster/resolve/fetch/artifacts/sync/probe` + tests), the CLIs
   (`python3 -m bnet sync [--lookup NAME] [--force] [--dry-run] [--state-dir DIR]`, `python3 -m bnet probe
   [extra_name …]`), the `candidates`/`ambiguous` map rules, per-character `validation` blocks, and the
   `triggeredBy` provenance key — with the §4 amendment above about where that value comes from. The
   re-resolve rule recorded in §2 is implemented exactly as amended: `--force` bypasses the HTTP cache and never
   the confirmed-slug trust rule.
