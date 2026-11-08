# Battle.net "WoW: Forever" API — frozen Phase 1 contract

Produced by `tools/probe_bnet.py` against the live US API on **2026-11-08 (02:30 UTC)**.
Raw evidence: `docs/bnet-probe-raw.json` (same run). Every statement below carries the
HTTP status that was actually observed; anything not observed is explicitly marked
`not probed` or `UNVERIFIED`. Nothing here is inferred from retail docs.

**This run supersedes the 01:57 UTC run.** The tracker pushed a new `characters.json`
between the two runs (surnames added to most rows, `Heal Yeah` → `Heal Naw`, new names
`Hallowed Bonez` / `Vlinder Stormveil` / `Voidlight Glows`, `Shock Nawe` / `Seamus` /
`drood` / `SirNobel` / `morc nmindy` gone). The two characters that resolved last time
(`prime-rib`, `heal-yeah`) are **no longer in the roster**, so nothing in this document is
carried over from them; every payload fact below comes from this run's four profiles.

Run totals: **159 requests / 300 budget, 1 token mint, 70.4 s**, statuses
`66 × 200`, `89 × 404`, `1 × 401`, `1 × 403`, `1 × 304`, no 429, no retries
(max attempts on any request: 1), zero warnings. 159 requests = 158 GETs recorded plus the
token POST.

Four roster characters are live: **`shiftybeef-stroganoff`** (Shiftybeef Stroganoff, lvl 20,
Druid), **`morc-theorc`** (Morc Theorc, lvl 13, Hunter), **`beef-pho`** (Beef Pho, lvl 19,
Warrior) and **`voidlight-glows`** (Voidlight Glows, lvl 14, Paladin). All per-field paths
below come from those payloads; where a datum was not present on them it says so instead of
guessing.

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
| Rate limiting | No `429`, no `Retry-After`, and **no rate-limit headers on any of the 158 responses** — quota cannot be read from responses. Client-side pacing of 0.25 s/request was never stressed. |
| Token lifetime header | `blizzard-token-expires` is present in the union of observed header names (the probe records header names, not this value, so its format is `not probed`). Do not rely on it without measuring it. |

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
  a JSON `BLZWEBAPI00000404` body (60 bytes, `Content-Type: application/json;charset=UTF-8`,
  plus a `battlenet-namespace` echo header) means the API routed the request and found
  nothing, an empty 0-byte 404 with **no** namespace header means the gateway never routed it.
* `403` = authenticated but the namespace is not valid — **new semantics**, not covered
  by the old 401-vs-404 rule of thumb.

---

## 2. Name resolution

Hypothesis under test: canonical form is `{name}-{surname}` (space → hyphen).

**Verdict: CONFIRMED for the separator, and the route is still case-sensitive — the form is
`{lowercase(name)}-{lowercase(surname)}`.** All four resolved names had to be lowercased
before they answered, and this run proves it on a *known-good* character instead of
inferring it: `Beef-Pho` and `Voidlight-Glows` returned **404** on the first attempt and
**200** on the `*_lower` attempt seconds later. The two names that resolved on the first
try (`shiftybeef stroganoff`, `morc theorc`) were already written lowercase in the roster,
so they are consistent with — not evidence against — the rule.

Case-sensitivity, re-isolated on all four resolved slugs (`case_sensitivity.verdict`,
same token, seconds apart):

| Slug | lowercase | UPPERCASE | Titlecase |
|---|---|---|---|
| `shiftybeef-stroganoff` | **200** | **404** | **404** |
| `morc-theorc` | **200** | **404** | **404** |
| `beef-pho` | **200** | **404** | **404** |
| `voidlight-glows` | **200** | **404** | **404** |

Every one of those 8 negative variants carries the JSON `BLZWEBAPI00000404` body, i.e. the
route exists and the differently-cased slug simply is not a character — this is a
case-sensitivity result, not a routing failure.

`slug` == `lowercase(name) + "-" + lowercase(surname)` is now confirmed six ways: the four
profile self-hrefs above, plus two guild-roster members whose `members[].character.slug`
values (`shiftybeef-stroganoff`, `beef-pho`) match the profile slugs byte for byte.

### Roster table (all 20 entries from `characters.json`, one row per entry)

`space_to_hyphen` = `Foo-Bar`, `space_removed` = `FooBar`, `space_encoded` = `Foo%20Bar`,
`*_lower` = lowercased variant. Names that stop at 3 attempts are ones whose lowercase
variants duplicate an earlier candidate (`pandamancer emony` etc.). `#` and `id` are the
position and the tracker id in `characters.json` — the roster, not this document, is the
keying authority.

