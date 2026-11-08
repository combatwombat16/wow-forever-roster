// roster.js — the compact roster grid (faction backdrop, badges, micro-actions) and
// the Export action. Repaints are driven by state.js's render dispatcher.

import { characters, currentFilter, registerRenderHook } from './state.js';
import { party } from './party.js';
import { CLASS_COLORS, PROF_ABBR, escapeHtml, getFaction } from './util.js';

export function getTrackDot(track) {
  if (track === 'flowmasters') return `<span title="Flowmasters" class="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0"></span>`;
  if (track === 'family') return `<span title="Family" class="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0"></span>`;
  return `<span title="Alts" class="w-1.5 h-1.5 rounded-full bg-purple-400 shrink-0"></span>`;
}

export function getRoleIcon(role) {
  if (role === 'Tank') return '🛡️';
  if (role === 'Healer') return '💚';
  return '⚔️';
}

// Dense, compact single-row card rendering with Faction background coloring
export function renderRoster() {
  const grid = document.getElementById('character-grid');
  const filtered = characters.filter(c => currentFilter === 'all' ? true : c.track === currentFilter);
  document.getElementById('roster-count').innerText = `${filtered.length} Chars`;

  grid.innerHTML = '';
  if (filtered.length === 0) {
    grid.innerHTML = `<div class="text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800">No characters in this filter.</div>`;
    return;
  }

  filtered.forEach(c => {
    const classBadge = CLASS_COLORS[c.class] || 'text-slate-300 border-slate-700 bg-slate-800';
    const isPartyMember = party.some(p => p && p.id === c.id);
    const faction = getFaction(c.race);

    // Faction-based backdrop and border styling
    const factionStyles = faction === 'alliance'
      ? 'bg-gradient-to-r from-blue-950/45 via-slate-900/85 to-slate-900/90 border-blue-900/50 hover:border-blue-500/60 shadow-[inset_0_0_12px_rgba(30,58,138,0.2)]'
      : 'bg-gradient-to-r from-red-950/45 via-slate-900/85 to-slate-900/90 border-red-900/50 hover:border-red-500/60 shadow-[inset_0_0_12px_rgba(153,27,27,0.2)]';

    const factionTag = faction === 'alliance'
      ? '<span class="text-[9px] font-bold text-sky-400 bg-blue-950/80 px-1 py-0.2 rounded border border-blue-800/60">Alliance</span>'
      : '<span class="text-[9px] font-bold text-red-400 bg-red-950/80 px-1 py-0.2 rounded border border-red-800/60">Horde</span>';

    const prof1Str = c.prof1 && c.prof1 !== 'None' ? (PROF_ABBR[c.prof1] || c.prof1) : '';
    const prof2Str = c.prof2 && c.prof2 !== 'None' ? (PROF_ABBR[c.prof2] || c.prof2) : '';
    const profsFormatted = [prof1Str, prof2Str].filter(Boolean).map(escapeHtml).join('/');

    const card = document.createElement('div');
    card.className = `rounded-lg px-2.5 py-1.5 transition-all border flex items-center justify-between gap-2 text-xs ${factionStyles} ${isPartyMember ? 'ring-1 ring-amber-400 border-amber-400/80' : ''}`;
    card.innerHTML = `
      <!-- Left: Identity, Faction & Badges -->
      <div class="flex items-center gap-2 min-w-0 flex-1">
        ${getTrackDot(c.track)}
        <div class="min-w-0">
          <div class="flex items-center gap-1.5 leading-tight">
            <span class="font-bold text-slate-100 truncate text-[12px]">${escapeHtml(c.name)}</span>
            <span class="text-[10px] text-slate-400 truncate">(${escapeHtml(c.player)})</span>
            ${factionTag}
          </div>
          <div class="flex items-center gap-1.5 text-[10px] text-slate-400 mt-0.5 leading-none">
            <span class="font-semibold ${classBadge.split(' ')[0]}">${escapeHtml(c.class)}</span>
            <span class="text-slate-600">•</span>
            <span class="text-slate-300 font-normal">${escapeHtml(c.race)}</span>
            <span class="text-slate-600">•</span>
            <span class="text-amber-300/90 font-medium">${getRoleIcon(c.role)}${c.role}</span>
            ${c.roleSec && c.roleSec !== 'None' ? `<span class="text-sky-300/80">(${getRoleIcon(c.roleSec)}${c.roleSec})</span>` : ''}
            ${profsFormatted ? `<span class="text-slate-600">•</span><span class="text-slate-400">${profsFormatted}</span>` : ''}
          </div>
        </div>
      </div>

      <!-- Right: Compact Micro-actions -->
      <div class="flex items-center gap-1 shrink-0">
        <button onclick="togglePartyMember('${escapeHtml(c.id)}')" title="${isPartyMember ? 'Remove from party' : 'Add to party'}" class="px-1.5 py-0.5 rounded text-[11px] font-bold transition-all ${isPartyMember ? 'bg-amber-500 text-slate-950' : 'bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700'}">
          ${isPartyMember ? '✓' : '+'}
        </button>
        <button onclick="editCharacter('${escapeHtml(c.id)}')" title="Edit character" class="p-1 text-slate-400 hover:text-white text-[11px]">
          ✎
        </button>
        <button onclick="deleteCharacter('${escapeHtml(c.id)}')" title="Delete character" class="p-1 text-red-400 hover:text-red-300 text-[11px]">
          ✕
        </button>
      </div>
    `;
    grid.appendChild(card);
  });
}

export function exportData() {
  const json = JSON.stringify(characters, null, 2);
  const blob = new Blob([json], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `characters.json`;
  a.click();
  URL.revokeObjectURL(url);
}

// Registered first so the grid leads every repaint.
registerRenderHook(renderRoster);

// Inline on*="…" handlers resolve against the global scope.
window.exportData = exportData;
