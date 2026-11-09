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
  getGuildFilter,
  guildBuckets,
  registerGuildProvider,
  registerRenderHook,
  setGuildFilter
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

registerRenderHook(renderGuildFilter);

// Inline on*="…" handlers resolve against the global scope.
window.setGuildFilterIndex = setGuildFilterIndex;
