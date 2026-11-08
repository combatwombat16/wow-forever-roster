// github.js — repo status widgets, the PAT settings modal, the Contents API
// pull/push for data/characters.json, and (Phase 3d, design §1/§4) the review commit,
// the sync-bnet.yml workflow_dispatch and the run polling that backs "Refresh from
// Battle.net".
//
// Every call to api.github.com goes through ghApi(): it is the one place that attaches
// the PAT, the one place that sets Accept/Content-Type, and it never sends the token
// anywhere else (the URL is built from GH_API plus a literal path, and owner/repo/branch
// come from the user's own settings, not from response data — and even those are shape-
// checked with OWNER_REPO_RE before a URL is assembled, so a configured value can only
// ever be a path segment rather than something that moves the endpoint).
//
// Roster writes are read-modify-write (commitRosterFile): the file is GET first, the change is
// applied to THAT content, and the PUT carries THAT sha. A review action rebases onto whatever
// a second reviewer committed in the meantime; a whole-file bulk edit refuses instead of
// overwriting it. A failed commit never touches characters or localStorage.

import { STORAGE_KEY, getCharacters, normalizeRoster, setCharacters, renderAll } from './state.js';
import { ROSTER_PATH, ensureRosterEnvelope, getBaselineRoster, rosterCharactersFrom, serializeRoster,
  setBaselineRoster, setFetchedWrapper } from './data.js';
import { showToast } from './util.js';

export let ghConfig = JSON.parse(localStorage.getItem('wow_gh_config') || 'null') || {
  owner: '',
  repo: '',
  branch: 'main',
  token: ''
};

// The GitHub REST origin. Nothing in this module may fetch any other host with the PAT.
export const GH_API = 'https://api.github.com';
// The workflow the Refresh action dispatches (design §4). The file name is accepted where a
// workflow_id is called for (the dispatch and the single-workflow lookup), but the run list
// reports the numeric id, so syncWorkflowId() resolves it before anything is attributed.
export const SYNC_WORKFLOW_FILE = 'sync-bnet.yml';
// Poll cadence for a dispatched run (design §4: poll until completion, then refetch).
export const REFRESH_POLL_INTERVAL_MS = 10000;
export const REFRESH_POLL_TIMEOUT_MS = 180000;
// Tolerance between this browser's clock and GitHub's created_at stamps: the run we are
// waiting for is dispatched microseconds after we record `since`, so a client running a
// little ahead of GitHub's clock must not filter the only run that matters out.
export const REFRESH_CLOCK_SKEW_MS = 90000;

// ---------- the one GitHub API helper ----------

// The origin can never move: every URL is GH_API plus a path that starts with '/', so even a
// hostile repo or branch value stays on api.github.com. This guard is the second belt — the path
// is also restricted to path- and query-safe characters, so a value that tried to reach a
// different endpoint by way of a fragment or a stray backslash is refused before fetch().
const GH_PATH_RE = /^\/[A-Za-z0-9\-._~/?&=%]*$/;

// ghApi(path, opts) -> {ok, status, data}. Never throws and never rejects: a review
// commit needs to tell 409 (upstream moved) apart from 403 (PAT missing actions:write),
// so the status code is returned rather than flattened into an exception. No response
// body is trusted here — callers pick the fields they use, and every string they render
// is escaped at the point of use.
export async function ghApi(path, opts = {}) {
  if (!GH_PATH_RE.test(path)) {
    return { ok: false, status: 0, data: null, error: 'bad-path', message: 'Refusing to call an unexpected GitHub path.' };
  }
  if (!ghConfig.owner || !ghConfig.repo) {
    return { ok: false, status: 0, data: null, error: 'no-repo' };
  }
  // Shape-checked here, at the one choke point, so no caller can reach fetch() with a value
  // that changes which endpoint is called. Nothing is sent on a bad shape.
  if (!isOwnerRepoShape(ghConfig.owner) || !isOwnerRepoShape(ghConfig.repo)) {
    return { ok: false, status: 0, data: null, error: 'bad-repo',
      message: 'The configured owner/repo is not a valid GitHub name — open ⚙️ Settings and set them again.' };
  }
  const headers = { 'Accept': 'application/vnd.github.v3+json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (ghConfig.token) headers['Authorization'] = `token ${ghConfig.token}`;

  let res;
  try {
    res = await fetch(`${GH_API}${path}`, {
      method: opts.method || 'GET',
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body)
    });
  } catch (err) {
    return { ok: false, status: 0, data: null, error: 'network', message: `Could not reach GitHub (${err && err.name ? err.name : 'network error'}).` };
  }

  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;   // 204 No Content, or a non-JSON error page
  }
  const status = Number(res.status);
  return { ok: status >= 200 && status < 300, status: Number.isFinite(status) ? status : 0, data };
}