| # | Roster id | Roster name | Result | Matched form | Attempts (form=status) |
|---|---|---|---|---|---|
| 1 | 1791919748959 | **shiftybeef stroganoff** | **200** | `space_to_hyphen` → `shiftybeef-stroganoff` | **hyphen=200** |
| 2 | 1791919870010 | pandamancer emony | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 3 | 1791919950805 | combatwombat talion | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 4 | 1791922047617 | **morc theorc** | **200** | `space_to_hyphen` → `morc-theorc` | **hyphen=200** |
| 5 | 1791928493125 | **Beef Pho** | **200** | `space_to_hyphen_lower` → `beef-pho` | hyphen=404, removed=404, %20=404, **hyphen_lower=200** |
| 6 | 1791928548067 | Hallowed Bonez | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |
| 7 | 1791928613248 | Huntress | not_found (404) | — | as_is=404, lower=404 |
| 8 | 1791928676823 | stabby_mcgee | not_found (404) | — | as_is=404 |
| 9 | 1791928771432 | Tir Rheebeard ⚠ dup | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |
| 10 | 1791928813281 | Vlinder Stormveil | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |
| 11 | 1791928886528 | **Voidlight Glows** | **200** | `space_to_hyphen_lower` → `voidlight-glows` | hyphen=404, removed=404, %20=404, **hyphen_lower=200** |
| 12 | 1791928967366 | Tir Rheebeard ⚠ dup | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |
| 13 | 1791929013125 | boomster | not_found (404) | — | as_is=404 |
| 14 | 1791929106142 | puritee | not_found (404) | — | as_is=404 |
| 15 | 1791929283993 | shewter | not_found (404) | — | as_is=404 |
| 16 | 1791929450071 | pewter | not_found (404) | — | as_is=404 |
| 17 | 1791929660455 | pokeystabbey | not_found (404) | — | as_is=404 |
| 18 | 1791945705052 | koalaman dingoh | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 19 | 1791945790634 | phatbastid gnomeater | not_found (404) | — | hyphen=404, removed=404, %20=404 |
| 20 | 1793073514718 | Heal Naw | not_found (404) | — | hyphen=404, removed=404, %20=404, hyphen_lower=404, removed_lower=404 |

Counts: **4 resolved / 16 not-found of 20 entries**; `spaced_names` 13, single-token 7
(`Huntress`, `stabby_mcgee`, `boomster`, `puritee`, `shewter`, `pewter`, `pokeystabbey`).
Contrary to the assumption that most of the roster is spaced, **13 of 20** entries contain a
space, not 16. All 51 profile-lookup 404s carried the JSON `BLZWEBAPI00000404` body (0 of
them were 0-byte gateway 404s), so every one of these names was routed and answered
"no such character".

### Duplicate-mapping case: `Tir Rheebeard` (Phase 2 needs_review evidence)

