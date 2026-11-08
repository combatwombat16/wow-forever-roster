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
const editBtn = cards()[0].children.find(c => c.tagName === 'BUTTON' && /editCharacter/.test(c.attrs.onclick || ''));
const editModalWasHidden = hidden('char-modal');
fire(editBtn);
ok(editModalWasHidden && !hidden('char-modal'), '✎ opens the Add/Edit modal');
ok(hidden('detail-modal'), '✎ does not also open the detail modal');
globalThis.closeModal();
ok(hidden('char-modal'), 'closeModal() restores it');
const deleteBtn = cards()[0].children.find(c => c.tagName === 'BUTTON' && /deleteCharacter/.test(c.attrs.onclick || ''));
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
