// util.js — shared formatting: the single HTML escaper plus the display constant
// maps that more than one panel needs (class colours, faction, profession abbrevs).
// Leaf module: it imports nothing.

// ---------- own-data map lookups ----------

// Every display map in this file (class colours, bar fills, profession abbreviations, toast
// tones) — and the equivalents elsewhere, like the race→class table in editchar.js — is keyed by
// a string that comes out of data/characters.json, a sidecar, or a form filled from a roster row,
// i.e. from data any reviewer (or any commit) can edit. A plain `MAP[value]` also resolves members
// of Object.prototype: a roster row with "class": "constructor" makes `MAP[c.class]` a *function*,
// so `(MAP[c.class] || fallback).split(' ')[0]` throws and blanks the whole grid for every visitor
// — one committed line, no PAT needed. These helpers only ever return the map's own data, so an
// unknown key (or a prototype one) falls back to the caller's neutral styling instead of throwing.
// Object.hasOwn where available, with the equivalent hasOwnProperty call as the fallback.
const hasOwnData = typeof Object.hasOwn === 'function'
  ? (map, key) => Object.hasOwn(map, key)
  : (map, key) => Object.prototype.hasOwnProperty.call(map, key);

export function ownValue(map, key) {
  if (!map || typeof map !== 'object') return undefined;
  if (typeof key !== 'string' && typeof key !== 'number') return undefined;
  const name = String(key);
  return hasOwnData(map, name) ? map[name] : undefined;
}

// The own-string form of a lookup: '' when the key is absent, inherited, or its value is
// not a string. Callers `||` their own fallback, so the neutral case stays local to the panel.
export function ownString(map, key) {
  const value = ownValue(map, key);
  return typeof value === 'string' ? value : '';
}

// ---------- transient one-line status (Phase 3d: review commits, Battle.net refresh) ----------

// The write paths need feedback that is not an alert() dialog (a commit takes a second or
// two, and a modal-blocking dialog stacked on the detail modal is unusable). The toast is
// written with textContent, never innerHTML, so no call site can inject markup through it —
// a GitHub error string is response data like any other. The layout classes belong to the
// markup (#app-toast); this function only swaps the tone, so it can never fight the shell.
export const TOAST_MS = 4000;

export const TOAST_TONES = {
  info: 'bg-slate-900/95 border-slate-600 text-slate-100',
  success: 'bg-emerald-950/95 border-emerald-500 text-emerald-200',
  error: 'bg-red-950/95 border-red-500 text-red-200'
};

let toastTimer = null;

export function showToast(message, tone = 'info') {
  const el = document.getElementById('app-toast');
  if (!el) return;
  el.textContent = String(message == null ? '' : message);
  Object.keys(TOAST_TONES).forEach(name => el.classList.remove(...TOAST_TONES[name].split(' ')));
  el.classList.add(...(ownString(TOAST_TONES, tone) || ownString(TOAST_TONES, 'info')).split(' '));
  el.classList.remove('hidden');
  if (toastTimer !== null) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastTimer = null;
    el.classList.add('hidden');
  }, TOAST_MS);
}

// ---------- styling constants ----------

// Faction mapping for styling
const ALLIANCE_RACES = ["Human", "Dwarf", "Night Elf", "Gnome", "Alliance Skyborne"];
export function getFaction(race) {
  return ALLIANCE_RACES.includes(race) ? "alliance" : "horde";
}

export const CLASS_COLORS = {
  "Warrior": "text-[#C79C6E] border-[#C79C6E]/40 bg-[#C79C6E]/10",
  "Paladin": "text-[#F58CBA] border-[#F58CBA]/40 bg-[#F58CBA]/10",
  "Hunter": "text-[#ABD473] border-[#ABD473]/40 bg-[#ABD473]/10",
  "Rogue": "text-[#FFF569] border-[#FFF569]/40 bg-[#FFF569]/10",
  "Priest": "text-[#FFFFFF] border-[#FFFFFF]/40 bg-[#FFFFFF]/10",
  "Shaman": "text-[#0070DE] border-[#0070DE]/40 bg-[#0070DE]/10",
  "Mage": "text-[#40C7EB] border-[#40C7EB]/40 bg-[#40C7EB]/10",
  "Warlock": "text-[#8787ED] border-[#8787ED]/40 bg-[#8787ED]/10",
  "Druid": "text-[#FF7D0A] border-[#FF7D0A]/40 bg-[#FF7D0A]/10"
};

// Bar fills for the class distribution. Spelled out as literals because the
// Tailwind Play CDN only emits utilities it can find verbatim in this file, so
// interpolating a hex out of CLASS_COLORS would produce unstyled bars.
export const CLASS_BAR = {
  "Warrior": "bg-[#C79C6E]/75",
  "Paladin": "bg-[#F58CBA]/75",
  "Hunter": "bg-[#ABD473]/75",
  "Rogue": "bg-[#FFF569]/75",
  "Priest": "bg-[#FFFFFF]/75",
  "Shaman": "bg-[#0070DE]/75",
  "Mage": "bg-[#40C7EB]/75",
  "Warlock": "bg-[#8787ED]/75",
  "Druid": "bg-[#FF7D0A]/75"
};

export const RACE_BAR = {
  alliance: "bg-sky-500/75",
  horde: "bg-red-500/75"
};

export const PROF_ABBR = {
  "Alchemy": "Alch",
  "Blacksmithing": "BS",
  "Enchanting": "Ench",
  "Engineering": "Eng",
  "Herbalism": "Herb",
  "Leatherworking": "LW",
  "Mining": "Min",
  "Skinning": "Skin",
  "Tailoring": "Tail"
};

// Roster names and player handles are typed into a form and arrive from a shared
// JSON file, so they must never be able to close the tag they are printed inside.
export function escapeHtml(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => entities[ch]);
}
