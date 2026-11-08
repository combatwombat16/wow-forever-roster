/**
 * tailwind.config.js — Phase 3a companion to the Tailwind Play CDN that index.html
 * still loads (the CDN stays until the Pages workflow builds CSS in Phase 3c).
 *
 * index.html has no inline `tailwind.config = { … }` block to mirror, so this file
 * declares what the app actually depends on: the scan targets (markup plus the JS
 * that injects class strings at runtime — the arbitrary hex utilities only get
 * emitted because they appear verbatim in js/util.js) and the class-colour palette
 * as named tokens.
 *
 * The hex values must stay in sync with CLASS_COLORS / CLASS_BAR in js/util.js;
 * verify_stats.mjs checks that pair for drift and will fail the build here first.
 */
module.exports = {
  content: ['index.html', './js/**/*.js'],
  theme: {
    extend: {
      colors: {
        // WoW class colours (js/util.js CLASS_COLORS / CLASS_BAR)
        warrior: '#C79C6E',
        paladin: '#F58CBA',
        hunter: '#ABD473',
        rogue: '#FFF569',
        priest: '#FFFFFF',
        shaman: '#0070DE',
        mage: '#40C7EB',
        warlock: '#8787ED',
        druid: '#FF7D0A',
      },
      fontFamily: {
        cinzel: ['Cinzel', 'serif'],
        inter: ['Inter', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
