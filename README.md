# wow-forever-roster
Flowmasters wow forever roster site
https://combatwombat16.github.io/wow-forever-roster/

A static guild roster for WoW: Forever: one page, no framework and no bundler. It
lists the guild's characters with their track, role, race/class and professions, a
5-man party simulator, a profession matrix and race/class distribution bars — and,
where a character has opted in on Battle.net, its level, item level, guild, gear and
sync freshness. Everything is plain HTML, ES modules and Tailwind utility classes;
GitHub Pages serves the repository's static files.

## Layout at a glance

| Path | What it is |
|---|---|
| `index.html` | The whole page: markup shell plus one `<script type="module" src="js/main.js">`. No inline app code. |
| `js/` | The app as ES modules. `main.js` bootstraps; `state.js` holds the single source of truth (characters + track filter + search query) and the `renderAll()` dispatcher; `data.js` loads the JSON files and joins them by roster id; `github.js` owns the PAT settings, the Contents API pull/commit and the refresh dispatch; `roster.js` paints the grid (status dot, Lv/iLvl/guild chips, lifecycle badge); `detail.js` is the character modal plus the review actions; `guildfilter.js` owns the guild chip row, the include-a-guild input and their handlers (it holds no state — the buckets and the scope live in `state.js`); `search.js`, `editchar.js`, `party.js`, `professions.js`, `stats.js` are the remaining panels; `util.js` holds the single HTML escaper and the class-colour maps. |
| `css/` | `app.css` is hand-written (borders, fonts, gradients). `tailwind.css` is **generated** by the deploy workflow from `tailwind.config.js`; a compiled copy is committed so local preview needs no build. Do not hand-edit it. |
| `data/` | `guild-config.json` (the one hand-maintained setting: which guild this roster tracks), `characters.json` (the roster, schema v2), `character-data.json` (Battle.net display data incl. gear tooltip fields, combat stats, talents, professions, reputations, avatar/class icons), `battle-net-map.json` (roster id → Battle.net resolution), `talent-trees.json` (spec talent-tree geometry, pinned to the static namespace). `guild-config.json` is the one file a human owns and the sync never writes. All sidecars are optional: any missing or unreadable file just means less decoration, never a broken page. `class-icons.json` and `profession-icons.json` (static Blizzard icon maps, pinned to the static namespace). |
| `tools/` | The Python side: `bnet/` is the sync + probe package (stdlib only, run as `python3 -m bnet sync`), `tests/` its offline unit tests, `migrate_v2.py` the one-shot roster migration. |
| `.github/workflows/` | `sync-bnet.yml` (nightly + manual Battle.net sync — the only workflow holding credentials) and `pages.yml` (Tailwind build + Pages deploy — zero secrets). |
| `.pi/scripts/` | Deterministic verification scripts (see below). |
| `docs/` | `bnet-api-notes.md` (the probed Battle.net contract), `refactor-design.md` (the design as built, with implementation deltas), `bnet-probe-raw.json` (captured probe payloads). |

All page-internal paths are relative, because the site is served under a subpath.

## Preview locally

The page loads ES modules and `fetch()`es `data/*.json`, both of which a `file://` URL
blocks with a cross-origin error. Serve the repository root over HTTP instead:

```sh
python3 -m http.server 8000     # then open http://localhost:8000/
```

No build step is needed: the committed `css/tailwind.css` is already compiled. If you
change markup, classes or `tailwind.config.js` and want the stylesheet regenerated
locally, run the same command the deploy runs:

```sh
npx --yes tailwindcss@3.4.19 -c tailwind.config.js -o css/tailwind.css --minify
```

Without a `data/character-data.json` or `battle-net-map.json`, the roster renders exactly
as it did before the Battle.net integration.

## How the data flows

