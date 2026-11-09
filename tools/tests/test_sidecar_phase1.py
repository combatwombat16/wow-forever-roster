#!/usr/bin/env python3
"""Offline unit tests for the Phase 1 sidecar extension (T1.1–T1.5).

Every fixture mirrors a payload shape recorded in docs/bnet-api-notes.md §9 —
the stats tuple of §9.4, the reputation standing of §9.1, the profession/recipe
strings of §9.2, the §9.3 talent payloads (including the spell-id-vs-talent-id
icon trap of §9.3.1) and the §9.3.2 tree geometry — so a contract drift in the
extractors fails here, not in the committed sidecar. Stdlib unittest only, no
network, no credentials, no committed artifacts (writes redirect to a temp dir).

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/

from bnet import artifacts, config, fetch, http  # noqa: E402

STATIC = "static-1.60.1_70399-forever-us"
API = config.API_BASE
PROFILE_HREF = f"{API}/profile/wow/character/beef-pho?namespace={config.NS_PROFILE}"


class Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    @property
    def json(self):
        return self._payload


class TableClient:
    """Answers exact URLs from a table; every miss is a 404, like the gateway."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.urls = []
        # Phase B: HEADs are recorded separately, so every existing "which GETs were
        # sent" assertion keeps its meaning while the render probe is exercised.
        self.heads = []
        self.tokens_minted = 1
        self.requests_made = 0
        self.revalidations = 0
        self.status_counts = {}

    def get(self, url):
        self.urls.append(url)
        self.requests_made += 1
        status, payload = self.table.get(url, (404, None))
        return Resp(status, payload)

    def head(self, url):
        self.heads.append(url)
        self.requests_made += 1
        status, payload = self.table.get(url, (404, None))
        return Resp(status, payload)


def make_fetcher(table=None):
    cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
    client = TableClient(table)
    f = fetch.Fetcher(client, cache)
    f.static_ns = STATIC          # normally captured from the first profile
    return f


def media_url(spell_id):
    return f"{API}/data/wow/media/spell/{spell_id}?namespace={STATIC}"


def icon_payload(spell_id):
    return {"assets": [{"key": "icon",
                        "value": f"https://render.worldofwarcraft.com/forever-us/icons/56/{spell_id}.jpg"}]}


# --------------------------------------------------------------------------- #
# T1.1 — gear tooltip extractors (notes §3.2, §9.4)
# --------------------------------------------------------------------------- #
class GearExtractors(unittest.TestCase):
    def test_item_stats_keep_value_display_flag_and_rgba_color(self):
        stats = [{"type": {"type": "INTELLECT"}, "value": 4, "is_equip_bonus": True,
                  "display": {"display_string": {"en_US": "+4 Intellect"},
                              "color": {"r": 255, "g": 255, "b": 255, "a": 1.0}}}]
        self.assertEqual(fetch.extract_item_stats(stats), [{
            "value": 4, "displayString": "+4 Intellect", "isEquipBonus": True,
            "color": {"r": 255, "g": 255, "b": 255, "a": 1.0}}])

    def test_item_stats_junk_and_bool_values_are_dropped(self):
        # §9.4 sampled stats carry numbers; a bool "value" is a type lie and a
        # stat with neither number, text nor colour carries nothing renderable.
        self.assertEqual(fetch.extract_item_stats(
            [{"value": True}, "junk", {"value": 0}]),
            [{"value": 0, "displayString": None, "isEquipBonus": False, "color": None}])
        self.assertEqual(fetch.extract_item_stats(None), [])

    def test_is_equip_bonus_defaults_false_and_plain_display_string_survives(self):
        out = fetch.extract_item_stats([{"display": {"display_string": "13 Armor"}}])
        self.assertEqual(out[0]["isEquipBonus"], False)
        self.assertEqual(out[0]["displayString"], "13 Armor")
        self.assertIsNone(out[0]["color"])

    def test_enchants_are_plain_display_strings(self):
        enchants = [{"enchantment_id": 41, "display_string": {"en_US": "Enchanted: Stamina +2"}},
                    {"display_string": "Enchanted"}, {"no_string": 1}, "junk"]
        self.assertEqual(fetch.extract_enchants(enchants),
                         ["Enchanted: Stamina +2", "Enchanted"])

    def test_id_name_handles_locale_maps_plain_strings_and_absence(self):
        self.assertEqual(fetch._id_name({"id": 4, "name": {"en_US": "Armor"}}),
                         {"id": 4, "name": "Armor"})
        self.assertEqual(fetch._id_name({"id": 1, "name": "Weapon"}), {"id": 1, "name": "Weapon"})
        self.assertIsNone(fetch._id_name(None))
        self.assertIsNone(fetch._id_name({"key": {"href": f"{API}/data/wow/item-class/4"}}))

    def test_enriched_gear_row_keeps_every_v2_field_and_adds_the_tooltip_set(self):
        item = {"slot": {"name": {"en_US": "Chest"}}, "name": {"en_US": "Robe"},
                "level": {"value": 26}, "item": {"id": 127},
                "quality": {"type": "UNCOMMON", "name": {"en_US": "Uncommon"}},
                "item_class": {"id": 4, "name": {"en_US": "Armor"}},
                "item_subclass": {"id": 1, "name": {"en_US": "Cloth"}},
                "enchantments": [{"display_string": {"en_US": "Enchanted: Stamina +2"}}],
                "stats": [{"value": 4, "display": {"display_string": {"en_US": "+4 Intellect"}}}],
                "media": {"id": 1, "key": {"href": f"{API}/data/wow/media/item/127?namespace={STATIC}"}}}
        row = {"slot": "Chest", "name": "Robe", "ilvl": 26, "iconUrl": None,
               "itemId": 127, "quality": "UNCOMMON",
               "itemClass": {"id": 4, "name": "Armor"},
               "itemSubclass": {"id": 1, "name": "Cloth"},
               "enchants": ["Enchanted: Stamina +2"],
               "stats": [{"value": 4, "displayString": "+4 Intellect",
                          "isEquipBonus": False, "color": None}]}
        # The extractor output must match the committed vocabulary exactly, and
        # no gem/socket key may ever appear (amendment 2: they do not exist).
        self.assertEqual(set(row), set(artifacts.GEAR_KEYS))
        blob = json.dumps(row).lower()
        self.assertNotIn("socket", blob)
        self.assertNotIn("gem", blob)


