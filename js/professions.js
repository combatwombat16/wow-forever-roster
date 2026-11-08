// professions.js — the guild profession matrix and its coverage headline.

import { getFilteredCharacters, registerRenderHook } from './state.js';
import { escapeHtml } from './util.js';

export const ALL_PROFESSIONS = [
  "Alchemy", "Blacksmithing", "Enchanting", "Engineering",
  "Herbalism", "Leatherworking", "Mining", "Skinning", "Tailoring"
];

export function renderProfessions() {
  const container = document.getElementById('profession-tags');
  const activeTrackChars = getFilteredCharacters();

  const counts = {};
  ALL_PROFESSIONS.forEach(p => counts[p] = []);

  activeTrackChars.forEach(c => {
    if (c.prof1 && counts[c.prof1]) counts[c.prof1].push(c.player);
    if (c.prof2 && counts[c.prof2]) counts[c.prof2].push(c.player);
  });

  let covered = 0;
  container.innerHTML = ALL_PROFESSIONS.map(p => {
    const players = counts[p] || [];
    const isCovered = players.length > 0;
    if (isCovered) covered++;

    return `
      <div class="p-2 rounded border ${isCovered ? 'bg-amber-950/20 border-amber-600/40' : 'bg-slate-950/40 border-slate-800/60 opacity-60'}">
        <div class="font-bold text-[11px] ${isCovered ? 'text-amber-300' : 'text-slate-400'}">${p}</div>
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
