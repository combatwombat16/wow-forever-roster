// verify_search_detail.mjs — Phase 3c ESM smoke harness.
//
// Imports the real ES modules under a small DOM/localStorage/fetch stub and asserts the
// behaviour the Phase 3c task promises, in the order the UI would hit it:
//
//   [1] search AND track composition through state.getFilteredCharacters(), all four tracks
//       plus the empty case, against an expectation computed here from the JSON files
//   [2] every panel repaints on a query change (roster, race, class, professions, party
//       pool + synergy) — no panel is allowed to keep stale numbers
//   [3] the search box plumbing: ~120 ms debounce, clear (✕) button, clearSearch()
//   [4] card click opens the detail modal; ✓/✎/✕ keep working without opening it; the
//       "pick candidate" badge opens it straight to the candidates
//   [5] detail modal for sidecar-present / sidecar-absent / fetch_error rows, gear icon host
//       allowlisting (foreign-host + javascript: + look-alike host), gear/progress section
//       omission, escaping, ESC and backdrop close
//   [6] no localStorage growth: the only keys ever written stay the roster and the PAT config
//
// The [5] fixtures (including the XSS / foreign-icon payloads) are written to a fresh /tmp
// directory and served through the fetch stub — nothing is ever written into the repo's
// data/. XSS payloads only ever appear as escaped string fixtures.
//
//   node .pi/scripts/verify_search_detail.mjs [repo-root]

import fs from 'fs';
import os from 'os';
import path from 'path';
import { mkdtempSync } from 'fs';
import { pathToFileURL } from 'url';

const ROOT = path.resolve(process.argv[2] || '.');

let passes = 0;
let fails = 0;
const ok = (cond, msg) => {
  if (cond) { passes++; console.log(`  PASS ${msg}`); }
  else { fails++; console.log(`  FAIL ${msg}`); }
};

// ------------------------------------------------------------------ DOM stub
//
// Just faithful enough to run the app and dispatch clicks: elements have a token-backed
// classList, inline handlers are recovered from innerHTML so a dispatched click bubbles the
// way a browser's would, and the handlers are *interpreted* (fn('arg') / fn(event)) rather
// than eval'd.

function decodeEntities(text) {
  return String(text)
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

class El {
  constructor(id = '', tag = 'div') {
    this.id = id;
    this.tagName = String(tag).toUpperCase();
    this._html = '';
    this.textContent = '';
    this.innerText = '';
    this.title = '';
    this.value = '';
    this.attrs = {};
    this.listeners = {};
    this.parent = null;
    this.children = [];
    this._classes = new Set();
    this.style = {};
  }

  get className() { return [...this._classes].join(' '); }
  set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); }

  get classList() {
    const self = this;
    return {
      add: (...names) => names.forEach(n => self._classes.add(n)),
      remove: (...names) => names.forEach(n => self._classes.delete(n)),
      contains: n => self._classes.has(n),
      toggle: (n, force) => {
        const on = force === undefined ? !self._classes.has(n) : Boolean(force);
        if (on) self._classes.add(n); else self._classes.delete(n);
        return on;
      },
    };
  }

  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = String(value);
    this.children = [];
    // Recover the elements that carry inline handlers so a click on them can be dispatched
    // with the same bubbling the browser would give (card → its buttons → the grid).
    for (const match of this._html.matchAll(/<(button|span|a|div)\b([^>]*)>/gi)) {
      const onclick = /\bonclick="([^"]*)"/i.exec(match[2]);
      if (!onclick) continue;
      const child = new El('', match[1]);
      child.attrs.onclick = decodeEntities(onclick[1]);
      child.parent = this;
      this.children.push(child);
    }
  }

  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] === undefined ? null : this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  // Enough focus support to assert where the keyboard cursor went: the roving-tabindex tab bar
  // moves focus to the *new* button after every repaint, and that is unobservable without this.
  focus() { documentStub.activeElement = this; }
  appendChild(child) { child.parent = this; this.children.push(child); this._html += child._html; return child; }
  querySelectorAll(selector) {
    if (selector === 'button') return this.children.filter(c => c.tagName === 'BUTTON');
    return [];
  }
  closest() { return null; }
}

const registry = new Map();
function byId(id) {
  if (!registry.has(id)) registry.set(id, new El(id));
  return registry.get(id);
}

const markup = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8').replace(/<script[\s\S]*?<\/script>/g, '');
// Seed the registry from the markup, classes included, so "is this hidden?" answers the same
// question the browser would (#detail-modal ships with `hidden` on it).
for (const tag of markup.matchAll(/<(\w+)([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
  const element = byId(tag[3]);
  const classes = /\bclass="([^"]*)"/i.exec(tag[2]);
  element.className = classes ? decodeEntities(classes[1]) : '';
}

const documentStub = {
  getElementById: byId,
  querySelectorAll: () => [],
  createElement: tag => new El('', tag),
  activeElement: null,
  listeners: {},
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
};

const storageWrites = [];
const store = new Map();

// ------------------------------------------------------------------ globals
globalThis.window = globalThis;
globalThis.document = documentStub;
globalThis.localStorage = {
  getItem: key => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => { store.set(key, String(value)); storageWrites.push(key); },
  removeItem: key => store.delete(key),
};
globalThis.alert = () => {};
globalThis.confirm = () => false;
globalThis.prompt = () => null;

// Fetch resolves relative paths against the current source directory: the repo for the
// composition run, the /tmp fixture directory afterwards.
let sourceDir = ROOT;
globalThis.fetch = async url => {
  const rel = String(url).replace(/^\.?\//, '');
  const file = path.join(sourceDir, rel);
  if (!fs.existsSync(file)) return { ok: false, status: 404, json: async () => null };
  return { ok: true, status: 200, json: async () => JSON.parse(fs.readFileSync(file, 'utf8')) };
};

// ------------------------------------------------------------------ click dispatch
function splitArgs(raw) {
  const out = [];
  let current = '';
  let quote = null;
  for (const ch of String(raw)) {
    if (quote) { current += ch; if (ch === quote) quote = null; continue; }
    if (ch === "'" || ch === '"') { quote = ch; current += ch; continue; }
    if (ch === ',') { out.push(current.trim()); current = ''; continue; }
    current += ch;
  }
  if (current.trim() !== '') out.push(current.trim());
  return out;
}

// Inline handlers are `fn(arg, …)` calls against the global scope: interpreted, never eval'd.
function runInline(code, event) {
  const call = /^\s*([A-Za-z_$][\w$]*)\((.*)\)\s*;?\s*$/.exec(code);
  if (!call) throw new Error(`unparsed inline handler: ${code}`);
  const fn = globalThis[call[1]];
  if (typeof fn !== 'function') throw new Error(`inline handler ${call[1]}() is not global`);
  const args = splitArgs(call[2]).map(arg => {
    if (arg === 'event') return event;
    const quoted = /^'([\s\S]*)'$/.exec(arg) || /^"([\s\S]*)"$/.exec(arg);
    if (quoted) return quoted[1];
    if (/^-?\d+(\.\d+)?$/.test(arg)) return Number(arg);
    throw new Error(`unparsed inline handler argument: ${arg}`);
  });
  return fn(...args);
}

function fire(node, extra = {}) {
  const event = { type: 'click', target: node, _stopped: false, stopPropagation() { this._stopped = true; } };
  Object.assign(event, extra);
  let current = node;
  while (current) {
    if (current.attrs && current.attrs.onclick) runInline(current.attrs.onclick, event);
    (current.listeners.click || []).forEach(fn => fn(event));
    if (event._stopped) break;
    current = current.parent;
  }
  return event;
}

function pressKey(key) {
  (documentStub.listeners.keydown || []).forEach(fn => fn({ key }));
}

const hidden = id => byId(id).classList.contains('hidden');
const cards = () => byId('character-grid').children.filter(c => c.attrs['data-character-id'] !== undefined);
const cardFor = id => cards().find(c => c.attrs['data-character-id'] === String(id));

// ------------------------------------------------------------------ boot
const moduleUrl = name => pathToFileURL(path.join(ROOT, 'js', name)).href;
await import(moduleUrl('main.js'));
await new Promise(resolve => setTimeout(resolve, 60));

const state = await import(moduleUrl('state.js'));
const dataMod = await import(moduleUrl('data.js'));
const detail = await import(moduleUrl('detail.js'));
const searchMod = await import(moduleUrl('search.js'));
const partyMod = await import(moduleUrl('party.js'));

const rosterDoc = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'characters.json'), 'utf8'));
const rosterChars = Array.isArray(rosterDoc) ? rosterDoc : rosterDoc.characters;
const charDataDoc = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'character-data.json'), 'utf8'));
const mapDoc = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'battle-net-map.json'), 'utf8'));

console.log(`\n[0] boot against ${path.relative(process.cwd(), ROOT) || ROOT}`);
ok(state.getCharacters().length === rosterChars.length,
  `data/characters.json loaded into state (${state.getCharacters().length} chars)`);
ok(typeof detail.isSafeIconUrl === 'function' && typeof state.setQuery === 'function'
  && typeof searchMod.applyQuery === 'function', 'search + detail entry points exported');

// ---------------------------------------------------- independent expectation
// Computed here from the raw files, so a change to the app's own matching has to keep up.
const SIDECAR_GUILD = id => {
  const row = charDataDoc.characters[String(id)];
  return row && typeof row.guild === 'string' ? [row.guild] : [];
};
const MAP_NAMES = id => {
  const entry = mapDoc.entries[String(id)];
  if (!entry || typeof entry !== 'object') return [];
  return ['bnetName', 'surname', 'slug']
    .map(key => entry[key])
    .filter(v => typeof v === 'string' || typeof v === 'number')
    .map(v => String(v));
};
function expectedIds(track, query) {
  const terms = String(query).toLowerCase().split(/\s+/).filter(Boolean);
  return rosterChars.filter(c => {
    if (track !== 'all' && c.track !== track) return false;
    if (terms.length === 0) return true;
    const fields = [c.name, c.player, c.class, c.role, c.roleSec]
      .concat(SIDECAR_GUILD(c.id), MAP_NAMES(c.id))
      .map(v => String(v).toLowerCase());
    return terms.every(term => fields.some(f => f.includes(term)));
  }).map(c => c.id);
}

const TRACKS = ['all', 'flowmasters', 'family', 'alts'];
const guildOf = Object.values(charDataDoc.characters).map(r => r && r.guild).find(g => typeof g === 'string' && g !== '');
const surnameOf = Object.values(mapDoc.entries).map(e => e.surname).find(s => typeof s === 'string' && s !== '');
const slugOf = Object.values(mapDoc.entries).map(e => e.slug).find(s => typeof s === 'string' && s !== '');
const classOf = rosterChars[0].class;
const playerOf = rosterChars[0].player.slice(0, 4);
const roleOf = 'tank';

// ---------------------------------------------------------------- 1. composition
console.log('\n[1] search AND track composition (state.js is the only filter path)');
const QUERIES = ['', classOf, playerOf, guildOf, surnameOf, slugOf,
  `${classOf} ${playerOf}`.toLowerCase(), roleOf, 'zzzz-no-such-character'];
let compositionFails = 0;
const checkedCombinations = [];
for (const track of TRACKS) {
  for (const query of QUERIES) {
    state.setFilter(track);
    state.setQuery(query);
    const want = expectedIds(track, query).slice().sort();
    const got = state.getFilteredCharacters().map(c => c.id).sort();
    const same = want.length === got.length && want.every((id, i) => id === got[i]);
    if (!same) {
      compositionFails++;
      console.log(`      track=${track} query="${query}" want ${want.length} got ${got.length}`);
    }
    checkedCombinations.push(`${track}/${query || '∅'}`);
  }
}
ok(compositionFails === 0,
  `${checkedCombinations.length} track×query combinations match an independent tally (${TRACKS.length} tracks × ${QUERIES.length} queries)`);
ok(expectedIds('all', surnameOf).length > 0 && expectedIds('alts', 'zzzz-no-such-character').length === 0,
  `the fixture set exercises both a sidecar-only hit (${expectedIds('all', surnameOf).length}) and the empty case`);

// A query that spans tracks, then narrowed by the track filter: proves AND composition on
// both axes rather than "search replaced the filter" or "filter replaced the search".
const spanQuery = rosterChars.length > 2 ? rosterChars[0].player : '';
const spanTrackHits = TRACKS.filter(t => expectedIds(t, spanQuery).length > 1);
state.setFilter('all');
state.setQuery(spanQuery);
const spanAll = state.getFilteredCharacters().length;
state.setFilter('family');
const spanFamily = state.getFilteredCharacters().length;
ok(spanTrackHits.length > 0 && spanAll >= spanFamily && spanAll === expectedIds('all', spanQuery).length
  && spanFamily === expectedIds('family', spanQuery).length,
  `player "${spanQuery}" narrows ${spanAll} → ${spanFamily} when the track filter is added`);

// ---------------------------------------------------------------- 2. panels repaint
console.log('\n[2] every panel follows a query change through renderAll()');
function panelSnapshot() {
  return {
    roster: byId('character-grid').innerHTML,
    count: byId('roster-count').innerText,
    race: byId('race-stats').innerHTML,
    raceSummary: byId('race-summary').innerText,
    cls: byId('class-stats').innerHTML,
    classSummary: byId('class-summary').innerText,
    professions: byId('profession-tags').innerHTML,
    coverage: byId('prof-coverage-pct').innerText,
  };
}

state.setFilter('all');
state.setQuery('');
const before = panelSnapshot();
const narrowQuery = classOf.toLowerCase();
state.setQuery(narrowQuery);
const after = panelSnapshot();
const pool = rosterChars.filter(c => c.class.toLowerCase().includes(narrowQuery));

const raceRows = new Set(pool.map(c => c.race)).size;
const classRows = new Set(pool.map(c => c.class)).size;
const coveredProfs = new Set(pool.flatMap(c => [c.prof1, c.prof2]).filter(p => p && p !== 'None')).size;
ok(before.roster !== after.roster && cards().length === pool.length
  && after.roster.includes(pool[0].name),
  `roster grid repaints on query change (${cards().length} cards for "${narrowQuery}")`);
ok(after.count === `${pool.length} Chars`, `roster count follows the query: "${after.count}"`);
ok(before.race !== after.race && after.raceSummary === `${raceRows} Races / ${pool.length} Chars`,
  `race panel repaints: "${after.raceSummary}"`);
ok(before.cls !== after.cls && after.classSummary === `${classRows} Classes / ${pool.length} Chars`,
  `class panel repaints: "${after.classSummary}"`);
ok(before.professions !== after.professions &&
  after.coverage === `${Math.round(coveredProfs / 9 * 100)}% Guild Coverage (${coveredProfs}/9)`,
  `profession matrix repaints: "${after.coverage}"`);

// Party pool: Auto-Form draws from the filtered pool only.
state.setFilter('all');
state.setQuery('');
partyMod.clearParty();
const oneChar = rosterChars.find(c => c.role === 'DPS');
state.setQuery(oneChar.name.toLowerCase().slice(0, 6));
partyMod.autoFillDungeonGroup();
const pooled = partyMod.party.filter(Boolean);
ok(pooled.length > 0 && pooled.every(p => p.name.toLowerCase().includes(oneChar.name.toLowerCase().slice(0, 6))),
  `party pool obeys the query (${pooled.length} slots filled, all matching "${state.getQuery()}")`);
// Synergy follows the same dispatcher: its content is the party's, and the party was just
// formed from the query-filtered pool, so the score must equal an independent tally of it.
const partyClasses = pooled.map(p => p.class);
const activeBuffs = [
  partyClasses.includes('Shaman'), partyClasses.includes('Priest'), partyClasses.includes('Mage'),
  partyClasses.includes('Paladin'), partyClasses.includes('Druid'),
  partyClasses.includes('Druid') || partyClasses.includes('Warlock'),
  partyClasses.includes('Mage') || partyClasses.includes('Rogue'),
].filter(Boolean).length;
ok(byId('synergy-score').textContent === `${activeBuffs} / 7 Active`
  && byId('buff-checklist').innerHTML.includes('Bloodlust'),
  `synergy repaints from the query-filtered party: "${byId('synergy-score').textContent}"`);

partyMod.clearParty();
state.setQuery('');

// Empty result: existing empty-state styling, in both the grid and the stat panels.
state.setFilter('all');
state.setQuery('zzzz-no-such-character');
ok(cards().length === 0 && byId('character-grid').innerHTML.includes('text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800'),
  'empty search keeps the existing empty-state styling in the roster');
ok(byId('character-grid').innerHTML.includes('zzzz-no-such-character'), 'empty search echoes the query back');
ok(byId('race-stats').innerHTML.includes('No characters in this filter.')
  && byId('class-stats').innerHTML.includes('No characters in this filter.'),
  'empty search shows the stat panels placeholder');
ok(byId('character-grid').innerHTML.includes('&lt;') === false, 'placeholder is not double-escaped');
state.setQuery('');

// The query itself is inert markup.
const XSS = '<img src=x onerror=alert(1)>';
state.setQuery(XSS);
const echoed = [...registry.values()].map(el => el.innerHTML).join('');
ok(!echoed.includes('<img src=x'), 'a query containing markup cannot open a tag in any panel');
state.setQuery('');

// ---------------------------------------------------------------- 3. search box
console.log('\n[3] search box plumbing');
ok(searchMod.SEARCH_DEBOUNCE_MS === 120, `SEARCH_DEBOUNCE_MS is 120 (${searchMod.SEARCH_DEBOUNCE_MS})`);
ok(markup.includes('id="search-input"') && /id="search-input"[\s\S]{0,400}?oninput="onSearchInput\(event\)"/.test(markup),
  'index.html declares #search-input wired to onSearchInput(event)');
ok(markup.includes('id="clear-search"') && /id="clear-search"[\s\S]{0,300}?onclick="clearSearch\(\)"/.test(markup),
  'index.html declares the #clear-search (✕) button');