// GitHub's own login and repository name shape (alphanumerics plus the . _ - a repository
// name may carry), anchored, so a configured owner/repo can only ever be a path segment. The
// origin was already immovable (every URL is GH_API plus a path), but `..` or `?` in a repo
// name could steer the *endpoint* while still looking same-origin, so the value is checked
// before a URL is assembled rather than classified afterwards.
export const OWNER_REPO_RE = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$/;

export function isOwnerRepoShape(value) {
  return typeof value === 'string' && OWNER_REPO_RE.test(value);
}

// The shortest human-readable explanation GitHub gave, with a status fallback. Returned
// as text: the caller renders it through the toast (textContent) or escapeHtml.
export function ghMessage(res) {
  const message = res && res.data && typeof res.data.message === 'string' ? res.data.message.trim() : '';
  return message === '' ? (res && res.status ? `HTTP ${res.status}` : 'no response from GitHub') : message;
}

// A PAT plus a configured repo is what every write path needs; the review actions are not
// even rendered without it, and each handler re-checks it as a belt for the disabled button.
export function ghHasPat() {
  return Boolean(ghConfig.owner && ghConfig.repo && ghConfig.token);
}

// ---------- reviewer identity (design §2 reviewedBy) ----------

// The login of the PAT's owner. Cached in memory only — an audit field that names the
// wrong person is worse than one that is missing, so it is never guessed from localStorage
// and never derived from the roster.
let cachedLogin = null;

export function resetGhLoginCache() {
  cachedLogin = null;
}

export async function ghUserLogin() {
  if (cachedLogin !== null) return cachedLogin;
  if (!ghConfig.token) return null;
  const res = await ghApi('/user');
  const login = res.ok && res.data && typeof res.data.login === 'string' ? res.data.login.trim() : '';
  if (login === '') return null;
  cachedLogin = login;
  return cachedLogin;
}

// ---------- Phase 3d: the review commit ----------

// ---------- reading the file back (the read side of read-modify-write) ----------

// The Contents API returns base64 with a MIME newline every 60 characters. This is the same
// decode pullFromGitHub has always used, factored out so the modify path reconciles against
// exactly the bytes the browser loaded, and so a truncated or non-UTF-8 body produces null
// ("unreadable") rather than a half-parsed roster.
function contentsText(data) {
  const encoded = data && typeof data.content === 'string' ? data.content : '';
  if (encoded === '') return '';
  try {
    return decodeURIComponent(escape(atob(encoded.replace(/\s/g, ''))));
  } catch (err) {
    return '';
  }
}

// The characters array the branch actually holds, or null when the response carries no
// readable roster.
function freshRosterFrom(res) {
  const text = contentsText(res.data);
  if (text === '') return null;
  try {
    return rosterCharactersFrom(JSON.parse(text));
  } catch (err) {
    return null;
  }
}

// Keys sorted, so "the same roster" means the same JSON value rather than the same bytes: a
// re-serialised row must not look like drift because one key moved or the indent changed.
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort()
      .map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value === undefined ? null : value);
}

export function rostersSemanticallyEqual(a, b) {
  return canonicalJson(a) === canonicalJson(b);
}