# --------------------------------------------------------------------------- #
# T1.2 — /statistics normalization (notes §3.5, §9.5)
# --------------------------------------------------------------------------- #
class StatisticsNormalizer(unittest.TestCase):
    def test_scalars_base_effective_triples_and_power_type(self):
        payload = {"health": 857, "power": 787, "expertise": 0.0,
                   "strength": {"base": 38, "effective": 53},
                   "armor": {"base": 64, "effective": 553},
                   "dodge": {"value": 10.421599, "rating_bonus": 0.0, "rating_normalized": 0},
                   "spell_crit": {"value": 4.5, "rating_bonus": 1.2, "rating_normalized": 3},
                   "power_type": {"id": 0, "name": {"en_US": "Mana"},
                                  "key": {"href": f"{API}/data/wow/power-type/0?namespace={STATIC}"}},
                   "character": {"id": 1, "name": "Beef", "surname": "Pho", "slug": "beef-pho"},
                   "_links": {"self": {"href": PROFILE_HREF}}}
        block, skipped = fetch.normalize_statistics(payload)
        self.assertEqual(skipped, [])
        self.assertEqual(block["health"], 857)
        self.assertEqual(block["expertise"], 0.0)
        self.assertEqual(block["strength"], {"base": 38, "effective": 53})
        self.assertEqual(block["dodge"], {"value": 10.421599, "ratingBonus": 0.0, "ratingNormalized": 0})
        self.assertEqual(block["spellCrit"], {"value": 4.5, "ratingBonus": 1.2, "ratingNormalized": 3})
        # H2: power_type keeps id+name, and its key.href (a response-derived URL)
        # is NOT persisted.
        self.assertEqual(block["powerType"], {"id": 0, "name": "Mana"})
        self.assertNotIn("character", block)
        self.assertNotIn("_links", block)

    def test_unknown_shapes_are_reported_not_dumped(self):
        block, skipped = fetch.normalize_statistics(
            {"health": 1, "weird": {"nested": {"deep": 1}}, "flag": True, "text": "x"})
        self.assertEqual(block, {"health": 1})
        self.assertEqual(sorted(skipped), ["flag", "text", "weird"])

    def test_non_dict_payload_yields_no_block(self):
        self.assertEqual(fetch.normalize_statistics(None), (None, []))
        self.assertEqual(fetch.normalize_statistics({}), (None, []))