1. **Roster (`data/characters.json`).** The external WoW: Forever tracker used to commit a
   root-level `characters.json`; that path is retired — the root file is gone and the last
   tracker commit to it predates the `data/` migration. It has three writers. A human edits
   it by hand in this repository, or from the site itself, which commits through the
   Contents API with the maintainer's own PAT. And since Phase C the nightly sync may append
   rows to it: for every guild member Battle.net will show us that no roster row is known
   for, the run appends one placeholder row — a fresh 13-digit id, the member's
   `"<name> <surname>"`, `player` set to the member's name as a stand-in alias, `track:
   "alts"`, `guild` set to the guild's name, `"None"` in the role and profession columns,
   and `validation.state: "pending"` to mark it as unreviewed. Since the guild-scope change the
   run appends from **one** guild only: `data/guild-config.json`'s `includedGuild` (see §1a), so a
   member of some other guild stops arriving as a new row. Resolution and enrichment are not
   scoped — a character already on the roster is still resolved and refreshed whoever guilds them,
   because the scope decides who may arrive and the UI decides what is listed; the nightly never
   removes a row. **Appending is all it may
   do:** before it publishes anything the run re-checks every row it was given against the
   snapshot it took of the roster when the run started (`copy.deepcopy` in `bnet.sync`, so an
   edit made by the run itself is caught too), and a run that would remove, reorder or
   rewrite a row writes nothing at all, roster or artifacts. The independent witness is a
   separate step, not part of `bnet`: the `verify_sync_artifacts` gate
   (`.pi/scripts/verify_sync_artifacts.py`, run in CI) compares the rows on disk with the
   roster committed at `git HEAD`. Curation afterwards is manual, through the site's
   edit UI, exactly like any other draft row.

   Stated plainly, because it is a limit rather than a bug: only characters whose owners
   have opted in to the public API are in a guild roster at all, so "every guild member"
   means every **visible** one. A member who has opted out can never be auto-added and has
   to be added by hand — the nightly cannot see them to know they are missing. The guild
   roster is also cached upstream for three hours, so a character who joined or left today
   may not appear until tomorrow's run.

   The ten original fields (`id, player, name, track, role, roleSec, race, class, prof1,
   prof2`) are unchanged, `guild` is the one optional addition (an auto-added row carries
   the guild name there so the guild filter can place a character that has no sidecar row
   yet), and everything is wrapped in a `{ "schemaVersion": 2, "characters": [ … ] }`
   envelope.

1a. **Guild scope (`data/guild-config.json`).** `{ "schemaVersion": 1, "includedGuild":
   "Flowmasters" }` — the roster keeps one guild, so the site and the nightly both ask that file
   instead of every surface deciding for itself.

   * **The site** (`js/state.js` `getIncludedGuild()`): a row is in the roster at all only when its
     bucket is the included guild or a no-guild bucket. Every other guild's characters are absent
     from the grid, the party pool and both distribution panels — they all read
     `getFilteredCharacters()`, so there is no panel that can forget the rule. The guild chips then
     offer exactly **All**, the included guild and **Guildless** (`Unknown` — no sidecar row to ask
     — folds into `Guildless`, because "never looked up" is not a third guild and 16 hand-added
     rows have to stay reachable under something). Names compare trimmed, case-folded and with
     punctuation gone, so `"flowmasters"`, `"Flowmasters"` and the slug `flow-masters` all select the
     same guild: that rule is not a per-reader convention, it is
     [`tools/tests/fixtures/guild-name-cases.json`](tools/tests/fixtures/guild-name-cases.json), which
     pins the nightly's `bnet.sync.same_guild` and the site's `state.guildNameEquals` to one table —
     where they differ, the sync adds members the browser then never lists.
   * **The filter row** (bottom of the roster aside) can *input* a guild as well as pick one: the
     text field datalists every guild the roster knows — including the hidden ones, which is how a
     guild comes back — and **Set** / Enter applies it for this browser only, in localStorage under
     `wow_forever_guild_scope` (an empty field is an answer: it unscopes). The ↺ button drops the
     override and takes the committed default back. Nothing about this is a commit: an experiment
     with a guild cannot become a write to `data/guild-config.json`.
   * **The nightly** (`bnet.config.included_guild()`, applied in `Sync.discover_guilds`) reads the
     same file and discovers only that guild, so an excluded guild's roster is never fetched and
     never planned. The run summary then states the scope and names the guilds it passed over,
     because "0 rows added" has more than one cause.
   * A missing, unreadable or blank file means **unscoped**, which is the pre-change behaviour: a
     setting nobody configured must not hide rows.
2. **Battle.net sidecars (`data/character-data.json`, `data/battle-net-map.json`).** Written
   only by the sync pipeline, never by the browser: a scheduled run each night, plus
   on-demand refreshes a maintainer dispatches from the character modal. The sync mints a
   Battle.net token inside the Actions job from repository secrets, resolves each roster id,
   normalizes what it finds (no raw Blizzard payloads are committed), runs the artifact
   verification gate, and only then commits both files. Deploy follows the commit.
3. **The browser reads all three files as plain same-origin `fetch()`es** and joins them by
   roster id. Battle.net data is display-only in every state; user-authored fields are
   editable only while a row is a draft, and validating a row locks it until it is explicitly
   re-opened. The site never calls the Blizzard API and never holds a Blizzard credential.

## Your GitHub PAT

Anything read-only needs no token. Committing from the browser and refreshing data do:

- **Contents: read & write** — pull the roster from your branch, and commit roster edits and
  review actions (confirm a match, pick a candidate, report a mismatch, re-open a row).
- **Actions: read and write** — the "Refresh from Battle.net" button, which starts the sync
  workflow for one character and waits for it to finish.

Paste the token into the in-page settings; it is stored only in that browser's localStorage
and is sent only to GitHub. Use a fine-grained token scoped to this repository, and give it
no more permissions than the two lines above. Never commit a token, and never put one in a
file in this repository. The Battle.net client credentials live exclusively in GitHub Actions
secrets (`BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`) and are readable only by the sync
workflow on scheduled or manual runs of the default branch.

Trust boundary, stated plainly: anyone who can commit to this repository can add a workflow
that reads those secrets, and anyone with a `contents: write` PAT can commit a roster
validation block (including pinning a Battle.net slug) — sync deliberately trusts what a
human validated, with git history as the audit trail. Acceptable for a small private-team
repo; reconsider before widening access.

## Verification

Deterministic, offline, and safe to run anywhere:

```sh
python3 .pi/scripts/verify_sync_artifacts.py     # artifact schemas, hosts, secret scan, workflow shape
node    .pi/scripts/verify_stats.mjs             # module structure, ids, handlers, escaping, rendered stats
node    .pi/scripts/verify_writes.mjs            # browser write paths under a DOM stub
node    .pi/scripts/verify_search_detail.mjs     # search + detail modal behaviour under a DOM stub
python3 .pi/scripts/verify_bnet_probe.py         # the probed API contract still matches the notes
python3 -m unittest discover -s tools -p "test_*" # sync package unit tests
```

Task-by-task status, phases and open items live in `armory-tasks.md`.
