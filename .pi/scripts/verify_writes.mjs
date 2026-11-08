// verify_writes.mjs — Phase 3d verification for the interactive write paths.
//
// The modules are imported for real, under a DOM/localStorage stub, against a fixture roster
// and sidecars written to a throwaway /tmp directory. Every network call is intercepted: the
// only two origins this harness will ever answer for are https://api.github.com and the fixture
// directory behind the same-origin data/ paths. Anything else fails the run.
//
// What it proves, in order:
//
//   [1] no-PAT gating — no action markup at all, and a handler called anyway opens the PAT
//       modal and sends nothing
//   [2] the action set per lifecycle state (confirm / resolve+radio / report / hint / re-open)
//   [3] the commit happy path — a fresh sha GET immediately before the PUT, the v2 envelope,
//       the design §2 payload shape, GET /user as the source of reviewedBy (cached in memory),
//       the in-memory + localStorage update, the repaint and the success flash
//   [4] the 409 sha-conflict path — pull-first message, and NOTHING applied locally
//   [5] RESOLVE — the radio selection is the only thing CONFIRM SELECTED can commit
//   [6] REPORT MISMATCH — note required, ≤240, escaped where it renders, {note, reportedBy,
//       reportedAt} with no state key, and the row stays a draft
//   [7] RE-OPEN — confirm() gates it, and the committed row carries no validation block
//   [8] REFRESH — the dispatch body (inputs.lookup_name / inputs.force / client_payload.actor),
//       403 → actions: write, the bounded poll to completion, the cache-busted sidecar reload
//       and repaint, a failed run, a timeout, and no double dispatch while one is in flight
//   [9] editchar gating — a validated row refuses to open the edit form and refuses to save,
//       delete warns it will need re-creation, and a draft still saves
//  [10] boundary — the only storage keys written are the roster and the PAT config, and no
//       request left the harness for any other origin
//
//   node .pi/scripts/verify_writes.mjs [repo-root]

import fs from 'fs';
import os from 'os';
import path from 'path';
import { mkdtempSync } from 'fs';
import { pathToFileURL } from 'url';

const ROOT = path.resolve(process.argv[2] || '.');
const GH_ORIGIN = 'https://api.github.com';

let passes = 0;
let fails = 0;
const ok = (cond, msg) => {
  if (cond) { passes++; console.log(`  PASS ${msg}`); }
  else { fails++; console.log(`  FAIL ${msg}`); }
};

// ------------------------------------------------------------------ DOM stub
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
    // Recover every element carrying an inline handler (buttons, and the Phase 3d radio whose
    // onchange is the resolve selection) so a dispatched event reaches it the way the browser's
    // would. Handlers are interpreted, never eval'd.
    for (const match of this._html.matchAll(/<(button|span|a|div|input|textarea)\b([^>]*)>/gi)) {
      const onclick = /\bonclick="([^"]*)"/i.exec(match[2]);
      const onchange = /\bonchange="([^"]*)"/i.exec(match[2]);
      if (!onclick && !onchange) continue;
      const child = new El('', match[1]);
      if (onclick) child.attrs.onclick = decodeEntities(onclick[1]);
      if (onchange) child.attrs.onchange = decodeEntities(onchange[1]);
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

// confirm/alert/prompt are recorded so the dialogs that gate re-open and delete can be asserted,
// and answered per test rather than globally.
const dialogs = { confirmQueue: [], confirmMessages: [], alerts: [] };
globalThis.alert = message => { dialogs.alerts.push(String(message)); };
globalThis.confirm = message => {
  dialogs.confirmMessages.push(String(message));
  return dialogs.confirmQueue.length > 0 ? dialogs.confirmQueue.shift() : false;
};
globalThis.prompt = () => null;

// ------------------------------------------------------------------ fetch stub
// Two origins only: api.github.com (scripted below) and the fixture directory behind the
// same-origin data/ paths. A request for anything else is recorded as a violation.
const net = [];
const netAll = [];      // net is trimmed between sections; this one is the whole run
const violations = [];
let sourceDir = ROOT;
let routes = [];
let runScript = [];

function route(method, pattern, handler) { routes.push({ method, pattern, handler }); }
function resetRoutes() { routes = []; }

function replyFor(method, ghPath, body) {
  for (const entry of routes) {
    if (entry.method !== method) continue;
    if (!entry.pattern.test(ghPath)) continue;
    const result = entry.handler({ path: ghPath, body });
    return result === undefined ? { status: 204, data: null } : result;
  }
  violations.push(`unrouted GitHub call: ${method} ${ghPath}`);
  return { status: 599, data: { message: `unrouted: ${method} ${ghPath}` } };
}

globalThis.fetch = async (url, opts = {}) => {
  const raw = String(url);
  const method = String(opts.method || 'GET').toUpperCase();
  if (raw.startsWith(GH_ORIGIN)) {
    const ghPath = raw.slice(GH_ORIGIN.length);
    const body = typeof opts.body === 'string' ? JSON.parse(opts.body) : null;
    const headers = opts.headers || {};
    const record = { host: 'github', method, path: ghPath, body, auth: headers.Authorization || null,
      contentType: headers['Content-Type'] || null };
    net.push(record);
    netAll.push(record);
    const reply = replyFor(method, ghPath, body);
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      json: async () => (reply.data === undefined ? null : reply.data)
    };
  }
  const rel = raw.replace(/^\.?\//, '').split('?')[0];
  if (!/^[A-Za-z0-9_./-]+$/.test(rel) || rel.startsWith('..')) {
    violations.push(`non-origin fetch: ${raw}`);
    return { ok: false, status: 400, json: async () => null };
  }
  const file = path.join(sourceDir, rel);
  if (raw.indexOf('?cb=') !== -1) net.push({ host: 'fixture', method, path: raw, reloaded: true });
  else net.push({ host: 'fixture', method, path: raw, reloaded: false });
  if (!fs.existsSync(file)) return { ok: false, status: 404, json: async () => null };
  return { ok: true, status: 200, json: async () => JSON.parse(fs.readFileSync(file, 'utf8')) };
};

