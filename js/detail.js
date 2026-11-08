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
import { byRosterId, formatSyncedAt, isSidecarFresh, reloadSidecars } from './data.js';
import { commitValidationOnFresh, dispatchAndAwaitSync, ghHasPat, ghUserLogin, openGhModal, updateRosterInMemory } from './github.js';
import { getLifecycleBadge } from './roster.js';
import { CLASS_COLORS, escapeHtml, getFaction, ownString, showToast } from './util.js';

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
let candidatesOpen = false;
let mismatchOpen = false;
let chosenSlug = null;
let chosenFor = null;
let lastResult = null;      // {id, tone, message}
let refresh = null;         // {id, message} while a dispatch/poll is in flight
let actionRunning = false;

function resetReviewView() {
  candidatesOpen = false;
  mismatchOpen = false;
  chosenSlug = null;
  chosenFor = null;
  lastResult = null;
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
    const picked = chosenFor === String(character.id) ? chosenSlug : null;
    return section('Review', `${resultBanner(character)}
      ${hintBlock('Battle.net could not pick one profile for this name. Select a candidate above and confirm it, or report a mismatch.')}
      ${rows(
        actionButton('Confirm selected', `confirmSelectedCandidate('${id}')`, 'primary', {
          disabled: busy || !picked,
          title: picked ? `Validate this row as ${picked}` : 'Select one candidate first'
        }),
        actionButton('Report mismatch', `toggleMismatchForm('${id}')`, 'danger', {
          disabled: busy,
          title: 'None of these candidates is this character'
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

// The whole modal body for one row. Exported for the verification harness: it is pure over
// (roster row, sidecar row, map entry, badge markup), so the escaping and the section-omission
// rules can be asserted without a click.
// actionsHtml is an internal seam (and a harness seam): null renders this module's own
// review action area for the row, a string replaces it.
export function detailHtml(character, sidecar, mapEntry, lifecycle, badgeHtml = null, actionsHtml = null) {
  if (!character || typeof character !== 'object') return emptyState('No such character.');
  const badge = badgeHtml || getLifecycleBadge(character, lifecycle);
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

  const actions = actionsHtml === null ? reviewHtml(character, mapEntry, lifecycle) : actionsHtml;

  return header + body + gearGrid(sidecar) + progressSections(sidecar) + noteSection
    + reviewNoteSection(character)
    + candidatesIfOpen(lifecycle, mapEntry, character) + actions + noSidecar;
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
// default for exactly those rows; the badge toggle then collapses it. Without one, the Phase 3c
// behaviour stands: collapsed until the badge is clicked, and read-only when shown.
function candidatesIfOpen(lifecycle, mapEntry, character) {
  if (lifecycle !== LIFECYCLE.NEEDS_RESOLUTION) return '';
  const interactive = ghHasPat() && handlerArg(character && character.id) !== '';
  if (!interactive) return candidatesOpen ? candidatesBlock(mapEntry) : '';
  if (candidatesOpen) return '';
  return candidatesBlock(mapEntry, {
    interactive: true,
    id: character && character.id,
    chosenSlug: chosenFor === String(character && character.id) ? chosenSlug : null
  });
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
  resetReviewView();
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
  // Inside the modal the badge is the affordance that toggles the candidate list, so it
  // becomes a button; the card keeps it a plain (clickable) span.
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
window.confirmMatch = confirmMatch;
window.selectCandidate = selectCandidate;
window.confirmSelectedCandidate = confirmSelectedCandidate;
window.toggleMismatchForm = toggleMismatchForm;
window.reportMismatch = reportMismatch;
window.reopenForEditing = reopenForEditing;
window.refreshFromBattleNet = refreshFromBattleNet;