`characters.json` holds **two** entries with the identical name `Tir Rheebeard` — ids
`1791928771432` (NahBrah, track `family`, role Tank) and `1791928967366` (NahBrah, track
`alts`, role DPS). The probe was changed this run (see §8) so that duplicates are **not**
de-duplicated: both entries were probed independently and both rows are in the table above
(#9 and #12).

* Both entries generated the **identical candidate list** (same five slugs, same order,
  byte-identical URLs) and the **identical outcome**: `404` on all five, i.e. 10 requests
  bought zero discriminating information.
* **No slug was observed for either entry**, so the feared "two ids → one slug" collision
  could not be confirmed or refuted this run. What *is* proven: name lookup cannot separate
  them, and any Phase 2 map keyed on the normalized name would silently collapse these two
  ids into one row.
* Normalizing the whole roster shows `tir-rheebeard` is the **only** slug collision in
  `characters.json`; the other 18 names are unique.
* Neither entry appears in the only guild roster this run reached (`flowmasters`,
  `member_count` 4), so the guild-roster lever (§7.2) also cannot disambiguate them today.
* Phase 2 rule: key the map by roster `id`, never by name/slug; if two ids resolve to the
  same slug (or both fail identically), emit `needs_review` with both ids rather than
  picking one.

### Name/surname fields (from the four 200 payloads)

| Fact | Evidence |
|---|---|
| A `surname` field exists | `surname` = `"Stroganoff"` / `"Theorc"` / `"Pho"` / `"Glows"` (top level) |
| `name` is the first token only | `name` = `"Shiftybeef"` / `"Morc"` / `"Beef"` / `"Voidlight"` |
| Display form with the space exists separately | `name_search` = `"Shiftybeef Stroganoff"` / `"Morc Theorc"` / `"Beef Pho"` / `"Voidlight Glows"` |
| Canonical URL is the slug, not `name` | `_links.self.href` = `https://us.api.blizzard.com/profile/wow/character/beef-pho?namespace=profile-forever-us` |
| `slug` field | Present on every sub-resource payload as `character.slug` and on guild-roster members as `members[].character.slug`; **absent from the profile payload itself** |
| Realm | **No realm field anywhere** in any payload — Forever is realmless, re-confirmed |
| Collision behaviour across hidden realms | Not testable: there are no realms; not probed |

**16/20 404 is still the headline finding.** Because the deliberately bogus character name
produces the identical JSON 404, this run cannot tell "member never opted in / never logged
in post-launch" apart from "wrong name form". The four characters that did answer prove the
route and the slug rule work.

---

## 3. Character endpoints and exact JSON paths

Base for these: `https://us.api.blizzard.com`. `{slug}` is the **lowercase**
`{name}-{surname}` form. Namespace for every row in this section:
`namespace=profile-forever-us`. "Linked?" = the profile payload carries an href for it, so
Phase 2 can follow the payload instead of building URLs. Statuses are for the **four**
resolved characters unless a narrower count is given.

| Endpoint (GET) | Status | Linked from profile via |
|---|---|---|
| `/profile/wow/character/{slug}?namespace=profile-forever-us` | **200** (4/4 resolved slugs) | — |
| `…/{slug}/equipment` | **200** (4/4) | `equipment.href` |
| `…/{slug}/achievements` | **200** (4/4) | `achievements.href` |
| `…/{slug}/achievements/statistics` | **200** (2/4 — probe per-kind cap; not probed for `beef-pho`, `voidlight-glows`) | `achievements_statistics.href` |
| `…/{slug}/statistics` | **200** (4/4) | `statistics.href` |
| `…/{slug}/specializations` | **200** (2/4 — probe cap; not probed for `beef-pho`, `voidlight-glows`) | `specializations.href` |
| `…/{slug}/encounters` | **200** (4/4) | `encounters.href` |
| `…/{slug}/encounters/raids` | **200** (4/4) | `raids.href` (inside the `/encounters` payload) |
| `…/{slug}/encounters/dungeons` | **200** (4/4) | `dungeons.href` (inside the `/encounters` payload) |
| `…/{slug}/mythic-keystone-profile` | **200** (4/4) | `mythic_keystone_profile.href` |
| `…/{slug}/pvp-summary` | **200** (4/4) | `pvp_summary.href` |
| `…/{slug}/character-media` | **200** (2/4 — probe cap; not probed for `beef-pho`, `voidlight-glows`) | `media.href` |
| `/data/wow/guild/{slug}` (and `/roster`, `/activity`, `/achievements`) | **200** each, for `flowmasters` | `guild.key.href` |
| `…/{slug}/guild` | not probed (guild data is inline in the profile; the guild document was fetched at `/data/wow/guild/{slug}` instead) | `guild.key.href` |
| `…/{slug}/hunter-pets` | not probed (href only exists on the Hunter profile) | `hunter_pets.href` (present on `morc-theorc` only) |
| `…/{slug}/summary` | **404** ×4 — JSON body → routed, no such resource | not linked |
| `…/{slug}/progression` | **404** ×4 — JSON body | not linked |
| `…/{slug}/progression-raiders` | **404** ×4 — JSON body | not linked |
| `…/{slug}/pvp` | **404** ×4 — JSON body; only `pvp-summary` exists | not linked |
| `…/{slug}/mythic-keystone-profile/season-current` | **404** ×4 — **0-byte gateway 404**, no namespace header → the route does not exist | not linked |

Also linked from the profile (routes exist; **not fetched** this run to stay in budget —
`titles.href`, `collections.href`, `appearance.href`, `reputations.href`, `quests.href`,
`professions.href`): status for these six is **not probed**.

### 3.1 Profile payload — `battlenet-schema: character`, revision 1

| Datum | JSON path | Observed value / note |
|---|---|---|
| Battle.net character id | `id` | `249157`, `1382436`, `877365`, `18824944` (small ints — **not** the roster's `id` strings) |
| First name | `name` | `Shiftybeef`, `Morc`, `Beef`, `Voidlight` |
| Surname | `surname` | `Stroganoff`, `Theorc`, `Pho`, `Glows` |
| Display name | `name_search` | `Shiftybeef Stroganoff`, `Morc Theorc`, `Beef Pho`, `Voidlight Glows` |
| Level | `level` | 20, 13, 19, 14 |
| Experience | `experience` | 15131, 9316, 10261, 7679 |
| Achievement points | `achievement_points` | 0 on all four |
| Average ilvl | `average_item_level` | 13, 4, 12, 4 |
| Equipped ilvl | `equipped_item_level` | 13, 4, 12, 4 |
| Last login | `last_login_timestamp` | ms epoch (`1794089740000`, `1793994897000`, `1794090097000`, `1794102264000`) — **no** `last_login_time`, **no** `last_updated_timestamp` in the body; the doc timestamp is the `Last-Modified` header |
| Guild name / id / slug | `guild.name`, `guild.id`, `guild.slug` | `Flowmasters` / `246033` / `flowmasters` on 3 of 4; **`morc-theorc` has no `guild` key at all** → guildless characters simply omit it, so Phase 2 must treat the whole object as optional |
| Guild faction | `guild.faction.type` | `HORDE` |
| Guild document href | `guild.key.href` | `/data/wow/guild/{slug}?namespace=profile-forever-us` |
| Race | `race.id`, `race.name` (localized), `race.key.href` | 6 = Tauren, 2 = Orc, 6 = Tauren, 5 = Undead |
| Class | `character_class.id`, `character_class.name` (localized), `character_class.key.href` | 11 = Druid, 3 = Hunter, 1 = Warrior, 2 = Paladin — note the key is **`character_class`**, not `playable_class` |
| Faction | `faction.type` | `HORDE` (all four) |
| Gender | `gender.type`, `gender.name` (localized) | `MALE` (all four) |
| Ruleset | `ruleset.id`, `ruleset.name` (localized), `ruleset.key.href` | all four are ruleset `2` = `Normal` (last run's two characters were ruleset `1` = `PvP`, so the value is real per-character data, not a constant) |
| Remix flag | `is_remix` | `false` (all four) |
| Hunter-only extra surface | `hunter_pets.href` | present only on `morc-theorc`; route content not probed |
| Canonical self URL | `_links.self.href` | lowercase slug form, ready to store as the map key |
| Active spec name | **not present in the profile** — comes from `/specializations` (§3.4) | — |
| Per-gear list | **not present** — `/equipment` (§3.2) | — |

### 3.2 `/equipment` — `battlenet-schema: character-equipment`, revision 1

Container: `equipped_items[]` — **12 items** (`shiftybeef-stroganoff`), **11**
(`morc-theorc`), **13** (`beef-pho`), **9** (`voidlight-glows`).

| Datum | JSON path | Observed |
|---|---|---|
| Slot label / enum | `equipped_items[].slot.name` (localized), `equipped_items[].slot.type` | `Shoulders` / `SHOULDER`, `Shirt` / `SHIRT` |
| Item name | `equipped_items[].name` (localized) | `Talbar Mantle`, `Deckhand's Shirt` |
| **Item level (ilvl)** | `equipped_items[].level.value` (+ `equipped_items[].level.display_string`) | 26 / `"Item Level 26"` — there is **no** `ilvl` field and **no** `equipped_item_quantity` field |
| Stack quantity | `equipped_items[].quantity` | 1 (stack count, not ilvl) |
| Item id / document href | `equipped_items[].item.id`, `equipped_items[].item.key.href` | href points into the version-pinned static ns |
| Quality | `equipped_items[].quality.type`, `equipped_items[].quality.name` (localized) | `UNCOMMON`, `COMMON` |
| **Icon** | **no icon URL in this payload.** Only `equipped_items[].media.id` and `equipped_items[].media.key.href` → `GET /data/wow/media/item/{id}?namespace=<static ns>` (§6) | one extra request per distinct item |
| Item class (new this run) | `equipped_items[].item_class.id`, `equipped_items[].item_class.name` (localized), `equipped_items[].item_class.key.href` | 4 = `Armor` |
| Item subclass (new this run) | `equipped_items[].item_subclass.id`, `equipped_items[].item_subclass.name` (localized), `equipped_items[].item_subclass.key.href` | 1 = `Cloth`, 0 = `Miscellaneous`; href is `/data/wow/item-class/{classId}/item-subclass/{subId}` |
| Subclass-hidden flag | `equipped_items[].is_subclass_hidden` | `true` on the shirt |
| Inventory slot type (2nd enum) | `equipped_items[].inventory_type.type`, `equipped_items[].inventory_type.name` | `SHOULDER`, `BODY` |
| **Enchant (new this run)** | `equipped_items[].enchantments[].enchantment_id`, `…enchantment_slot.id`, `…enchantment_slot.type`, `…source_item.id`, `…source_item.name` (localized), `…source_item.key.href`, `…display_string` (localized) | seen on 1 item of `morc-theorc` only: `enchantment_id` 41, slot `PERMANENT`, source item `273459` = `Enchant Chest - Inferior Stamina`, display `Enchanted: Stamina +2`. Absent from every other equipped item in the sample → optional field, and `…/source_item.key.href` points at `/data/wow/item/{id}` which 404s for some ids (§8) |
| Binding | `equipped_items[].binding.type`, `.name` | `ON_ACQUIRE` |
| Stats | `equipped_items[].stats[].type.type`, `equipped_items[].stats[].value`, `equipped_items[].stats[].display.display_string` | `INTELLECT` 4 → `+4 Intellect` |
| Armor | `equipped_items[].armor.value`, `equipped_items[].armor.display.display_string` | 30 → `30 Armor` |
| Required level | `equipped_items[].requirements.level.value`, `…display_string` | 17 → `Requires Level 17` (only on items that have a requirement) |
| Durability | `equipped_items[].durability.value`, `…display_string` | 38 → `Durability 38 / 40` |
| Sell price | `equipped_items[].sell_price.value` (copper) + `equipped_items[].sell_price.display_strings.header` / `…gold` / `…silver` / `…copper` | 920 → `0` gold `9` silver `20` copper |
| Colour of displayed stat text | `equipped_items[].stats[].display.color.r` / `.g` / `.b` / `.a` | 255/255/255/1.0 (also on `equipped_items[].armor.display.color.*`) |
| Context (source) | `equipped_items[].context` | 11, 1 |
| Character back-reference | `character.name`, `character.surname`, `character.slug`, `character.id` | e.g. `beef-pho` |

Average/equipped ilvl do **not** need this endpoint: the profile already carries
`average_item_level` and `equipped_item_level`.

### 3.3 Achievements

`/achievements` — `battlenet-schema: character-achievement-summary`, revision 1:

| Datum | JSON path | Observed |
|---|---|---|
| **Achievement points** | `total_points` | 0 (all four) |
| Achievements completed | `total_quantity` | 1, 1, 0, 1 |
| Achievements list | `achievements[]` | lengths 11, 9, 9, 7 |
| Per-achievement | `achievements[].id`, `achievements[].achievement.id`, `achievements[].achievement.name` (plain string here, **not** a locale map), `achievements[].achievement.key.href` | `Journeyman Leatherworker`, `Expert Blacksmith`, `Master of Warsong Gulch` |
| Criteria / progress | `achievements[].criteria.id`, `achievements[].criteria.is_completed`, `achievements[].criteria.child_criteria[].id`, `…child_criteria[].amount`, `…child_criteria[].is_completed` | present on all four (`is_completed` false on the first two rows of every character) |
| Completed time | **not observed this run.** The previous run reported a per-achievement completion timestamp; none of the four current payloads carries one, so that claim is withdrawn — do not plan a "completed on" column until it is seen again | — |
| Per-category rollup | `category_progress[].category.id`, `category_progress[].category.name` (plain string), `category_progress[].category.key.href`, `category_progress[].quantity`, `category_progress[].points` | present on 3 of 4; **absent for `beef-pho`, whose `total_quantity` is 0** → the array is optional, guard for a missing key |
| Recent unlocks | `recent_events[].achievement.id`, `recent_events[].achievement.name`, `recent_events[].achievement.key.href`, `recent_events[].timestamp` | present on 3 of 4 (`Explore Moonglade`, `Explore Durotar`, `Explore Tirisfal Glades`) |
| Statistics href | `statistics.href` | → `/achievements/statistics` |

`/achievements/statistics` — `battlenet-schema: character-achievement-statistics`,
revision 1 (fetched for `shiftybeef-stroganoff` and `morc-theorc`):
`categories[len]` 7 and 8; `categories[].id`, `categories[].name` (localized),
`categories[].statistics[].id` / `…name` (localized) / `…quantity` (float) /
`…last_updated_timestamp` (ms), and the same four fields one level deeper as
`categories[].sub_categories[].id`, `categories[].sub_categories[].name`,
`categories[].sub_categories[].statistics[].id` / `.name` / `.quantity` /
`.last_updated_timestamp`. `categories[].sub_categories[].statistics[].description`
(localized) appears when the statistic names an item ("Bandage used most" → `Linen Bandage`).
**This payload now carries the only populated raid/encounter-style numbers in the run — see §4.**

### 3.4 `/specializations` — `character-specializations`, revision 1 — **this is the active-spec source**

| Datum | JSON path | Observed |
|---|---|---|
| Active spec flag | `specialization_groups[].is_active` | `true` (one group per character, `specialization_groups[len]` = 1 on both fetched payloads) |
| Active spec name | `specialization_groups[].specializations[].specialization_name` (localized) | `Feral Combat` (`shiftybeef-stroganoff`); **not observed for `morc-theorc`** |
| Spec id / href | `specialization_groups[].specializations[].specialization.id`, `…specialization.key.href` | 134, → `/data/wow/playable-specialization/{id}` in the static ns |
| Talent points | `specialization_groups[].specializations[].spent_points` | 11 |
| Talents | `specialization_groups[].specializations[].talents[].talent_rank`, `.talent`, `.spell_tooltip` | present, ranks 3 and 5 in the first two entries |

**New this run:** the Hunter payload (`morc-theorc`) has `specialization_groups[len]` = 1 and
`specialization_groups[0].is_active` = `true` but **no specializations array inside the
group** — a character that has not picked a spec. Phase 2 must read the active spec as
optional and render "no spec chosen" instead of assuming index 0 exists.

### 3.5 `/statistics` — `character-statistics`, revision 2 — combat stats, **not** raid progress

Fetched for all four. `health` (759 / 370 / 675 / 302), `power` (749 / 331 / 100 / 375),
`power_type.id`, `power_type.name` (localized: `Mana`, `Rage`), `power_type.key.href`,
`strength.base` / `.effective`, `agility.base` / `.effective`, `intellect.base` / `.effective`,
`stamina.base` / `.effective`, `spirit.base` / `.effective`, `armor.base` / `.effective`,
`defense.base` / `.effective`, `attack_power`, `ranged_attack_power`, `main_hand_damage_min` /
`_max` / `main_hand_dps` / `main_hand_speed`, `off_hand_damage_min` / `_max` / `off_hand_dps` /
`off_hand_expertise` / `off_hand_speed`, `spell_power`, `spell_healing`, `mana_regen`,
`mana_regen_combat`, `movement_speed`, `bonus_armor` (64 / 1 / 0 / 0), `armor_penetration`,
`spell_penetration`, `versatility`, `versatility_damage_done_bonus`,
`versatility_damage_taken_bonus`, `versatility_healing_done_bonus`, and the five
resistances as `<type>.base` / `.effective` (`arcane`, `fire`, `frost`, `nature`, `shadow`).
Rating-style values come in triples — `dodge.value` / `dodge.rating_bonus` /
`dodge.rating_normalized`, and the same for `parry`, `block`, `avoidance`, `lifesteal`,
`mastery`, `speed`, `melee_crit`, `melee_haste`, `melee_hit`, `ranged_crit`, `ranged_haste`,
`ranged_hit`, `off_hand_hit`, `spell_crit`, `spell_haste`, `spell_hit`.
No raid, keystone or PvP fields in this payload.

---

## 4. Tier-2 verdict (raid progress / M+ / PvP)

| Surface | Route status | Data observed? | Verdict for Phase 2 |
|---|---|---|---|
| **Raid progress** | `/encounters` **200** ×4 (schema `character-raid-progression`), `/encounters/raids` **200** ×4 (schema `character-raid-progression-raids`), `/encounters/dungeons` **200** ×4 (schema `character-raid-progression-dungeons`) | **Still no raid/encounter entries.** All four `/encounters/raids` bodies were only `_links.self.href` + `character.*`; all four `/encounters/dungeons` bodies were only `_links.self.href`; the parent `encounters` bodies carry just `raids.href` and `dungeons.href`. Highest level in the sample is 20. | **Route exists, content empty for every sampled character.** Retail `/progression` and `/progression-raiders` are **404** with a JSON body (routed, no such resource) — do not use them. Phase 2: fetch `encounters/raids`, treat "no raid keys" as "no raid progress", and re-probe on a levelled character before shipping a raid UI. |
| **Boss kills (new this run)** | `/achievements/statistics` **200** (2 of 4, probe cap) | **Populated.** `categories[]` entry id 14807 `Dungeons & Raids` → `categories[].sub_categories[]` id 14821 `Boss Kills` → `categories[].sub_categories[].statistics[].name` = `Mutanus the Devourer kills (Wailing Caverns)` with `statistics[0].quantity` = 1.0 for `shiftybeef-stroganoff`, and `Bazzalan kills (Ragefire Chasm)` = 1.0 for `morc-theorc`; the flat `categories[].statistics[]` sibling carries `Total 5-player dungeons entered` = 3.0 / 1.0 | **This is the only encounter-shaped data the API gives for low-level characters.** Read boss kills as name/quantity pairs from `categories[].sub_categories[].statistics[]` (the statistic *id* is boss-specific, e.g. 15028, so match on `name`, do not hardcode ids). It is a per-kill counter, not a lock state — it does not replace `encounters/raids`. |
| **Mythic+** | `/mythic-keystone-profile` **200** ×4 (schema `mythic-keystone-profile`) | **No keystone or score data**: every body was only `current_period.period.id` = `1088` + `current_period.period.key.href` + `character.*`. No `mythic_keystone_score`, no `keystone_latest/deserialized`, no `season` fields observed. | **Route exists, per-character M+ numbers not present for our sample.** `/mythic-keystone-profile/season-current` is **404 (0-byte gateway)** → the route does not exist. The period document the payload advertises (`/data/wow/mythic-keystone/period/1088?namespace=dynamic-forever-us`) is **404**, and it is also **404** when re-requested in the static ns; the period **index** is **404** too → **the keystone period dictionary is unresolvable for Forever right now**. Phase 2 must not depend on season labels. |
| **PvP** | `/pvp-summary` **200** ×4 (schema `pvp-summary`) | `honorable_kills` = 0 / 0 / 0 / **34** (`voidlight-glows`) and `pvp_rank` = 0 on all four. **No rating, no bracket, no season fields.** | **Route exists with a thin payload; honorable kills are real data now.** `/pvp` is **404** (JSON body). Phase 2 can ship honorable kills + rank; ratings must stay optional until seen. |
| Guild surfaces (bonus) | `/data/wow/guild/{slug}` **200** (schema `guild/3`), `/roster` **200** (`guild-roster/1`), `/activity` **200** (`guild-activity/1`), `/achievements` **200** (`guild-achievement-summary/1`) | roster enumerated for `flowmasters` — see §7.2; `/activity` and `/achievements` bodies carried only `guild.*` + `_links` (no entries) | Usable, see mapping strategy |

**Net Tier-2 answer:** the *routes* for M+ and PvP are live and the raid route family is
`encounters/*` (not `progression*`), but with four low-level (13–20) visible characters the
probe still captured **no** populated raid-lock, keystone-score or PvP-rating numbers. The
one new populated surface is per-boss kill counters inside `/achievements/statistics`. Any
Phase 3 UI section for raid/M+/PvP must render from "missing" and be re-validated once more
members opt in and level up.

---

## 5. Namespaces (dynamic, static, and re-discovery)

| Namespace | Working URLs (observed) | Dead URLs (observed) |
|---|---|---|
| `profile-forever-us` | All `/profile/wow/character/...` rows in §3; `/data/wow/guild/{slug}`, `/data/wow/guild/{slug}/roster`, `/activity`, `/achievements` | `/data/wow/character-search/index` → **404** (0-byte → not routed; there is no character-search API) |
| `dynamic-forever-us` | `/data/wow/ruleset/index` **200** (`rulesets[len]` = 4; `rulesets[].id`, `rulesets[].key.href`; first two ids 1 and 15), `/data/wow/region/index` **200** (`regions[len]` = 1, `regions[].href` → `/data/wow/region/91`, schema `region/1`) | `/data/wow/playable-class/index` → **404**; `/data/wow/realm/index` → **404** (Forever has no realms); `/data/wow/mythic-keystone/period/index` → **404**; `/data/wow/mythic-keystone/period/1088` → **404** |
| `static-1.60.1_70399-forever-us` | `/data/wow/playable-class/index` **200** (`classes[len]` = **9**, `classes[].id`, `classes[].name`), `/data/wow/playable-race/index` **200** (`races[len]` = **10**, `races[].id`, `races[].name` — first two are 95 `High Order Skyborne`, 96 `Windshaper Skyborne`), `/data/wow/playable-class/{1,2,3,11}` **200**, `/data/wow/playable-race/{2,5,6}` **200**, `/data/wow/playable-specialization/134` **200** (schema `playable-specialization/2`), `/data/wow/achievement-category/{14777,14778}` **200**, `/data/wow/item/127` **200** (schema `item/1`) but **404 for 10657**, `/data/wow/media/item/10657` **200** (schema `media/2`) | unpinned `static-forever-us`: **not probed this run** (a static ns was always found; the earlier session recorded 403 for it — treat as UNVERIFIED) |

`/data/wow/playable-class/index?namespace=dynamic-forever-us` being **404** still holds: the
class/race dictionaries must be read from the **static** namespace, not dynamic.

**Observed current static namespace: `static-1.60.1_70399-forever-us`**
(36 href occurrences in this run; it is the only candidate seen, and it is the same
version pin as the 01:57 run, so no patch landed between the two runs).

Re-discovery at runtime (never hardcode — Phase 2 must do this every run):

1. GET any character profile (`/profile/wow/character/{slug}?namespace=profile-forever-us`).
2. Regex over the body: `namespace=(static-[A-Za-z0-9._-]+)`.
   Real examples in one payload: `race.key.href`, `character_class.key.href`,
   `guild.key.href` → not static, but `equipped_items[].item.key.href`,
   `equipped_items[].media.key.href` and
   `specialization_groups[].specializations[].specialization.key.href` are.
3. Use the captured value for every `playable_*`, `item`, `media/item`,
   `achievement-category` request. `set namespace=` on the href, do not string-build.

Localized strings: class/race/spec docs do **not** use a "name object carrying an href"
localized-string indirection — the names are inline locale maps (`name`, `gender.name`,
`classes[].name`, `races[].name`), so read `.en_US`. Roster-relevant cross-check: the static
index returns **9 classes and 10 races**, matching the nine `CLASS_COLORS` and ten
`FOREVER_RACE_CLASSES` keys in `index.html` — including the Skyborne races that three
unresolved roster entries list as `Horde Skyborne`.

---

## 6. Media / icon URL forms observed

| Kind | Documented source path | URL form observed |
|---|---|---|
| Item icon | `GET /data/wow/media/item/{item_id}?namespace=<static ns>` → `assets[len]` 1, `assets[].key` = `icon`, `assets[].value`, `assets[].file_data_id`, plus `id` | `https://render.worldofwarcraft.com/forever-us/icons/56/{file_data_id}.jpg` — observed `…/icons/56/135040.jpg` for item 10657 (`file_data_id` 135040) |
| Character render | `GET …/character/{slug}/character-media?namespace=profile-forever-us` → `assets[len]` = 4, `assets[].key` ∈ `main-raw`, `main-sd-raw` (only the first two keys are captured in the inventory), `assets[].value` (**not** an `href`) | `https://render.worldofwarcraft.com/forever-us/character/{raceId}/{characterId}-main-raw.png` — observed `…/character/69/249157-main-raw.png` (Tauren druid) and `…/character/36/1382436-main-raw.png` (Orc hunter) |
| Spec / class art | `media.key.href` on the static docs → `…/data/wow/media/playable-specialization/{id}?namespace=<static ns>` | not fetched (not needed; budget) → **not probed** |

Hosts seen: `render.worldofwarcraft.com` (5 distinct render/icon URLs) and
`us.api.blizzard.com` (API hrefs only). Note the Forever render path segment
`/forever-us/` — retail's `render.us.blizzard.com` forms never appeared. The path embeds the
**race id** and the **Battle.net character id**, not the slug, so a render URL cannot be
built from a roster name alone — it must come from `character-media`. The `…/icons/56/`
shard directory is Blizzard's own; treat the URL as opaque and store it verbatim. These are
hotlinkable image URLs (approved third-party image source); Phase 3 must still not add any
CDN *script*, only `<img>` sources.

Cost note: the equipment payload has **no** inline icon, so icons cost
`1 request per distinct item` (9–13 per character in this run) unless Phase 2 caches the
`item_id → icon URL` map across runs. Those media docs ship
`Cache-Control: public, max-age=86400` and item ids are stable, so a persistent map
collapses the steady-state cost to ~0.

---

## 7. Implications for Phase 2

1. **Normalization rule (hard requirement):** `slug = lowercase(roster_name).replace(" ", "-")`,
   URL-encoded. Mixed case returns 404 (§2) — proven on four characters this run, and the
   two roster names that hit on the first try were already lowercase. This is the single most
   important Phase 2 line.
2. **Mapping strategy seed.**
   * Step 1: direct GET with the normalized slug. 200 → store
     `_links.self.href`, `id`, `name`, `surname`, `name_search`; status `ok`.
   * Step 2 (the new lever): every gilded character exposes `guild.slug` and
     `guild.key.href`. `GET /data/wow/guild/{slug}/roster?namespace=profile-forever-us`
     returned **200** with **4** members for `flowmasters`, each carrying
     `members[].character.slug`, `.name`, `.surname`, `.id`, `.level`,
     `.playable_class.id`, `.playable_race.id`, `.faction.type`, `.key.href` and
     `members[].rank`. Matching roster rows against guild rosters by
     `(lowercase(name), lowercase(surname))` resolves members even when the direct name
     guess is ambiguous. Caveats observed: (a) **`morc-theorc` has no `guild` key**, so a
     guildless member contributes nothing; (b) the roster contains members that are **not**
     in our tracker (`healy-bones`, id 10740225) and the guild doc's `member_count` (4) can
     disagree with the resolved-name count, so match on name pairs, never on counts;
     (c) `members[].character.level` can lag the profile (roster said 18 for `beef-pho`
     while its profile said 19) — the guild roster is cached `max-age=10800`;
     (d) guild slugs are only discoverable from a resolved member (there is no guild
     search), so this needs ≥1 resolved character per guild; this run discovered exactly one
     guild, `flowmasters` (id 246033).
   * Step 3: anything still unmatched → `not_found`, never guessed (unchanged policy).
   * **Duplicate names:** see §2 — key on roster `id`, emit `needs_review` when several ids
     produce the same slug or the same all-404 result.
   * Privacy caveat: a public sidecar must **not** embed the fetched guild roster;
     use it in the sync job to resolve ids, emit only per-character data.
3. **ETag verdict: no ETags.** 0 of 158 responses (all 66 200s included) carried `ETag`,
   so `If-None-Match` is untestable and useless here — the probe recorded
   `If-None-Match: attempted=false, reason="no response in this run carried an ETag header"`.
   **`Last-Modified` is on every one of the 66 200 responses** and `If-Modified-Since`
   revalidation was proven end-to-end: re-GETting `shiftybeef-stroganoff` with the captured
   timestamp returned **304 with a 0-byte body**. Phase 2 should store `Last-Modified` per
   document and send `If-Modified-Since`; note the granularity is 1 second and the timestamps
   are per document (a character's profile/equipment/achievements/etc. share one timestamp:
   `Sat, 7 Nov 2026 22:15:40 GMT` for `shiftybeef-stroganoff`, `Fri, 6 Nov 2026 19:54:57 GMT`
   for `morc-theorc`, `Sat, 7 Nov 2026 22:21:37 GMT` for `beef-pho`,
   `Sun, 8 Nov 2026 01:44:24 GMT` for `voidlight-glows`).
4. **`Cache-Control` observed per surface** (138 of 158 responses carry none):
   character profile and all `…/character/{slug}/*` docs → **no `Cache-Control` header**;
   `/data/wow/guild/*` → `public, max-age=10800` (4 responses); static reference docs
   (`playable-class`, `playable-race`, `playable-specialization`, `achievement-category`,
   `item`, `media/item`) **and** `/data/wow/region/index` → `public, max-age=86400`
   (15 responses); `/data/wow/ruleset/index` → `public, max-age=30` (1 response).
5. **Schema drift tripwire:** every 200 echoes `battlenet-schema` and
   `battlenet-schema-revision`. Observed pairs this run: `character/1`,
   `character-equipment/1`, `character-achievement-summary/1`,
   `character-achievement-statistics/1`, `character-statistics/2`,
   `character-specializations/1`, `character-raid-progression/1`,
   `character-raid-progression-raids/1`, `character-raid-progression-dungeons/1`,
   `mythic-keystone-profile/1`, `pvp-summary/1`, `character-media/1`, `media/2`, `item/1`,
   `playable-class/1`, `playable-race/2`, `playable-specialization/2`,
   `achievement-category/2`, `guild/3`, `guild-roster/1`, `guild-activity/1`,
   `guild-achievement-summary/1`, `ruleset/1`, `region/1`. Log them in the sync job;
   a change means the field tables above need re-probing. `character-statistics` is the only
   one at revision 2.
6. **Budget planning:** this run spent **159** requests (158 GETs + the token POST):
   55 profile lookups (20 roster entries, worst case 5 forms each), 40 per-character probes
   (4 resolved × 10: 6 unlinked candidates + equipment/achievements/statistics/summary), 24
   linked follows (guild family 4, encounters 4, keystone 5, pvp 4, specializations 2,
   achievements/statistics 2, character-media 2, item media 1), 6 dynamic index /
   character-search probes, 15 static-ns index + reference-doc checks, 5 auth controls, 12
   case-variant re-checks, 1 conditional GET. A full nightly for 20 *mapped* characters ≈ 20 profiles + 20 × 8 useful sub-docs
   (equipment, achievements, achievements/statistics, statistics, specializations,
   encounters/raids, pvp-summary, mythic-keystone-profile) + guild docs + ~10 item icons per
   character **on first sight only** (§6) — i.e. ≈250–350 requests, so Phase 2 must budget
   its own cap; the probe's 300-request ceiling is not the account ceiling
   (36 000/h, 100/s, and no rate-limit headers to read). Measured round trip ≈0.44 s
   including the 0.25 s client pause.
7. **Freshness:** `last_login_timestamp` (ms) is the only in-body freshness signal; the
   body has no `last_updated_timestamp`. Modal wording should say data updates when the
   character logs out, and can show `last_login_timestamp` plus the doc's `Last-Modified`.
8. **Timestamps are epoch **milliseconds**, not seconds** (`1794102264000`).
9. **Localized strings:** read `.en_US` from every locale map. Exceptions where the name is
   already a plain string: `achievements[].achievement.name`,
   `category_progress[].category.name`, `guild.name`, `name`, `surname`, `name_search`.
10. **Do not build realm logic.** `realm` never appears in a Forever payload;
    `/data/wow/realm/index` is 404; guild hrefs are realmless
    (`/data/wow/guild/{slug}`).
11. **Optional keys to guard, all proven optional this run:** `guild` (absent for a
    guildless character), `category_progress` and `recent_events` (absent when
    `total_quantity` is 0), `specialization_groups[].specializations` (absent before a spec
    is chosen), `equipped_items[].enchantments`, `equipped_items[].requirements`,
    `equipped_items[].armor`, `equipped_items[].stats`, `equipped_items[].durability`,
    `equipped_items[].binding` (all absent on plain items such as the shirt),
    `hunter_pets` (Hunter only).

---

## 8. Known instabilities and caveats

* **16 of 20 roster entries 404.** A deliberately bogus name yields the byte-identical JSON
  404, so a 404 cannot be attributed to opt-in vs. login vs. naming. Re-run the probe after
  members opt in and log out; treat the map as provisional.
* **Probe change made for this run:** `load_roster_names()` no longer de-duplicates names,
  because `characters.json` legitimately holds the same name under two ids and the
  roster-coverage check counts entries. One probe row is now produced per roster entry
  (§2, rows #9 and #12). Nothing else in `tools/probe_bnet.py` was modified; the run
  produced zero warnings.
* **Case-sensitive slug lookup contradicts the usual "Blizzard names are case-insensitive"
  assumption.** If Blizzard later makes it case-insensitive the probe still passes (the
  lowercase form keeps working), but nothing may rely on a mixed-case slug.
* **Blizzard's own hrefs are not always fetchable.** The keystone period href served inside
  a 200 body 404s in the namespace the href itself names *and* in the static namespace;
  `/data/wow/item/10657` 404s (the id of an item the character is actually wearing, and its
  media route `/data/wow/media/item/10657` is 200) while `/data/wow/item/127` 200s in the
  same static ns. Phase 2 must treat any single reference-doc 404 as non-fatal.
* **`dynamic-forever-us` is half-populated:** ruleset and region indexes work, class/race
  and keystone-period indexes do not.
* **No rate-limit headers** and no `Retry-After` observed, so back-off logic can only react
  to a 429 (untested path this run: 0 occurrences) — keep the code but note it is unproven.
* **No ETags** (§7.3): conditional GETs work only via `If-Modified-Since`, at 1-second
  granularity, and a 304 body is empty (0 bytes) — do not attempt to JSON-parse it.
* **Tier-2 payloads are still empty for every visible character** (levels 13–20); the
  routes being 200 is *not* proof that raid/M+/PvP numbers will render — see §4. The only
  populated encounter-shaped data is per-boss kill counters in `/achievements/statistics`.
* **Roster vs API mismatches to report to the tracker owner (do not hand-edit
  `characters.json`):** `morc theorc` is listed as **Shaman** but the API says
  `character_class.id` 3 = **Hunter** (race Orc matches). The other three resolved
  characters match their roster race/class exactly.
* **Renamed characters leave stale rows.** The tracker renamed `Heal Yeah` → `Heal Naw`;
  `heal-naw` returns **404** on all five forms and `heal-yeah` (last run's slug) was **not
  probed** this run because it is no longer a roster name. The `flowmasters` guild roster
  contains a `healy-bones` (id 10740225) which matches **no** roster entry — the probe makes
  **no claim** that it is the renamed character.
* **Guild rosters are public** (a 4-member roster fetched with an unauthenticated-scope
  client-credentials token): keep them out of the committed sidecar.
* Endpoint surface is undocumented and version-pinned; the probe keeps every path and
  namespace in one config block at the top of `tools/probe_bnet.py` — Phase 2 should mirror
  that and re-run this probe after each game patch.
* Single-token names were each tried in `as_is` and lowercase only; because no single-token
  character resolved, "single-token lowercase always works" is extrapolated from the
  four resolved spaced names, **not** directly verified for e.g. `boomster`.
