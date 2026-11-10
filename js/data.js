// data.js — the same-origin data/ loads and the join across them.
//
//   data/characters.json      the roster (v1 bare array or v2 envelope), authoritative
//                             for user-authored fields, mirrored into localStorage.
//   data/character-data.json  Blizzard-sourced display data, keyed by roster id.
//   data/battle-net-map.json  roster id → Battle.net resolution (status, slug, …).
//
// The two sidecars are display-only: they are held in module scope, joined to the
// roster by id (byRosterId), and are NEVER written to localStorage — a stale sidecar
// in storage would outlive the nightly sync it is supposed to reflect. Each loads
// independently and degrades to null (design §3), so a missing or broken sidecar
// leaves the panels that need it showing nothing extra and the site behaving as it
// did before they existed.

import { STORAGE_KEY, getCharacters, normalizeRoster, registerRenderHook, registerSearchProvider, renderAll, setCharacters, setGuildScopeDefault } from './state.js';
import { ownValue } from './util.js';
import { escapeHtml } from './util.js';

export const ROSTER_PATH = 'data/characters.json';
export const CHARACTER_DATA_PATH = 'data/character-data.json';
export const BATTLE_NET_MAP_PATH = 'data/battle-net-map.json';
// The one hand-maintained setting the site reads: which guild this roster is a roster OF. Like the
// sidecars it is display-side configuration that never enters localStorage (a copy in storage would
// outlive the commit that changed it), and like them a 404 degrades to "no value": an absent file
// means an unscoped roster, not a broken one.
export const GUILD_CONFIG_PATH = 'data/guild-config.json';
// Talent-tree geometry is static-namespace data (the file pins its own staticNamespace), not
// per-character data, so it is fetched lazily on the first Talents tab instead of on every page
// view: it is the largest committed artifact (~200 KB) and only the handful of characters that
// actually have a talent selection ever read it.
export const TALENT_TREES_PATH = 'data/talent-trees.json';

// The sync runs once a day; anything fetched longer ago than this is shown stale.
export const SIDECAR_FRESH_HOURS = 36;

// The sidecars as parsed, or null when absent / unreadable / not an object.
let characterData = null;
let battleNetMap = null;

// The v2 envelope as parsed, or null when the roster arrived as a bare v1 array.
let fetchedWrapper = null;

// The roster this browser is entitled to write back over: the characters array of the file it
// last read from (or committed to) the repository, as a JSON string. github.js compares the
// branch content against it before a whole-file PUT, which is what turns a bulk edit into a
// compare-and-replace instead of a blind overwrite of rows this page never saw. It lives here
// next to `fetchedWrapper` because both are "what the repository handed us", and github.js
// already depends on this module (the other direction would be a cycle).
let baselineRoster = null;

export function getBaselineRoster() {
  return baselineRoster;
}

// null clears the baseline, which is how the write paths say "this browser has no idea what is
// on the branch" — and a whole-file push without one is refused rather than guessed at.
export function setBaselineRoster(list) {
  baselineRoster = Array.isArray(list) ? JSON.stringify(list) : null;
}

// The single adoption entry point: call it whenever a characters array becomes "what this browser
// believes the branch holds", so that there is exactly one canonical baseline — the parsed characters
// array last adopted from upstream, in the representation the branch itself used — and exactly one
// place that writes it. The three readers go through it:
//
//   * loadRoster()               the deployed copy of data/characters.json (a page load)
//   * pullFromGitHub()           the Contents API read the settings form / Pull performs
//   * commitRosterFile() success the list this browser just committed, rebased included
//
// A list this browser invented must NOT come through here: pushToGitHub()'s local-only and no-PAT
// paths hold the edit in state and localStorage without the branch ever agreeing to it, and a
// baseline the branch never held turns the next legitimate push into a phantom "changed upstream".
export function adoptUpstreamRoster(list) {
  setBaselineRoster(list);
  return Array.isArray(list) ? list : null;
}

export function setFetchedWrapper(doc) {
  fetchedWrapper = Array.isArray(doc) ? null : doc;
}

