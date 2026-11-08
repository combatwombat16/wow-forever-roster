// data.js — the same-origin roster load (data/characters.json) plus the shape
// bookkeeping the GitHub push needs to round-trip in the shape it was fetched in.
//
// Accepts the v1 bare array and the v2 envelope { schemaVersion, characters: [...] }.
// On success the roster overrides localStorage exactly as the Contents API pull does;
// on any failure it stays silent and the localStorage / DEFAULT_CHARACTERS seed that
// state.js already applied stands, so party planning keeps working offline.

import { STORAGE_KEY, getCharacters, normalizeRoster, renderAll, setCharacters } from './state.js';

export const ROSTER_PATH = 'data/characters.json';

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
