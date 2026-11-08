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

import { STORAGE_KEY, getCharacters, normalizeRoster, registerRenderHook, registerSearchProvider, renderAll, setCharacters } from './state.js';
import { escapeHtml } from './util.js';

export const ROSTER_PATH = 'data/characters.json';
export const CHARACTER_DATA_PATH = 'data/character-data.json';
export const BATTLE_NET_MAP_PATH = 'data/battle-net-map.json';

// The sync runs once a day; anything fetched longer ago than this is shown stale.
export const SIDECAR_FRESH_HOURS = 36;

// The sidecars as parsed, or null when absent / unreadable / not an object.
let characterData = null;
let battleNetMap = null;

// The v2 envelope as parsed, or null when the roster arrived as a bare v1 array.
let fetchedWrapper = null;

export function setFetchedWrapper(doc) {
  fetchedWrapper = Array.isArray(doc) ? null : doc;
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

function sidecarEntry(collection, id) {
  if (!collection || id == null) return null;
  const entry = collection[String(id)];
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
    setCharacters(normalizeRoster(list));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    renderAll();
  } catch (err) {
    console.error('data/characters.json load failed:', err);
  }
}

// One sidecar file, or null on any failure. Degrading silently is the point: the
// sidecars are enrichment, and a 404 or a truncated file must not change the roster.
export async function loadSidecar(path) {
  try {
    const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const doc = await res.json();
    return doc && typeof doc === 'object' && !Array.isArray(doc) ? doc : null;
  } catch (err) {
    console.error(`${path} load failed:`, err);
    return null;
  }
}

// Both sidecars, in parallel, then one repaint. Never rejects, and never touches
// localStorage: sidecar state lives only for the lifetime of the page.
export async function loadSidecars() {
  const [data, map] = await Promise.all([
    loadSidecar(CHARACTER_DATA_PATH),
    loadSidecar(BATTLE_NET_MAP_PATH)
  ]);
  characterData = data;
  battleNetMap = map;
  renderSyncLine();
  renderAll();
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