// The roster on main is the v2 envelope, so every commit this site makes must keep it that
// way. A browser that never completed the fetch (offline start, or a roster read straight
// out of localStorage) has no wrapper to copy, and serializeRoster would then emit the bare
// v1 array the migration removed. Declare the envelope instead of downgrading the file.
export function ensureRosterEnvelope() {
  if (!fetchedWrapper || typeof fetchedWrapper !== 'object') {
    fetchedWrapper = { schemaVersion: 2 };
  }
  return fetchedWrapper;
}

// v1 array or v2 envelope → the characters list, or null when neither is usable.
export function rosterCharactersFrom(doc) {
  const list = Array.isArray(doc) ? doc : (doc && doc.characters);
  return Array.isArray(list) && list.length > 0 ? list : null;
}

// Serialise a roster in the shape the file was fetched in: keep every key of the v2
// envelope and swap its characters, or emit the bare v1 array as before.
export function serializeRoster(newCharacters) {
  if (fetchedWrapper && typeof fetchedWrapper === 'object') {
    return JSON.stringify({ ...fetchedWrapper, characters: newCharacters }, null, 2);
  }
  return JSON.stringify(newCharacters, null, 2);
}

// ---------- sidecar accessors ----------

// The keyed collection of a sidecar document, or null when the file/shape is unusable.
function sidecarCollection(doc, key) {
  const value = doc && typeof doc === 'object' ? doc[key] : null;
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

// Own-data lookup: the collection is a parsed sidecar and the id comes from the roster, so
// neither may resolve a member of Object.prototype into the join (see util.js ownValue).
function sidecarEntry(collection, id) {
  if (!collection || id == null) return null;
  const entry = ownValue(collection, id);
  return entry && typeof entry === 'object' && !Array.isArray(entry) ? entry : null;
}

// The three-file join at the point of use: either half is null when that file is
// missing, the id is absent from it, or its entry is malformed. Callers must degrade.
export function byRosterId(id) {
  return {
    sidecar: sidecarEntry(sidecarCollection(characterData, 'characters'), id),
    mapEntry: sidecarEntry(sidecarCollection(battleNetMap, 'entries'), id)
  };
}

// Age of a sidecar row in hours, or null when fetchedAt is missing/unparseable.
export function sidecarAgeHours(sidecar, now = Date.now()) {
  const fetchedAt = sidecar && typeof sidecar === 'object' ? sidecar.fetchedAt : null;
  const stamp = Date.parse(fetchedAt);
  if (!Number.isFinite(stamp)) return null;
  return (now - stamp) / 3600000;
}

// Fresh when the row was fetched less than SIDECAR_FRESH_HOURS ago; an unparseable or
// absent fetchedAt is not fresh (a row we cannot date is not a row we can trust).
export function isSidecarFresh(sidecar, now = Date.now()) {
  const age = sidecarAgeHours(sidecar, now);
  return age !== null && age < SIDECAR_FRESH_HOURS;
}

// ---------- talent trees (lazy, one request, never persisted) ----------

// Like the other sidecars this lives in module scope only: a tree file in localStorage would
// outlive the static namespace it was generated against. `talentTreesLoad` memoises the request
// so twenty tab switches are one fetch, and a failed load clears it so the next open retries
// (a transient 404 must not strand the Talents tab on its degrade path for the session).
let talentTrees = null;
let talentTreesLoad = null;

export function talentTreesDoc() {
  return talentTrees;
}

// True while the first request is still in flight, which is what the Talents tab renders
// instead of a tree ("loading", not "no data" — the two read very differently).
export function talentTreesLoading() {
  return talentTrees === null && talentTreesLoad !== null;
}

export function ensureTalentTrees() {
  if (talentTrees) return Promise.resolve(talentTrees);
  if (talentTreesLoad) return talentTreesLoad;
  talentTreesLoad = loadSidecar(TALENT_TREES_PATH).then(doc => {
    talentTrees = doc && typeof doc === 'object' && !Array.isArray(doc) ? doc : null;
    if (talentTrees === null) talentTreesLoad = null;
    else renderAll();
    return talentTrees;
  });
  return talentTreesLoad;
}

// The static namespace the committed geometry was built against, for the modal's provenance
// line. '' until the file has loaded (the panel then says nothing rather than guessing).
export function talentTreesStaticNamespace() {
  return talentTrees && typeof talentTrees === 'object' ? scalarOwnText(talentTrees, 'staticNamespace') : '';
}

function scalarOwnText(source, key) {
  const value = ownValue(source, key);
  return typeof value === 'string' ? value.trim() : '';
}

// spec id → that spec's tree, or null. Own-data lookup: both the file and the spec id are
// committed/response data, so `specs['constructor']` must not hand back Object.prototype.
export function talentTreeFor(specId) {
  const specs = talentTrees && typeof talentTrees === 'object' ? talentTrees.specs : null;
  if (!specs || typeof specs !== 'object') return null;
  const tree = ownValue(specs, specId);
  return tree && typeof tree === 'object' && !Array.isArray(tree) ? tree : null;
}

// ---------- site icon maps (P5.C): class-icons.json / profession-icons.json ----------

// Static-namespace lookup tables the sync commits (each file pins its own staticNamespace, the
// way talent-trees.json does), shaped:
//
//   {"staticNamespace": "…", "updatedAt": "…", "icons": {"<Name>": {"id": <int>, "iconUrl": "https://render…"}}}
//
// They load lazily on the first panel that paints one and are memoised per path, so the class
// distribution, the party slots and any later reader share ONE request per map per page — the
// ensureTalentTrees() discipline: the success memo makes twenty repaints one fetch, and a failed
// load clears the memo so the next repaint retries (a transient 404 must not strand every icon
// panel for the session). They are deliberately absent from loadSidecars()/reloadSidecars(): a
// daily sync rewrites them byte-identically unless the static namespace moves, and a namespace
// move arrives with a new deploy. Module scope only — a map in localStorage would outlive the
// namespace it was generated against, exactly like the trees.
export const CLASS_ICONS_PATH = 'data/class-icons.json';
export const PROFESSION_ICONS_PATH = 'data/profession-icons.json';

const siteIconMaps = new Map();
const siteIconLoads = new Map();

// True once the map has loaded. Renderers gate their icon MARKUP on this: while the file is
// absent (the P5.B artifact has not landed yet, or the fetch failed) the panels draw byte-for-byte
// what they drew before Phase 5 existed — reserved icon slots only enter the layout once there is
// a map that could fill one, so "no file" never shifts a label by a pixel.
export function siteIconMapLoaded(iconPath) {
  return siteIconMaps.has(iconPathKey(iconPath));
}

export function ensureSiteIconMap(iconPath) {
  const key = iconPathKey(iconPath);
  if (key === '') return Promise.resolve(null);
  const loaded = siteIconMaps.get(key);
  if (loaded) return Promise.resolve(loaded);
  const pending = siteIconLoads.get(key);
  if (pending) return pending;
  const load = loadSidecar(key).then(doc => {
    const valid = doc && typeof doc === 'object' && !Array.isArray(doc) ? doc : null;
    // The in-flight memo drops either way; only the success memo persists, so a failure is what
    // the next repaint retries against (no renderAll on failure — nothing changed to repaint).
    siteIconLoads.delete(key);
    if (valid === null) return null;
    siteIconMaps.set(key, valid);
    renderAll();
    return valid;
  });
  siteIconLoads.set(key, load);
  return load;
}

function iconPathKey(iconPath) {
  return iconPath == null ? '' : String(iconPath);
}

// Drop the memoised icon maps and any in-flight loads. This exists for the verification harness
// only: the no-map boot (the graceful-absence layout) happens once per page, and a harness that
// boots against the repo first must be able to observe a fixture directory WITHOUT the artifacts
// as if they had never loaded. App code never calls it — there is no runtime path where a loaded
// static map should be forgotten mid-page.
export function resetSiteIconMaps() {
  siteIconMaps.clear();
  siteIconLoads.clear();
}

// The icon URL recorded for one name, or ''. Own-data lookups end to end: the map is a committed
// artifact and the name is roster/sidecar text, so `icons["constructor"]` must resolve nothing.
// The match is exact first, then case-insensitive over the map's OWN keys (the roster's spelling
// and the artifact's are written by different code paths; the maps hold ~10 entries, so the scan
// is bounded and cannot invent a row the file does not carry).
//
// The URL comes back RAW on purpose. isSafeIconUrl() is the single gate between a committed URL
// and an <img>, it lives in detail.js, and importing that from here would close a module cycle
// (detail.js imports this one) — so every renderer passes this value through the window-published
// check (the roster.js safeSidecarIconUrl precedent) before it reaches markup.
export function siteIconUrl(iconPath, name) {
  const doc = siteIconMaps.get(iconPathKey(iconPath));
  const icons = doc && typeof doc === 'object' ? ownValue(doc, 'icons') : null;
  if (!icons || typeof icons !== 'object' || Array.isArray(icons)) return '';
  const wanted = name == null ? '' : String(name).trim();
  if (wanted === '') return '';
  let entry = ownValue(icons, wanted);
  if (!entry || typeof entry !== 'object') {
    const lower = wanted.toLowerCase();
    const hit = Object.keys(icons).find(mapKey => String(mapKey).toLowerCase() === lower);
    entry = hit === undefined ? null : ownValue(icons, hit);
  }
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return '';
  const url = ownValue(entry, 'iconUrl');
  return typeof url === 'string' ? url.trim() : '';
}

// ---------- header provenance line ----------

// 'schedule' (the nightly cron) reads as "nightly"; anything else is the github.actor
// login that dispatched the sync (design §3 triggeredBy).
export function syncTriggerLabel(triggeredBy) {
  const raw = String(triggeredBy == null ? '' : triggeredBy).trim();
  return raw === '' || raw === 'schedule' ? 'nightly' : raw;
}

// Fixed-format, locale- and timezone-independent: the sync only ever writes ISO-8601 Z
// stamps, so slicing beats Date parsing here (a parse would render differently per
// client locale and turn a provenance line into a moving target).
export function formatSyncedAt(iso) {
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso == null ? '' : iso));
  return parts ? `${parts[1]}-${parts[2]}-${parts[3]} ${parts[4]}:${parts[5]} UTC` : '';
}