// The compare-and-replace rule for the whole-file (bulk edit) path. The PUT replaces
// characters.json outright, so it is allowed only while the branch still holds what this
// browser loaded; anything else is "pull first". Without a baseline the comparison is
// impossible, and overwriting rows this page never saw is precisely the failure the check
// exists for, so that case refuses too (⚙️ Settings pulls, and Pull re-arms it).
function baselineDriftMessage(freshList, upstreamMissing) {
  if (upstreamMissing) return '';      // a create has nothing to drift from
  const baseline = getBaselineRoster();
  if (typeof baseline !== 'string' || baseline === '') {
    return 'This browser has no copy of the roster on GitHub to compare against — pull first, then repeat this edit.';
  }
  let baselineList = null;
  try {
    baselineList = JSON.parse(baseline);
  } catch (err) {
    baselineList = null;
  }
  if (!Array.isArray(baselineList) || !Array.isArray(freshList)) {
    return 'The roster on GitHub could not be read as JSON — pull first, then repeat this edit.';
  }
  if (rostersSemanticallyEqual(freshList, baselineList)) return '';
  return 'Roster changed upstream — pull first, then repeat this edit. Nothing was committed, and your local roster was left untouched.';
}

// ---------- the review commit ----------

// Apply one row's review mutation to the roster as the branch holds it (read-modify-write).
//
// `mutateFn` receives a copy of the fresh row and mutates that copy's `validation` block in
// place (detail.js owns the payload shape; this function owns the reconciliation). The row is
// verified before anything is written: it has to exist exactly once and still look like a
// roster row, because stamping a validation block onto a row this browser never saw would be a
// review of nothing. Aborts return {ok:false, message} and the caller commits nothing.
export function setValidationOnFresh(freshList, id, mutateFn) {
  if (typeof mutateFn !== 'function') {
    return { ok: false, message: 'The review action carried no mutation to apply.' };
  }
  if (!Array.isArray(freshList) || freshList.length === 0) {
    return { ok: false, message: 'The roster on GitHub could not be read — pull first, then repeat this review action.' };
  }
  const key = String(id == null ? '' : id);
  const matches = freshList.filter(c => c && typeof c === 'object' && String(c.id) === key);
  if (matches.length === 0) {
    return { ok: false, message: 'That character is no longer in the roster on GitHub — pull first, then repeat this review action.' };
  }
  if (matches.length > 1) {
    return { ok: false, message: 'The roster on GitHub holds more than one row with that id — resolve the duplicate before reviewing.' };
  }
  const row = matches[0];
  if (typeof row.name !== 'string' || row.name.trim() === '') {
    return { ok: false, message: 'That row on GitHub has no character name — fix the roster before reviewing it.' };
  }
  const copy = { ...row };
  mutateFn(copy);
  const list = freshList.slice();
  list[freshList.indexOf(row)] = copy;
  return { ok: true, list };
}

