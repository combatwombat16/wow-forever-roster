// state.js — the single source of truth: the characters array, the active Squad
// Filter, the active guild filter, the active search query, the derived Battle.net
// lifecycle, and the render
// dispatcher that repaints every dependent panel.
//
// Leaf module (imports nothing) so anything may import it without creating a cycle.
// Cross-module calls that used to be direct are now hook registries:
//   registerRenderHook(fn)    — panels repaint on renderAll()
//   onFilterChange(fn)        — things that only follow the filter (party's button label)
//   registerSearchProvider(fn) — extra searchable text (data.js: guild + lookup name)
//   registerGuildProvider(fn)  — a row's Battle.net guild + whether a sidecar row exists
//                                (guildfilter.js), so the guild chips need no import here
// deriveLifecycleState() stays pure for the same reason: the map entry arrives as an
// argument, because the sidecar lookup lives in data.js and importing it here would
// turn this leaf into a hub.

export const STORAGE_KEY = 'wow_forever_roster';

export const DEFAULT_CHARACTERS = [
  { id: '1', player: 'Ben', name: 'Ironshield', race: 'Undead', class: 'Paladin', role: 'Tank', roleSec: 'Healer', track: 'flowmasters', prof1: 'Blacksmithing', prof2: 'Mining' },
  { id: '2', player: 'Ben', name: 'Moonfang', race: 'Alliance Skyborne', class: 'Druid', role: 'Healer', roleSec: 'Tank', track: 'family', prof1: 'Herbalism', prof2: 'Alchemy' },
  { id: '3', player: 'Ben', name: 'Lockbox', race: 'Gnome', class: 'Rogue', role: 'DPS', roleSec: 'None', track: 'alts', prof1: 'Engineering', prof2: 'Mining' },
  { id: '4', player: 'Alex', name: 'Stormcaller', race: 'Dwarf', class: 'Shaman', role: 'Healer', roleSec: 'DPS', track: 'flowmasters', prof1: 'Alchemy', prof2: 'Herbalism' },
  { id: '5', player: 'Sarah', name: 'Pyroclast', race: 'Orc', class: 'Mage', role: 'DPS', roleSec: 'None', track: 'flowmasters', prof1: 'Engineering', prof2: 'Mining' },
  { id: '6', player: 'Dave', name: 'Shadowspark', race: 'Gnome', class: 'Priest', role: 'DPS', roleSec: 'Healer', track: 'flowmasters', prof1: 'Tailoring', prof2: 'Enchanting' },
  { id: '7', player: 'Chris', name: 'Netherhex', race: 'Troll', class: 'Warlock', role: 'DPS', roleSec: 'None', track: 'flowmasters', prof1: 'Skinning', prof2: 'Leatherworking' },
  { id: '8', player: 'Mallory', name: 'Sunstrider', race: 'Human', class: 'Hunter', role: 'DPS', roleSec: 'None', track: 'family', prof1: 'Leatherworking', prof2: 'Skinning' }
];

// Any roster entering state (localStorage, data/characters.json, the Contents API)
// is normalised here so old saved tracks/roles cannot leak into the UI.
export function normalizeRoster(list) {
  return list.map(c => ({
    ...c,
    track: c.track === 'together' ? 'flowmasters' : c.track,
    roleSec: c.roleSec || 'None'
  }));
}

export let characters;
export let currentFilter = 'all';
// The three guild-filter bucket names, declared with the state that reads them so no module
// evaluation can hit them before their initialiser (they are consts, but TDZ is still TDZ).
export const GUILD_FILTER_ALL = 'all';
export const GUILD_GUILDLLESS = 'Guildless';
export const GUILD_UNKNOWN = 'Unknown';
// The guild chip's selection (GUILD_FILTER_ALL == every guild). Like the track and the query
// it has exactly one owner, and like them it is never persisted: a refresh starts from All.
export let currentGuildFilter = GUILD_FILTER_ALL;
// The search box's value as normalised for matching ('' == no search active). Only ever
// written through setQuery() so the query, like the track, has exactly one owner.
export let currentQuery = '';

