// main.js — bootstrap. Imports every module (which registers its render hooks and
// publishes its inline handlers on window), then runs the same init sequence the old
// inline script ran: repo display, Auto-Form label, class list, first paint, and the
// roster fetch/pull chain.
//
// Import order matters: roster.js registers its render hook before party/professions/
// stats, so a repaint paints the grid first, exactly as renderRoster() used to.

import { renderAll } from './state.js';
import { loadRoster, loadSidecars } from './data.js';
import { ghConfig, pullFromGitHub, setSyncStatus, updateRepoDisplay } from './github.js';
import './roster.js';
import './party.js';
import './professions.js';
import './stats.js';
import { onRaceChange } from './editchar.js';
import { updateAutoFormButtonLabel } from './party.js';

updateRepoDisplay();
updateAutoFormButtonLabel();
onRaceChange();
renderAll();

// The Battle.net sidecars first (they only decorate the render, and loading them can
// never fail the page), then data/characters.json, then the Contents API pull when a
// repo is configured, so the pull (which knows the user's branch) always wins the last
// write. A failed fetch leaves the localStorage / DEFAULT_CHARACTERS seed from state.js
// in place, and a failed sidecar leaves the badges in their no-sidecar shape.
loadSidecars().then(() => loadRoster()).then(() => {
  if (ghConfig.owner && ghConfig.repo) {
    pullFromGitHub();
  } else {
    setSyncStatus('Local only', false);
  }
});
