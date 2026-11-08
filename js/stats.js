// stats.js — race and class distribution bars. Both panels follow the Squad Filter
// through state.js's getFilteredCharacters(), and both go through the one
// renderDistribution() renderer so they can never drift apart.

import { getFilteredCharacters, registerRenderHook } from './state.js';
import { CLASS_BAR, CLASS_COLORS, RACE_BAR, escapeHtml, getFaction } from './util.js';

// Counted by character, not by player: race and class belong to the character.
export function tallyBy(chars, keyFn) {
  const counts = new Map();
  chars.forEach(c => {
    const key = keyFn(c);
    if (!key) return;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export function raceStyle(race) {
  return getFaction(race) === 'alliance'
    ? { bar: RACE_BAR.alliance, text: 'text-sky-300' }
    : { bar: RACE_BAR.horde, text: 'text-red-300' };
}

export function classStyle(cls) {
  const colors = CLASS_COLORS[cls];
  const bar = CLASS_BAR[cls];
  if (colors && bar) return { bar: bar, text: colors.split(' ')[0] };
  return { bar: 'bg-slate-600', text: 'text-slate-300' };
}

export function renderDistribution(containerId, summaryId, unitLabel, rows, total, styleFor) {
  const container = document.getElementById(containerId);
  document.getElementById(summaryId).innerText = `${rows.length} ${unitLabel} / ${total} Chars`;

  if (total === 0) {
    container.innerHTML = `<div class="text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800">No characters in this filter.</div>`;
    return;
  }

  container.innerHTML = rows.map(([label, count]) => {
    const pct = (count / total) * 100;
    const style = styleFor(label);
    return `
      <div class="flex items-center gap-2">
        <span class="w-[120px] shrink-0 text-[11px] font-semibold truncate ${style.text}" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
        <div class="flex-1 h-3 rounded bg-slate-800/70 border border-slate-700/60 overflow-hidden">
          <div class="h-full ${style.bar}" style="width:${pct.toFixed(2)}%"></div>
        </div>
        <span class="w-[64px] shrink-0 text-right text-[10px] font-semibold text-slate-400">${count} (${Math.round(pct)}%)</span>
      </div>
    `;
  }).join('');
}

export function renderRaceStats() {
  const chars = getFilteredCharacters();
  renderDistribution('race-stats', 'race-summary', 'Races', tallyBy(chars, c => c.race), chars.length, raceStyle);
}

export function renderClassStats() {
  const chars = getFilteredCharacters();
  renderDistribution('class-stats', 'class-summary', 'Classes', tallyBy(chars, c => c.class), chars.length, classStyle);
}

registerRenderHook(renderRaceStats);
registerRenderHook(renderClassStats);
