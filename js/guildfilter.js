// guildfilter.js — the guild chip row under the Squad Filter (roster-ui-tasks Phase C).
//
// This module owns the chip markup and the sidecar join that feeds it; it owns no filter
// state. The selection lives in state.js (setGuildFilter) and the predicate runs inside
// getFilteredCharacters(), so the guild chips compose with the track filter and the search
// query through the one render path — nothing here repaints a panel by hand.
//
// It is its own module rather than more code in state.js for the same reason the search
// provider is: knowing a row's guild means asking the Battle.net sidecars, and state.js is
// documented as the leaf that imports nothing. So the sidecar facts come in through
// registerGuildProvider() and the chip row is painted from state.js's derived buckets.

import { byRosterId } from './data.js';
import { escapeHtml } from './util.js';
import {
  GUILD_FILTER_ALL,
  GUILD_GUILDLLESS,
  GUILD_UNKNOWN,
  getCharacters,
  getGuildBucket,
  getGuildFilter,
  getIncludedGuild,
  guildBuckets,
  guildKey,
  guildNameEquals,
  guildScopeIsOverridden,
  registerGuildProvider,
  registerRenderHook,
  resetIncludedGuild,
  setGuildFilter,
  setIncludedGuild
} from './state.js';

// The sidecar half of the join. It degrades to `{ guild: '', hasSidecar: false }` when
// data/character-data.json is missing or has no row for this id, which is exactly the
// Unknown bucket: no sidecar to ask, so the UI says it does not know rather than guessing
// "Guildless" (a member with no guild is a claim; having never looked is not).
registerGuildProvider(character => {
  const id = character && typeof character === 'object' ? character.id : null;
  const { sidecar } = byRosterId(id);
  return {
    guild: sidecar && typeof sidecar.guild === 'string' ? sidecar.guild : '',
    hasSidecar: Boolean(sidecar)
  };
});

// The same utilities the Squad Filter chips are written with, stated once so the active and
// inactive pair cannot drift apart, and so the deploy's Tailwind scan sees every token in the
// source (these are all already in index.html: no regenerated stylesheet for this row).
// The chips deliberately do NOT carry `filter-btn`: setFilter() repaints every `.filter-btn`
// in the document and re-highlights exactly one track, which would blank a selected guild chip
// on the next track click.
const CHIP_BASE = 'px-1.5 py-1 rounded text-[11px] font-bold transition-all text-center';
const CHIP_OFF = `${CHIP_BASE} text-slate-400 hover:text-white border border-transparent`;
const CHIP_ON = `${CHIP_BASE} bg-amber-500/20 text-amber-300 border border-amber-500/40`;

// Long guild names are truncated in the string rather than by a CSS width: the full name
// stays available in the title, and no new arbitrary utility enters the stylesheet.
const GUILD_LABEL_MAX = 16;

function chipsEl() {
  return document.getElementById('guild-filter-chips');
}

// Exactly one thing crosses from data into the handler: the chip's index. A guild name is sidecar
// data, and an apostrophe inside a quoted inline-handler argument breaks the handler in a real
// browser just as surely as it does in the harness — so names are never handler arguments.
function chipHtml(index, bucket, active) {
  const label = bucket === GUILD_FILTER_ALL ? 'All' : bucket;
  const shown = escapeHtml(label.length > GUILD_LABEL_MAX ? `${label.slice(0, GUILD_LABEL_MAX - 1)}…` : label);
  return `<button type="button" id="guild-chip-${index}" title="${escapeHtml(label)}"`
    + ` onclick="setGuildFilterIndex(${index})"`
    + ` class="guild-filter-btn ${active ? CHIP_ON : CHIP_OFF}">${shown}</button>`;
}

// Rebuilt on every repaint, because the buckets come from data that arrives after the first
// paint (the sidecars, then data/characters.json, then the Contents API pull): a chip row
// written once would go stale the moment a guild appeared or disappeared.
export function renderGuildFilter() {
  renderGuildScope();
  const el = chipsEl();
  if (!el || typeof el.classList === 'undefined') return;
  const selected = getGuildFilter();
  const buckets = guildBuckets();
  // A chip can vanish under an active selection — sidecars land, a pull renames a guild, the
  // last row of a guild is deleted. The track filter cannot hit that (its chips are in the
  // markup), so the dynamic row says what is actually clickable: fall back to All instead of
  // holding a selection with no chip and an empty grid. setGuildFilter() repaints, and this
  // returns without writing the stale row; 'all' is always a valid bucket, so it settles there.
  if (selected !== GUILD_FILTER_ALL && buckets.indexOf(selected) === -1) {
    setGuildFilter(GUILD_FILTER_ALL);
    return;
  }
  const chips = [chipHtml(0, GUILD_FILTER_ALL, selected === GUILD_FILTER_ALL)];
  buckets.forEach((bucket, i) => chips.push(chipHtml(i + 1, bucket, selected === bucket)));
  el.innerHTML = chips.join('');
}

// Index 0 is All, then the buckets in guildBuckets() order. Re-derived here rather than
// captured in the markup, so a click always addresses the row the user is looking at; an
// index out of range (a repaint between paint and click) falls back to All.
export function setGuildFilterIndex(index) {
  const i = Number(index);
  const buckets = guildBuckets();
  const bucket = Number.isInteger(i) && i > 0 && i <= buckets.length ? buckets[i - 1] : GUILD_FILTER_ALL;
  setGuildFilter(bucket);
}