// Synchronous seed: localStorage first, DEFAULT_CHARACTERS on empty/parse failure.
// The async fetch in data.js may override this afterwards, exactly as the Contents
// API pull used to.
let stored = localStorage.getItem(STORAGE_KEY);
characters = DEFAULT_CHARACTERS;
if (stored) {
  try {
    const parsed = JSON.parse(stored);
    if (Array.isArray(parsed) && parsed.length > 0) {
      characters = normalizeRoster(parsed);
    }
  } catch (e) {
    characters = DEFAULT_CHARACTERS;
  }
}

export function getCharacters() {
  return characters;
}

export function getQuery() {
  return currentQuery;
}

// ---------- derived lifecycle (docs/refactor-design.md §2) ----------
// Only `validated` carries meaning (in the roster's validation block); every other
// state is derived from the battle-net-map status of the same roster id, so the map
// can never drift out of sync with a stored state that the sync stopped writing.
//
// One second value exists as DATA, not as behaviour: the nightly auto-add (Phase C) stamps
// `validation.state: 'pending'` on the placeholder rows it appends for Battle.net-visible
// guild members. Nothing below recognises it — a pending row is not validated, so it falls
// through to exactly the map-status derivation an unvalidated draft gets. That is intended:
// the marker exists so a human curating the roster (and the sync gate) can tell a row the
// nightly wrote from one a person typed, and it must never freeze resolution.
export const LIFECYCLE = Object.freeze({
  AWAITING_SYNC: 'awaiting_sync',
  AWAITING_CONFIRMATION: 'awaiting_confirmation',
  NEEDS_RESOLUTION: 'needs_resolution',
  NOT_FOUND: 'not_found',
  FETCH_ERROR: 'fetch_error',
  VALIDATED: 'validated'
});

// battle-net-map.json status → derived state. `needs_review` (the sync's duplicate /
// collision verdict, and the state a user-reported mismatch lands in) is a human
// "pick / re-point this" verdict, so it derives to needs_resolution like `ambiguous`.
// Anything else, including an absent entry or an unknown status, is awaiting_sync.
function lifecycleFromMapStatus(status) {
  switch (status) {
    case 'ok': return LIFECYCLE.AWAITING_CONFIRMATION;
    case 'ambiguous': return LIFECYCLE.NEEDS_RESOLUTION;
    case 'needs_review': return LIFECYCLE.NEEDS_RESOLUTION;
    case 'not_found': return LIFECYCLE.NOT_FOUND;
    case 'fetch_error': return LIFECYCLE.FETCH_ERROR;
    default: return LIFECYCLE.AWAITING_SYNC;
  }
}

// character: a roster entry; mapEntry: that id's battle-net-map.json entry or null.
// A character with no validation block and no map entry (the state a hand-added draft
// is in) derives to awaiting_sync, which is exactly how the site behaved before the
// sidecars existed.
export function deriveLifecycleState(character, mapEntry) {
  const validation = character && typeof character === 'object' ? character.validation : null;
  if (validation && typeof validation === 'object' && validation.state === LIFECYCLE.VALIDATED) {
    return LIFECYCLE.VALIDATED;
  }
  const status = mapEntry && typeof mapEntry === 'object' ? mapEntry.status : null;
  return lifecycleFromMapStatus(status);
}

// User-authored fields stay editable until a row is validated (§2 "Editable?").
export function isLifecycleEditable(character, mapEntry) {
  return deriveLifecycleState(character, mapEntry) !== LIFECYCLE.VALIDATED;
}

export function setCharacters(list) {
  characters = list;
}

const renderHooks = [];
const filterHooks = [];

export function registerRenderHook(fn) {
  renderHooks.push(fn);
}

export function onFilterChange(fn) {
  filterHooks.push(fn);
}

// The one repaint path: roster grid first (it is what the filter visually selects),
// then the panels derived from it.
export function renderAll() {
  renderHooks.forEach(fn => fn());
}

export function setFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.className = 'filter-btn px-1.5 py-1 rounded text-[11px] font-bold transition-all text-slate-400 hover:text-white border border-transparent text-center';
  });
  const activeBtn = document.getElementById(`filter-${filter}`);
  if (activeBtn) {
    activeBtn.className = 'filter-btn px-1.5 py-1 rounded text-[11px] font-bold transition-all bg-amber-500/20 text-amber-300 border border-amber-500/40 text-center';
  }
  filterHooks.forEach(fn => fn());
  renderAll();
}

