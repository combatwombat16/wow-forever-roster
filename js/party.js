// party.js — the 5-man dungeon simulator: slots, buff/synergy diagnostics, the
// auto-form picker and the filter-aware Auto-Form button label.
//
// `party` is owned here. editchar.js mutates it through dropPartyCharacter /
// syncPartyCharacter rather than reassigning the binding (ESM imports are read-only),
// and repaints go through state.js's renderAll() instead of calling roster.js back
// (that would close a cycle).

import { characters, currentFilter, getFilteredCharacters, onFilterChange, registerRenderHook, renderAll } from './state.js';
import { CLASS_ICONS_PATH, ensureSiteIconMap, siteIconMapLoaded, siteIconUrl } from './data.js';
import { CLASS_COLORS, escapeHtml, getFaction, ownString } from './util.js';

export let party = [null, null, null, null, null];

// Roster mutations that must keep the party slots consistent (delete / edit).
export function dropPartyCharacter(id) {
  party = party.map(p => (p && p.id === id ? null : p));
}

export function syncPartyCharacter(charData) {
  party = party.map(p => (p && p.id === charData.id ? { ...charData, assignedRole: p.assignedRole || charData.role } : p));
}

// The class icon beside a slotted name (P5.C), from the same lazily-loaded class-icons.json the
// distribution bars use (one request shared, data.js owns it). Nothing at all while the map is
// unloaded — the slot markup stays exactly what it was before the feature — and a reserved box
// once it is, so a name cannot shift between a class the map names and one it does not. The URL
// gate is the window-published isSafeIconUrl (the roster.js precedent: a panel module does not
// import detail.js for one check).
function partySlotIconHtml(cls) {
  if (!siteIconMapLoaded(CLASS_ICONS_PATH)) return '';
  const raw = siteIconUrl(CLASS_ICONS_PATH, cls);
  const url = raw !== '' && typeof window.isSafeIconUrl === 'function' && window.isSafeIconUrl(raw) ? raw : '';
  // The alt describes the IMAGE: the Battle.net class icon, not the roster word beside it.
  return `<span class="wow-card-icon">${url === '' ? ''
    : `<img src="${escapeHtml(url)}" loading="lazy" alt="${escapeHtml(`${cls} class icon`)}" class="wow-card-icon-img">`}</span>`;
}

export function renderParty() {
  // The slots are what ask for the class-icon map here (lazy, memoised, retryable in data.js;
  // party.js and stats.js share the one request).
  ensureSiteIconMap(CLASS_ICONS_PATH);
  const container = document.getElementById('party-slots');
  container.innerHTML = '';

  const slotLabels = ['Tank 🛡️', 'Healer 💚', 'DPS 1 ⚔️', 'DPS 2 ⚔️', 'DPS 3 ⚔️'];
  const slotRoles = ['Tank', 'Healer', 'DPS', 'DPS', 'DPS'];

  for (let i = 0; i < 5; i++) {
    const char = party[i];
    const slotEl = document.createElement('div');

    if (char) {
      const faction = getFaction(char.race);
      const factionBg = faction === 'alliance' 
        ? 'bg-gradient-to-b from-blue-950/60 to-slate-950/90 border-blue-900/60' 
        : 'bg-gradient-to-b from-red-950/60 to-slate-950/90 border-red-900/60';
      // Own-data lookup (see util.js): a prototype-shaped class falls back to neutral rather
      // than handing a function to `.split()`.
      const color = ownString(CLASS_COLORS, char.class) || 'text-slate-300';
      const isOffspec = char.assignedRole && char.assignedRole === char.roleSec;
      const slotIcon = partySlotIconHtml(char.class);

      slotEl.className = `${factionBg} border rounded-lg p-2.5 flex flex-col justify-between min-h-[95px] shadow-sm`;
      slotEl.innerHTML = `
        <div>
          <div class="text-[9px] font-bold text-slate-400 uppercase flex items-center justify-between">
            <span>${slotLabels[i]}</span>
            <div class="flex items-center gap-1">
              ${isOffspec ? '<span class="text-[8px] text-sky-300 font-semibold bg-sky-950 px-1 rounded border border-sky-800/40">Off-spec</span>' : ''}
              <span class="text-[8px] font-bold ${faction === 'alliance' ? 'text-sky-400' : 'text-red-400'}">${faction === 'alliance' ? 'Alliance' : 'Horde'}</span>
            </div>
          </div>
          <div class="flex items-center gap-1.5 mt-0.5">${slotIcon}<span class="font-bold text-sm text-slate-100 truncate">${escapeHtml(char.name)}</span></div>
          <div class="text-xs font-semibold ${color.split(' ')[0]}">${escapeHtml(char.class)}</div>
          <div class="text-[10px] text-slate-400">${escapeHtml(char.player)} • ${escapeHtml(char.race)}</div>
        </div>
        <button onclick="removeFromPartyIndex(${i})" class="text-[10px] text-red-400 hover:text-red-300 mt-1.5 text-left">Remove</button>
      `;
    } else {
      slotEl.className = 'bg-slate-950/80 border border-slate-800 rounded-lg p-2.5 flex flex-col justify-between min-h-[95px]';
      slotEl.innerHTML = `
        <div class="text-[9px] font-bold text-slate-500 uppercase">${slotLabels[i]}</div>
        <div class="text-xs text-slate-600 italic my-auto">Slot Empty</div>
        <span class="text-[9px] text-slate-600">Needs ${slotRoles[i]}</span>
      `;
    }
    container.appendChild(slotEl);
  }

  updateSynergyDiagnostics();
}

