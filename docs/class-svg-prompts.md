# Class Silhouette Prompts — WoW:Forever Roster

Prompts for an SVG-generating LLM/agent, one per WoW:Forever class. Each produces a
full-body class figurine to use as the gear paper-doll background in the character
modal, in the style of an oversized classic WoW class icon.

**Formula:** `[Subject/Metaphor] - [Style] - [Color Palette] - [Complexity/Modifier] - [Technical Constraints]`

Each prompt below is self-contained and copy-pasteable as one paragraph. The
Technical Constraints clause repeats the shared doll contract verbatim so no prompt
depends on this header.

## The shared contract (why the constraints read the way they do)

- **Canvas:** 300×400 viewBox (3:4), centred on x=150. The doll box in `app.css` is
  420px tall with `background-size: contain`, so a 3:4 canvas maps its height axis
  exactly onto the socket positions at typical viewport widths.
- **Socket landmarks (as % of canvas height, from `assets/silhouettes/generic.svg`):**
  head-circle centre ≈ **11.5%**, hands/arm-tip ≈ **52%**, feet bottom ≈ **93.5%**;
  the whole figure spans ≈ **5.5% to 93.5%**. Item sockets are painted on top at
  Head 7%, Wrist/Hands 49%, Feet 92%, Ranged/Ammo row 93% — a head lower than ~15%
  or feet past ~94% puts sockets over empty air.
- **Readability:** the silhouette is a background layer. Item tooltips, slot borders
  and the render-image layer sit on top of it, so the fill must be flat and faint
  (fill-opacity ≈ 0.16 — the value `generic.svg` uses for its `#d4af37` body).
- **Palette source of truth:** the official class hexes in `js/util.js` `CLASS_COLORS`.
- **Drop-in path:** save as `assets/silhouettes/<class>.svg` (lowercase). The Pages
  workflow allow-list and `verify_sync_artifacts.py` already permit
  `assets/silhouettes/*.svg` and nothing wider.

---

## Warrior

> Full-body silhouette of an Orc warrior in a wide aggressive stance, cleaving a massive two-handed axe overhead, torso braced, cape flaring behind — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of warm bronze #C79C6E at fill-opacity 0.16 on a fully transparent background — complexity modifier: a low swirl of rage mist curling up around the boots and a few jagged anger motes rising off the shoulders, kept as simple closed shapes with no thin spicules — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with crown top near 5.5%, weapon overhead allowed to fill the band above, axe handle ends and open hands near 52% of height, feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/warrior.svg`. The overhead axe occupies the
band above the head — keep it light enough that the Head socket tooltip (7%) still
reads clearly over it.

---

## Paladin

> Full-body silhouette of a Human paladin in heavy plate, standing in solemn guardianship with a tall kite shield planted at the side and a hammer raised in benediction, tabard draping to the feet — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of rose gold #F58CBA at fill-opacity 0.16 on a fully transparent background — complexity modifier: a simple six-rayed halo ring floating behind the head and two soft curved light shafts framing the shoulders, all as clean geometric shapes — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with crown top near 5.5%, halo centred near 11.5% of height around the head, shield top and raised hammer hand near 52% of height, feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/paladin.svg`. The halo sits exactly on the
Head socket band (7–11.5%) — that is intentional; keep its rays out of x 120–180 if
the socket circle needs a clean centre.

---

## Hunter

> Full-body silhouette of a Night Elf hunter in lithe profile stance, drawing a longbow to full anchor with the trailing hand extended, one knee slightly bent as if mid-stride, quiver on the back, cloak lifting in wind — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of fern green #ABD473 at fill-opacity 0.16 on a fully transparent background — complexity modifier: three simple leaf-and-feather shapes drifting off the bowstring's path and a faint crescent moon arc behind the head, minimal and airy — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with crown and bow tips near 5.5%, anchor hand and bow ends near 52% of height, feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/hunter.svg`. The drawn bow pushes wide on x —
keep the vertical centreline clear enough for the Chest/Body socket column.

---

## Rogue

> Full-body silhouette of a Human rogue crouched in a stalking half-turn, twin daggers held low in reverse grip near the hips, hood drawn forward, body angled as if emerging from shadow — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of pale acrobat yellow #FFF569 at fill-opacity 0.16 on a fully transparent background — complexity modifier: two thin wisp curves of smoke dissolving off the shoulders and a simple curved slash arc behind the leading dagger, all tapering closed shapes — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with hooded crown near 5.5%, dagger hands near 52% of height, feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/rogue.svg`. #FFF569 is the brightest class
hex — resist the urge to raise opacity; 0.16 is what keeps gear slots readable.