// Tooltip text is a plain DOM property, never markup, so it needs no escaping — but a
// tooltip that *looks* like a tag is an accident waiting for the next refactor, so the
// angle brackets are dropped. The rendered line itself uses escapeHtml.
function plainText(value) {
  return String(value == null ? '' : value).replace(/[<>]/g, '');
}

// The header "Last synced …" line beside the repo-status widget. Written as innerHTML,
// so both dynamic halves go through the one shared escaper: a poisoned syncedAt or
// triggeredBy in the committed sidecar cannot open a tag.
export function renderSyncLine() {
  const el = document.getElementById('bnet-sync-line');
  if (!el) return;
  const stamp = formatSyncedAt(characterData && characterData.syncedAt);
  if (!stamp) {
    el.innerHTML = 'No Battle.net data';
    el.title = 'No data/character-data.json sidecar, or it carries no syncedAt';
    return;
  }
  const trigger = syncTriggerLabel(characterData && characterData.triggeredBy);
  el.innerHTML = `<span class="text-amber-300 font-semibold">${escapeHtml(stamp)}</span> ` +
    `<span class="text-slate-600">·</span> <span class="text-slate-300">${escapeHtml(trigger)}</span>`;
  el.title = `Battle.net data synced ${plainText(stamp)} by ${plainText(trigger)}`;
}