// ---------- guild filter (roster-ui-tasks Phase C) ----------
// A third predicate on the same source of truth: Squad Filter AND guild AND query, all of
// them inside getFilteredCharacters(), so the roster grid, the party pool and the
// distribution panels can never disagree about which rows are in play.
//
// The buckets are derived, never configured: every distinct guild name the roster knows,
// plus GUILD_GUILDLLESS (a sidecar row that carries no guild) and GUILD_UNKNOWN (no sidecar
// row at all). Guild for a row is joined at render time in this order:
//
//   1. the sidecar's guild string, when non-empty — the freshest source;
//   2. the row's own `guild` field, what a nightly auto-add stamps before any sidecar row
//      exists for it (README, data-flow §1);
//   3. Guildless, when a sidecar row exists and genuinely says "no guild";
//   4. Unknown, when there is no sidecar row to ask.
//
// Step 2 outranks step 3 on purpose: a committed guild name is a claim about the row, while
// an empty sidecar guild is the absence of one. The bucket name constants live with the state
// variables above.

// The sidecar half of the join, contributed by the module that owns the sidecars
// (guildfilter.js) for exactly the reason registerSearchProvider exists: this file imports
// nothing. A provider answers `{ guild, hasSidecar }`; anything else contributes nothing, so
// a broken provider degrades to "no sidecar data" rather than to a wrong bucket.
const guildProviders = [];

export function registerGuildProvider(fn) {
  if (typeof fn === 'function') guildProviders.push(fn);
}

function sidecarGuildInfo(character) {
  let guild = '';
  let hasSidecar = false;
  guildProviders.forEach(fn => {
    const info = fn(character);
    if (!info || typeof info !== 'object') return;
    const value = info.guild;
    if (guild === '' && (typeof value === 'string' || typeof value === 'number')) {
      guild = String(value).trim();
    }
    if (info.hasSidecar === true) hasSidecar = true;
  });
  return { guild, hasSidecar };
}

// The bucket one row belongs in, per the fallback order above. Exported for reuse: the chip
// labels, the counts and any later panel all read the same derived value a row filters on,
// so a chip can never advertise a bucket its own rows would not match.
export function getGuildBucket(character) {
  if (!character || typeof character !== 'object') return GUILD_UNKNOWN;
  const info = sidecarGuildInfo(character);
  if (info.guild !== '') return info.guild;
  const rowGuild = typeof character.guild === 'string' ? character.guild.trim() : '';
  if (rowGuild !== '') return rowGuild;
  return info.hasSidecar ? GUILD_GUILDLLESS : GUILD_UNKNOWN;
}

export function getGuildFilter() {
  return currentGuildFilter;
}

// ---------- guild scope (owner request, 2026-11-10) ----------
// The guild filter answers "which guild's rows do I want to look at?". The scope answers a
// different question the roster actually has: "which guild IS this roster?" — a Flowmasters
// tracker should not spend half its grid on a Heghmoh character the nightly happened to see
// through one member's profile.
//
// So the scope is a visibility rule, not a fourth predicate: with a guild included, a row is in
// the roster at all only when its bucket is that guild or a no-guild bucket, and every other
// guild's rows are absent from the grid, the party pool, the profession matrix and both
// distribution panels — they all read getFilteredCharacters(), so there is nothing to forget.
//
// The three buckets the row then offers are exactly All / the included guild / Guildless, and
// Unknown folds into Guildless: "never looked up" is not a third guild, and the 16 rows a hand-
// maintaining human added without ever being resolved have to stay reachable under something.
//
// Two sources, in a documented order:
//   1. this browser's localStorage value, when the key exists (the input's Set button writes it,
//      including the empty string, which means "scope off" — an absent key and an empty one are
//      deliberately different states);
//   2. data/guild-config.json, the committed default the deploy and the nightly both read.
// With neither, the scope is inert and the guild UI behaves exactly as Phase C shipped it.

export const GUILD_SCOPE_STORAGE_KEY = 'wow_forever_guild_scope';

// The committed default ('' == no config read / config says nothing), and this browser's override
// (null == no override, '' == explicitly scoped off). Read once at boot like the roster seed.
let includedGuildFile = '';
let includedGuildOverride = null;
try {
  const scopeStored = localStorage.getItem(GUILD_SCOPE_STORAGE_KEY);
  if (scopeStored !== null) includedGuildOverride = String(scopeStored).trim();
} catch (e) {
  includedGuildOverride = null;
}