ok(markup.indexOf('id="search-input"') > markup.indexOf('Squad Filter') && markup.indexOf('id="search-input"') < markup.indexOf('id="character-grid"'),
  'the search box sits in the Squad Filter panel, above the roster list');

const input = byId('search-input');
input.value = 'dr';
searchMod.onSearchInput({ target: input });
ok(state.getQuery() === '', 'keystrokes are not applied synchronously (debounced)');
ok(hidden('clear-search'), 'clear button state still pending before the debounce fires');
input.value = 'dru';
searchMod.onSearchInput({ target: input });
input.value = 'drui';
searchMod.onSearchInput({ target: input });
await new Promise(resolve => setTimeout(resolve, 260));
ok(state.getQuery() === 'drui', `only the last keystroke survives the debounce ("${state.getQuery()}")`);
ok(!hidden('clear-search'), 'clear button is revealed once a query is active');

input.value = '';
searchMod.clearSearch();
ok(state.getQuery() === '' && hidden('clear-search'), 'clearSearch() empties the query and hides the ✕');
await new Promise(resolve => setTimeout(resolve, 200));
ok(state.getQuery() === '', 'a pending debounced keystroke is cancelled by clearSearch()');

// ---------------------------------------------------------------- 4. card click
console.log('\n[4] card click opens the modal; micro-actions still work');
state.setFilter('all');
state.setQuery('');
const detailHiddenBefore = hidden('detail-modal');
const someCard = cards()[0];
const someId = someCard.attrs['data-character-id'];
ok(detailHiddenBefore && cards().length > 0, 'detail modal starts hidden with cards rendered');
fire(someCard);
ok(!hidden('detail-modal') && detail.isDetailOpen(), 'clicking a roster card opens #detail-modal');
ok(byId('detail-content').innerHTML.includes(rosterChars.find(c => c.id === someId).name),
  'the modal shows the clicked row');
detail.closeDetail();
ok(hidden('detail-modal') && !detail.isDetailOpen(), 'closeDetail() hides it again');
detail.openCharacterDetail('no-such-id');
ok(hidden('detail-modal') && !detail.isDetailOpen(), 'an unknown id does not open the modal');

// ✓ party toggle: works, and does not open the modal.
const toggle = cards()[0].children.find(c => c.tagName === 'BUTTON' && /togglePartyMember/.test(c.attrs.onclick || ''));
fire(toggle);
ok(hidden('detail-modal'), 'the party toggle (✓/+) does not open the modal (stopPropagation)');
ok(partyMod.party.filter(Boolean).length === 1, 'the party toggle still added a member');
fire(toggle);
ok(partyMod.party.filter(Boolean).length === 0, 'and still removes it');

// ✎ edit and ✕ delete: work, and do not open the modal.
// Pick an EDITABLE card: the committed roster legitimately carries validated rows whose ✎
// refuses by design (3d gating) — the harness must not depend on which rows a human validated.
const editableCard = cards().find(c => c.children.some(b => b.tagName === 'BUTTON'
  && /editCharacter/.test(b.attrs.onclick || '') && !('disabled' in b.attrs))) || cards()[0];
const editBtn = editableCard.children.find(c => c.tagName === 'BUTTON' && /editCharacter/.test(c.attrs.onclick || ''));
const editModalWasHidden = hidden('char-modal');
fire(editBtn);
ok(editModalWasHidden && !hidden('char-modal'), '✎ opens the Add/Edit modal');
ok(hidden('detail-modal'), '✎ does not also open the detail modal');
globalThis.closeModal();
ok(hidden('char-modal'), 'closeModal() restores it');
const deleteBtn = editableCard.children.find(c => c.tagName === 'BUTTON' && /deleteCharacter/.test(c.attrs.onclick || ''));
fire(deleteBtn);
ok(hidden('detail-modal') && state.getCharacters().length === rosterChars.length,
  '✕ stays a delete (declined here) and never opens the detail modal');

// The "pick candidate" badge path, against the committed map (it carries needs_review rows).
const needsPick = Object.values(mapDoc.entries)
  .filter(e => e && (e.status === 'needs_review' || e.status === 'ambiguous')).length;
const badgeCards = cards().filter(c => c.children.some(x => /openCandidateList/.test(x.attrs.onclick || '')));
ok(badgeCards.length === needsPick && needsPick > 0,
  `exactly the ${needsPick} rows needing a human pick render their badge as a candidate handler`);
const pickBadge = badgeCards[0].children.find(x => /openCandidateList/.test(x.attrs.onclick || ''));
fire(pickBadge);
ok(!hidden('detail-modal') && byId('detail-content').innerHTML.includes('Confirm/resolve coming with validation'),
  'clicking that badge opens the modal straight at the candidate list (not the plain view)');
ok(byId('detail-content').innerHTML.includes('>Candidates<'), 'the candidate section is the one shown');
detail.closeDetail();

// ESC + backdrop.
fire(cards()[0]);
ok(!hidden('detail-modal'), 'reopened for the close-path checks');
pressKey('Escape');
ok(hidden('detail-modal'), 'ESC closes the detail modal');
fire(cards()[0]);
const backdropEvent = fire(byId('detail-modal'));
ok(hidden('detail-modal'), 'a click on the backdrop itself closes the detail modal');
fire(cards()[0]);
const inside = new El('', 'div');
inside.parent = byId('detail-modal');
fire(inside);
ok(!hidden('detail-modal'), 'a click inside the panel does not close it');
pressKey('Enter');
ok(!hidden('detail-modal'), 'other keys do not close it');
detail.closeDetail();

// ------------------------------------------- 4b. Phase 2 modal shell (character-tasks V.1)
//
// The tab component, the shared hover tooltip, the visual talent tree, the reputation maths, the
// empty states and the handler wiring of the Phase 2 modal rework. These run against the COMMITTED
// repo artifacts — sourceDir is still ROOT here, the /tmp fixtures of [5] are written below — so a
// regression in data/character-data.json or data/talent-trees.json fails here as well as in the UI.
// Nothing in [1]–[4] is relaxed by it: this section only opens and closes the modal, and one of its
// own checks asserts that doing so wrote nothing to localStorage (pi-warden's two-key rule).
console.log('\n[4b] Phase 2: tab bar, tooltip, talent tree, reputation, empty states');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// The harness must not borrow the app's own escaper to prove the app's escaping.
const esc = value => String(value == null ? '' : value)
  .replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

const TAB_IDS = ['gear', 'talents', 'professions', 'reputation', 'stats', 'progress'];
const TAB_LABELS = ['Gear', 'Talents', 'Professions', 'Reputation', 'Stats', 'Progress'];
const RICH_ID = '1791928493125';    // Warrior: gear with stats, two specs, professions, reputations
const SPARSE_ID = '1791919870010';  // status-only sidecar row: all six tabs are empty states
const richSidecar = charDataDoc.characters[RICH_ID];
const sparseSidecar = charDataDoc.characters[SPARSE_ID];
const richRow = rosterChars.find(c => c.id === RICH_ID);
const treesDoc = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'talent-trees.json'), 'utf8'));
const writesBeforePhase2 = storageWrites.length;

ok(richSidecar && richSidecar.gear.length > 0 && richSidecar.talents.length === 2
  && richSidecar.reputations.length > 0 && richSidecar.stats && richSidecar.media,
  `the rich fixture row ${RICH_ID} carries gear+stats, two specs, professions, reputations, media`);
ok(sparseSidecar && !sparseSidecar.gear && !sparseSidecar.reputations && !sparseSidecar.stats
  && !sparseSidecar.talents && !sparseSidecar.professions,
  `the sparse row ${SPARSE_ID} is status-only, so all six of its tabs must be empty states`);

// ---------- markup readers (the panels are born inside one innerHTML string) ----------

