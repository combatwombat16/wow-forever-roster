// github.js — repo status widgets, the PAT settings modal, and the Contents API
// pull/push for data/characters.json.

import { STORAGE_KEY, getCharacters, setCharacters, renderAll } from './state.js';
import { ROSTER_PATH, rosterCharactersFrom, serializeRoster, setFetchedWrapper } from './data.js';

export let ghConfig = JSON.parse(localStorage.getItem('wow_gh_config') || 'null') || {
  owner: '',
  repo: '',
  branch: 'main',
  token: ''
};

let currentSha = null;

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
  const url = `https://api.github.com/repos/${ghConfig.owner}/${ghConfig.repo}/contents/${ROSTER_PATH}?ref=${ghConfig.branch}`;
  const headers = { 'Accept': 'application/vnd.github.v3+json' };
  if (ghConfig.token) headers['Authorization'] = `token ${ghConfig.token}`;

  try {
    const res = await fetch(url, { headers });
    if (res.status === 404) {
      setSyncStatus('File missing', true);
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();
    currentSha = data.sha;
    const decoded = decodeURIComponent(escape(atob(data.content.replace(/\s/g, ''))));
    const doc = JSON.parse(decoded);
    const list = rosterCharactersFrom(doc);
    if (!list) throw new Error('no characters in data/characters.json');

    setFetchedWrapper(doc);
    setCharacters(list);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    setSyncStatus(`Live (${new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})})`);
    renderAll();
  } catch (err) {
    console.error('GitHub fetch failed:', err);
    setSyncStatus('Sync error', true);
  }
}

export async function pushToGitHub(newCharacters) {
  if (!ghConfig.owner || !ghConfig.repo) {
    setCharacters(newCharacters);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    renderAll();
    return;
  }

  if (!ghConfig.token) {
    const tokenPrompt = prompt("To commit changes to characters.json, enter your GitHub Personal Access Token (PAT):");
    if (tokenPrompt) {
      ghConfig.token = tokenPrompt.trim();
      localStorage.setItem('wow_gh_config', JSON.stringify(ghConfig));
    } else {
      setCharacters(newCharacters);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
      renderAll();
      alert("Saved locally only. Add a GitHub PAT in Settings to commit.");
      return;
    }
  }

  setSyncStatus('Committing...');
  const url = `https://api.github.com/repos/${ghConfig.owner}/${ghConfig.repo}/contents/${ROSTER_PATH}`;

  try {
    if (!currentSha) {
      const checkRes = await fetch(`${url}?ref=${ghConfig.branch}`, {
        headers: {
          'Accept': 'application/vnd.github.v3+json',
          'Authorization': `token ${ghConfig.token}`
        }
      });
      if (checkRes.ok) {
        const checkData = await checkRes.json();
        currentSha = checkData.sha;
      }
    }

    const jsonString = serializeRoster(newCharacters);
    const encodedContent = btoa(unescape(encodeURIComponent(jsonString)));

    const bodyData = {
      message: `Update characters.json via WoW: Forever tracker [skip ci]`,
      content: encodedContent,
      branch: ghConfig.branch
    };
    if (currentSha) bodyData.sha = currentSha;

    const putRes = await fetch(url, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': `token ${ghConfig.token}`
      },
      body: JSON.stringify(bodyData)
    });

    if (!putRes.ok) {
      const errData = await putRes.json();
      throw new Error(errData.message || `HTTP ${putRes.status}`);
    }

    const result = await putRes.json();
    currentSha = result.content.sha;
    setCharacters(newCharacters);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    setSyncStatus(`Saved (${new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})})`);
    renderAll();
  } catch (err) {
    console.error('GitHub push failed:', err);
    setCharacters(newCharacters);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getCharacters()));
    renderAll();
    alert(`Failed to commit to GitHub: ${err.message}. Saved locally.`);
    setSyncStatus('Commit failed', true);
  }
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
