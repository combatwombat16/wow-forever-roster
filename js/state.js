// state.js — the single source of truth: the characters array, the active Squad
// Filter, and the render dispatcher that repaints every dependent panel.
//
// Leaf module (imports nothing) so anything may import it without creating a cycle.
// Cross-module calls that used to be direct are now hook registries:
//   registerRenderHook(fn) — panels repaint on renderAll()
//   onFilterChange(fn)     — things that only follow the filter (party's button label)

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

// The one place the Squad Filter is applied, so the roster grid, the profession
// matrix and both distribution panels can never disagree about the active squad.
export function getFilteredCharacters() {
  return characters.filter(c => currentFilter === 'all' ? true : c.track === currentFilter);
}

// Inline on*="…" handlers in index.html resolve against the global scope, so the
// handlers each module owns are published on window deliberately.
window.setFilter = setFilter;