const contentHtml = () => byId('detail-content').innerHTML;
const openRow = id => { detail.openCharacterDetail(id); return contentHtml(); };
function attrOf(name, source) {
  const m = new RegExp(`(?:^|[\\s"])${name}="([^"]*)"`).exec(source);
  return m ? m[1] : '';
}
// Phase 4b scoped these two readers to the TOP-LEVEL bar and its six panels: the Talents panel now
// nests a second-level tablist of its own (one sub-tab per spec group), and an unscoped reader would
// count the six tabs as eight and call the six panels eight. `data-detail-tab` / the `detail-panel-`
// id prefix are what the top-level markup owns, so the scope is structural rather than positional.
function tabButtons(html) {
  return [...html.matchAll(/<button type="button" role="tab"([\s\S]*?)>([\s\S]*?)<\/button>/g)]
    .filter(m => /\bdata-detail-tab="/.test(m[1]))
    .map(m => ({
    attrs: m[1],
    label: m[2].trim(),
    id: attrOf('id', m[1]),
    tab: attrOf('data-detail-tab', m[1]),
    controls: attrOf('aria-controls', m[1]),
    selected: attrOf('aria-selected', m[1]),
    tabindex: attrOf('tabindex', m[1]),
  }));
}
function tabPanels(html) {
  return [...html.matchAll(/<div role="tabpanel" id="(detail-panel-[^"]+)" aria-labelledby="([^"]*)"([^>]*)class="([^"]*)"/g)]
    .map(m => ({ id: m[1], labelledBy: m[2], focusable: m[3].includes('tabindex="0"'),
      classes: m[4].split(/\s+/).filter(Boolean) }));
}
// The sub-tab row inside the Talents panel (Phase 4b F1), read by the same rules the top-level bar
// is read by so the two can be checked against the same contract.
function subTabButtons(html) {
  return [...html.matchAll(/<button type="button" role="tab"([\s\S]*?)>([\s\S]*?)<\/button>/g)]
    .filter(m => /\bdata-detail-subtab="/.test(m[1]))
    .map(m => ({
      attrs: m[1],
      label: m[2].trim(),
      id: attrOf('id', m[1]),
      subtab: attrOf('data-detail-subtab', m[1]),
      controls: attrOf('aria-controls', m[1]),
      selected: attrOf('aria-selected', m[1]),
      tabindex: attrOf('tabindex', m[1]),
      specActive: attrOf('data-wow-spec-active', m[1]),
      meta: ((/<span class="wow-subtab-meta">([\s\S]*?)<\/span>/.exec(m[2]) || [])[1] || '').trim(),
      name: ((/<span class="wow-subtab-label">([\s\S]*?)<\/span>/.exec(m[2]) || [])[1] || '').trim(),
    }));
}
function subTabPanels(html) {
  return [...html.matchAll(/<div role="tabpanel" id="(detail-subpanel-\d+)" aria-labelledby="([^"]*)"([^>]*)class="([^"]*)"/g)]
    .map(m => ({ id: m[1], labelledBy: m[2], focusable: m[3].includes('tabindex="0"'),
      classes: m[4].split(/\s+/).filter(Boolean) }));
}
// One spec group's sub-panel, ending at the next sub-panel (or at the end of the enclosing panel).
function subPanelSlice(html, index) {
  const marker = `<div role="tabpanel" id="detail-subpanel-${index}"`;
  const start = html.indexOf(marker);
  if (start === -1) return '';
  let next = html.length;
  for (let i = 0; i < 12; i += 1) {
    if (i === Number(index)) continue;
    const at = html.indexOf(`<div role="tabpanel" id="detail-subpanel-${i}"`, start + marker.length);
    if (at !== -1 && at < next) next = at;
  }
  return html.slice(start, next);
}
// The bar is the bytes between the tablist opener and the first panel: a byte-identical slice is a
// stronger claim than "the same six labels", because decision #5 is that the bar cannot vary.
function tabBarSlice(html) {
  const start = html.indexOf('<div role="tablist"');
  const end = html.indexOf('<div role="tabpanel"');
  return start === -1 || end <= start ? '' : html.slice(start, end);
}
// The slice ends at the NEXT TOP-LEVEL panel rather than at the next `role="tabpanel"` anywhere: a
// sub-panel nested inside the Talents panel is part of that panel's content, not its end.
function panelSlice(html, id) {
  const marker = `<div role="tabpanel" id="detail-panel-${id}"`;
  const start = html.indexOf(marker);
  if (start === -1) return '';
  let next = html.length;
  TAB_IDS.forEach(other => {
    if (other === id) return;
    const at = html.indexOf(`<div role="tabpanel" id="detail-panel-${other}"`, start + marker.length);
    if (at !== -1 && at < next) next = at;
  });
  return html.slice(start, next);
}
const countOf = (needle, hay) => hay.split(needle).length - 1;

// ---------- a. tab bar structure ----------

state.setFilter('all');
state.setQuery('');
detail.closeDetail();
ok(detail.DETAIL_TABS.map(t => t.id).join('·') === TAB_IDS.join('·'),
  `DETAIL_TABS ids are gear·talents·professions·reputation·stats·progress in order`);
ok(detail.DETAIL_TABS.map(t => t.label).join('·') === TAB_LABELS.join('·'),
  `DETAIL_TABS labels are Gear·Talents·Professions·Reputation·Stats·Progress`);
ok(detail.DEFAULT_DETAIL_TAB === 'gear', `DEFAULT_DETAIL_TAB is "${detail.DEFAULT_DETAIL_TAB}"`);

const richHtml = openRow(RICH_ID);
const richButtons = tabButtons(richHtml);
const richPanels = tabPanels(richHtml);
// Phase 4b: scoped to the bar slice, because the Talents panel now nests a second tablist of its own
// (F1). "Exactly one top-level tablist" is the invariant this check has always owned; the nested one
// is asserted separately, inside the panel, below.
ok(countOf('role="tablist"', tabBarSlice(richHtml)) === 1
  && attrOf('aria-label', tabBarSlice(richHtml)) === 'Character detail sections',
  'the bar is exactly one role="tablist" with an accessible name');
ok(richButtons.length === 6 && richButtons.map(b => b.tab).join('·') === TAB_IDS.join('·'),
  `the rendered bar draws six role="tab" buttons in the agreed order (${richButtons.map(b => b.tab).join(' · ') || 'none'})`);
ok(richButtons.map(b => b.label).join('·') === TAB_LABELS.join('·'),
  `each tab's visible label matches its agreed copy (${richButtons.map(b => b.label).join(' · ') || 'none'})`);
ok(richButtons.every(b => b.controls === `detail-panel-${b.tab}`)
  && richPanels.length === 6
  && richPanels.every(p => TAB_IDS.some(id => p.id === `detail-panel-${id}`)),
  `every tab's aria-controls names its own panel id (6 panels: ${richPanels.map(p => p.id).join(', ') || 'none'})`);
ok(richPanels.every(p => p.labelledBy === `detail-tab-${p.id.replace('detail-panel-', '')}`),
  'every panel points back at its own tab button (aria-labelledby)');
ok(richButtons.filter(b => b.selected === 'true').length === 1
  && richButtons.some(b => b.selected === 'true' && b.tab === 'gear'),
  `exactly one tab is aria-selected="true" on open (${(richButtons.find(b => b.selected === 'true') || {}).tab || 'none'})`);
ok(richButtons.filter(b => b.tabindex === '0').length === 1
  && (richButtons.find(b => b.tabindex === '0') || {}).tab === 'gear'
  && richButtons.filter(b => b.tabindex === '-1').length === 5,
  'roving tabindex: the selected button is the only tabindex="0", the other five are tabindex="-1"');
ok(richPanels.filter(p => !p.classes.includes('hidden')).length === 1
  && (richPanels.find(p => !p.classes.includes('hidden')) || {}).id === 'detail-panel-gear',
  'exactly one tabpanel is visible on open, and it is the selected one');
ok(richPanels.filter(p => p.focusable).length === 1
  && (richPanels.find(p => p.focusable) || {}).id === 'detail-panel-gear',
  'the visible panel is tabindex="0" so Tab reaches its scroll region');
ok(countOf('disabled', tabBarSlice(richHtml)) === 0,
  'no tab is disabled or hidden for a row that has data (decision #5: all six, always)');
ok(markup.indexOf('id="detail-tooltip"') === markup.lastIndexOf('id="detail-tooltip"')
  && markup.indexOf('id="detail-tooltip"') > markup.indexOf('</div>', markup.indexOf('id="detail-content"')),
  'index.html declares exactly one #detail-tooltip, outside the #detail-content it never rebuilds');
ok(!richHtml.includes('id="detail-tooltip"') && !richHtml.includes('wow-tooltip'),
  'the tooltip is never re-created inside the modal content (one shared element, T2.3)');
// T2.9: the review footer stays pinned below the panels, so it must come after every panel.
const lastPanelStart = richHtml.lastIndexOf('<div role="tabpanel"');
ok(markup.indexOf('id="detail-content"') !== -1 && lastPanelStart !== -1
  && richHtml.indexOf('role="tablist"') > richHtml.indexOf('<h3') && richHtml.indexOf('role="tablist"') < lastPanelStart,
  'document order is header → tab bar → panels (the bar replaced the stacked <h4> headings)');

// ---------- a2. tree geometry loads on demand, and says so while it does ----------

detail.closeDetail();
ok(dataMod.talentTreesDoc() === null && dataMod.talentTreesLoading() === false,
  'talent-tree geometry is not fetched on page load: no doc, no request in flight (lazy, ~200 KB, T1.3)');
ok(!contentHtml().includes('Loading talent tree data'), 'no loading notice before the Talents tab is ever opened');
openRow(RICH_ID);
detail.setDetailTab('talents');
ok(dataMod.talentTreesLoading() === true
  && panelSlice(contentHtml(), 'talents').includes('Loading talent tree data'),
  'asking for the Talents tab is what asks for the file, and it says "loading" not "no data"');
await dataMod.ensureTalentTrees();
await sleep(30);
ok(dataMod.talentTreesDoc() !== null && dataMod.talentTreesLoading() === false,
  `the trees arrive once (${Object.keys(dataMod.talentTreesDoc().specs).length} specs) and the repaint follows`);

// ---------- b. setDetailTab switches the visible panel ----------

detail.closeDetail();
openRow(RICH_ID);
ok(detail.detailActiveTab() === 'gear', 'a freshly opened row starts on the Gear tab');
ok(detail.setDetailTab('stats') === true && detail.detailActiveTab() === 'stats',
  'setDetailTab("stats") reports success and becomes the active tab');
const statsHtml = contentHtml();
ok(tabPanels(statsHtml).filter(p => !p.classes.includes('hidden')).map(p => p.id).join() === 'detail-panel-stats'
  && panelSlice(statsHtml, 'stats').includes('Attributes'),
  'the stats panel is the only un-hidden panel after switching to it');
ok(panelSlice(statsHtml, 'gear').includes(' hidden'), 'the gear panel is hidden once another tab is selected');
ok(tabButtons(statsHtml).some(b => b.selected === 'true' && b.tab === 'stats')
  && tabButtons(statsHtml).some(b => b.tabindex === '0' && b.tab === 'stats'),
  'the bar follows the switch: aria-selected and tabindex both move to the new tab');
ok(tabPanels(statsHtml).some(p => p.id === 'detail-panel-stats' && p.focusable)
  && !tabPanels(statsHtml).some(p => p.id === 'detail-panel-gear' && p.focusable),
  'only the newly visible panel keeps the panel tabindex');
ok(detail.setDetailTab('stats') === true && detail.detailActiveTab() === 'stats',
  're-selecting the active tab is a no-op that still reports success');
ok(detail.setDetailTab('no-such-tab') === false && detail.detailActiveTab() === 'stats',
  'an unknown tab id is refused and changes nothing');
ok([null, undefined, '', 42, {}].every(id => detail.setDetailTab(id) === false)
  && detail.detailActiveTab() === 'stats',
  'null/undefined/empty/number/object tab ids are all refused');

// Per-open state: a tab chosen on one character is not a fact about the next one.
detail.setDetailTab('progress');
openRow(SPARSE_ID);
ok(detail.detailActiveTab() === 'gear'
  && tabPanels(contentHtml()).filter(p => !p.classes.includes('hidden')).map(p => p.id).join() === 'detail-panel-gear',
  'switching characters resets the tab to Gear (per-open state, T2.1)');
detail.closeDetail();
detail.setDetailTab('reputation');
openRow(RICH_ID);
ok(detail.detailActiveTab() === 'gear', 'closeDetail() then reopening also resets the tab');
ok(detail.isDetailOpen() === true, 'the modal is open for the checks below');

// ---------- c. keyboard navigation ----------

const keyEvent = key => ({ key, preventDefault() { this._prevented = true; } });
detail.setDetailTab('gear');
const rightChain = [];
for (let i = 0; i < 6; i += 1) {
  detail.detailTabKeydown(keyEvent('ArrowRight'), detail.detailActiveTab());
  rightChain.push(detail.detailActiveTab());
}
ok(rightChain.join('·') === 'talents·professions·reputation·stats·progress·gear',
  `ArrowRight walks the bar and wraps at the end (${rightChain.join(' → ')})`);
detail.setDetailTab('gear');
const leftChain = [];
for (let i = 0; i < 3; i += 1) {
  detail.detailTabKeydown(keyEvent('ArrowLeft'), detail.detailActiveTab());
  leftChain.push(detail.detailActiveTab());
}
ok(leftChain.join('·') === 'progress·stats·reputation',
  `ArrowLeft wraps backwards from the first tab (${leftChain.join(' → ')})`);
detail.setDetailTab('stats');
detail.detailTabKeydown(keyEvent('Home'), detail.detailActiveTab());
const homeTab = detail.detailActiveTab();
detail.detailTabKeydown(keyEvent('End'), detail.detailActiveTab());
ok(homeTab === 'gear' && detail.detailActiveTab() === 'progress',
  `Home selects the first tab and End the last (${homeTab} → ${detail.detailActiveTab()})`);
detail.setDetailTab('stats');
const INERT_KEYS = ['Enter', ' ', 'Escape', 'Tab', 'ArrowUp', 'ArrowDown', 'PageDown', 'a', ''];
const inertMoved = INERT_KEYS.filter(key => detail.detailTabKeydown(keyEvent(key), 'stats') !== false
  || detail.detailActiveTab() !== 'stats');
ok(inertMoved.length === 0,
  `${INERT_KEYS.length} unhandled keys are inert (return false, active tab stays "stats")`);
const handledKey = keyEvent('ArrowRight');
const inertKey = keyEvent('Enter');
detail.detailTabKeydown(handledKey, 'gear');
detail.detailTabKeydown(inertKey, 'gear');
ok(handledKey._prevented === true && inertKey._prevented === undefined,
  'handled keys call preventDefault (the browser must not scroll); unknown keys do not');
ok([null, undefined, '', 'no-such-tab'].every(id => detail.detailTabKeydown(keyEvent('ArrowRight'), id) === false),
  'keydown for an unknown/absent tab id is refused outright');
detail.setDetailTab('gear');
detail.detailTabKeydown(keyEvent('ArrowRight'), 'gear');
const kbHtml = contentHtml();
// Phase 4b: the visibility half of this check now reads the structured panel list rather than
// grepping the slice for ` hidden`, because the Talents panel legitimately contains hidden sub-panels
// (the non-selected spec groups). Same claim: the talents PANEL is the visible one.
ok(tabButtons(kbHtml).filter(b => b.selected === 'true').map(b => b.tab).join() === 'talents'
  && tabButtons(kbHtml).filter(b => b.tabindex === '0').map(b => b.tab).join() === 'talents'
  && tabPanels(kbHtml).filter(p => !p.classes.includes('hidden')).map(p => p.id).join() === 'detail-panel-talents',
  'one ArrowRight repaints the bar and the panel together (roving tabindex cannot drift)');
ok(documentStub.activeElement && documentStub.activeElement.id === 'detail-tab-talents',
  `focus follows the keyboard selection to the replacement button (${documentStub.activeElement && documentStub.activeElement.id})`);

// ---------- d. the shared tooltip ----------

const tip = byId('detail-tooltip');
const tipIdentity = tip;
tip.offsetWidth = 300;
tip.offsetHeight = 150;
globalThis.innerWidth = 1000;
globalThis.innerHeight = 800;
const triggerOf = rect => ({
  attrs: {},
  getBoundingClientRect: () => rect,
  setAttribute(key, value) { this.attrs[key] = String(value); },
  getAttribute(key) { return this.attrs[key] === undefined ? null : this.attrs[key]; },
  removeAttribute(key) { delete this.attrs[key]; },
});

detail.closeDetail();
const gearHtmlRich = openRow(RICH_ID);
const hoverWiring = [...gearHtmlRich.matchAll(/data-wow-slot="([^"]*)"[^>]*onmouseenter="showDetailTooltip\(event, (\d+)\)"[^>]*onmouseleave="hideDetailTooltip\(\)"[^>]*onfocus="showDetailTooltip\(event, (\d+)\)"[^>]*onblur="hideDetailTooltip\(\)"/g)];
ok(hoverWiring.length === richSidecar.gear.length,
  `every one of the ${richSidecar.gear.length} synced items is hoverable AND focusable (${hoverWiring.length} slots wired)`);
const hoverIndices = hoverWiring.map(m => Number(m[2]));
ok(hoverWiring.every(m => m[2] === m[3]), 'the hover and focus handlers register the same tooltip index');
ok(Math.min(...hoverIndices) === 0 && new Set(hoverIndices).size === hoverIndices.length
  && Math.max(...hoverIndices) === hoverIndices.length - 1,
  `tooltip indices are one contiguous, unique 0..n-1 registry (max ${Math.max(...hoverIndices)}, count ${hoverIndices.length})`);
ok(!/onmouseenter="[^"]*[a-zA-Z]\w*\s*:/.test(gearHtmlRich) && !/showDetailTooltip\(event, '?[^0-9\)"]/.test(gearHtmlRich),
  'only a numeric index crosses into a tooltip handler — no sidecar string becomes handler data');

const headItem = richSidecar.gear.find(g => g.slot === 'Head');
const headIndex = Number((hoverWiring.find(m => m[1] === 'Head') || [])[2]);
ok(Number.isInteger(headIndex), `the Head slot's tooltip index was recovered from the markup (${headIndex})`);
const headTrigger = triggerOf({ left: 50, right: 100, top: 20, bottom: 72 });
ok(detail.showDetailTooltip({ currentTarget: headTrigger }, headIndex) === true,
  `showDetailTooltip(event, ${headIndex}) opens the tooltip for the Head item`);
ok(!tip.classList.contains('hidden') && tip.getAttribute('aria-hidden') === 'false',
  'the shared element is un-hidden and announced (aria-hidden="false")');
ok(tip.innerHTML.includes(`data-wow-quality="${headItem.quality.toLowerCase()}"`)
  && tip.innerHTML.includes(esc(headItem.name)),
  `tooltip name carries the quality class (data-wow-quality="${headItem.quality.toLowerCase()}" for ${headItem.name})`);
const headStatLines = headItem.stats.map(s => esc(s.displayString));
ok(headStatLines.every(text => tip.innerHTML.includes(text)),
  `every stat displayString renders (${headStatLines.join(' | ')})`);
const headColor = headItem.stats[0].color;
ok(tip.innerHTML.includes(`style="color:rgba(${headColor.r}, ${headColor.g}, ${headColor.b}, 1)"`),
  `stat colour reaches the page as a rebuilt rgba(${headColor.r}, ${headColor.g}, ${headColor.b}, 1)`);
ok(tip.innerHTML.includes(`Item Level ${headItem.ilvl}`)
  && tip.innerHTML.includes(headItem.itemClass.name) && tip.innerHTML.includes(headItem.itemSubclass.name),
  'ilvl + class/subclass meta line renders');
ok(!tip.innerHTML.includes('<img') && !tip.innerHTML.includes(headItem.iconUrl),
  'the tooltip carries text only: the item icon URL never reaches it as markup');

// Tooltip position maths: right of the trigger, flipping left at the viewport edge, clamped inside it.
ok(tip.style.left === '110px' && tip.style.top === '20px',
  `tooltip sits right of the trigger when there is room (left ${tip.style.left}, top ${tip.style.top})`);
detail.showDetailTooltip({ currentTarget: triggerOf({ left: 850, right: 900, top: 100, bottom: 150 }) }, headIndex);
ok(tip.style.left === '540px', `flips to the trigger's left when it would run off (${tip.style.left})`);
detail.showDetailTooltip({ currentTarget: triggerOf({ left: 100, right: 150, top: 780, bottom: 800 }) }, headIndex);
ok(tip.style.top === '642px', `bottom row is clamped into the viewport (top ${tip.style.top})`);
detail.showDetailTooltip({ currentTarget: triggerOf({ left: -60, right: -20, top: 300, bottom: 350 }) }, headIndex);
ok(tip.style.left === '8px', `a trigger scrolled off the left edge still leaves the tooltip on screen (left ${tip.style.left})`);

// Invalid registry reads never open a tooltip.
detail.closeDetail();
detail.openCharacterDetail(RICH_ID);
ok([-1, 9999, 1.5, 'abc', undefined, NaN, {}].every(index => detail.showDetailTooltip({ currentTarget: triggerOf({ left: 0, right: 1, top: 0, bottom: 1 }) }, index) === false)
  && tip.classList.contains('hidden'),
  'out-of-range / non-integer / non-numeric tooltip indices return false and leave it hidden');

// The registry belongs to the current paint: an index from one row must not describe another.
detail.closeDetail();
detail.openCharacterDetail(SPARSE_ID);
const staleIndex = hoverIndices.length - 1;
ok(detail.showDetailTooltip({ currentTarget: triggerOf({ left: 0, right: 1, top: 0, bottom: 1 }) }, staleIndex) === false
  && tip.innerHTML === '' && tip.classList.contains('hidden'),
  `a tooltip index from the previous row resolves to nothing after a switch (index ${staleIndex} on a row with no gear)`);

// aria-describedby tracks the live trigger and is dropped when the tooltip goes away (hardening a).
const firstTrigger = triggerOf({ left: 50, right: 100, top: 20, bottom: 72 });
detail.closeDetail();
detail.openCharacterDetail(RICH_ID);
const headIdx2 = Number((hoverWiring.find(m => m[1] === 'Head') || [])[2]);
detail.showDetailTooltip({ currentTarget: firstTrigger }, headIdx2);
ok(firstTrigger.getAttribute('aria-describedby') === 'detail-tooltip',
  'the hovered trigger is described by the tooltip while it is up');
const secondTrigger = triggerOf({ left: 200, right: 250, top: 20, bottom: 72 });
detail.showDetailTooltip({ currentTarget: secondTrigger }, headIdx2);
ok(secondTrigger.getAttribute('aria-describedby') === 'detail-tooltip'
  && firstTrigger.getAttribute('aria-describedby') === null,
  'retargeting moves the description to the new node instead of leaving two claimants');
detail.closeDetail();
ok(firstTrigger.getAttribute('aria-describedby') === null && secondTrigger.getAttribute('aria-describedby') === null
  && tip.classList.contains('hidden') && tip.innerHTML === '',
  'closeDetail() clears aria-describedby off the last trigger along with the tooltip content');
detail.openCharacterDetail(RICH_ID);
const repaintTrigger = triggerOf({ left: 50, right: 100, top: 20, bottom: 72 });
detail.showDetailTooltip({ currentTarget: repaintTrigger }, headIdx2);
state.renderAll();
ok(repaintTrigger.getAttribute('aria-describedby') === null && tip.classList.contains('hidden'),
  'a repaint (background refresh, query change) dismisses the tooltip and its dangling reference');

// The hide is delayed, so moving between adjacent slots does not blank the tooltip first.
detail.showDetailTooltip({ currentTarget: repaintTrigger }, headIdx2);
detail.hideDetailTooltip();
ok(!tip.classList.contains('hidden'), `the hide is deferred, so an adjacent hover keeps it up (${detail.TOOLTIP_HIDE_MS} ms window)`);
await sleep(detail.TOOLTIP_HIDE_MS + 120);
ok(tip.classList.contains('hidden') && tip.innerHTML === '' && tip.getAttribute('aria-hidden') === 'true',
  'and it does dismiss itself once the window passes');
detail.closeDetail();
ok(byId('detail-tooltip') === tipIdentity, 'the tooltip stays one element across opens, closes and repaints');

// ---------- e. the visual talent tree vs data/talent-trees.json ----------

detail.closeDetail();
openRow(RICH_ID);
detail.setDetailTab('talents');
const talentsHtml = panelSlice(contentHtml(), 'talents');
ok(talentsHtml.includes(`Tree geometry: ${esc(treesDoc.staticNamespace)}`),
  `the panel states the static namespace the geometry was built against (${treesDoc.staticNamespace})`);
const richSpecs = richSidecar.talents.filter(g => g && typeof g === 'object');
const treeBlocks = (() => {
  const marker = '<div class="wow-tree overflow-hidden"';
  const positions = [];
  for (let i = talentsHtml.indexOf(marker); i !== -1; i = talentsHtml.indexOf(marker, i + 1)) positions.push(i);
  return positions.map((start, idx) => talentsHtml.slice(start, positions[idx + 1] === undefined ? talentsHtml.length : positions[idx + 1]));
})();
// Phase 4b (F1): the two spec sections are no longer stacked with a divider between them — they are
// two sub-panels of one row, and every group's tree is still rendered (the inactive panel is hidden)
// so the geometry can be asserted for both specs, not just the visible one.
ok(treeBlocks.length === richSpecs.length && richSpecs.length === 2,
  `a two-spec group renders one tree per spec sub-panel (${treeBlocks.length} blocks for ${richSpecs.map(s => s.specName).join(' + ')})`);
ok(countOf('<div class="pt-2 border-t border-slate-800"></div>', talentsHtml) === 0,
  'the stacked section dividers are gone now that the spec groups are sub-tabs (a rule above a hidden panel would paint inside the visible one)');
ok(richSpecs.every(s => talentsHtml.includes(`>${esc(s.specName)}</h4>`)),
  `both spec headings render (${richSpecs.map(s => s.specName).join(', ')})`);
ok(countOf('>Active<', talentsHtml) === 1 && talentsHtml.includes(`>${esc(richSidecar.activeSpec)}</h4>`),
  `exactly the active specialization is badged (${richSidecar.activeSpec})`);

// Node grid, rank labels and unlock edges, re-derived from the JSON here.
function selectionMapsFor(group) {
  const byTalent = new Map();
  const bySpell = new Map();
  (group.talents || []).forEach(entry => {
    const rank = Number(entry.rank);
    if (!Number.isFinite(rank) || rank <= 0) return;
    if (Number.isFinite(Number(entry.talentId))) {
      const id = Number(entry.talentId);
      byTalent.set(id, Math.max(byTalent.get(id) || 0, rank));
    }
    if (Number.isFinite(Number(entry.spellId))) {
      const id = Number(entry.spellId);
      bySpell.set(id, Math.max(bySpell.get(id) || 0, rank));
    }
  });
  return { byTalent, bySpell };
}
let gridMismatch = [];
let rankMismatch = [];
let edgeMismatch = [];
let spentMismatch = [];
richSpecs.forEach((group, index) => {
  const tree = treesDoc.specs[String(group.specId)];
  if (!tree) { gridMismatch.push(`${group.specId}: no tree in data/talent-trees.json`); return; }
  const nodes = (tree.nodes || []).filter(n => n && typeof n === 'object');
  const placed = nodes.filter(n => n.displayRow !== null && n.displayRow !== undefined
    && n.displayCol !== null && n.displayCol !== undefined);
  const rows = placed.map(n => Number(n.displayRow));
  const cols = placed.map(n => Number(n.displayCol));
  const minRow = Math.min(...rows); const minCol = Math.min(...cols);
  const block = treeBlocks[index] || '';
  const expectedStyle = placed.map(n => `left:${(Number(n.displayCol) - minCol) * detail.TREE_CELL + (detail.TREE_CELL - detail.TREE_NODE) / 2}px;top:${(Number(n.displayRow) - minRow) * detail.TREE_CELL + (detail.TREE_CELL - detail.TREE_NODE) / 2}px`);
  const actualStyle = [...block.matchAll(/class="wow-tree-node"[\s\S]*?style="left:(-?[\d.]+)px;top:(-?[\d.]+)px"/g)].map(m => `left:${m[1]}px;top:${m[2]}px`);
  if (actualStyle.length !== placed.length) gridMismatch.push(`${group.specId}: ${actualStyle.length} nodes rendered, ${placed.length} placeable in the JSON`);
  expectedStyle.forEach((want, i) => { if (actualStyle[i] !== want) gridMismatch.push(`${group.specId} node ${i}: want ${want} got ${actualStyle[i] || 'none'}`); });
  const expectedBox = `style="width:${(Math.max(...cols) - minCol + 1) * detail.TREE_CELL}px;height:${(Math.max(...rows) - minRow + 1) * detail.TREE_CELL}px"`;
  if (!block.includes(expectedBox)) gridMismatch.push(`${group.specId}: tree box want ${expectedBox}`);
  const selection = selectionMapsFor(group);
  const expectedRanks = placed.map(n => {
    const ranks = (n.ranks || []).filter(r => r && typeof r === 'object');
    let taken = 0;
    ranks.forEach(rank => {
      let chosen = rank.talentId !== null && rank.talentId !== undefined ? selection.byTalent.get(Number(rank.talentId)) : undefined;
      if (chosen === undefined && rank.spellId !== null && rank.spellId !== undefined) chosen = selection.bySpell.get(Number(rank.spellId));
      if (typeof chosen === 'number' && chosen > taken) taken = Math.min(chosen, ranks.length);
    });
    return `${taken}/${ranks.length}`;
  });
  const actualRanks = [...block.matchAll(/<span class="wow-tree-rank">(\d+)\/(\d+)<\/span>/g)].map(m => `${m[1]}/${m[2]}`);
  if (actualRanks.length !== expectedRanks.length) rankMismatch.push(`${group.specId}: ${actualRanks.length} rank labels for ${expectedRanks.length} nodes`);
  expectedRanks.forEach((want, i) => { if (actualRanks[i] !== want) rankMismatch.push(`${group.specId} node ${i}: want ${want} got ${actualRanks[i] || 'none'}`); });
  const byIdMap = new Map(nodes.map(n => [Number(n.id), n]));
  const placedIds = new Set(placed.map(n => Number(n.id)));
  const expectedEdges = placed.reduce((total, n) => total + (n.unlocks || []).filter(target => {
    const child = byIdMap.get(Number(target));
    return child && placedIds.has(Number(target));
  }).length, 0);
  const actualEdges = countOf('<line ', block);
  if (actualEdges !== expectedEdges) edgeMismatch.push(`${group.specId}: ${actualEdges} connector lines, ${expectedEdges} unlock edges between placed nodes`);
  const spent = expectedRanks.filter(label => !label.startsWith('0/')).length;
  if (spent === 0 && (group.talents || []).length > 0) spentMismatch.push(`${group.specId}: selection has ${(group.talents || []).length} entries, no node lit`);
});
ok(gridMismatch.length === 0,
  `node grid matches the committed geometry: ${treeBlocks.reduce((n, b) => n + countOf('class="wow-tree-node"', b), 0)} nodes at (col-minCol)×${detail.TREE_CELL}px / (row-minRow)×${detail.TREE_CELL}px${gridMismatch.length ? ` — ${gridMismatch.slice(0, 3).join('; ')}` : ''}`);
ok(rankMismatch.length === 0,
  `every node's rank label is n/m against the selection (${treeBlocks.reduce((n, b) => n + countOf('class="wow-tree-rank"', b), 0)} labels)${rankMismatch.length ? ` — ${rankMismatch.slice(0, 3).join('; ')}` : ''}`);
ok(edgeMismatch.length === 0,
  `connector lines come from the payload's own unlock edges, none invented (${edgeMismatch.join('; ') || 'counts match'})`);
ok(spentMismatch.length === 0, 'a non-empty selection always lights at least one node (no all-grey tree)');
const litNodes = [...talentsHtml.matchAll(/data-wow-state="(selected|locked|open)"/g)].map(m => m[1]);
const expectedLit = [...talentsHtml.matchAll(/<span class="wow-tree-rank">(\d+)\/\d+<\/span>/g)]
  .filter(m => Number(m[1]) > 0).length;
ok(litNodes.length > 0 && litNodes.every(state => ['selected', 'locked', 'open'].includes(state))
  && countOf('data-wow-state="selected"', talentsHtml) === expectedLit,
  `a node is "selected" exactly when its rank is > 0 (${expectedLit} lit of ${litNodes.length} nodes)`);
const pxInTree = [...talentsHtml.matchAll(/(?:left|top|width|height):(-?[\d.]+)px/g)].map(m => Number(m[1]));
ok(pxInTree.length > 0 && pxInTree.every(n => Number.isFinite(n) && n >= 0 && n <= detail.TREE_PIXEL_MAX),
  `every rendered tree pixel is finite and inside 0..${detail.TREE_PIXEL_MAX} (${pxInTree.length} values, max ${Math.max(...pxInTree)})`);
ok(!/NaN|Infinity|undefined/.test(talentsHtml),
  'no NaN/Infinity/undefined leaks into the tree markup');
const talentImgs = [...talentsHtml.matchAll(/<img src="([^"]+)"/g)].map(m => m[1]);
ok(talentImgs.length > 0 && talentImgs.every(src => detail.isSafeIconUrl(src)),
  `every talent icon that reached an <img> passed isSafeIconUrl (${talentImgs.length} icons)`);
// A talent node's tooltip is the rank the player is paying for.
const nodeTipIndex = Number((( /aria-label="([^"]*)"[\s\S]{0,400}?onmouseenter="showDetailTooltip\(event, (\d+)\)"/.exec(talentsHtml) || [])[2]));
ok(Number.isInteger(nodeTipIndex), `a tree node exposes its tooltip index (${nodeTipIndex})`);
detail.showDetailTooltip({ currentTarget: triggerOf({ left: 100, right: 150, top: 40, bottom: 90 }) }, nodeTipIndex);
ok(tip.innerHTML.includes('Rank ') && /Rank \d+\/\d+/.test(tip.innerHTML),
  `the talent tooltip states the rank as n/m (${(/Rank \d+\/\d+/.exec(tip.innerHTML) || ['none'])[0]})`);
detail.closeDetail();

// ---------- e2. Phase 4b (F1): the spec sub-tab row inside the Talents panel ----------
//
// The multi-spec stack became a second-level tab row, so the row is checked against the same
// contract the top-level bar is: one named tablist, one tab per group, aria-controls /
// aria-labelledby pairs, one aria-selected, one tabindex="0", arrow keys that wrap, focus that
// follows the repaint, and a top-level bar that does not move one byte.
const activeSpecPosition = richSpecs.findIndex(g => g.specName === richSidecar.activeSpec);
detail.closeDetail();
openRow(RICH_ID);
detail.setDetailTab('talents');
const subHtml = panelSlice(contentHtml(), 'talents');
ok(countOf('role="tablist"', subHtml) === 1 && countOf('role="tablist"', contentHtml()) === 2,
  `the Talents panel nests exactly one second-level tablist (${countOf('role="tablist"', contentHtml())} in the whole modal: top-level + nested)`);
ok(attrOf('aria-label', subHtml) === 'Talent specializations'
  && attrOf('aria-label', subHtml) !== attrOf('aria-label', tabBarSlice(subHtml)),
  `the nested tablist has its own accessible name ("${attrOf('aria-label', subHtml)}"), distinct from the top bar's`);
const subTabs = subTabButtons(subHtml);
const subPanels = subTabPanels(subHtml);
ok(subTabs.length === richSpecs.length && richSpecs.length === 2,
  `one sub-tab per spec group (${subTabs.length} tabs for ${richSpecs.map(s => s.specName).join(' + ')})`);
ok(subTabs.map(t => t.name).join('·') === richSpecs.map(s => esc(s.specName)).join('·'),
  `each sub-tab is labelled with its specName (${subTabs.map(t => t.name).join(' · ') || 'none'})`);
ok(subTabs.every(t => t.controls === `detail-subpanel-${t.subtab}`)
  && subPanels.length === subTabs.length
  && subPanels.every(p => p.labelledBy === `detail-subtab-${p.id.replace('detail-subpanel-', '')}`),
  `every sub-tab's aria-controls names its own sub-panel and each panel points back (${subPanels.map(p => p.id).join(', ') || 'none'})`);
ok(subTabs.filter(t => t.selected === 'true').length === 1
  && (subTabs.find(t => t.selected === 'true') || {}).subtab === String(activeSpecPosition),
  `the default selection is the active specialization's group (${richSidecar.activeSpec} = index ${activeSpecPosition})`);
ok(detail.detailActiveSpecIndex() === activeSpecPosition,
  `detailActiveSpecIndex() agrees with the markup (${detail.detailActiveSpecIndex()})`);
ok(subTabs.filter(t => t.tabindex === '0').length === 1
  && (subTabs.find(t => t.tabindex === '0') || {}).subtab === String(activeSpecPosition)
  && subTabs.filter(t => t.tabindex === '-1').length === subTabs.length - 1,
  'the sub-bar has its own roving tabindex: the selected tab is the only one in the tab sequence');
ok(subTabs.filter(t => t.specActive === 'true').map(t => t.name).join() === esc(richSidecar.activeSpec)
  && subTabs.filter(t => t.specActive === 'false').length === subTabs.length - 1,
  `the active spec is marked on its tab and the others are not (${subTabs.map(t => `${t.name}:${t.specActive}`).join(', ')})`);
ok(subTabs.every((t, i) => t.meta === `${richSpecs[i].spentPoints} pts`),
  `spentPoints is the secondary text on every tab (${subTabs.map(t => t.meta).join(' · ') || 'none'})`);
ok([...subHtml.matchAll(/setDetailSpecTab\('([^']*)'\)/g)].every(m => /^\d{1,2}$/.test(m[1]))
  && [...subHtml.matchAll(/detailSpecTabKeydown\(event, '([^']*)'\)/g)].every(m => /^\d{1,2}$/.test(m[1])),
  'every sub-tab handler argument is a numeric group index — no spec name reaches the handler');
ok(countOf('wow-tree overflow-hidden', subPanelSlice(subHtml, activeSpecPosition)) === 1
  && countOf('wow-tree overflow-hidden', subPanelSlice(subHtml, 1 - activeSpecPosition)) === 1,
  'both groups\' trees are in the markup: one per sub-panel, the inactive one hidden rather than unbuilt');

// Switching: the panel that changes is the sub-panel, and only it.
const barBeforeSubTabSwitch = tabBarSlice(contentHtml());
ok(detail.setDetailSpecTab(1 - activeSpecPosition) === true
  && detail.detailActiveSpecIndex() === 1 - activeSpecPosition,
  `setDetailSpecTab(${1 - activeSpecPosition}) switches the spec group`);
const switchedHtml = contentHtml();
const switchedPanel = panelSlice(switchedHtml, 'talents');
const switchedSubs = subTabButtons(switchedHtml);
const switchedPanels = subTabPanels(switchedHtml);
ok(switchedPanels.filter(p => !p.classes.includes('hidden')).map(p => p.id).join()
  === `detail-subpanel-${1 - activeSpecPosition}`,
  'exactly one sub-panel is visible after the switch, and it is the newly selected one');
ok(switchedPanels.filter(p => p.focusable).map(p => p.id).join() === `detail-subpanel-${1 - activeSpecPosition}`,
  'the visible sub-panel takes the panel tabindex and the hidden one drops it');
ok(switchedSubs.filter(t => t.selected === 'true').map(t => t.subtab).join() === String(1 - activeSpecPosition)
  && switchedSubs.filter(t => t.tabindex === '0').map(t => t.subtab).join() === String(1 - activeSpecPosition),
  'aria-selected and the roving tabindex move together on the sub-bar');
ok(countOf('wow-tree overflow-hidden', subPanelSlice(switchedPanel, 1 - activeSpecPosition)) === 1
  && subPanelSlice(switchedPanel, 1 - activeSpecPosition).includes(' hidden') === false
  && subPanelSlice(switchedPanel, activeSpecPosition).includes(' hidden'),
  'the tree under the switch is the other spec\'s, and the group left behind is the hidden one');
ok(tabBarSlice(switchedHtml) === barBeforeSubTabSwitch,
  'switching a spec sub-tab does not move one byte of the top-level tab bar (decision #5)');
ok(tabPanels(switchedHtml).filter(p => !p.classes.includes('hidden')).map(p => p.id).join() === 'detail-panel-talents',
  'and the top-level panel that is visible is still Talents (the two bars cannot drift into each other)');

// Keyboard, against the sub-bar alone.
detail.setDetailSpecTab(activeSpecPosition);
const subRight = [];
for (let i = 0; i < richSpecs.length + 1; i += 1) {
  detail.detailSpecTabKeydown(keyEvent('ArrowRight'), detail.detailActiveSpecIndex());
  subRight.push(detail.detailActiveSpecIndex());
}
const subWantRight = [];
for (let i = 1; i <= richSpecs.length + 1; i += 1) subWantRight.push((activeSpecPosition + i) % richSpecs.length);
ok(subRight.join('·') === subWantRight.join('·'),
  `ArrowRight walks the sub-tabs and wraps at the end (${subRight.join(' → ')})`);
detail.setDetailSpecTab(0);
detail.detailSpecTabKeydown(keyEvent('ArrowLeft'), 0);
const subWrapped = detail.detailActiveSpecIndex();
detail.detailSpecTabKeydown(keyEvent('Home'), subWrapped);
const subHome = detail.detailActiveSpecIndex();
detail.detailSpecTabKeydown(keyEvent('End'), subHome);
ok(subWrapped === richSpecs.length - 1 && subHome === 0 && detail.detailActiveSpecIndex() === richSpecs.length - 1,
  `Home/End reach the first and last sub-tab (${subWrapped} → ${subHome} → ${detail.detailActiveSpecIndex()})`);
const subInert = ['Enter', ' ', 'Escape', 'Tab', 'ArrowUp', 'PageDown', 'a', '']
  .filter(key => detail.detailSpecTabKeydown(keyEvent(key), detail.detailActiveSpecIndex()) !== false
    || detail.detailActiveSpecIndex() !== richSpecs.length - 1);
ok(subInert.length === 0, `${['Enter', ' ', 'Escape', 'Tab', 'ArrowUp', 'PageDown', 'a', ''].length} keys are inert on the sub-bar`);
const subHandledKey = keyEvent('ArrowRight');
const subInertKey = keyEvent('Enter');
detail.detailSpecTabKeydown(subHandledKey, detail.detailActiveSpecIndex());
detail.detailSpecTabKeydown(subInertKey, detail.detailActiveSpecIndex());
ok(subHandledKey._prevented === true && subInertKey._prevented === undefined,
  'the sub-bar preventDefaults only the keys it consumes (the panel keeps its own scroll)');
ok([null, undefined, '', 'no-such-group', -1, 1.5, 99, {}].every(index => detail.setDetailSpecTab(index) === false),
  'out-of-range / non-numeric / absent sub-tab indices are refused and change nothing');
ok(documentStub.activeElement && documentStub.activeElement.id === `detail-subtab-${detail.detailActiveSpecIndex()}`,
  `focus follows the keyboard selection to the replacement sub-tab (${documentStub.activeElement && documentStub.activeElement.id})`);

// Degrade path, per spec: no geometry for a group means that group's sub-panel lists its talents.
const noGeometrySidecar = { activeSpec: 'Frost', talents: [{ specId: 98765, specName: 'Frost', spentPoints: 2,
  talents: [{ rank: 2, spellName: 'Frost Bolt', description: 'Ice.', castTime: '2.5 sec' }] }] };
const degraded = detail.talentsTabHtml(noGeometrySidecar);
const degradedSubs = subTabButtons(degraded);
ok(degradedSubs.length === 1 && subTabPanels(degraded).length === 1 && countOf('role="tablist"', degraded) === 1,
  'a single-spec character still gets the sub-tab row, with one tab (a stable shape beats a panel that grows a bar for two specs)');
ok(degradedSubs[0].selected === 'true' && degradedSubs[0].tabindex === '0'
  && degradedSubs[0].specActive === 'true' && degradedSubs[0].meta === '2 pts',
  `the one tab is selected, focusable, marked active and carries its spent points (${degradedSubs[0] ? `${degradedSubs[0].name} ${degradedSubs[0].meta}` : 'none'})`);
ok(subPanelSlice(degraded, 0).includes('No tree geometry synced for this spec')
  && subPanelSlice(degraded, 0).includes('(2)') && subPanelSlice(degraded, 0).includes('Ice.'),
  'the degrade path lives inside the sub-panel: the compact selected-talent list, ranks and descriptions intact');
ok(!subPanelSlice(degraded, 0).includes('wow-tree'), 'a group with no geometry renders no tree shell at all');
// A group list that disagrees with the cached selection falls back to the derived one rather than
// showing an empty panel (the sidecar can change under an open modal).
ok(detail.selectedSpecIndex(richSpecs, richSidecar, 42) === activeSpecPosition
  && detail.selectedSpecIndex(richSpecs, richSidecar, null) === activeSpecPosition
  && detail.selectedSpecIndex(richSpecs, { activeSpec: 'Not A Spec' }, null) === 0
  && detail.selectedSpecIndex([], richSidecar, 3) === 0,
  'selectedSpecIndex: out-of-range and unknown fall back to the active spec, then to the first group');
// The same fallbacks with the active specialization in the SECOND group. Every committed row in this
// roster happens to have activeSpec at index 0, where a "null means index 0" bug and "null means
// derive" agree; this fixture is what makes the derive path observable.
// A fresh open, with no reviewer choice in play: the keyboard pass above left a selection, and a
// chosen index legitimately wins over the derived one (that is the reviewer's pick surviving a
// repaint). Close first so every assertion below measures the derive path only.
detail.closeDetail();
const laterGroupSidecar = {
  activeSpec: richSpecs[1].specName,
  talents: [{ ...richSpecs[0] }, { ...richSpecs[1] }],
};
ok(richSpecs.length === 2 && richSpecs[1].specName !== richSidecar.activeSpec
  && detail.selectedSpecIndex(laterGroupSidecar.talents, laterGroupSidecar, null) === 1
  && detail.selectedSpecIndex(laterGroupSidecar.talents, laterGroupSidecar, undefined) === 1
  && detail.selectedSpecIndex(laterGroupSidecar.talents, laterGroupSidecar, '') === 1
  && detail.selectedSpecIndex(laterGroupSidecar.talents, laterGroupSidecar, 0) === 0,
  'a null/absent requested index DERIVES the active group rather than reading as index 0 (Number(null) === 0 made the documented fallback unreachable); an explicit 0 is still honoured');
const laterGroupRender = detail.talentsTabHtml(laterGroupSidecar);
const laterGroupTabs = subTabButtons(laterGroupRender);
ok(laterGroupTabs.filter(t => t.selected === 'true').map(t => t.subtab).join() === '1'
  && laterGroupTabs.filter(t => t.specActive === 'true').map(t => t.subtab).join() === '1'
  && laterGroupTabs.filter(t => t.tabindex === '0').map(t => t.subtab).join() === '1',
  `a fresh render of a character whose active spec is the second group selects THAT tab (${laterGroupTabs.map(t => `${t.subtab}:${t.selected}/${t.specActive}`).join(', ')}): aria-selected and data-wow-spec-active cannot disagree`);
ok(subTabPanels(laterGroupRender).filter(p => !p.classes.includes('hidden')).map(p => p.id).join()
  === 'detail-subpanel-1',
  'and the visible sub-panel is the active specialization\'s, not the first group\'s');
// A derived index the markup cannot address (the handler argument is capped at two digits) must not
// blank the panel: it falls back to the first group, which always renders.
ok(detail.selectedSpecIndex(Array.from({ length: 101 }, (unused, i) => ({ specId: 1000 + i, specName: `Spec ${i}` })),
  { activeSpec: 'Spec 100' }, null) === 0,
  'a derived index beyond the renderable range falls back to the first group instead of selecting an addressable-less tab');


detail.closeDetail();

// ---------- f. professions / stats / progress tab content ----------

detail.setDetailTab('professions');
const profPanelLive = panelSlice(contentHtml(), 'professions');
ok(countOf('wow-bar-fill', profPanelLive) > 0, 'the professions panel renders a skill bar per synced tier');
ok(profPanelLive.includes(esc(richSidecar.professions.primaries[0].name)),
  `the first primary profession renders (${richSidecar.professions.primaries[0].name})`);
const firstTier = richSidecar.professions.primaries[0].tiers[0];
const wantSkillPct = Math.min(100, Math.max(0, Math.round((firstTier.skillPoints / firstTier.maxSkillPoints) * 100)));
ok(profPanelLive.includes(`data-wow-kind="skill" style="width:${wantSkillPct}%"`),
  `skill bar is value/max maths (${firstTier.skillPoints}/${firstTier.maxSkillPoints} → ${wantSkillPct}%)`);
ok(profPanelLive.includes(`${esc(String(firstTier.skillPoints))}`) && profPanelLive.includes(`/ ${esc(String(firstTier.maxSkillPoints))}`),
  'the numeric skill line renders beside the bar');
ok(profPanelLive.includes(esc(firstTier.knownRecipes[0].name)), `known recipes render (${firstTier.knownRecipes[0].name})`);

detail.setDetailTab('stats');
const statsPanelLive = panelSlice(contentHtml(), 'stats');
ok(statsPanelLive.includes('>Attributes<') && statsPanelLive.includes('>Combat<')
  && statsPanelLive.includes('>Crit, Hits &amp; Ratings<') && statsPanelLive.includes('>Resistances<'),
  'the four agreed stat groups render');
ok(statsPanelLive.includes(esc(String(richSidecar.stats.strength.effective)))
  && statsPanelLive.includes(`base ${esc(String(richSidecar.stats.strength.base))}`),
  `a {base, effective} pair shows effective with base in parentheses (${richSidecar.stats.strength.effective}, base ${richSidecar.stats.strength.base})`);
ok(statsPanelLive.includes(esc(richSidecar.stats.powerType.name)), `a {id, name} value renders its name (${richSidecar.stats.powerType.name})`);
// The tab formats with its own two-decimal rule, re-derived here rather than trusted.
const fmtStat = n => (Number.isInteger(Number(n)) ? String(Number(n)) : String(Math.round(Number(n) * 100) / 100));
ok(statsPanelLive.includes(`${esc(fmtStat(richSidecar.stats.meleeCrit.value))}%`),
  `a rating triple renders as a percentage (${richSidecar.stats.meleeCrit.value} → ${fmtStat(richSidecar.stats.meleeCrit.value)}%)`);
ok(!statsPanelLive.includes('[object Object]'), 'no stat value is ever stringified as an object');

detail.setDetailTab('progress');
const progressPanelLive = panelSlice(contentHtml(), 'progress');
ok(!richSidecar.progress && progressPanelLive.includes(esc('No raid, Mythic+ or PvP progress synced yet.')),
  'the rich row carries no progress block, and its Progress tab says so instead of disappearing');
const progressPanelFor = sidecar => panelSlice(detail.detailHtml(richRow, sidecar, null, 'ok', '<span></span>', ''), 'progress');
const progressPanel = progressPanelFor({ ...sparseSidecar, progress: {
  raid: { raidProgress: [{ name: 'Molten Core', progress: { completedCount: 10, possibleCount: 10 } }] },
  mythicPlus: { currentRatings: [{ season: 12, keystoneLevel: 15 }] },
  pvp: { honorableKills: 34, pvpRank: 5 },
} });
ok(progressPanel.includes('>Raid<') && progressPanel.includes('Molten Core · Completed')
  && progressPanel.includes('>Mythic+<') && progressPanel.includes('Keystone level')
  && progressPanel.includes('>PvP<') && progressPanel.includes('Honorable kills'),
  'the raid / Mythic+ / PvP blocks moved into the tab unchanged (T2.8): same headings, same row labels');

// ---------- g. reputation bar maths ----------

detail.setDetailTab('reputation');
const repPanel = panelSlice(contentHtml(), 'reputation');
const repRows = (richSidecar.reputations || []).filter(r => r && typeof r === 'object');
const repBars = [...repPanel.matchAll(/data-wow-kind="([^"]+)" style="width:([\d.]+)%"/g)].map(m => ({ kind: m[1], pct: Number(m[2]) }));
const valuePct = row => {
  const standing = row.standing || {};
  const value = Number(standing.value); const max = Number(standing.max);
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((value / max) * 100)));
};
const rawPct = row => {
  const standing = row.standing || {};
  const value = Number(standing.raw); const max = Number(standing.max);
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((value / max) * 100)));
};
ok(repBars.length === repRows.length && repBars.every((bar, i) => bar.pct === valuePct(repRows[i])),
  `all ${repRows.length} reputation bars are value/max of the current standing (${repBars.map(b => b.pct).join(', ') || 'none'})`);
const crossed = repRows.find(r => r.standing && Number(r.standing.raw) !== Number(r.standing.value));
ok(!!crossed && repBars[repRows.indexOf(crossed)].pct === valuePct(crossed)
  && rawPct(crossed) !== valuePct(crossed),
  `${crossed ? crossed.factionName : '—'} proves value not raw: ${valuePct(crossed || {})}% drawn, ${rawPct(crossed || {})}% would be raw/max`);
ok(!!crossed && repPanel.includes(`· total ${esc(String(crossed.standing.raw))}`),
  `the raw total is still on the row (${crossed ? crossed.standing.raw : '—'})`);
ok(!!crossed && repPanel.includes(`data-wow-tier="${esc(crossed.standing.name.toLowerCase())}"`)
  && repPanel.includes(`>${esc(crossed.standing.name)}</span>`)
  && repPanel.includes(`data-wow-kind="rep-${esc(crossed.standing.name.toLowerCase())}"`),
  `the standing label and its bar colour key come from the payload (${crossed ? crossed.standing.name : '—'})`);
ok(detail.reputationTierLabel({ tier: 0 }) === 'Hated' && detail.reputationTierLabel({ tier: 7 }) === 'Exalted'
  && detail.reputationTierLabel({ tier: 8 }) === '' && detail.reputationTierLabel({ tier: -1 }) === ''
  && detail.reputationTierLabel({ tier: 42 }) === '' && detail.reputationTierLabel({}) === '',
  'reputationTierLabel is the 0-based classic table, and refuses out-of-range or absent tiers');
ok(detail.reputationTierLabel({ tier: 2, name: 'Exalted' }) === 'Exalted'
  && detail.reputationTierLabel({ tier: 3, name: 'Not A Standing' }) === 'Neutral',
  'a payload standing name wins over the table, and an unknown name falls back to the tier index');
const repEdge = detail.reputationTabHtml({ reputations: [
  { factionName: 'No Max', standing: { value: 50 } },
  { factionName: 'No Value', standing: { max: 100 } },
  { factionName: 'Unknown Tier', standing: { value: 10, max: 100, tier: 42 } },
  { factionName: 'Over Full', standing: { value: 400, max: 100, tier: 7 } },
  { factionName: 'Broken Standing', standing: 'not-an-object' },
  'not-an-entry',
] });
ok(countOf('rounded border border-slate-800 bg-slate-950/50 p-1.5 space-y-1', repEdge) === 5
  && !repEdge.includes('not-an-entry'), 'a malformed reputation entry drops without taking the panel with it');
ok(/>No Max<([\s\S]*?)width:0%([\s\S]*?)>50<\/div>/.test(repEdge)
  && !/>No Max<([\s\S]*?)\/ 0</.test(repEdge),
  'a standing with no max draws an empty bar and prints the value without a fake denominator');
ok(/>No Value<([\s\S]*?)width:0%([\s\S]*?)— \/ 100/.test(repEdge),
  'a standing with no value says — rather than NaN, and keeps the max it does have');
ok(repEdge.includes('Unknown standing') && repEdge.includes('data-wow-kind="rep"'),
  'an unknown tier falls back to the neutral bar and says "Unknown standing"');
const edgeBars = [...repEdge.matchAll(/style="width:([\d.]+)%"/g)].map(m => Number(m[1]));
ok(edgeBars.length === 5 && edgeBars[3] === 100 && edgeBars.every(v => v >= 0 && v <= 100),
  `an over-full value clamps to 100% and nothing exceeds the rail (${edgeBars.join(', ')})`);
ok(!/NaN|undefined|Infinity/.test(repEdge), 'no NaN/undefined/Infinity in the reputation edge cases');

// ---------- h. empty states: the bar cannot change, the panels all speak ----------

detail.closeDetail();
const sparseHtml = openRow(SPARSE_ID);
const EMPTY_STATES = {
  gear: 'No gear data synced yet.',
  talents: 'No talents data synced yet.',
  professions: 'No professions data synced yet.',
  reputation: 'No reputation data synced yet.',
  stats: 'No combat statistics synced yet.',
  progress: 'No raid, Mythic+ or PvP progress synced yet.',
};
const emptyMissing = Object.keys(EMPTY_STATES).filter(id => panelSlice(sparseHtml, id).includes(esc(EMPTY_STATES[id])) === false);
ok(emptyMissing.length === 0,
  `a status-only row shows all six distinct empty states (${Object.values(EMPTY_STATES).length} messages)${emptyMissing.length ? ` — missing ${emptyMissing.join(', ')}` : ''}`);
ok(new Set(Object.values(EMPTY_STATES)).size === 6
  && Object.keys(EMPTY_STATES).filter(id => countOf(esc(EMPTY_STATES[id]), sparseHtml) > 1).length === 0,
  'the six messages are distinct and each appears exactly once');
ok(tabBarSlice(sparseHtml) === tabBarSlice(richHtml),
  'the tab bar markup is byte-identical between the data-rich row and the status-only row (decision #5)');
ok(tabButtons(sparseHtml).map(b => `${b.tab}:${b.selected}:${b.tabindex}`).join('|')
  === tabButtons(richHtml).map(b => `${b.tab}:${b.selected}:${b.tabindex}`).join('|'),
  'tab count, order, aria-selected and roving tabindex are identical across a rich row and an empty one');
ok(tabPanels(sparseHtml).map(p => `${p.id}:${p.classes.includes('hidden') ? 'hidden' : 'shown'}:${p.focusable ? 'focusable' : '-'}`).join('|')
  === tabPanels(richHtml).map(p => `${p.id}:${p.classes.includes('hidden') ? 'hidden' : 'shown'}:${p.focusable ? 'focusable' : '-'}`).join('|'),
  'the six panels are the same six panels, in the same visibility pattern, whatever the row carries');
ok(tabButtons(sparseHtml).length === 6 && countOf('disabled', tabBarSlice(sparseHtml)) === 0,
  'no tab is hidden or disabled on the sparse row either (decision #5)');
const builderEmpties = [
  ['gearTabHtml', detail.gearTabHtml], ['talentsTabHtml', detail.talentsTabHtml],
  ['professionsTabHtml', detail.professionsTabHtml], ['reputationTabHtml', detail.reputationTabHtml],
  ['statsTabHtml', detail.statsTabHtml],
];
const builderFails = builderEmpties.filter(([name, fn]) => {
  const rendered = fn({});
  const renderedNull = fn(null);
  return !/text-slate-500 italic/.test(rendered) || !/text-slate-500 italic/.test(renderedNull)
    || rendered === '' || renderedNull === '';
});
ok(builderFails.length === 0,
  `every panel builder returns a styled empty state for {} and null (${builderEmpties.map(([n]) => n).join(', ')})`);
ok(detail.gearTabHtml({ gear: [] }) === detail.gearTabHtml(null)
  && detail.statsTabHtml({ stats: {} }) === detail.statsTabHtml(null)
  && detail.professionsTabHtml({ professions: { primaries: [], secondaries: [] } }) === detail.professionsTabHtml(null),
  'an empty collection and an absent block produce the identical message (no half-built panels)');
ok(detail.talentsTabHtml({ talents: [] }) === detail.talentsTabHtml({ talents: [null] })
  && detail.talentsTabHtml({ talents: [] }).includes('No spec chosen yet'),
  'a character with no specialization gets the "no spec chosen" message, not a blank tab');
ok(detail.statsTabHtml({ stats: { strength: { base: 'x', effective: 'y' } } }) === detail.statsTabHtml(null),
  'a stat that cannot be named is skipped, and a stats block of only those empties the tab');
ok(detail.statsTabHtml({ stats: { strength: 12 } }).includes('>Strength<')
  && detail.statsTabHtml({ stats: { notAStatKey: 5 } }) === detail.statsTabHtml(null),
  'the stats tab only renders keys the STAT_GROUPS table can name');
ok(!detail.statsTabHtml({ stats: { strength: { base: 1, effective: 1 } } }).includes('base 1'),
  'a base==effective pair does not repeat the base value');
ok(/No tree geometry synced for this spec/.test(detail.talentsTabHtml({ activeSpec: 'Frost', talents:
  [{ specId: 98765, specName: 'Frost', spentPoints: 1, talents: [{ rank: 2, spellName: 'Frost Bolt', description: 'Ice.', castTime: '2.5 sec', iconUrl: 'https://evil.tld/i.jpg' }] }] }))
  && detail.talentsTabHtml({ talents: [{ specId: 98765, specName: 'Frost', talents: [{ rank: 2, spellName: 'Frost Bolt', description: 'Ice.' }] }] }).includes('(2)')
  && detail.talentsTabHtml({ talents: [{ specId: 98765, specName: 'Frost', talents: [{ rank: 2, spellName: 'Frost Bolt', description: 'Ice.' }] }] }).includes('Ice.'),
  'a spec with no synced geometry falls back to the compact selected-talent list (rank + description)');
ok(!detail.talentsTabHtml({ talents: [{ specId: 98765, specName: 'Frost', talents: [{ rank: 2, spellName: 'Frost Bolt', iconUrl: 'https://evil.tld/i.jpg' }] }] }).includes('evil.tld'),
  'a rejected talent icon is dropped outright, not echoed into the fallback list');

// ---------- i. rgba() and tree-pixel poison-proofing ----------

const RGBA_CASES = [
  [{ r: 255, g: 255, b: 255, a: 1 }, 'rgba(255, 255, 255, 1)'],
  [{ r: 0, g: 255, b: 0 }, 'rgba(0, 255, 0, 1)'],
  [{ r: -40, g: 900, b: 12.6 }, 'rgba(0, 255, 13, 1)'],
  [{ r: 0, g: 0, b: 0, a: 5 }, 'rgba(0, 0, 0, 1)'],
  [{ r: 0, g: 0, b: 0, a: -1 }, 'rgba(0, 0, 0, 0)'],
  [{ r: 0, g: 0, b: 0, a: 0.2567 }, 'rgba(0, 0, 0, 0.257)'],
  [{ r: 1, g: 1 }, ''], [{ r: '1px;} x', g: 0, b: 0 }, ''], [{ r: Infinity, g: 0, b: 0 }, ''],
  ['a string', ''], [null, ''], [undefined, ''], [[1, 2, 3], ''], [{}, ''],
];
const rgbaFails = RGBA_CASES.filter(([input, want]) => detail.rgbaColor(input) !== want);
ok(rgbaFails.length === 0,
  `rgbaColor rebuilds every colour triple from clamped components (${RGBA_CASES.length} cases)${rgbaFails.length ? ` — ${rgbaFails.map(([i, w]) => `${JSON.stringify(i)} → ${detail.rgbaColor(i) || "''"} want ${w || "''"}`).join('; ')}` : ''}`);
ok(RGBA_CASES.every(([input]) => !/[;<>]/.test(detail.rgbaColor(input))),
  'no colour triple can smuggle a delimiter, tag character or declaration into the style attribute');
ok(detail.qualityKey('EPIC') === 'epic' && detail.qualityKey('rare') === 'rare'
  && detail.qualityKey('NONSENSE') === '' && detail.qualityKey(null) === '' && detail.qualityKey('constructor') === '',
  'qualityKey maps the API vocabulary only, and a prototype key yields no class');
// The stat lines live in the tooltip, so the poisoned row is driven end to end: detailHtml builds
// the registry, showDetailTooltip renders the payload a hover would have picked.
const poisonSidecar = { ...sparseSidecar, gear: [
  { slot: 'Head', name: 'Plain Hat', quality: 'POOR', stats: [{ displayString: 'Equip: +10', isEquipBonus: true }, { displayString: '+5 Stamina' }] },
  { slot: 'Chest', name: '<img src=x onerror=alert(1)>', quality: 'nonsense', iconUrl: 'https://evil.tld/x.jpg', stats: [{ displayString: 'poisoned', color: { r: '1px; color:red', g: 0, b: 0 } }] },
] };
const gearPoison = detail.detailHtml(richRow, poisonSidecar, null, 'ok', '<span></span>', '');
ok(gearPoison.includes('data-wow-quality="poor"') && !/data-wow-quality="(nonsense|constructor)"/.test(gearPoison),
  'a known quality gets its CSS key, an unknown one gets no colour attribute at all');
ok(gearPoison.includes('&lt;img src=x onerror=alert(1)&gt;') && !gearPoison.includes('<img src=x'),
  'an item name carrying markup is escaped in the gear grid');
ok(/wow-slot-glyph/.test(gearPoison) && !gearPoison.includes('evil.tld'),
  'a rejected item icon renders the empty socket and does not name the host');
detail.showDetailTooltip({ currentTarget: triggerOf({ left: 10, right: 20, top: 10, bottom: 20 }) }, 1);
ok(!tip.innerHTML.includes('style="color:') && !tip.innerHTML.includes('px;')
  && tip.innerHTML.includes('text-slate-300') && tip.innerHTML.includes('poisoned'),
  'a poisoned stat colour produces no style attribute (and no truncated declaration), just the class colour');
detail.showDetailTooltip({ currentTarget: triggerOf({ left: 10, right: 20, top: 10, bottom: 20 }) }, 0);
ok(tip.innerHTML.includes('text-emerald-300/90') && tip.innerHTML.includes('text-slate-300')
  && tip.innerHTML.includes('Equip: +10') && tip.innerHTML.includes('+5 Stamina'),
  'an equip bonus and a plain stat fall back to their class colours when no colour is supplied');
detail.hideDetailTooltip();
const CLAMP_CASES = [[0, 0], [8, 8], [-5, 0], [42.4, 42], ['42', 42], [1e12, detail.TREE_PIXEL_MAX],
  [-1e12, 0], [Infinity, 0], [NaN, 0], ['abc', 0], [undefined, 0], [null, 0],
  [detail.TREE_PIXEL_MAX - 0.4, detail.TREE_PIXEL_MAX]];
const clampFails = CLAMP_CASES.filter(([input, want]) => detail.clampTreePx(input) !== want);
ok(clampFails.length === 0,
  `clampTreePx coerces and bounds every derived pixel (${CLAMP_CASES.length} cases)${clampFails.length ? ` — ${clampFails.map(([i, w]) => `${String(i)} → ${detail.clampTreePx(i)} want ${w}`).join('; ')}` : ''}`);
ok(detail.TREE_PIXEL_MAX === 100000 && detail.TREE_CELL === 68 && detail.TREE_NODE === 52,
  'the tree constants stay the documented 68px grid / 52px node / 100000px cap (app.css parity)');
ok(/wow-tree overflow-hidden[\s\S]{0,200}style="width:\d+px;height:\d+px"/.test(talentsHtml),
  'the tree container box is emitted in whole px (an unbounded number would not match)');

// ---------- j. avatar / icon allowlist gating (T2.2) ----------

const headerFor = sidecar => detail.detailHtml(richRow, sidecar, null, 'ok', '<span></span>', '');
const richHeader = headerFor(richSidecar);
ok(richHeader.includes(`src="${esc(richSidecar.media.avatarSd)}"`) && !richHeader.includes(`src="${esc(richSidecar.media.avatar)}"`),
  `the header prefers avatar-sd over avatar (${richSidecar.media.avatarSd})`);
ok(headerFor({ media: { avatarSd: 'https://evil.tld/x.jpg', avatar: 'https://render.worldofwarcraft.com/ok.jpg' } })
  .includes('src="https://render.worldofwarcraft.com/ok.jpg"'),
  'a poisoned avatar-sd falls through to the allowlisted avatar rather than vanishing');
const avatarEvil = headerFor({ media: { avatarSd: 'https://evil.tld/x.jpg', avatar: '//evil.tld/y.jpg' } });
ok(!avatarEvil.includes('evil.tld') && avatarEvil.includes('wow-avatar-none'),
  'two rejected media URLs render the initial placeholder, and neither host is named on the page');
ok(headerFor({ media: { avatarSd: 'javascript:alert(1)' } }).includes('wow-avatar-none')
  && !headerFor({ media: { avatarSd: 'javascript:alert(1)' } }).includes('javascript:'),
  'a javascript: avatar URL never reaches a src attribute');
ok(headerFor({}).includes('wow-avatar-none') && headerFor(null).includes('wow-avatar-none'),
  'a row with no media block at all still renders a placeholder avatar');
ok(headerFor({ media: 'not-an-object' }).includes('wow-avatar-none'),
  'a non-object media block degrades to the placeholder instead of throwing');
const allModalImgs = [...openRow(RICH_ID).matchAll(/<img src="([^"]+)"/g)].map(m => m[1]);
ok(allModalImgs.length > 0 && allModalImgs.every(src => detail.isSafeIconUrl(src)),
  `every <img> anywhere in the modal is on the Blizzard allowlist (${allModalImgs.length} images)`);

// ---------- k. inline handlers resolve against the global scope ----------

const handlerNames = html => [...new Set([...html.matchAll(/\bon[a-z]+="([A-Za-z_$][\w$]*)\(/g)].map(m => m[1]))];
const modalHandlers = [...new Set([...handlerNames(richHtml), ...handlerNames(sparseHtml),
  ...handlerNames(talentsHtml), ...handlerNames(repPanel), ...handlerNames(profPanelLive),
  ...handlerNames(statsPanelLive)])];
const dangling = modalHandlers.filter(name => typeof globalThis[name] !== 'function');
ok(modalHandlers.length > 0 && dangling.length === 0,
  `every inline handler the modal emits is a global function (${modalHandlers.sort().join(', ')})${dangling.length ? ` — dangling: ${dangling.join(', ')}` : ''}`);
ok(['setDetailTab', 'detailTabKeydown', 'showDetailTooltip', 'hideDetailTooltip'].every(name => modalHandlers.includes(name)),
  'the four Phase 2 handlers are the ones the markup actually calls (the scan above is not vacuous)');
ok(window.setDetailTab === detail.setDetailTab && window.detailTabKeydown === detail.detailTabKeydown
  && window.showDetailTooltip === detail.showDetailTooltip && window.hideDetailTooltip === detail.hideDetailTooltip,
  'window exposes the same function objects the ES module exports (no shim copy drifted)');
ok([...richHtml.matchAll(/setDetailTab\('([^']*)'\)/g)].every(m => TAB_IDS.includes(m[1]))
  && [...richHtml.matchAll(/detailTabKeydown\(event, '([^']*)'\)/g)].every(m => TAB_IDS.includes(m[1])),
  'every tab handler argument is a known tab id: no sidecar string can reach the handler');

// ---------- m. Phase 4b (F2/F3/F4): the mismatch flag, the guarded tie, Blizzard icons ----------

const CLASS_ICON = 'https://render.worldofwarcraft.com/us/profile-icons/class/3.jpg';
const RACE_ICON = 'https://us.api.blizzard.com/profile-icons/race/6.jpg';
const agreeSidecar = { ...sparseSidecar, race: { id: 5, name: richRow.race }, class: { id: 1, name: richRow.class } };
const disagreeSidecar = { ...sparseSidecar, race: { id: 9, name: 'Tauren' }, class: { id: 3, name: 'Hunter' } };
const mismatchRow = { ...richRow, race: 'Orc', class: 'Shaman' };

ok(detail.identityMismatchRows(mismatchRow, disagreeSidecar).length === 2
  && detail.identityMismatchRows(mismatchRow, disagreeSidecar).map(r => r.field).join('·') === 'race·class',
  'both disagreeing fields are reported, in roster order');
ok(JSON.stringify(detail.identityMismatchRows({ ...richRow, class: 'Shaman' }, { class: { id: 3, name: 'Hunter' } }))
  === JSON.stringify([{ field: 'class', roster: 'Shaman', bnet: 'Hunter', id: '3' }]),
  'a class mismatch carries both spellings and the Battle.net class id');
ok(detail.identityMismatchRows(richRow, agreeSidecar).length === 0,
  'an agreeing sidecar is not a warning (the flag would train reviewers to ignore it)');
ok(detail.identityMismatchRows({ ...richRow, race: richRow.race.toLowerCase(), class: richRow.class.toUpperCase() }, agreeSidecar).length === 0,
  'the comparison is case-insensitive: ROSTER says WARRIOR and Battle.net says Warrior is the same character');
ok([sparseSidecar, null, undefined, {}, { race: 'Orc' }, { race: {}, class: [] }, { race: 7, class: 'x' }]
  .every(side => detail.identityMismatchRows(richRow, side).length === 0
    && detail.identityMismatchHtml(richRow, side) === ''),
  'an absent or malformed identity block is "nothing to say", not a mismatch and not a throw');
ok(detail.identityMismatchRows({ ...richRow, race: '', class: null }, disagreeSidecar).length === 0,
  'a roster row with no race/class of its own cannot disagree with anything');
const flagHtml = detail.identityMismatchHtml(mismatchRow, disagreeSidecar);
ok(flagHtml.includes('Roster says') && flagHtml.includes('>Tauren</span>') && flagHtml.includes('>Hunter</span>')
  && flagHtml.includes('(id 9)') && flagHtml.includes('(id 3)'),
  'the warning names both sources per field: what the roster says, what Battle.net says, and the id');
ok(flagHtml.includes('⚠') && !/disabled/.test(flagHtml) && !/onclick=/.test(flagHtml),
  'it is a notice, not a control: no handler, nothing disabled');
const flaggedModal = detail.detailHtml(mismatchRow, disagreeSidecar, null, 'ok', '<span></span>', '');
ok(flaggedModal.indexOf('Race / class mismatch') > flaggedModal.indexOf('<h3')
  && flaggedModal.indexOf('Race / class mismatch') < flaggedModal.indexOf('role="tablist"'),
  'the flag renders in the header area, above the tab bar — a reviewer sees it before choosing a tab');
ok(tabButtons(flaggedModal).length === 6 && tabPanels(flaggedModal).length === 6
  && tabBarSlice(flaggedModal) === tabBarSlice(detail.detailHtml(richRow, agreeSidecar, null, 'ok', '<span></span>', '')),
  'a mismatching row still gets the full six-tab shell with a byte-identical bar: the flag is warning-only');
const flaggedEve = detail.detailHtml(mismatchRow,
  { ...disagreeSidecar, class: { id: 3, name: '<img src=x onerror=alert(1)>' } }, null, 'ok', '<span></span>', '');
ok(flaggedEve.includes('&lt;img src=x onerror=alert(1)&gt;') && !flaggedEve.includes('<img src=x'),
  'a Battle.net race/class name is escaped where the flag prints it');

// ---- the tie guard (F3): the vocabularies, then the decision table ----
ok(detail.ROSTER_RACE_VOCABULARY.length === 10 && detail.ROSTER_CLASS_VOCABULARY.length === 9
  && detail.ROSTER_PROFESSION_VOCABULARY.length === 9,
  `the tie guards with the three frozen vocabularies (${detail.ROSTER_RACE_VOCABULARY.length} races from editchar.js, ${detail.ROSTER_CLASS_VOCABULARY.length} classes from CLASS_COLORS, ${detail.ROSTER_PROFESSION_VOCABULARY.length} professions)`);
ok(detail.ROSTER_RACE_VOCABULARY.includes(richRow.race) && detail.ROSTER_CLASS_VOCABULARY.includes(richRow.class)
  && detail.rosterClassesForRace(richRow.race).includes(richRow.class),
  `the committed roster row is inside them (${richRow.race} ${richRow.class}), so the guard is the list the roster is already written against`);
ok(detail.canonicalVocabularyName('hunter', detail.ROSTER_CLASS_VOCABULARY) === 'Hunter'
  && detail.canonicalVocabularyName('  SHAMAN ', detail.ROSTER_CLASS_VOCABULARY) === 'Shaman'
  && detail.canonicalVocabularyName('Nief', detail.ROSTER_CLASS_VOCABULARY) === ''
  && detail.canonicalVocabularyName('', detail.ROSTER_RACE_VOCABULARY) === ''
  && detail.canonicalVocabularyName(null, detail.ROSTER_RACE_VOCABULARY) === '',
  'canonicalVocabularyName trims, matches case-insensitively, and returns the vocabulary\'s own spelling');
ok(detail.canonicalVocabularyName('constructor', detail.ROSTER_CLASS_VOCABULARY) === ''
  && detail.canonicalVocabularyName('toString', detail.ROSTER_RACE_VOCABULARY) === ''
  && detail.rosterClassesForRace('constructor').length === 0
  && detail.rosterClassesForRace('toString').length === 0,
  'a prototype-named race/class resolves to nothing at all (both lookups walk own list members)');

const TIE_ROW = { id: 'TIE', race: 'Orc', class: 'Warrior', prof1: 'Mining', prof2: 'None' };
const tie = (row, sidecar) => detail.sidecarTie(row, sidecar);
ok(JSON.stringify(tie(TIE_ROW, { race: { id: 9, name: 'Tauren' }, class: { id: 11, name: 'Druid' } }).patch)
  === JSON.stringify({ race: 'Tauren', class: 'Druid' }),
  'a legal Tauren Druid on an Orc Warrior row ties both fields');
const agreeTie = tie({ ...TIE_ROW, race: 'Orc', class: 'Warrior' }, { race: { id: 5, name: 'orc' }, class: { id: 1, name: 'WARRIOR' } });
ok(agreeTie.written.length === 0 && agreeTie.skipped.length === 0 && Object.keys(agreeTie.patch).length === 0,
  'an identity that already matches (any case) writes nothing and says nothing');
ok(JSON.stringify(tie(TIE_ROW, { race: { id: 9, name: 'Tauren' } }).patch) === JSON.stringify({ race: 'Tauren' }),
  'a race-only sidecar still ties the race when the row\'s class is legal for it (Warrior is a Tauren class)');
ok(JSON.stringify(tie(TIE_ROW, { class: { id: 3, name: 'Hunter' } }).patch) === JSON.stringify({ class: 'Hunter' }),
  'a class-only sidecar ties the class when the row\'s race allows it (an Orc can be a Hunter)');
const illegalPair = tie(TIE_ROW, { race: { id: 9, name: 'Tauren' }, class: { id: 5, name: 'Priest' } });
ok(Object.keys(illegalPair.patch).length === 0 && illegalPair.skipped.length === 1
  && /Tauren Priest/.test(illegalPair.skipped[0]),
  `an impossible pair is skipped whole, not half-applied (${illegalPair.skipped[0]})`);
const raceOnlyIllegal = tie({ race: 'Tauren', class: 'Druid', prof1: 'None', prof2: 'None' }, { race: { id: 4, name: 'Gnome' } });
ok(Object.keys(raceOnlyIllegal.patch).length === 0 && raceOnlyIllegal.skipped.length === 1,
  'a race the row\'s current class cannot belong to is skipped (writing it alone would break the pair)');
const classOnlyIllegal = tie({ ...TIE_ROW, race: 'Tauren' }, { class: { id: 6, name: 'Mage' } });
ok(Object.keys(classOnlyIllegal.patch).length === 0 && classOnlyIllegal.skipped.length === 1,
  'and the mirror case: a class the row\'s race cannot be is skipped');
const unknowns = tie(TIE_ROW, { race: { id: 98, name: 'Goblin' }, class: { id: 99, name: 'Monk' } });
ok(Object.keys(unknowns.patch).length === 0 && unknowns.skipped.join(' ').includes('Goblin')
  && unknowns.skipped.join(' ').includes('Monk'),
  'race and class outside the frozen lists are both refused and both named');
ok(tie(TIE_ROW, { race: { name: 'constructor' }, class: { name: 'constructor' } }).skipped.length === 2
  && Object.keys(tie(TIE_ROW, { race: { name: 'constructor' } }).patch).length === 0,
  'a sidecar whose identity is a prototype key writes nothing');
ok(tie(TIE_ROW, { race: { name: 'tauren' }, class: { id: 11, name: 'dRuId' } }).patch.class === 'Druid',
  'the value written is the vocabulary\'s spelling, never the wire\'s');
ok(Object.keys(tie(null, agreeSidecar).patch).length === 0 && Object.keys(tie(TIE_ROW, null).patch).length === 0
  && Object.keys(tie(TIE_ROW, 'not-an-object').patch).length === 0,
  'no row, no sidecar, or a sidecar that is not an object all tie nothing');

const profTie = tie({ ...TIE_ROW, prof1: 'None', prof2: 'None' },
  { professions: { primaries: [{ name: 'H erbalism' }, { name: 'Blacksmithing' }] } });
ok(profTie.patch.prof1 === 'Blacksmithing' && profTie.patch.prof2 === undefined
  && profTie.skipped.join(' ').includes('H erbalism'),
  'a profession the matrix cannot count is skipped and named; the usable one moves up to prof1 and prof2 keeps the reviewer\'s value');
const profTwo = tie(TIE_ROW, { professions: { primaries: [{ name: 'herbalism' }, { name: 'Skinning' }] } });
ok(profTwo.patch.prof1 === 'Herbalism' && profTwo.patch.prof2 === 'Skinning',
  'the first two in-vocabulary primaries become prof1 and prof2, canonical-cased');
const profOne = tie({ ...TIE_ROW, prof1: 'Mining' }, { professions: { primaries: [{ name: 'Mining' }, { name: 'Fishing' }] } });
ok(profOne.patch.prof1 === undefined && profOne.patch.prof2 === undefined
  && profOne.skipped.join('') === 'profession Fishing',
  'one usable primary ties nothing when the row already agrees, and Fishing is the only skip note');
const profNone = tie(TIE_ROW, { professions: {} });
ok(Object.keys(profNone.patch).length === 0 && profNone.skipped.length === 0,
  'no primaries synced is not a skip: prof1/prof2 are left exactly as they were (never overwritten with None)');
ok(Object.keys(tie(TIE_ROW, { professions: { primaries: ['Mining', 'Skinning', 'Mining'] } }).patch).join() === 'prof2'
  && tie(TIE_ROW, { professions: { primaries: ['Mining', 'Skinning', 'Mining'] } }).patch.prof2 === 'Skinning',
  'string entries and duplicates are handled: the vocabulary dedupes before it writes (prof1 already says Mining)');
ok(tie(TIE_ROW, { professions: 'not-an-object' }).skipped.length === 0,
  'a professions block that is not an object yields nothing rather than throwing');
ok(JSON.stringify(detail.sidecarPrimaryProfessions({ professions: { primaries: [{ name: 'Mining' }, null, {}, { name: 'Nief' }] } }))
  === JSON.stringify({ known: ['Mining'], unknown: ['Nief'] }),
  'sidecarPrimaryProfessions drops empty/malformed entries and sorts the rest into known/unknown');

const tieTarget = { ...TIE_ROW };
const applied = detail.applySidecarTie(tieTarget, { race: { id: 9, name: 'Tauren' }, class: { id: 11, name: 'Druid' } });
ok(tieTarget.race === 'Tauren' && tieTarget.class === 'Druid'
  && applied.written.join('·') === 'race Tauren·class Druid'
  && TIE_ROW.race === 'Orc' && TIE_ROW.class === 'Warrior',
  'applySidecarTie mutates the row it is handed (the commit\'s fresh copy) and reports what it wrote');
// The two profession slots are written as a PAIR: a sidecar that offers for prof1 what the row
// already carries in prof2 must not produce a character with the same profession twice.
const dupSidecar = primaries => ({ professions: { primaries, secondaries: [] } });
const dupTie = detail.sidecarTie({ race: 'Troll', class: 'Hunter', prof1: 'None', prof2: 'Skinning' },
  dupSidecar([{ name: 'Skinning' }]));
ok(Object.keys(dupTie.patch).length === 0 && dupTie.written.length === 0
  && dupTie.skipped.join().includes('profession Skinning'),
  `a profession already held in prof2 is not written into prof1 (${JSON.stringify(dupTie.patch)} / ${dupTie.skipped.join('; ') || 'no skips'})`);
const swapTie = detail.sidecarTie({ race: 'Troll', class: 'Hunter', prof1: 'Mining', prof2: 'None' },
  dupSidecar([{ name: 'Skinning' }, { name: 'Mining' }]));
ok(swapTie.patch.prof1 === 'Skinning' && swapTie.patch.prof2 === 'Mining'
  && swapTie.patch.prof1 !== swapTie.patch.prof2,
  'a full pair still writes both slots (Mining/Skinning may swap order), and the two slots differ');
const keepTie = detail.sidecarTie({ race: 'Troll', class: 'Hunter', prof1: 'Skinning', prof2: 'Skinning' },
  dupSidecar([{ name: 'Skinning' }]));
ok(Object.keys(keepTie.patch).length === 0 && keepTie.skipped.length === 0,
  'a row that already matches the sidecar writes nothing and reports nothing (the note stays empty)');
ok(detail.sidecarTieNote({ written: [], skipped: [] }) === '' && detail.sidecarTieNote(null) === '',
  'a tie that did nothing adds no words to the success message (it stays byte-identical for the common case)');
ok(/^ Roster tied to Battle.net: race Tauren\.$/.test(detail.sidecarTieNote({ written: ['race Tauren'], skipped: [] }))
  && /Skipped \(outside the roster vocabulary\): profession Fishing\.$/.test(detail.sidecarTieNote({ written: [], skipped: ['profession Fishing'] })),
  'the note reports writes and skips separately, so a partial tie cannot read as a full one');
const boundedNote = detail.sidecarTieNote({ written: [],
  skipped: Array.from({ length: detail.TIE_NOTE_MAX_SKIPPED + 3 }, (unused, i) => `profession P${i + 1}`) });
ok(boundedNote.includes('profession P1') && boundedNote.includes(`profession P${detail.TIE_NOTE_MAX_SKIPPED}`)
  && !boundedNote.includes('profession P7') && boundedNote.includes('(+3 more).'),
  `a sidecar entirely outside the vocabulary names ${detail.TIE_NOTE_MAX_SKIPPED} skips and counts the rest (${boundedNote.trim()})`);
ok(detail.sidecarTieNote({ written: [], skipped: ['profession Fishing', 'class Paladin'] }).includes('(+') === false,
  'a short skipped list is listed in full, with no counting suffix');

// ---- the icons (F4) ----
const headerWithIcons = headerFor({ ...sparseSidecar, classIconUrl: CLASS_ICON });
ok(headerWithIcons.includes(`src="${esc(CLASS_ICON)}"`),
  'the header shows the class icon when the probe synced it');
ok(countOf('class="wow-id-icon"', headerFor(sparseSidecar)) === 1
  && countOf('<img src="', headerFor(sparseSidecar)) === 0,
  'with no media synced the header reserves exactly one 16px slot (the class) and draws no <img>: the text cannot shift between rows');
ok(countOf('class="wow-id-icon"', headerWithIcons) === 1 && countOf('<img src="', headerWithIcons) === 1,
  'the icon is the same single slot, filled (no second layout)');
// Race icons are proven not to exist (docs/bnet-api-notes.md §10.3: no media key on the race doc, no
// media index, no humantype family), so the header must not reserve a slot for one. A dashed box for
// a field the API will never serve is UI for a case that cannot happen.
const raceIconOnRow = headerFor({ ...sparseSidecar, raceIconUrl: RACE_ICON });
ok(countOf('class="wow-id-icon"', raceIconOnRow) === 1 && !raceIconOnRow.includes('race/6.jpg')
  && raceIconOnRow.includes('>Tauren</span>'),
  'a raceIconUrl on the row renders no box at all: the race is named in text and has no icon slot');
const iconPoison = headerFor({ ...sparseSidecar, classIconUrl: 'https://evil.tld/c.jpg' });
ok(!iconPoison.includes('evil.tld') && countOf('<img src="', iconPoison) === 0
  && countOf('class="wow-id-icon"', iconPoison) === 1,
  'a rejected icon URL renders the empty slot and does not name the host anywhere on the page');
ok(headerFor({ ...sparseSidecar, classIconUrl: 'javascript:alert(1)' }).includes('<img src="') === false
  && headerFor({ ...sparseSidecar, classIconUrl: '//render.worldofwarcraft.com/c.jpg' }).includes('<img src="') === false,
  'javascript: and protocol-relative icon URLs never reach a src attribute');
const profIcons = detail.professionsTabHtml({ professions: {
  primaries: [{ name: 'Mining', iconUrl: CLASS_ICON }, { name: 'Blacksmithing', iconUrl: 'https://evil.tld/x.jpg' }],
  secondaries: [{ name: 'Archaeology' }],
} });
ok(profIcons.includes(`src="${esc(CLASS_ICON)}"`) && !profIcons.includes('evil.tld')
  && countOf('class="wow-id-icon"', profIcons) === 3,
  'each profession row gets its own icon slot, gated by the same allowlist, and keeps the slot when empty');
ok(countOf('class="wow-id-icon"', detail.professionsTabHtml(sparseSidecar)) === 0,
  'a row with no professions block still gets the professions empty state, icons and all');
ok([...headerWithIcons.matchAll(/<img src="([^"]+)"/g)].every(m => detail.isSafeIconUrl(m[1])),
  'every icon the header emits passes isSafeIconUrl (the allowlist is the only gate, again)');

// ---------- l. the modal stays view state: nothing is persisted ----------

ok([...new Set(storageWrites.slice(writesBeforePhase2))].length === 0,
  `the whole tab/tooltip/tree pass wrote no localStorage key (${storageWrites.slice(writesBeforePhase2).join(', ') || 'no writes'})`);
detail.closeDetail();
state.setQuery('');
state.setFilter('all');

// ---------------------------------------------------------------- 5. detail modal content
console.log('\n[5] detail modal: sidecar present / absent / fetch_error, gear hosts, escaping');

// Fixtures live in a throwaway /tmp directory. The payloads below are inert strings; nothing
// is written inside the repo's data/.
const FIXTURES = mkdtempSync(path.join(os.tmpdir(), '3c-detail-'));
fs.mkdirSync(path.join(FIXTURES, 'data'), { recursive: true });
const nowStamp = new Date(Date.now() - 900000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const evilName = '<img src=x onerror=alert(1)>';
const fixtureRoster = {
  schemaVersion: 2,
  characters: [
    { id: 'A1', player: 'p1', name: 'Gearman', track: 'flowmasters', role: 'DPS', roleSec: 'None', race: 'Orc', class: 'Warrior', prof1: 'Mining', prof2: 'None' },
    { id: 'B2', player: 'p2', name: 'NoSidecar', track: 'family', role: 'DPS', roleSec: 'None', race: 'Gnome', class: 'Mage', prof1: 'None', prof2: 'None' },
    { id: 'C3', player: 'p3', name: 'Errored', track: 'alts', role: 'Healer', roleSec: 'None', race: 'Undead', class: 'Priest', prof1: 'None', prof2: 'None' },
    { id: 'D4', player: 'p4', name: 'Tir Rheebeard', track: 'flowmasters', role: 'Tank', roleSec: 'Healer', race: 'Tauren', class: 'Druid', prof1: 'None', prof2: 'None' },
    { id: 'E5', player: evilName, name: evilName, track: 'alts', role: 'DPS', roleSec: 'None', race: 'Troll', class: 'Warlock', prof1: 'None', prof2: 'None' },
  ],
};
const fixtureData = {
  schemaVersion: 2, syncedAt: nowStamp, triggeredBy: 'schedule',
  characters: {
    A1: {
      status: 'ok',
      bnet: { name: 'Gear', surname: 'Man', href: 'https://us.api.blizzard.com/profile/wow/character/gear-man?namespace=profile-forever-us' },
      level: 60, guild: 'Flowmasters', activeSpec: 'Fury', itemLevel: 142, achievementPoints: 42,
      gear: [
        { slot: 'Head', name: 'Helm of <b>Trust</b>', ilvl: 100, iconUrl: 'https://render.worldofwarcraft.com/forever-us/icons/56/1.jpg' },
        { slot: 'Hands', name: 'Api Gauntlets', ilvl: 95, iconUrl: 'https://us.api.blizzard.com/static/2.jpg' },
        { slot: 'Chest', name: 'Foreign Plate', ilvl: 99, iconUrl: 'https://evil.tld/x.jpg' },
        { slot: 'Legs', name: 'Protocol Plate', ilvl: 98, iconUrl: '//evil.tld/y.jpg' },
        { slot: 'Feet', name: 'Lookalike Plate', ilvl: 97, iconUrl: 'https://render.worldofwarcraft.com.evil.tld/z.jpg' },
        { slot: 'Ring 1', name: 'Scheme Ring', ilvl: 96, iconUrl: 'javascript:alert(1)' },
        { slot: 'Trinket', name: 'Null Idol', ilvl: 94, iconUrl: null },
      ],
      progress: {
        raid: { raidProgress: [{ name: 'Molten Core', progress: { completedCount: 10, possibleCount: 10 } }] },
        mythicPlus: { currentRatings: [{ season: 12, keystoneLevel: 15 }] },
        pvp: { honorableKills: 34, pvpRank: 5 },
      },
      fetchedAt: nowStamp,
    },
    C3: { status: 'fetch_error', fetchedAt: nowStamp },
    E5: {
      status: 'ok', level: 1, guild: 'Evil <script>guild</script>', achievementPoints: 0,
      gear: [{ slot: 'Head', name: evilName, iconUrl: 'https://render.worldofwarcraft.com/ok.jpg' }],
      progress: { pvp: {} },
      fetchedAt: nowStamp,
    },
  },
};
const fixtureMap = {
  schemaVersion: 2, updatedAt: nowStamp,
  entries: {
    A1: { rosterName: 'gearman', status: 'ok', slug: 'gear-man', href: 'https://us.api.blizzard.com/profile/wow/character/gear-man', bnetName: 'Gear', surname: 'Man', matchedForm: 'lower_hyphen', mappedVia: 'direct', note: null, candidates: [] },
    C3: { rosterName: 'errored', status: 'fetch_error', slug: null, href: null, bnetName: null, surname: null, matchedForm: null, mappedVia: null, note: 'BLIZZARD 503 on <b>Errored</b>', candidates: [] },
    D4: {
      rosterName: 'tir rheebeard', status: 'needs_review', slug: null, href: null, bnetName: null, surname: null,
      matchedForm: null, mappedVia: null, note: 'duplicate normalization with id A1',
      candidates: [
        { slug: 'tir-rheebeard', name: 'Tir', surname: 'Rheebeard', matchedForm: 'lower_hyphen', href: 'https://us.api.blizzard.com/profile/wow/character/tir-rheebeard' },
        { slug: 'tir-rheebeard-2', name: evilName, surname: 'Rheebeard', matchedForm: 'space_hyphen', href: 'https://evil.tld/x' },
      ],
    },
    E5: { rosterName: evilName, status: 'ok', slug: 'evil', href: null, bnetName: '<svg onload=alert(4)>', surname: null, matchedForm: 'lower_hyphen', mappedVia: 'direct', note: null, candidates: [] },
  },
};
fs.writeFileSync(path.join(FIXTURES, 'data', 'characters.json'), JSON.stringify(fixtureRoster, null, 2));
fs.writeFileSync(path.join(FIXTURES, 'data', 'character-data.json'), JSON.stringify(fixtureData, null, 2));
fs.writeFileSync(path.join(FIXTURES, 'data', 'battle-net-map.json'), JSON.stringify(fixtureMap, null, 2));
ok(fs.existsSync(path.join(FIXTURES, 'data', 'character-data.json')) && !path.join(FIXTURES).startsWith(ROOT),
  `XSS fixtures written to ${FIXTURES} (repo data/ untouched)`);

sourceDir = FIXTURES;
await dataMod.loadRoster();
await dataMod.loadSidecars();
state.setFilter('all');
state.setQuery('');
const storeKeysBefore = [...store.keys()];

const gearHtml = (() => { detail.openCharacterDetail('A1'); return byId('detail-content').innerHTML; })();
ok(!hidden('detail-modal'), 'modal opens for a sidecar-present row');
ok(gearHtml.includes('Gear Man') && gearHtml.includes('Flowmasters') && gearHtml.includes('Fury'),
  'header carries bnet name+surname, guild and activeSpec');
ok(gearHtml.includes('>Level<') && /Lv|>60</.test(gearHtml) && gearHtml.includes('142'), 'level and item level render');
ok(gearHtml.includes('Achievement points') && gearHtml.includes('>42<'), 'achievementPoints renders when present');
ok(gearHtml.includes('Synced') && gearHtml.includes(nowStamp.slice(0, 10)), 'per-character fetchedAt timestamp renders');
const srcs = [...gearHtml.matchAll(/<img src="([^"]+)"/g)].map(m => m[1]);
ok(srcs.length === 2, `exactly the 2 allowlisted icon URLs become <img> (got ${srcs.length})`);
ok(srcs.every(s => /^https:\/\/(render\.worldofwarcraft\.com|us\.api\.blizzard\.com)\//.test(s)),
  `every injected icon host is Blizzard-owned (${srcs.join(', ') || 'none'})`);
ok(!/src="(javascript|data):/i.test(gearHtml) && !gearHtml.includes('evil.tld'),
  'no javascript:/data:/foreign-host value reaches a src attribute');
ok((gearHtml.match(/aria-hidden="true"/g) || []).length === 5,
  'the 5 rejected icon URLs degrade to a placeholder, not a request');
ok(gearHtml.includes('alt="Helm of &lt;b&gt;Trust&lt;/b&gt;"'), 'icon alt text is escaped');
ok(gearHtml.includes('Molten Core · Completed') && gearHtml.includes('Mythic+') && gearHtml.includes('Honorable kills'),
  'raid, mythic+ and pvp progress sections render');

detail.openCharacterDetail('B2');
const bareHtml = byId('detail-content').innerHTML;
ok(!hidden('detail-modal') && bareHtml.includes('NoSidecar'), 'a row with no sidecar still opens');
ok(bareHtml.includes('No Battle.net data'), 'and shows the "No Battle.net data" placeholder');
ok(!bareHtml.includes('>Gear<'), 'no gear section when there is no gear');
ok(!bareHtml.includes('>Progress<'), 'no progress section when there is no progress');

detail.openCharacterDetail('C3');
const errorHtml = byId('detail-content').innerHTML;
ok(errorHtml.includes('>sync error<'), 'a fetch_error row shows its lifecycle badge');
ok(errorHtml.includes('&lt;b&gt;Errored&lt;/b&gt;'), 'the map note is escaped, not parsed');
ok(!errorHtml.includes('>Gear<') && !errorHtml.includes('>Candidates<'),
  'absent gear/candidates sections are omitted, not rendered empty');

detail.openCharacterDetail('D4');
const plainD4 = byId('detail-content').innerHTML;
ok(plainD4.includes('>pick candidate<') && !plainD4.includes('>Candidates<'),
  'a needs_resolution row opens without the candidate list expanded');
ok(cardFor('D4').children.some(c => /openCandidateList/.test(c.attrs.onclick || '')),
  'the fixture row that needs a pick renders the badge handler too');
detail.openCandidateList({ stopPropagation() {} }, 'D4');
const candHtml = byId('detail-content').innerHTML;
ok(candHtml.includes('>Candidates<') && candHtml.includes('tir-rheebeard'),
  'the "pick candidate" path lists the map candidates inside the modal');
ok(candHtml.includes('Confirm/resolve coming with validation'), 'the read-only hint is present');
ok(!/onclick="(confirm|resolve|select)/.test(candHtml) && !candHtml.includes('<input') && !candHtml.includes('<select'),
  'the candidate list is read-only: no control, no action handler (3d wires it)');
ok(candHtml.split('>Candidates<').length - 1 === 1, 'the candidates block renders exactly once');
ok(!candHtml.includes('<a href') && !candHtml.includes('evil.tld'),
  'candidate hrefs are neither injected as links nor echoed anywhere in the modal');
ok(candHtml.includes('&lt;img src=x onerror=alert(1)&gt;'), 'candidate names are escaped');
const badgeButton = byId('detail-content').children.find(c => /toggleDetailCandidates/.test(c.attrs.onclick || ''));
ok(!!badgeButton, 'the in-modal lifecycle badge is a toggle button');
fire(badgeButton);
ok(!byId('detail-content').innerHTML.includes('>Candidates<'), 'clicking that badge closes the list');
fire(byId('detail-content').children.find(c => /toggleDetailCandidates/.test(c.attrs.onclick || '')));
ok(byId('detail-content').innerHTML.includes('>Candidates<'), 'and clicking it again reopens it');
detail.closeDetail();

detail.openCharacterDetail('E5');
const evilHtml = byId('detail-content').innerHTML;
ok(!evilHtml.includes('<img src=x') && !evilHtml.includes('<svg onload') && !evilHtml.includes('<script>guild'),
  'no payload from the roster or either sidecar renders as a tag');
ok(evilHtml.includes('&lt;img src=x onerror=alert(1)&gt;') && evilHtml.includes('&lt;svg onload=alert(4)&gt;'),
  'the payloads appear HTML-escaped');
ok(evilHtml.includes('>0<') && evilHtml.includes('Achievement points'), 'achievementPoints: 0 is present, not treated as absent');
ok(!evilHtml.includes('>PvP<'), 'an empty progress object omits its section entirely');
detail.closeDetail();

// The modal is a render hook: a repaint keeps it live rather than stale.
detail.openCharacterDetail('A1');
state.setQuery('nothing-matches-this');
ok(!hidden('detail-modal') && byId('detail-content').innerHTML.includes('Gear Man'),
  'an open modal survives (and repaints with) a query change');
state.setCharacters(state.getCharacters().filter(c => c.id !== 'A1'));
state.renderAll();
ok(hidden('detail-modal') && !detail.isDetailOpen(), 'the modal closes itself when its row disappears');
state.setCharacters(fixtureRoster.characters);
state.setQuery('');
state.renderAll();

// isSafeIconUrl unit boundary.
[['https://render.worldofwarcraft.com/a.jpg', true], ['https://us.api.blizzard.com/x.jpg', true],
  ['https://render.worldofwarcraft.com.evil.tld/a.jpg', false], ['https://evil.tld/', false],
  ['//render.worldofwarcraft.com/a.jpg', false], ['javascript:alert(1)', false],
  ['https://render.worldofwarcraft.com@evil.tld/', false], ['', false], [null, false], [undefined, false],
  [42, false], ['https://render.worldofwarcraft.com.evil.tld', false]].forEach(([url, want]) => {
  ok(detail.isSafeIconUrl(url) === want, `isSafeIconUrl(${JSON.stringify(url)}) -> ${want}`);
});

// ------------------------------------------- 4c. Phase 4b end-to-end (the write, and the card icon)
//
// Two behaviours cannot be proved from a markup builder: the identity tie (F3) is a COMMIT, and the
// roster card's class icon (F4) has to come out of the sidecar the page already loaded rather than
// from a request of its own. Both run here against a throwaway fixture directory plus a scripted
// api.github.com, and the page is then put back the way [5] left it so [6] still answers the question
// it was written for.
console.log('\n[4c] Phase 4b end-to-end: the tie reaches the commit; the card icon costs no request');
const FIXTURES4C = mkdtempSync(path.join(os.tmpdir(), '4b-identity-'));
fs.mkdirSync(path.join(FIXTURES4C, 'data'), { recursive: true });
const roster4c = {
  schemaVersion: 2,
  characters: [
    { id: 'T1', player: 'playerone', name: 'Tie Target', track: 'flowmasters', role: 'DPS', roleSec: 'None', race: 'Orc', class: 'Warrior', prof1: 'Mining', prof2: 'None' },
    { id: 'T2', player: 'playerone', name: 'Iconless', track: 'alts', role: 'DPS', roleSec: 'None', race: 'Troll', class: 'Hunter', prof1: 'None', prof2: 'None' },
    { id: 'T3', player: 'playerone', name: 'Second Path', track: 'alts', role: 'Healer', roleSec: 'None', race: 'Undead', class: 'Priest', prof1: 'None', prof2: 'None' },
  ],
};
const mapEntry4c = (name, status, extra) => ({
  rosterName: name, status, slug: null, href: null, bnetName: null, surname: null,
  matchedForm: null, mappedVia: null, note: null, candidates: [], ...extra,
});
const map4c = {
  schemaVersion: 2, updatedAt: nowStamp,
  entries: {
    T1: mapEntry4c('tie target', 'ok', { slug: 'tie-target', bnetName: 'Tie', matchedForm: 'lower_hyphen', mappedVia: 'direct' }),
    T2: mapEntry4c('iconless', 'ok', { slug: 'iconless', bnetName: 'Iconless', matchedForm: 'lower_hyphen', mappedVia: 'direct' }),
    T3: mapEntry4c('second path', 'ambiguous', {
      candidates: [{ slug: 'second-target', name: 'Second', surname: null, matchedForm: 'lower_hyphen', href: null }],
    }),
  },
};
const data4c = {
  schemaVersion: 2, syncedAt: nowStamp, triggeredBy: 'schedule',
  characters: {
    T1: { status: 'ok', level: 60, fetchedAt: nowStamp,
      race: { id: 9, name: 'Tauren' }, class: { id: 11, name: 'Druid' },
      professions: { primaries: [{ name: 'H erbalism' }, { name: 'Skinning' }], secondaries: [] } },
    // A race icon URL the code is never supposed to ask for: real-looking media, on a field the API
    // does not serve (§10.3), so any accidental render of it is visible to the assertions below.
    // The two spec groups put the ACTIVE specialization second, which is what makes the sub-tab
    // derive path observable end to end (the committed rows all have it first).
    T2: { status: 'ok', level: 60, fetchedAt: nowStamp, classIconUrl: CLASS_ICON, raceIconUrl: RACE_ICON,
      activeSpec: 'Survival',
      talents: [
        { specId: 253, specName: 'Beast Mastery', spentPoints: 3, talents: [{ rank: 1, spellName: 'Bestial Wrath' }] },
        { specId: 255, specName: 'Survival', spentPoints: 5, talents: [{ rank: 2, spellName: 'Rapid Fire' }] },
      ] },
    T3: { status: 'ok', level: 60, fetchedAt: nowStamp,
      race: { id: 98, name: 'Goblin' }, class: { id: 99, name: 'Monk' } },
  },
};
fs.writeFileSync(path.join(FIXTURES4C, 'data', 'characters.json'), JSON.stringify(roster4c, null, 2));
fs.writeFileSync(path.join(FIXTURES4C, 'data', 'battle-net-map.json'), JSON.stringify(map4c, null, 2));
fs.writeFileSync(path.join(FIXTURES4C, 'data', 'character-data.json'), JSON.stringify(data4c, null, 2));

const github = await import(moduleUrl('github.js'));
const GH_ORIGIN = 'https://api.github.com';
const originReads = [];
const ghRequests = [];
let putBody = null;
const fetchBefore4c = globalThis.fetch;
globalThis.fetch = async (url, opts = {}) => {
  const raw = String(url);
  if (!raw.startsWith(GH_ORIGIN)) {
    originReads.push(raw.replace(/^.*\//, ''));
    return fetchBefore4c(url, opts);
  }
  const ghPath = raw.slice(GH_ORIGIN.length);
  const method = String(opts.method || 'GET').toUpperCase();
  ghRequests.push({ method, path: ghPath });
  if (method === 'GET' && ghPath === '/user') {
    return { ok: true, status: 200, json: async () => ({ login: 'reviewer-4b' }) };
  }
  if (method === 'GET' && ghPath.startsWith('/repos/acme/wow/contents/data/characters.json')) {
    return { ok: true, status: 200, json: async () => ({
      sha: 'sha-4b', content: Buffer.from(JSON.stringify(roster4c)).toString('base64'),
    }) };
  }
  if (method === 'PUT') {
    putBody = typeof opts.body === 'string' ? JSON.parse(opts.body) : null;
    return { ok: true, status: 200, json: async () => ({ content: { sha: 'sha-after-4b' }, commit: { sha: 'commit-4b' } }) };
  }
  return { ok: false, status: 404, json: async () => null };
};
const ghBackup = { ...github.ghConfig };
github.ghConfig.owner = 'acme';
github.ghConfig.repo = 'wow';
github.ghConfig.branch = 'main';
github.ghConfig.token = 'ghp_test_never_sent_elsewhere';

sourceDir = FIXTURES4C;
await dataMod.loadRoster();
await dataMod.loadSidecars();
state.setFilter('all');
state.setQuery('');
const committedDoc4c = () => (putBody && typeof putBody.content === 'string'
  ? JSON.parse(Buffer.from(putBody.content, 'base64').toString('utf8')) : null);
const committedRow4c = () => {
  const doc = committedDoc4c();
  return doc && Array.isArray(doc.characters) ? doc.characters.find(c => c && c.id === 'T1') : null;
};

// ---- F4 on the card: the icon comes off the sidecar the page already had ----
const iconCard = cardFor('T2');
const bareCard = cardFor('T1');
ok(!!iconCard && iconCard.innerHTML.includes(`src="${esc(CLASS_ICON)}"`),
  'a roster card paints the class icon next to the class badge from the sidecar row');
ok(!iconCard.innerHTML.includes('race/6.jpg') && countOf('class="wow-card-icon"', iconCard.innerHTML) === 1
  && countOf('<img src=', iconCard.innerHTML) === 1,
  'a raceIconUrl on the row is nowhere on the card: the card draws the class icon and no second box');
ok(countOf('class="wow-card-icon"></span>', bareCard.innerHTML) === 1 && !bareCard.innerHTML.includes('<img src='),
  'a card whose sidecar row carries no classIconUrl still reserves the slot, so class names line down the grid');
ok(originReads.filter(u => u.indexOf('character-data.json') !== -1).length === 1,
  `the whole grid painted from ONE read of data/character-data.json (${originReads.join(', ')}) — the card icon reused the join, it added no fetch`);

// ---- F1 in a live modal: the derived default is the ACTIVE group, not whichever group is first ----
detail.openCharacterDetail('T2');
detail.setDetailTab('talents');
const t2Talents = panelSlice(contentHtml(), 'talents');
const t2Subs = subTabButtons(t2Talents);
ok(t2Subs.length === 2
  && t2Subs.filter(t => t.selected === 'true').map(t => t.subtab).join() === '1'
  && t2Subs.filter(t => t.specActive === 'true').map(t => t.subtab).join() === '1'
  && t2Subs.filter(t => t.tabindex === '0').map(t => t.subtab).join() === '1',
  `a modal opened on a character whose active spec is the SECOND group opens on that tab (${t2Subs.map(t => `${t.subtab}:${t.selected}/${t.specActive}`).join(', ')})`);
ok(subTabPanels(t2Talents).filter(p => !p.classes.includes('hidden')).map(p => p.id).join() === 'detail-subpanel-1'
  && subPanelSlice(t2Talents, 1).includes('Rapid Fire') && subPanelSlice(t2Talents, 0).includes('Bestial Wrath'),
  'the visible sub-panel is the active group\'s, and the inactive group is hidden rather than unbuilt');
detail.closeDetail();

// ---- F2 in the live modal, then F3 through the real commit ----
detail.openCharacterDetail('T1');
const t1Html = byId('detail-content').innerHTML;
ok(t1Html.includes('Race / class mismatch') && t1Html.includes('>Tauren</span>') && t1Html.includes('>Druid</span>'),
  'the open modal shows the mismatch flag for the row before any action is taken');
ok(/onclick="confirmMatch\('T1'\)"/.test(decodeEntities(t1Html)), 'and CONFIRM MATCH is still offered: the flag blocks nothing');
const confirmed4c = await globalThis.confirmMatch('T1');
await new Promise(resolve => setTimeout(resolve, 0));
const committed = committedRow4c();
ok(confirmed4c === true && ghRequests.filter(r => r.method === 'PUT').length === 1 && !!committed,
  'confirm commits exactly once through the Contents API');
ok(committed.race === 'Tauren' && committed.class === 'Druid',
  'the committed row carries the Battle.net race and class (a legal Tauren Druid)');
ok(committed.prof1 === 'Skinning' && committed.prof2 === 'None',
  'the one in-vocabulary primary became prof1; the out-of-vocabulary one left prof2 as the reviewer had it');
ok(committed.name === 'Tie Target' && committed.track === 'flowmasters' && committed.role === 'DPS'
  && committed.player === 'playerone', 'the rest of the row rides along untouched');
ok(committed.validation.state === 'validated' && committed.validation.confirmedSlug === 'tie-target'
  && committed.validation.reviewedBy === 'reviewer-4b'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(committed.validation.reviewedAt)
  && Object.keys(committed.validation).sort().join() === 'confirmedSlug,reviewedAt,reviewedBy,state',
  'the validation block itself is byte-compatible with before the tie existed (same four keys)');
const toast4c = byId('app-toast').textContent;
ok(toast4c.startsWith('Confirmed Tie Target as tie-target.')
  && toast4c.includes('Roster tied to Battle.net: race Tauren, class Druid, prof1 Skinning.')
  && toast4c.includes('Skipped (outside the roster vocabulary): profession H erbalism.'),
  `one toast says the confirm, what was tied and what was skipped ("${toast4c}")`);
ok(byId('app-toast').classList.contains('bg-emerald-950/95'), 'and it is still the success tone');

// The second path that records a validation (CONFIRM SELECTED), with a sidecar the vocabulary
// refuses outright: nothing is written, and the toast says so rather than failing silently.
putBody = null;
detail.openCharacterDetail('T3');
detail.selectCandidate('T3', 0);
const resolved4c = await globalThis.confirmSelectedCandidate('T3');
await new Promise(resolve => setTimeout(resolve, 0));
const doc3 = committedDoc4c();
const committed3 = doc3 && Array.isArray(doc3.characters) ? doc3.characters.find(c => c.id === 'T3') : null;
ok(resolved4c === true && committed3 && committed3.validation.confirmedSlug === 'second-target',
  'CONFIRM SELECTED still records the validation block');
ok(committed3.race === 'Undead' && committed3.class === 'Priest'
  && committed3.prof1 === 'None' && committed3.prof2 === 'None',
  'a Goblin Monk sidecar writes no identity at all: the roster row keeps exactly what it had');
const toast3 = byId('app-toast').textContent;
ok(toast3.startsWith('Mapped Second Path to second-target.')
  && toast3.includes('Skipped (outside the roster vocabulary): race Goblin; class Monk.'),
  `and the skip is surfaced on the same success toast ("${toast3}")`);

// ---- put the page back for [6] ----
detail.closeDetail();
globalThis.fetch = fetchBefore4c;
github.ghConfig.owner = ghBackup.owner;
github.ghConfig.repo = ghBackup.repo;
github.ghConfig.branch = ghBackup.branch;
github.ghConfig.token = ghBackup.token;
ok(github.ghHasPat() === false, 'the fake PAT is off the page again');
sourceDir = FIXTURES;
await dataMod.loadRoster();
await dataMod.loadSidecars();
state.setFilter('all');
state.setQuery('');
ok(state.getCharacters().length === fixtureRoster.characters.length
  && hidden('detail-modal') === true && detail.isDetailOpen() === false,
  `the [5] fixtures are reloaded and the modal is closed again (${state.getCharacters().length} rows)`);

// ---------------------------------------------------------------- 6. storage
console.log('\n[6] persistence boundary');
const strayKeys = [...new Set([...storeKeysBefore, ...storageWrites])].filter(k => k !== 'wow_forever_roster' && k !== 'wow_gh_config');
ok(strayKeys.length === 0, `no new localStorage keys (all writes: ${[...new Set(storageWrites)].join(', ') || 'none'})`);
const persistedBlob = [...store.values()].join('\n');
ok(!persistedBlob.includes('gear-man') && !persistedBlob.includes('Molten Core')
  && !persistedBlob.includes('render.worldofwarcraft.com') && !persistedBlob.includes('Flowmasters'),
  'no gear / sidecar content was persisted');

console.log(`\n${fails === 0 ? 'ALL CHECKS PASSED' : 'FAILURES PRESENT'}: ${passes} passed, ${fails} failed`);
process.exit(fails === 0 ? 0 : 1);