// ---------- loads ----------

export async function loadRoster() {
  try {
    const res = await fetch(ROSTER_PATH, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const doc = await res.json();
    const list = rosterCharactersFrom(doc);
    if (!list) throw new Error('no characters in data/characters.json');

    setFetchedWrapper(doc);
    // The file just read is the baseline every later whole-file commit is compared to.
    adoptUpstreamRoster(list);
    setCharacters(normalizeRoster(list));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    renderAll();
  } catch (err) {
    console.error('data/characters.json load failed:', err);
  }
}

// One sidecar file, or null on any failure. Degrading silently is the point: the
// sidecars are enrichment, and a 404 or a truncated file must not change the roster.
export async function loadSidecar(path, opts = {}) {
  // A cache-busting query is only ever appended by reloadSidecars(), after a sync run the
  // user asked for: the committed JSON is immutable per deploy, so without it a caching
  // proxy (or Pages' own max-age) can hand back the file the run replaced.
  const url = opts.cacheBust
    ? `${path}${path.indexOf('?') === -1 ? '?' : '&'}cb=${encodeURIComponent(String(opts.cacheBust))}`
    : path;
  try {
    const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const doc = await res.json();
    return doc && typeof doc === 'object' && !Array.isArray(doc) ? doc : null;
  } catch (err) {
    console.error(`${path} load failed:`, err);
    return null;
  }
}

// Both character sidecars, in parallel, then one repaint. Never rejects, and never touches
// localStorage: sidecar state lives only for the lifetime of the page.
//
// The talent trees are deliberately absent from this pair (and from reloadSidecars()): a sync run
// rewrites that file byte-identically unless the static namespace moves, and a namespace move
// arrives with a new deploy, so a cache-busting re-read of ~200 KB after every Refresh would buy
// nothing. ensureTalentTrees() owns it instead.
export async function loadSidecars(opts = {}) {
  const [data, map, guildConfig] = await Promise.all([
    loadSidecar(CHARACTER_DATA_PATH, opts),
    loadSidecar(BATTLE_NET_MAP_PATH, opts),
    // NOT cache-busted, on purpose (the talent-trees rule): the sync never writes this file, so a
    // Refresh has no new copy of it to show. It changes with a deploy, and a deploy is a new page.
    loadSidecar(GUILD_CONFIG_PATH, {})
  ]);
  characterData = data;
  battleNetMap = map;
  // The committed guild scope — and the one place where "the read failed" must NOT become "the
  // config says nothing". loadSidecar() cannot tell a 404 (this site has no scope) from a 503 or a
  // dropped connection, and reloadSidecars() runs after every Battle.net refresh, which is exactly
  // when a blip is most likely: collapsing both into '' would unscope the roster for the rest of the
  // session and label it "No guild selected", which reads like a choice the user made. So a failed
  // read keeps the last value the app actually saw (at boot that is still '', so an absent file is
  // still "unscoped", and this browser's own override is never touched by a read at all).
  if (guildConfig !== null) {
    // Own-data lookup on a committed file (util.js discipline). A document that is present but says
    // nothing — missing key, non-string, blank — is a real "no scope", and it does clear.
    const configured = ownValue(guildConfig, 'includedGuild');
    setGuildScopeDefault(typeof configured === 'string' ? configured.trim() : '');
  } else if (!opts.cacheBust) {
    // The boot read is the one that gets to answer "does this deployment have a scope at all", and
    // at boot the value it overwrites is '' anyway, so a failure here is free. A FAILED REFRESH is
    // not: cacheBust means a sync run just finished and we went looking for its output, so this read
    // failing says nothing about the config and everything about the network — and the alternative
    // (clearing) would hand the user "No guild selected" as though they had chosen it, unscoped, for
    // the rest of the session. Keep what the page last read; the next successful refresh settles it.
    setGuildScopeDefault('');
  }
  renderSyncLine();
  renderAll();
}

// Re-read both sidecars past the HTTP cache, for the caller that just watched a sync run
// publish them (design §4: poll to completion, then refetch data/). Same assignment path as
// the first load — the sidecars stay in module scope and out of localStorage.
export function reloadSidecars() {
  return loadSidecars({ cacheBust: Date.now() });
}

// Repaints keep the header line and the badges consistent after any setCharacters().
registerRenderHook(renderSyncLine);

// The searchable sidecar half: guild, and the Battle.net lookup name the sync resolved (or
// failed to resolve) for this roster row. It enters the search through state.js's provider
// hook rather than a state.js import, so that module stays the leaf it is documented as,
// and it degrades to nothing when either sidecar is absent (search just matches less).
registerSearchProvider(c => {
  const { sidecar, mapEntry } = byRosterId(c && c.id);
  const extra = [];
  const guild = sidecar ? sidecar.guild : null;
  if (typeof guild === 'string' || typeof guild === 'number') extra.push(String(guild));
  if (mapEntry && typeof mapEntry === 'object') {
    ['bnetName', 'surname', 'slug'].forEach(key => {
      const value = mapEntry[key];
      if (typeof value === 'string' || typeof value === 'number') extra.push(String(value));
    });
  }
  return extra;
});
