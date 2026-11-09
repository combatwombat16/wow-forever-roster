// professions.js — the guild profession matrix and its coverage headline.
//
// P5.C paints a Blizzard profession icon on each tag from the lazily-loaded
// data/profession-icons.json (data.js owns the fetch). The URL gate is the window-published
// isSafeIconUrl — the roster.js precedent: detail.js imports this module for its profession
// vocabulary, so importing it back would make the matrix paint on a module cycle.

import { getFilteredCharacters, registerRenderHook } from './state.js';
import { PROFESSION_ICONS_PATH, ensureSiteIconMap, siteIconMapLoaded, siteIconUrl } from './data.js';
import { escapeHtml, ownValue } from './util.js';

export const ALL_PROFESSIONS = [
  "Alchemy", "Blacksmithing", "Enchanting", "Engineering",
  "Herbalism", "Leatherworking", "Mining", "Skinning", "Tailoring"
];

// The tag's icon slot, same discipline as the class distribution rows: nothing at all while the
// map is unloaded (the tag renders exactly as before), a reserved box once it is loaded, and a
// URL that fails the host allowlist leaves the box empty without naming its host.
function professionTagIconHtml(name) {
  if (!siteIconMapLoaded(PROFESSION_ICONS_PATH)) return '';
  const raw = siteIconUrl(PROFESSION_ICONS_PATH, name);
  const url = raw !== '' && typeof window.isSafeIconUrl === 'function' && window.isSafeIconUrl(raw) ? raw : '';
  // The alt describes the IMAGE: the Battle.net profession icon for this tag.
  return `<span class="wow-id-icon">${url === '' ? ''
    : `<img src="${escapeHtml(url)}" loading="lazy" alt="${escapeHtml(`${name} profession icon`)}" class="wow-id-icon-img">`}</span>`;
}

export function renderProfessions() {
  // The matrix is what asks for the profession-icon map (lazy, memoised, retryable in data.js).
  ensureSiteIconMap(PROFESSION_ICONS_PATH);
  const container = document.getElementById('profession-tags');
  const activeTrackChars = getFilteredCharacters();

  const counts = {};
  ALL_PROFESSIONS.forEach(p => counts[p] = []);

  // `counts` is keyed by profession, and a roster row's prof1/prof2 are user text: a plain
  // `counts[c.prof1]` on the row "prof1: 'constructor'" resolves Object.prototype and the
  // `.push` throws, blanking the panel. Only the map's own buckets are ever written.
  activeTrackChars.forEach(c => {
    const first = ownValue(counts, c.prof1);
    if (Array.isArray(first)) first.push(c.player);
    const second = ownValue(counts, c.prof2);
    if (Array.isArray(second)) second.push(c.player);
  });

  let covered = 0;
  container.innerHTML = ALL_PROFESSIONS.map(p => {
    const players = counts[p] || [];
    const isCovered = players.length > 0;
    if (isCovered) covered++;

    return `
      <div class="p-2 rounded border ${isCovered ? 'bg-amber-950/20 border-amber-600/40' : 'bg-slate-950/40 border-slate-800/60 opacity-60'}">
        <div class="font-bold text-[11px] flex items-center gap-1 ${isCovered ? 'text-amber-300' : 'text-slate-400'}">${professionTagIconHtml(p)}<span>${escapeHtml(p)}</span></div>
        <div class="text-[9px] text-slate-400 mt-0.5 truncate">
          ${isCovered ? players.map(escapeHtml).join(', ') : '<span class="text-red-400/80">Unassigned</span>'}
        </div>
      </div>
    `;
  }).join('');

  const pct = Math.round((covered / ALL_PROFESSIONS.length) * 100);
  document.getElementById('prof-coverage-pct').innerText = `${pct}% Guild Coverage (${covered}/${ALL_PROFESSIONS.length})`;
}

registerRenderHook(renderProfessions);
