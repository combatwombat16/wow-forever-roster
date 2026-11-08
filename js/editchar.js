// editchar.js — the Add/Edit Character modal: race→class filtering under Forever's
// rules, the form open/populate/close paths, save and delete (both committed through
// github.js so the local-only fallback stays in one place).

import { LIFECYCLE, characters, currentFilter, deriveLifecycleState, isLifecycleEditable } from './state.js';
import { byRosterId } from './data.js';
import { dropPartyCharacter, syncPartyCharacter } from './party.js';
import { pushToGitHub } from './github.js';
import { ownValue, showToast } from './util.js';

// The 6-space indentation of the race rows is load-bearing: verify_stats.mjs reads
// the canonical race list straight out of this table.
export const FOREVER_RACE_CLASSES = {
      "Human": ["Warrior", "Hunter", "Mage", "Rogue", "Priest", "Warlock", "Paladin"],
      "Dwarf": ["Warrior", "Hunter", "Rogue", "Priest", "Paladin", "Shaman"],
      "Night Elf": ["Warrior", "Hunter", "Rogue", "Priest", "Druid"],
      "Gnome": ["Warrior", "Mage", "Rogue", "Priest", "Warlock"],
      "Alliance Skyborne": ["Warrior", "Hunter", "Mage", "Rogue", "Druid"],
      "Orc": ["Warrior", "Hunter", "Mage", "Rogue", "Warlock", "Shaman"],
      "Undead": ["Warrior", "Mage", "Rogue", "Priest", "Warlock", "Paladin"],
      "Tauren": ["Warrior", "Hunter", "Druid", "Shaman"],
      "Troll": ["Warrior", "Hunter", "Mage", "Rogue", "Priest", "Warlock", "Shaman"],
      "Horde Skyborne": ["Warrior", "Hunter", "Rogue", "Druid", "Shaman"]
};

export function onRaceChange() {
  const race = document.getElementById('char-race').value;
  const classSelect = document.getElementById('char-class');
  // Own-data lookup: `race` is a form value populated from the roster, so an inherited key must
  // yield no class list at all rather than a member of Object.prototype (see util.js).
  const ownClasses = ownValue(FOREVER_RACE_CLASSES, race);
  const validClasses = Array.isArray(ownClasses) ? ownClasses : [];
  const currentSelected = classSelect.value;

  classSelect.innerHTML = '';
  validClasses.forEach(cls => {
    const opt = document.createElement('option');
    opt.value = cls;
    opt.textContent = cls;
    if (cls === currentSelected) opt.selected = true;
    classSelect.appendChild(opt);
  });
}

export function openModal() {
  document.getElementById('modal-title').innerText = 'Add Forever Character';
  document.getElementById('edit-id').value = '';
  document.getElementById('player-name').value = '';
  document.getElementById('char-name').value = '';
  document.getElementById('char-track').value = currentFilter !== 'all' ? currentFilter : 'flowmasters';
  document.getElementById('char-role').value = 'DPS';
  document.getElementById('char-role-sec').value = 'None';
  onRaceChange();
  document.getElementById('char-modal').classList.remove('hidden');
}

export function closeModal() {
  document.getElementById('char-modal').classList.add('hidden');
}

// The lifecycle gate, enforced on this side of the browser as well as on the button.
//
// A validated row is read-only by design (design §2), and the roster card already renders its
// ✎ disabled — but the form is still reachable (a repaint that predates the validation commit,
// a call from the console, a future entry point), and the GitHub Contents API this site commits
// through accepts a whole-file rewrite from any PAT. So the open path and the save path both
// check the derived state, and neither trusts the button that led here.
function lifecycleOf(character) {
  const { mapEntry } = byRosterId(character && character.id);
  return deriveLifecycleState(character, mapEntry);
}

function refuseIfValidated(character, action) {
  if (!character) return true;                     // nothing to edit, nothing to allow
  const { mapEntry } = byRosterId(character.id);
  if (isLifecycleEditable(character, mapEntry)) return true;
  showToast(`${action} refused: ${character.name} is validated. Re-open it for editing from its detail modal first.`, 'error');
  return false;
}

export function editCharacter(id) {
  const c = characters.find(x => x.id === id);
  if (!c) return;
  if (!refuseIfValidated(c, 'Edit')) return;
  document.getElementById('modal-title').innerText = 'Edit Forever Character';
  document.getElementById('edit-id').value = c.id;
  document.getElementById('player-name').value = c.player;
  document.getElementById('char-name').value = c.name;
  document.getElementById('char-track').value = c.track;
  document.getElementById('char-role').value = c.role;
  document.getElementById('char-role-sec').value = c.roleSec || 'None';
  document.getElementById('char-race').value = c.race;
  onRaceChange();
  document.getElementById('char-class').value = c.class;
  document.getElementById('char-prof1').value = c.prof1 || 'None';
  document.getElementById('char-prof2').value = c.prof2 || 'None';
  document.getElementById('char-modal').classList.remove('hidden');
}

export async function deleteCharacter(id) {
  const target = characters.find(x => x.id === id);
  if (!target) return;
  // Deleting a validated row is recoverable only by re-typing it and validating it again, so it
  // gets its own warning rather than the generic one. Deletion itself stays allowed: a row that
  // left the roster must be removable even while locked (design §2 locks editing, not pruning).
  const warning = lifecycleOf(target) === LIFECYCLE.VALIDATED
    ? 'This character is validated — deleting it means it will need re-creation and re-validation. Delete anyway?'
    : 'Delete this character from the roster?';
  if (confirm(warning)) {
    const updated = characters.filter(c => c.id !== id);
    dropPartyCharacter(id);
    await pushToGitHub(updated);
  }
}

export async function saveCharacter(e) {
  e.preventDefault();
  const editId = document.getElementById('edit-id').value;
  // Belt for the disabled ✎: an existing row that became validated since the form was opened
  // (a review commit from another tab, or the nightly landing) is not saved over. New rows are
  // always drafts, so only the edit path can cross the line.
  if (editId) {
    const existing = characters.find(x => x.id === editId);
    if (!refuseIfValidated(existing, 'Save')) {
      closeModal();
      return;
    }
  }
  const charData = {
    id: editId || Date.now().toString(),
    player: document.getElementById('player-name').value.trim(),
    name: document.getElementById('char-name').value.trim(),
    track: document.getElementById('char-track').value,
    role: document.getElementById('char-role').value,
    roleSec: document.getElementById('char-role-sec').value,
    race: document.getElementById('char-race').value,
    class: document.getElementById('char-class').value,
    prof1: document.getElementById('char-prof1').value,
    prof2: document.getElementById('char-prof2').value
  };

  let updated;
  if (editId) {
    updated = characters.map(c => c.id === editId ? charData : c);
    syncPartyCharacter(charData);
  } else {
    updated = [...characters, charData];
  }

  closeModal();
  await pushToGitHub(updated);
}

// Inline on*="…" handlers resolve against the global scope.
window.openModal = openModal;
window.closeModal = closeModal;
window.onRaceChange = onRaceChange;
window.editCharacter = editCharacter;
window.deleteCharacter = deleteCharacter;
window.saveCharacter = saveCharacter;
