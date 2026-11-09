// detail.js — the character detail modal: the read-only Battle.net view (armory-tasks 3.3,
// design §6 "UI placement: detail modal on card click") plus the Phase 3d review actions of
// design §2 (confirm match, resolve candidate, report mismatch, re-open for editing) and the
// design §4 "Refresh from Battle.net" dispatch.
//
// Trust boundaries, unchanged from Phase 3c and extended for the write paths:
//   * every string, including every Battle.net-sourced one, goes through the shared
//     escapeHtml before it reaches innerHTML (gear names, candidate names, sync notes,
//     GitHub error messages);
//   * Battle.net-derived data is display-only in every state: no action here writes a level,
//     a gear row or a progress value, and the only values this module commits are the ones a
//     human just chose (a slug from the candidate list, a free-text note, a reviewer login
//     read back from GitHub);
//   * nothing is persisted locally. There is still no localStorage access in this module —
//     the write paths go through github.js's commitValidationOnFresh (read-modify-write over
//     the roster as the branch holds it, so a second reviewer's commit is rebased onto rather
//     than reverted), and the in-memory roster is updated by github.js's updateRosterInMemory
//     so the storage key keeps one owner. A commit that did not land applies nothing here.
//   * the only values interpolated into an inline handler are a roster id matched against
//     HANDLER_ARG_RE and a candidate's numeric index. Untrusted strings (slugs, names, notes)
//     never become handler arguments, so no sidecar value can rewrite the handler it is
//     rendered into;
//   * every action is rendered only when a PAT is configured, and each handler re-checks it
//     as a belt for the disabled button, opening the PAT modal instead of failing silently.
//
// Opening is driven by roster.js (card click / lifecycle badge); ESC and a backdrop click
// close it. The modal is registered as a render hook so a repaint — a query change, a
// sidecar landing, a roster edit — keeps an open modal consistent instead of stale.

import { LIFECYCLE, characters, deriveLifecycleState, isLifecycleEditable, registerRenderHook, renderAll } from './state.js';
import { byRosterId, ensureTalentTrees, formatSyncedAt, isSidecarFresh, reloadSidecars, talentTreeFor, talentTreesLoading, talentTreesStaticNamespace } from './data.js';
import { commitValidationOnFresh, dispatchAndAwaitSync, ghHasPat, ghUserLogin, openGhModal, updateRosterInMemory } from './github.js';
import { getLifecycleBadge } from './roster.js';
import { CLASS_COLORS, escapeHtml, getFaction, ownString, ownValue, showToast } from './util.js';

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