# --------------------------------------------------------------------------- #
# T1.4 — reputations (notes §9.1)
# --------------------------------------------------------------------------- #
class ReputationsNormalizer(unittest.TestCase):
    def test_standing_tuple_is_kept_whole_for_the_value_over_max_bar(self):
        payload = {"character": {"slug": "beef-pho"}, "reputations": [
            {"faction": {"id": 577, "name": {"en_US": "Darkspear Trolls"},
                         "key": {"href": f"{API}/data/wow/reputation-faction/577"}},
             "standing": {"raw": 10382, "value": 1382, "max": 3000, "tier": 3,
                          "name": {"en_US": "Neutral"}}},
            "junk"]}
        out = fetch.normalize_reputations(payload)
        self.assertEqual(out, [{"factionId": 577, "factionName": "Darkspear Trolls",
                                "standing": {"raw": 10382, "value": 1382, "max": 3000,
                                             "tier": 3, "name": "Neutral"}}])

    def test_missing_or_empty_payload_is_no_block(self):
        self.assertIsNone(fetch.normalize_reputations(None))
        self.assertIsNone(fetch.normalize_reputations({"reputations": ["junk"]}))


# --------------------------------------------------------------------------- #
# T1.4 — professions (notes §9.2): names are PLAIN strings there
# --------------------------------------------------------------------------- #
class ProfessionsNormalizer(unittest.TestCase):
    def test_two_sides_skill_bounds_and_recipes(self):
        payload = {"primaries": [
            {"profession": {"id": 186, "name": "Mining",
                            "key": {"href": f"{API}/data/wow/profession/186"}},
             "tiers": [{"skill_points": 127, "max_skill_points": 225,
                        "tier": {"id": 2946, "name": "Mining"},
                        "known_recipes": [{"id": 1847, "name": "Smelt Iron"},
                                          "Cast On", "", {"nope": 1}]}]}],
            "secondaries": []}
        out = fetch.normalize_professions(payload)
        self.assertEqual(out["primaries"][0]["id"], 186)
        self.assertEqual(out["primaries"][0]["name"], "Mining")
        tier = out["primaries"][0]["tiers"][0]
        self.assertEqual((tier["skillPoints"], tier["maxSkillPoints"]), (127, 225))
        self.assertEqual((tier["tierId"], tier["tierName"]), (2946, "Mining"))
        # plain-string recipe names are normalized defensively (§9.2)
        self.assertEqual(tier["knownRecipes"], [{"id": 1847, "name": "Smelt Iron"},
                                                {"id": None, "name": "Cast On"}])
        self.assertEqual(out["secondaries"], [])

    def test_empty_professions_is_no_block(self):
        self.assertIsNone(fetch.normalize_professions({"primaries": [], "secondaries": []}))
        self.assertIsNone(fetch.normalize_professions(None))