// The ONE guild-name rule, shared with the nightly's bnet.sync.same_guild: trimmed and case-folded,
// and — because a Blizzard guild SLUG is its display name with the punctuation flattened — again
// with every letter/digit outside it gone. That second comparison is what lets the config hold
// "Twelve Guards" and still match the slug a profile carried (twelve-guards), and it is why the
// browser and the sync cannot disagree about who is in the scope: an owner who typed the slug form
// used to be admitted by the nightly and hidden by the site, which grows rows nobody can ever see.
// (Neither reader folds composed vs decomposed accents; verify_sync_artifacts says so out loud.)
// Display always keeps whatever spelling the row itself carries.
export function guildNameEquals(a, b) {
  const left = guildKey(a);
  const right = guildKey(b);
  // An empty name matches nothing — including another empty one — exactly as same_guild() does, so
  // a blank config value can never scope the site to "every guild".
  return left !== '' && left === right;
}

// The equivalence class guildNameEquals() compares on, as a single string: squashed, so it is also
// the dedupe key the chip row and the datalist use. Exported for exactly that: anywhere the UI folds
// guild names into one entry must fold on the SAME class the predicate matches with, or two surfaces
// disagree about whether two spellings are one guild.
export function guildKey(name) {
  const folded = String(name == null ? '' : name).trim().toLowerCase();
  return folded === '' ? '' : squashGuildName(folded);
}

// "Flow-masters" and "Flowmasters" are one name; the squash only ever runs on the comparison, never
// on what the UI prints.
function squashGuildName(name) {
  return name.replace(/[^\p{L}\p{N}]/gu, '');
}

// The guild this roster includes, or '' when the roster is unscoped.
export function getIncludedGuild() {
  if (includedGuildOverride !== null) return includedGuildOverride;
  return includedGuildFile;
}

// True when this browser's input has said something other than the committed default — what the
// filter row shows its "reset to the committed config" affordance on.
export function guildScopeIsOverridden() {
  return includedGuildOverride !== null;
}

// The committed default, written by the module that loads data/guild-config.json (data.js) — the
// same provider discipline the guild chips use, so this file stays the leaf that imports nothing.
export function setGuildScopeDefault(name) {
  const value = typeof name === 'string' ? name.trim() : '';
  if (value === includedGuildFile) return;
  includedGuildFile = value;
  renderAll();
}

// The filter row's Set action. The empty string is a real answer here ("unscope this browser"),
// which is why the write is unconditional and the key's PRESENCE is the override, not its value.
export function setIncludedGuild(name) {
  const value = typeof name === 'string' ? name.trim() : '';
  includedGuildOverride = value;
  try {
    localStorage.setItem(GUILD_SCOPE_STORAGE_KEY, value);
  } catch (e) {
    // A storage that refuses writes costs persistence, not the filter: the in-memory scope above
    // is what the repaint reads.
  }
  renderAll();
}

// Drop this browser's override and go back to data/guild-config.json.
export function resetIncludedGuild() {
  includedGuildOverride = null;
  try {
    localStorage.removeItem(GUILD_SCOPE_STORAGE_KEY);
  } catch (e) {
    // As above: the in-memory reset still repaints the scope correctly.
  }
  renderAll();
}

// The bucket a row is OFFERED under the scope, or '' when the scope hides the row entirely.
// getGuildBucket stays the raw four-step fact; this is the view of it the guild UI filters and
// labels with, so a foreign-guild row is excluded by one rule rather than by every reader
// remembering to skip it.
export function getScopedGuildBucket(character) {
  const bucket = getGuildBucket(character);
  const scope = getIncludedGuild();
  if (scope === '') return bucket;
  // The row's own spelling, not the typed one: the chip is labelled with what the roster says, so a
  // config that reads "flowmasters" never puts a lower-case word in the UI.
  if (guildNameEquals(bucket, scope)) return bucket;
  if (bucket === GUILD_GUILDLLESS || bucket === GUILD_UNKNOWN) return GUILD_GUILDLLESS;
  return '';
}

// Is this row in the roster at all right now? True for every row when the roster is unscoped.
export function isIncludedByGuildScope(character) {
  return getScopedGuildBucket(character) !== '';
}

