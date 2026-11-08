# Battle.net "WoW: Forever" API — frozen Phase 1 contract

Produced by `tools/probe_bnet.py` against the live US API on **2026-11-08 (01:57 UTC)**.
Raw evidence: `docs/bnet-probe-raw.json` (same run). Every statement below carries the
HTTP status that was actually observed; anything not observed is explicitly marked
`not probed` or `UNVERIFIED`. Nothing here is inferred from retail docs.

Run totals: **115 requests / 300 budget, 1 token mint, 50.6 s**, statuses
`46 × 200`, `65 × 404`, `1 × 401`, `1 × 403`, `1 × 304`, no 429, no retries
(max attempts on any request: 1), zero warnings.

Two roster characters were live: **`prime-rib`** (Prime Rib, lvl 33) and
**`heal-yeah`** (Heal Yeah, lvl 16). All per-field paths below come from those two
payloads; where a datum was not present on them it says so instead of guessing.

Path notation is the raw-JSON notation: `a.b` object keys, `a[]` array element
(`a[len]` is the element count that the probe records alongside the first elements).
Fields typed `localized_string` are inline locale maps — read `.en_US` off them
(11–12 locales per map: `en_US, es_MX, pt_BR, de_DE, en_GB, es_ES, fr_FR, it_IT, ru_RU, ko_KR, zh_TW, zh_CN`).

---

## 1. Auth & limits (re-verified this run)

| Item | Observed |
|---|---|
| Token mint | `POST https://us.battle.net/oauth/token` — **200** — HTTP Basic `client_id:client_secret`, body `grant_type=client_credentials`, `Content-Type: application/x-www-form-urlencoded` |
| Token payload | `token_type: "bearer"`, `expires_in: 86399` (≈24 h), `scope: null`, token length 34. The token value is never written to disk or stdout. |
| Mints this run | 1 (initial). The 401 → re-mint path did not fire (`401` appeared only for the deliberate bogus-token control). |
| Data host | `https://us.api.blizzard.com`, `Authorization: Bearer <token>`, `Accept: application/json` |
| Rate limiting | No `429`, no `Retry-After`, and **no rate-limit headers on any of the 114 responses** — quota cannot be read from responses. Client-side pacing of 0.25 s/request was never stressed. |

Status semantics, all re-measured this run (`auth.control_semantics`):

| Control | Status | Body |
|---|---|---|
| Routed path, **no** token | **404** | empty (0 bytes) |
| Routed path, **bogus** token | **401** | empty (0 bytes) |
| Routed path, **valid** token, unknown character | **404** | `{"code":404,"type":"BLZWEBAPI00000404","detail":"Not Found"}` |
| Unrouted path, valid token (`/profile/wow/not-routed-xyz-123`) | **404** | empty (0 bytes) |
| Routed path, valid token, **unknown namespace** (`profile-forever-us-typo`) | **403** | `{"code":403,"type":"BLZWEBAPI00000403","detail":"Forbidden"}` |

Consequences for Phase 2:

* `404` cannot distinguish "route missing" from "character not found" from "character
  not sharing its profile"; the empty-vs-JSON body **is** a usable signal —
  a JSON `BLZWEBAPI00000404` body means the API routed the request and found nothing
  (the bogus-character control produced exactly it), an empty 0-byte 404 means the
  gateway never routed it.
* `403` = authenticated but the namespace is not valid — **new semantics**, not covered
  by the old 401-vs-404 rule of thumb.

---

## 2. Name resolution

Hypothesis under test: canonical form is `{name}-{surname}` (space → hyphen).

**Verdict: CONFIRMED for the separator, but the route is case-sensitive — the form is
`{lowercase(name)}-{lowercase(surname)}`.** For both characters that resolved, the
mixed-case hyphen form (`Prime-Rib`, `Heal-Yeah`), the no-space form (`PrimeRib`) and the
`%20` form all returned **404**; only the lowercase hyphen slug returned **200**.

Case-sensitivity was isolated on a *known-good* character (`case_sensitivity.verdict`,
same token, seconds apart — so not a transient):