// Commit the roster to data/characters.json in the v2 envelope. Every write path in the site
// goes through here, and every one of them is read-modify-write over the branch content:
//
//   * the sha is fetched immediately before the PUT, every time, and no sha cached at page-load
//     is ever used as the base — the nightly sync commits to the same branch, so a load-time sha
//     is exactly the stale base that would either 409 or (worse) clobber the sync's commit;
//   * `opts.mutate` (the review actions) applies the change to the roster that GET returned, so
//     a commit that landed after this page loaded is rebased onto rather than reverted;
//   * `opts.compareBaseline` (the bulk edit path) refuses a whole-file PUT when the branch
//     content drifted semantically from the baseline this browser loaded.
//
// A 409 stays the honest answer for the file moving between the GET and the PUT, and maps to a
// "pull first" message rather than a silent local fallback.
//
// Returns {ok, sha, list} or {ok:false, error, message} where error is one of
// 'no-repo' | 'no-pat' | 'bad-payload' | 'conflict' | 'forbidden' | 'http' | 'network'.
export async function commitRosterFile(message, charactersArray, opts = {}) {
  if (!ghConfig.owner || !ghConfig.repo) {
    return { ok: false, error: 'no-repo', message: 'No GitHub repository is configured — open ⚙️ Settings and set owner and repo first.' };
  }
  if (!ghConfig.token) {
    return { ok: false, error: 'no-pat', message: 'A GitHub PAT is required to commit the roster.' };
  }
  const mutate = typeof opts.mutate === 'function' ? opts.mutate : null;
  if (!mutate && (!Array.isArray(charactersArray) || charactersArray.length === 0)) {
    return { ok: false, error: 'bad-payload', message: 'Refusing to commit an empty roster.' };
  }

  const contentsPath = `/repos/${ghConfig.owner}/${ghConfig.repo}/contents/${ROSTER_PATH}`;
  const fresh = await ghApi(`${contentsPath}?ref=${encodeURIComponent(ghConfig.branch)}`);
  // 404 means the file does not exist on this branch yet, which is a create, not an error.
  if (!fresh.ok && fresh.status !== 404) {
    return { ok: false, error: fresh.error || 'http', message: `Could not read the current roster: ${ghMessage(fresh)}` };
  }
  const upstreamMissing = fresh.status === 404;
  const freshList = upstreamMissing ? null : freshRosterFrom(fresh);
  // Every write path here either mutates or compares against the branch content, so an
  // unreadable file stops the commit rather than degrading to "commit the local copy anyway".
  if (!upstreamMissing && freshList === null) {
    return { ok: false, error: 'conflict', message: 'The roster on GitHub could not be read as JSON — pull first, then repeat this action.' };
  }
  const baseSha = fresh.ok && fresh.data && typeof fresh.data.sha === 'string' && fresh.data.sha !== ''
    ? fresh.data.sha : null;

  let listToCommit = charactersArray;
  if (mutate) {
    const rebased = setValidationOnFresh(freshList, opts.id, mutate);
    if (!rebased.ok) return { ok: false, error: 'conflict', message: rebased.message };
    listToCommit = rebased.list;
  } else if (opts.compareBaseline === true) {
    const drift = baselineDriftMessage(freshList, upstreamMissing);
    if (drift !== '') return { ok: false, error: 'conflict', message: drift };
  }
  if (!Array.isArray(listToCommit) || listToCommit.length === 0) {
    return { ok: false, error: 'bad-payload', message: 'Refusing to commit an empty roster.' };
  }

  // Never emit a bare v1 array: main holds the v2 envelope, and a browser that only ever
  // had localStorage would otherwise rewrite the file back to the pre-migration shape.
  ensureRosterEnvelope();
  const body = {
    message: String(message == null ? '' : message),
    content: btoa(unescape(encodeURIComponent(serializeRoster(listToCommit)))),
    branch: ghConfig.branch
  };
  if (baseSha) body.sha = baseSha;

  const put = await ghApi(contentsPath, { method: 'PUT', body });
  if (put.ok) {
    const written = put.data && put.data.content && typeof put.data.content.sha === 'string'
      ? put.data.content.sha : null;
    // The branch now holds exactly this list, so it is the baseline the next bulk edit compares to.
    setBaselineRoster(listToCommit);
    return { ok: true, sha: written, list: listToCommit, data: put.data };
  }
  if (put.status === 409) {
    return { ok: false, error: 'conflict', message: 'The roster changed upstream — pull first, then repeat this review action.' };
  }
  if (put.status === 401 || put.status === 403) {
    return { ok: false, error: 'forbidden', message: `GitHub refused the commit (the PAT needs Contents: read & write): ${ghMessage(put)}` };
  }
  return { ok: false, error: 'http', message: `Commit failed: ${ghMessage(put)}` };
}

// The review commit used by detail.js: `mutateFn` mutates a copy of the row as the branch
// holds it, so two reviewers editing different rows both land (read-modify-write).
export function commitValidationOnFresh(message, id, mutateFn) {
  return commitRosterFile(message, null, { id, mutate: mutateFn });
}