// The buckets to offer, derived from the roster as it stands: guild names alphabetically
// (case-insensitively, so the chip order does not depend on which sync wrote a file first),
// then the two synthetic buckets in a fixed order. A bucket nobody is in is not offered —
// EXCEPT the scoped guild, which is offered while a scope is set even at zero rows: the scope is
// a choice the user made, and a chip row that hid it whenever it was momentarily empty would
// fight renderGuildFilter()'s "a selection with no chip falls back to All" guard.
export function guildBuckets() {
  const scope = getIncludedGuild();
  const names = new Set();
  let hasGuildless = false;
  let hasUnknown = false;
  const foldedSeen = new Set();
  characters.forEach(c => {
    const bucket = scope === '' ? getGuildBucket(c) : getScopedGuildBucket(c);
    if (bucket === '') return;
    if (bucket === GUILD_GUILDLLESS) hasGuildless = true;
    else if (bucket === GUILD_UNKNOWN) hasUnknown = true;
    else if (!foldedSeen.has(guildKey(bucket) || bucket)) {
      // Two rows spelling one guild differently are one bucket, labelled with the first spelling
      // they appear in — a chip per spelling would be two chips whose rows are indistinguishable.
      // The key is guildNameEquals' equivalence class, so the chip row and matchesGuild() can never
      // disagree about whether two names are one guild.
      // `|| bucket`: a name with no letters or digits at all ('---') squashes to the empty class,
      // which guildNameEquals() says matches NOTHING — so it must not become one shared bucket here.
      foldedSeen.add(guildKey(bucket) || bucket);
      names.add(bucket);
    }
  });
  if (scope !== '') {
    // Scoped: Unknown is not a chip (it folded into Guildless above), and the guild is offered even
    // at zero rows, because the user just asked for it.
    hasUnknown = false;
    if (!foldedSeen.has(guildKey(scope))) names.add(scope);
  }
  const sorted = [...names].sort((a, b) => {
    const x = a.toLowerCase();
    const y = b.toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  });
  if (hasGuildless) sorted.push(GUILD_GUILDLLESS);
  if (hasUnknown) sorted.push(GUILD_UNKNOWN);
  return sorted;
}

// An empty selection matches everything the scope lets through, which is what "no guild filter
// active" means and what keeps the guild predicate inert until a chip is clicked.
export function matchesGuild(character, bucket = currentGuildFilter) {
  const scoped = getScopedGuildBucket(character);
  if (bucket === GUILD_FILTER_ALL) return scoped !== '';
  // The same folded comparison in BOTH modes, because guildBuckets() folds in both modes too: a
  // chip row that offers one spelling of a guild and then matches only that exact spelling strands
  // every row spelled differently — no chip on screen returns it, not even "All" (which is scoped).
  // That state is reachable without touching the config, by pressing Set with an empty field.
  return scoped !== '' && guildNameEquals(scoped, bucket);
}

// The chip row is the only writer. It repaints through renderAll() like setFilter() does, so
// a guild change repaints exactly what a track change repaints.
export function setGuildFilter(bucket) {
  const value = typeof bucket === 'string' ? bucket.trim() : '';
  currentGuildFilter = value === '' ? GUILD_FILTER_ALL : value;
  renderAll();
}

// ---------- account grouping (roster-ui-tasks Phase D) ----------
// The roster's hand-maintained `player` field is the only account grouping key the site has:
// docs/bnet-api-notes.md probed every available endpoint and Battle.net exposes no account /
// Battletag identity at all, so the alias a reviewer types is the whole of it.
//
// The grouping is therefore DERIVED, never stored: no characters.json field is introduced, no
// row is mutated, nothing is persisted, and the grid order is whatever it already was. A row is
// flagged only when its alias owns at least ACCOUNT_GROUP_MIN rows, which is what makes the chip
// absence (rather than an empty chip) the single-character case: nothing is inserted, so nothing
// moves.
//
// Comparison trims and case-folds, because the alias is typed by hand into the edit form and
// drifts ("NahBrah" beside "nahbrah" is one account, not two). The alias that is DISPLAYED is
// the first row's own trimmed spelling in roster order, so the UI never rewrites what a reviewer
// typed — only the comparison is folded.
export const ACCOUNT_GROUP_MIN = 2;

