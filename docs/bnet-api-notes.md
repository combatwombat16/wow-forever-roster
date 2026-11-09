# Battle.net "WoW: Forever" API — frozen Phase 1 contract

> **Read §9 first for the Character Modal Rework.** §§1–8 are the frozen Phase 1
> contract and are unchanged below. §9 is the 2026-11-08 (21:02–21:20 UTC) Phase 0
> addendum: it settles the equipment-stat question, documents `reputations` and
> `professions` (both 200, previously `not probed`), and **supersedes three claims
> below** — the "no talent tree data in any API" premise (§3.4, decision #3), the
> `inset`/`headshot` media assumption (§6 recorded 2 of 4 asset keys; the assumption
> lived in the plan's P0.5/T1.5), and the unpinned-`static-forever-us`
> 403 note (§5). Corrections are listed in §9.7.

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

---

## 9. Phase 0 addendum — reputations, professions, talents, item stats (2026-11-08, 21:02–21:20 UTC)

Produced for the Character Modal Rework (`character-tasks.md` P0.1–P0.6) by four targeted
probe passes driven through the frozen transport (`bnet.http.ProbeHttp`: host-pinned, 0.25 s
pacing, credential-masking) plus the probe's own `path_inventory` helper, so the notation
below matches §§1–8 exactly. **`python3 -m bnet probe` was deliberately not run**: it would
overwrite `docs/bnet-probe-raw.json`, the FROZEN Phase 1 evidence file that
`.pi/scripts/verify_bnet_probe.py` grounds every cited path against, and its roster-coverage
check is pinned to the probe-era `characters.json` blob (20 names) that no longer matches
today's 22-name roster. Raw Phase 0 evidence therefore lives outside the repo
(`/tmp/p0/evidence{,2,3,4}.json`, same schema, credential-masked) and the new payload shapes
are recorded below as fenced trees rather than inline path citations — an inline
`` `new.path` `` would be ungrounded against the frozen raw file and would fail that
verifier's §5 check. Statuses below are the observed ones; nothing here is inferred.

Run totals for the four scripted passes, read from their saved meta blocks: **414 requests /
4 token mints**, statuses `225 × 200`, `170 × 404`, `15 × 403`. A dozen short interactive
follow-up sweeps (≈40 further requests, one mint each) covered the talent-tree, spell-icon,
reputation-tier, namespace and 404-signature controls. **No 429 and no retry anywhere** (max
attempts on any request: 1), zero warnings, and no rate-limit headers on any response. Static namespace
re-discovered from live hrefs, unchanged: **`static-1.60.1_70399-forever-us`** (10 href
occurrences, sole candidate).

Five live characters (up from four; `healy-bones` joined the map since the 02:30 run):
**`shiftybeef-stroganoff`** (Druid, lvl 22, Feral Combat), **`beef-pho`** (Warrior, lvl 20,
Arms + Protection), **`morc-theorc`** (Hunter, lvl 19, Beast Mastery), **`healy-bones`**
(Priest, lvl 22, id 10740225 — last run's unmatched `flowmasters` roster member, §8), and
**`voidlight-glows`** (Paladin, lvl 15, Holy). Schemas observed this run, with revisions:
`character/1`, `character-equipment/1`, `character-reputations/1`, `character-professions/1`,
`character-specializations/1`, `character-statistics/2`, `character-media/1`, `item/1`,
`spell/1`, `talent/2`, `talent-tree/2`, `playable-specialization/2`, `playable-class/1`,
`profession/2`, `recipe/2`, `reputation-faction/1`, `reputation-tiers/1`, `media/2`.

### 9.1 P0.1 `/reputations` — `character-reputations`, revision 1 — **AVAILABLE (4/4 200)**

Routed (`reputations.href` on every profile). 6855–8633 bytes; **11 factions** on every
sampled character. Flat list: **there is no category grouping in this payload** — the only
grouping primitive is the standing tier plus the static faction doc (§9.1.1).

```
character.{id,name,surname,slug,key.href}
reputations[len]                                  = 11 on all four probes
reputations[].faction.id                          e.g. 577, 81, 609
reputations[].faction.name                        localized_string (12 locales, read .en_US)
reputations[].faction.key.href                    → /data/wow/reputation-faction/{id} (static ns)
reputations[].standing.raw                        cumulative total (10382)
reputations[].standing.value                      value within the current tier (1382)
reputations[].standing.max                        current-tier width (3000 | 6000 | 12000)
reputations[].standing.tier                       int 3 | 4 | 5 observed (Neutral/Friendly/Honored)
reputations[].standing.name                       localized_string ("Neutral", "Friendly", "Honored")
```

Renderer facts: a bar is `standing.value / standing.max` (both same-tier), **not** `raw / max`
— `raw` is cumulative and exceeds `max` whenever `tier` > 1 (4 of 11 rows on `healy-bones`
disagreed). The standing tier (int) is the stable key for colour/label; standing name is the label.
`max` is tier-dependent (3000 Neutral, 6000 Friendly, 12000 Honored observed), so never
hardcode a single denominator.

#### 9.1.1 Reputation static docs
| Endpoint | Status | Note |
|---|---|---|
| `/data/wow/reputation-faction/577` (static ns) | **200** `reputation-faction/1` | `id`, `name`, `description`, and a reputation-tiers reference (`id` plus its document href) |
| `/data/wow/reputation-tiers/0` (static ns) | **200** `reputation-tiers/1` | `tiers[len]` = 8, `tiers[].{id,name,min_value,max_value}` — Hated `-42000/-6000` … (the full classic ladder) |
| `/data/wow/reputation-category/index` (static ns) | **404** — **0-byte gateway 404**, no namespace header | route does not exist: **reputation categories do not exist in Forever** |
| `/data/wow/reputation-category/1` (static ns) | **404** — 0-byte, unrouted | same |

So the Reputation tab groups by `tier` (or renders flat, ordered by `value/max`) and cannot
group by Blizzard category. `reputation-tiers` is the one doc needed if the UI ever wants the
canonical 8-rung ladder with names.

### 9.2 P0.2 `/professions` — `character-professions`, revision 1 — **AVAILABLE (4/4 200)**

Routed (`professions.href`). 89–92 inventory paths per payload. Primary and secondary live in
**two sibling arrays**, each carrying skill points and **known recipes**.

```
character.{id,name,surname,slug,key.href}
primaries[len]                                    = 2 (beef-pho: Mining, Blacksmithing)
primaries[].profession.id                         186 Mining / 164 Blacksmithing
primaries[].profession.name                       PLAIN STRING (not a locale map)
primaries[].profession.key.href                   → /data/wow/profession/{id} (static ns)
primaries[].tiers[len]                            = 1 on every sampled profession
primaries[].tiers[].skill_points                  127 (current skill)
primaries[].tiers[].max_skill_points              225 (cap for the tier)
primaries[].tiers[].tier.id                       2946
primaries[].tiers[].tier.name                     PLAIN STRING ("Mining")
primaries[].tiers[].known_recipes[len]            8 | 37 (Blacksmithing carries 37)
primaries[].tiers[].known_recipes[].id            1847
primaries[].tiers[].known_recipes[].name          PLAIN STRING ("Smelt Iron")
primaries[].tiers[].known_recipes[].key.href      → /data/wow/recipe/{id} (static ns)
secondaries[len]                                  = 3 (Cooking, Fishing, First Aid)
secondaries[].…                                   same shape as primaries[]
```

Observed values: Mining 127/225, Blacksmithing 112/150, Cooking 49/75, Fishing 1/75,
First Aid 75/150 — so `max_skill_points` varies per profession and per tier; render
`skill_points / max_skill_points`. The known-recipes list is **name + id only** — no icons,
no reagents inline. **Three names are plain strings here** — the profession name, the tier
name and each known-recipe name — the same exception class as `achievements[].achievement.name` (§7.9)
— do not `.en_US` them. Professions tab can list known recipe names with zero extra
requests; per-recipe reagents/crafted item cost one static-ns request each.

| Supporting doc | Status | Fields |
|---|---|---|
| `/data/wow/recipe/1798` (static ns) | **200** `recipe/2` | `id`, `name`, `media`, `crafted_item`, `crafted_quantity`, `reagents` |
| `/data/wow/profession/164` (static ns) | **200** `profession/2` | `id`, `name`, `description`, `type`, `skill_tiers`, `media` |
| `/data/wow/media/profession/164` (static ns) | **200** `media/2` | `assets[].key` = `icon` → `…/forever-us/icons/56/136241.jpg` |

### 9.3 P0.3 `/specializations` full payload — talents confirmed, spell icons YES, tree data YES

**Confirmed on 5/5 characters** (200, `character-specializations/1`, 12 589–17 560 bytes).
The §3.4 table still holds; the deep payload adds things the leaf-capped Phase 1 inventory
missed.

```
specialization_groups[len]                        = 1 (all five characters, incl. both multi-spec)
specialization_groups[].is_active                 true
specialization_groups[].specializations[len]      1 | 2  ← multi-spec is REAL (see below)
  .specialization.{id,name,key.href}              26 Arms / 257 Protection / 613 Discipline
  .specialization_name                            localized_string
  .spent_points                                   5 | 6 | 7 | 10 | 11
  .talents[len]                                   1 | 2 | 3
  .talents[].talent_rank                          int (1..5)                       ✔ confirmed
  .talents[].talent.id                            int, e.g. 135489                  ✔ (Phase 1 saw only "<2 keys>")
  .talents[].talent.name                          localized_string                  ✔
  .talents[].spell_tooltip.spell.id               int, e.g. 12282                   ← NEW: the spell id
  .talents[].spell_tooltip.spell.name             localized_string                  ← NEW
  .talents[].spell_tooltip.description            localized_string                  ✔
  .talents[].spell_tooltip.cast_time              localized_string ("Passive")      ✔
  .talents[].spell_tooltip.power_cost             dict — EMPTY ({}) on every observed talent
  .talents[].spell_tooltip.range                  present on some talents           ← union of keys seen
  .talents[].spell_tooltip.cooldown               present on some talents           ← union of keys seen
```

`spell_tooltip` key union across all sampled talents: `spell`, `description`, `cast_time`,
`power_cost`, `range`, `cooldown`. `power_cost` was `{}` in every sample, so treat it as
present-but-useless and do not render an empty cost row.

**Multi-spec correction (matters for T2.4):** a group carries **more than one**
specialization when the character has spent points in more than one tree — `beef-pho`
returned Arms (5 pts, 2 talents) **and** Protection (6 pts, 2 talents); `healy-bones`
returned Discipline and Shadow Magic. Phase 1 read `specializations[0]` only; the Talents tab
must iterate the array and group by spec. `is_active` sits on the **group**, not on a
specification, so this payload does **not** identify which of the two specs is currently
active — the profile carries no active-spec field either (§3.1). The sidecar's existing
`activeSpec` is a Phase 1 inference from index 0 and should be relabelled or dropped.

#### 9.3.1 Spell icons: **RESOLVABLE** — one request per spell, static ns only

The spell id nested at `spell_tooltip` → `spell` → `id` (§9.3) is the id the icon route
wants. 16 distinct spell ids were drawn from four characters; 8 were probed end-to-end:

| Endpoint | Status | Evidence |
|---|---|---|
| `/data/wow/spell/{id}` **@ static ns** | **200 × 8/8** `spell/1` | keys `id`, `name`, `description`, `media`, `_links` |
| `/data/wow/media/spell/{id}` **@ static ns** | **200 × 8/8** `media/2` | `assets[].key` = `icon`, `assets[].file_data_id`, `assets[].value` |
| `/data/wow/media/spell/{id}` @ `dynamic-forever-us` | **404 × 8/8** | JSON 404 — routed, wrong namespace |
| `/data/wow/media/spell/{id}` @ `profile-forever-us` | **404 × 8/8** | JSON 404 — routed, wrong namespace |
| `/data/wow/spell/{id}/media` @ static ns | **404** — 0-byte | unrouted; only the `/data/wow/media/spell/` form exists |

Icon URL form is identical to item icons and passes the current allowlist:

```
GET /data/wow/media/spell/12282?namespace=static-1.60.1_70399-forever-us   200
assets[0] = { key: "icon", file_data_id: 132282,
              value: "https://render.worldofwarcraft.com/forever-us/icons/56/132282.jpg" }
```

**Trap recorded so Phase 1 does not repeat it:** the icon route takes the **spell id**
(`spell_tooltip` → spell → id, e.g. 12282), not the **talent id**
(`specialization_groups[].specializations[].talents[]` → talent → id, e.g. 135489). Every
`/data/wow/media/spell/{talent_id}` probe returned **404** (6 ids, 18 requests, 3 namespaces). The same talent ids DO resolve at `/data/wow/talent/{id}`
(§9.3.2) — two different id spaces.

#### 9.3.2 Talent-tree data **EXISTS** — supersedes the "no tree structure in any API" premise

Decision #3 / M1 assumed a visual tree needed embedded community data. **That assumption is
now false.** The static namespace serves full, version-pinned talent trees, including node
positions and prerequisite edges, and the selected-talent ids from §9.3 map onto them
directly (talent 135489 is a node's ranks/tooltip/talent id inside tree 1117 (spec 26)).

| Endpoint | Status | Evidence |
|---|---|---|
| `/data/wow/playable-specialization/{id}` @ static | **200** `playable-specialization/2` | keys now include **`spec_talent_tree`** (`{key.href, name}`) — Phase 1 recorded this doc as 200 but never listed the key |
| `/data/wow/talent-tree/index` @ static | **200** `talent-tree/2`, 13 806 bytes | `class_talent_trees[len]` = **9**, `spec_talent_trees[len]` = **27**, each `{key.href, name}` |
| `/data/wow/talent-tree/{treeId}/playable-specialization/{specId}` @ static | **200** `talent-tree/2`, 127 697 (Arms) / 171 121 (Feral) bytes | full node list, see tree below |
| `/data/wow/talent/{talentId}` @ static | **200 × 7/7** `talent/2` | `id`, `rank_descriptions[].{rank,description}`, `spell.{id,name,key.href}`, `playable_class`, `playable_specialization` |
| `/data/wow/media/talent-tree/{treeId}/playable-specialization/{specId}` @ static | **404** — 0-byte | unrouted: **no tree background artwork**, despite the `media.key.href` the tree doc serves |
| `/data/wow/playable-class/{id}/specializations` @ static | **404** — 0-byte | unrouted; read specs off `playable-class.specializations` instead |

Tree document shape (Arms, tree 1117 / spec 26 — 17 nodes; Feral, tree 1089 / spec 134 — 20
nodes):

```
id                                                  1117
playable_class.{id,name,key.href}                   1 Warrior
playable_specialization.{id,name,key.href}          26 Arms
name                                                localized_string
media.key.href                                      served but the route 404s (row above)
spec_talent_nodes[len]                              17 (Arms) / 20 (Feral)
spec_talent_nodes[].id                              node id, e.g. 105958
spec_talent_nodes[].node_type.{id,type}             0 / "ACTIVE" (only value seen; 37/37 nodes)
spec_talent_nodes[].display_row                     1..n   ← grid position
spec_talent_nodes[].display_col                     1..n   ← grid position
spec_talent_nodes[].raw_position_x                  1020 … 5620
spec_talent_nodes[].raw_position_y                  2130 …
spec_talent_nodes[].unlocks[len]                    node ids this node opens (4/17 Arms nodes)
spec_talent_nodes[].locked_by[len]                  node ids gating this node (5/17 Arms nodes)
spec_talent_nodes[].ranks[len]                      up to 5
spec_talent_nodes[].ranks[].rank                    1..5
spec_talent_nodes[].ranks[].tooltip.talent.{id,name,key.href}
spec_talent_nodes[].ranks[].tooltip.spell_tooltip.{spell,description,cast_time,power_cost}
```

`display_row` + `display_col` + `raw_position_x/y` are present on **37/37** nodes sampled and
`unlocks`/`locked_by` give the prerequisite graph, so a real WoW-style tree is buildable from
Blizzard data alone — **M1 no longer needs third-party tree definitions**. Cost is one static
request per spec tree (~130–170 KB each), cacheable forever (`max-age=86400`, version-pinned
ns). `talent-tree` is at revision **2** while `character-specializations` is at **1** —
log both to the §7.5 drift tripwire.

Two honest caveats for M1: the node-type value was `"ACTIVE"` on every one of the 37 sampled
nodes, so the predicted-in-game gating is unproven, and no node carries a class-tree
(`playable-specialization`-less) variant that our characters have points in — the class-tree
routes were not enumerated beyond the index (9 entries observed, not fetched).

### 9.4 P0.4 Item-stat hunt — **VERDICT: AVAILABLE, from one source only (`/equipment`)**

This is the decision the user asked for. Equipment stat numbers **are** available
authoritatively from Blizzard, but **only inside `/profile/wow/character/{slug}/equipment`**
(schema `character-equipment/1`, already GET by the sync and thrown away at `fetch.py`
~L273–295). The item *dictionary* endpoints carry **no** stat numbers at top level, the
bonus-list route does not exist, and every classic-era namespace is closed to this client.

**Verdict table — what answers, and with what:**

| Source | Status | Stat numbers? | Evidence |
|---|---|---|---|
| `/profile/wow/character/{slug}/equipment` | **200 × 5/5** | **YES — authoritative** | 48 of 61 sampled equipped items carry `equipped_items[].stats[]` (**104 stat entries**, each with a stat-type enum, a numeric `value`, a display `display_string` and an RGBA `color`); 41 `equipped_items[].armor.value`, 10 `weapon.damage.{min_value,max_value,display_string}` + `attack_speed` + `dps`, 1 shield-block value ("1,320 Block"), 24 stats flagged `is_equip_bonus`, 4 `equipped_items[].enchantments[].display_string`, 3 item-spell descriptions, 1 plain `description` |
| `/data/wow/item/{id}` @ static (pinned **or** unpinned) | **200 × 32 / 404 × 25** of 57 real gear ids | **NO at top level** — `stats` present on **0 of 32** | top-level keys are only `id`, `name`, `quality`, `level`, `required_level`, `media`, `item_class`, `item_subclass`, `inventory_type`, `purchase_price`, `sell_price`, `max_count`, `is_equippable`, `is_stackable`, `appearances`, `preview_item`, `_links` |
| — same doc, nested **`preview_item`** | same 200s | **YES, but partial and id-404-gated** | 28 of the 32 answering docs carry a `preview_item` with a stats array / armor / weapon; 23 carry preview durability, 20 preview requirements, 1 preview spells; **0** sockets/gems |
| `/data/wow/item/{id}` @ `dynamic-forever-us` | **404 × 57/57** | no | JSON `BLZWEBAPI00000404` |
| `/data/wow/item/{id}` @ `profile-forever-us` | **404** | no | JSON 404 |
| `/data/wow/item/{id}/bonus-list` (static / profile / dynamic) | **404 × 0-byte, unrouted** | **route does not exist** | no `battlenet-namespace` echo, no `Content-Type` — the §1 unrouted signature |
| `/data/wow/item/{id}/bonuses` | **404 × 0-byte** | no | unrouted |
| `/data/wow/bonus-list/index` @ static | **404 × 0-byte** | no | unrouted |
| `/data/wow/item/{id}` @ `classic-era-1/2/3/4-us`, `classic-us`, `classic-mainline-us`, `profile-classic-era-1-us`, `dynamic-classic-era-1-us`, `static-classic-era-1-us` | **403 × 15/15** | **namespace closed** | `{"code":403,"type":"BLZWEBAPI00000403","detail":"Forbidden"}` — byte-identical to the §1 unknown-namespace control, i.e. **the classic-era namespace family is not licensed to this client at all**, so no era-specific stat dictionary is reachable |
| `/data/wow/playable-class/index` @ the same 9 classic-era namespaces | **403 × 9/9** | n/a | same 403 — the whole namespaces, not the item route, are the blocker |

**Definitive answer for decision #1: stats are AVAILABLE.** Ship authoritative numbers in the
gear tooltip, sourced from the `/equipment` payload the sync already fetches — no third-party
item DB, and **no extra request per item** for the numbers themselves. Per §3.2 the same
payload also carries quality, class/subclass, `equipped_items[].level.display_string`, enchant
`display_string`s and durability, so the enriched tooltip needs exactly the one document it
already gets.

**Why the item dictionary is not a viable source** (this is what §8's `10657` note was
reaching for, now measured): of the **57 distinct item ids the five characters are actually
wearing**, only **32 (56 %)** resolve at `/data/wow/item/{id}` — **25 return 404**, including
`10657` (an item Shiftybeef is wearing right now) and 24 more: `3288, 3645, 5404, 6449,
6461, 6465, 6469, 6472, 6480, 6481, 6540, 6627, 8071, 9777, 10919, 12006, 13245, 14174,
14745, 15011, 15450, 270008, 273088, 286744`. The pinned and unpinned static namespaces give
the **identical** 32/25 split, so it is not a namespace-selection problem. And on the 32 that
do answer, the top level still has **zero** stat fields. Building tooltips on item docs would
therefore lose numbers for 44 % of real gear *and* for the remaining 56 % need the nested
`preview_item` anyway. `preview_item` is still worth caching opportunistically (it is the
only place a non-equipped item's stats exist), but it cannot be the primary source.

**`bonus_list` is opaque.** 5 of 61 equipped items carry a `bonus_list` array (e.g. `[12745]`)
but the only route that could decode it does not exist in any namespace (rows above), so
bonus-list ids cannot be turned into extra stat lines. Render the item without them; the
`is_equip_bonus` flag on 24 stat entries is the only bonus signal available.

**Not available anywhere: sockets and gems.** Zero `socket*` / `gem*` keys across all 61
equipped items and all 32 answering item documents (a 1 400-leaf / depth-16 inventory of
every character's full equipment found nothing). **T1.1 and T2.3 must drop the
gems/sockets requirement** — it is not a "not captured yet" gap, it is absent from the API.
`set` and `cost` blocks were likewise absent on all 61 items.

### 9.5 P0.5 `/statistics` and `character-media` — both 200, icons pass the host policy

`/statistics` (**200** on every probe — three distinct characters: `shiftybeef-stroganoff`,
`morc-theorc`, `beef-pho`; `character-statistics/2`, 2 964–2 997 bytes) confirms §3.5 and is
unchanged in shape: **60 top-level keys** on the two characters inventoried in depth. Value shapes verified
directly: plain scalars (`health` 857, `power` 787, `expertise` 0.0, `mana_regen` 61.0,
`main_hand_damage_min` 58.04286), base/effective pairs (`strength` `{base: 38, effective: 53}`,
`armor` `{base: 64, effective: 553}`), and rating triples (`dodge`
`{value: 10.421599, rating_bonus: 0.0, rating_normalized: 0}`, same for `spell_crit`). The
only route change worth noting: this run's payload carries **`expertise`** as a plain number
— new relative to §3.5's list, which names only `off_hand_expertise`, so §3.5 is to be read
as including `expertise` — and **no** `ranged_dps`/`spell_damage` keys (§3.5 never listed
those either).
`power_type.key.href` points at `/data/wow/power-type/0` in the static ns.

`character-media` (**200 × 5/5**, `character-media/1`, 760–812 bytes) returns exactly four
assets, **all four with `id` and `file_data_id` absent (`null`)** — only `key` and `value`
are populated, and `value` is a finished URL:

```
assets[0].key = "main-raw"      value …/forever-us/character/{raceId}/{characterId}-main-raw.png
assets[1].key = "main-sd-raw"   value …/character/{raceId}/{characterId}-main-sd-raw.png
assets[2].key = "avatar"        value …/character/{raceId}/{characterId}-avatar.jpg
assets[3].key = "avatar-sd"     value …/character/{raceId}/{characterId}-avatar-sd.jpg
```

**No `inset` and no `headshot` key exists** on any of the five characters (20 asset slots
inspected). Those are retail key names; on Forever the header portrait must come from
**`avatar` / `avatar-sd`** and the full render from `main-raw`. **T1.5 must target
`avatar`, not `inset`/`headshot`.** Race ids seen: 1, 240, 53, 36, 69 — again the render path
embeds race id + Battle.net id, never the slug (§6).

**Safe-host verdict: PASS, and no allowlist change is needed.** Every URL harvested across
all four passes — **633 URLs** — landed on exactly two hosts, and both are already in
`ICON_HOSTS` in `js/detail.js`:

| Host | URLs | `isSafeIconUrl()` |
|---|---|---|
| `us.api.blizzard.com` | 594 | **pass** (594/594) |
| `render.worldofwarcraft.com` | 39 | **pass** (39/39) |

Checked by executing the real `isSafeIconUrl`/`ICON_HOSTS` from `js/detail.js` under node
(browser globals stubbed for the js/state.js import), not by reimplementing the rule. **T2.11
is a no-op:** spell icons (§9.3.1), profession icons (§9.2) and character renders share the
existing `/forever-us/` render host, so no new Blizzard host needs admitting.

### 9.6 Phase 0 drift and sample notes

* **Roster coverage grew**: `healy-bones` (id **10740225**, Priest lvl 22, Discipline) now
  resolves and is mapped. That is the same id §8 recorded as a `flowmasters` guild-roster
  member matching **no** tracker entry — it has since been added as roster id
  `1791929106142` "Healy Bones", so the §8 concern is resolved for this id.
* `morc-theorc` is still reported by the roster as **Shaman** while the API says
  `character_class.id` 3 = **Hunter** (§8) — still unfixed, still the tracker owner's to fix.
* Levels moved between runs (Shiftybeef 20 → 22, Morc 13 → 19), so §4's "re-probe on a
  levelled character" is now partially satisfiable: **`healy-bones` at 22 and
  `shiftybeef-stroganoff` at 22 are the best Tier-2 re-probe candidates**, though still far
  from raid level.
* The duplicate-name case (§2, `Tir Rheebeard` × 2) gained a **negative** result worth
  recording: the guess that a realm suffix might separate the two ids is refuted —
  `tir-rheebeard-nahbrah` returns **404** with the JSON `BLZWEBAPI00000404` body (routed,
  no such character). There is no realm-qualified slug form; the `needs_review` rule stands.
* Achievements criteria now carry a `child_criteria[].{id,amount,is_completed}` shape
  (2/2 sampled achievements on 2 characters) and **no `ref_text`** was seen, so
  `fetch.py`'s `ref_text`-driven "Linen Bandage"-style description extraction still has no
  live evidence in this run — the only such description remains the one §3.3 observed inside
  `/achievements/statistics`.
* No `429`, no `Retry-After`, and no rate-limit headers again across the 414 scripted
  requests (the ~40 interactive-sweep requests — including the talent-tree route statuses
  cited above — were likewise under limit), so the §1 "quota is unreadable from responses"
  finding is re-confirmed at a 2.6× larger volume.
* No `ETag` observed on any 200 this run either — §7.3's `Last-Modified`-only rule holds.

### 9.7 Corrections to §§1–8 caused by this run

1. **§3.4 / decision #3 / M1:** "No tree structure/prerequisites in any API" is **withdrawn**.
   `playable-specialization` carries `spec_talent_tree`, and `/data/wow/talent-tree/...`
   serves positions (`display_row/col`, `raw_position_x/y`) and prerequisite edges
   (`unlocks`, `locked_by`) — see §9.3.2.
2. **§3.4:** a `specialization_group` can hold **several** specializations; reading
   `specializations[0]` is not the active spec (§9.3). No API field marks the active spec.
3. **§6 / T1.5:** `character-media` has **no** `inset`/`headshot`; the usable keys are
   `main-raw`, `main-sd-raw`, `avatar`, `avatar-sd` (§9.5).
4. **§8:** "even where it answers it carries no stat numbers" is **confirmed for the top
   level** of `/data/wow/item/{id}` (0/32) but **incomplete** — the nested `preview_item`
   carries `stats`/`armor`/`weapon` on 28/32 ids, and the authoritative numbers for worn gear
   were always in `/equipment` (§9.4).
5. **§5:** unpinned `static-forever-us` **is routable for item docs** — `/data/wow/item/127`
   returned **200** with byte-identical size (5 331 B) to the pinned form, and the same
   32/25 200/404 split. The old 403 note is superseded; still prefer the re-discovered
   pinned value, because a pinned namespace is what makes `max-age=86400` caching safe.
6. **§3 "not probed" list:** `reputations.href` and `professions.href` are now **probed and
   200** (§9.1, §9.2). Still not fetched: `titles.href`, `collections.href`,
   `appearance.href`, `quests.href`, `…/{slug}/guild`, `…/{slug}/hunter-pets`.
7. **New drift tripwires:** `talent-tree/2`, `talent/2`, `spell/1`, `recipe/2`,
   `profession/2`, `reputation-faction/1`, `reputation-tiers/1` are now load-bearing for the
   rework and should join `config.DRIFT_SURFACES`.

---

## 10. Phase 4a addendum — class / race / profession **icon** media (2026-11-09, 01:05–01:10 UTC)

Produced for the Phase 4a sync/data extension (race + class identity in the sidecar, profession
icons, and the class/race icon question the modal needs answered). Three scripted read-only
sweeps drove the frozen transport (`bnet.http.ProbeHttp`: host-pinned, credential-masking,
`REQUEST_SPACING_S` = 0.25 s pacing) directly, because `probe.py`'s path inventory does not
contain these routes. **`python3 -m bnet probe` was deliberately not run** — it would overwrite
`docs/bnet-probe-raw.json`, the FROZEN Phase 1 evidence `.pi/scripts/verify_bnet_probe.py`
grounds every cited path against — so the raw evidence for this section lives outside the repo
(`/tmp/p4a/evidence.json`, `evidence2.json`, `evidence3.json`, same request-log schema,
credential-masked), exactly as §9's did, and the new payload shapes are recorded below as fenced
trees rather than inline `` `new.path` `` citations.

Credentials came from the repo-root `.env` via `set -a && . ./.env && set +a`; no secret value
was printed, logged or written. Static namespace re-discovered from a live profile href,
unchanged: **`static-1.60.1_70399-forever-us`**.

Run totals, read from the three saved meta blocks: **45 requests / 3 token mints** (one per
sweep), statuses **20 × 200, 22 × 404**. **No 429, no retry, max attempts on any request: 1**,
no rate-limit headers, and no `ETag` on any response — §1, §7.3 and §9.6 re-confirmed at this
volume. Every 200 static-ns doc carried `Last-Modified` and `Cache-Control: public,
max-age=86400`, so these docs are cacheable exactly like the item/spell media of §6 / §9.3.1.

### 10.1 Verdict table — the three icon surfaces

| Surface | Verdict | Route that answers (GET, static ns) | Example value |
|---|---|---|---|
| **Playable-class icon** | **AVAILABLE** (5/5 ids) | `/data/wow/media/playable-class/{class_id}` | `https://render.worldofwarcraft.com/forever-us/icons/56/626008.jpg` (1 Warrior) |
| **Playable-race icon** | **UNAVAILABLE — no route exists** | — | — (see 10.3) |
| **Profession icon** | **AVAILABLE / re-confirmed** (3/3 ids, primary **and** secondary) | `/data/wow/media/profession/{profession_id}` | `…/icons/56/136241.jpg` (164 Blacksmithing), `…/136248.jpg` (186 Mining), `…/133971.jpg` (185 Cooking) |

**Safe-host verdict: PASS, and `isSafeIconUrl()` still needs no change.** Every icon URL the
three sweeps harvested — **8 of 8** — landed on `render.worldofwarcraft.com` under
`/forever-us/icons/56/`, the same form as the item icons (§6) and the spell icons (§9.3.1), so
no new Blizzard host appears and T2.11 stays a no-op.

### 10.2 Class icons — **AVAILABLE**, prefix route only

| Endpoint | Status | Evidence |
|---|---|---|
| `/data/wow/playable-class/{1,3,11}/media` @ static | **404 × 0-byte, unrouted** | no `battlenet-namespace` echo, no `Content-Type` — the §1 unrouted signature: **the `…/{id}/media` sub-resource form does not exist** |
| `/data/wow/media/playable-class/{1,2,3,5,11}` @ static | **200 × 5/5** `media/2` | ids 1/2/3/5/11 = Warrior, Paladin, Hunter, Priest, Druid — every class a live roster character uses. Payload below |
| `/data/wow/media/playable-class/95` @ static | **404 with a 60-byte JSON body** (routed) | id 95 is a **race** id, not a class: the routed-vs-unrouted split proves the row above is a *route* verdict, not an id-size accident |
| `/data/wow/playable-class/{1,3,11}` @ static | **200** `playable-class/1` | the doc embeds its own media reference, and — unlike the talent-tree case of §9.3.2 — **the href it serves is fetchable**: `media.key.href` names the 200 prefix route byte for byte |

```
GET /data/wow/media/playable-class/1?namespace=static-1.60.1_70399-forever-us   200 media/2
id = 1
assets[len]        = 1
assets[].key       = "icon"
assets[].file_data_id  626008
assets[].value     https://render.worldofwarcraft.com/forever-us/icons/56/626008.jpg
```

Observed file_data_ids: 1 → 626008, 2 → 626003, 3 → 626000, 5 → 626004, 11 → 625999.

### 10.3 Race icons — **UNAVAILABLE**, evidenced nine ways

| Probe | Status | Reading |
|---|---|---|
| `/data/wow/playable-race/{2,5,6}/media` @ static | **404 × 0-byte unrouted** | sub-resource form absent |
| `/data/wow/playable-race/1/media` @ `dynamic-forever-us` | **404 × 0-byte unrouted** | absent in the other plausible namespace too |
| `/data/wow/media/playable-race/{1,2,5,6,36,53,69,240}` @ static | **404 × 0-byte unrouted (8/8)** | eight race ids spanning the classic ids (1 Human, 2 Orc, 5 Undead, 6 Tauren) **and** the four race ids embedded in this roster's live `character-media` render paths (§9.5: 36, 53, 69, 240) — so the 404 is systematic, not "a race we do not have" |
| `/data/wow/media/playable-race/2` @ `dynamic-forever-us`, @ `profile-forever-us`, @ unpinned `static-forever-us` | **404 × 0-byte unrouted (3/3)** | no namespace answers this route |
| `/data/wow/media/playable-race/index` @ static | **404 × 0-byte unrouted** | there is not even a media **index**, so the family cannot be discovered |
| `/data/wow/playable-race/{2,5,6,95}` @ static | **200** `playable-race/2` | **the doc carries no media key at all** (keys below) — the §9.3.2 "served-but-404ing href" case does not even arise |
| `/data/wow/playable-race/index` @ static | **200** | elements carry only `races[].id`, `races[].key.href`, `races[].name` — no icon field to fall back on |
| `/data/wow/humantype/index`, `/data/wow/media/humantype/1` @ static | **404 × 0-byte unrouted** | the retail human-type artwork family (the usual race-icon source) is unrouted here too |
| `racial_spells[].key.href` on the race doc | href → `/data/wow/spell/20572` (Orc "Blood Fury") | the only artwork a race doc points at is a **spell** icon, resolvable via the §9.3.1 route. **Recorded, deliberately not used**: a spell glyph is not a race icon, and inventing that mapping in a committed artifact is worse than no icon |

```
GET /data/wow/playable-race/2?namespace=static-1.60.1_70399-forever-us   200 playable-race/2
top-level keys: id, name, faction, gender_name, is_allied_race,
                is_selectable, playable_classes[], racial_spells[], _links
                → NO media key (checked on race ids 2, 5, 6 and 95 alike)
```

### 10.4 Consequences for the sync (implemented in this phase)

1. The sidecar now stores **`race` and `class` as `{id, name}`**, straight off the profile
   payload the sync already fetches — `race.id`, `race.name` and `character_class.id`,
   `character_class.name` (§3.1). Zero extra requests. A reference missing either half is
   **reported as a warning and the key is omitted**, never published half-populated.
2. **`classIconUrl`** is stored per character from the 10.2 route: one request per distinct
   class id per run (URL-de-duplicated like every other media), `Cache-Control`-friendly,
   H2-gated through `safe_artifact_url`, null on a 404.
3. **`raceIconUrl` is not a key the sidecar has.** Publishing a permanent null would read as
   "synced, and Blizzard returned nothing for this character", which is a different claim from
   "this API has no race artwork at all". The sync therefore omits the key entirely, and
   `.pi/scripts/verify_sync_artifacts.py` WARNs (soft, exit 0) on any row that grows one —
   loud in the run report, not a nightly red-line.
4. Every **profession** entry (both `primaries` and `secondaries`) now carries **`iconUrl`**
   from the 10.1 route, gated and nulled the same way.
5. Two new drift tripwires joined `config.DRIFT_SURFACES`: `profession_media` and
   `playable_class_media` (schemas observed here: `media/2`, `playable-class/1`,
   `playable-race/2`).

## 11. Phase 5b addendum — class / profession **dictionaries**, and the two static icon maps (2026-11-09, 04:28–04:34 UTC)

Produced for P5.B (`character-tasks.md` Phase 5: `data/class-icons.json`,
`data/profession-icons.json`). §10 proved the two per-id media routes but never enumerated
the id space, so this addendum covers what a site-wide icon map needs and nothing else: the
**index** documents, the media route re-confirmed across every id those indexes list, and the
shape of the two committed artifacts.

Two scripted read-only sweeps drove the frozen transport (`bnet.http.ProbeHttp`: host-pinned,
credential-masking, `REQUEST_SPACING_S` = 0.25 s pacing) directly, because `probe.py`'s path
inventory contains neither index. **`python3 -m bnet probe` was deliberately not run** — it would
overwrite `docs/bnet-probe-raw.json`, the FROZEN Phase 1 evidence
`.pi/scripts/verify_bnet_probe.py` grounds every cited path against — so the sweep output stayed
outside the repo (`/tmp/p5b_probe.py`, `/tmp/p5b_probe2.py`, request-log schema, credential-masked)
exactly as §9's and §10's did, and the payload shapes below are fenced trees rather than inline
path citations.

Run totals, read from the two saved runs: **sweep 1 — 5 requests / 1 token mint, 3 × 200 and
1 × 404; sweep 2 — 4 requests / 1 mint, 2 × 200 and 1 × 404**. **No 429, no retry, max attempts
on any request: 1**, no rate-limit headers and no `ETag` on any response (§1, §7.3, §9.6,
§10 re-confirmed at this volume); both 200 index docs carried `Last-Modified` and
`Cache-Control: public, max-age=86400`, i.e. cacheable like every other static doc. Credentials
came from the repo-root `.env` via `set -a && . ./.env && set +a`; no secret value was printed,
logged or written. Static namespace re-discovered from live profile hrefs during the following
sync, still identical to the Phase 1 / §9 / §10 value: **`static-1.60.1_70399-forever-us`**.

The full local sync then ran once on this contract (`PYTHONPATH=tools python3 -m bnet sync`):
**131 requests / 1 mint, 38 × 200, 54 × 304, 38 × 404, runtime 59.4 s**, exit 0, and both maps
landed **9/9 classes and 13/13 professions with zero refusals**. That same run appended
`guild roster fetch failed for heghmoh` to the 14 `not_found` note fields in
`battle-net-map.json` — a reported soft sub-resource failure (the guild-roster lookup that runs
per `not_found` re-query), not an icon-map event; the next nightly refresh rewrites the notes.

### 11.1 Verdict table — the dictionary surfaces

| Endpoint (GET, static ns) | Status | Reading |
|---|---|---|
| `/data/wow/playable-class/index` | **200** `playable-class/1`, 3 133 B | `classes[len]` = **9** — ids 1, 2, 3, 4, 5, 7, 8, 9, 11 (Warrior, Paladin, Hunter, Rogue, Priest, Shaman, Mage, Warlock, Druid). Already §5; re-read here because the map needs the whole list, not the roster's subset. |
| `/data/wow/profession/index` | **200** `profession/2`, 4 968 B | `professions[len]` = **13**, **primaries and secondaries in ONE flat list**: 171 Alchemy, 182 Herbalism, 129 First Aid, 164 Blacksmithing, 165 Leatherworking, 185 Cooking, 186 Mining, 197 Tailoring, 202 Engineering, 333 Enchanting, 356 Fishing, 393 Skinning, **2933 Test Profession [DNT]** |
| `/data/wow/media/profession/index` | **404**, 60-byte JSON body `BLZWEBAPI00000404` with the namespace echoed and `Content-Type: application/json;charset=UTF-8` | **routed 404** (the §10 signature of "wrong id / no such resource"), NOT the 0-byte unrouted kind: the media family exists, but it has **no index**. The dictionary therefore supplies names and ids and every icon still costs one per-id media GET. |
| `/data/wow/media/playable-class/{1,2,3,4,5,7,8,9,11}` | **200** `media/2` — **9/9** | §10 sampled 1, 2, 3, 5, 11; the four it did not (4 Rogue, 7 Shaman, 8 Mage, 9 Warlock) answered from the same route in the sync run above, one fresh 200 icon asset each, so the route now covers the entire class id space this build serves. |
| `/data/wow/media/profession/{129,164,165,171,182,185,186,197,202,333,356,393,2933}` | **200** `media/2` — **13/13** | Includes the secondary professions (129 First Aid, 185 Cooking, 356 Fishing, 393 Skinning) and the DNT row 2933: **one route for every profession the index lists**, exactly as §10.1 predicted from three samples. |
| Playable-**race** dictionary for icons | **unchanged: UNAVAILABLE** | §10.3 stands nine ways. The race index answers 200 but its elements carry no media key and no icon field, so **there is no race icon map and none is planned** — the site's race rows stay text-only. |

### 11.2 The two index payloads (observed shapes)

Both dictionaries are the same three-key element shape and **neither carries a
primary/secondary flag, a type field, or any icon field** — checked on every element of both
indexes, not just the first:

```
GET /data/wow/playable-class/index?namespace=static-1.60.1_70399-forever-us   200 playable-class/1
top-level keys: _links, classes
classes[len]      = 9
classes[].id      int: 1, 2, 3, 4, 5, 7, 8, 9, 11 — the list simply skips 6 and 10 (the payload
                  gives no entry and no reason for the gaps; the retail meaning of those ids was
                  not probed and is not assumed)
classes[].name    LOCALIZED string map, 11 locales on all nine classes: en_US, en_GB, de_DE,
                  es_ES, es_MX, fr_FR, pt_BR, ru_RU, ko_KR, zh_CN, zh_TW — no it_IT on any class
                  (read .en_US, §7.9)
classes[].key.href → /data/wow/playable-class/{id}?namespace=<same static ns>
(no type, no media, no icon, no specialization list)

GET /data/wow/profession/index?namespace=static-1.60.1_70399-forever-us        200 profession/2
top-level keys: _links, professions
professions[len]  = 13
professions[].id  int
professions[].name LOCALIZED string map — 12 locales on the twelve real professions (the 11 above
                  plus it_IT), 2 (en_US, en_GB) on id 2933 Test Profession [DNT]
professions[].key.href → /data/wow/profession/{id}?namespace=<same static ns>
(no type/primary/secondary flag anywhere in the payload)
```

**Correction to the §9.2 plain-string exception as applied to the dictionary:** §9.2 records
"THREE names are plain strings here" for the **per-character** `character-professions` payload,
and that still holds. The **dictionary** localizes the same names, so a reader that reuses one
helper for both must go through the locale reader (`.en_US`) and tolerate a plain string — which
is what `tools/bnet/fetch.py` `loc()` already does.

### 11.3 The two committed artifacts (the UI contract)

Both files use the `talent-trees.json` envelope, because the site already knows what
"pinned to this static namespace, updated at this time" means, and both are keyed by
**Blizzard's own name** rather than by id — the surfaces that consume them (class distribution
bars, the guild professions matrix, party slots) start from a word, not an id:

```
data/class-icons.json                              data/profession-icons.json
staticNamespace  static-1.60.1_70399-forever-us    same
updatedAt        ISO8601 UTC                        same
icons            keyed by the 9 class names         keyed by the 13 profession names
icons[<name>]    { id: int, iconUrl: string }       same (iconUrl is NEVER null here)
```

Observed icon values (all 22 landed in the run above; every one is the §6/§9.3.1/§10.1 form
`https://render.worldofwarcraft.com/forever-us/icons/56/<file_data_id>.jpg`, so **`isSafeIconUrl()`
still needs no change** and T2.11 stays a no-op):

| Class id → file_data_id | | Profession id → file_data_id |
|---|---|---|
| 1 Warrior → 626008 | 2 Paladin → 626003 | 129 First Aid → 135966 | 164 Blacksmithing → 136241 |
| 3 Hunter → 626000 | 4 Rogue → 626005 | 165 Leatherworking → 136247 | 171 Alchemy → 136240 |
| 5 Priest → 626004 | 7 Shaman → 626006 | 182 Herbalism → 136246 | 185 Cooking → 133971 |
| 8 Mage → 626001 | 9 Warlock → 626007 | 186 Mining → 136248 | 197 Tailoring → 136249 |
| 11 Druid → 625999 | | 202 Engineering → 136243 | 333 Enchanting → 136244 |
| (164/185/186 match §10.1 and ids 1/2/3/5/11 match §10.2 byte for byte) | | 356 Fishing → 136245 | 393 Skinning → 134366 |
| | | 2933 Test Profession [DNT] → 134400 |

`2933 Test Profession [DNT]` is **included on purpose**: the index lists it, its media doc answers
200, and the P5.B rule is "every profession the dictionary returns". A UI that must not show it
filters on the name — inventing an exclusion list here would silently hide whatever Blizzard ships
next under a different marker.

### 11.4 Consequences for the sync (implemented in this phase)

1. **One index read per map per run** plus **one media GET per distinct id**, through the same
   `icon_for` plumbing as everything else: URL de-duplication (a class icon the per-character pass
   already fetched costs nothing extra), `If-Modified-Since` conditional reuse, and
   `safe_artifact_url` as the only writer of a URL. The route constants live in
   `tools/bnet/config.py` beside §10's.
2. **Merge by NAME with the committed file, only while the pinned static namespace is unchanged**
   — the talent-tree rule of §9.3.2. A same-patch nightly therefore costs **two index GETs and
   zero media GETs** for both maps together (verified on the second local run: `200:2` for the
   whole run, `class icons: 9 of 9 index entries landed (9 carried from the committed map, …)` and
   the same for 13 professions), and a patch (new namespace) drops the old map rather than merging
   geometry from two versions. A carried entry is re-validated from scratch (render-host gate + int id) and is
   only reused when the index still gives that name the same id.
3. **No icon, no entry.** A 404 or an off-host asset omits the key and reports **one line per
   refused entry** in the run summary (the `feature_failures` pattern), because "the map has 8
   classes today" is a finding with a cause and never a silent shortening. An entry never carries
   a null iconUrl: an entry exists precisely because its icon resolved. This is the opposite of the
   per-character fields, where null is the documented §8 outcome.
4. **A name must earn the right to be a key.** The map key is response data that a browser does
   lookups with, so it is refused (and the refusal reported) when it is empty, longer than the
   64-character ceiling, carries a control character or a URL scheme, or is an
   `Object.prototype` property name (`__proto__`, `constructor`, `toString`, …) — the last case
   would make a JS consumer's `out[name] = entry` set the prototype instead of adding an entry.
   Real Blizzard names are unaffected; truncation is deliberately never used, because a cut-down
   name would answer lookups for a different profession.
5. **A failed index read writes nothing.** The committed file stays as published: an unreachable
   dictionary must not reach the site as "this game has no classes".
6. Both maps are written **after** the map/sidecar pair lands (like the talent trees), so a refused
   or crashed run never leaves half a reference dataset updated, and
   `.github/workflows/sync-bnet.yml` commits both paths.
7. One new drift tripwire joined `config.DRIFT_SURFACES`: `profession_index` (schemas observed
   here: `profession/2`, `playable-class/1`, `media/2`). The class index and both media surfaces
   were already listed from §5 and §10.4.5.
8. `.pi/scripts/verify_sync_artifacts.py` gained the matching gates in the same change as the
   data: exact envelope keys, ISO `updatedAt`, a version-pinned Forever static namespace, per-entry
   `{id: int, iconUrl}` with the render-host regex, the key rules of (4) re-implemented
   independently, **duplicate JSON keys at any depth** (a repeated name would otherwise hide one
   entry behind another), the 64 KiB size ceiling, the foreign-URL and secret scans over both
   files, and a soft WARNING when the maps stop covering a class or profession the roster renders
   (a patch that renames a class is a finding, not an outage — §10.4.3 reasoning).