| Character | `prime-rib` / `heal-yeah` | `PRIME-RIB` / `HEAL-YEAH` | `Prime-Rib` / `Heal-Yeah` |
|---|---|---|---|
| prime-rib | **200** | **404** | **404** |
| heal-yeah | **200** | **404** | **404** |

`slug` == `lowercase(name) + "-" + lowercase(surname)` is confirmed four ways: the two
profile self-hrefs, plus two independent guild-roster members
(`Chicken`/`Dinner` → `chicken-dinner`, `Jakes`/`Warrior` → `jakes-warrior`).

### Roster table (all 20 names from `characters.json`)

`space_to_hyphen` = `Foo-Bar`, `space_removed` = `FooBar`, `space_encoded` = `Foo%20Bar`,
`*_lower` = lowercased variant. Names that stop at 3 attempts are ones whose lowercase
variants duplicate an earlier candidate (`morc nmindy` etc.).

| # | Roster name | Result | Matched form | Attempts (form=status) |
|---|---|---|---|---|
| 1 | shiftybeef | not_found (404) | — | as_is=404 |
| 2 | pandamancer | not_found (404) | — | as_is=404 |
| 3 | combatwombat | not_found (404) | — | as_is=404 |
| 4 | morc nmindy | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 5 | **Prime Rib** | **200** | `space_to_hyphen_lower` → `prime-rib` | hyphen=404, removed=404, %20=404, **hyphen_lower=200** |
| 6 | Shock Nawe | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |
| 7 | Huntress | not_found (404) | — | as_is=404, lower=404 |
| 8 | stabby_mcgee | not_found (404) | — | as_is=404 |
| 9 | Seamus | not_found (404) | — | as_is=404, lower=404 |
| 10 | drood | not_found (404) | — | as_is=404 |
| 11 | SirNobel | not_found (404) | — | as_is=404, lower=404 |
| 12 | Tir Rheebeard | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |
| 13 | boomster | not_found (404) | — | as_is=404 |
| 14 | puritee | not_found (404) | — | as_is=404 |
| 15 | shewter | not_found (404) | — | as_is=404 |
| 16 | pewter | not_found (404) | — | as_is=404 |
| 17 | pokeystabbey | not_found (404) | — | as_is=404 |
| 18 | koalaman dingoh | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 19 | phatbastid gnomeater | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 20 | **Heal Yeah** | **200** | `space_to_hyphen_lower` → `heal-yeah` | hyphen=404, removed=404, %20=404, **hyphen_lower=200** |

Counts: 2 resolved / 18 not-found of 20. Note the roster actually has **7** spaced names,
not 6 (`morc nmindy`, `Prime Rib`, `Shock Nawe`, `Tir Rheebeard`, `koalaman dingoh`,
`phatbastid gnomeater`, `Heal Yeah`).

### Name/surname fields (from the two 200 payloads)

| Fact | Evidence |
|---|---|
| A `surname` field exists | `surname` = `"Rib"` / `"Yeah"` (top level) |
| `name` is the first token only | `name` = `"Prime"` / `"Heal"` |
| Display form with the space exists separately | `name_search` = `"Prime Rib"` / `"Heal Yeah"` |
| Canonical URL is the slug, not `name` | `_links.self.href` = `https://us.api.blizzard.com/profile/wow/character/prime-rib?namespace=profile-forever-us` |
| `slug` field | Present on every sub-resource payload as `character.slug` (`prime-rib`, `heal-yeah`) and on guild-roster members as `members[].character.slug`; **absent from the profile payload itself** |
| Realm | **No realm field anywhere** in the profile payload — Forever is realmless, confirmed |
| Collision behaviour across hidden realms | Not testable: there are no realms; not probed |

**18/20 404 is the headline finding.** Because the deliberately bogus character name
produces the identical JSON 404, this run cannot tell "member never opted in / never
logged in post-launch" apart from "wrong name form". The two characters that did answer
prove the route and the slug rule work.

---

## 3. Character endpoints and exact JSON paths

