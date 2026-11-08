// detail.js — the read-only character detail modal (armory-tasks 3.3, design §6 "UI
// placement: detail modal on card click").
//
// Display-only by construction:
//   * every string, including every Battle.net-sourced one, goes through the shared
//     escapeHtml before it reaches innerHTML (gear names, candidate names, sync notes);
//   * gear icon URLs are host-allowlisted before an <img> is emitted, and a rejected URL
//     leaves a placeholder rather than a request to someone else's server;
//   * nothing is persisted. There is no localStorage write in this module, and the gear
//     list exists only in the string it is rendered from (design §2 "display-only").
//
// Opening is driven by roster.js (card click / lifecycle badge); ESC and a backdrop click
// close it. The modal is registered as a render hook so a repaint — a query change, a
// sidecar landing, a roster edit — keeps an open modal consistent instead of stale.

import { LIFECYCLE, characters, deriveLifecycleState, registerRenderHook } from './state.js';
import { byRosterId, formatSyncedAt, isSidecarFresh } from './data.js';
import { getLifecycleBadge } from './roster.js';
import { CLASS_COLORS, escapeHtml, getFaction } from './util.js';

// Blizzard's own media hosts. A committed sidecar is untrusted input (the sync writes what
// the API returned, and any of these files can be hand-edited), so the check is on the
// parsed URL's exact hostname over https: `https://render.worldofwarcraft.com.evil.tld/`,
// `https://evil.tld/?x=https://render.worldofwarcraft.com/`, `javascript:` and a
// protocol-relative `//evil.tld/` all fail closed and render no <img> at all.
export const ICON_HOSTS = ['render.worldofwarcraft.com', 'us.api.blizzard.com'];

export function isSafeIconUrl(url) {
  if (typeof url !== 'string' || url === '') return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch (err) {
    return false;
  }
  return parsed.protocol === 'https:' && ICON_HOSTS.indexOf(parsed.hostname) !== -1;
}

// Progress sections in display order. A key that is absent, empty, or carries no scalar in
// it is omitted outright (design §3: progress keys appear only where data exists).
export const PROGRESS_SECTIONS = [
  ['raid', 'Raid'],
  ['mythicPlus', 'Mythic+'],
  ['pvp', 'PvP']
];

// Guards on the generic progress walker: bounded so a surprise shape produces a truncated
// list rather than an unbounded wall, and only scalars are ever printed so no payload can
// arrive at markup.
export const PROGRESS_MAX_DEPTH = 4;
export const PROGRESS_MAX_ROWS = 24;

const PROGRESS_LABELS = {
  honorableKills: 'Honorable kills',
  pvpRank: 'PvP rank',
  completedCount: 'Completed',
  possibleCount: 'Possible',
  clearedCount: 'Cleared',
  currentRatings: 'Current ratings',
  keystoneLevel: 'Keystone level',
  keystoneScore: 'Keystone score',
  mythicRating: 'Mythic rating',
  weeklyScore: 'Weekly score',
  season: 'Season',
  level: 'Level',
  region: 'Region',
  faction: 'Faction'
};

export function humanizeKey(key) {
  const name = PROGRESS_LABELS[key];
  if (name) return name;
  const spaced = String(key).replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ').trim();
  return spaced === '' ? String(key) : spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// Scalars only (finite number, non-empty string, boolean). Objects and arrays are walked,
// never stringified, so `[object Object]` can never appear.
function scalarText(value) {
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'string') return value.trim();
  return '';
}

// Flatten one progress value into [{label, value}] rows. Only two things contribute a label:
// a leaf key (the metric) and a `name` field on an object (the group it belongs to), so a raid
// tier reads as "Molten Core · Completed" rather than a whole dotted path. Structural keys are
// traversed and dropped: the section heading above them already says what they are.
export function progressRows(value, path = [], depth = 0, out = []) {
  if (value == null || depth > PROGRESS_MAX_DEPTH || out.length >= PROGRESS_MAX_ROWS) return out;
  if (Array.isArray(value)) {
    value.forEach(item => progressRows(item, path, depth + 1, out));
    return out;
  }
  if (typeof value === 'object') {
    const name = typeof value.name === 'string' ? value.name.trim() : '';
    const groupPath = name === '' ? path : path.concat([name]);
    Object.keys(value).forEach(key => {
      if (key === 'name' && name !== '') return;
      const child = value[key];
      const leaf = child !== null && typeof child !== 'object';
      progressRows(child, leaf ? groupPath.concat([humanizeKey(key)]) : groupPath, depth + 1, out);
    });
    return out;
  }
  const text = scalarText(value);
  if (text !== '' && path.length > 0) out.push({ label: path.join(' · '), value: text });
  return out;
}

// ---------- small markup builders (all class-based: no inline colours) ----------