// ---------- the guild scope input (owner request, 2026-11-10) ----------
// The chips say which bucket of THIS roster to look at; the input says which guild this roster IS.
// Typing a name and pressing Set (or Enter) scopes the roster to it, which both limits the chips to
// All / Guildless / <that guild> and removes every other guild's characters from every panel — the
// scope is a visibility rule inside state.getFilteredCharacters(), so the grid, the party pool and
// the distribution panels cannot disagree about it.
//
// The value lives in state.js (this browser's localStorage override over the committed
// data/guild-config.json default); this module only paints the control and forwards the edit, which
// is the same split the chips already use.

function scopeInputEl() {
  return document.getElementById('guild-scope-input');
}

// What renderGuildScope() last wrote into the field, or null before its first paint. This is what
// distinguishes "the state moved" from "the owner is typing" — see renderGuildScope().
let scopePaintedValue = null;

// Every guild name the roster knows — sidecar guild, the guild an auto-added row carries — raw,
// NOT scope-filtered: the point of the list is also to let you pick a guild the scope currently
// hides, which is how a guild comes back. Synthetics (Guildless/Unknown) are not guilds.
function knownGuildNames() {
  const names = new Map();
  getCharacters().forEach(character => {
    const bucket = getGuildBucket(character);
    if (bucket === GUILD_GUILDLLESS || bucket === GUILD_UNKNOWN) return;
    const key = guildKey(bucket);
    if (!names.has(key)) names.set(key, bucket);
  });
  const current = getIncludedGuild();
  if (current !== '' && !names.has(guildKey(current))) names.set(guildKey(current), current);
  return [...names.keys()].sort().map(key => names.get(key));
}

function renderGuildScope() {
  const input = scopeInputEl();
  const list = document.getElementById('guild-scope-list');
  const note = document.getElementById('guild-scope-note');
  const reset = document.getElementById('guild-scope-reset');
  const scope = getIncludedGuild();
  if (list && typeof list.classList !== 'undefined') {
    // A datalist, not a <select>: the owner asked to both type and pick. The names come from the
    // roster, so the list cannot offer a guild nobody is in — and it still offers the current scope
    // even when the scope just emptied it, so the value on screen is always selectable.
    list.innerHTML = knownGuildNames()
      .map(name => `<option value="${escapeHtml(name)}"></option>`)
      .join('');
  }
  if (input && typeof input.classList !== 'undefined') {
    // Follow the state only while the field holds what this function last put there. Checking focus
    // is not enough: clicking anywhere else (the search box repaints through renderAll) blurs the
    // field, and a typed-but-unapplied name would silently revert to the current scope. Set, Enter
    // and ↺ all land here with the field equal to the scope or with the state changed, so each of
    // them still repaints the field.
    const untouchedSincePaint = scopePaintedValue === null || input.value === scopePaintedValue;
    if (input.value === scope || untouchedSincePaint) {
      input.value = scope;
      scopePaintedValue = scope;
    }
  }
  if (reset && typeof reset.classList !== 'undefined') {
    reset.classList.toggle('hidden', !guildScopeIsOverridden());
    reset.title = 'Back to data/guild-config.json (currently '
      + (getIncludedGuild() === '' ? 'no guild' : getIncludedGuild()) + ')';
  }
  if (note && typeof note.classList !== 'undefined') {
    const hidden = hiddenByScopeCount();
    if (scope === '') {
      note.innerHTML = 'No guild selected — every guild shows';
      note.title = 'Type a guild above (or set includedGuild in data/guild-config.json) to scope '
        + 'the roster to it';
    } else {
      note.innerHTML = `Included: <span class="text-amber-300 font-semibold">${escapeHtml(scope)}</span>`
        + `<span class="text-slate-600"> ·</span> ${hidden} other-guild row(s) hidden`
        + (guildScopeIsOverridden() ? '<span class="text-slate-600"> ·</span> this browser'
          : '<span class="text-slate-600"> ·</span> guild-config.json');
      note.title = 'Rows in any other guild are out of the roster list, the party pool and the '
        + 'stats until the scope changes.';
    }
  }
}

// Rows the scope currently hides. Counted from every row's raw bucket, so the note answers
// "what is this setting costing me?" rather than restating what is visible.
function hiddenByScopeCount() {
  if (getIncludedGuild() === '') return 0;
  let hidden = 0;
  getCharacters().forEach(character => {
    const bucket = getGuildBucket(character);
    if (bucket === GUILD_GUILDLLESS || bucket === GUILD_UNKNOWN) return;
    if (!guildNameMatches(getIncludedGuild(), bucket)) hidden += 1;
  });
  return hidden;
}

// THE state.js comparison, not a second one: the count in the note and the rows state.js admits
// have to agree, or the note would say "1 hidden" while a chip showed the row.
function guildNameMatches(a, b) {
  return guildNameEquals(a, b);
}

// The Set button. An empty field is an answer, not a no-op: it unscopes this browser (state.js
// writes the empty string, which outranks the committed default until ↺ clears the override).
export function applyGuildScope() {
  const input = scopeInputEl();
  setIncludedGuild(input && typeof input.value === 'string' ? input.value : '');
}

// Enter in the field does what the button next to it does.
export function onGuildScopeKey(event) {
  const key = event && typeof event === 'object' ? event.key : '';
  if (key === 'Enter') {
    if (typeof event.preventDefault === 'function') event.preventDefault();
    applyGuildScope();
  }
}

// Drop this browser's override and take the committed default back.
export function clearGuildScopeOverride() {
  resetIncludedGuild();
}

registerRenderHook(renderGuildFilter);

// Inline on*="…" handlers resolve against the global scope.
window.setGuildFilterIndex = setGuildFilterIndex;
window.applyGuildScope = applyGuildScope;
window.onGuildScopeKey = onGuildScopeKey;
window.clearGuildScopeOverride = clearGuildScopeOverride;
