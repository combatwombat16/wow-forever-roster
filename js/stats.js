// stats.js — race and class distribution bars. Both panels follow the Squad Filter
// through state.js's getFilteredCharacters(), and both go through the one
// renderDistribution() renderer so they can never drift apart.
//
// P5.C paints a Blizzard class icon on each class row from the lazily-loaded
// data/class-icons.json (data.js owns the fetch; one request shared with party.js). The race
// bars deliberately pass no icon reader at all: Blizzard race icons are proven nonexistent
// (docs/bnet-api-notes.md §10.3), so their rendering is untouched — not "empty slots", nothing.

import { getFilteredCharacters, registerRenderHook } from './state.js';
import { CLASS_ICONS_PATH, ensureSiteIconMap, siteIconMapLoaded, siteIconUrl } from './data.js';
import { CLASS_BAR, CLASS_COLORS, RACE_BAR, escapeHtml, getFaction, ownString } from './util.js';

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

// Both lookups are own-data ones (see util.js ownString): the label reaching here is a class
// name read out of the roster, so `cls === "constructor"` must fall back to the neutral bar
// instead of resolving an inherited function.
export function classStyle(cls) {
  const colors = ownString(CLASS_COLORS, cls);
  const bar = ownString(CLASS_BAR, cls);
  if (colors && bar) return { bar: bar, text: colors.split(' ')[0] };
  return { bar: 'bg-slate-600', text: 'text-slate-300' };
}

// The class row's icon slot (P5.C). With no map loaded yet (the file is absent, or still in
// flight) it returns the empty string outright: the row markup stays exactly what it was before
// the feature, so a missing artifact changes no layout. Once the map IS loaded the slot is drawn
// whether or not this row's name resolves — the wow-id-icon discipline: every row reserves the
// same box, so labels cannot shift between a class the map names and one it does not.
// The URL passes through the window-published allowlist (the roster.js precedent — importing
// detail.js from here would drag the whole modal machinery into a panel module for one check);
// a rejected URL renders the empty box and never names its host.
function classIconSlot(cls) {
  if (!siteIconMapLoaded(CLASS_ICONS_PATH)) return '';
  const raw = siteIconUrl(CLASS_ICONS_PATH, cls);
  const url = raw !== '' && typeof window.isSafeIconUrl === 'function' && window.isSafeIconUrl(raw) ? raw : '';
  // The alt describes the IMAGE, the Battle.net class icon for this row's label.
  return `<span class="wow-id-icon">${url === '' ? ''
    : `<img src="${escapeHtml(url)}" loading="lazy" alt="${escapeHtml(`${cls} class icon`)}" class="wow-id-icon-img">`}</span>`;
}

// `iconFor` is opt-in: pass a function and every row gets its slot from it, pass nothing (the
// race panel) and the markup is byte-for-byte the pre-icon row.
export function renderDistribution(containerId, summaryId, unitLabel, rows, total, styleFor, iconFor) {
  const container = document.getElementById(containerId);
  document.getElementById(summaryId).innerText = `${rows.length} ${unitLabel} / ${total} Chars`;

  if (total === 0) {
    container.innerHTML = `<div class="text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800">No characters in this filter.</div>`;
    return;
  }

  const slotFor = typeof iconFor === 'function' ? iconFor : null;
  container.innerHTML = rows.map(([label, count]) => {
    const pct = (count / total) * 100;
    const style = styleFor(label);
    return `
      <div class="flex items-center gap-2">
        ${slotFor ? slotFor(label) : ''}
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
  // Asking for the class rows is what asks for the class-icon map (lazy, memoised, retryable in
  // data.js; a successful load repaints through renderAll, and this render is then the second one
  // with the slots filled).
  ensureSiteIconMap(CLASS_ICONS_PATH);
  const chars = getFilteredCharacters();
  renderDistribution('class-stats', 'class-summary', 'Classes', tallyBy(chars, c => c.class), chars.length, classStyle, classIconSlot);
}

registerRenderHook(renderRaceStats);
registerRenderHook(renderClassStats);