Base for all of these: `https://us.api.blizzard.com`. `{slug}` is the
**lowercase** `{name}-{surname}` form. Namespace for every row in this section:
`namespace=profile-forever-us`. "Linked?" = the profile payload carries an href for it,
so Phase 2 can follow the payload instead of building URLs.

| Endpoint (GET) | Status | Linked from profile via |
|---|---|---|
| `/profile/wow/character/{slug}?namespace=profile-forever-us` | **200** (2/2 attempted slugs) | — |
| `…/{slug}/equipment` | **200** | `equipment.href` |
| `…/{slug}/achievements` | **200** | `achievements.href` |
| `…/{slug}/achievements/statistics` | **200** | `achievements_statistics.href` |
| `…/{slug}/statistics` | **200** | `statistics.href` |
| `…/{slug}/specializations` | **200** | `specializations.href` |
| `…/{slug}/encounters` | **200** | `encounters.href` |
| `…/{slug}/encounters/raids` | **200** | `raids.href` (inside `encounters`) |
| `…/{slug}/encounters/dungeons` | **200** | `dungeons.href` (inside `encounters`) |
| `…/{slug}/mythic-keystone-profile` | **200** | `mythic_keystone_profile.href` |
| `…/{slug}/pvp-summary` | **200** | `pvp_summary.href` |
| `…/{slug}/character-media` | **200** | `media.href` |
| `…/{slug}/guild` | not probed (guild data is inline in the profile; the guild document lives elsewhere — §5) | `guild.key.href` |
| `…/{slug}/summary` | **404** (0-byte gateway 404 → route does not exist) | not linked |
| `…/{slug}/progression` | **404** (0-byte) | not linked |
| `…/{slug}/progression-raiders` | **404** (0-byte) | not linked |
| `…/{slug}/pvp` | **404** (0-byte) — only `pvp-summary` exists | not linked |
| `…/{slug}/mythic-keystone-profile/season-current` | **404** (0-byte) | not linked |

Also linked from the profile (routes exist; **not fetched** this run to stay in budget —
`titles.href`, `collections.href`, `appearance.href`, `reputations.href`, `quests.href`,
`professions.href`): status for these six is **not probed**.

### 3.1 Profile payload — `battlenet-schema: character`, revision 1

