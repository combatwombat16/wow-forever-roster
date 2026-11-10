import fs from 'fs';
import path from 'path';

const ROOT = process.argv[2] || '.';
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
// Phase 2 migration: the roster lives at data/characters.json and is the v2
// envelope {schemaVersion, characters:[...]}; the v1 bare array is still accepted
// during the transition window (design §8). Path + shape only — no assertion here
// changed. Since Phase 3a the UI itself is js/*.js modules (see [1]).
const rosterDoc = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'characters.json'), 'utf8'));
const repoChars = Array.isArray(rosterDoc) ? rosterDoc : rosterDoc.characters;

let fails = 0, passes = 0;
const ok = (cond, msg) => { if (cond) { passes++; console.log(`  PASS ${msg}`); } else { fails++; console.log(`  FAIL ${msg}`); } };

// ---------- 1. static structure ----------
console.log('\n[1] module extraction + static structure');
// Phase 3a migration: the app moved out of index.html's inline <script> into js/*.js
// ES modules, with <script type="module" src="js/main.js"> as the only entry. Path
// following only: the same assertions now run over the concatenation of the module
// sources, in dependency order, with import/export syntax stripped so the behavioural
// harness below can still evaluate them as one script body.
// guildfilter.js belongs to this list for the same reason every other module does: it publishes
// inline handlers (setGuildFilterIndex, applyGuildScope, clearGuildScopeOverride) that only exist
// as strings in index.html, and a handler written in a module nobody scans is a handler nobody
// checks is global.
const MODULES = ['util.js', 'state.js', 'data.js', 'github.js', 'roster.js', 'party.js',
  'professions.js', 'stats.js', 'editchar.js', 'detail.js', 'search.js', 'guildfilter.js',
  'main.js'];
const moduleSrc = {};
MODULES.forEach(f => {
  const file = path.join(ROOT, 'js', f);
  moduleSrc[f] = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
});
ok(MODULES.every(f => moduleSrc[f].length > 0),
  `all ${MODULES.length} js modules present (missing: ${MODULES.filter(f => !moduleSrc[f]).join(',') || 'none'})`);
ok(/<script type="module" src="js\/main\.js"><\/script>/.test(html), 'module entry script located');
ok(!/<script>[\s\S]*?<\/script>/.test(html), 'no inline app <script> logic remains in index.html');
const stripModuleSyntax = (s) => s
  .replace(/^\s*import\s[^;]*;\s*$/gm, '')
  .replace(/^export\s+(?=(let|const|var|function|class|async)\b)/gm, '');
const src = MODULES.map(f => stripModuleSyntax(moduleSrc[f])).join('\n');
fs.writeFileSync('/tmp/app_extract.js', src);