// Label lookup is own-data too: the keys of a committed `progress` object are response data, and
// a row that carries "constructor" must not have Object.prototype's members printed at it.
export function humanizeKey(key) {
  const name = ownString(PROGRESS_LABELS, key);
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

// ---------- Phase 2 (T2.1): the tabbed shell ----------
//
// Six tabs, always all six (decision #5): a tab is never hidden, disabled or added because of
// what a sidecar row happens to carry, so the bar cannot tell one character's data from
// another's and never reflows between rows. The active tab is per-open module state — reset by
// openCharacterDetail()/closeDetail(), never persisted: the modal is a view, and pi-warden keeps
// the two storage keys to their existing owners.
//
// Every panel is rendered on every paint and hidden with `display:none`, rather than only the
// active one being built: the modal is one innerHTML string (the contract the verification
// harnesses read), a repaint then cannot lose a panel's scroll state to a partial build, and the
// tooltip registry below indexes into exactly the markup that is on screen.
export const DETAIL_TABS = [
  { id: 'gear', label: 'Gear' },
  { id: 'talents', label: 'Talents' },
  { id: 'professions', label: 'Professions' },
  { id: 'reputation', label: 'Reputation' },
  { id: 'stats', label: 'Stats' },
  { id: 'progress', label: 'Progress' }
];
export const DEFAULT_DETAIL_TAB = 'gear';

// The tooltip is one element that is retargeted rather than rebuilt, so moving the cursor from a
// slot to its neighbour swaps content in place: the hide is delayed just long enough that the
// brief gap between two adjacent slots cannot take the tooltip away first.
export const TOOLTIP_HIDE_MS = 120;
export const TOOLTIP_GAP = 10;
// Used when the element cannot be measured (no layout yet, or a DOM without geometry).
export const TOOLTIP_FALLBACK_W = 320;
export const TOOLTIP_FALLBACK_H = 200;

const TAB_BUTTON_PREFIX = 'detail-tab-';
const TAB_PANEL_PREFIX = 'detail-panel-';

let activeTab = DEFAULT_DETAIL_TAB;
// The current paint's tooltip payloads, addressed by the numeric index in the hover handler —
// the same rule as the candidate radios: only an index crosses into a handler, never a
// sidecar string.
const tooltipTargets = [];
let tooltipHideTimer = null;
// The node that currently carries `aria-describedby="detail-tooltip"`. One element describes many
// slots, so at most one node may claim it at a time, and the claim has to be dropped rather than
// left dangling: a repaint (or a close) throws the trigger away before its mouseleave/blur can
// run, which would leave a removed-or-replaced node pointing at an empty tooltip.
let lastTooltipTrigger = null;

// Ids written by this module's own markup are composed by function rather than spelled out at a
// call site: the invariant that every such lookup resolves is about index.html's shell, and these
// nodes are born and die inside #detail-content (see reviewNoteFieldId above).
function tabButtonId(id) { return TAB_BUTTON_PREFIX + id; }
function tabPanelId(id) { return TAB_PANEL_PREFIX + id; }

function isKnownTab(id) {
  const key = String(id == null ? '' : id);
  return DETAIL_TABS.some(tab => tab.id === key);
}

export function detailActiveTab() {
  return isKnownTab(activeTab) ? activeTab : DEFAULT_DETAIL_TAB;
}

// The bar replaces the per-section <h4> the single-scroll modal stacked: a tab label IS its
// section heading, so no panel repeats it. The label is kept on its own line, which also keeps
// `>Gear<` meaning "a heading called Gear" — the verification scripts assert on exactly that for
// rows with no gear, so reformatting this onto one line would look like a regression there.
function tabBarHtml() {
  const tabs = DETAIL_TABS.map(tab => {
    const selected = tab.id === detailActiveTab();
    return `<button type="button" role="tab"
      id="${tabButtonId(tab.id)}"
      data-detail-tab="${escapeHtml(tab.id)}"
      aria-controls="${tabPanelId(tab.id)}"
      aria-selected="${selected ? 'true' : 'false'}"
      tabindex="${selected ? '0' : '-1'}"
      class="wow-tab"
      onclick="setDetailTab('${escapeHtml(tab.id)}')"
      onkeydown="detailTabKeydown(event, '${escapeHtml(tab.id)}')"
    >
      ${escapeHtml(tab.label)}
    </button>`;
  }).join('');
  return `<div role="tablist" aria-label="Character detail sections" class="wow-tabs shrink-0">${tabs}</div>`;
}

function tabPanel(id, body) {
  const selected = id === detailActiveTab();
  return `<div role="tabpanel" id="${tabPanelId(id)}" aria-labelledby="${tabButtonId(id)}"${selected ? ' tabindex="0"' : ''} class="wow-tab-panel min-h-0 flex-1 overflow-y-auto${selected ? '' : ' hidden'}">
      ${body}
    </div>`;
}

function focusDetailTab(id) {
  const el = document.getElementById(tabButtonId(id));
  // A repaint replaced the button, so focus has to be moved to the new one or the keyboard user
  // lands back at the top of the document. Guarded because not every DOM in play here has focus.
  if (el && typeof el.focus === 'function') el.focus();
}

export function setDetailTab(id) {
  const next = String(id == null ? '' : id);
  if (!isKnownTab(next)) return false;
  if (next === activeTab) return true;
  activeTab = next;
  // The tree geometry is the one block the modal cannot render from the character sidecar, so
  // asking for the tab is what asks for the file (data.js owns the fetch and repaints on arrival).
  if (next === 'talents') ensureTalentTrees();
  renderDetail();
  focusDetailTab(next);
  return true;
}

// Arrow/Home/End move the selected tab (the roving-tabindex pattern: one tab button is in the
// tab sequence at a time, so Tab leaves the bar for the panel).
export function detailTabKeydown(event, id) {
  const key = event && typeof event.key === 'string' ? event.key : '';
  const position = DETAIL_TABS.findIndex(tab => tab.id === String(id == null ? '' : id));
  if (position === -1) return false;
  let next = null;
  if (key === 'ArrowRight') next = (position + 1) % DETAIL_TABS.length;
  else if (key === 'ArrowLeft') next = (position - 1 + DETAIL_TABS.length) % DETAIL_TABS.length;
  else if (key === 'Home') next = 0;
  else if (key === 'End') next = DETAIL_TABS.length - 1;
  if (next === null) return false;
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  return setDetailTab(DETAIL_TABS[next].id);
}

// ---------- shared hover/focus tooltip (T2.3, T2.4) ----------

function tooltipEl() {
  return document.getElementById('detail-tooltip');
}

// Called from detailHtml() before any panel is built: the registry is only meaningful for the
// markup that is currently on screen, and a stale index must never show another row's item.
function resetTooltipTargets() {
  tooltipTargets.length = 0;
}

function registerTooltipTarget(payload) {
  tooltipTargets.push(payload && typeof payload === 'object' ? payload : null);
  return tooltipTargets.length - 1;
}

// An item stat line's colour is three numbers off the wire, so it is rebuilt into rgba() from
// validated, clamped components instead of interpolated: a poisoned `color` object can never put
// arbitrary text into a style attribute, and a missing one falls back to the class colour.
export function rgbaColor(color) {
  if (!color || typeof color !== 'object' || Array.isArray(color)) return '';
  const r = numberOr(color.r);
  const g = numberOr(color.g);
  const b = numberOr(color.b);
  if (r === null || g === null || b === null) return '';
  const alpha = numberOr(color.a);
  const clamp255 = v => Math.min(255, Math.max(0, Math.round(v)));
  const a = alpha === null ? 1 : Math.min(1, Math.max(0, Math.round(alpha * 1000) / 1000));
  return `rgba(${clamp255(r)}, ${clamp255(g)}, ${clamp255(b)}, ${a})`;
}

// The API spells qualities as vocabulary strings; the colour lives in css/app.css keyed by the
// lower-case key so no dynamic colour ever leaves the stylesheet.
const ITEM_QUALITY_KEYS = {
  POOR: 'poor', COMMON: 'common', UNCOMMON: 'uncommon', RARE: 'rare',
  EPIC: 'epic', LEGENDARY: 'legendary', ARTIFACT: 'artifact', HEIRLOOM: 'heirloom'
};

export function qualityKey(value) {
  return ownString(ITEM_QUALITY_KEYS, scalarText(value).toUpperCase());
}

function namedThing(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? scalarText(value.name) : '';
}

// One gear tooltip: quality-coloured name, ilvl, slot, class/subclass, enchant strings and the
// stat lines the equipment endpoint carries. Gem/socket lines are absent by design — this API has
// no gem data (Phase 0), and an item with no stats renders only what it does have.
function itemTooltipHtml(item) {
  if (!item || typeof item !== 'object') return '';
  const name = scalarText(item.name);
  if (name === '') return '';
  const quality = qualityKey(item.quality);
  const title = quality === ''
    ? `<div class="text-[13px] font-bold leading-snug">${escapeHtml(name)}</div>`
    : `<div class="wow-quality text-[13px] font-bold leading-snug" data-wow-quality="${quality}">${escapeHtml(name)}</div>`;
  const ilvl = numberOr(item.ilvl);
  const sub = namedThing(item.itemSubclass);
  const cls = namedThing(item.itemClass);
  const meta = [
    ilvl !== null ? `Item Level ${ilvl}` : '',
    scalarText(item.slot),
    [cls, sub].filter(Boolean).join(' ')
  ].filter(Boolean);
  const enchants = (Array.isArray(item.enchants) ? item.enchants : [])
    .map(entry => scalarText(entry)).filter(text => text !== '');
  const stats = (Array.isArray(item.stats) ? item.stats : []).map(statLineHtml).filter(Boolean);
  const lines = [
    meta.length === 0 ? '' : `<div class="text-[10px] text-slate-400 leading-snug pt-0.5">${escapeHtml(meta.join(' · '))}</div>`,
    enchants.length === 0 ? '' : enchants.map(text => `<div class="wow-tooltip-text text-[11px] text-sky-300 leading-snug">${escapeHtml(text)}</div>`).join(''),
    stats.length === 0 ? '' : `<div class="pt-1 space-y-0.5">${stats.join('')}</div>`,
    ilvl === null && enchants.length === 0 && stats.length === 0
      ? '<div class="text-[10px] text-slate-500 italic pt-1">No further item data synced.</div>' : ''
  ].filter(Boolean).join('');
  return `<div class="space-y-0.5">${title}${lines}</div>`;
}

function statLineHtml(stat) {
  if (!stat || typeof stat !== 'object') return '';
  const text = scalarText(stat.displayString);
  if (text === '') return '';
  const color = rgbaColor(stat.color);
  const equip = stat.isEquipBonus === true ? ' text-emerald-300/90' : ' text-slate-300';
  // Only the rebuilt rgba() reaches the style attribute; the display string stays text.
  return `<div class="text-[11px] leading-snug${color === '' ? equip : ''}"${color === '' ? '' : ` style="color:${color}"`}>${escapeHtml(text)}</div>`;
}

function talentTooltipHtml(payload) {
  const entry = payload && payload.entry;
  if (!entry || typeof entry !== 'object') return '';
  const name = scalarText(entry.spellName);
  if (name === '') return '';
  const description = scalarText(entry.description);
  const cast = scalarText(entry.castTime);
  const rank = numberOr(payload.rank);
  const max = numberOr(payload.maxRanks);
  const rankLine = rank !== null && max !== null ? `Rank ${rank}/${max}` : (rank !== null ? `Rank ${rank}` : '');
  return `<div class="space-y-1">
    <div class="flex items-baseline justify-between gap-2">
      <span class="text-[13px] font-bold text-amber-200 leading-snug cinzel">${escapeHtml(name)}</span>
      ${rankLine === '' ? '' : `<span class="text-[10px] text-slate-400 shrink-0">${escapeHtml(rankLine)}</span>`}
    </div>
    ${cast === '' ? '' : `<div class="text-[10px] uppercase tracking-wider text-slate-500 font-bold">${escapeHtml(cast)}</div>`}
    ${description === '' ? '<div class="text-[11px] text-slate-500 italic">No description synced for this rank.</div>' : `<div class="wow-tooltip-text text-[11px] text-slate-300 leading-snug">${escapeHtml(description)}</div>`}
  </div>`;
}

function tooltipHtmlFor(index) {
  const position = Number(index);
  if (!Number.isInteger(position) || position < 0 || position >= tooltipTargets.length) return '';
  const target = tooltipTargets[position];
  if (!target) return '';
  return target.kind === 'gear' ? itemTooltipHtml(target.item) : talentTooltipHtml(target);
}

function triggerFromEvent(event) {
  if (!event || typeof event !== 'object') return null;
  const node = event.currentTarget || event.target;
  return node && typeof node.getBoundingClientRect === 'function' ? node : null;
}

// Right of the trigger, flipping to the left when that would run off the viewport, then clamped
// vertically so a tooltip on the bottom row of the tree stays readable.
function positionTooltip(trigger) {
  const tip = tooltipEl();
  if (!tip || !trigger) return;
  const rect = trigger.getBoundingClientRect();
  const viewportW = numberOr(typeof window === 'undefined' ? null : window.innerWidth);
  const viewportH = numberOr(typeof window === 'undefined' ? null : window.innerHeight);
  const tipW = numberOr(tip.offsetWidth) || TOOLTIP_FALLBACK_W;
  const tipH = numberOr(tip.offsetHeight) || TOOLTIP_FALLBACK_H;
  let left = rect.right + TOOLTIP_GAP;
  if (viewportW !== null && left + tipW > viewportW - 8) left = rect.left - tipW - TOOLTIP_GAP;
  left = Math.max(8, left);
  let top = Math.max(8, rect.top);
  if (viewportH !== null && top + tipH > viewportH - 8) top = Math.max(8, viewportH - tipH - 8);
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

export function showDetailTooltip(event, index) {
  const tip = tooltipEl();
  if (!tip) return false;
  const html = tooltipHtmlFor(index);
  if (html === '') return false;
  if (tooltipHideTimer !== null) {
    clearTimeout(tooltipHideTimer);
    tooltipHideTimer = null;
  }
  const trigger = triggerFromEvent(event);
  tip.innerHTML = html;
  tip.classList.remove('hidden');
  tip.setAttribute('aria-hidden', 'false');
  positionTooltip(trigger);
  // Point the hovered node at the tooltip while it is up: the shared element can only describe
  // the one it belongs to, so the reference is set on the live node rather than baked into the
  // markup of all nineteen slots. Retargeting clears the previous node first, so a cursor that
  // moves slot → slot (with no leave in between, as happens across a repaint) never leaves two
  // nodes claiming the same tooltip.
  clearTooltipDescription();
  lastTooltipTrigger = trigger;
  if (trigger && typeof trigger.setAttribute === 'function') trigger.setAttribute('aria-describedby', 'detail-tooltip');
  return true;
}

export function hideDetailTooltip() {
  if (tooltipHideTimer !== null) clearTimeout(tooltipHideTimer);
  tooltipHideTimer = setTimeout(() => {
    tooltipHideTimer = null;
    dismissDetailTooltip();
  }, TOOLTIP_HIDE_MS);
  return true;
}

// Immediate, for the paths where the trigger is already gone (a repaint, a close): leaving a
// tooltip open over different content would be worse than a tooltip that never appeared.
function dismissDetailTooltip() {
  if (tooltipHideTimer !== null) {
    clearTimeout(tooltipHideTimer);
    tooltipHideTimer = null;
  }
  const tip = tooltipEl();
  if (tip) {
    tip.classList.add('hidden');
    tip.innerHTML = '';
    tip.setAttribute('aria-hidden', 'true');
  }
  // A tooltip with no content describes nothing, so the node that pointed at it must stop saying
  // it does — otherwise an assistive tech reading the (repainted) slot announces the previous
  // character's item.
  clearTooltipDescription();
}

// Method-guarded because the DOM in play is not always a browser's: the verification harnesses
// drive these paths with stub nodes that implement only what the code under test needs.
function clearTooltipDescription() {
  const previous = lastTooltipTrigger;
  lastTooltipTrigger = null;
  if (previous && typeof previous.removeAttribute === 'function') previous.removeAttribute('aria-describedby');
}

// ---------- gear tab (T2.3) ----------

// The paper doll in fixed order: the fifteen body slots, drawn as three rows of five, then the
// weapon/ranged row. The frame is drawn whether or not the row has any gear in it — the layout is
// the character sheet, not a list of whatever arrived.
export const GEAR_BODY_SLOTS = ['Head', 'Neck', 'Shoulders', 'Back', 'Chest', 'Shirt', 'Waist', 'Legs',
  'Feet', 'Wrist', 'Hands', 'Ring 1', 'Ring 2', 'Trinket 1', 'Trinket 2'];
export const GEAR_HAND_SLOTS = ['Main Hand', 'Off Hand', 'Ranged', 'Ammo'];

// The sidecar spells a slot the way the API returned it, and the shape has moved across patches
// (a lone "Trinket", a "Weapon" for a one-hander). Anything that still maps onto the doll lands
// in its cell; anything else renders in the "Other" row rather than silently disappearing.
const GEAR_SLOT_ALIASES = {
  trinket: 'Trinket 1', ring: 'Ring 1', weapon: 'Main Hand', mainhand: 'Main Hand',
  'one-hand': 'Main Hand', offhand: 'Off Hand', 'off-hand': 'Off Hand', 'ranged left': 'Off Hand',
  bow: 'Ranged', gun: 'Ranged', crossbow: 'Ranged', wand: 'Ranged', thrown: 'Ranged',
  relic: 'Ranged', missile: 'Ammo', bolt: 'Ammo'
};

function frameSlotFor(item) {
  const raw = scalarText(item.slot);
  if (GEAR_BODY_SLOTS.indexOf(raw) !== -1 || GEAR_HAND_SLOTS.indexOf(raw) !== -1) return raw;
  const alias = ownString(GEAR_SLOT_ALIASES, raw.toLowerCase());
  return GEAR_BODY_SLOTS.indexOf(alias) !== -1 || GEAR_HAND_SLOTS.indexOf(alias) !== -1 ? alias : '';
}

// A rejected icon URL renders the empty socket and nothing else: no <img>, no src, and the URL is
// never echoed into a title or data attribute, so a foreign host is not even named on the page.
function gearIconHtml(item, name) {
  return isSafeIconUrl(item.iconUrl)
    ? `<img src="${escapeHtml(item.iconUrl)}" loading="lazy" alt="${escapeHtml(name)}" class="w-full h-full object-cover rounded">`
    : '<span class="wow-slot-glyph" aria-hidden="true"></span>';
}

function gearSlotHtml(slot, item) {
  if (!item) {
    return `<div class="wow-slot wow-slot-empty" data-wow-slot="${escapeHtml(slot)}">
      <span class="wow-slot-face"></span>
      <span class="wow-slot-name">${escapeHtml(slot)}</span>
    </div>`;
  }
  const name = scalarText(item.name);
  const index = registerTooltipTarget({ kind: 'gear', item });
  const quality = qualityKey(item.quality);
  const nameAttrs = quality === '' ? 'class="wow-slot-name"' : `class="wow-slot-name wow-quality" data-wow-quality="${quality}"`;
  const ilvl = numberOr(item.ilvl);
  return `<button type="button" class="wow-slot wow-slot-filled" data-wow-slot="${escapeHtml(slot)}"
    onmouseenter="showDetailTooltip(event, ${index})" onmouseleave="hideDetailTooltip()"
    onfocus="showDetailTooltip(event, ${index})" onblur="hideDetailTooltip()">
      <span class="wow-slot-face">${gearIconHtml(item, name)}</span>
      <span ${nameAttrs}>${escapeHtml(name)}</span>
      ${ilvl === null ? '' : `<span class="wow-slot-ilvl">${escapeHtml(String(ilvl))}</span>`}
    </button>`;
}

function gearChipsHtml(items) {
  return items.map(item => {
    const name = scalarText(item.name);
    const index = registerTooltipTarget({ kind: 'gear', item });
    return `<button type="button" class="wow-gear-chip" onmouseenter="showDetailTooltip(event, ${index})"
      onmouseleave="hideDetailTooltip()" onfocus="showDetailTooltip(event, ${index})" onblur="hideDetailTooltip()">
      ${escapeHtml(name === '' ? 'Unnamed item' : name)}
    </button>`;
  }).join('');
}

export function gearTabHtml(sidecar) {
  const gear = sidecar && typeof sidecar === 'object' && Array.isArray(sidecar.gear) ? sidecar.gear : null;
  if (!gear || gear.length === 0) return emptyState('No gear data synced yet.');
  const bySlot = new Map();
  const other = [];
  gear.forEach(item => {
    if (!item || typeof item !== 'object') return;
    const slot = frameSlotFor(item);
    // A second item claiming the same cell keeps its own row in the "Other" strip: the doll shows
    // one item per slot, but nothing in the sidecar is dropped on the floor.
    if (slot === '' || bySlot.has(slot)) {
      other.push(item);
      return;
    }
    bySlot.set(slot, item);
  });
  const cells = slots => slots.map(slot => gearSlotHtml(slot, bySlot.get(slot) || null)).join('');
  const known = [...bySlot.values()].length;
  if (known === 0 && other.length === 0) return emptyState('No gear data synced yet.');
  return `<div class="space-y-1.5">
    <div class="grid grid-cols-5 gap-1.5">${cells(GEAR_BODY_SLOTS)}</div>
    <div class="grid grid-cols-4 gap-1.5">${cells(GEAR_HAND_SLOTS)}</div>
    ${other.length === 0 ? '' : `<div class="pt-0.5">
      <div class="text-[9px] uppercase tracking-wider text-slate-500 font-bold">Unslotted</div>
      <div class="flex flex-wrap gap-1.5 pt-1">${gearChipsHtml(other)}</div>
    </div>`}
    <div class="text-[10px] text-slate-500 italic pt-0.5">Hover or focus an item for its stats, enchant and quality.</div>
  </div>`;
}

// ---------- talents tab (T2.4): the visual tree ----------

// Geometry is a grid, not a free canvas: displayRow/displayCol give the cell, and the tree is
// normalised to its own bounding box because the API numbers the columns across the whole class
// (Arms occupies 1–4, Feral 7–10, Protection 14–17). Rendering each spec inside its own box is
// what lets two specs of one class sit side by side in one modal.
export const TREE_CELL = 68;      // px between node centres — must match .wow-tree in app.css
export const TREE_NODE = 52;      // px node size — must match .wow-tree-node in app.css
// displayRow/displayCol arrive from data/talent-trees.json, a committed file any reviewer can
// hand-edit, so a coordinate is response data, not a trusted layout number: uncoerced it reaches
// the page as `left:1e+21px` / an SVG viewBox of the same order, which no browser can lay out.
// The cap is the same poison-proofing standard rgbaColor() applies to the colour triple — far
// wider than any real tree (1,470 columns) yet small enough to stay renderable.
export const TREE_PIXEL_MAX = 100000;

export function clampTreePx(value) {
  const n = numberOr(value);
  if (n === null) return 0;
  return Math.min(TREE_PIXEL_MAX, Math.max(0, Math.round(n)));
}

function treeNumber(value) {
  const n = numberOr(value);
  return n === null ? null : Math.round(n);
}

// { talentId → rank } plus { spellId → rank } of one spec group's selection. Both keys, because a
// tree rank and a selection entry are only guaranteed to agree on one of them depending on which
// id the payload carried.
function selectionMaps(entries) {
  const byTalent = new Map();
  const bySpell = new Map();
  entries.forEach(entry => {
    const rank = numberOr(entry.rank);
    if (rank === null || rank <= 0) return;
    const talentId = numberOr(entry.talentId);
    const spellId = numberOr(entry.spellId);
    if (talentId !== null) byTalent.set(talentId, Math.max(byTalent.get(talentId) || 0, rank));
    if (spellId !== null) bySpell.set(spellId, Math.max(bySpell.get(spellId) || 0, rank));
  });
  return { byTalent, bySpell };
}

function nodeRankState(node, selection) {
  const ranks = (Array.isArray(node.ranks) ? node.ranks : []).filter(r => r && typeof r === 'object');
  let taken = 0;
  ranks.forEach(rank => {
    const talentId = numberOr(rank.talentId);
    const spellId = numberOr(rank.spellId);
    const chosen = talentId !== null ? selection.byTalent.get(talentId) : undefined;
    const bySpelled = chosen === undefined && spellId !== null ? selection.bySpell.get(spellId) : chosen;
    if (typeof bySpelled === 'number' && bySpelled > taken) taken = Math.min(bySpelled, ranks.length);
  });
  // Rank N is the rank the player is paying for; with nothing spent, rank 1 is what the node
  // would do, which is the useful thing to show on hover.
  const shown = ranks.length === 0 ? null : ranks[Math.max(0, Math.min(taken, ranks.length) - (taken > 0 ? 1 : 0))];
  return { taken, maxRanks: ranks.length, shown };
}

function treeNodeHtml(node, ctx) {
  const row = treeNumber(node.displayRow);
  const col = treeNumber(node.displayCol);
  if (row === null || col === null) return '';
  const state = nodeRankState(node, ctx.selection);
  const entry = state.shown;
  const locked = (Array.isArray(node.lockedBy) ? node.lockedBy : []).some(parent => {
    const blocker = ctx.byId.get(parent);
    return blocker ? nodeRankState(blocker, ctx.selection).taken === 0 : false;
  });
  const visual = state.taken > 0 ? 'selected' : (locked ? 'locked' : 'open');
  const name = scalarText(entry && entry.spellName);
  const index = registerTooltipTarget({
    kind: 'talent',
    entry,
    rank: state.taken > 0 ? state.taken : (state.maxRanks > 0 ? 1 : null),
    maxRanks: state.maxRanks
  });
  const icon = isSafeIconUrl(entry && entry.iconUrl)
    ? `<img src="${escapeHtml(entry.iconUrl)}" loading="lazy" alt="${escapeHtml(name)}" class="w-full h-full object-cover">`
    : `<span class="wow-tree-glyph">${escapeHtml(name === '' ? '?' : name.charAt(0))}</span>`;
  const offset = (TREE_CELL - TREE_NODE) / 2;
  const left = clampTreePx((col - ctx.minCol) * TREE_CELL + offset);
  const top = clampTreePx((row - ctx.minRow) * TREE_CELL + offset);
  const fill = state.maxRanks === 0 ? 0 : Math.round((state.taken / state.maxRanks) * 100);
  return `<button type="button" class="wow-tree-node" data-wow-state="${visual}"
    style="left:${left}px;top:${top}px"
    aria-label="${escapeHtml(name === '' ? 'Talent node' : `${name}, rank ${state.taken} of ${state.maxRanks}`)}"
    onmouseenter="showDetailTooltip(event, ${index})" onmouseleave="hideDetailTooltip()"
    onfocus="showDetailTooltip(event, ${index})" onblur="hideDetailTooltip()">
      ${icon}
      <span class="wow-tree-rank">${state.taken}/${state.maxRanks}</span>
      <span class="wow-tree-fill" style="width:${Math.min(100, Math.max(0, fill))}%"></span>
    </button>`;
}

function talentTreeHtml(tree, selection) {
  const nodes = (Array.isArray(tree.nodes) ? tree.nodes : []).filter(n => n && typeof n === 'object');
  const placed = nodes.filter(n => treeNumber(n.displayRow) !== null && treeNumber(n.displayCol) !== null);
  if (placed.length === 0) return '';
  const rowValues = placed.map(n => treeNumber(n.displayRow));
  const colValues = placed.map(n => treeNumber(n.displayCol));
  const minRow = Math.min.apply(null, rowValues);
  const maxRow = Math.max.apply(null, rowValues);
  const minCol = Math.min.apply(null, colValues);
  const maxCol = Math.max.apply(null, colValues);
  const width = clampTreePx((maxCol - minCol + 1) * TREE_CELL);
  const height = clampTreePx((maxRow - minRow + 1) * TREE_CELL);
  const byId = new Map();
  nodes.forEach(node => {
    const id = numberOr(node.id);
    if (id !== null) byId.set(id, node);
  });
  const centre = node => ({
    x: clampTreePx((treeNumber(node.displayCol) - minCol) * TREE_CELL + TREE_CELL / 2),
    y: clampTreePx((treeNumber(node.displayRow) - minRow) * TREE_CELL + TREE_CELL / 2)
  });
  // Connector lines come from the payload's own unlock edges, so the picture is the API's
  // dependency graph rather than a guess from positions.
  const links = [];
  placed.forEach(node => {
    (Array.isArray(node.unlocks) ? node.unlocks : []).forEach(targetId => {
      const child = byId.get(numberOr(targetId));
      if (!child || treeNumber(child.displayRow) === null || treeNumber(child.displayCol) === null) return;
      const from = centre(node);
      const to = centre(child);
      links.push(`<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}" class="wow-tree-link"></line>`);
    });
  });
  const ctx = { selection, byId, minRow, minCol };
  return `<div class="wow-tree overflow-hidden" style="width:${width}px;height:${height}px">
      <svg class="wow-tree-links" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">${links.join('')}</svg>
      ${placed.map(node => treeNodeHtml(node, ctx)).join('')}
    </div>`;
}

// The fallback the task calls for: a selection whose spec has no entry in data/talent-trees.json
// still shows every talent it holds, ranks and descriptions included.
function talentCompactListHtml(entries) {
  const rows = entries.map(entry => {
    const name = scalarText(entry.spellName);
    const rank = numberOr(entry.rank);
    const description = scalarText(entry.description);
    const cast = scalarText(entry.castTime);
    const icon = isSafeIconUrl(entry.iconUrl)
      ? `<img src="${escapeHtml(entry.iconUrl)}" loading="lazy" alt="${escapeHtml(name)}" class="w-8 h-8 rounded border border-slate-700 bg-slate-950 object-cover shrink-0">`
      : '<span class="w-8 h-8 rounded border border-slate-800 bg-slate-950/60 shrink-0" aria-hidden="true"></span>';
    return `<div class="flex items-start gap-2 rounded border border-slate-800 bg-slate-950/50 p-1.5">
      ${icon}
      <div class="min-w-0">
        <div class="text-[11px] text-slate-200 truncate">${escapeHtml(name === '' ? 'Unnamed talent' : name)}${rank === null ? '' : ` <span class="text-slate-500">(${escapeHtml(String(rank))})</span>`}</div>
        ${cast === '' ? '' : `<div class="text-[9px] uppercase tracking-wider text-slate-500 font-bold truncate">${escapeHtml(cast)}</div>`}
        ${description === '' ? '' : `<div class="text-[10px] text-slate-400 leading-snug">${escapeHtml(description)}</div>`}
      </div>
    </div>`;
  }).join('');
  return `<div class="space-y-1.5">${rows}</div>`;
}

function talentSpecHtml(group, sidecar) {
  const specId = numberOr(group.specId);
  const specName = scalarText(group.specName);
  const spent = numberOr(group.spentPoints);
  const entries = (Array.isArray(group.talents) ? group.talents : []).filter(t => t && typeof t === 'object');
  const active = specName !== '' && specName === scalarText(sidecar && sidecar.activeSpec);
  const heading = `<div class="flex flex-wrap items-center gap-1.5">
      <h4 class="cinzel text-[12px] font-bold text-amber-200/90">${escapeHtml(specName === '' ? `Spec ${specId === null ? '?' : specId}` : specName)}</h4>
      ${active ? '<span class="text-[9px] uppercase tracking-wider font-bold text-amber-300 bg-amber-500/10 border border-amber-500/40 rounded px-1 py-0.5">Active</span>' : ''}
      <span class="text-[9px] uppercase tracking-wider text-slate-500">${spent === null ? `${entries.length} talents` : `${spent} points spent`}</span>
    </div>`;
  if (entries.length === 0) {
    return `<div class="space-y-1.5">${heading}${emptyState('No talents spent in this spec yet.')}</div>`;
  }
  const tree = specId === null ? null : talentTreeFor(specId);
  const selection = selectionMaps(entries);
  if (!tree || !Array.isArray(tree.nodes) || tree.nodes.length === 0) {
    return `<div class="space-y-1.5">${heading}
      <div class="text-[10px] text-slate-500 italic">No tree geometry synced for this spec, so its selected talents are listed.</div>
      ${talentCompactListHtml(entries)}</div>`;
  }
  const built = talentTreeHtml(tree, selection);
  if (built === '') {
    return `<div class="space-y-1.5">${heading}${talentCompactListHtml(entries)}</div>`;
  }
  const className = scalarText(tree.className);
  return `<div class="space-y-1.5">${heading}
      ${className === '' ? '' : `<div class="text-[9px] uppercase tracking-wider text-slate-500 font-bold">${escapeHtml(className)} tree</div>`}
      <div class="overflow-x-auto pb-1">${built}</div>
    </div>`;
}

export function talentsTabHtml(sidecar) {
  const hasSidecar = sidecar && typeof sidecar === 'object';
  const raw = hasSidecar ? sidecar.talents : null;
  if (!hasSidecar || !Array.isArray(raw)) return emptyState('No talents data synced yet.');
  if (raw.length === 0) return emptyState('No spec chosen yet — this character has no talent specialization selected.');
  const groups = raw.filter(g => g && typeof g === 'object');
  if (groups.length === 0) return emptyState('No spec chosen yet — this character has no talent specialization selected.');
  const loading = talentTreesLoading();
  const sections = groups.map(group => talentSpecHtml(group, sidecar)).join('<div class="pt-2 border-t border-slate-800"></div>');
  const notice = loading
    ? '<div class="text-[10px] text-slate-400 italic pb-1">Loading talent tree data…</div>'
    : '';
  const provenance = talentTreesStaticNamespace();
  return `<div class="space-y-2">${notice}${sections}
      ${provenance === '' ? '' : `<div class="text-[9px] text-slate-600">Tree geometry: ${escapeHtml(provenance)}</div>`}
    </div>`;
}

// The trees file is version-pinned to a Blizzard static namespace and the modal says so, the way
// the rest of the site states the provenance of anything the sync produced.

// ---------- professions tab (T2.5) ----------

// A recipe is a plain string in some payloads and an object in others (and the sync keeps what the
// API gave), so the name is read defensively and an unusable entry contributes nothing.
function recipeName(entry) {
  if (typeof entry === 'string') return entry.trim();
  if (entry && typeof entry === 'object') return scalarText(entry.name);
  return '';
}

function barHtml(value, max, kind) {
  const pct = max !== null && max > 0 && value !== null
    ? Math.min(100, Math.max(0, Math.round((value / max) * 100)))
    : 0;
  return `<span class="wow-bar"><span class="wow-bar-fill" data-wow-kind="${escapeHtml(kind)}" style="width:${pct}%"></span></span>`;
}

function professionHtml(profession) {
  if (!profession || typeof profession !== 'object') return '';
  const name = scalarText(profession.name);
  const tiers = (Array.isArray(profession.tiers) ? profession.tiers : []).filter(t => t && typeof t === 'object');
  const tierBlocks = tiers.map(tier => {
    const skill = numberOr(tier.skillPoints);
    const max = numberOr(tier.maxSkillPoints);
    const tierName = scalarText(tier.tierName);
    const recipes = (Array.isArray(tier.knownRecipes) ? tier.knownRecipes : []).map(recipeName).filter(Boolean);
    return `<div class="space-y-1">
      <div class="flex items-baseline justify-between gap-2">
        <span class="text-[11px] text-slate-300 truncate">${escapeHtml(tierName === '' ? 'Skill' : tierName)}</span>
        <span class="text-[10px] text-slate-400 shrink-0">${skill === null ? '—' : escapeHtml(String(skill))}${max === null ? '' : ` / ${escapeHtml(String(max))}`}</span>
      </div>
      ${barHtml(skill, max, 'skill')}
      ${recipes.length === 0 ? '<div class="text-[10px] text-slate-500 italic">No recipes known.</div>' : `<div class="flex flex-wrap gap-1 pt-0.5">${recipes.map(recipe => `<span class="wow-recipe">${escapeHtml(recipe)}</span>`).join('')}</div>`}
    </div>`;
  }).join('');
  return `<div class="rounded border border-slate-800 bg-slate-950/50 p-2 space-y-2">
      <div class="cinzel text-[12px] font-bold text-amber-200/90">${escapeHtml(name === '' ? 'Profession' : name)}</div>
      ${tierBlocks === '' ? '<div class="text-[10px] text-slate-500 italic">No skill tiers synced.</div>' : tierBlocks}
    </div>`;
}

function professionGroupHtml(heading, list) {
  const entries = (Array.isArray(list) ? list : []).filter(p => p && typeof p === 'object');
  const body = entries.length === 0
    ? emptyState(`${heading}: none synced.`)
    : `<div class="space-y-1.5">${entries.map(professionHtml).join('')}</div>`;
  return `<div class="space-y-1">
      <h4 class="text-[10px] uppercase font-bold tracking-wider text-slate-400">${escapeHtml(heading)}</h4>
      ${body}
    </div>`;
}

export function professionsTabHtml(sidecar) {
  const professions = sidecar && typeof sidecar === 'object' && sidecar.professions
    && typeof sidecar.professions === 'object' && !Array.isArray(sidecar.professions) ? sidecar.professions : null;
  if (!professions) return emptyState('No professions data synced yet.');
  const primaries = Array.isArray(professions.primaries) ? professions.primaries : [];
  const secondaries = Array.isArray(professions.secondaries) ? professions.secondaries : [];
  if (primaries.length === 0 && secondaries.length === 0) return emptyState('No professions data synced yet.');
  return `<div class="space-y-2.5">
      ${professionGroupHtml('Primary professions', primaries)}
      ${professionGroupHtml('Secondary professions', secondaries)}
    </div>`;
}

// ---------- reputation tab (T2.6) ----------

// The classic standing table, zero-based: the forever-us namespace numbers Neutral as tier 3,
// Friendly 4, Honored 5, which only lines up with a 0-based list. The payload's own `name` wins
// where it carries one — the label is the API's, the table is only the fallback.
export const REP_TIER_NAMES = ['Hated', 'Hostile', 'Unfriendly', 'Neutral', 'Friendly', 'Honored', 'Revered', 'Exalted'];

const REP_TIER_KEYS = {
  Hated: 'hated', Hostile: 'hostile', Unfriendly: 'unfriendly', Neutral: 'neutral',
  Friendly: 'friendly', Honored: 'honored', Revered: 'revered', Exalted: 'exalted'
};

export function reputationTierLabel(standing) {
  const named = standing && typeof standing === 'object' ? scalarText(standing.name) : '';
  if (ownString(REP_TIER_KEYS, named) !== '') return named;
  const tier = numberOr(standing && standing.tier);
  return tier === null || tier < 0 || tier >= REP_TIER_NAMES.length ? '' : REP_TIER_NAMES[tier];
}

function reputationRowHtml(entry) {
  if (!entry || typeof entry !== 'object') return '';
  const name = scalarText(entry.factionName);
  const standing = entry.standing && typeof entry.standing === 'object' && !Array.isArray(entry.standing)
    ? entry.standing : null;
  const tierLabel = reputationTierLabel(standing);
  const tierKey = ownString(REP_TIER_KEYS, tierLabel);
  // The bar is value/max — the progress *inside the current standing* — never raw/max, which
  // would paint a Honored character three quarters full against a bar that means something else.
  const value = numberOr(standing && standing.value);
  const max = numberOr(standing && standing.max);
  const raw = numberOr(standing && standing.raw);
  return `<div class="rounded border border-slate-800 bg-slate-950/50 p-1.5 space-y-1">
      <div class="flex items-baseline justify-between gap-2">
        <span class="text-[11px] text-slate-200 truncate">${escapeHtml(name === '' ? 'Unknown faction' : name)}</span>
        <span class="wow-rep-tier text-[10px] shrink-0${tierKey === '' ? ' text-slate-400' : ''}"${tierKey === '' ? '' : ` data-wow-tier="${tierKey}"`}>${escapeHtml(tierLabel === '' ? 'Unknown standing' : tierLabel)}</span>
      </div>
      ${barHtml(value, max, tierKey === '' ? 'rep' : `rep-${tierKey}`)}
      <div class="text-[9px] text-slate-500">${value === null ? '—' : escapeHtml(String(value))}${max === null ? '' : ` / ${escapeHtml(String(max))}`}${raw === null ? '' : ` · total ${escapeHtml(String(raw))}`}</div>
    </div>`;
}

export function reputationTabHtml(sidecar) {
  const rows = sidecar && typeof sidecar === 'object' && Array.isArray(sidecar.reputations)
    ? sidecar.reputations : null;
  if (!rows || rows.length === 0) return emptyState('No reputation data synced yet.');
  const body = rows.map(reputationRowHtml).filter(Boolean).join('');
  if (body === '') return emptyState('No reputation data synced yet.');
  return `<div class="space-y-1.5">${body}</div>`;
}

// ---------- stats tab (T2.7) ----------

// The classic /statistics contract, grouped the way a player reads a paper doll. A key that the
// payload does not carry is skipped outright (no zero, no dash, no invented row), so the panel
// shrinks to exactly the data that was synced.
export const STAT_GROUPS = [
  ['Attributes', [
    ['strength', 'Strength'], ['agility', 'Agility'], ['intellect', 'Intellect'], ['stamina', 'Stamina'],
    ['spirit', 'Spirit'], ['health', 'Health'], ['power', 'Power'], ['powerType', 'Power type'],
    ['armor', 'Armor'], ['bonusArmor', 'Bonus armor'], ['movementSpeed', 'Movement speed']
  ]],
  ['Combat', [
    ['attackPower', 'Attack power'], ['rangedAttackPower', 'Ranged attack power'],
    ['spellPower', 'Spell power'], ['spellHealing', 'Spell healing'], ['spellPenetration', 'Spell penetration'],
    ['manaRegen', 'Mana regen (out of combat)'], ['manaRegenCombat', 'Mana regen (in combat)'],
    ['mainHandDamageMin', 'Main hand damage min'], ['mainHandDamageMax', 'Main hand damage max'],
    ['mainHandSpeed', 'Main hand speed'], ['mainHandDps', 'Main hand DPS'],
    ['offHandDamageMin', 'Off hand damage min'], ['offHandDamageMax', 'Off hand damage max'],
    ['offHandSpeed', 'Off hand speed'], ['offHandDps', 'Off hand DPS'],
    ['rangedDamageMin', 'Ranged damage min'], ['rangedDamageMax', 'Ranged damage max'],
    ['speed', 'Attack speed haste'], ['meleeHaste', 'Melee haste'], ['rangedHaste', 'Ranged haste'],
    ['spellHaste', 'Spell haste'], ['versatility', 'Versatility'],
    ['versatilityDamageDoneBonus', 'Versatility damage bonus'],
    ['versatilityHealingDoneBonus', 'Versatility healing bonus'],
    ['versatilityDamageTakenBonus', 'Versatility damage taken']
  ]],
  ['Crit, Hits & Ratings', [
    ['meleeCrit', 'Melee crit'], ['rangedCrit', 'Ranged crit'], ['spellCrit', 'Spell crit'],
    ['meleeHit', 'Melee hit'], ['offHandHit', 'Off hand hit'], ['rangedHit', 'Ranged hit'],
    ['spellHit', 'Spell hit'], ['dodge', 'Dodge'], ['parry', 'Parry'], ['block', 'Block'],
    ['defense', 'Defense'], ['expertise', 'Expertise'], ['offHandExpertise', 'Off hand expertise'],
    ['armorPenetration', 'Armor penetration'], ['mastery', 'Mastery'], ['lifesteal', 'Lifesteal'],
    ['avoidance', 'Avoidance']
  ]],
  ['Resistances', [
    ['fireResistance', 'Fire resistance'], ['shadowResistance', 'Shadow resistance'],
    ['natureResistance', 'Nature resistance'], ['arcaneResistance', 'Arcane resistance'],
    ['frostResistance', 'Frost resistance']
  ]]
];

function fmtStat(value) {
  const n = numberOr(value);
  if (n === null) return '';
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

// The three shapes the endpoint uses: a scalar, a {base, effective} pair and a
// {value, ratingBonus, ratingNormalized} triple. Anything else is not rendered — the panel shows
// numbers it can name, never a stringified object.
function statValueParts(raw) {
  if (typeof raw === 'number' || typeof raw === 'string') {
    const text = typeof raw === 'string' ? raw.trim() : fmtStat(raw);
    return text === '' ? null : { value: text, detail: '' };
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const base = numberOr(ownValue(raw, 'base'));
  const effective = numberOr(ownValue(raw, 'effective'));
  if (base !== null && effective !== null) {
    return { value: fmtStat(effective), detail: base === effective ? '' : `base ${fmtStat(base)}` };
  }
  const value = ownValue(raw, 'value');
  if (value !== undefined) {
    const main = fmtStat(value);
    if (main === '') return null;
    const bonus = numberOr(ownValue(raw, 'ratingBonus'));
    return { value: `${main}%`, detail: bonus !== null && bonus !== 0 ? `${bonus > 0 ? '+' : ''}${fmtStat(bonus)} rating` : '' };
  }
  const named = namedThing(raw);
  return named === '' ? null : { value: named, detail: '' };
}

function statRowHtml(key, label, stats) {
  const raw = ownValue(stats, key);
  if (raw === undefined || raw === null) return '';
  const parts = statValueParts(raw);
  if (parts === null || parts.value === '') return '';
  return `<div class="flex items-baseline justify-between gap-2 text-[11px]">
      <span class="text-slate-400 truncate">${escapeHtml(label)}</span>
      <span class="text-slate-200 font-semibold text-right shrink-0">${escapeHtml(parts.value)}${parts.detail === '' ? '' : ` <span class="text-slate-500 font-normal">(${escapeHtml(parts.detail)})</span>`}</span>
    </div>`;
}

export function statsTabHtml(sidecar) {
  const stats = sidecar && typeof sidecar === 'object' && sidecar.stats
    && typeof sidecar.stats === 'object' && !Array.isArray(sidecar.stats) ? sidecar.stats : null;
  if (!stats) return emptyState('No combat statistics synced yet.');
  const groups = STAT_GROUPS.map(([heading, rows]) => {
    const lines = rows.map(([key, label]) => statRowHtml(key, label, stats)).filter(Boolean);
    if (lines.length === 0) return '';
    return `<div class="rounded border border-slate-800 bg-slate-950/50 p-2 space-y-0.5">
        <div class="text-[10px] font-bold uppercase tracking-wider text-amber-200/80">${escapeHtml(heading)}</div>
        ${lines.join('')}
      </div>`;
  }).filter(Boolean).join('');
  if (groups === '') return emptyState('No combat statistics synced yet.');
  return `<div class="space-y-1.5">${groups}</div>`;
}

// ---------- progress tab (T2.8) ----------

function progressTabHtml(sidecar) {
  const built = progressSections(sidecar);
  return built === '' ? emptyState('No raid, Mythic+ or PvP progress synced yet.') : built;
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

// ---------- Phase 3d: review actions (design §2 lifecycle, §4 refresh) ----------

// A mismatch note is free text typed by a reviewer and committed into characters.json, where
// the sync reads it and the UI re-renders it. The cap keeps it a note rather than an essay in
// a roster that every visitor downloads.
export const REVIEW_NOTE_MAX = 240;

// A roster id interpolated into an inline handler has to be something an id can legitimately
// be. Date.now()-generated ids and small integers pass; a value carrying a quote, a bracket or
// whitespace does not, and the row renders no action buttons instead of handing that value
// back to the HTML/JS parser. Candidate slugs never enter a handler at all (the radios carry
// the candidate's numeric index), so untrusted map data cannot reach this surface.
export const HANDLER_ARG_RE = /^[A-Za-z0-9_.:-]{1,64}$/;

// A Battle.net realmless slug (notes §2): lowercase alphanumerics, hyphen-separated. A
// confirmedSlug is written to characters.json and the sync builds its profile URL straight from
// it, so an out-of-shape value is refused here rather than committed for the sync to fetch.
export const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

// The class colour for a roster row: an own-data lookup with a neutral fallback, so a row whose
// class is not one of the nine (or is a prototype key such as "constructor") still renders its
// badge instead of throwing on a function and blanking the modal. The fallback is a literal so
// Tailwind can see it.
export function classColorFor(value) {
  return (ownString(CLASS_COLORS, value) || 'text-slate-300').split(' ')[0];
}

// The handler-argument form of a roster id: '' when the id is not usable, which is how every
// action builder in this file fails closed.
export function handlerArg(value) {
  const text = String(value == null ? '' : value);
  return HANDLER_ARG_RE.test(text) ? text : '';
}

// The sync writes fixed-precision ISO-8601 Z stamps; the review audit fields match them so the
// roster, the map and the sidecar diff cleanly.
function nowStamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function rowById(id) {
  const key = String(id == null ? '' : id);
  return characters.find(c => c && c.id === key) || null;
}

function validationBlock(character) {
  const validation = character && typeof character === 'object' ? character.validation : null;
  return validation && typeof validation === 'object' && !Array.isArray(validation) ? validation : null;
}

// The reviewer's stored report (design §2 "report mismatch"), or null. Read-only display; the
// sync turns it into a map needs_review entry until a validation.state=validated commit lands.
export function reviewReport(character) {
  const validation = validationBlock(character);
  const note = validation ? scalarText(validation.note) : '';
  if (note === '') return null;
  return {
    note,
    reportedBy: scalarText(validation.reportedBy),
    reportedAt: scalarText(validation.reportedAt)
  };
}

// ---------- module-local view state (never persisted) ----------

// Which row the modal is showing (roster id, or null when closed), whether its candidate list
// is expanded, whether the mismatch form is open, the candidate picked in the radio list, the
// last commit result, and the in-flight Battle.net refresh. None of it is persisted: the modal
// is a view, and a stale selection from another row would be a wrong review, not just a stale
// render.
let openId = null;
// Whether the candidate list is shown. null means "the mode's own default": shown when the reviewer
// can act on it (3d made the resolve control the default view for these rows) and collapsed when
// they cannot (the read-only 3c list, hidden until asked for). true/false is a choice the reviewer
// made, and both badge paths go through it: the card's "pick candidate" badge sets it to shown, the
// badge inside the modal flips it. It used to be one boolean whose true meant "collapsed" in the
// interactive mode, which inverted the card badge into a hide-candidates button.
let candidatesToggled = null;
let mismatchOpen = false;
let chosenSlug = null;
let chosenFor = null;
let lastResult = null;      // {id, tone, message}
let refresh = null;         // {id, message} while a dispatch/poll is in flight
let actionRunning = false;

function candidatesVisible(interactive) {
  return candidatesToggled === null ? interactive === true : candidatesToggled;
}

function resetReviewView() {
  candidatesToggled = null;
  mismatchOpen = false;
  chosenSlug = null;
  chosenFor = null;
  lastResult = null;
}

// Per-open view state (T2.1): the active tab and any open tooltip. Neither is persisted, and both
// are dropped when the row changes — a tab chosen on one character is not a fact about the next,
// and a tooltip left open across a row switch would show the previous character's item.
function resetDetailView() {
  activeTab = DEFAULT_DETAIL_TAB;
  dismissDetailTooltip();
}

function refreshFor(character) {
  return refresh && character && refresh.id === String(character.id) ? refresh : null;
}

function setLastResult(character, tone, message) {
  lastResult = { id: String(character.id), tone, message };
}

function resultBanner(character) {
  if (!lastResult || lastResult.id !== String(character.id)) return '';
  const tone = lastResult.tone === 'success'
    ? 'border-emerald-700/70 bg-emerald-950/50 text-emerald-200'
    : 'border-red-800/70 bg-red-950/50 text-red-200';
  return `<div role="status" class="rounded border px-2 py-1.5 text-[11px] font-semibold ${tone}">${escapeHtml(lastResult.message)}</div>`;
}

// ---------- markup atoms ----------

const ACTION_TONES = {
  primary: 'bg-amber-500 hover:bg-amber-400 text-slate-950 border border-amber-400',
  neutral: 'bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700',
  danger: 'bg-red-950/70 hover:bg-red-900 text-red-100 border border-red-800'
};

function actionButton(label, call, tone = 'neutral', opts = {}) {
  const disabled = opts.disabled === true;
  const classes = `px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider transition-all ${ownString(ACTION_TONES, tone) || ownString(ACTION_TONES, 'neutral')}`
    + (disabled ? ' opacity-50 cursor-not-allowed' : '');
  return `<button type="button"${disabled ? ' disabled' : ''} onclick="${escapeHtml(call)}"`
    + ` title="${escapeHtml(opts.title == null ? '' : opts.title)}" class="${classes}">${escapeHtml(label)}</button>`;
}

function hintBlock(text) {
  return `<div class="text-[11px] text-slate-400 leading-snug">${escapeHtml(text)}</div>`;
}

function reviewNoteFieldId(id) {
  // Built dynamically on purpose: the field belongs to one row, so a repaint for another row
  // can never read the previous row's half-typed note.
  return `mismatch-note-${String(id == null ? '' : id)}`;
}

// ---------- the actions for one row ----------

// The per-state action set (design §2 table). Everything here is either a constant label or an
// escaped value, and every handler argument is the validated id plus, where needed, an index.
function stateActions(character, mapEntry, lifecycle, id, busy) {
  // One row of buttons: takes the cells as rest arguments (or a nested array), so a button that
  // a guard returned '' for simply disappears instead of leaving an empty slot.
  const rows = (...cells) => `<div class="flex flex-wrap items-center gap-1.5 pt-1.5">${cells.flat().filter(Boolean).join('')}</div>`;

  if (lifecycle === LIFECYCLE.VALIDATED) {
    const validation = validationBlock(character) || {};
    const reviewedBy = scalarText(validation.reviewedBy);
    const reviewedAt = formatSyncedAt(scalarText(validation.reviewedAt)) || scalarText(validation.reviewedAt);
    const confirmed = scalarText(validation.confirmedSlug);
    const who = reviewedBy === '' ? 'a reviewer' : reviewedBy;
    return section('Locked', `${hintBlock(`Validated by ${who}${reviewedAt === '' ? '' : ` on ${reviewedAt}`}.`
      + `${confirmed === '' ? '' : ` Confirmed slug: ${confirmed}.`}`
      + ' Re-opening it writes a new commit and makes the row editable again.')}
      ${rows(actionButton('Re-open for editing', `reopenForEditing('${id}')`, 'danger', {
        disabled: busy,
        title: 'Remove the validation block so this row can be edited again'
      }))}`);
  }

  if (lifecycle === LIFECYCLE.AWAITING_CONFIRMATION) {
    const name = [scalarText(mapEntry && mapEntry.bnetName), scalarText(mapEntry && mapEntry.surname)].filter(Boolean).join(' ');
    const slug = scalarText(mapEntry && mapEntry.slug);
    const mapped = name === '' ? (SLUG_RE.test(slug) ? slug : 'a Battle.net character') : `${name}${SLUG_RE.test(slug) ? ` (${slug})` : ''}`;
    return section('Review', `${resultBanner(character)}
      ${hintBlock(`Battle.net resolved this row to ${mapped}. Confirming records the match and locks the row read-only.`)}
      ${rows(
        actionButton('Confirm match', `confirmMatch('${id}')`, 'primary', {
          disabled: busy || !SLUG_RE.test(slug),
          title: SLUG_RE.test(slug)
            ? 'Validate this row with the mapped slug'
            : 'The mapped slug is not in Battle.net shape, so there is nothing safe to confirm'
        }),
        actionButton('Report mismatch', `toggleMismatchForm('${id}')`, 'danger', {
          disabled: busy,
          title: 'Battle.net matched the wrong character — leave a note for the reviewer'
        })
      )}
      ${mismatchForm(character, id, busy)}`);
  }

  if (lifecycle === LIFECYCLE.NEEDS_RESOLUTION) {
    // Both `ambiguous` and `needs_review` derive to this state (state.js), and they arrive in three
    // different shapes: a candidate list to choose from, an identity worth signing off (the slug a
    // reported mismatch carries, or one the sync kept for a collided row), or neither at all. Each
    // shape gets the affordance it can actually use, and none of them gets a dead button:
    //
    //   * a mapped slug  → CONFIRM THIS MATCH, which is confirmMatch() unchanged — the same flow, the
    //     same §2 payload, and the slug taken from the committed map entry rather than typed;
    //   * candidate(s)   → the radio RESOLVE panel (rendered above this section) plus CONFIRM
    //     SELECTED, which stays disabled until one is chosen;
    //   * neither        → no confirm action at all, and a hint that says to Refresh.
    // Before this, a needs_review row offered only CONFIRM SELECTED: for the "unsure about a match"
    // rows that carry no candidates — every reported mismatch, and every duplicate normalization in
    // the live map — the button was permanently disabled and the row had no way out.
    const picked = chosenFor === String(character.id) ? chosenSlug : null;
    const entrySlug = scalarText(mapEntry && mapEntry.slug);
    const mapped = SLUG_RE.test(entrySlug) ? entrySlug : '';
    const pickable = mapCandidates(character).some(c => c && SLUG_RE.test(scalarText(c.slug)));
    const hint = mapped === '' && pickable === false
      ? 'Battle.net recorded no profile and no candidate for this row (a duplicate, or a collision it will not guess between). Refresh to query Battle.net again, or report a mismatch if the roster row itself is wrong.'
      : 'Battle.net is not sure which profile this row is. Confirm the mapping it recorded, choose one of the candidates below, or report a mismatch.';
    return section('Review', `${resultBanner(character)}
      ${hintBlock(hint)}
      ${rows(
        mapped === '' ? '' : actionButton('Confirm this match', `confirmMatch('${id}')`, 'primary', {
          disabled: busy,
          title: `Validate this row as ${mapped}, the mapping Battle.net recorded`
        }),
        pickable === false ? '' : actionButton('Confirm selected', `confirmSelectedCandidate('${id}')`, 'primary', {
          disabled: busy || !picked,
          title: picked ? `Validate this row as ${picked}` : 'Select one candidate first'
        }),
        actionButton('Report mismatch', `toggleMismatchForm('${id}')`, 'danger', {
          disabled: busy,
          title: 'None of these is this character'
        })
      )}
      ${mismatchForm(character, id, busy)}`);
  }

  // awaiting_sync / not_found / fetch_error: there is nothing recorded to confirm or dispute
  // yet, so the only useful thing the modal can say is how to get data.
  return section('Review', `${resultBanner(character)}
    ${hintBlock('No Battle.net mapping to review yet — use Refresh to query Battle.net for this character.')}`);
}

// The mismatch form: free text, required, capped, and rendered back escaped. The textarea value
// is read at submit time from its own per-row id, so nothing typed is kept in module state.
function mismatchForm(character, id, busy) {
  if (!mismatchOpen) return '';
  const fieldId = reviewNoteFieldId(character.id);
  return `<div class="pt-2 space-y-1.5">
    <label for="${escapeHtml(fieldId)}" class="block text-[10px] uppercase tracking-wider text-slate-400 font-bold">Why is this not the right character? (required, ${REVIEW_NOTE_MAX} max)</label>
    <textarea id="${escapeHtml(fieldId)}" rows="2" maxlength="${REVIEW_NOTE_MAX}"
      class="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:border-amber-400"
      placeholder="e.g. this is the other server's Shiftybeef, mine is a tauren"></textarea>
    <div class="flex flex-wrap items-center gap-1.5">
      ${actionButton('Submit report', `reportMismatch('${id}')`, 'danger', {
        disabled: busy,
        title: 'Commit the note; the row stays editable and the sync will flag it for review'
      })}
      ${actionButton('Cancel', `toggleMismatchForm('${id}')`, 'neutral', { disabled: busy, title: 'Discard the note' })}
    </div>
  </div>`;
}

// The whole action area for a row, or '' when the viewer cannot write (no PAT): with no token
// the modal is exactly the read-only Phase 3c view.
export function reviewHtml(character, mapEntry, lifecycle) {
  if (!character || typeof character !== 'object') return '';
  const id = handlerArg(character.id);
  if (id === '') {
    return ghHasPat()
      ? section('Review', hintBlock('This row’s id is not usable for review actions.'))
      : '';
  }
  if (!ghHasPat()) return '';
  const busy = actionRunning || refreshFor(character) !== null;
  const state = refreshFor(character);
  const refreshSection = section('Battle.net', `${hintBlock('Starts a sync-bnet.yml run for this name on GitHub — your PAT only; no Blizzard credential is ever sent from the browser. The data reloads by itself once the run finishes.')}
    <div class="flex flex-wrap items-center gap-1.5 pt-1.5">
      <button type="button"${state || busy ? ' disabled' : ''} onclick="${escapeHtml(`refreshFromBattleNet('${id}')`)}" title="Ask GitHub to refresh this character from Battle.net" class="px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider transition-all bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700${state || busy ? ' opacity-50 cursor-not-allowed' : ''}">${escapeHtml(state ? state.message : 'Refresh from Battle.net')}</button>
    </div>`);
  return stateActions(character, mapEntry, lifecycle, id, busy) + refreshSection;
}

// ---------- the write paths ----------

// One action at a time, one banner per row, and a repaint either way so the button state can
// never be left showing a spinner the action has outlived.
async function runAction(character, work) {
  if (actionRunning) return false;
  actionRunning = true;
  renderDetail();
  try {
    return await work();
  } finally {
    actionRunning = false;
    renderDetail();
  }
}

// Guard shared by every commit: PAT present, row still in state, and the row in the lifecycle
// the action needs — editable for confirm/resolve/report, validated for re-open. The disabled
// buttons are the UI's answer; this is ours, and reopenForEditing goes through it too rather
// than re-deriving the same rule.
function guardCommit(character, { requireValidated = false } = {}) {
  if (!ghHasPat()) {
    showToast('A GitHub PAT is required for review actions — add it in Settings.', 'error');
    openGhModal();
    return 'no-pat';
  }
  if (!character) {
    showToast('That character is no longer in the roster.', 'error');
    return 'missing';
  }
  const { mapEntry } = byRosterId(character.id);
  const editable = isLifecycleEditable(character, mapEntry);
  if (requireValidated) {
    if (editable) {
      showToast('This row is not validated, so there is nothing to re-open.', 'error');
      return 'unlocked';
    }
    return null;
  }
  if (!editable) {
    showToast('This row is validated — re-open it for editing first.', 'error');
    return 'locked';
  }
  return null;
}

// Commit one row's validation change, and make the committed roster the in-memory truth.
//
// The mutation is applied by github.js to the row as the branch holds it (read-modify-write),
// so a second reviewer's commit that landed after this page loaded survives: this browser only
// rewrites the one row it was asked to change, and it rebases onto the rest. `nextValidation`
// may be a function of the fresh row when the payload has to merge with what is already there
// (the mismatch report keeps the other validation keys it did not touch).
//
// On failure nothing is applied locally: a review that did not reach the repository would
// otherwise show as locked here and unlocked for everyone else.
async function commitValidation(character, nextValidation, { message, success }) {
  const result = await commitValidationOnFresh(message, character.id, copy => {
    if (nextValidation === null) delete copy.validation;
    else copy.validation = typeof nextValidation === 'function' ? nextValidation(copy) : nextValidation;
    // Deliberate asymmetry (review f2d4edd nit): confirm/candidate actions pass a whole block,
    // so a mismatch note reported upstream after this page loaded is erased by a successful
    // confirm — a completed validation supersedes the objection it answers. reportMismatch
    // passes a function and merges, so notes coexist. Other row keys are always preserved
    // (the fresh-row copy in github.js).
  });
  if (!result.ok) {
    if (result.error === 'no-pat' || result.error === 'no-repo' || result.error === 'bad-repo') openGhModal();
    const text = result.message || 'The commit did not land.';
    setLastResult(character, 'error', text);
    showToast(text, 'error');
    return false;
  }
  updateRosterInMemory(result.list);
  setLastResult(character, 'success', success);
  showToast(success, 'success');
  return true;
}

// The reviewer identity is read from GitHub itself (GET /user, cached in memory), never
// inferred, because it becomes the audit field of the commit.
async function reviewerLogin() {
  const login = await ghUserLogin();
  if (login === null || login === '') {
    showToast('Could not read your GitHub login (GET /user). Check the PAT.', 'error');
    return null;
  }
  return login;
}

// CONFIRM MATCH: map status ok → validation {state: validated, confirmedSlug: entry.slug, …}
export async function confirmMatch(id) {
  const character = rowById(id);
  if (guardCommit(character) !== null) return false;
  const { mapEntry } = byRosterId(character.id);
  const slug = scalarText(mapEntry && mapEntry.slug);
  if (!SLUG_RE.test(slug)) {
    showToast('There is no Battle.net slug to confirm for this row.', 'error');
    return false;
  }
  return runAction(character, async () => {
    const reviewedBy = await reviewerLogin();
    if (reviewedBy === null) return false;
    return commitValidation(character, {
      state: LIFECYCLE.VALIDATED,
      confirmedSlug: slug,
      reviewedBy,
      reviewedAt: nowStamp()
    }, {
      message: `Validate ${character.name} as ${slug} [skip ci]`,
      success: `Confirmed ${character.name} as ${slug}.`
    });
  });
}

// The radio selection. The candidate is taken from the map entry by index, so the value that
// ends up in characters.json is the committed candidate slug and never a string the browser
// was handed back.
export function selectCandidate(id, index) {
  const character = rowById(id);
  if (!character) return false;
  const position = Number(index);
  const candidates = mapCandidates(character);
  const pick = Number.isInteger(position) && position >= 0 && position < candidates.length
    ? candidates[position] : null;
  const slug = pick ? scalarText(pick.slug) : '';
  if (!pick || !SLUG_RE.test(slug)) {
    chosenSlug = null;
    chosenFor = null;
    renderDetail();
    return false;
  }
  chosenSlug = slug;
  chosenFor = character.id;
  renderDetail();
  return true;
}

function mapCandidates(character) {
  const { mapEntry } = byRosterId(character.id);
  return mapEntry && typeof mapEntry === 'object' && Array.isArray(mapEntry.candidates) ? mapEntry.candidates : [];
}

// RESOLVE → CONFIRM SELECTED: same validation block, with the reviewer's chosen slug.
export async function confirmSelectedCandidate(id) {
  const character = rowById(id);
  if (guardCommit(character) !== null) return false;
  const slug = chosenFor === character.id ? scalarText(chosenSlug) : '';
  if (!SLUG_RE.test(slug)) {
    showToast('Select one candidate first.', 'error');
    return false;
  }
  return runAction(character, async () => {
    const reviewedBy = await reviewerLogin();
    if (reviewedBy === null) return false;
    const ok = await commitValidation(character, {
      state: LIFECYCLE.VALIDATED,
      confirmedSlug: slug,
      reviewedBy,
      reviewedAt: nowStamp()
    }, {
      message: `Resolve ${character.name} to ${slug} [skip ci]`,
      success: `Mapped ${character.name} to ${slug}.`
    });
    if (ok) chosenSlug = null;
    return ok;
  });
}

export function toggleMismatchForm(id) {
  const character = rowById(id);
  if (!character) return false;
  if (ghHasPat() === false) {
    openGhModal();
    return false;
  }
  mismatchOpen = !mismatchOpen;
  renderDetail();
  return mismatchOpen;
}

// REPORT MISMATCH: a required, escaped, ≤240-character note, and deliberately no state key —
// the row stays a draft, and the sync turns the note into a map needs_review entry until a
// validated commit replaces it (design §2).
export async function reportMismatch(id) {
  const character = rowById(id);
  if (guardCommit(character) !== null) return false;
  const field = document.getElementById(reviewNoteFieldId(character.id));
  const raw = field && typeof field.value === 'string' ? field.value.trim() : '';
  if (raw === '') {
    showToast('A note is required: say what is wrong with the match.', 'error');
    return false;
  }
  if (raw.length > REVIEW_NOTE_MAX) {
    showToast(`Keep the note under ${REVIEW_NOTE_MAX} characters (it is ${raw.length}).`, 'error');
    return false;
  }
  return runAction(character, async () => {
    const reportedBy = await reviewerLogin();
    if (reportedBy === null) return false;
    // The keys merged under the note are the FRESH row's, not this browser's cache: another
    // reviewer may have confirmed the row (or had it confirmed) since the modal was opened.
    const ok = await commitValidation(character, row => ({
      ...(validationBlock(row) || {}), note: raw, reportedBy, reportedAt: nowStamp()
    }), {
      message: `Report mismatch on ${character.name} [skip ci]`,
      success: `Reported a mismatch on ${character.name}. The row stays editable.`
    });
    if (ok) {
      mismatchOpen = false;
      // The textarea belongs to this render; drop its value so a repaint cannot re-offer it.
      const after = document.getElementById(reviewNoteFieldId(character.id));
      if (after) after.value = '';
    }
    return ok;
  });
}

// RE-OPEN FOR EDITING: removes the validation block entirely (design §2: re-opening is an
// explicit action with its own commit). confirm() because it unlocks a row the whole team can
// see as verified.
export async function reopenForEditing(id) {
  const character = rowById(id);
  // The same guard every commit runs, in its "must be validated" direction.
  if (guardCommit(character, { requireValidated: true }) !== null) return false;
  if (!confirm('Re-open this validated character for editing? The validation block is removed and the row becomes editable again.')) {
    return false;
  }
  return runAction(character, () => commitValidation(character, null, {
    message: `Re-open ${character.name} for editing [skip ci]`,
    success: `Re-opened ${character.name} for editing.`
  }));
}

// REFRESH FROM BATTLE.NET (design §4). opts exists for the verification harness (poll timing);
// the UI path calls it with none.
export async function refreshFromBattleNet(id, opts = {}) {
  const character = rowById(id);
  if (!ghHasPat()) {
    showToast('Your PAT needs Actions: write to refresh from Battle.net.', 'error');
    openGhModal();
    return false;
  }
  if (!character) {
    showToast('That character is no longer in the roster.', 'error');
    return false;
  }
  if (refresh !== null) return false;
  refresh = { id: character.id, message: 'Starting…' };
  renderDetail();
  try {
    const result = await dispatchAndAwaitSync(character.name, {
      ...opts,
      onPhase: message => {
        refresh = { id: character.id, message };
        renderDetail();
      }
    });
    if (result.ok) {
      await reloadSidecars();
      renderAll();
      setLastResult(character, 'success', 'Battle.net refresh finished.');
      showToast(`Battle.net refresh finished for ${character.name}.`, 'success');
      return true;
    }
    const text = result.message || 'The refresh did not complete.';
    setLastResult(character, 'error', text);
    showToast(text, 'error');
    return false;
  } catch (err) {
    const text = 'The refresh failed before it reached GitHub.';
    setLastResult(character, 'error', text);
    showToast(text, 'error');
    return false;
  } finally {
    refresh = null;
    renderDetail();
  }
}

// ---------- candidates (read-only list; Phase 3d adds the resolve radios) ----------

// The read-only list is the fallback whenever a write path is unavailable (no PAT, or a row
// whose id cannot safely reach a handler), which is also the Phase 3c behaviour: with no PAT
// configured the modal renders exactly what it did before this file could write anything.
export const CANDIDATES_READ_ONLY_HINT = 'Confirm/resolve coming with validation';

// opts is optional and defaults to the read-only rendering: {interactive, id, chosenSlug}.
export function candidatesBlock(mapEntry, opts = {}) {
  const candidates = mapEntry && typeof mapEntry === 'object' && Array.isArray(mapEntry.candidates)
    ? mapEntry.candidates : [];
  const interactive = opts.interactive === true && handlerArg(opts.id) !== '';
  const chosen = scalarText(opts.chosenSlug);
  const radioName = `resolve-candidate-${String(opts.id == null ? '' : opts.id)}`;
  let rows = '';
  if (candidates.length === 0) {
    rows = emptyState('No candidates recorded for this character.');
  } else {
    rows = candidates.map((cand, index) => {
      if (!cand || typeof cand !== 'object') return '';
      const name = [scalarText(cand.name), scalarText(cand.surname)].filter(Boolean).join(' ');
      const slug = scalarText(cand.slug);
      const matched = scalarText(cand.matchedForm);
      const details = [slug !== '' ? slug : null, matched !== '' ? `matched: ${matched}` : null].filter(Boolean).join(' • ');
      // A candidate without a slug cannot be confirmed, so it gets no radio; it stays listed
      // because the reviewer still needs to see what Battle.net offered.
      const selectable = interactive && SLUG_RE.test(slug);
      const checked = selectable && slug === chosen;
      const radio = selectable
        ? `<input type="radio" name="${escapeHtml(radioName)}" value="${escapeHtml(slug)}"`
          + ` id="${escapeHtml(`${radioName}-${index}`)}"${checked ? ' checked' : ''}`
          + ` onchange="${escapeHtml(`selectCandidate('${handlerArg(opts.id)}', '${index}')`)}"`
          + ` class="accent-amber-400 w-3.5 h-3.5 shrink-0 bg-slate-900 border-slate-700">`
        : '';
      return `<label for="${escapeHtml(`${radioName}-${index}`)}" class="flex items-start gap-2 rounded border p-2 ${checked ? 'border-amber-500/70 bg-amber-500/10' : 'border-slate-800 bg-slate-950/50'}">
        ${radio}
        <span class="min-w-0">
          <span class="block text-[11px] text-slate-200 truncate">${escapeHtml(name !== '' ? name : (slug !== '' ? slug : 'unnamed candidate'))}</span>
          <span class="block text-[9px] text-slate-500 truncate">${escapeHtml(details)}</span>
        </span>
      </label>`;
    }).join('');
  }
  const footer = interactive
    ? '<div class="text-[10px] text-slate-500 italic">Select one candidate, then confirm it below.</div>'
    : `<div class="text-[10px] text-slate-500 italic">${escapeHtml(CANDIDATES_READ_ONLY_HINT)}</div>`;
  return section('Candidates', `<div class="space-y-1.5">${rows}${footer}</div>`);
}

// ---------- header + body ----------

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

// The character render (T2.2): avatar-sd, then avatar, then the initial-framed placeholder the
// modal showed before media existed. Both URLs go through the same host allowlist as every other
// icon, and a rejected URL renders the placeholder rather than the URL.
function avatarHtml(sidecar, name) {
  const media = sidecar && typeof sidecar === 'object' && sidecar.media
    && typeof sidecar.media === 'object' && !Array.isArray(sidecar.media) ? sidecar.media : null;
  const sd = media ? scalarText(media.avatarSd) : '';
  const small = media ? scalarText(media.avatar) : '';
  const url = isSafeIconUrl(sd) ? sd : (isSafeIconUrl(small) ? small : '');
  if (url !== '') {
    return `<img src="${escapeHtml(url)}" loading="lazy" alt="${escapeHtml(name)}" class="wow-avatar wow-border">`;
  }
  const initial = name === '' ? '?' : name.charAt(0).toUpperCase();
  return `<span class="wow-avatar wow-avatar-none cinzel" title="No character render synced">${escapeHtml(initial)}</span>`;
}

// The whole modal body for one row. Exported for the verification harness: it is pure over
// (roster row, sidecar row, map entry, badge markup), so the escaping and the section-omission
// rules can be asserted without a click.
// actionsHtml is an internal seam (and a harness seam): null renders this module's own
// review action area for the row, a string replaces it.
export function detailHtml(character, sidecar, mapEntry, lifecycle, badgeHtml = null, actionsHtml = null) {
  if (!character || typeof character !== 'object') return emptyState('No such character.');
  // The tooltip registry belongs to the markup this call produces: an index handed to a hover
  // handler must never resolve against the previous row's items.
  resetTooltipTargets();
  const hasCands = Array.isArray(mapEntry && mapEntry.candidates) && mapEntry.candidates.length > 0;
  const badge = badgeHtml || getLifecycleBadge(character, lifecycle, null, hasCands);
  const cls = scalarText(character.class);
  const classColor = classColorFor(cls);
  const faction = getFaction(character.race);
  const factionTag = faction === 'alliance'
    ? '<span class="text-[9px] font-bold uppercase tracking-wider text-sky-400 bg-blue-950/80 px-1 py-0.5 rounded border border-blue-800/60">Alliance</span>'
    : '<span class="text-[9px] font-bold uppercase tracking-wider text-red-400 bg-red-950/80 px-1 py-0.5 rounded border border-red-800/60">Horde</span>';
  const roles = [scalarText(character.role), scalarText(character.roleSec)]
    .filter(r => r !== '' && r !== 'None').join(' / ');
  const synced = formatSyncedAt(sidecar && sidecar.fetchedAt);
  const note = mapEntry && typeof mapEntry === 'object' ? scalarText(mapEntry.note) : '';
  const achievement = numberOr(sidecar && sidecar.achievementPoints);

  const fullName = displayName(sidecar, mapEntry, character);
  const header = `<div class="flex items-start justify-between gap-3 pb-2.5 border-b border-slate-800">
    <div class="flex items-start gap-2.5 min-w-0">
      ${avatarHtml(sidecar, fullName)}
      <div class="min-w-0">
      <h3 class="cinzel text-lg font-bold truncate ${classColor}">${escapeHtml(fullName)}</h3>
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

  // ---------- T2.9: the pinned footer ----------
  // The review workflow is never behind a tab: the sync note, the reviewer's own report, the
  // candidate picker and the action buttons live below the panel and outside its scroll region.
  // The footer itself scrolls internally past a cap, so a long candidate list cannot push the
  // action buttons off a short screen ("always visible" is the point, not "always fully tall").
  const noteSection = note === '' ? '' : section('Sync note',
    `<div class="rounded border border-slate-800 bg-slate-950/50 p-2 text-[11px] text-slate-300">${escapeHtml(note)}</div>`);

  const noSidecar = !sidecar && !mapEntry
    ? section('Battle.net', emptyState('No Battle.net data'))
    : '';

  const actions = actionsHtml === null ? reviewHtml(character, mapEntry, lifecycle) : actionsHtml;

  const foot = noteSection + reviewNoteSection(character)
    + candidatesIfOpen(lifecycle, mapEntry, character) + actions + noSidecar;

  return `<div class="shrink-0">${header}${body}</div>
    ${tabBarHtml()}
    ${tabPanel('gear', gearTabHtml(sidecar))}
    ${tabPanel('talents', talentsTabHtml(sidecar))}
    ${tabPanel('professions', professionsTabHtml(sidecar))}
    ${tabPanel('reputation', reputationTabHtml(sidecar))}
    ${tabPanel('stats', statsTabHtml(sidecar))}
    ${tabPanel('progress', progressTabHtml(sidecar))}
    ${foot === '' ? '' : `<div class="wow-detail-foot shrink-0 overflow-y-auto pt-2 mt-1 border-t border-slate-800">${foot}</div>`}`;
}

// The reviewer's own report, shown whenever the roster row carries one (with or without a PAT:
// it is data, not an affordance). The sync mirrors it into the map entry's note, so a reported
// row reads the same in the committed artifacts and on the card.
function reviewNoteSection(character) {
  const report = reviewReport(character);
  if (!report) return '';
  const by = report.reportedBy === '' ? 'a reviewer' : report.reportedBy;
  const when = formatSyncedAt(report.reportedAt) || report.reportedAt;
  return section('Review note', `<div class="rounded border border-amber-800/60 bg-amber-950/30 p-2 space-y-1">
    <div class="text-[11px] text-slate-200">${escapeHtml(report.note)}</div>
    <div class="text-[9px] text-slate-400">reported by ${escapeHtml(by)}${when === '' ? '' : ` · ${escapeHtml(when)}`}</div>
  </div>`);
}

// The candidates list is only reachable for a row that actually needs a human pick, so the
// flag is re-derived from the row on every render instead of trusted from the click.
//
// With a PAT the list is the resolve control rather than a read-only peek, so it is open by
// default for exactly those rows (and an entry with no candidates shows no list at all — there is
// nothing to resolve, and the Review hint says what to do instead). Without a PAT the Phase 3c
// behaviour stands: collapsed until the badge is clicked, read-only when shown, empty state and all.
// `candidatesToggled` is what both badges drive, so the card's "pick candidate" badge lands on the
// list in either mode rather than inverting it.
function candidatesIfOpen(lifecycle, mapEntry, character) {
  if (lifecycle !== LIFECYCLE.NEEDS_RESOLUTION) return '';
  const interactive = ghHasPat() && handlerArg(character && character.id) !== '';
  if (!candidatesVisible(interactive)) return '';
  const listed = mapEntry && typeof mapEntry === 'object' && Array.isArray(mapEntry.candidates)
    ? mapEntry.candidates : [];
  // A resolve control over an empty list is not a control, and the Review section already says what
  // to do with a row that has nothing to pick. The read-only list keeps its empty state exactly as
  // Phase 3c rendered it.
  if (interactive && listed.length === 0) return '';
  return interactive
    ? candidatesBlock(mapEntry, {
      interactive: true,
      id: character && character.id,
      chosenSlug: chosenFor === String(character && character.id) ? chosenSlug : null
    })
    : candidatesBlock(mapEntry);
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
  resetReviewView();
  resetDetailView();
  const character = characters.find(c => c.id === openId);
  if (!character) {
    closeDetail();
    return;
  }
  const { mapEntry } = byRosterId(openId);
  // The badge path asks for the list explicitly, in both modes. A row that does not need a pick
  // keeps the mode's default rather than being shown an empty list.
  candidatesToggled = openSection === 'candidates'
    && deriveLifecycleState(character, mapEntry) === LIFECYCLE.NEEDS_RESOLUTION ? true : null;
  renderDetail();
  modal.classList.remove('hidden');
}

export function closeDetail() {
  openId = null;
  resetReviewView();
  resetDetailView();
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
  candidatesToggled = !candidatesVisible(ghHasPat() && handlerArg(character.id) !== '');
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
  // Inside the modal the badge is the affordance that toggles the candidate list, so it
  // becomes a button; the card keeps it a plain (clickable) span.
  const hasCands = mapEntry != null && Array.isArray(mapEntry.candidates) && mapEntry.candidates.length > 0;
  const badge = lifecycle === LIFECYCLE.NEEDS_RESOLUTION && hasCands
    ? `<button type="button" onclick="toggleDetailCandidates()" title="Toggle candidate list" class="cursor-pointer">${getLifecycleBadge(character, lifecycle, null, true)}</button>`
    : getLifecycleBadge(character, lifecycle, null, hasCands);
  // A repaint replaces every node, so the panel's scroll offset and any open tooltip are
  // re-established by hand: without this, a background Refresh would throw a reader back to the
  // top of a long reputation list with the tooltip still showing a row that no longer exists.
  const panelId = tabPanelId(detailActiveTab());
  const before = document.getElementById(panelId);
  const scrollTop = before ? Number(before.scrollTop) || 0 : 0;
  dismissDetailTooltip();
  content.innerHTML = detailHtml(character, sidecar, mapEntry, lifecycle, badge);
  const after = document.getElementById(panelId);
  if (after && scrollTop > 0) after.scrollTop = scrollTop;
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
window.confirmMatch = confirmMatch;
window.selectCandidate = selectCandidate;
window.confirmSelectedCandidate = confirmSelectedCandidate;
window.toggleMismatchForm = toggleMismatchForm;
window.reportMismatch = reportMismatch;
window.reopenForEditing = reopenForEditing;
window.refreshFromBattleNet = refreshFromBattleNet;
window.setDetailTab = setDetailTab;
window.detailTabKeydown = detailTabKeydown;
window.showDetailTooltip = showDetailTooltip;
window.hideDetailTooltip = hideDetailTooltip;