function field(labelText, value, valueClass = 'text-slate-200 font-semibold') {
  const text = scalarText(value);
  if (text === '') return '';
  return `<div class="min-w-0">
    <div class="text-[9px] uppercase tracking-wider text-slate-500 font-bold">${escapeHtml(labelText)}</div>
    <div class="text-[12px] ${valueClass} truncate" title="${escapeHtml(text)}">${escapeHtml(text)}</div>
  </div>`;
}

function section(heading, body) {
  if (body === '') return '';
  return `<div class="pt-2.5 border-t border-slate-800">
    <h4 class="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1.5">${escapeHtml(heading)}</h4>
    ${body}
  </div>`;
}

function emptyState(text) {
  return `<div class="text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800">${escapeHtml(text)}</div>`;
}

function numberOr(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// ---------- gear ----------

// One cell per gear entry: the Blizzard icon (only when its host is allowlisted) plus the
// item's own name, slot and ilvl. A rejected icon URL is dropped silently — the row keeps
// its text, nothing from that host is ever requested.
export function gearGrid(sidecar) {
  const gear = sidecar && typeof sidecar === 'object' && Array.isArray(sidecar.gear) ? sidecar.gear : null;
  if (!gear || gear.length === 0) return '';
  const cells = gear.map(item => {
    if (!item || typeof item !== 'object') return '';
    const name = scalarText(item.name);
    const slot = scalarText(item.slot);
    const ilvl = numberOr(item.ilvl);
    const icon = isSafeIconUrl(item.iconUrl)
      ? `<img src="${escapeHtml(item.iconUrl)}" loading="lazy" alt="${escapeHtml(name)}" class="w-8 h-8 rounded border border-slate-700 bg-slate-950 object-cover shrink-0">`
      : '<span class="w-8 h-8 rounded border border-slate-800 bg-slate-950/60 shrink-0" aria-hidden="true"></span>';
    const meta = [slot, ilvl !== null ? `ilvl ${ilvl}` : ''].filter(Boolean).join(' • ');
    return `<div class="flex items-center gap-1.5 min-w-0 rounded border border-slate-800 bg-slate-950/50 p-1.5" title="${escapeHtml(name)}">
      ${icon}
      <div class="min-w-0">
        <div class="text-[11px] text-slate-200 truncate">${escapeHtml(name)}</div>
        <div class="text-[9px] text-slate-500 truncate">${escapeHtml(meta)}</div>
      </div>
    </div>`;
  }).join('');
  if (cells === '') return '';
  return section('Gear', `<div class="grid grid-cols-1 sm:grid-cols-2 gap-1.5">${cells}</div>`);
}

// ---------- progress ----------

export function progressSections(sidecar) {
  const progress = sidecar && typeof sidecar === 'object' && sidecar.progress
    && typeof sidecar.progress === 'object' && !Array.isArray(sidecar.progress) ? sidecar.progress : null;
  if (!progress) return '';
  const blocks = PROGRESS_SECTIONS.map(([key, heading]) => {
    const rows = progressRows(progress[key]);
    if (rows.length === 0) return '';
    const lines = rows.map(row => `<div class="flex items-center justify-between gap-2 text-[11px]">
      <span class="text-slate-400 truncate" title="${escapeHtml(row.label)}">${escapeHtml(row.label)}</span>
      <span class="text-slate-200 font-semibold shrink-0">${escapeHtml(row.value)}</span>
    </div>`).join('');
    return `<div class="rounded border border-slate-800 bg-slate-950/50 p-2 space-y-0.5">
      <div class="text-[10px] font-bold uppercase tracking-wider text-amber-200/80">${escapeHtml(heading)}</div>
      ${lines}
    </div>`;
  }).join('');
  return section('Progress', blocks);
}

// ---------- candidates (read-only until Phase 3d wires the resolve action) ----------

export function candidatesBlock(mapEntry) {
  const candidates = mapEntry && typeof mapEntry === 'object' && Array.isArray(mapEntry.candidates)
    ? mapEntry.candidates : [];
  const rows = candidates.length === 0
    ? emptyState('No candidates recorded for this character.')
    : candidates.map(cand => {
      if (!cand || typeof cand !== 'object') return '';
      const name = [scalarText(cand.name), scalarText(cand.surname)].filter(Boolean).join(' ');
      const slug = scalarText(cand.slug);
      const matched = scalarText(cand.matchedForm);
      const details = [slug !== '' ? slug : null, matched !== '' ? `matched: ${matched}` : null].filter(Boolean).join(' • ');
      return `<div class="rounded border border-slate-800 bg-slate-950/50 p-2">
        <div class="text-[11px] text-slate-200 truncate">${escapeHtml(name !== '' ? name : (slug !== '' ? slug : 'unnamed candidate'))}</div>
        <div class="text-[9px] text-slate-500 truncate">${escapeHtml(details)}</div>
      </div>`;
    }).join('');
  return section('Candidates', `<div class="space-y-1.5">${rows}
    <div class="text-[10px] text-slate-500 italic">Confirm/resolve coming with validation</div>
  </div>`);
}

// ---------- header + body ----------

// Which row the modal is showing (roster id, or null when closed) and whether its read-only
// candidate list is expanded. Both are module-local: the modal is a view, not state the rest
// of the app reads, and neither is ever persisted.
let openId = null;
let candidatesOpen = false;

function displayName(sidecar, mapEntry, character) {
  const bnet = sidecar && typeof sidecar === 'object' && sidecar.bnet && typeof sidecar.bnet === 'object' ? sidecar.bnet : null;
  const source = bnet || mapEntry;
  const name = source ? scalarText(source.name) : '';
  const bnetName = name !== '' ? name : (source ? scalarText(source.bnetName) : '');
  const surname = source ? scalarText(source.surname) : '';
  const full = [bnetName, surname].filter(Boolean).join(' ');
  return full !== '' ? full : scalarText(character && character.name);
}

// 'flowmasters' → 'Flowmasters': the roster's own vocabulary, display-cased for the modal.
function trackLabel(track) {
  const text = scalarText(track);
  return text === '' ? '' : text.charAt(0).toUpperCase() + text.slice(1);
}

// The whole modal body for one row. Exported for the verification harness: it is pure over
// (roster row, sidecar row, map entry, badge markup), so the escaping and the section-omission
// rules can be asserted without a click.
export function detailHtml(character, sidecar, mapEntry, lifecycle, badgeHtml = null) {
  if (!character || typeof character !== 'object') return emptyState('No such character.');
  const badge = badgeHtml || getLifecycleBadge(character, lifecycle);
  const cls = scalarText(character.class);
  const classColor = (CLASS_COLORS[cls] || 'text-slate-300').split(' ')[0];
  const faction = getFaction(character.race);
  const factionTag = faction === 'alliance'
    ? '<span class="text-[9px] font-bold uppercase tracking-wider text-sky-400 bg-blue-950/80 px-1 py-0.5 rounded border border-blue-800/60">Alliance</span>'
    : '<span class="text-[9px] font-bold uppercase tracking-wider text-red-400 bg-red-950/80 px-1 py-0.5 rounded border border-red-800/60">Horde</span>';
  const roles = [scalarText(character.role), scalarText(character.roleSec)]
    .filter(r => r !== '' && r !== 'None').join(' / ');
  const synced = formatSyncedAt(sidecar && sidecar.fetchedAt);
  const note = mapEntry && typeof mapEntry === 'object' ? scalarText(mapEntry.note) : '';
  const achievement = numberOr(sidecar && sidecar.achievementPoints);

  const header = `<div class="flex items-start justify-between gap-3 pb-2.5 border-b border-slate-800">
    <div class="min-w-0">
      <h3 class="cinzel text-lg font-bold text-amber-300 truncate">${escapeHtml(displayName(sidecar, mapEntry, character))}</h3>
      <div class="text-[11px] text-slate-400 truncate">Roster: <span class="text-slate-200 font-semibold">${escapeHtml(scalarText(character.name))}</span>${scalarText(character.player) !== '' ? ` <span class="text-slate-500">(${escapeHtml(scalarText(character.player))})</span>` : ''}</div>
      <div class="flex flex-wrap items-center gap-1.5 mt-1.5 text-[10px]">
        ${factionTag}
        <span class="font-semibold ${classColor}">${escapeHtml(cls)}</span>
        <span class="text-slate-500">•</span>
        <span class="text-slate-300">${escapeHtml(scalarText(character.race))}</span>
        ${roles !== '' ? `<span class="text-slate-500">•</span><span class="text-amber-300/90">${escapeHtml(roles)}</span>` : ''}
        <span class="text-slate-500">•</span>
        <span class="text-slate-400">${escapeHtml(trackLabel(character.track))}</span>
        ${badge}
      </div>
    </div>
    <button onclick="closeDetail()" title="Close" class="shrink-0 p-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded border border-slate-700 text-xs transition-all">✕</button>
  </div>`;

  const fields = [
    field('Level', sidecar && sidecar.level),
    field('Item level', sidecar && sidecar.itemLevel),
    field('Guild', sidecar && sidecar.guild, 'text-amber-200/90 font-semibold'),
    field('Active spec', sidecar && sidecar.activeSpec),
    achievement !== null ? field('Achievement points', achievement) : '',
    field('Battle.net slug', mapEntry && mapEntry.slug, 'font-mono text-slate-300'),
    synced !== '' ? `<div class="min-w-0">
      <div class="text-[9px] uppercase tracking-wider text-slate-500 font-bold">Synced</div>
      <div class="text-[12px] ${isSidecarFresh(sidecar) ? 'text-emerald-300' : 'text-amber-300'} font-semibold" title="${escapeHtml(synced)}">${escapeHtml(synced)}</div>
    </div>` : ''
  ].filter(Boolean).join('');

  const body = fields === ''
    ? emptyState('No Battle.net data')
    : `<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-2.5">${fields}</div>`;

  const noteSection = note === '' ? '' : section('Sync note',
    `<div class="rounded border border-slate-800 bg-slate-950/50 p-2 text-[11px] text-slate-300">${escapeHtml(note)}</div>`);

  const noSidecar = !sidecar && !mapEntry
    ? section('Battle.net', emptyState('No Battle.net data'))
    : '';

  return header + body + gearGrid(sidecar) + progressSections(sidecar) + noteSection
    + candidatesIfOpen(lifecycle, mapEntry) + noSidecar;
}

// The candidates list is only reachable for a row that actually needs a human pick, so the
// flag is re-derived from the row on every render instead of trusted from the click.
function candidatesIfOpen(lifecycle, mapEntry) {
  if (!candidatesOpen) return '';
  if (lifecycle !== LIFECYCLE.NEEDS_RESOLUTION) return '';
  return candidatesBlock(mapEntry);
}

// ---------- open / close ----------

function modalEl() {
  return document.getElementById('detail-modal');
}

export function isDetailOpen() {
  return openId !== null;
}

// openSection === 'candidates' opens straight to the read-only candidate list (the roster's
// "pick candidate" badge path); any other value opens the plain detail view.
export function openCharacterDetail(id, openSection) {
  const modal = modalEl();
  if (!modal) return;
  openId = id == null ? null : String(id);
  const character = characters.find(c => c.id === openId);
  if (!character) {
    closeDetail();
    return;
  }
  const { mapEntry } = byRosterId(openId);
  candidatesOpen = openSection === 'candidates'
    && deriveLifecycleState(character, mapEntry) === LIFECYCLE.NEEDS_RESOLUTION;
  renderDetail();
  modal.classList.remove('hidden');
}

export function closeDetail() {
  openId = null;
  candidatesOpen = false;
  const modal = modalEl();
  if (modal) modal.classList.add('hidden');
}

// The badge inside the modal toggles the same read-only list.
export function toggleDetailCandidates() {
  if (openId === null) return;
  const character = characters.find(c => c.id === openId);
  if (!character) return;
  const { mapEntry } = byRosterId(openId);
  if (deriveLifecycleState(character, mapEntry) !== LIFECYCLE.NEEDS_RESOLUTION) return;
  candidatesOpen = !candidatesOpen;
  renderDetail();
}

// Roster-card badge path: open the modal straight at the candidates, without also firing the
// card's own click handler.
export function openCandidateList(event, id) {
  if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
  openCharacterDetail(id, 'candidates');
}

// Repaint in place: keeps an open modal honest when the roster or the sidecars change
// underneath it, and closes it if its row is gone. Runs as a render hook, so it follows the
// one dispatcher like every other panel.
export function renderDetail() {
  const modal = modalEl();
  const content = document.getElementById('detail-content');
  if (!modal || !content) return;
  if (openId === null) return;
  const character = characters.find(c => c.id === openId);
  if (!character) {
    closeDetail();
    return;
  }
  const { sidecar, mapEntry } = byRosterId(openId);
  const lifecycle = deriveLifecycleState(character, mapEntry);
  // Inside the modal the badge is the affordance that opens the read-only candidate list, so
  // it becomes a button; the card keeps it a plain (clickable) span.
  const badge = lifecycle === LIFECYCLE.NEEDS_RESOLUTION
    ? `<button type="button" onclick="toggleDetailCandidates()" title="Toggle candidate list" class="cursor-pointer">${getLifecycleBadge(character, lifecycle)}</button>`
    : getLifecycleBadge(character, lifecycle);
  content.innerHTML = detailHtml(character, sidecar, mapEntry, lifecycle, badge);
}

// Backdrop click closes; clicks inside the panel do not (the target is the backdrop itself).
const modal = modalEl();
if (modal && typeof modal.addEventListener === 'function') {
  modal.addEventListener('click', event => {
    if (event && event.target === modal) closeDetail();
  });
}

// ESC closes the detail modal only; the Add/Edit modal keeps its own Cancel path.
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('keydown', event => {
    if (event && event.key === 'Escape' && openId !== null) closeDetail();
  });
}

registerRenderHook(renderDetail);

// Inline on*="…" handlers resolve against the global scope.
window.openCharacterDetail = openCharacterDetail;
window.closeDetail = closeDetail;
window.openCandidateList = openCandidateList;
window.toggleDetailCandidates = toggleDetailCandidates;