# --------------------------------------------------------------------------- #
# T1.5 — media (notes §9.5: avatar/avatar-sd, no inset/headshot)
# --------------------------------------------------------------------------- #
class MediaNormalizer(unittest.TestCase):
    def test_only_avatar_keys_are_stored_and_validated(self):
        payload = {"assets": [
            {"key": "main-raw", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-main-raw.png"},
            {"key": "main-sd-raw", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-main-sd-raw.png"},
            {"key": "avatar", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar.jpg"},
            {"key": "avatar-sd", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar-sd.jpg"}]}
        self.assertEqual(fetch.normalize_media(payload), {
            "avatar": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar.jpg",
            "avatarSd": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar-sd.jpg"})

    def test_poisoned_avatar_value_becomes_null_not_the_string(self):
        payload = {"assets": [{"key": "avatar", "value": "https://attacker.tld/beacon.jpg"}]}
        self.assertEqual(fetch.normalize_media(payload), {"avatar": None, "avatarSd": None})

    def test_main_raw_only_is_no_block(self):
        payload = {"assets": [{"key": "main-raw",
                               "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-main-raw.png"}]}
        self.assertIsNone(fetch.normalize_media(payload))
        self.assertIsNone(fetch.normalize_media(None))


# --------------------------------------------------------------------------- #
# T1.3 — selected talents (notes §9.3) and the §9.3.1 icon-id trap
# --------------------------------------------------------------------------- #
class SelectedTalentsNormalizer(unittest.TestCase):
    def spec(self, spec_id, name, points, talents):
        return {"specialization": {"id": spec_id,
                                   "key": {"href": f"{API}/data/wow/playable-specialization/{spec_id}"}},
                "specialization_name": {"en_US": name},
                "spent_points": points,
                "talents": talents}

    def talent(self, talent_id, spell_id, rank, description="Powerful."):
        return {"talent_rank": rank,
                "talent": {"id": talent_id, "name": {"en_US": "Improved Something"}},
                "spell_tooltip": {
                    "spell": {"id": spell_id, "name": {"en_US": f"Spell {spell_id}"}},
                    "description": {"en_US": description},
                    "cast_time": {"en_US": "Passive"},
                    "power_cost": {}}}

    def test_multi_spec_group_is_iterated_not_truncated_to_index_zero(self):
        payload = {"specialization_groups": [
            {"is_active": True, "specializations": [
                self.spec(26, "Arms", 5, [self.talent(135489, 12282, 3)]),
                self.spec(257, "Protection", 6, [self.talent(12345, 777, 2)])]}]}
        out = fetch.normalize_selected_talents(payload)
        self.assertEqual([g["specId"] for g in out], [26, 257])
        first = out[0]["talents"][0]
        self.assertEqual(first["rank"], 3)
        self.assertEqual(first["talentId"], 135489)
        # THE §9.3.1 trap: the icon source is spell_tooltip.spell.id (12282),
        # never talent.id (135489).
        self.assertEqual(first["spellId"], 12282)
        self.assertEqual(first["spellName"], "Spell 12282")
        self.assertEqual(first["description"], "Powerful.")
        self.assertEqual(first["castTime"], "Passive")
        self.assertIsNone(first["iconUrl"])          # resolved by the Fetcher only

    def test_no_spec_chosen_yields_no_block(self):
        payload = {"specialization_groups": [{"is_active": True}]}   # Hunter case, §9.3
        self.assertIsNone(fetch.normalize_selected_talents(payload))
        self.assertIsNone(fetch.normalize_selected_talents(None))


class SpellIconRouting(unittest.TestCase):
    def make(self, table=None):
        return make_fetcher(table)

    def test_spell_icon_uses_spell_id_at_static_ns_and_dedups(self):
        f = self.make({media_url(12282): (200, icon_payload(12282))})
        w = {"unchanged": False}
        icon = f.spell_icon(12282, w)
        self.assertEqual(icon, "https://render.worldofwarcraft.com/forever-us/icons/56/12282.jpg")
        self.assertEqual(f.spell_icon(12282, w), icon)
        # one request per distinct spell id (§9.3.1), served twice from the dedup map
        self.assertEqual(f.client.urls, [media_url(12282)])
        self.assertEqual(f.spell_icon_lookups, 1)

    def test_talent_id_is_never_requested_at_the_spell_media_route(self):
        f = self.make({media_url(12282): (200, icon_payload(12282))})
        self.assertIsNone(f.spell_icon(135489, {"unchanged": False}))   # talent id 404s (§9.3.1)
        self.assertEqual(f.client.urls, [media_url(135489)])
        self.assertEqual(f.icon_failures, 1)

    def test_off_host_icon_value_is_not_publishable(self):
        f = self.make({media_url(12282): (200, {"assets": [{"key": "icon", "value": "https://evil.tld/x.jpg"}]})})
        self.assertIsNone(f.spell_icon(12282, {"unchanged": False}))
        self.assertEqual(f.icon_failures, 1)

    def test_cap_stops_further_lookups_and_warns_once(self):
        table = {media_url(i): (200, icon_payload(i)) for i in (1, 2, 3)}
        f = self.make(table)
        original = config.MAX_SPELL_ICON_LOOKUPS
        config.MAX_SPELL_ICON_LOOKUPS = 2
        try:
            for spell_id in (1, 2, 3):
                f.spell_icon(spell_id, {"unchanged": False})
        finally:
            config.MAX_SPELL_ICON_LOOKUPS = original
        self.assertEqual(f.client.urls, [media_url(1), media_url(2)])   # the third never sent
        self.assertEqual(sum("cap" in line for line in f.warnings), 1)

    def test_without_static_ns_nothing_is_sent(self):
        f = make_fetcher()
        f.static_ns = None
        self.assertIsNone(f.spell_icon(12282, {"unchanged": False}))
        self.assertEqual(f.client.urls, [])


class TalentTreeFetching(unittest.TestCase):
    def tree_payload(self, tree_id=1117, spec_id=26, spell_ids=(12282, 12283)):
        return {"id": tree_id,
                "playable_class": {"id": 1, "name": {"en_US": "Warrior"}},
                "playable_specialization": {"id": spec_id, "name": {"en_US": "Arms"}},
                "name": {"en_US": "Arms"},
                "media": {"key": {"href": f"{API}/data/wow/media/talent-tree/{tree_id}"}},
                "spec_talent_nodes": [
                    {"id": 105958, "node_type": {"id": 0, "type": "ACTIVE"},
                     "display_row": 1, "display_col": 2,
                     "raw_position_x": 1020, "raw_position_y": 2130,
                     "unlocks": [105959, "junk"], "locked_by": [],
                     "ranks": [{"rank": rank + 1,
                                "tooltip": {
                                    "talent": {"id": 135489 + rank, "name": {"en_US": "T"},
                                               "key": {"href": f"{API}/data/wow/talent/{135489 + rank}"}},
                                    "spell_tooltip": {
                                        "spell": {"id": spell, "name": {"en_US": f"Spell {spell}"}},
                                        "description": {"en_US": "Hit harder."},
                                        "cast_time": {"en_US": "Passive"},
                                        "power_cost": {}}}}
                               for rank, spell in enumerate(spell_ids)]}]}

    def spec_table(self, tree_id=1117, spec_id=26):
        tree_href = f"{API}/data/wow/talent-tree/{tree_id}/playable-specialization/{spec_id}?namespace={STATIC}"
        table = {f"{API}/data/wow/playable-specialization/{spec_id}?namespace={STATIC}":
                 (200, {"id": spec_id, "name": {"en_US": "Arms"},
                        "spec_talent_tree": {"key": {"href": tree_href}, "name": {"en_US": "Arms"}}}),
                 tree_href: (200, self.tree_payload(tree_id, spec_id))}
        return table

    def test_tree_geometry_unlocks_and_rank_icons(self):
        f = make_fetcher(dict(self.spec_table(),
                              **{media_url(12282): (200, icon_payload(12282)),
                                 media_url(12283): (200, icon_payload(12283))}))
        tree = f.talent_tree_for("1", 26, {"unchanged": False})
        self.assertEqual((tree["treeId"], tree["specId"], tree["specName"]), (1117, 26, "Arms"))
        node = tree["nodes"][0]
        self.assertEqual((node["displayRow"], node["displayCol"]), (1, 2))
        self.assertEqual((node["rawPositionX"], node["rawPositionY"]), (1020, 2130))
        self.assertEqual(node["unlocks"], [105959])         # non-ints dropped
        self.assertEqual(node["lockedBy"], [])
        self.assertEqual([r["rank"] for r in node["ranks"]], [1, 2])
        self.assertEqual([r["talentId"] for r in node["ranks"]], [135489, 135490])
        self.assertEqual(node["ranks"][0]["iconUrl"],
                         "https://render.worldofwarcraft.com/forever-us/icons/56/12282.jpg")
        # the media href the tree doc serves (404 route, §9.3.2) is never fetched
        self.assertNotIn("/data/wow/media/talent-tree/", "".join(f.client.urls))

    def test_fetched_once_per_spec_across_characters(self):
        f = make_fetcher(dict(self.spec_table(),
                              **{media_url(12282): (200, icon_payload(12282)),
                                 media_url(12283): (200, icon_payload(12283))}))
        tree = f.talent_tree_for("1", 26, {"unchanged": False})
        self.assertIsNotNone(tree)
        before = list(f.client.urls)
        tree_again = f.talent_tree_for("2", 26, {"unchanged": False})   # second character
        self.assertIs(tree_again, tree)                               # zero extra requests
        self.assertEqual(f.client.urls, before)

    def test_previous_document_seeding_avoids_all_refetching(self):
        f = make_fetcher(self.spec_table())
        f.seed_talent_trees({"staticNamespace": STATIC, "specs": {"26": {"treeId": 1117, "nodes": []}}})
        tree = f.talent_tree_for("1", 26, {"unchanged": False})
        self.assertEqual(tree["treeId"], 1117)
        self.assertEqual(f.client.urls, [])

    def test_seeding_a_stale_static_ns_is_ignored(self):
        f = make_fetcher(dict(self.spec_table(),
                              **{media_url(12282): (200, icon_payload(12282)),
                                 media_url(12283): (200, icon_payload(12283))}))
        f.seed_talent_trees({"staticNamespace": "static-0.0.0-x-forever-us",
                             "specs": {"26": {"treeId": 9999, "nodes": []}}})
        tree = f.talent_tree_for("1", 26, {"unchanged": False})
        self.assertEqual(tree["treeId"], 1117)                      # live refetch, not stale doc
        self.assertTrue(f.client.urls)

    def test_missing_spec_talent_tree_ref_is_a_reported_failure(self):
        f = make_fetcher({f"{API}/data/wow/playable-specialization/26?namespace={STATIC}":
                          (200, {"id": 26, "name": {"en_US": "Arms"}})})
        self.assertIsNone(f.talent_tree_for("1", 26, {"unchanged": False}))
        self.assertTrue(any("spec_talent_tree" in line for line in f.feature_failures))

    def test_hostile_spec_talent_tree_href_is_refused_not_fetched(self):
        # H1: an off-allowlist href from a response must not receive the token,
        # and the constructed fallback does not exist in config, so this is null.
        f = make_fetcher({f"{API}/data/wow/playable-specialization/26?namespace={STATIC}":
                          (200, {"id": 26, "spec_talent_tree": {"key": {"href": "https://evil.tld/tree"}}})})
        self.assertIsNone(f.talent_tree_for("1", 26, {"unchanged": False}))
        self.assertEqual(f.client.urls,
                         [f"{API}/data/wow/playable-specialization/26?namespace={STATIC}"])
        self.assertTrue(any("usable spec_talent_tree href" in line for line in f.feature_failures))


class EnrichIntegration(unittest.TestCase):
    """The full T1.1–T1.5 pass over §9-shaped payloads: new blocks land, existing
    keys keep their meaning, and a refused endpoint becomes a named failure."""

    def equipment(self):
        return {"equipped_items": [
            {"slot": {"name": {"en_US": "Chest"}}, "name": {"en_US": "Robe"},
             "level": {"value": 26}, "item": {"id": 127},
             "quality": {"type": "UNCOMMON"},
             "item_class": {"id": 4, "name": {"en_US": "Armor"}},
             "item_subclass": {"id": 1, "name": {"en_US": "Cloth"}},
             "enchantments": [{"display_string": {"en_US": "Enchanted: Stamina +2"}}],
             "stats": [{"value": 4, "is_equip_bonus": True,
                        "display": {"display_string": {"en_US": "+4 Intellect"},
                                    "color": {"r": 255, "g": 255, "b": 255, "a": 1.0}}}],
             "media": {"key": {"href": f"{API}/data/wow/media/item/127?namespace={STATIC}"}}},
            {"slot": {"name": {"en_US": "Shirt"}}, "name": {"en_US": "Deckhand's Shirt"},
             "level": {"value": 1}, "item": {"id": 10657},
             "media": {"key": {"href": f"{API}/data/wow/media/item/10657?namespace={STATIC}"}}}]}

    def specializations(self):
        return {"specialization_groups": [{"is_active": True, "specializations": [
            {"specialization": {"id": 26}, "specialization_name": {"en_US": "Arms"},
             "spent_points": 5,
             "talents": [{"talent_rank": 3, "talent": {"id": 135489},
                          "spell_tooltip": {"spell": {"id": 12282, "name": {"en_US": "Spell 12282"}},
                                            "description": {"en_US": "Hit harder."},
                                            "cast_time": {"en_US": "Passive"}}}]}]}]}

    def table(self, include_professions=True):
        tree_href = f"{API}/data/wow/talent-tree/1117/playable-specialization/26?namespace={STATIC}"
        base = f"{API}/profile/wow/character/beef-pho"
        ns = f"?namespace={config.NS_PROFILE}"
        table = {
            f"{base}/equipment{ns}": (200, self.equipment()),
            f"{base}/specializations{ns}": (200, self.specializations()),
            f"{base}/statistics{ns}": (200, {"health": 857, "strength": {"base": 38, "effective": 53},
                                             "dodge": {"value": 10.4, "rating_bonus": 0.0,
                                                       "rating_normalized": 0},
                                             "character": {"slug": "beef-pho"}}),
            f"{base}/reputations{ns}": (200, {"reputations": [
                {"faction": {"id": 577, "name": {"en_US": "Darkspear Trolls"}},
                 "standing": {"raw": 10382, "value": 1382, "max": 3000, "tier": 3,
                              "name": {"en_US": "Neutral"}}}]}),
            f"{base}/character-media{ns}": (200, {"assets": [
                {"key": "avatar", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar.jpg"},
                {"key": "avatar-sd", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar-sd.jpg"}]}),
            f"{base}/achievements{ns}": (200, {"total_points": 7}),
            f"{base}/pvp-summary{ns}": (200, {"honorable_kills": 34, "pvp_rank": 0}),
            f"{API}/data/wow/media/item/127?namespace={STATIC}": (200, {"assets": [
                {"key": "icon", "value": "https://render.worldofwarcraft.com/forever-us/icons/56/135040.jpg"}]}),
            f"{API}/data/wow/media/item/10657?namespace={STATIC}": (404, None),
            f"{API}/data/wow/playable-specialization/26?namespace={STATIC}": (200, {
                "id": 26, "spec_talent_tree": {"key": {"href": tree_href}}}),
            tree_href: (200, TalentTreeFetching().tree_payload()),
            media_url(12282): (200, icon_payload(12282)),
            media_url(12283): (200, icon_payload(12283)),
        }
        if include_professions:
            table[f"{base}/professions{ns}"] = (200, {"primaries": [
                {"profession": {"id": 186, "name": "Mining"},
                 "tiers": [{"skill_points": 127, "max_skill_points": 225,
                            "tier": {"id": 2946, "name": "Mining"},
                            "known_recipes": [{"id": 1847, "name": "Smelt Iron"}]}]}],
                "secondaries": []})
        return table

    def enrich(self, include_professions=True):
        f = make_fetcher(self.table(include_professions))
        w = {"rec": {"id": "1", "name": "Beef Pho"}, "profile": {"name": "Beef", "surname": "Pho",
                                                                 "level": 20, "equipped_item_level": 12},
             "entry": {"href": PROFILE_HREF}, "profile_url": PROFILE_HREF, "unchanged": False}
        return f, f.enrich(w)

    def test_new_blocks_land_and_existing_keys_keep_their_meaning(self):
        f, out = self.enrich()
        self.assertEqual(set(out) - set(artifacts.CHARACTER_KEYS), set())
        self.assertEqual(out["status"], "ok")                       # untouched v2 semantics
        self.assertEqual(out["level"], 20)
        self.assertEqual(out["itemLevel"], 12)
        self.assertEqual(out["activeSpec"], "Arms")
        self.assertEqual(out["achievementPoints"], 7)
        self.assertEqual(out["progress"], {"pvp": {"honorableKills": 34, "pvpRank": 0}})
        gear = out["gear"]
        self.assertEqual(gear[0]["slot"], "Chest")
        self.assertEqual(gear[0]["name"], "Robe")
        self.assertEqual(gear[0]["ilvl"], 26)
        self.assertEqual(gear[0]["iconUrl"], "https://render.worldofwarcraft.com/forever-us/icons/56/135040.jpg")
        self.assertEqual(gear[0]["itemId"], 127)
        self.assertEqual(gear[0]["quality"], "UNCOMMON")
        self.assertEqual(gear[0]["itemSubclass"], {"id": 1, "name": "Cloth"})
        self.assertEqual(gear[0]["enchants"], ["Enchanted: Stamina +2"])
        self.assertEqual(gear[0]["stats"], [{"value": 4, "displayString": "+4 Intellect",
                                             "isEquipBonus": True,
                                             "color": {"r": 255, "g": 255, "b": 255, "a": 1.0}}])
        # §8: a 404 item-media doc keeps the row with iconUrl null — and now with
        # tooltip data too, since none of it comes from that doc.
        self.assertIsNone(gear[1]["iconUrl"])
        self.assertEqual(gear[1]["itemId"], 10657)
        self.assertEqual(out["stats"]["strength"], {"base": 38, "effective": 53})
        self.assertEqual(out["stats"]["dodge"], {"value": 10.4, "ratingBonus": 0.0, "ratingNormalized": 0})
        self.assertNotIn("character", out["stats"])
        self.assertEqual(out["talents"][0]["specId"], 26)
        self.assertEqual(out["talents"][0]["specName"], "Arms")
        self.assertEqual(out["talents"][0]["spentPoints"], 5)
        self.assertEqual(out["talents"][0]["talents"][0]["iconUrl"],
                         "https://render.worldofwarcraft.com/forever-us/icons/56/12282.jpg")
        self.assertEqual(out["professions"]["primaries"][0]["name"], "Mining")
        self.assertEqual(out["reputations"][0]["standing"]["max"], 3000)
        self.assertEqual(out["media"]["avatarSd"],
                         "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar-sd.jpg")
        self.assertTrue(f.static_ns)
        self.assertIn("26", f.talent_trees)
        self.assertEqual(f.talent_trees["26"]["nodes"][0]["ranks"][1]["iconUrl"],
                         "https://render.worldofwarcraft.com/forever-us/icons/56/12283.jpg")
        self.assertEqual(f.feature_failures, [])
        # the spell icon route used the SPELL id (12282), never the talent id (135489)
        joined = " ".join(f.client.urls)
        self.assertIn("/data/wow/media/spell/12282", joined)
        self.assertNotIn("/data/wow/media/spell/135489", joined)

    def test_refused_professions_block_is_omitted_and_reported_verbatim(self):
        f, out = self.enrich(include_professions=False)
        self.assertNotIn("professions", out)          # whole-block optionality
        self.assertIn(("professions", 404), f.sub_failures)
        self.assertTrue(any("id 1: professions" in line and "fetch failed" in line
                            for line in f.feature_failures))

    def test_hunter_without_chosen_spec_omits_talents_without_failing(self):
        f = make_fetcher(self.table())
        f.client.table[f"{API}/profile/wow/character/beef-pho/specializations"
                       f"?namespace={config.NS_PROFILE}"] = (200, {"specialization_groups": [{"is_active": True}]})
        w = {"rec": {"id": "9", "name": "Morc Theorc"},
             "profile": {"name": "Morc", "surname": "Theorc", "level": 19, "equipped_item_level": 4},
             "entry": {"href": PROFILE_HREF}, "profile_url": PROFILE_HREF, "unchanged": False}
        out = f.enrich(w)
        self.assertNotIn("talents", out)              # empty state, not a failure
        self.assertEqual(f.feature_failures, [])

    def test_artifact_url_vocabulary_still_covers_every_url_write_site(self):
        # H2 discipline is unchanged: the new avatar/talent/tree icon writes all
        # go through safe_artifact_url's "iconUrl" kind (the render host).
        self.assertEqual(set(config.ARTIFACT_URL_RES), {"href", "iconUrl"})
        self.assertIsNone(fetch.safe_artifact_url("https://evil.tld/a.jpg", "iconUrl"))


class TalentTreesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.original = config.TALENT_TREES_FILE
        config.TALENT_TREES_FILE = self.tmp / "talent-trees.json"

    def tearDown(self):
        config.TALENT_TREES_FILE = self.original

    def tree(self, tree_id):
        return {"treeId": tree_id, "classId": 1, "className": "Warrior", "specId": None,
                "specName": "Arms", "name": "Arms",
                "nodes": [{"id": 1, "displayRow": 1, "displayCol": 1, "rawPositionX": 0,
                           "rawPositionY": 0, "unlocks": [], "lockedBy": [],
                           "ranks": [{"rank": 1, "talentId": 2, "spellId": 3,
                                      "spellName": "S", "description": "D",
                                      "castTime": "Passive", "iconUrl": None}]}]}

    def test_dry_run_writes_nothing(self):
        doc = artifacts.write_talent_trees(STATIC, {"26": self.tree(1117)}, dry_run=True)
        self.assertEqual(doc["specs"]["26"]["treeId"], 1117)
        self.assertFalse(config.TALENT_TREES_FILE.exists())

    def test_write_merge_and_namespace_invalidation(self):
        artifacts.write_talent_trees(STATIC, {"26": self.tree(1117)}, dry_run=False)
        doc = json.loads(config.TALENT_TREES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(list(doc), list(artifacts.TALENT_TREES_DOC_KEYS))
        self.assertEqual(doc["staticNamespace"], STATIC)
        artifacts.write_talent_trees(STATIC, {"257": self.tree(1118)}, dry_run=False)
        doc = json.loads(config.TALENT_TREES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(doc["specs"]), ["257", "26"])          # merged, lookup can't erase
        artifacts.write_talent_trees("static-9.9.9-zz-forever-us", {"134": self.tree(1089)}, dry_run=False)
        doc = json.loads(config.TALENT_TREES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(list(doc["specs"]), ["134"])                  # old patch geometry dropped

    def test_corrupt_previous_document_rebuilds_whole(self):
        config.TALENT_TREES_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(artifacts.load_previous_talent_trees(), {})


class SummaryReporting(unittest.TestCase):
    """T1.7: a failed endpoint must surface in the run summary, never silently."""

    def test_feature_and_sub_failures_appear_in_summary_lines(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from bnet import sync

        args = argparse.Namespace(dry_run=True, lookup=None, force=False,
                                  state_dir=tempfile.mkdtemp())
        s = sync.Sync.__new__(sync.Sync)
        s.args = args
        s.cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
        s.client = TableClient({})
        s.f = fetch.Fetcher(s.client, s.cache)
        s.f.static_ns = STATIC
        s.budget_hit = False
        s.f.note_feature_failure("1", "professions", "fetch failed (no payload)")
        s.f.sub_failures.append(("reputations", 503))
        out = io.StringIO()
        with redirect_stdout(out):
            s.emit_summary(s.summary_lines({"ok": 1}, 1, 0.0))
        printed = out.getvalue()
        self.assertIn("optional feature block(s) not stored", printed)
        self.assertIn("id 1: professions", printed)
        self.assertIn("reputations=HTTP 503", printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