---

## Priest

> Full-body silhouette of a Human priest in flowing robes and wide sleeves, head bowed in prayer with both hands raised and open at chest height, a holy book levitating between them — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of pure white #FFFFFF at fill-opacity 0.16 on a fully transparent background — complexity modifier: a radiant eight-ray sunburst nimbus centred behind the head and two long soft light rays descending past the sleeves, all as clean closed shapes — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with crown top near 5.5%, nimbus centred near 11.5% of height, raised hands near 52% of height, robe and feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/priest.svg`. Priests are the one class whose
robes can swallow the Wrist socket — keep sleeve ends no lower than 52%.

---

## Shaman

> Full-body silhouette of an Orc shaman standing squared and grounded, arms outstretched holding a double-headed totem staff planted before one foot, lightning rod crest on the shoulders, tusks and braided beard in profile mass — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of deep blue #0070DE at fill-opacity 0.16 on a fully transparent background — complexity modifier: one clean forked lightning bolt arcing from the staff head to the upper corner and two tiny spiral wind curls at the wrists, bold and geometric — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with crown and totem top near 5.5%, outstretched hands near 52% of height, feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/shaman.svg`. Deep blue reads faintest of the
nine on the dark panel; if it vanishes, prefer adding a second shape over raising
opacity past 0.2.

---

## Mage

> Full-body silhouette of a Human mage in tall pointed hat and long belted robes, one arm thrust skyward channeling a crackling orb, the other hand gathering at the waist, robes and hair streaming upward with arcane lift — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of arcane cyan #40C7EB at fill-opacity 0.16 on a fully transparent background — complexity modifier: a ring of five simple diamond motes orbiting the raised orb and two concentric arcane-circle arcs at the feet, pure geometry, no script or runes that read as text — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with hat tip and orb near 5.5%, gathering hand near 52% of height, robe hem and feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/mage.svg`. The feet arcane circle overlaps the
Feet 92% and Ranged 93% row — keep it as thin concentric rings so both sockets stay
legible inside it.

---

## Warlock

> Full-body silhouette of a Goblin-cursed human warlock hunched in a commanding crouch, one hand clutching a glowing soul shard at the chest, the other clawed outward, horned hood and billowing cloak pooling on the ground — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of purple #8787ED at fill-opacity 0.16 on a fully transparent background — complexity modifier: a single large unblinking eye disc floating behind the hood and three teardrop flame licks rising off the trailing cloak hem, closed shapes with smooth curves only — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum, no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with horned hood near 5.5%, shard hand and clawed hand near 52% of height, cloak pooling and feet bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/warlock.svg`. The pooling cloak may fill the
bottom band — that's fine, but keep the Feet socket area (92%) a smooth simple mass
with no internal detail.

---

## Druid

> Full-body silhouette of a Tauren druid standing calm and rooted, antlered helm crowned high, a gnarled living staff cradled in one forepaw, the other hand resting at the chest in a healing gesture, faint translucent bear-spirit shape rising behind the shoulders — flat minimalist vector class-icon style of classic World of Warcraft, bold readable outline shapes, heroic mythic proportions, no facial detail, no gradients used as shading — single flat fill of sunset orange #FF7D0A at fill-opacity 0.16 on a fully transparent background — complexity modifier: the bear-spirit behind the torso as one simplified ghosted shape at half the body's opacity, plus two single leaf spirals drifting from the staff crown — technical constraints: SVG with viewBox="0 0 300 400" centred on x=150, hand-authorable <path> geometry only, one or two flat fills at fill-opacity 0.16 maximum (the ghost spirit at ≤0.08), no gradients-as-shading, no filters, no masks, no <image>, no text, no external references, figure spanning y 5.5% to 93.5% with antler tips near 5.5%, cradled and chest hands near 52% of height, hooves bottoms at 93.5% of height so gear-doll sockets at Head 7%, Hands 49% and Feet 92% land on body or immediate aura, not empty canvas.

**Usage:** save as `assets/silhouettes/druid.svg`. The antlers are the tallest
headwear of the nine — they may crowd the Head socket, so keep the band directly
above the crown (7%) open between the tines.

---

## Verification checklist (after generating each SVG)

1. `viewBox="0 0 300 400"`, no width/height forcing, no raster content.
2. Landmarks hold: crown ≈5.5%, hands ≈52%, feet ≈93.5% of height.
3. Flat fills only, max fill-opacity 0.16 (druid spirit ≤0.08).
4. Save to `assets/silhouettes/<class>.svg`; run
   `python3 -m http.server 8000` and check the modal gear tab with a character of
   that class — item sockets must remain clearly visible over the figure.