| Datum | JSON path | Observed value / note |
|---|---|---|
| Battle.net character id | `id` | `2404563`, `7744020` (small ints — **not** the roster's `id` strings) |
| First name | `name` | `Prime`, `Heal` |
| Surname | `surname` | `Rib`, `Yeah` |
| Display name | `name_search` | `Prime Rib`, `Heal Yeah` |
| Level | `level` | 33, 16 |
| Experience | `experience` | 6103, 6099 |
| Achievement points | `achievement_points` | 0, 0 |
| Average ilvl | `average_item_level` | 27, 6 |
| Equipped ilvl | `equipped_item_level` | 27, 6 |
| Last login | `last_login_timestamp` | ms epoch (`1794086077000`) — **no** `last_login_time`, **no** `last_updated_timestamp` in the body; the doc timestamp is the `Last-Modified` header |
| Guild name / id / slug | `guild.name`, `guild.id`, `guild.slug` | `Compound`/`142084`/`compound`; `Shadow Wizard Money Gang`/`5001`/`shadow-wizard-money-gang` |
| Guild document href | `guild.key.href` | `/data/wow/guild/{slug}?namespace=profile-forever-us` |
| Race | `race.id`, `race.name` (localized), `race.key.href` | 6 = Tauren; 5 = Undead |
| Class | `character_class.id`, `character_class.name` (localized), `character_class.key.href` | 1 = Warrior; 5 = Priest — note the key is **`character_class`**, not `playable_class` |
| Faction | `faction.type` | `HORDE` |
| Gender | `gender.type`, `gender.name` (localized) | `MALE` |
| Ruleset | `ruleset.id`, `ruleset.name` (localized), `ruleset.key.href` | both characters are on ruleset 1 = `PvP` |
| Remix flag | `is_remix` | `false` |
| Canonical self URL | `_links.self.href` | lowercase slug form, ready to store as the map key |
| Active spec name | **not present in the profile** — comes from `/specializations` (§3.4) | — |
| Per-gear list | **not present** — `/equipment` (§3.2) | — |

### 3.2 `/equipment` — `battlenet-schema: character-equipment`, revision 1

Container: `equipped_items[]` (18 items for `prime-rib`).

| Datum | JSON path | Observed |
|---|---|---|
| Slot label / enum | `equipped_items[].slot.name` (localized), `equipped_items[].slot.type` | `Neck` / `NECK`, `Shoulders` / `SHOULDER` |
| Item name | `equipped_items[].name` (localized) | `Roadwatcher's Confidence`, `Reliquary Mantle` |
| **Item level (ilvl)** | `equipped_items[].level.value` (+ `equipped_items[].level.display_string`) | 33 / `"Item Level 33"` — there is **no** `ilvl` field and **no** `equipped_item_quantity` field |
| Stack quantity | `equipped_items[].quantity` | 1 (stack count, not ilvl) |
| Item id / document href | `equipped_items[].item.id`, `equipped_items[].item.key.href` | href points into the version-pinned static ns |
| Quality | `equipped_items[].quality.type`, `equipped_items[].quality.name` (localized) | `UNCOMMON`, `RARE` |
| **Icon** | **no icon URL in this payload.** Only `equipped_items[].media.id` and `equipped_items[].media.key.href` → `GET /data/wow/media/item/{id}?namespace=<static ns>` (§6) | one extra request per distinct item |
| Inventory slot type (2nd enum) | `equipped_items[].inventory_type.type` | `NECK`, `SHOULDER` |
| Binding | `equipped_items[].binding.type`, `.name` | `ON_ACQUIRE` |
| Stats | `equipped_items[].stats[].type.type`, `equipped_items[].stats[].value`, `equipped_items[].stats[].display.display_string` | `STAMINA` 12 → `+12 Stamina` |
| Armor | `equipped_items[].armor.value` | 171 |
| Required level | `equipped_items[].requirements.level.value` | 28 |
| Durability | `equipped_items[].durability.value` | 55 |
| Sell price | `equipped_items[].sell_price.value` | 3276 (copper) |
| Context (source) | `equipped_items[].context` | 11, 1 |
| Character back-reference | `character.name`, `character.surname`, `character.slug`, `character.id` | `prime-rib` |

Average/equipped ilvl do **not** need this endpoint: the profile already carries
`average_item_level` and `equipped_item_level`.

### 3.3 Achievements

`/achievements` — `battlenet-schema: character-achievement-summary`, revision 1:

| Datum | JSON path | Observed |
|---|---|---|
| **Achievement points** | `total_points` | 0 (both characters) |
| Achievements completed | `total_quantity` | 3 |
| Achievements list | `achievements[]` |
| Per-achievement | `achievements[].id`, `achievements[].achievement.id`, `achievements[].achievement.name` (plain string here, **not** a locale map), `achievements[].achievement.key.href` | `Novice Warrior`, `Journeyman Blacksmith` |
| Criteria / progress | `achievements[].criteria.id`, `achievements[].criteria.is_completed`, `achievements[].criteria.child_criteria[].amount`, `achievements[].criteria.child_criteria[].is_completed` | present |
| Completed time | `achievements[].completed_timestamp` | ms epoch |
| Per-category rollup | `category_progress[].category.id`, `category_progress[].category.name` (plain string), `category_progress[].quantity`, `category_progress[].points` | Adventure 2, Classes 1 |
| Recent unlocks | `recent_events[].achievement.name`, `recent_events[].timestamp` | present |

`/achievements/statistics` — `battlenet-schema: character-achievement-statistics`,
revision 1: `categories[].id`, `categories[].name`,
`categories[].id`, `categories[].name`,
`categories[].sub_categories[].statistics[].id`,
`categories[].sub_categories[].statistics[].name` (localized),
`categories[].sub_categories[].statistics[].quantity` (float),
`categories[].sub_categories[].statistics[].description` (localized),
`categories[].sub_categories[].statistics[].last_updated_timestamp` (ms).

### 3.4 `/specializations` — `character-specializations`, revision 1 — **this is the active-spec source**

| Datum | JSON path | Observed |
|---|---|---|
| Active spec flag | `specialization_groups[].is_active` | `true` (one group per character) |
| Active spec name | `specialization_groups[].specializations[].specialization_name` (localized) | `Protection` (prime-rib), `Discipline` (heal-yeah) |
| Spec id / href | `specialization_groups[].specializations[].specialization.id`, `…specialization.key.href` | 257, 613 |
| Talent points | `specialization_groups[].specializations[].spent_points` | 24, 4 |
| Talents | `specialization_groups[].specializations[].talents[].talent_rank`, `.talent`, `.spell_tooltip` | present |

### 3.5 `/statistics` — `character-statistics`, revision 2 — combat stats, **not** raid progress

`health` (1447), `power` (100), `power_type.name` (Rage), `strength.base`,
`strength.effective`, `agility.base`, `intellect.base`, `stamina.effective`,
`speed.value`, `melee_crit.value`, `melee_haste.value`, `mastery.value`,
`bonus_armor` (168), `lifesteal.value`, `versatility`, plus `*.rating_bonus` /
`*.rating_normalized` siblings. No raid, keystone or PvP fields in this payload.

---

## 4. Tier-2 verdict (raid progress / M+ / PvP)

| Surface | Route status | Data observed? | Verdict for Phase 2 |
|---|---|---|---|
| **Raid progress** | `/encounters` **200** (schema `character-raid-progression`), `/encounters/raids` **200** (schema `character-raid-progression-raids`), `/encounters/dungeons` **200** (schema `character-raid-progression-dungeons`) | **No data**: the `raids`/`dungeons` bodies contained only `_links` + `character.*` — zero raid/encounter entries; the parent `encounters` body only carries `raids.href` and `dungeons.href` | **Route exists, content empty for our sample.** Both characters are level 33 / 16, which is very plausibly why. Retail `/progression` and `/progression-raiders` are **404** (0-byte, gateway) — do not use them. Phase 2: fetch `encounters/raids`, treat "no raid keys" as "no raid progress", and re-probe on a levelled character before shipping a raid UI. |
| **Mythic+** | `/mythic-keystone-profile` **200** (schema `mythic-keystone-profile`) | **No keystone or score data**: body was only `current_period.period.id` = `1088` + `current_period.period.key.href` + `character.*`. No `mythic_keystone_score`, no `keystone_latest/deserialized`, no `season` fields observed. | **Route exists, per-character M+ numbers not present for our sample.** `/mythic-keystone-profile/season-current` is **404**. The period document the payload advertises (`/data/wow/mythic-keystone/period/1088?namespace=dynamic-forever-us`) is **404**, and it is also **404** when re-requested in the static ns; the period **index** (`/data/wow/mythic-keystone/period/index?namespace=dynamic-forever-us`) is **404** too → **the keystone period dictionary is unresolvable for Forever right now**. Phase 2 must not depend on season labels. |
| **PvP** | `/pvp-summary` **200** (schema `pvp-summary`) | `honorable_kills` (11 / 0) and `pvp_rank` (0 / 0). **No rating, no bracket, no season fields.** | **Route exists with a thin payload.** `/pvp` is **404**. Phase 2 can ship honorable kills + rank now; ratings must stay optional until seen. |
| Guild surfaces (bonus) | `/data/wow/guild/{slug}` **200**, `/roster` **200**, `/activity` **200**, `/achievements` **200** | see §7 — roster members are enumerated | Usable, see mapping strategy |

**Net Tier-2 answer:** the *routes* for M+ and PvP are live and the raid route family is
`encounters/*` (not `progression*`), but with only two low-level visible characters the
probe captured **no** populated raid/M+/PvP numbers. Any Phase 3 UI section for these
must render from "missing" and be re-validated once more members opt in.

---

## 5. Namespaces (dynamic, static, and re-discovery)

| Namespace | Working URLs (observed) | Dead URLs (observed) |
|---|---|---|
| `profile-forever-us` | All `/profile/wow/character/...` rows in §3; `/data/wow/guild/{slug}`, `/data/wow/guild/{slug}/roster`, `/activity`, `/achievements` | `/data/wow/character-search/index` → **404** (re-confirmed: there is no character-search API) |
| `dynamic-forever-us` | `/data/wow/ruleset/index` **200** (`rulesets[]`: 4 entries, `rulesets[].id`, `rulesets[].key.href`); `/data/wow/region/index` **200** (`regions[]`: exactly **1** region, `regions[0].href` → `/data/wow/region/91`) | `/data/wow/playable-class/index` → **404**; `/data/wow/realm/index` → **404** (Forever has no realms); `/data/wow/mythic-keystone/period/index` → **404**; `/data/wow/mythic-keystone/period/1088` → **404** |
| `static-1.60.1_70399-forever-us` | `/data/wow/playable-class/index` **200** (`classes[].id`, `classes[].name`: **9** classes), `/data/wow/playable-race/index` **200** (`races[].id`, `races[].name`: **10** races), `/data/wow/playable-class/{id}` **200**, `/data/wow/playable-race/{id}` **200**, `/data/wow/playable-specialization/{id}` **200**, `/data/wow/achievement-category/{id}` **200**, `/data/wow/item/{id}` **200 for 15459 but 404 for 14025**, `/data/wow/media/item/{id}` **200** | unpinned `static-forever-us`: **not probed this run** (a static ns was always found; the earlier session recorded 403 for it — treat as UNVERIFIED) |

`/data/wow/playable-class/index?namespace=dynamic-forever-us` being **404** is new: the
class/race dictionaries must be read from the **static** namespace, not dynamic.

**Observed current static namespace: `static-1.60.1_70399-forever-us`**
(25 href occurrences in this run; it is the only candidate seen).

Re-discovery at runtime (never hardcode — Phase 2 must do this every run):

1. GET any character profile (`/profile/wow/character/{slug}?namespace=profile-forever-us`).
2. Regex over the body: `namespace=(static-[A-Za-z0-9._-]+)`.
   Real examples in one payload: `race.key.href`, `character_class.key.href`,
   `equipment.href` → not static, but `equipped_items[].item.key.href` and
   `specialization_groups[].specializations[].specialization.key.href` are.
3. Use the captured value for every `playable_*`, `item`, `media/item`,
   `achievement-category` request. `set namespace=` on the href, do not string-build.

Localized strings: class/race/spec docs do **not** use a "name object carrying an href"
localized-string indirection — the names are inline locale maps (`name`, `gender_name.male`,
`gender_name.female`, `playable_classes[].name`, `specializations[].name`), so read
`.en_US`. The requested "resolve the localized-name href for every class/race in the roster"
step is
therefore unnecessary; class/race names are already in the profile payload. Roster-relevant
cross-check: the static index returns **9 classes and 10 races**, matching the nine
`CLASS_COLORS` and ten `FOREVER_RACE_CLASSES` keys in `index.html`.

---

## 6. Media / icon URL forms observed

| Kind | Documented source path | URL form observed |
|---|---|---|
| Item icon | `GET /data/wow/media/item/{item_id}?namespace=<static ns>` → `assets[].key` = `icon`, `assets[].value`, `assets[].file_data_id` | `https://render.worldofwarcraft.com/forever-us/icons/56/{file_data_id}.jpg` (e.g. `…/icons/56/132601.jpg`) |
| Character render | `GET …/character/{slug}/character-media?namespace=profile-forever-us` → `assets[].key` ∈ `main-raw`, `main-sd-raw`, … ; URL is in `assets[].value` (**not** an `href`) | `https://render.worldofwarcraft.com/forever-us/character/{raceId}/{characterId}-main-raw.png` (e.g. `…/character/211/2404563-main-raw.png`) |
| Spec / class art | `media.key.href` on the static docs → `…/data/wow/media/playable-specialization/{id}?namespace=<static ns>` | not fetched (not needed; budget) |

Hosts seen: `render.worldofwarcraft.com` (5 distinct render/icon URLs) and
`us.api.blizzard.com` (API hrefs only). Note the Forever render path segment
`/forever-us/` — retail's `render.us.blizzard.com` forms never appeared.
The `…/icons/56/` shard directory is Blizzard's own; treat the URL as opaque and store it
verbatim. These are hotlinkable image URLs (approved third-party image source); Phase 3
must still not add any CDN *script*, only `<img>` sources.

Cost note: the equipment payload has **no** inline icon, so icons cost
`1 request per distinct item` (18 for one geared character) unless Phase 2 caches the
`item_id → icon URL` map across runs. Those media docs ship
`Cache-Control: public, max-age=86400` and item ids are stable, so a persistent map
collapses the steady-state cost to ~0.

---

## 7. Implications for Phase 2

1. **Normalization rule (hard requirement):** `slug = lowercase(roster_name).replace(" ", "-")`,
   URL-encoded. Mixed case returns 404 (§2). This is the single most important Phase 2 line.
2. **Mapping strategy seed.**
   * Step 1: direct GET with the normalized slug. 200 → store
     `_links.self.href`, `id`, `name`, `surname`, `name_search`; status `ok`.
   * Step 2 (the new lever): every resolved character exposes `guild.slug` and
     `guild.key.href`. `GET /data/wow/guild/{slug}/roster?namespace=profile-forever-us`
     returned **200** with **142** members for `compound`, each carrying
     `members[].character.slug`, `.name`, `.surname`, `.id`, `.level`,
     `.playable_class.id`, `.playable_race.id`, `.faction.type`, `.key.href` and
     `members[].rank`. Matching roster rows against guild rosters by
     `(lowercase(name), lowercase(surname))` resolves members even when the direct name
     guess is ambiguous, and it disambiguates duplicates. Guild slugs are only discoverable
     from a resolved member (there is no guild search), so this needs ≥1 resolved character
     per guild; this run discovered two guilds: `compound` (142 members) and
     `shadow-wizard-money-gang`.
   * Step 3: anything still unmatched → `not_found`, never guessed (unchanged policy).
   * Privacy caveat: a public sidecar must **not** embed the fetched guild roster;
     use it in the sync job to resolve ids, emit only per-character data.
3. **ETag verdict: no ETags.** 0 of 114 responses (all 46 200s included) carried `ETag`,
   so `If-None-Match` is untestable and useless here — the probe recorded
   `If-None-Match: attempted=false, reason="no response in this run carried an ETag header"`.
   **`Last-Modified` is on every 200 response** and `If-Modified-Since` revalidation was
   proven end-to-end: re-GETting `prime-rib` with the captured timestamp returned
   **304 with a 0-byte body**. Phase 2 should store `Last-Modified` per document and send
   `If-Modified-Since`; note the granularity is 1 second and the timestamps are per
   document (profile/equipment/achievements shared `Sat, 7 Nov 2026 21:14:37 GMT`;
   `character-media` `Sat, 7 Nov 2026 15:01:17 GMT`; guild doc `Sat, 7 Nov 2026 23:21:49 GMT`).
4. **`Cache-Control` observed per surface** (useful for picking sync cadence):
   character profile and all `…/character/{slug}/*` docs → **no `Cache-Control` header**;
   `/data/wow/guild/*` → `public, max-age=10800`; static reference docs
   (`playable-class`, `playable-race`, `achievement-category`, `media/item`) →
   `public, max-age=86400`; `/data/wow/ruleset/index` → `public, max-age=30`.
5. **Schema drift tripwire:** every response echoes `battlenet-schema` and
   `battlenet-schema-revision` (observed: `character/1`, `character-equipment/1`,
   `character-achievement-summary/1`, `character-achievement-statistics/1`,
   `character-statistics/2`, `character-specializations/1`, `character-raid-progression/1`,
   `character-raid-progression-raids/1`, `character-raid-progression-dungeons/1`,
   `mythic-keystone-profile/1`, `pvp-summary/1`, `character-media/1`, `media/2`,
   `item/…`, `playable-class/1`, `playable-race/2`, `playable-specialization/…`,
   `achievement-category/2`, `guild/3`, `ruleset/1`, `region/…`). Log them in the sync job;
   a change means the field tables above need re-probing.
6. **Budget planning:** a full nightly for 20 mapped characters ≈ 20 profiles +
   20 × 8 sub-docs (equipment, achievements, achievements/statistics, statistics,
   specializations, encounters/raids, pvp-summary, mythic-keystone-profile) + guild docs.
   Item icons add ~18 requests per character **on first sight only** (§6), so the steady
   state is ~200 requests/run — inside the 36 000/h, 100/s account limit, and this run
   measured ≈0.44 s per request round trip including the 0.25 s client pause.
7. **Freshness:** `last_login_timestamp` (ms) is the only in-body freshness signal; the
   body has no `last_updated_timestamp`. Modal wording should say data updates when the
   character logs out, and can show `last_login_timestamp` plus the doc's `Last-Modified`.
8. **Timestamps are epoch **milliseconds**, not seconds** (`1794086077000`).
9. **Localized strings:** read `.en_US` from every locale map. Exceptions where the name is
   already a plain string: `achievements[].achievement.name`,
   `category_progress[].category.name`, `guild.name`, `name`, `surname`, `name_search`.
10. **Do not build realm logic.** `realm` never appears in a Forever payload;
    `/data/wow/realm/index` is 404; guild hrefs are realmless
    (`/data/wow/guild/{slug}`).

---

## 8. Known instabilities and caveats

* **18 of 20 roster names 404.** A deliberately bogus name yields the byte-identical JSON
  404, so a 404 cannot be attributed to opt-in vs. login vs. naming. Re-run the probe after
  members opt in and log out; treat the map as provisional.
* **Case-sensitive slug lookup contradicts the usual "Blizzard names are case-insensitive"
  assumption.** If Blizzard later makes it case-insensitive the probe still passes (the
  lowercase form keeps working), but nothing may rely on a mixed-case slug.
* **Blizzard's own hrefs are not always fetchable.** The keystone period href served inside
  a 200 body 404s in the namespace the href itself names *and* in the static namespace;
  `/data/wow/item/14025` 404s while `/data/wow/item/15459` 200s in the same static ns.
  Phase 2 must treat any single reference-doc 404 as non-fatal.
* **`dynamic-forever-us` is half-populated:** ruleset and region indexes work, class/race
  and keystone-period indexes do not.
* **No rate-limit headers** and no `Retry-After` observed, so back-off logic can only react
  to a 429 (untested path this run: 0 occurrences) — keep the code but note it is unproven.
* **No ETags** (§7.3): conditional GETs work only via `If-Modified-Since`, at 1-second
  granularity, and a 304 body is empty (0 bytes) — do not attempt to JSON-parse it.
* **Tier-2 payloads are empty for the only two visible characters**; the routes being 200
  is *not* proof that raid/M+/PvP numbers will render — see §4.
* **Possible stale roster data:** the API reports `heal-yeah` as `race.id` 5 = **Undead**
  while `characters.json` lists `Troll` (class matches: Priest). `prime-rib` matches
  (Tauren / Warrior). Both resolved characters are on ruleset 1 = `PvP`. Flag to the
  tracker owner; do not hand-edit `characters.json`.
* **Guild rosters are public** (142 members fetched with an unauthenticated-scope
  client-credentials token): keep them out of the committed sidecar.
* Endpoint surface is undocumented and version-pinned; the probe keeps every path and
  namespace in one config block at the top of `tools/probe_bnet.py` — Phase 2 should mirror
  that and re-run this probe after each game patch.
* Single-token names were each tried in `as_is` and lowercase only; because no single-token
  character resolved, "single-token lowercase always works" is extrapolated from the
  two resolved spaced names, **not** directly verified for e.g. `shiftybeef`.