// Apply a committed roster to this browser: state is the source of truth, localStorage is
// the offline mirror (the same two writes every other roster mutation in this file does),
// and the repaint goes through the one dispatcher. A rebased list arrives straight off the
// branch, so it passes through the same normalizer as every other roster entering state.
export function updateRosterInMemory(nextCharacters) {
  setCharacters(normalizeRoster(Array.isArray(nextCharacters) ? nextCharacters : []));
  localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
  renderAll();
}

// ---------- Phase 3d: on-demand Battle.net refresh (design §4) ----------

// POST …/actions/workflows/sync-bnet.yml/dispatches.
//
// The inputs are exactly the two the workflow declares (`lookup_name`, `force`); the
// workflow_dispatch API rejects undeclared input keys with 422, so nothing else goes in
// there. Values are strings because REST input values are strings — the workflow reads
// them as github.event.inputs.force == "true", which is the comparison it already does.
//
// `force` is always false: a refresh is a request for this character's current data, not
// a request to bypass the Last-Modified cache (and design §2 keeps --force out of the
// trust rules entirely).
//
// `client_payload` is sent because design §4 asks for it, and it is inert here:
// client_payload is a repository_dispatch field, so a workflow_dispatch run cannot read
// it. That is the safer direction — the provenance the sync records (artifacts.triggered_by)
// comes from the runner's own GITHUB_ACTOR, i.e. from the authenticated actor, and never
// from a client-supplied string a browser could forge.
export async function dispatchSyncWorkflow(lookupName, actor) {
  const path = `/repos/${ghConfig.owner}/${ghConfig.repo}/actions/workflows/${SYNC_WORKFLOW_FILE}/dispatches`;
  const res = await ghApi(path, {
    method: 'POST',
    body: {
      ref: ghConfig.branch,
      inputs: { lookup_name: String(lookupName == null ? '' : lookupName), force: 'false' },
      client_payload: { actor: String(actor == null ? '' : actor) }
    }
  });
  if (res.ok) return { ok: true, status: res.status };
  if (res.status === 401 || res.status === 403) {
    return { ok: false, error: 'forbidden', message: 'Your PAT needs Actions: write to refresh from Battle.net. Add the scope in GitHub, then paste the token again in ⚙️ Settings.' };
  }
  if (res.status === 404) {
    return { ok: false, error: 'not-found', message: `GitHub has no ${SYNC_WORKFLOW_FILE} in ${ghConfig.owner}/${ghConfig.repo} (${ghConfig.branch}).` };
  }
  if (res.status === 422) {
    return { ok: false, error: 'unprocessable', message: `GitHub rejected the dispatch: ${ghMessage(res)}` };
  }
  return { ok: false, error: res.error || 'http', message: `Could not start the sync: ${ghMessage(res)}` };
}

// The numeric id of the sync workflow, cached per repository.
//
// `run.workflow_id` is the only field that binds a polled run to the workflow we dispatched:
// the dispatch answer is a 202 with no body, so there is nothing to compare at dispatch time,
// and `event`/`created_at` alone would credit a colleague's dispatch of some other workflow. The
// workflow lookup by file name is a single request, so the id is read once per owner/repo (and
// re-read when the repository changes, which is why the key travels with the cached value).
let syncWorkflowCache = { key: '', id: null, name: '' };

export function resetSyncWorkflowCache() {
  syncWorkflowCache = { key: '', id: null, name: '' };
}

// The workflow's id as a string, or null when GitHub does not report a workflow whose file is
// sync-bnet.yml. Callers fail closed on null: an unidentified run is never reported as ours.
export async function syncWorkflowId() {
  const key = `${ghConfig.owner}/${ghConfig.repo}`;
  if (syncWorkflowCache.key === key && syncWorkflowCache.id !== null) return syncWorkflowCache.id;
  const res = await ghApi(`/repos/${key}/actions/workflows/${SYNC_WORKFLOW_FILE}`);
  const data = res && res.ok && res.data && typeof res.data === 'object' ? res.data : null;
  const path = data && typeof data.path === 'string' ? data.path : '';
  const id = data && (typeof data.id === 'number' || typeof data.id === 'string') ? String(data.id).trim() : '';
  if (id === '' || path.split('/').pop() !== SYNC_WORKFLOW_FILE) return null;
  syncWorkflowCache = { key, id, name: data && typeof data.name === 'string' ? data.name : '' };
  return id;
}