export function updateSynergyDiagnostics() {
  const activeClasses = party.filter(Boolean).map(c => c.class);
  const buffContainer = document.getElementById('buff-checklist');

  const synergies = [
    { name: "Bloodlust / Windfury", icon: "⚡", has: activeClasses.includes("Shaman"), desc: "Shaman utility" },
    { name: "PW: Fortitude", icon: "🛡️", has: activeClasses.includes("Priest"), desc: "+Stamina buff" },
    { name: "Arcane Intellect", icon: "📖", has: activeClasses.includes("Mage"), desc: "+Mana & Int" },
    { name: "Blessings & Auras", icon: "✨", has: activeClasses.includes("Paladin"), desc: "Paladin utility" },
    { name: "Mark of the Wild", icon: "🐾", has: activeClasses.includes("Druid"), desc: "+Stats & Resist" },
    { name: "Combat Res (Brez)", icon: "♻️", has: activeClasses.includes("Druid") || activeClasses.includes("Warlock"), desc: "Rebirth / Soulstone" },
    { name: "Crowd Control", icon: "🐑", has: activeClasses.includes("Mage") || activeClasses.includes("Rogue"), desc: "Polymorph / Sap" }
  ];

  let activeCount = 0;
  buffContainer.innerHTML = synergies.map(s => {
    if (s.has) activeCount++;
    return `
      <div class="rounded-lg p-1.5 transition-all flex flex-col justify-between border ${s.has ? 'bg-emerald-950/60 border-emerald-500/60 shadow-[0_0_8px_rgba(16,185,129,0.15)]' : 'bg-slate-950/50 border-slate-800/80 opacity-50'}">
        <div class="flex items-center justify-between gap-1">
          <span class="text-[11px] font-bold ${s.has ? 'text-emerald-200' : 'text-slate-400'} flex items-center gap-1 truncate">
            <span>${s.icon}</span> <span class="truncate">${s.name}</span>
          </span>
          <span class="text-[11px] font-black ${s.has ? 'text-emerald-400' : 'text-slate-600'}">${s.has ? '✓' : '✗'}</span>
        </div>
        <div class="text-[8px] text-slate-400 mt-0.5 truncate">${s.desc}</div>
      </div>
    `;
  }).join('');

  document.getElementById('synergy-score').textContent = `${activeCount} / ${synergies.length} Active`;

  const tankCount = party.filter(c => c && c.assignedRole === 'Tank').length;
  const healCount = party.filter(c => c && c.assignedRole === 'Healer').length;
  const dpsCount = party.filter(c => c && c.assignedRole === 'DPS').length;

  const statusEl = document.getElementById('trinity-status');
  if (tankCount === 1 && healCount === 1 && dpsCount === 3) {
    statusEl.className = 'ml-1 text-[11px] font-semibold px-2 py-0.5 rounded bg-emerald-950 border border-emerald-700/60 text-emerald-400';
    statusEl.innerText = 'Holy Trinity Complete! (1T / 1H / 3DPS)';
  } else {
    statusEl.className = 'ml-1 text-[11px] font-semibold px-2 py-0.5 rounded bg-slate-800/80 border border-slate-700 text-amber-400';
    statusEl.innerText = `Current: ${tankCount} Tank, ${healCount} Healer, ${dpsCount} DPS`;
  }
}

