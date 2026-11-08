// roster.js — the compact roster grid (faction backdrop, badges, Battle.net status
// dot and lifecycle badge, micro-actions) and the Export action. Repaints are driven
// by state.js's render dispatcher.
//
// Everything Battle.net-sourced is display-only (design §2): it is read through the
// data.js join, escaped like any other untrusted string, and never written back.

import { LIFECYCLE, characters, deriveLifecycleState, getFilteredCharacters, getQuery, isLifecycleEditable, registerRenderHook } from './state.js';
import { byRosterId, formatSyncedAt, isSidecarFresh } from './data.js';
import { party } from './party.js';
import { CLASS_COLORS, PROF_ABBR, escapeHtml, getFaction, ownString } from './util.js';

// Guild names are shown inline in a ~410px column; anything longer is elided with the
// full value in the title attribute.
export const SIDECAR_TEXT_MAX = 14;

function truncateLabel(value, max = SIDECAR_TEXT_MAX) {
  const text = String(value == null ? '' : value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function statusDot(bg, title) {
  return `<span title="${escapeHtml(title)}" class="w-1.5 h-1.5 rounded-full ${bg} shrink-0 inline-block"></span>`;
}

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

// The Battle.net freshness dot: red on a sync error, grey when there is no usable
// mapping, otherwise green/amber on the age of the sidecar row. A mapped id whose data
// has not landed yet is grey — there is nothing to date, which is not the same as stale.
export function getSidecarDot(sidecar, mapEntry) {
  const status = mapEntry && typeof mapEntry === 'object' ? mapEntry.status : null;
  if (status === 'fetch_error') {
    return statusDot('bg-red-500', 'Battle.net sync error — display data may be out of date');
  }
  if (!mapEntry) {
    return statusDot('bg-slate-600', 'No Battle.net mapping for this character');
  }
  if (status === 'not_found') {
    return statusDot('bg-slate-600', 'No Battle.net match found for this character');
  }
  const fetched = formatSyncedAt(sidecar && sidecar.fetchedAt);
  if (!fetched) {
    return statusDot('bg-slate-600', 'Mapped to Battle.net, no synced data yet');
  }
  return isSidecarFresh(sidecar)
    ? statusDot('bg-emerald-400', `Battle.net data fresh (fetched ${fetched})`)
    : statusDot('bg-amber-400', `Battle.net data stale (fetched ${fetched})`);
}

// The lifecycle badge, shown for every row whether or not a sidecar exists for it.
// clickId is optional: a row that needs a human pick gets a badge that opens its candidate
// list inside the detail modal (armory-tasks 3.3 — read-only there; the resolve action is
// Phase 3d). Only the roster's own id is interpolated, escaped, and only into the handler.
// hasCandidates (default true): the NEEDS_RESOLUTION label promises a pick, so it may only
// say 'pick candidate' when the map entry actually carries candidates. A needs_review row
// with neither slug nor candidates gets 'review match' — its real action is Refresh, and a
// badge that promises options it does not have is exactly the confusion this label caused
// live (review of the bug wave, live browser check 2026-11-08).
export function getLifecycleBadge(character, lifecycle, clickId = null, hasCandidates = true) {
  const validation = character && typeof character === 'object' ? character.validation : null;
  let label = 'unvalidated';
  let tone = 'text-slate-400 border-slate-700 bg-slate-800';
  let title = 'Not validated — editable draft, awaiting Battle.net sync';

  if (lifecycle === LIFECYCLE.AWAITING_CONFIRMATION) {
    label = 'confirm match';
    tone = 'text-amber-300 border-amber-500/40 bg-amber-500/20';
    title = 'Battle.net match resolved — confirm it in Review';
  } else if (lifecycle === LIFECYCLE.NEEDS_RESOLUTION) {
    if (hasCandidates) {
      label = 'pick candidate';
      title = 'Battle.net mapping is ambiguous — make a human pick in Review';
    } else {
      label = 'review match';
      title = 'Battle.net mapping flagged for review — refresh the lookup, or report a mismatch';
    }
    tone = 'text-amber-300 border-amber-500/40 bg-amber-500/20';
  } else if (lifecycle === LIFECYCLE.NOT_FOUND) {
    label = 'no bnet match';
    tone = 'text-red-300 border-red-800/60 bg-red-950/40';
    title = 'No Battle.net character matched this roster name';
  } else if (lifecycle === LIFECYCLE.FETCH_ERROR) {
    label = 'sync error';
    tone = 'text-red-300 border-red-800/60 bg-red-950/40';
    title = 'Battle.net sync errored on this character';
  } else if (lifecycle === LIFECYCLE.VALIDATED) {
    label = 'validated';
    tone = 'text-emerald-300 border-emerald-700/60 bg-emerald-900/40';
    const reviewedBy = validation && typeof validation === 'object' ? validation.reviewedBy : null;
    title = reviewedBy ? `Validated by ${reviewedBy} — read-only` : 'Validated — read-only';
  }

  const needsPick = lifecycle === LIFECYCLE.NEEDS_RESOLUTION && clickId != null;
  const handler = needsPick
    ? ` onclick="openCandidateList(event, '${escapeHtml(String(clickId))}')"`
    : '';

  return `<span${handler} title="${escapeHtml(title)}" class="text-[9px] font-bold uppercase tracking-wider px-1 py-0.5 rounded border ${tone}${needsPick ? ' cursor-pointer hover:brightness-125' : ''}">${escapeHtml(label)}</span>`;
}

// Level · item level · guild, straight off the sidecar. Numbers are rendered only when
// the sidecar really carries a number; the guild string is truncated then escaped.
export function getSidecarMeta(sidecar) {
  if (!sidecar) return '';
  const parts = [];
  const level = Number(sidecar.level);
  const itemLevel = Number(sidecar.itemLevel);
  if (Number.isFinite(level) && level > 0) {
    parts.push(`<span class="text-slate-200 font-semibold">Lv${escapeHtml(String(Math.round(level)))}</span>`);
  }
  if (Number.isFinite(itemLevel) && itemLevel > 0) {
    parts.push(`<span class="text-slate-300">iLvl ${escapeHtml(String(Math.round(itemLevel)))}</span>`);
  }
  const guildRaw = sidecar.guild;
  const guild = (typeof guildRaw === 'string' || typeof guildRaw === 'number') ? String(guildRaw).trim() : '';
  if (guild) {
    parts.push(`<span title="${escapeHtml(guild)}" class="text-amber-200/80 font-medium">${escapeHtml(truncateLabel(guild))}</span>`);
  }
  return parts.join('<span class="text-slate-600">•</span>');
}

// Dense, compact single-row card rendering with Faction background coloring
export function renderRoster() {
  const grid = document.getElementById('character-grid');
  // The one filtered view (track AND search), so the grid can never show rows the panels
  // to its right are not counting.
  const filtered = getFilteredCharacters();
  const query = getQuery();
  document.getElementById('roster-count').innerText = `${filtered.length} Chars`;

  grid.innerHTML = '';
  if (filtered.length === 0) {
    // Same empty-state styling as the no-match case for the track filter; only the wording
    // distinguishes them, and the echoed query is escaped like any other roster string.
    const reason = query ? `No characters match “${escapeHtml(query)}”.` : 'No characters in this filter.';
    grid.innerHTML = `<div class="text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800">${reason}</div>`;
    return;
  }

  filtered.forEach(c => {
    // Own-data lookup: an unknown (or prototype) class falls back to the neutral badge and
    // the row still renders, so a poisoned roster row can never blank the grid.
    const classBadge = ownString(CLASS_COLORS, c.class) || 'text-slate-300 border-slate-700 bg-slate-800';
    const isPartyMember = party.some(p => p && p.id === c.id);
    const faction = getFaction(c.race);

    // Battle.net side of the row: null-safe join, then the state derived from it.
    const { sidecar, mapEntry } = byRosterId(c.id);
    const lifecycle = deriveLifecycleState(c, mapEntry);
    const sidecarMeta = getSidecarMeta(sidecar);
    // A row that needs a human pick gets a badge that opens the modal straight at its (read-only)
    // candidate list; every other badge is plain status display.
    const badgeClickId = lifecycle === LIFECYCLE.NEEDS_RESOLUTION ? c.id : null;
    const bnetLine = `${getSidecarDot(sidecar, mapEntry)}${sidecarMeta ? `<span class="flex items-center gap-1">${sidecarMeta}</span>` : ''}${getLifecycleBadge(c, lifecycle, badgeClickId,
      Array.isArray(mapEntry && mapEntry.candidates) && mapEntry.candidates.length > 0)}`;

    // Faction-based backdrop and border styling
    const factionStyles = faction === 'alliance'
      ? 'bg-gradient-to-r from-blue-950/45 via-slate-900/85 to-slate-900/90 border-blue-900/50 hover:border-blue-500/60 shadow-[inset_0_0_12px_rgba(30,58,138,0.2)]'
      : 'bg-gradient-to-r from-red-950/45 via-slate-900/85 to-slate-900/90 border-red-900/50 hover:border-red-500/60 shadow-[inset_0_0_12px_rgba(153,27,27,0.2)]';

    const factionTag = faction === 'alliance'
      ? '<span class="text-[9px] font-bold text-sky-400 bg-blue-950/80 px-1 py-0.2 rounded border border-blue-800/60">Alliance</span>'
      : '<span class="text-[9px] font-bold text-red-400 bg-red-950/80 px-1 py-0.2 rounded border border-red-800/60">Horde</span>';

    const prof1Str = c.prof1 && c.prof1 !== 'None' ? (ownString(PROF_ABBR, c.prof1) || c.prof1) : '';
    const prof2Str = c.prof2 && c.prof2 !== 'None' ? (ownString(PROF_ABBR, c.prof2) || c.prof2) : '';
    const profsFormatted = [prof1Str, prof2Str].filter(Boolean).map(escapeHtml).join('/');

    const card = document.createElement('div');
    card.className = `cursor-pointer rounded-lg px-2.5 py-1.5 transition-all border flex items-center justify-between gap-2 text-xs ${factionStyles} ${isPartyMember ? 'ring-1 ring-amber-400 border-amber-400/80' : ''}`;
    card.setAttribute('data-character-id', String(c.id == null ? '' : c.id));
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
            <span class="text-amber-300/90 font-medium">${getRoleIcon(c.role)}${escapeHtml(c.role)}</span>
            ${c.roleSec && c.roleSec !== 'None' ? `<span class="text-sky-300/80">(${getRoleIcon(c.roleSec)}${escapeHtml(c.roleSec)})</span>` : ''}
            ${profsFormatted ? `<span class="text-slate-600">•</span><span class="text-slate-400">${profsFormatted}</span>` : ''}
          </div>
          <!-- Battle.net status dot, synced display data, lifecycle badge -->
          <div class="flex items-center gap-1.5 text-[10px] mt-0.5 leading-none min-w-0 flex-wrap">${bnetLine}</div>
        </div>
      </div>

      <!-- Right: Compact Micro-actions -->
      <div class="flex items-center gap-1 shrink-0">
        <button onclick="togglePartyMember('${escapeHtml(c.id)}')" title="${isPartyMember ? 'Remove from party' : 'Add to party'}" class="px-1.5 py-0.5 rounded text-[11px] font-bold transition-all ${isPartyMember ? 'bg-amber-500 text-slate-950' : 'bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700'}">
          ${isPartyMember ? '✓' : '+'}
        </button>
        ${isLifecycleEditable(c, mapEntry)
          ? `<button onclick="editCharacter('${escapeHtml(c.id)}')" title="Edit character" class="p-1 text-slate-400 hover:text-white text-[11px]">
              ✎
            </button>`
          : `<button disabled title="Validated — use Review to re-open" class="p-1 text-slate-600 text-[11px] cursor-not-allowed opacity-50">
              ✎
            </button>`}
        <button onclick="deleteCharacter('${escapeHtml(c.id)}')" title="Delete character" class="p-1 text-red-400 hover:text-red-300 text-[11px]">
          ✕
        </button>
      </div>
    `;
    // A card click opens the read-only detail modal (armory-tasks 3.3); the micro-actions
    // stopPropagation, so ✓/✎/✕ keep their own behaviour without also opening it. detail.js
    // is called through window on purpose: it imports getLifecycleBadge from this module, and
    // importing it back would make a load-order cycle the reason the click works.
    card.addEventListener('click', () => {
      if (typeof window.openCharacterDetail === 'function') window.openCharacterDetail(c.id);
    });
    card.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', e => e.stopPropagation());
    });
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
