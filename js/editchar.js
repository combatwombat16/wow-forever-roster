// editchar.js — the Add/Edit Character modal: race→class filtering under Forever's
// rules, the form open/populate/close paths, save and delete (both committed through
// github.js so the local-only fallback stays in one place).

import { characters, currentFilter } from './state.js';
import { dropPartyCharacter, syncPartyCharacter } from './party.js';
import { pushToGitHub } from './github.js';

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
  const validClasses = FOREVER_RACE_CLASSES[race] || [];
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

export function editCharacter(id) {
  const c = characters.find(x => x.id === id);
  if (!c) return;
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
  if (confirm('Delete this character from the roster?')) {
    const updated = characters.filter(c => c.id !== id);
    dropPartyCharacter(id);
    await pushToGitHub(updated);
  }
}

export async function saveCharacter(e) {
  e.preventDefault();
  const editId = document.getElementById('edit-id').value;
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