// The list-runs query. `created=>T` is GitHub's greater-than form for that filter; it is
// percent-encoded, and candidateRuns() re-checks created_at locally, so a run older than T
// can never be mistaken for ours even if the server ignored the filter.
export function recentDispatchRunsPath(sinceIso) {
  return `/repos/${ghConfig.owner}/${ghConfig.repo}/actions/runs`
    + `?event=workflow_dispatch&created=${encodeURIComponent(`>${sinceIso}`)}`;
}

// Newest first, and only the runs that can actually be attributed to our dispatch:
//
//   * workflow_dispatch (a push, schedule or somebody else's workflow is never ours);
//   * `run.workflow_id` equal to the dispatched workflow's id, when the caller supplied one —
//     the attribution the 202 dispatch response cannot give us;
//   * the workflow's display name, when one was read, has to agree: a belt for a response that
//     names the run after something else;
//   * `run.head_branch` equal to the branch we dispatched against;
//   * created at or after `sinceIso`. The clock allowance is applied exactly once, by
//     dispatchAndAwaitSync, so this re-checks the same bound instead of widening it again (the
//     old double subtraction let 180 s of unrelated runs through).
//
// Anything unshapely is dropped rather than guessed about.
export function candidateRuns(res, sinceIso, opts = {}) {
  const raw = res && res.data && Array.isArray(res.data.workflow_runs) ? res.data.workflow_runs : [];
  const since = Date.parse(sinceIso);
  const workflowId = opts.workflowId == null || opts.workflowId === '' ? null : String(opts.workflowId);
  const workflowName = typeof opts.workflowName === 'string' ? opts.workflowName : '';
  const branch = typeof opts.branch === 'string' && opts.branch !== '' ? opts.branch : ghConfig.branch;
  return raw.filter(run => {
    if (!run || typeof run !== 'object') return false;
    if (typeof run.event === 'string' && run.event !== 'workflow_dispatch') return false;
    if (workflowId !== null && String(run.workflow_id) !== workflowId) return false;
    if (workflowName !== '' && typeof run.name === 'string' && run.name !== '' && run.name !== workflowName) return false;
    if (typeof run.head_branch !== 'string' || run.head_branch !== branch) return false;
    const created = Date.parse(run.created_at);
    return Number.isFinite(created) && Number.isFinite(since) ? created >= since : false;
  }).sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
}

// Wait for the dispatched run to complete. One request per interval, bounded by timeoutMs,
// so a hung workflow costs a message and not an open-ended spinner (the workflow itself
// has a 15 minute job timeout, which is why the client gives up first).
export async function pollSyncRun(sinceIso, opts = {}) {
  const intervalMs = Number.isFinite(opts.intervalMs) && opts.intervalMs > 0 ? opts.intervalMs : REFRESH_POLL_INTERVAL_MS;
  const timeoutMs = Number.isFinite(opts.timeoutMs) && opts.timeoutMs > 0 ? opts.timeoutMs : REFRESH_POLL_TIMEOUT_MS;
  const wait = opts.wait || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const deadline = Date.now() + timeoutMs;
  let sawRun = false;

  for (;;) {
    await wait(intervalMs);
    const res = await ghApi(recentDispatchRunsPath(sinceIso));
    if (!res.ok) {
      return { ok: false, error: 'http', message: `Could not check the sync run: ${ghMessage(res)}` };
    }
    const runs = candidateRuns(res, sinceIso, {
      workflowId: opts.workflowId,
      workflowName: syncWorkflowCache.name,
      branch: ghConfig.branch
    });
    if (runs.length > 0) sawRun = true;
    const completed = runs.find(run => run.status === 'completed');
    if (completed) {
      const conclusion = typeof completed.conclusion === 'string' ? completed.conclusion : 'unknown';
      if (conclusion === 'success') return { ok: true, conclusion, run: completed };
      return {
        ok: false,
        error: 'run-failed',
        conclusion,
        run: completed,
        message: `The Battle.net sync run finished as "${conclusion}". Nothing was published for ${lookupNameHint(completed)}.`
      };
    }
    if (Date.now() >= deadline) {
      return {
        ok: false,
        error: 'timeout',
        message: sawRun
          ? 'The Battle.net sync is still running after 3 minutes. Try again in a moment — the nightly run may be occupying the sync queue.'
          : 'No dispatch was recorded after 3 minutes. The workflow may not have started; check the Actions tab.'
      };
    }
  }
}

