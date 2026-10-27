import fs from 'fs';
import path from 'path';

const ROOT = process.argv[2] || '.';
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const repoChars = JSON.parse(fs.readFileSync(path.join(ROOT, 'characters.json'), 'utf8'));

let fails = 0, passes = 0;
const ok = (cond, msg) => { if (cond) { passes++; console.log(`  PASS ${msg}`); } else { fails++; console.log(`  FAIL ${msg}`); } };

// ---------- 1. static structure ----------
console.log('\n[1] script extraction + static structure');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
ok(!!scriptMatch, 'inline app script located');
const src = scriptMatch[1];
fs.writeFileSync('/tmp/app_extract.js', src);

const markup = html.slice(0, html.indexOf('<script>'));
const declaredIds = [...markup.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
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

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', innerText: '', className: '', value: '', title: '',
    style: {}, children: [],
    classList: { add() {}, remove() {} },
    appendChild(child) { this.children.push(child); this.innerHTML += child.innerHTML || ''; },
  };
}

function runApp(seededChars) {
  const registry = new Map();
  declaredIds.forEach(id => registry.set(id, makeEl(id)));
  const missing = [];
  const documentStub = {
    getElementById(id) {
      if (!registry.has(id)) { missing.push(id); registry.set(id, makeEl(id)); }
      return registry.get(id);
    },
    querySelectorAll() { return []; },
    createElement(tag) { return makeEl(`<${tag}>`); },
  };
  const storageStub = {
    getItem(key) { return key === 'wow_forever_roster' ? JSON.stringify(seededChars) : null; },
    setItem() {},
  };
  const factory = new Function(
    'document', 'localStorage', 'setInterval', 'alert', 'confirm', 'prompt', 'fetch', 'console',
    `${src}\nreturn { setFilter, renderRoster, get characters() { return characters; } };`
  );
  const api = factory(documentStub, storageStub, () => {}, () => {}, () => false, () => null,
    () => Promise.reject(new Error('network disabled in harness')), { error() {}, log() {} });
  return { api, registry, missing };
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

console.log(`\n${fails === 0 ? 'ALL CHECKS PASSED' : 'FAILURES PRESENT'}: ${passes} passed, ${fails} failed`);
process.exit(fails === 0 ? 0 : 1);