const ghCalls = (method, needle) => net.filter(c =>
  c.host === 'github' && c.method === method && c.path.indexOf(needle) !== -1);
const callsOf = (method, needle) => ghCalls(method, needle);
const lastCall = (method, needle) => [...ghCalls(method, needle)].pop() || null;
const decodedRoster = body => JSON.parse(Buffer.from(body.content, 'base64').toString('utf8'));
const rowIn = (doc, id) => doc.characters.find(c => c.id === id);

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

// A radio/change event: the resolve selection is the onchange handler, and the harness fires it
// on the recovered <input> child so the value that gets committed comes from the candidate list.
function change(node) {
  const event = { type: 'change', target: node };
  if (node.attrs && node.attrs.onchange) runInline(node.attrs.onchange, event);
  return event;
}

const hidden = id => byId(id).classList.contains('hidden');
const modalHtml = () => byId('detail-content').innerHTML;
// Handler attributes are HTML-escaped in the markup (a quote is &#39;), exactly as a browser
// receives them and decodes them for the attribute value — so assertions about handler text run
// against the decoded form.
const decodedHtml = () => decodeEntities(modalHtml());
const findAction = pattern => byId('detail-content').children.find(c =>
  pattern.test(c.attrs.onclick || '') || pattern.test(c.attrs.onchange || ''));
const toast = () => byId('app-toast');
const settled = () => new Promise(resolve => setTimeout(resolve, 0));