function lookupNameHint(run) {
  const title = run && typeof run.display_title === 'string' ? run.display_title.trim() : '';
  return title === '' ? 'this character' : title;
}

// The whole Refresh flow: who am I, dispatch, then poll. Returns the poll verdict (or the
// dispatch failure), so the caller only decides what to render.
export async function dispatchAndAwaitSync(lookupName, opts = {}) {
  const onPhase = typeof opts.onPhase === 'function' ? opts.onPhase : () => {};
  const actor = await ghUserLogin();
  // The dispatch is a 202 with no body, so the run can only be identified by the workflow's own
  // id. Resolve it BEFORE dispatching: no id means no attribution, and failing closed here means
  // we never start a run we cannot watch. (Reviewer nit f2d4edd: resolving it after the dispatch
  // also pushed `since` behind any slow id lookup, risking our own run falling outside the window.)
  const workflowId = await syncWorkflowId();
  if (workflowId === null) {
    return {
      ok: false,
      error: 'workflow-unknown',
      message: `GitHub did not report ${SYNC_WORKFLOW_FILE} for ${ghConfig.owner}/${ghConfig.repo}, so its run cannot be identified. Check the Actions tab.`
    };
  }
  onPhase('Dispatching the Battle.net sync…');
  const dispatch = await dispatchSyncWorkflow(lookupName, actor);
  if (!dispatch.ok) return dispatch;
  // The one and only clock-skew correction: `since` already carries the allowance, and
  // candidateRuns() re-checks against this same instant instead of subtracting it again.
  const since = new Date(Date.now() - REFRESH_CLOCK_SKEW_MS).toISOString().replace(/\.\d{3}Z$/, 'Z');
  onPhase('Sync queued — waiting for the run to finish…');
  return pollSyncRun(since, { ...opts, workflowId });
}

export function setSyncStatus(msg, isError = false) {
  const el = document.getElementById('sync-status');
  el.textContent = msg;
  el.className = isError ? 'text-red-400 font-semibold truncate max-w-[130px]' : 'text-emerald-400 font-semibold truncate max-w-[130px]';
}

export function updateRepoDisplay() {
  const el = document.getElementById('repo-display');
  if (ghConfig.owner && ghConfig.repo) {
    el.textContent = `${ghConfig.owner}/${ghConfig.repo}`;
    el.title = `${ghConfig.owner}/${ghConfig.repo} (${ghConfig.branch})`;
  } else {
    el.textContent = 'Not Set';
    el.title = 'Click ⚙️ to configure GitHub repo';
  }
}

