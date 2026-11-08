// search.js — the search box beside the Squad Filter (armory-tasks 3.4).
//
// This module owns the input, the debounce and the clear affordance; it owns no filter
// state. The query itself lives in state.js (setQuery), so the search composes with the
// track filter inside getFilteredCharacters() and every panel follows it through the one
// render dispatcher — nothing here repaints a panel by hand.

import { setQuery } from './state.js';

// Keystrokes are coalesced for this window: an instant-feeling box that does not repaint
// the whole page per character typed. Stated once so the harness can assert the value.
export const SEARCH_DEBOUNCE_MS = 120;

let timer = null;

function inputEl() {
  return document.getElementById('search-input');
}

function clearBtnEl() {
  return document.getElementById('clear-search');
}

// The ✕ only exists while there is something to clear.
function syncClearButton(value) {
  const btn = clearBtnEl();
  if (!btn || typeof btn.classList === 'undefined') return;
  if (String(value == null ? '' : value) === '') btn.classList.add('hidden');
  else btn.classList.remove('hidden');
}

// The synchronous half of the path, split out so the debounce is testable apart from the
// query it applies. setQuery() repaints through state.js's own dispatcher, so this is the
// whole repaint — search.js never calls a panel renderer.
export function applyQuery(value) {
  syncClearButton(value);
  setQuery(value);
}

export function onSearchInput(event) {
  const input = inputEl();
  const value = event && event.target && typeof event.target.value === 'string'
    ? event.target.value
    : (input ? input.value : '');
  if (timer !== null) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    applyQuery(value);
  }, SEARCH_DEBOUNCE_MS);
}

export function clearSearch() {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
  const input = inputEl();
  if (input) input.value = '';
  applyQuery('');
}

// Inline on*="…" handlers resolve against the global scope.
window.onSearchInput = onSearchInput;
window.clearSearch = clearSearch;