export function updateAutoFormButtonLabel() {
  const btn = document.getElementById('auto-form-btn');
  if (!btn) return;
  if (currentFilter === 'flowmasters') {
    btn.textContent = 'Auto-Form from "Flow"';
  } else if (currentFilter === 'family') {
    btn.textContent = 'Auto-Form from "Family"';
  } else if (currentFilter === 'alts') {
    btn.textContent = 'Auto-Form from "Alts"';
  } else {
    btn.textContent = 'Auto-Form from "All"';
  }
}

export function togglePartyMember(id) {
  const char = characters.find(c => c.id === id);
  if (!char) return;

  const existingIndex = party.findIndex(p => p && p.id === id);
  if (existingIndex !== -1) {
    party[existingIndex] = null;
    renderParty();
    renderAll();
    return;
  }

  if (char.role === 'Tank' && !party[0]) {
    party[0] = { ...char, assignedRole: 'Tank' };
  } else if (char.role === 'Healer' && !party[1]) {
    party[1] = { ...char, assignedRole: 'Healer' };
  } else if (char.roleSec === 'Tank' && !party[0]) {
    party[0] = { ...char, assignedRole: 'Tank' };
  } else if (char.roleSec === 'Healer' && !party[1]) {
    party[1] = { ...char, assignedRole: 'Healer' };
  } else {
    const emptyDps = [2, 3, 4].find(idx => !party[idx]);
    if (emptyDps !== undefined) {
      party[emptyDps] = { ...char, assignedRole: 'DPS' };
    } else {
      const firstEmpty = party.findIndex(p => p === null);
      if (firstEmpty !== -1) {
        party[firstEmpty] = { ...char, assignedRole: firstEmpty === 0 ? 'Tank' : firstEmpty === 1 ? 'Healer' : 'DPS' };
      } else {
        alert('5-man party is currently full! Remove a member first.');
      }
    }
  }

  renderParty();
  renderAll();
}

export function removeFromPartyIndex(index) {
  party[index] = null;
  renderParty();
  renderAll();
}

export function clearParty() {
  party = [null, null, null, null, null];
  renderParty();
  renderAll();
}

export function autoFillDungeonGroup() {
  const pool = getFilteredCharacters();

  if (pool.length === 0) {
    const groupName = currentFilter === 'all' ? 'All' : currentFilter.charAt(0).toUpperCase() + currentFilter.slice(1);
    alert(`No characters found in "${groupName}" to form a party!`);
    return;
  }

  party = [null, null, null, null, null];
  const usedIds = new Set();

  // Step 1: Tank Selection
  let tank = pool.find(c => c.role === 'Tank');
  if (!tank) {
    tank = pool.find(c => c.roleSec === 'Tank');
  }
  if (tank) {
    party[0] = { ...tank, assignedRole: 'Tank' };
    usedIds.add(tank.id);
  }

  // Step 2: Healer Selection
  let healer = pool.find(c => !usedIds.has(c.id) && c.role === 'Healer');
  if (!healer) {
    healer = pool.find(c => !usedIds.has(c.id) && c.roleSec === 'Healer');
  }
  if (healer) {
    party[1] = { ...healer, assignedRole: 'Healer' };
    usedIds.add(healer.id);
  }

  // Step 3: DPS Selection (Slots 2, 3, 4)
  for (let slot = 2; slot < 5; slot++) {
    let dps = pool.find(c => !usedIds.has(c.id) && c.role === 'DPS');
    if (!dps) {
      dps = pool.find(c => !usedIds.has(c.id) && c.roleSec === 'DPS');
    }
    if (!dps) {
      dps = pool.find(c => !usedIds.has(c.id));
    }

    if (dps) {
      party[slot] = { ...dps, assignedRole: 'DPS' };
      usedIds.add(dps.id);
    }
  }

  renderParty();
  renderAll();
}

registerRenderHook(renderParty);
onFilterChange(updateAutoFormButtonLabel);

// Inline on*="…" handlers (static markup plus the roster/party templates) resolve
// against the global scope.
window.togglePartyMember = togglePartyMember;
window.removeFromPartyIndex = removeFromPartyIndex;
window.clearParty = clearParty;
window.autoFillDungeonGroup = autoFillDungeonGroup;