// ------------------------------------------------------------------ fixtures
const FIXTURES = mkdtempSync(path.join(os.tmpdir(), '3d-writes-'));
fs.mkdirSync(path.join(FIXTURES, 'data'), { recursive: true });
const stamp = new Date(Date.now() - 900000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const EVIL = '<img src=x onerror=alert(1)>';

const ROWS = {
  ok: 'r-ok',            // map status ok        → awaiting_confirmation
  ambiguous: 'r-amb',    // map status ambiguous → needs_resolution (2 candidates)
  review: 'r-rev',       // map status needs_review → needs_resolution (candidates kept)
  missing: 'r-nf',       // map status not_found
  blank: 'r-blank',      // no map entry at all  → awaiting_sync
  locked: 'r-locked',    // validation.state validated
  draft: 'r-draft',      // editable draft, used for the save/delete checks
};

function character(id, name, extra = {}) {
  return {
    id, player: 'pandamancer', name, track: 'flowmasters', role: 'DPS', roleSec: 'None',
    race: 'Orc', class: 'Warrior', prof1: 'Mining', prof2: 'None', ...extra,
  };
}

const fixtureRoster = {
  schemaVersion: 2,
  characters: [
    character(ROWS.ok, 'Gearman'),
    character(ROWS.ambiguous, 'Tir Rheebeard'),
    character(ROWS.review, 'Dup Row'),
    character(ROWS.missing, 'Nowhere'),
    character(ROWS.blank, 'No Map'),
    character(ROWS.locked, 'Locked', {
      validation: { state: 'validated', confirmedSlug: 'locked-slug', reviewedBy: 'octocat', reviewedAt: stamp },
    }),
    character(ROWS.draft, 'Drafty'),
  ],
};

const profileHref = slug => `https://us.api.blizzard.com/profile/wow/character/${slug}?namespace=profile-forever-us`;
function mapEntry(name, status, extra = {}) {
  return {
    rosterName: name, status, slug: null, href: null, bnetName: null, surname: null,
    matchedForm: null, mappedVia: null, note: null, candidates: [], ...extra,
  };
}

const fixtureMap = {
  schemaVersion: 2, updatedAt: stamp,
  entries: {
    [ROWS.ok]: mapEntry('gearman', 'ok', {
      slug: 'gear-man', href: profileHref('gear-man'), bnetName: 'Gear', surname: 'Man',
      matchedForm: 'lower_hyphen', mappedVia: 'direct', note: null,
    }),
    [ROWS.ambiguous]: mapEntry('tir rheebeard', 'ambiguous', {
      note: '2 distinct profiles matched the name forms',
      mappedVia: 'direct',
      candidates: [
        { slug: 'tir-rheebeard', name: 'Tir', surname: 'Rheebeard', matchedForm: 'lower_hyphen', href: profileHref('tir-rheebeard') },
        { slug: 'tir-rheebeard-two', name: EVIL, surname: 'Rheebeard', matchedForm: 'space_hyphen', href: profileHref('tir-rheebeard-two') },
      ],
    }),
    [ROWS.review]: mapEntry('dup row', 'needs_review', {
      slug: 'dup-row', href: profileHref('dup-row'), bnetName: 'Dup', surname: 'Row',
      mappedVia: 'direct', note: 'slug dup-row also claimed by id 2; never auto-pick between them',
      candidates: [{ slug: 'dup-row', name: 'Dup', surname: 'Row', matchedForm: 'lower', href: profileHref('dup-row') }],
    }),
    [ROWS.missing]: mapEntry('nowhere', 'not_found', { note: '404 on all forms — likely not opted in (task 0.4)' }),
    [ROWS.locked]: mapEntry('locked', 'ok', {
      slug: 'locked-slug', href: profileHref('locked-slug'), bnetName: 'Locked', surname: null,
      matchedForm: 'confirmed_slug', mappedVia: 'confirmed', note: 'user-confirmed',
    }),
    [ROWS.draft]: mapEntry('drafty', 'not_found', { note: '404 on all forms' }),
  },
};

const fixtureData = {
  schemaVersion: 2, syncedAt: stamp, triggeredBy: 'schedule',
  characters: {
    [ROWS.ok]: {
      status: 'ok',
      bnet: { name: 'Gear', surname: 'Man', href: profileHref('gear-man') },
      level: 60, guild: 'Flowmasters', activeSpec: 'Fury', itemLevel: 142, achievementPoints: 42,
      gear: [{ slot: 'Head', name: 'Helm Before Refresh', ilvl: 100, iconUrl: 'https://render.worldofwarcraft.com/forever-us/icons/56/1.jpg' }],
      progress: { pvp: { honorableKills: 3 } },
      fetchedAt: stamp,
    },
    [ROWS.locked]: { status: 'ok', bnet: { name: 'Locked', surname: null, href: profileHref('locked-slug') }, level: 60, fetchedAt: stamp },
    [ROWS.missing]: { status: 'not_found', fetchedAt: stamp },
  },
};

const writeFixtures = () => {
  fs.writeFileSync(path.join(FIXTURES, 'data', 'characters.json'), JSON.stringify(fixtureRoster, null, 2));
  fs.writeFileSync(path.join(FIXTURES, 'data', 'battle-net-map.json'), JSON.stringify(fixtureMap, null, 2));
  fs.writeFileSync(path.join(FIXTURES, 'data', 'character-data.json'), JSON.stringify(fixtureData, null, 2));
};
writeFixtures();

// ------------------------------------------------------------------ boot
const moduleUrl = name => pathToFileURL(path.join(ROOT, 'js', name)).href;
sourceDir = FIXTURES;
await import(moduleUrl('main.js'));
await new Promise(resolve => setTimeout(resolve, 60));

const state = await import(moduleUrl('state.js'));
const dataMod = await import(moduleUrl('data.js'));
const detail = await import(moduleUrl('detail.js'));
const github = await import(moduleUrl('github.js'));
const editchar = await import(moduleUrl('editchar.js'));

await dataMod.loadRoster();
await dataMod.loadSidecars();
state.setFilter('all');
state.setQuery('');

console.log(`\n[0] boot against ${path.relative(process.cwd(), ROOT) || ROOT}, fixtures at ${FIXTURES}`);
ok(state.getCharacters().length === fixtureRoster.characters.length,
  `fixture roster loaded (${state.getCharacters().length} rows)`);
ok(detail.isDetailOpen() === false && hidden('detail-modal'), 'the detail modal starts closed');
ok(net.every(c => c.host === 'fixture'), `boot made no GitHub request with no repo configured (${net.length} calls, all same-origin)`);

// The PAT, when a test needs it. ghConfig is exported as an object, so this is the same
// mutation the settings form performs.
function usePat(on = true) {
  github.ghConfig.owner = on ? 'acme' : '';
  github.ghConfig.repo = on ? 'wow-forever-roster' : '';
  github.ghConfig.branch = 'main';
  github.ghConfig.token = on ? 'ghp_test_token_never_sent_elsewhere' : '';
}

const openRow = id => { detail.openCharacterDetail(id); return modalHtml(); };

// Sections mutate the roster (a confirmed row stays confirmed), so each one that needs the
// fixture states starts from a fresh copy of them.
function resetRoster() {
  state.setCharacters(JSON.parse(JSON.stringify(fixtureRoster.characters)));
  state.renderAll();
}

// ---------------------------------------------------------------- 1. no PAT
console.log('\n[1] no PAT: no actions rendered, and a handler called anyway prompts the modal');
usePat(false);
net.length = 0;
const noPatHtml = openRow(ROWS.ok);
ok(noPatHtml.includes('Gear Man') && noPatHtml.includes('Flowmasters'),
  'the read-only Battle.net half still renders without a PAT');
ok(!/onclick="(confirmMatch|confirmSelectedCandidate|reportMismatch|reopenForEditing|refreshFromBattleNet)/.test(noPatHtml),
  'no write action is rendered for any state without a PAT');
ok(noPatHtml.includes('>Candidates<') === false && noPatHtml.includes('<input') === false,
  'no resolve controls (candidates or radios) are offered without a PAT');
detail.openCandidateList({ stopPropagation() {} }, ROWS.ambiguous);
ok(modalHtml().includes('Confirm/resolve coming with validation') && !modalHtml().includes('<input'),
  'without a PAT the candidate list stays the read-only Phase 3c view (hint, no radio)');
detail.closeDetail();

for (const [handler, arg] of [['confirmMatch', ROWS.ok], ['reportMismatch', ROWS.ok],
  ['reopenForEditing', ROWS.locked], ['refreshFromBattleNet', ROWS.ok]]) {
  byId('gh-modal').classList.add('hidden');
  const before = net.length;
  const result = await globalThis[handler](arg);
  ok(result === false, `${handler}() declines without a PAT (returned ${result})`);
  ok(net.length === before, `${handler}() sent no request without a PAT`);
  ok(hidden('gh-modal') === false, `${handler}() opened the PAT settings modal instead`);
}
byId('gh-modal').classList.add('hidden');
ok(toast().textContent.length > 0, `the refusal is explained in the toast ("${toast().textContent.slice(0, 46)}…")`);

// ---------------------------------------------------------------- 2. action sets
console.log('\n[2] the action set follows the lifecycle state (with a PAT configured)');
usePat(true);

const okHtml = openRow(ROWS.ok);
ok(okHtml.includes('Confirm match') && /onclick="confirmMatch\('/.test(decodedHtml()),
  'an ok row (awaiting_confirmation) offers CONFIRM MATCH');
ok(okHtml.includes('Report mismatch') && okHtml.includes('Gear Man (gear-man)'),
  'and REPORT MISMATCH, naming the mapping being disputed');
ok(okHtml.includes('Refresh from Battle.net'), 'and REFRESH FROM BATTLE.NET');
ok(!/onclick="reopenForEditing\(/.test(okHtml), 'a draft does not offer re-open');
ok(!okHtml.includes('<input type="radio"'), 'no resolve radio where there is nothing to pick');

const ambHtml = openRow(ROWS.ambiguous);
ok(ambHtml.includes('>Candidates<') && (ambHtml.match(/<input type="radio"/g) || []).length === 2,
  'an ambiguous row shows the candidate list with one radio per candidate');
ok(ambHtml.includes('Confirm selected')
  && /disabled onclick="confirmSelectedCandidate\('/.test(decodedHtml()),
  'CONFIRM SELECTED starts disabled (nothing selected yet)');
ok(ambHtml.includes('Report mismatch'), 'and REPORT MISMATCH');
ok(ambHtml.includes('&lt;img src=x onerror=alert(1)&gt;') && !ambHtml.includes('<img src=x'),
  'a candidate name that is an XSS payload renders escaped');
ok(!/onclick="[^"]*tir-rheebeard/.test(decodedHtml()),
  'no candidate slug is interpolated into a handler (the radios carry the index)');

const reviewHtml = openRow(ROWS.review);
ok(reviewHtml.includes('Confirm selected') && reviewHtml.includes('>Candidates<'),
  'a needs_review row gets the same resolve affordance (its candidates survived the note rule)');

const missingHtml = openRow(ROWS.missing);
ok(missingHtml.includes('use Refresh to query Battle.net'),
  'a not_found row says to use Refresh to query Battle.net');
ok(!/onclick="(confirmMatch|confirmSelectedCandidate|reportMismatch)/.test(missingHtml),
  'and offers no confirm/resolve/report action');
ok(missingHtml.includes('Refresh from Battle.net'), 'while still offering REFRESH');

const blankHtml = openRow(ROWS.blank);
ok(blankHtml.includes('use Refresh to query Battle.net') && !/onclick="confirmMatch\(/.test(blankHtml),
  'an awaiting_sync row (no map entry) behaves the same way');

const lockedHtml = openRow(ROWS.locked);
ok(lockedHtml.includes('Validated by octocat') && /onclick="reopenForEditing\('/.test(decodedHtml()),
  'a validated row shows validated-by and offers RE-OPEN FOR EDITING');
ok(!/onclick="(confirmMatch|reportMismatch)\(/.test(decodedHtml()),
  'and nothing else: a validated row cannot be confirmed or disputed again');
ok(lockedHtml.includes('Refresh from Battle.net'), 'a validated row can still be refreshed');
detail.closeDetail();

// ---------------------------------------------------------------- 3. happy path
console.log('\n[3] commit happy path: fresh sha, v2 envelope, §2 payload, in-memory + flash');
let shaSeq = 0;
const freshSha = () => `sha-fresh-${++shaSeq}`;
const CONTENTS = '/contents/data/characters.json';

function happyCommitRoutes() {
  resetRoutes();
  route('GET', /^\/user$/, () => ({ status: 200, data: { login: 'octocat' } }));
  route('GET', new RegExp(`^/repos/acme/wow-forever-roster${CONTENTS}\\?ref=main$`), () => ({
    status: 200, data: { sha: freshSha(), content: Buffer.from(JSON.stringify(fixtureRoster)).toString('base64') },
  }));
  route('PUT', new RegExp(`^/repos/acme/wow-forever-roster${CONTENTS}$`), ({ body }) => ({
    status: 200, data: { content: { sha: `sha-after-${body.sha}` }, commit: { sha: 'commit-1' } },
  }));
}

happyCommitRoutes();
net.length = 0;
openRow(ROWS.ok);
const confirmed = await globalThis.confirmMatch(ROWS.ok);
await settled();
const putCall = lastCall('PUT', CONTENTS);
ok(confirmed === true && putCall !== null, 'CONFIRM MATCH commits once');
const getsBeforePut = net.filter(c => c.method === 'GET' && c.path.indexOf(CONTENTS) !== -1).length;
ok(net.filter(c => c.method === 'PUT').length === 1 && getsBeforePut === 1,
  'exactly one GET of the file, then one PUT (never a PUT on a cached sha)');
ok(net.findIndex(c => c.method === 'PUT') > net.findIndex(c => c.method === 'GET' && c.path.indexOf(CONTENTS) !== -1),
  'the sha is read immediately before the PUT, in call order');
ok(putCall.auth === 'token ghp_test_token_never_sent_elsewhere' && putCall.contentType === 'application/json',
  'the PUT carries the PAT and nothing else does');
ok(net.filter(c => c.host === 'github' && c.auth === null).length === 0,
  'no GitHub request was sent without the Authorization header');
ok(putCall.body.branch === 'main' && putCall.body.sha === 'sha-fresh-1',
  `the PUT is against branch main at the freshly read sha (${putCall.body.sha})`);
const committedDoc = decodedRoster(putCall.body);
ok(committedDoc.schemaVersion === 2 && Array.isArray(committedDoc.characters)
  && committedDoc.characters.length === fixtureRoster.characters.length,
  'the PUT body is the v2 envelope with the whole roster (no partial file)');
const committedRow = rowIn(committedDoc, ROWS.ok);
ok(JSON.stringify(Object.keys(committedRow.validation).sort()) ===
  JSON.stringify(['confirmedSlug', 'reviewedAt', 'reviewedBy', 'state']),
  `validation keys are exactly {state, confirmedSlug, reviewedBy, reviewedAt} (${Object.keys(committedRow.validation).join(', ')})`);
ok(committedRow.validation.state === 'validated' && committedRow.validation.confirmedSlug === 'gear-man',
  'payload = {state: validated, confirmedSlug: <the map entry slug>}');
ok(committedRow.validation.reviewedBy === 'octocat',
  'reviewedBy comes from GET /user, not from the roster or the sidecar');
ok(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(committedRow.validation.reviewedAt),
  `reviewedAt is an ISO-8601 Z stamp (${committedRow.validation.reviewedAt})`);
ok(/\[skip ci\]/.test(putCall.body.message), `the commit message carries [skip ci] ("${putCall.body.message}")`);
ok(ghCalls('GET', '/user').length === 1, 'GET /user was needed once');
ok(committedRow.name === 'Gearman' && committedRow.class === 'Warrior',
  'the ten original fields ride along untouched');

const inMemory = state.getCharacters().find(c => c.id === ROWS.ok);
ok(inMemory.validation && inMemory.validation.state === 'validated',
  'the in-memory row is updated after the commit lands');
ok(JSON.parse(store.get('wow_forever_roster')).find(c => c.id === ROWS.ok).validation.state === 'validated',
  'the localStorage mirror follows (one writer, github.js)');
ok(modalHtml().includes('Validated by octocat') && /onclick="reopenForEditing\('/.test(decodedHtml()),
  'the open modal repaints into its validated state');
ok(toast().classList.contains('hidden') === false && /Confirmed Gearman as gear-man/.test(toast().textContent),
  `the success flash reads "${toast().textContent}"`);
ok(toast().classList.contains('bg-emerald-950/95') && !toast().classList.contains('bg-red-950/95'),
  'the flash is visibly a success (emerald tone), not just a message');
ok(byId('character-grid').innerHTML.includes('Validated by octocat')
  && byId('character-grid').innerHTML.includes('Validated — use Review to re-open'),
  'the roster card repainted too, and its edit affordance is now the disabled one');
ok(ghCalls('GET', '/user').length === 1, 'the login is cached in memory across actions (no second GET /user)');

// ---------------------------------------------------------------- 4. 409 conflict
console.log('\n[4] sha conflict: pull-first message, nothing applied locally');
resetRoutes();
route('GET', /^\/user$/, () => ({ status: 200, data: { login: 'octocat' } }));
route('GET', new RegExp(CONTENTS), () => ({ status: 200, data: { sha: 'sha-nobody-saw' } }));
route('PUT', new RegExp(CONTENTS), () => ({
  status: 409, data: { message: 'Pull request is required to push to this branch.' },
}));
net.length = 0;
const before409 = JSON.stringify(state.getCharacters());
openRow(ROWS.ambiguous);
change(findAction(/selectCandidate\(/));                    // select the first candidate
const conflicted = await globalThis.confirmSelectedCandidate(ROWS.ambiguous);
await settled();
ok(conflicted === false, 'CONFIRM SELECTED returns false on a 409');
ok(modalHtml().includes('changed upstream') && /pull first/i.test(modalHtml()),
  `the modal explains it: "${(/>[^<]*changed upstream[^<]*</.exec(modalHtml()) || ['(missing)'])[0].slice(1, -1).trim()}"`);
ok(/changed upstream/.test(toast().textContent), 'the toast says the same');
ok(toast().classList.contains('bg-red-950/95'), 'and it is visibly an error tone');
ok(JSON.stringify(state.getCharacters()) === before409,
  'the roster is byte-identical afterwards: a commit that did not land is not shown as landed');
ok(rowIn(decodedRoster(lastCall('PUT', CONTENTS).body), ROWS.ambiguous).validation.state === 'validated',
  'the attempted payload was still well-formed (the conflict is about the base sha, not the shape)');

// ---------------------------------------------------------------- 5. resolve flow
console.log('\n[5] RESOLVE: only a radio selection can be confirmed');
happyCommitRoutes();
net.length = 0;
openRow(ROWS.ambiguous);
ok((findAction(/selectCandidate\(/) || {}).tagName === 'INPUT', 'the candidate rows are radios (one group, one pick)');
const unselected = await globalThis.confirmSelectedCandidate(ROWS.ambiguous);
ok(unselected === false && net.length === 0,
  'with nothing selected, CONFIRM SELECTED commits nothing and says so');
ok(/Select one candidate first/.test(toast().textContent), `the hint says why ("${toast().textContent}")`);
change(findAction(/selectCandidate\(/));                     // select candidate index 0 → tir-rheebeard
ok(modalHtml().includes('Validate this row as tir-rheebeard'),
  'the repaint shows which slug the confirm button will commit');
change(findAction(/selectCandidate\(/));                     // the group only ever holds one selection
const resolved = await globalThis.confirmSelectedCandidate(ROWS.ambiguous);
await settled();
const resolvedRow = rowIn(decodedRoster(lastCall('PUT', CONTENTS).body), ROWS.ambiguous);
ok(resolved === true && resolvedRow.validation.confirmedSlug === 'tir-rheebeard',
  'the chosen candidate slug is what lands in confirmedSlug');
ok(resolvedRow.validation.state === 'validated' && resolvedRow.validation.reviewedBy === 'octocat',
  'the resolve payload is the same §2 shape as confirm-match');
// a slug the map never offered cannot be smuggled in through the handler
ok(detail.selectCandidate(ROWS.ambiguous, '99') === false, 'an out-of-range candidate index selects nothing');
ok(detail.selectCandidate(ROWS.ok, '0') === false, 'a row without candidates selects nothing');

// ---------------------------------------------------------------- 6. report mismatch
console.log('\n[6] REPORT MISMATCH: required, ≤240, escaped, no state key, row stays draft');
resetRoster();
happyCommitRoutes();
net.length = 0;
openRow(ROWS.ok);
fire(findAction(/toggleMismatchForm/));
ok(modalHtml().includes('mismatch-note-') && modalHtml().includes('maxlength="240"'),
  'the form opens with a 240-capped textarea');
const noteField = byId(`mismatch-note-${ROWS.ok}`);
noteField.value = '   ';
const emptyNote = await globalThis.reportMismatch(ROWS.ok);
ok(emptyNote === false && net.length === 0, 'a blank note is refused with no commit');
ok(/note is required/i.test(toast().textContent), `and explained ("${toast().textContent}")`);
noteField.value = 'x'.repeat(241);
const longNote = await globalThis.reportMismatch(ROWS.ok);
ok(longNote === false && net.length === 0, 'a 241-character note is refused too');
noteField.value = `wrong realm: ${EVIL}`;
const reported = await globalThis.reportMismatch(ROWS.ok);
await settled();
const reportCall = lastCall('PUT', CONTENTS);
const reportedRow = reportCall ? rowIn(decodedRoster(reportCall.body), ROWS.ok) : null;
ok(reported === true && reportedRow !== null, 'a valid note commits');
ok(JSON.stringify(Object.keys(reportedRow.validation).sort()) === JSON.stringify(['note', 'reportedAt', 'reportedBy']),
  `report payload keys are exactly {note, reportedBy, reportedAt} (${Object.keys(reportedRow.validation).join(', ')})`);
ok(reportedRow.validation.state === undefined,
  'no state key is written, so the row stays a draft (design §2)');
ok('note' in reportedRow.validation && reportedRow.validation.note.length <= 240,
  'and the stored note is within the cap');
ok(reportedRow.validation.note === `wrong realm: ${EVIL}`,
  'the note is stored verbatim as data (the JSON file is not markup)');
ok(reportedRow.validation.reportedBy === 'octocat'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(reportedRow.validation.reportedAt),
  'reportedBy/reportedAt are the reviewer and the moment');
ok(modalHtml().includes('&lt;img src=x onerror=alert(1)&gt;') && !modalHtml().includes(`<img src=x onerror=alert(1)`),
  'the note renders escaped in the modal (Review note section), never as a tag');
ok(modalHtml().includes('reported by octocat'), 'the review note section names who reported it');
const draftAfter = state.getCharacters().find(c => c.id === ROWS.ok);
ok(draftAfter.validation && draftAfter.validation.state === undefined,
  'the in-memory row is still a draft after the report');
ok(state.deriveLifecycleState(draftAfter, { status: 'ok' }) === 'awaiting_confirmation',
  'and its derived state is still awaiting_confirmation (editable)');
ok(/Report mismatch on Gearman/.test(reportCall.body.message) && /\[skip ci\]/.test(reportCall.body.message),
  `commit message identifies the report ("${reportCall.body.message}")`);

// ---------------------------------------------------------------- 7. re-open
console.log('\n[7] RE-OPEN FOR EDITING: confirm-gated, removes the whole block');
resetRoster();
happyCommitRoutes();
net.length = 0;
openRow(ROWS.locked);
dialogs.confirmQueue = [false];
const declined = await globalThis.reopenForEditing(ROWS.locked);
await settled();
ok(declined === false && net.length === 0, 'declining the dialog commits nothing');
ok(/re-open/i.test(dialogs.confirmMessages[dialogs.confirmMessages.length - 1]),
  'the dialog asks before unlocking a row the team sees as verified');

dialogs.confirmQueue = [true];
const reopened = await globalThis.reopenForEditing(ROWS.locked);
await settled();
const reopenCall = lastCall('PUT', CONTENTS);
const reopenedRow = reopenCall ? rowIn(decodedRoster(reopenCall.body), ROWS.locked) : null;
ok(reopened === true && reopenedRow !== null, 'accepting the dialog commits');
ok(Object.prototype.hasOwnProperty.call(reopenedRow, 'validation') === false,
  'the committed row carries no validation block at all (the whole block is removed)');
ok(state.getCharacters().find(c => c.id === ROWS.locked).validation === undefined,
  'and the in-memory row lost it too');
ok(state.deriveLifecycleState(reopenedRow, fixtureMap.entries[ROWS.locked]) === 'awaiting_confirmation',
  'with the block gone the row derives from the map again (back to a draft)');
ok(/Re-open Locked for editing/.test(reopenCall.body.message), 'the commit message says which row was re-opened');

// ---------------------------------------------------------------- 8. refresh
console.log('\n[8] REFRESH FROM BATTLE.NET: dispatch shape, poll, cache-busted reload');
let dispatched = null;
let runsCallCount = 0;
let htmlDuringPoll = '';
let secondDispatch = null;
const RUNS = '/actions/runs';
const DISPATCH = '/actions/workflows/sync-bnet.yml/dispatches';

function refreshRoutes(conclusion = 'success') {
  resetRoutes();
  runsCallCount = 0;
  route('GET', /^\/user$/, () => ({ status: 200, data: { login: 'octocat' } }));
  route('POST', new RegExp(`${DISPATCH}$`), ({ body }) => { dispatched = body; return { status: 202 }; });
  route('GET', new RegExp(`${RUNS}\\?`), () => {
    runsCallCount += 1;
    if (runsCallCount === 1) {
      htmlDuringPoll = modalHtml();
      // a second Refresh clicked while the first is in flight must not double-dispatch
      secondDispatch = net.filter(c => c.method === 'POST').length;
    }
    if (runsCallCount < 2) return { status: 200, data: { workflow_runs: [{ status: 'queued' }] } };
    return {
      status: 200,
      data: {
        workflow_runs: [
          { name: 'Sync Battle.net data', run_id: 2, status: 'completed', conclusion,
            created_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'), display_title: 'Gearman' },
          { name: 'Sync Battle.net data', run_id: 1, status: 'in_progress', conclusion: null,
            created_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z') },
        ],
      },
    };
  });
}

refreshRoutes('success');
// The run republishes the sidecars: change the fixture file so a reload is observable.
fixtureData.characters[ROWS.ok].gear[0].name = 'Helm After Refresh';
fixtureData.characters[ROWS.ok].fetchedAt = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
fixtureData.triggeredBy = 'octocat';
writeFixtures();

net.length = 0;
openRow(ROWS.ok);
const refreshed = await detail.refreshFromBattleNet(ROWS.ok, { intervalMs: 5, timeoutMs: 500 });
await settled();
const dispatchCall = lastCall('POST', DISPATCH);
ok(refreshed === true && dispatchCall !== null, 'the refresh dispatches the workflow and waits for it');
ok(dispatchCall.path.endsWith(DISPATCH) && dispatchCall.auth !== null,
  'POST …/actions/workflows/sync-bnet.yml/dispatches with the PAT');
ok(dispatched.ref === 'main', `ref is the configured branch (${dispatched.ref})`);
ok(dispatched.inputs && dispatched.inputs.lookup_name === 'Gearman',
  `inputs.lookup_name is the character name (${JSON.stringify(dispatched.inputs)})`);
ok(dispatched.inputs.force === 'false', `inputs.force is false — a refresh never bypasses the cache (${JSON.stringify(dispatched.inputs.force)})`);
ok(dispatched.client_payload && dispatched.client_payload.actor === 'octocat',
  'client_payload.actor carries the logged-in actor');
ok(Object.keys(dispatched.inputs).sort().join(',') === 'force,lookup_name',
  'the inputs are exactly the two the workflow declares (an undeclared key would 422)');
ok(/event=workflow_dispatch/.test(lastCall('GET', RUNS).path),
  'the poll lists workflow_dispatch runs');
ok(/created=%3E\d{4}-\d{2}-\d{2}T\d{2}%3A\d{2}%3A\d{2}Z/.test(lastCall('GET', RUNS).path),
  `with a percent-encoded created=>T filter (${lastCall('GET', RUNS).path.split('?')[1]})`);
ok(runsCallCount >= 2, `the queued run was polled again until it completed (${runsCallCount} polls)`);
ok(/disabled/.test(htmlDuringPoll) && /Sync queued|Dispatching/.test(htmlDuringPoll),
  'while it runs the button is disabled and shows progress text');
ok(secondDispatch === 1, 'a second click mid-flight did not dispatch a second run');
const reloaded = net.filter(c => c.host === 'fixture' && c.reloaded);
ok(reloaded.length === 2 && reloaded.every(c => /cb=\d+/.test(c.path)),
  `both sidecars were re-read past the HTTP cache (${reloaded.map(c => c.path.split('/').pop()).join(', ')})`);
ok(modalHtml().includes('Helm After Refresh'),
  'the reloaded sidecar data is on screen (the reload was rendered, not just fetched)');
ok(byId('bnet-sync-line').innerHTML.includes('octocat'),
  'the reloaded provenance renders too (the header sync line reads the new triggeredBy)');
ok(/refresh finished/i.test(toast().textContent), `and the flash says so ("${toast().textContent}")`);

// 403 → actions: write
resetRoutes();
route('GET', /^\/user$/, () => ({ status: 200, data: { login: 'octocat' } }));
route('POST', new RegExp(`${DISPATCH}$`), () => ({ status: 403, data: { message: 'Resource not accessible by personal access token' } }));
net.length = 0;
openRow(ROWS.ok);
const forbidden = await detail.refreshFromBattleNet(ROWS.ok, { intervalMs: 5, timeoutMs: 100 });
ok(forbidden === false && /Actions: write/.test(toast().textContent),
  `a 403 explains the missing scope ("${toast().textContent}")`);
ok(net.filter(c => c.host === 'fixture' && c.reloaded).length === 0,
  'and no reload is attempted after a refused dispatch');

// a run that fails: report the conclusion, still no reload
refreshRoutes('failure');
openRow(ROWS.ok);
const failedRun = await detail.refreshFromBattleNet(ROWS.ok, { intervalMs: 5, timeoutMs: 500 });
ok(failedRun === false && /failure/.test(toast().textContent),
  `a completed-but-failed run is reported as such ("${toast().textContent}")`);

// timeout: bounded, and it stops asking
resetRoutes();
route('GET', /^\/user$/, () => ({ status: 200, data: { login: 'octocat' } }));
route('POST', new RegExp(`${DISPATCH}$`), () => ({ status: 202 }));
route('GET', new RegExp(`${RUNS}\\?`), () => ({ status: 200, data: { workflow_runs: [{ status: 'in_progress', created_at: new Date().toISOString() }] } }));
const pollsBefore = ghCalls('GET', RUNS).length;
const started = Date.now();
openRow(ROWS.ok);
const timedOut = await detail.refreshFromBattleNet(ROWS.ok, { intervalMs: 10, timeoutMs: 60 });
const elapsed = Date.now() - started;
ok(timedOut === false && /3 minutes|still running/.test(toast().textContent),
  `a run that never finishes gives up with a friendly message ("${toast().textContent}")`);
ok(ghCalls('GET', RUNS).length - pollsBefore <= 12 && elapsed < 3000,
  `the poll is bounded (3 calls in ${elapsed}ms with a 60 ms cap; production cap is 180 s)`);

// runs older than the dispatch are never mistaken for ours
ok(detail && github.candidateRuns({ data: { workflow_runs: [
  { status: 'completed', conclusion: 'success', created_at: '2020-01-01T00:00:00Z' },
  { status: 'completed', conclusion: 'success', event: 'push', created_at: new Date().toISOString() },
] } }, new Date().toISOString()) .length === 0,
  'a push run, or one older than the dispatch, is never treated as ours');

// ---------------------------------------------------------------- 9. editchar gating
console.log('\n[9] editCharacter / saveCharacter / deleteCharacter gating');
resetRoster();
happyCommitRoutes();
net.length = 0;
const lockedRow = state.getCharacters().find(c => c.id === ROWS.locked);
byId('char-modal').classList.add('hidden');
globalThis.editCharacter(ROWS.locked);
ok(hidden('char-modal'), 'a validated row does not open the edit form');
ok(/validated/.test(toast().textContent) && /re-open/i.test(toast().textContent),
  `and the toast explains the way out ("${toast().textContent}")`);

// Fill the form as though it had opened, then submit it: the save path is gated too.
byId('edit-id').value = ROWS.locked;
byId('player-name').value = 'pandamancer';
byId('char-name').value = 'Tampered';
byId('char-track').value = 'family';
byId('char-role').value = 'DPS';
byId('char-role-sec').value = 'None';
byId('char-race').value = 'Orc';
byId('char-class').value = 'Warrior';
byId('char-prof1').value = 'None';
byId('char-prof2').value = 'None';
await globalThis.saveCharacter({ preventDefault() {} });
await settled();
ok(net.length === 0, 'saving a validated row sends nothing');
ok(state.getCharacters().find(c => c.id === ROWS.locked).name === lockedRow.name,
  'and the row is unchanged in the roster');
ok(/Save refused/.test(toast().textContent), `with the same refusal ("${toast().textContent}")`);

// delete of a validated row: the warning names the consequence, and declining commits nothing
dialogs.confirmQueue = [false];
await globalThis.deleteCharacter(ROWS.locked);
ok(net.length === 0 && state.getCharacters().find(c => c.id === ROWS.locked) !== undefined,
  'declining a validated delete removes nothing');
ok(/validated[\s\S]*re-creation/i.test(dialogs.confirmMessages[dialogs.confirmMessages.length - 1]),
  `the delete dialog warns it will need re-creation ("${dialogs.confirmMessages[dialogs.confirmMessages.length - 1].slice(0, 64)}…")`);

// a draft still opens, saves and commits
byId('char-modal').classList.add('hidden');
globalThis.editCharacter(ROWS.draft);
ok(hidden('char-modal') === false, 'a draft row still opens the edit form');
byId('char-name').value = 'Draftier';
const netBeforeDraft = net.length;
await globalThis.saveCharacter({ preventDefault() {} });
await settled();
const draftPut = lastCall('PUT', CONTENTS);
ok(net.filter(c => c.method === 'PUT').length === 1 && draftPut !== null,
  'a draft save still commits through the same Contents API path');
ok(rowIn(decodedRoster(draftPut.body), ROWS.draft).name === 'Draftier',
  'the edit is in the committed roster');
ok(typeof draftPut.body.sha === 'string' && draftPut.body.sha !== '',
  `and the commit is based on a sha the module read from the API (${draftPut.body.sha})`);
ok(net.length > netBeforeDraft, 'the draft path did reach the network (the gate is per-row, not global)');

// --------------------------------------------------------------- 10. boundaries
console.log('\n[10] boundaries');
const keys = [...new Set(storageWrites)];
ok(keys.filter(k => k !== 'wow_forever_roster' && k !== 'wow_gh_config').length === 0,
  `only the roster and the PAT config are ever stored (writes: ${keys.join(', ') || 'none'})`);
ok(violations.length === 0, `no request left for an unexpected origin (${violations.slice(0, 2).join('; ') || 'none'})`);
ok(!JSON.stringify([...store.values()]).includes('ghp_test_token'),
  'the PAT is never written into the roster mirror in localStorage');
detail.closeDetail();
ok(hidden('detail-modal'), 'the modal closes cleanly after all of that');


// The endpoint inventory runs at the very end, so it covers every call the whole harness made.
const githubPaths = [...new Set(netAll.filter(c => c.host === 'github').map(c => c.method + ' ' + c.path.split('?')[0]
  .replace(/\/repos\/acme\/wow-forever-roster/, '/repos/{owner}/{repo}')))].sort();
const ALLOWED_ENDPOINTS = [
  'GET /user',
  'GET /repos/{owner}/{repo}/contents/data/characters.json',
  'PUT /repos/{owner}/{repo}/contents/data/characters.json',
  'POST /repos/{owner}/{repo}/actions/workflows/sync-bnet.yml/dispatches',
  'GET /repos/{owner}/{repo}/actions/runs',
];
const unexpected = githubPaths.filter(p => ALLOWED_ENDPOINTS.indexOf(p) === -1);
ok(unexpected.length === 0 && ALLOWED_ENDPOINTS.every(p => githubPaths.indexOf(p) !== -1),
  `every GitHub call is one of the five documented endpoints (unexpected: ${unexpected.join(', ') || 'none'}; seen: ${githubPaths.join(' | ')})`);
ok(netAll.every(c => c.host !== 'github' || /^token ghp_test_token/.test(c.auth || '')),
  'and every one of them carried the PAT (no unauthenticated GitHub call)');

// --------------------------------------------------------------- 11. PAT hygiene
console.log('\n[11] PAT hygiene: one origin, one header, and a saved token drops the cached login');
const beforeBad = netAll.length;
const badPath = await github.ghApi('/user#fragment');
ok(badPath.ok === false && badPath.error === 'bad-path' && netAll.length === beforeBad,
  'ghApi refuses a path carrying a fragment instead of sending it');
let userCalls = 0;
resetRoutes();
route('GET', /^\/user$/, () => { userCalls += 1; return { status: 200, data: { login: 'octocat' } }; });
route('GET', new RegExp(CONTENTS), () => ({ status: 404, data: { message: 'Not Found' } }));
const cachedNow = await github.ghUserLogin();
ok(cachedNow === 'octocat' && userCalls === 0, 'the reviewer login is served from the in-memory cache');
byId('gh-owner').value = 'acme';
byId('gh-repo').value = 'wow-forever-roster';
byId('gh-branch').value = 'main';
byId('gh-token').value = 'ghp_second_token';
globalThis.saveGhConfig({ preventDefault() {} });
ok(userCalls === 0, 'saving the settings does not itself re-read the login');
const afterTokenChange = await github.ghUserLogin();
ok(userCalls === 1 && afterTokenChange === 'octocat',
  'a saved (possibly different) PAT drops the cached login, so reviewedBy is always the token\u2019s own owner');
ok(byId('sync-status').textContent === 'File missing',
  'the pull after saving still degrades cleanly when the roster file is not there');

console.log(`\n${fails === 0 ? 'ALL CHECKS PASSED' : 'FAILURES PRESENT'}: ${passes} passed, ${fails} failed`);
process.exit(fails === 0 ? 0 : 1);