export async function pullFromGitHub() {
  if (!ghConfig.owner || !ghConfig.repo) {
    setSyncStatus('Local only', false);
    return;
  }

  setSyncStatus('Pulling...');
  const path = `/repos/${ghConfig.owner}/${ghConfig.repo}/contents/${ROSTER_PATH}?ref=${encodeURIComponent(ghConfig.branch)}`;

  try {
    const res = await ghApi(path);
    if (res.status === 404) {
      setSyncStatus('File missing', true);
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = res.data;
    const decoded = decodeURIComponent(escape(atob(data.content.replace(/\s/g, ''))));
    const doc = JSON.parse(decoded);
    const list = rosterCharactersFrom(doc);
    if (!list) throw new Error('no characters in data/characters.json');

    setFetchedWrapper(doc);
    // What the branch holds right now, recorded as the baseline the bulk edit path compares to.
    setBaselineRoster(list);
    setCharacters(list);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    setSyncStatus(`Live (${new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})})`);
    renderAll();
  } catch (err) {
    console.error('GitHub fetch failed:', err);
    setSyncStatus('Sync error', true);
  }
}

// The bulk edit path (the Add/Edit modal's save and delete). It replaces the whole file, so it
// goes through the same shared commit as the review actions: a freshly read sha, and
// `compareBaseline` — the branch must still hold what this browser loaded, or the commit is
// refused with "pull first".
//
// A failed commit applies NOTHING locally. The old path wrote `characters` and localStorage in
// its catch block, which showed an edit as saved here while the repository still said otherwise
// (and let a reload silently undo it).
export async function pushToGitHub(newCharacters) {
  // No repository configured is not a failure: it is the documented local-only mode, and no
  // request is possible, so the edit stays on this browser.
  if (!ghConfig.owner || !ghConfig.repo) {
    updateRosterInMemory(newCharacters);
    return;
  }

  if (!ghConfig.token) {
    const tokenPrompt = prompt("To commit changes to characters.json, enter your GitHub Personal Access Token (PAT):");
    if (tokenPrompt) {
      ghConfig.token = tokenPrompt.trim();
      // A token typed here can belong to a different person than the cached one, and it becomes
      // the reviewedBy audit field, so the cache dies with the token it belongs to — at the
      // assignment, not only where the settings form happens to call it.
      resetGhLoginCache();
      localStorage.setItem('wow_gh_config', JSON.stringify(ghConfig));
    } else {
      updateRosterInMemory(newCharacters);
      alert("Saved locally only. Add a GitHub PAT in Settings to commit.");
      return;
    }
  }

  setSyncStatus('Committing...');
  const result = await commitRosterFile('Update characters.json via WoW: Forever tracker [skip ci]',
    newCharacters, { compareBaseline: true });

  if (!result.ok) {
    console.error('GitHub push failed:', result.error, result.message || '');
    if (result.error === 'no-pat' || result.error === 'no-repo' || result.error === 'bad-repo') openGhModal();
    setSyncStatus('Commit failed', true);
    showToast(result.message || 'The commit did not land. Your roster was left unchanged.', 'error');
    return;
  }

  updateRosterInMemory(result.list);
  setSyncStatus(`Saved (${new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})})`);
}

export function openGhModal() {
  document.getElementById('gh-owner').value = ghConfig.owner || '';
  document.getElementById('gh-repo').value = ghConfig.repo || '';
  document.getElementById('gh-branch').value = ghConfig.branch || 'main';
  document.getElementById('gh-token').value = ghConfig.token || '';
  document.getElementById('gh-modal').classList.remove('hidden');
}

export function closeGhModal() {
  document.getElementById('gh-modal').classList.add('hidden');
}

export function saveGhConfig(e) {
  e.preventDefault();
  // A new token can be a different person. The review audit field is read back from GitHub and
  // cached for the session, so the cache has to die with the token it belongs to.
  resetGhLoginCache();
  // And a new owner/repo can be a different repository, whose sync workflow has a different id.
  resetSyncWorkflowCache();
  ghConfig = {
    owner: document.getElementById('gh-owner').value.trim(),
    repo: document.getElementById('gh-repo').value.trim(),
    branch: document.getElementById('gh-branch').value.trim() || 'main',
    token: document.getElementById('gh-token').value.trim()
  };
  localStorage.setItem('wow_gh_config', JSON.stringify(ghConfig));
  updateRepoDisplay();
  closeGhModal();
  pullFromGitHub();
}

// Inline on*="…" handlers resolve against the global scope.
window.pullFromGitHub = pullFromGitHub;
window.openGhModal = openGhModal;
window.closeGhModal = closeGhModal;
window.saveGhConfig = saveGhConfig;