const markup = html.replace(/<script[\s\S]*?<\/script>/g, '');
const declaredIds = [...markup.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
// The class list each declared element ships with, so the stub knows #detail-modal starts
// hidden the way the browser does.
const declaredClasses = new Map(
  [...markup.matchAll(/<(\w+)([^>]*\bid="([^"]+)"[^>]*)>/gi)]
    .map(m => [m[3], (/\bclass="([^"]*)"/i.exec(m[2]) || [, ''])[1]])
);
const dupIds = declaredIds.filter((id, i) => declaredIds.indexOf(id) !== i);
ok(dupIds.length === 0, `no duplicate ids (found: ${dupIds.join(',') || 'none'})`);

const usedIds = [...src.matchAll(/getElementById\('([^']+)'\)/g)].map(m => m[1]);
// renderDistribution receives its ids as arguments, so they must be collected too.
const paramIds = [...src.matchAll(/renderDistribution\('([^']+)',\s*'([^']+)'/g)].flatMap(m => [m[1], m[2]]);
usedIds.push(...paramIds);
const missingIds = [...new Set(usedIds)].filter(id => !declaredIds.includes(id));
ok(missingIds.length === 0, `every getElementById target exists (missing: ${missingIds.join(',') || 'none'})`);

const handlerNames = [...new Set([...html.matchAll(/\bon(?:click|submit|change|input)="([a-zA-Z_$][\w$]*)\(/g)].map(m => m[1]))];
const declaredFns = [...new Set([...src.matchAll(/function\s+([a-zA-Z_$][\w$]*)\s*\(/g)].map(m => m[1]))];
const missingFns = handlerNames.filter(n => !declaredFns.includes(n));
ok(missingFns.length === 0, `every inline handler resolves (${handlerNames.length} handlers, missing: ${missingFns.join(',') || 'none'})`);
// Phase 3a: an inline handler only resolves if a module published it on the global
// scope, so the invariant is now enforced where it lives — in the window.* assignments.
const windowExposures = [...new Set([...src.matchAll(/window\.([a-zA-Z_$][\w$]*)\s*=/g)].map(m => m[1]))];
const unexposedFns = handlerNames.filter(n => !windowExposures.includes(n));
ok(unexposedFns.length === 0, `every inline handler is assigned to window by a module (missing: ${unexposedFns.join(',') || 'none'})`);

const orphanIds = ['race-stats', 'class-stats', 'race-summary', 'class-summary'].filter(id => !usedIds.includes(id));
ok(paramIds.length === 4, `both distribution panels are wired up (${paramIds.join(', ')})`);
ok(orphanIds.length === 0, `new panels are read by the script (orphans: ${orphanIds.join(',') || 'none'})`);

// ---------- 2. CLASS_BAR / CLASS_COLORS hex agreement ----------
console.log('\n[2] colour maps agree');
const hex = (block) => [...block.matchAll(/"(\w+)":\s*"[^"]*?(#[0-9A-Fa-f]{6})/g)].map(m => [m[1], m[2].toUpperCase()]);
const colorsBlock = src.match(/const CLASS_COLORS = \{([\s\S]*?)\};/)[1];
const barBlock = src.match(/const CLASS_BAR = \{([\s\S]*?)\};/)[1];
const colors = Object.fromEntries(hex(colorsBlock));
const bars = Object.fromEntries(hex(barBlock));
const classes = Object.keys(colors);
ok(Object.keys(bars).length === classes.length, `CLASS_BAR covers all ${classes.length} classes`);
const drift = classes.filter(c => bars[c] !== colors[c]);
ok(drift.length === 0, `no hex drift between CLASS_COLORS and CLASS_BAR (${drift.join(',') || 'clean'})`);

// ---------- 3. behavioural harness ----------
console.log('\n[3] rendered output vs independent tally of characters.json');

// A token-set classList (Phase 3c toggles `hidden` on the search clear button and the detail
// modal, so add/remove has to be observable rather than a no-op) plus the DOM plumbing the
// card-click wiring touches: addEventListener, setAttribute, querySelectorAll.
function makeEl(id, classes = '') {
  let html = '';
  const el = {
    id, textContent: '', innerText: '', value: '', title: '', attrs: {},
    listeners: {}, style: {}, children: [], _classes: new Set(String(classes).split(/\s+/).filter(Boolean)),
    classList: {
      add: (...names) => names.forEach(n => el._classes.add(n)),
      remove: (...names) => names.forEach(n => el._classes.delete(n)),
      contains: n => el._classes.has(n),
      toggle: (n, force) => {
        const on = force === undefined ? !el._classes.has(n) : Boolean(force);
        if (on) el._classes.add(n); else el._classes.delete(n);
        return on;
      },
    },
    addEventListener(type, fn) { (el.listeners[type] = el.listeners[type] || []).push(fn); },
    setAttribute(key, value) { el.attrs[key] = String(value); },
    getAttribute(key) { return el.attrs[key] === undefined ? null : el.attrs[key]; },
    querySelectorAll() { return []; },
    appendChild(child) { this.children.push(child); html += child.innerHTML || ''; },
  };
  // Assigning innerHTML replaces the children (renderRoster clears the grid that way), while
  // appendChild adds to them — so the two stay distinguishable and card counts are meaningful.
  Object.defineProperty(el, 'innerHTML', {
    get: () => html,
    set: value => { html = String(value); el.children = []; },
  });
  Object.defineProperty(el, 'className', {
    get: () => [...el._classes].join(' '),
    set: value => { el._classes = new Set(String(value).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

function runApp(seededChars) {
  const registry = new Map();
  declaredIds.forEach(id => registry.set(id, makeEl(id, declaredClasses.get(id) || '')));
  const missing = [];
  const documentStub = {
    getElementById(id) {
      if (!registry.has(id)) { missing.push(id); registry.set(id, makeEl(id)); }
      return registry.get(id);
    },
    querySelectorAll() { return []; },
    createElement(tag) { return makeEl(`<${tag}>`); },
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  };
  const storageStub = {
    getItem(key) { return key === 'wow_forever_roster' ? JSON.stringify(seededChars) : null; },
    setItem() {},
  };
  const windowStub = {};
  const factory = new Function(
    'document', 'localStorage', 'setInterval', 'alert', 'confirm', 'prompt', 'fetch', 'console', 'window',
    `${src}\nreturn { setFilter, renderRoster, deriveLifecycleState, setQuery, getQuery, getFilteredCharacters,` +
    ` openCharacterDetail, closeDetail, isSafeIconUrl, applyQuery, clearSearch,` +
    ` get characters() { return characters; } };`
  );
  const api = factory(documentStub, storageStub, () => {}, () => {}, () => false, () => null,
    () => Promise.reject(new Error('network disabled in harness')), { error() {}, log() {} }, windowStub);
  return { api, registry, missing, documentStub };
}

const expected = (chars) => {
  const tally = (key) => {
    const m = new Map();
    chars.forEach(c => m.set(c[key], (m.get(c[key]) || 0) + 1));
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  };
  return { race: tally('race'), class: tally('class') };
};

const { api, registry, missing } = runApp(repoChars);
ok(missing.length === 0, `no id resolved at runtime that is absent from markup (${missing.join(',') || 'none'})`);
ok(api.characters.length === repoChars.length, `app loaded all ${repoChars.length} seeded characters`);

const ALL_RACES = [...src.matchAll(/^\s{6}"([^"]+)": \[/gm)].map(m => m[1]);
const ALL_CLASSES = Object.keys(colors);
console.log(`  info  canonical sets: ${ALL_RACES.length} races, ${ALL_CLASSES.length} classes`);

const TRACKS = ['all', 'flowmasters', 'family', 'alts'];
for (const track of TRACKS) {
  api.setFilter(track);
  const pool = track === 'all' ? repoChars : repoChars.filter(c => c.track === track);
  const exp = expected(pool);
  checkPanel('race', 'race-stats', 'race-summary', 'Races', exp.race, track);
  checkPanel('class', 'class-stats', 'class-summary', 'Classes', exp.class, track);
}

function checkPanel(kind, containerId, summaryId, unit, rows, track) {
  const htmlOut = registry.get(containerId).innerHTML;
  const summary = registry.get(summaryId).innerText;
  const total = rows.reduce((n, [, c]) => n + c, 0);
  const names = rows.map(([n]) => n);
  ok(summary === `${rows.length} ${unit} / ${total} Chars`, `[${track}] ${kind} summary "${summary}"`);
  const renderedAll = names.every(n => htmlOut.includes(`>${n}<`) &&
    htmlOut.includes(`${rows.find(r => r[0] === n)[1]} (${Math.round(rows.find(r => r[0] === n)[1] / total * 100)}%)`));
  ok(renderedAll, `[${track}] ${kind} renders ${rows.length} rows with count + %`);
  const strays = (kind === 'race' ? ALL_RACES : ALL_CLASSES).filter(n => !names.includes(n) && htmlOut.includes(`>${n}<`));
  ok(strays.length === 0, `[${track}] ${kind} shows no zero-count rows (strays: ${strays.join(',') || 'none'})`);
  const pctSum = rows.reduce((n, [, c]) => n + Math.round(c / total * 100), 0);
  ok(Math.abs(pctSum - 100) <= rows.length, `[${track}] ${kind} rounded percentages sum to ${pctSum} (~100)`);
  const widths = [...htmlOut.matchAll(/width:([\d.]+)%/g)].map(m => parseFloat(m[1]));
  ok(widths.length === rows.length && Math.abs(widths.reduce((a, b) => a + b, 0) - 100) < 1.5,
    `[${track}] ${kind} bar widths sum to ${widths.reduce((a, b) => a + b, 0).toFixed(2)}%`);
  const order = [...htmlOut.matchAll(/font-semibold truncate [^"]*">([^<]+)</g)].map(m => m[1]);
  const sortedOk = order.every((n, i) => i === 0 || rows.findIndex(r => r[0] === order[i - 1]) <= i - 1 && rows.findIndex(r => r[0] === n) === i);
  ok(sortedOk, `[${track}] ${kind} rows ordered by count desc, then name`);
}

// ---------- 4. empty filter ----------
console.log('\n[4] empty-filter placeholder');
const only_family = repoChars.map(c => ({ ...c, track: 'family' }));
const { api: api2, registry: reg2 } = runApp(only_family);
api2.setFilter('alts');
ok(reg2.get('race-stats').innerHTML.includes('No characters in this filter.'), 'race panel shows placeholder when filter is empty');
ok(reg2.get('class-stats').innerHTML.includes('No characters in this filter.'), 'class panel shows placeholder when filter is empty');
ok(reg2.get('race-summary').innerText === '0 Races / 0 Chars', 'race summary zeroes out');
ok(reg2.get('class-summary').innerText === '0 Classes / 0 Chars', 'class summary zeroes out');

// ---------- 5. escaping ----------
console.log('\n[5] escaping of roster-sourced text');
const evil = '<img src=x onerror=alert(1)>';
const { api: api3, registry: reg3 } = runApp([{
  id: '1', player: evil, name: evil, track: 'family', role: 'DPS', roleSec: 'None',
  race: 'Troll', class: evil, prof1: evil, prof2: 'None',
}]);
api3.setFilter('family');
const all = [...reg3.values()].map(el => el.innerHTML + el.innerText).join('\n');
ok(!all.includes('<img src=x'), 'no raw injected tag rendered anywhere');
ok(all.includes('&lt;img src=x'), 'payload appears HTML-escaped');
ok(reg3.get('race-stats').innerHTML.includes('>Troll<'), 'distribution still renders valid rows alongside');
api3.renderRoster();
ok(![...reg3.values()].map(el => el.innerHTML).join('').includes('<img src=x'), 'escaping holds on re-render');

// ---------- 6. Phase 3b: Battle.net sidecars + derived lifecycle ----------
console.log('\n[6] Battle.net sidecar wiring (Phase 3b)');
const dataSrc = moduleSrc['data.js'];
ok(/CHARACTER_DATA_PATH = 'data\/character-data\.json'/.test(dataSrc), 'data.js declares data/character-data.json');
ok(/BATTLE_NET_MAP_PATH = 'data\/battle-net-map\.json'/.test(dataSrc), 'data.js declares data/battle-net-map.json');
ok(/function byRosterId\(id\)[\s\S]{0,240}?sidecar:[\s\S]{0,160}?mapEntry:/.test(dataSrc), 'byRosterId joins the sidecar row and the map entry');
ok(/SIDECAR_FRESH_HOURS = 36/.test(dataSrc), 'the 36h freshness threshold is stated exactly once');

// The sidecars are display-only: nothing may persist them, so every localStorage write
// in the app must be keyed by the roster key or the PAT settings key.
// Three keys and no more: the roster mirror, the PAT settings, and the guild scope — which is a
// per-browser choice about what to LOOK at, never a fact about a character, and is the reason a
// reviewer can try a guild without one keystroke becoming a commit to data/guild-config.json.
const persistedKeys = [...new Set([...src.matchAll(/localStorage\.setItem\(\s*([^,)]+)/g)].map(m => m[1].trim()))];
const strayWrites = persistedKeys.filter(k => k !== 'STORAGE_KEY' && k !== "'wow_gh_config'"
  && k !== 'GUILD_SCOPE_STORAGE_KEY');
ok(strayWrites.length === 0, `no sidecar data is persisted (keys written: ${persistedKeys.join(', ')})`);

// Header provenance line: declared in markup, rendered by data.js, degraded when the
// sidecar is absent.
ok(declaredIds.includes('bnet-sync-line'), 'the header carries a bnet-sync-line element');
ok(/getElementById\('bnet-sync-line'\)/.test(dataSrc), 'data.js renders the header provenance line');
ok(/function renderSyncLine[\s\S]{0,700}?escapeHtml\(stamp\)[\s\S]{0,300}?escapeHtml\(trigger\)/.test(dataSrc), 'both dynamic halves of the sync line are escaped');

const lifecycleCases = [
  [null, null, 'awaiting_sync'],
  [{}, null, 'awaiting_sync'],
  [{}, { status: 'ok' }, 'awaiting_confirmation'],
  [{}, { status: 'ambiguous' }, 'needs_resolution'],
  [{}, { status: 'needs_review' }, 'needs_resolution'],
  [{}, { status: 'not_found' }, 'not_found'],
  [{}, { status: 'fetch_error' }, 'fetch_error'],
  [{}, { status: 'who-knows' }, 'awaiting_sync'],
  [{ validation: {} }, { status: 'ok' }, 'awaiting_confirmation'],
  [{ validation: { state: 'validated' } }, null, 'validated'],
  [{ validation: { state: 'validated' } }, { status: 'fetch_error' }, 'validated'],
];
const { api: api4, registry: reg4 } = runApp(repoChars);
lifecycleCases.forEach(([lcChar, lcMap, want]) => {
  ok(api4.deriveLifecycleState(lcChar, lcMap) === want,
    `deriveLifecycleState(${JSON.stringify(lcChar)}, ${JSON.stringify(lcMap)}) -> ${want}`);
});

// Row-level consequences: the lifecycle badge is always rendered, and a validated row
// loses its edit affordance (the Review modal that re-opens it is Phase 3c).
const baseRow = { player: 'p', name: 'Row', track: 'family', role: 'DPS', roleSec: 'None', race: 'Troll', class: 'Mage', prof1: 'None', prof2: 'None' };
const validatedRow = { ...baseRow, id: '901', name: 'Locked', validation: { state: 'validated', reviewedBy: 'pandamancer' } };
const draftRow = { ...baseRow, id: '902', name: 'Draft' };
const { api: api5, registry: reg5 } = runApp([validatedRow, draftRow]);
api5.setFilter('family');
const rows = reg5.get('character-grid').innerHTML;
const lockedRowHtml = rows.slice(rows.indexOf('Locked'), rows.indexOf('Draft'));
const draftRowHtml = rows.slice(rows.indexOf('Draft'));
ok(lockedRowHtml.includes('>validated<'), 'validated row renders its lifecycle badge');
ok(lockedRowHtml.includes('Validated — use Review to re-open') && lockedRowHtml.includes('disabled'), 'validated row renders a disabled edit button');
ok(!lockedRowHtml.includes("editCharacter('901')"), 'validated row carries no edit handler');
ok(lockedRowHtml.includes('Validated by pandamancer'), 'validated badge title names the reviewer');
ok(draftRowHtml.includes("editCharacter('902')") && draftRowHtml.includes('>unvalidated<'), 'draft row keeps its edit handler and badge');
ok(!draftRowHtml.includes('Validated — use Review to re-open'), 'draft row is not locked');
ok(reg4.get('bnet-sync-line').innerHTML === 'No Battle.net data', 'sync line degrades to "No Battle.net data" with no sidecar');

// ---------- 7. Phase 3c: search box + character detail modal ----------
console.log('\n[7] search box + detail modal (Phase 3c)');
const stateSrc = moduleSrc['state.js'];
const rosterSrc = moduleSrc['roster.js'];
const searchSrc = moduleSrc['search.js'];
const detailSrc = moduleSrc['detail.js'];

// markup: declared, hidden by default, in the visual language, and with no new CDN script
ok(declaredIds.includes('search-input') && /id="search-input"[^>]*oninput="onSearchInput\(event\)"/.test(markup),
  '#search-input is declared and wired to onSearchInput(event)');
ok(declaredIds.includes('clear-search') && markup.includes('onclick="clearSearch()"'),
  'the clear (✕) button is declared and wired to clearSearch()');
ok(markup.indexOf('Squad Filter') < markup.indexOf('id="search-input"')
  && markup.indexOf('id="search-input"') < markup.indexOf('id="character-grid"'),
  'the search box sits in the Squad Filter panel above the roster');
ok(declaredIds.includes('detail-modal') && declaredIds.includes('detail-content'),
  '#detail-modal + #detail-content are declared');
ok((declaredClasses.get('detail-modal') || '').includes('hidden'), 'the detail modal starts hidden');
ok(/id="detail-modal"[\s\S]{0,600}?class="wow-border-gold[^"]*"[\s\S]{0,400}?id="detail-content"/.test(markup),
  'the detail modal reuses the wow-border-gold panel shell');
ok(!/style="[^"]*(color|background)/i.test(markup), 'no inline-style colours anywhere in the markup');
// Phase 4 replaced the Tailwind Play CDN with the Pages-built stylesheet, so the old
// "no CDN script beyond Tailwind" allowance became a hard zero: an empty list used to
// satisfy it trivially, which would have let any CDN script in unnoticed. The order is
// load-bearing too — css/tailwind.css must precede css/app.css or app.css's
// hand-written overrides lose the cascade.
const remoteScripts = [...html.matchAll(/<script[^>]*\bsrc="(https?:[^"]+)"/g)].map(m => m[1]);
ok(remoteScripts.length === 0,
  `no remote <script> remains, the Tailwind CDN included (${remoteScripts.join(', ') || 'none'})`);
ok(!/cdn\.tailwindcss\.com/.test(html), 'index.html no longer references cdn.tailwindcss.com');
const twLink = html.indexOf('<link rel="stylesheet" href="css/tailwind.css">');
const appLink = html.indexOf('<link rel="stylesheet" href="css/app.css">');
ok(twLink !== -1 && appLink !== -1 && twLink < appLink,
  `css/tailwind.css is linked before css/app.css (tw=${twLink} app=${appLink})`);

// one filter path: search AND track, in state.js, nowhere else
ok(/export let currentQuery/.test(stateSrc) && /function setQuery\(query\)/.test(stateSrc),
  'state.js owns the query (currentQuery + setQuery)');
ok(/function getFilteredCharacters\(\)[\s\S]{0,220}currentFilter[\s\S]{0,220}matchesQuery/.test(stateSrc),
  'getFilteredCharacters applies track AND query');
// The panels must consume the filtered view; editchar.js's characters.filter() is a roster
// mutation (delete), not a render path, so the invariant is scoped to the render-path modules.
const renderPathModules = ['roster.js', 'party.js', 'professions.js', 'stats.js', 'detail.js'];
const filterForks = renderPathModules.filter(f => /characters\.filter\(/.test(moduleSrc[f]));
ok(filterForks.length === 0, `no render path re-implements the filter (forks: ${filterForks.join(',') || 'none'})`);
ok(!/currentFilter/.test(rosterSrc) && /getFilteredCharacters\(\)/.test(rosterSrc),
  'roster.js paints the filtered view instead of reading the raw filter');
ok(/registerSearchProvider\(fn\)|registerSearchProvider/.test(stateSrc) && /registerSearchProvider\(/.test(moduleSrc['data.js']),
  'guild + Battle.net lookup names join the search through a state.js provider hook (no cycle)');

// debounce + the search box does not fork the render path
ok((searchSrc.match(/SEARCH_DEBOUNCE_MS = 120/g) || []).length === 1, 'the ~120 ms debounce is stated exactly once');
ok(/setTimeout\([\s\S]{0,140}?SEARCH_DEBOUNCE_MS\)/.test(searchSrc), 'keystrokes are debounced with that constant');
ok(!/renderAll\(/.test(searchSrc) && /setQuery\(/.test(searchSrc),
  'search.js repaints only through state.setQuery → renderAll');

// detail modal trust boundaries
ok(/ICON_HOSTS = \[[^\]]*render\.worldofwarcraft\.com[^\]]*us\.api\.blizzard\.com[^\]]*\]/.test(detailSrc),
  'the icon host allowlist names both Blizzard hosts');
ok(/new URL\(url\)/.test(detailSrc) && /protocol === 'https:'/.test(detailSrc) && /parsed\.hostname/.test(detailSrc),
  'the icon check is on a parsed https hostname, not a substring');
ok(/loading="lazy"/.test(detailSrc) && /alt="\$\{escapeHtml\(name\)\}"/.test(detailSrc),
  'gear icons are lazy-loaded with escaped alt text');
ok(!/localStorage\s*\.\s*(setItem|getItem|removeItem)/.test(detailSrc),
  'the detail module performs no localStorage access at all');
// the click wiring on both sides of the roster/detail boundary
ok(/getLifecycleBadge\(\s*c,\s*lifecycle,\s*badgeClickId,\s*\n?\s*Array\.isArray\(mapEntry && mapEntry\.candidates\)/.test(rosterSrc)
  && /lifecycle === LIFECYCLE\.NEEDS_RESOLUTION \? c\.id : null/.test(rosterSrc),
  'the roster passes a candidate-click id for exactly the rows that need a pick, and the hasCandidates flag comes from the map entry');
ok(/window\.openCharacterDetail = openCharacterDetail/.test(detailSrc)
  && /window\.openCharacterDetail\(c\.id\)/.test(rosterSrc)
  && !/from '\.\/detail\.js'/.test(rosterSrc),
  'roster.js opens the modal through the window-published handler, and does not import detail.js');
ok(/window\.openCandidateList = openCandidateList/.test(detailSrc), 'the badge handler is published on window');
ok(/card\.addEventListener\('click'/.test(rosterSrc)
  && /querySelectorAll\('button'\)[\s\S]{0,120}stopPropagation\(\)/.test(rosterSrc),
  'the card click is bound, and its buttons stopPropagation');
ok(/event\.key === 'Escape'/.test(detailSrc), 'ESC closes the detail modal');
ok(/event\.target === modal/.test(detailSrc), 'a backdrop click closes the detail modal');
ok(/Confirm\/resolve coming with validation|CANDIDATES_READ_ONLY_HINT = 'Confirm\/resolve coming with validation'/.test(detailSrc),
  'the candidate list still carries the read-only hint (the no-PAT rendering)');

// behaviour: composition, panels, empty state, modal, escaping
const { api: api6, registry: reg6 } = runApp(repoChars);
api6.setFilter('all');
api6.setQuery('tank');
const tankExpected = repoChars.filter(c => [c.role, c.roleSec].some(r => String(r).toLowerCase().includes('tank')));
ok(api6.getFilteredCharacters().length === tankExpected.length,
  `query "tank" matches role/roleSec (${tankExpected.length} chars)`);
api6.setFilter('flowmasters');
const combined = api6.getFilteredCharacters();
ok(combined.length > 0 && combined.every(c => c.track === 'flowmasters'
  && [c.role, c.roleSec].some(r => String(r).toLowerCase().includes('tank'))),
  `search AND track compose (${combined.length} flowmasters match "tank")`);
ok(reg6.get('character-grid').children.length === combined.length,
  'the roster grid renders exactly the searched-and-filtered rows');
ok(reg6.get('race-summary').innerText === `${new Set(combined.map(c => c.race)).size} Races / ${combined.length} Chars`,
  `race panel counts the same pool: "${reg6.get('race-summary').innerText}"`);
ok(reg6.get('class-summary').innerText === `${new Set(combined.map(c => c.class)).size} Classes / ${combined.length} Chars`,
  `class panel counts the same pool: "${reg6.get('class-summary').innerText}"`);
api6.setQuery('zzzz-no-such-character');
ok(reg6.get('character-grid').children.length === 0
  && reg6.get('character-grid').innerHTML.includes('text-[11px] text-slate-500 italic p-3 text-center bg-slate-900/40 rounded border border-slate-800'),
  'an empty search keeps the existing empty-state styling');
ok(reg6.get('character-grid').innerHTML.includes('zzzz-no-such-character'), 'the empty state echoes the query');
api6.setFilter('all');
api6.setQuery('');
ok(reg6.get('character-grid').children.length === repoChars.length,
  `clearing the query restores every row (${reg6.get('character-grid').children.length})`);

ok(reg6.get('detail-modal').classList.contains('hidden'), 'the detail modal starts hidden');
api6.openCharacterDetail(String(repoChars[0].id));
ok(!reg6.get('detail-modal').classList.contains('hidden'), 'openCharacterDetail() reveals the detail modal');
ok(reg6.get('detail-content').innerHTML.includes(repoChars[0].name), 'the modal shows the requested row');
ok(reg6.get('detail-content').innerHTML.includes('No Battle.net data'),
  'with no sidecar loaded it degrades to the "No Battle.net data" placeholder');
ok(!reg6.get('detail-content').innerHTML.includes('>Gear<'), 'no gear section without gear data');
api6.closeDetail();
ok(reg6.get('detail-modal').classList.contains('hidden'), 'closeDetail() hides it again');

const evilRow = { id: '666', player: evil, name: evil, track: 'family', role: 'DPS', roleSec: 'None', race: 'Troll', class: 'Mage', prof1: 'None', prof2: 'None' };
const { api: api7, registry: reg7 } = runApp([evilRow]);
api7.openCharacterDetail('666');
const modalHtml = reg7.get('detail-content').innerHTML;
ok(!reg7.get('detail-modal').classList.contains('hidden'), 'the modal opens for a row whose text is an XSS payload');
ok(!modalHtml.includes('<img src=x') && modalHtml.includes('&lt;img src=x'), 'the modal escapes roster-sourced text');
ok([...reg7.values()].map(el => el.innerHTML).join('').split('<img src=x').length === 1,
  'no panel renders the payload as a tag while the modal is open');

[['https://render.worldofwarcraft.com/a.jpg', true], ['https://us.api.blizzard.com/x.jpg', true],
  ['https://render.worldofwarcraft.com.evil.tld/a.jpg', false], ['https://evil.tld/', false],
  ['//render.worldofwarcraft.com/a.jpg', false], ['javascript:alert(1)', false],
  ['https://render.worldofwarcraft.com@evil.tld/', false], ['', false], [null, false], [42, false]]
  .forEach(([url, want]) => ok(api7.isSafeIconUrl(url) === want, `isSafeIconUrl(${JSON.stringify(url)}) -> ${want}`));

// ---------- 8. Phase 3d: interactive write paths ----------
console.log('\n[8] Phase 3d review actions + on-demand refresh (static wiring)');
const githubSrc = moduleSrc['github.js'];

// every handler the modal renders is published, because inline on*="…" resolves against window
const reviewHandlers = ['confirmMatch', 'selectCandidate', 'confirmSelectedCandidate', 'toggleMismatchForm',
  'reportMismatch', 'reopenForEditing', 'refreshFromBattleNet'];
const unpublished = reviewHandlers.filter(fn => !new RegExp(`window\\.${fn} = ${fn}`).test(detailSrc));
ok(unpublished.length === 0, `every review handler is published on window (missing: ${unpublished.join(',') || 'none'})`);
const undeclared = reviewHandlers.filter(fn => !new RegExp(`(function|async function) ${fn}\\(`).test(detailSrc));
ok(undeclared.length === 0, `every review handler is a declared function (missing: ${undeclared.join(',') || 'none'})`);

// the network stays in github.js: detail.js renders and delegates, never fetches
ok(!/fetch\(/.test(detailSrc) && !/api\.github\.com/.test(detailSrc),
  'detail.js makes no request of its own and names no API host (all writes go through github.js)');
ok(!/localStorage\s*\.\s*(setItem|getItem|removeItem)/.test(detailSrc),
  'the review actions still write nothing to localStorage from the modal module');
// the review section is gated on a PAT in both directions: render and handler
ok(/if \(!ghHasPat\(\)\) return '';/.test(detailSrc), 'the action area renders only when a PAT is configured');
ok((detailSrc.match(/ghHasPat\(\)/g) || []).length >= 3,
  `handlers re-check the PAT as a belt for the disabled button (${(detailSrc.match(/ghHasPat\(\)/g) || []).length} checks)`);

// payload guards stated once, in the module that applies them
ok(/REVIEW_NOTE_MAX = 240/.test(detailSrc), 'the mismatch note cap is 240');
ok(/maxlength="\$\{REVIEW_NOTE_MAX\}"/.test(detailSrc), 'the note textarea declares the cap to the browser too');
ok(/HANDLER_ARG_RE|handlerArg\(value\)/.test(detailSrc) && /SLUG_RE = \//.test(detailSrc),
  'handler ids and confirmed slugs are shape-checked before they reach markup');

// the three payload shapes of design §2, as built by the module
ok(/state: LIFECYCLE\.VALIDATED,\s*confirmedSlug: slug,\s*reviewedBy,\s*reviewedAt: nowStamp\(\)/.test(detailSrc),
  'confirm/resolve build {state: validated, confirmedSlug, reviewedBy, reviewedAt}');
ok(/note: raw, reportedBy, reportedAt: nowStamp\(\)/.test(detailSrc),
  'report mismatch builds {note, reportedBy, reportedAt} and no state key');
ok(/delete copy\.validation;/.test(detailSrc), 're-open removes the validation block entirely');

// the grid escapes every roster string it interpolates — the role pair included — and no panel
// reads the display maps by raw bracket lookup any more (a committed row whose class is
// "constructor" must fall back to the neutral styling rather than resolve Object.prototype)
const gridSrc = moduleSrc['roster.js'];
ok(/\$\{getRoleIcon\(c\.role\)\}\$\{escapeHtml\(c\.role\)\}/.test(gridSrc)
  && /\$\{getRoleIcon\(c\.roleSec\)\}\$\{escapeHtml\(c\.roleSec\)\}/.test(gridSrc),
  'role and roleSec are escaped where the grid interpolates them');
ok(!/\$\{c\.(role|roleSec|name|player|class|race|prof1|prof2)\}/.test(gridSrc)
  && !/\$\{char\.(name|class|player|race|role)\}/.test(moduleSrc['party.js']),
  'no raw roster string is interpolated into the grid or party markup');
ok(/export function ownString\(map, key\)/.test(moduleSrc['util.js']) && /Object\.hasOwn/.test(moduleSrc['util.js']),
  'util.js exposes an own-data map lookup built on Object.hasOwn');
ok(!/CLASS_COLORS\[|CLASS_BAR\[|PROF_ABBR\[|RACE_BAR\[/.test(src),
  'no module bracket-indexes a display map with a roster-sourced value');
['roster.js', 'party.js', 'stats.js', 'professions.js', 'detail.js', 'editchar.js'].forEach(f =>
  ok(/ownString\(|ownValue\(/.test(moduleSrc[f]), `${f} reads its maps through the own-data helper`));

// github.js owns the write API: fresh sha before the PUT, 409 mapped, dispatch + poll
ok(/async function commitRosterFile\(message, charactersArray, opts = \{\}\)/.test(githubSrc),
  'commitRosterFile(message, characters, opts) exists (the one shared commit core)');
ok(/commitRosterFile\('Update characters\.json via WoW: Forever tracker \[skip ci\]',\s*newCharacters, \{ compareBaseline: true \}\)/.test(githubSrc),
  'the bulk edit path delegates to that same core with compareBaseline (no second PUT path left)');
ok(/export function setValidationOnFresh\(freshList, id, mutateFn\)/.test(githubSrc)
  && /return \{ ok: false, message: /.test(githubSrc),
  'review mutations are applied to the freshly read roster, and abort with a message when the row is not there');
ok(/function baselineDriftMessage\(freshList, upstreamMissing\)/.test(githubSrc)
  && /Roster changed upstream — pull first/.test(githubSrc),
  'a drifted baseline is refused with a pull-first message');
ok(!/catch \(err\) \{[\s\S]{0,400}localStorage\.setItem\(STORAGE_KEY/.test(githubSrc.split('export async function pushToGitHub')[1] || ''),
  'the bulk push has no failure path that writes the roster to localStorage');
ok(/ensureRosterEnvelope\(\)/.test(githubSrc), 'a commit writes the v2 envelope even with no fetched wrapper');
ok(/const fresh = await ghApi\(`/.test(githubSrc) && githubSrc.indexOf('const fresh = await ghApi(') < githubSrc.indexOf('const put = await ghApi(contentsPath'),
  'the current sha is fetched immediately before the PUT (never the cached one)');
ok(/put\.status === 409/.test(githubSrc) && /changed upstream/.test(githubSrc), 'a 409 sha conflict maps to a pull-first message');
ok(/async function ghApi\(path, opts = \{\}\)/.test(githubSrc), 'ghApi(path, opts) is the shared helper');
ok((githubSrc.match(/Authorization/g) || []).length === 1,
  `the Authorization header is built in exactly one place (${(githubSrc.match(/Authorization/g) || []).length})`);
ok(/actions\/workflows\/.+dispatches/.test(githubSrc), 'the refresh dispatches the sync-bnet.yml workflow_dispatch endpoint');
ok(/lookup_name:/.test(githubSrc) && /force: 'false'/.test(githubSrc),
  'the dispatch body carries the two declared workflow inputs, inputs.lookup_name and inputs.force');
ok(!/client_payload\s*:/.test(githubSrc),
  'no client_payload key is built anywhere in github.js (workflow_dispatch rejects it with 422; provenance is the runner’s GITHUB_ACTOR)');
ok((githubSrc.match(/^\s*cache: 'no-store',$/gm) || []).length === 1
  && new RegExp("fetch\\([\\s\\S]{0,220}cache: 'no-store'").test(githubSrc),
  'the one fetch() in ghApi opts out of the HTTP cache exactly once (a cached Contents read is a phantom "changed upstream")');
ok(/event=workflow_dispatch/.test(githubSrc) && /created=/.test(githubSrc), 'the poll lists workflow_dispatch runs since the dispatch');
ok(/run\.workflow_id/.test(githubSrc) && /run\.head_branch/.test(githubSrc),
  'a polled run is re-validated by workflow id and ref before it is credited to this dispatch');
ok(/async function syncWorkflowId\(\)/.test(githubSrc),
  'the dispatched workflow id is resolved once (the 202 dispatch response carries no id)');
ok(/REFRESH_POLL_INTERVAL_MS = 10000/.test(githubSrc) && /REFRESH_POLL_TIMEOUT_MS = 180000/.test(githubSrc),
  'the poll is 10 s / 3 min bounded');
ok(/Actions: write/.test(githubSrc), 'a 403 on dispatch explains the actions: write scope');

// data.js: cache-busted reload, for the caller that just watched a run publish the files
ok(/function reloadSidecars\(\)/.test(dataSrc) && /cb=\$\{encodeURIComponent/.test(dataSrc),
  'reloadSidecars() re-reads both sidecars past the HTTP cache');
ok(/function ensureRosterEnvelope\(\)/.test(dataSrc), 'data.js can guarantee the v2 envelope for a commit');

// the transient status line: declared, hidden by default, and written as text only
ok(declaredIds.includes('app-toast') && (declaredClasses.get('app-toast') || '').includes('hidden'),
  'the toast element is declared in the markup and starts hidden');
ok(/el\.textContent = String\(message/.test(moduleSrc['util.js'])
  && !/function showToast\([\s\S]{0,700}innerHTML/.test(moduleSrc['util.js']),
  'the toast is written with textContent only (a GitHub error string cannot become markup)');

// editchar.js: the validated row refuses to open or save, and delete warns
ok(/refuseIfValidated\(c, 'Edit'\)/.test(moduleSrc['editchar.js']), 'editCharacter refuses a validated row');
ok(/refuseIfValidated\(existing, 'Save'\)/.test(moduleSrc['editchar.js']), 'saveCharacter refuses to overwrite a validated row');
ok(/re-creation and re-validation/.test(moduleSrc['editchar.js']), 'deleting a validated row warns it will need re-creation');


console.log(`\n${fails === 0 ? 'ALL CHECKS PASSED' : 'FAILURES PRESENT'}: ${passes} passed, ${fails} failed`);
process.exit(fails === 0 ? 0 : 1);