// One player value folded for comparison. Anything that is not a string or a number folds to ''
// (and therefore to "no account", never to a group keyed on the empty string).
export function playerMatchKey(value) {
  if (typeof value === 'string') return value.trim().toLowerCase();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value).toLowerCase();
  return '';
}

// The folded aliases that own >= ACCOUNT_GROUP_MIN rows of `list`.
export function accountGroupKeys(list = characters) {
  const counts = new Map();
  if (!Array.isArray(list)) return new Set();
  list.forEach(c => {
    const key = c && typeof c === 'object' ? playerMatchKey(c.player) : '';
    if (key === '') return;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const keys = new Set();
  counts.forEach((count, key) => { if (count >= ACCOUNT_GROUP_MIN) keys.add(key); });
  return keys;
}

// The account a row belongs to, or null when there is nothing to say: no alias, or an alias the
// roster holds only once. `count` is every row of `list` sharing the alias, `alias` the display
// spelling (the first row of that group's own trimmed text, which for a consistent roster is the
// row's own value). The list is a parameter so the derivation is testable without the live
// roster; the default is the live roster, which is what the panels want.
export function accountGroupFor(character, list = characters) {
  if (!character || typeof character !== 'object') return null;
  const key = playerMatchKey(character.player);
  if (key === '') return null;
  let count = 0;
  let alias = '';
  if (Array.isArray(list)) {
    list.forEach(c => {
      if (!c || typeof c !== 'object' || playerMatchKey(c.player) !== key) return;
      count += 1;
      if (alias === '' && typeof c.player === 'string' && c.player.trim() !== '') {
        alias = c.player.trim();
      }
    });
  }
  if (count < ACCOUNT_GROUP_MIN || alias === '') return null;
  return { alias, count };
}

// ---------- search (docs/refactor-design.md §1, armory-tasks 3.4) ----------
// Search is a second predicate on the same source of truth rather than a second render
// path: getFilteredCharacters() is search AND track (AND guild), so every panel that already
// followed the track filter follows the query too.

// Extra searchable text for a character, contributed by a module whose data state.js must
// not import (data.js adds the sidecar guild and the Battle.net lookup name). A provider
// returns a string or an array of strings; anything else is ignored, so a provider cannot
// make a character match on garbage.
const searchProviders = [];

export function registerSearchProvider(fn) {
  if (typeof fn === 'function') searchProviders.push(fn);
}

// The lowercased haystack for one character: the roster identity fields plus whatever the
// providers add. Non-primitive contributions are dropped rather than stringified, so an
// object can never match on "[object object]".
export function getSearchFields(character) {
  if (!character || typeof character !== 'object') return [];
  const values = [character.name, character.player, character.class, character.role, character.roleSec];
  searchProviders.forEach(fn => {
    const extra = fn(character);
    if (Array.isArray(extra)) values.push(...extra);
    else if (extra != null) values.push(extra);
  });
  return values
    .filter(v => typeof v === 'string' || typeof v === 'number')
    .map(v => String(v).toLowerCase());
}

// Every whitespace-separated term must appear in at least one field (terms AND together,
// fields OR). An empty query matches everything, which is what "no search active" means.
export function matchesQuery(character, query = currentQuery) {
  const terms = String(query == null ? '' : query).toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const fields = getSearchFields(character);
  return terms.every(term => fields.some(field => field.indexOf(term) !== -1));
}

// The one place the Squad Filter, the guild chips and the search query are applied, so the
// roster grid, the party pool, the profession matrix and both distribution panels can never
// disagree about which characters are in play. All three predicates AND together.
export function getFilteredCharacters() {
  return characters.filter(c =>
    (currentFilter === 'all' ? true : c.track === currentFilter)
    && matchesQuery(c)
    && matchesGuild(c)
  );
}

// The search box is the only writer. Repaints through renderAll(), never through a
// panel-specific path, so a query change repaints exactly what a track change repaints.
export function setQuery(query) {
  currentQuery = String(query == null ? '' : query).trim();
  renderAll();
}

// Inline on*="…" handlers in index.html resolve against the global scope, so the
// handlers each module owns are published on window deliberately.
window.setFilter = setFilter;
window.setGuildFilter = setGuildFilter;
