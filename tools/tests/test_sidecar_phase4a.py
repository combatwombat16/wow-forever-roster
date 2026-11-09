#!/usr/bin/env python3
"""Offline unit tests for the Phase 4a sync/data extension (notes §10).

Fixtures mirror the payload shapes the three §10 sweeps recorded — the profile
``race`` / ``character_class`` references of §3.1, the §9.2 profession entries, and
the `/data/wow/media/<kind>/{id}` media docs (class and profession AVAILABLE, race
UNAVAILABLE) — so both halves of the contract are pinned here: what the sync may
publish, and what it must refuse to invent. Stdlib unittest only: no network, no
credentials, no committed artifacts (the cache redirects to a temp dir).

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/

from bnet import artifacts, config, fetch, http  # noqa: E402

STATIC = "static-1.60.1_70399-forever-us"
API = config.API_BASE
RENDER = "https://render.worldofwarcraft.com/forever-us/icons/56/"
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
        self.heads = []               # Phase B render probes, kept out of urls

    def get(self, url):
        self.urls.append(url)
        status, payload = self.table.get(url, (404, None))
        return Resp(status, payload)

    def head(self, url):
        self.heads.append(url)
        status, payload = self.table.get(url, (404, None))
        return Resp(status, payload)


def make_fetcher(table=None):
    cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
    f = fetch.Fetcher(TableClient(table), cache)
    f.static_ns = STATIC          # normally captured from the first profile
    return f


def class_media_url(class_id):
    return f"{API}/data/wow/media/playable-class/{class_id}?namespace={STATIC}"


def profession_media_url(profession_id):
    return f"{API}/data/wow/media/profession/{profession_id}?namespace={STATIC}"


def icon_payload(file_data_id):
    return {"id": 1, "assets": [{"key": "icon", "file_data_id": file_data_id,
                                 "value": f"{RENDER}{file_data_id}.jpg"}]}


# --------------------------------------------------------------------------- #
# 10.4.1 — race + class identity off the profile payload the sync already has
# --------------------------------------------------------------------------- #
class IdentityExtractor(unittest.TestCase):
    def profile(self, **overrides):
        """The §3.1 shape: race is a plain ref, class is keyed character_class."""
        profile = {"name": "Shiftybeef", "surname": "Stroganoff", "level": 22,
                   "race": {"id": 6, "name": {"en_US": "Tauren", "de_DE": "Tauren"},
                            "key": {"href": f"{API}/data/wow/playable-race/6?namespace={STATIC}"}},
                   "character_class": {"id": 11, "name": {"en_US": "Druid"},
                                       "key": {"href": f"{API}/data/wow/playable-class/11?namespace={STATIC}"}}}
        profile.update(overrides)
        return profile

    def test_both_refs_land_and_class_comes_from_character_class(self):
        block, malformed = fetch.extract_identity(self.profile())
        self.assertEqual(block, {"race": {"id": 6, "name": "Tauren"},
                                 "class": {"id": 11, "name": "Druid"}})
        self.assertEqual(malformed, [])

    def test_plain_string_names_survive_the_locale_reader(self):
        profile = self.profile(race={"id": 2, "name": "Orc"})
        block, malformed = fetch.extract_identity(profile)
        self.assertEqual(block["race"], {"id": 2, "name": "Orc"})
        self.assertEqual(malformed, [])

    def test_absent_refs_are_absent_not_malformed(self):
        block, malformed = fetch.extract_identity({"name": "Morc"})
        self.assertEqual(block, {})
        self.assertEqual(malformed, [])

    def test_half_populated_and_garbage_refs_are_skipped_and_reported(self):
        for key, value in (("race", {"id": 6}),                                  # no name
                           ("race", {"name": {"en_US": "Tauren"}}),              # no id
                           ("race", {"id": True, "name": {"en_US": "Tauren"}}),  # bool is not an id
                           ("race", "Tauren"),                                   # type lie
                           ("race", []),
                           ("character_class", {"id": 11}),
                           ("character_class", {"name": {"en_US": "Druid"}})):
            profile = self.profile()
            profile[key] = value
            block, malformed = fetch.extract_identity(profile)
            sidecar_key = "race" if key == "race" else "class"
            self.assertNotIn(sidecar_key, block, f"{key}={value!r} must not be stored")
            self.assertEqual(malformed, [key], f"{key}={value!r} must be reported by payload key")

    def test_non_dict_profile_is_reported_not_dumped(self):
        self.assertEqual(fetch.extract_identity(None), ({}, ["profile"]))
        self.assertEqual(fetch.extract_identity(["race"]), ({}, ["profile"]))

    def test_nothing_is_renamed_into_the_legacy_roster_shape(self):
        # pi-warden's characters.json keeps race/class as strings; the sidecar is
        # the API's own record, so the pair — never a bare name — is what ships.
        block, _ = fetch.extract_identity(self.profile())
        self.assertEqual(set(block["race"]), set(artifacts.IDENTITY_KEYS))
        self.assertEqual(set(block["class"]), set(artifacts.IDENTITY_KEYS))


# --------------------------------------------------------------------------- #
# 10.1 / 10.4.4 — profession icons (both arrays), cached, gated, null on 404
# --------------------------------------------------------------------------- #
class ProfessionIcons(unittest.TestCase):
    def payload(self):
        return {"primaries": [
            {"profession": {"id": 186, "name": "Mining",
                            "key": {"href": f"{API}/data/wow/profession/186"}},
             "tiers": [{"skill_points": 127, "max_skill_points": 225,
                        "tier": {"id": 2946, "name": "Mining"},
                        "known_recipes": [{"id": 1847, "name": "Smelt Iron"}]}]}],
            "secondaries": [
                {"profession": {"id": 185, "name": "Cooking"}, "tiers": []}]}

    def test_extractor_publishes_the_key_but_never_invents_a_url(self):
        out = fetch.normalize_professions(self.payload())
        for side in out["primaries"] + out["secondaries"]:
            self.assertIn("iconUrl", side)
            self.assertIsNone(side["iconUrl"])   # only the Fetcher can know a URL

    def test_both_arrays_get_their_icon_and_one_request_per_distinct_id(self):
        f = make_fetcher({profession_media_url(186): (200, icon_payload(136248)),
                          profession_media_url(185): (200, icon_payload(133971))})
        w = {"unchanged": False}
        first = f.profession_icon(186, w)
        second = f.profession_icon(185, w)
        self.assertEqual(first, f"{RENDER}136248.jpg")
        self.assertEqual(second, f"{RENDER}133971.jpg")
        again = f.profession_icon(186, w)                       # second character, same id
        self.assertEqual(again, first)
        self.assertEqual(f.client.urls, [profession_media_url(186), profession_media_url(185)])

    def test_404_is_null_and_counted_not_raised(self):
        # §8: one reference-doc 404 is non-fatal; the row survives without its icon.
        f = make_fetcher({})
        self.assertIsNone(f.profession_icon(186, {"unchanged": False}))
        self.assertEqual(f.icon_failures, 1)

    def test_off_host_asset_is_refused_and_never_published(self):
        f = make_fetcher({profession_media_url(186):
                          (200, {"assets": [{"key": "icon", "value": "https://attacker.tld/beacon.jpg"}]})})
        self.assertIsNone(f.profession_icon(186, {"unchanged": False}))
        self.assertEqual(f.icon_failures, 1)

    def test_without_a_static_namespace_nothing_is_sent(self):
        f = make_fetcher({profession_media_url(186): (200, icon_payload(136248))})
        f.static_ns = None
        self.assertIsNone(f.profession_icon(186, {"unchanged": False}))
        self.assertEqual(f.client.urls, [])

    def test_missing_or_bool_id_sends_nothing(self):
        f = make_fetcher({})
        self.assertIsNone(f.profession_icon(None, {"unchanged": False}))
        self.assertIsNone(f.profession_icon(True, {"unchanged": False}))
        self.assertEqual(f.client.urls, [])

    def test_cached_unchanged_document_reuses_the_body_and_still_gates_it(self):
        f = make_fetcher({profession_media_url(186):
                          (200, {"assets": [{"key": "icon", "value": "https://attacker.tld/x.jpg"}]})})
        f.cache.urls[profession_media_url(186)] = {
            "body": f.client.table[profession_media_url(186)][1], "lastModified": "x"}
        self.assertIsNone(f.profession_icon(186, {"unchanged": True}))
        self.assertEqual(f.client.urls, [])           # served from the cache, then validated
        self.assertEqual(f.saved_requests, 1)


# --------------------------------------------------------------------------- #
# 10.2 — playable-class icons
# --------------------------------------------------------------------------- #
class ClassIcons(unittest.TestCase):
    def test_class_icon_uses_the_prefix_route_confirmed_by_the_probe(self):
        f = make_fetcher({class_media_url(1): (200, icon_payload(626008))})
        self.assertEqual(f.class_icon(1, {"unchanged": False}), f"{RENDER}626008.jpg")
        self.assertEqual(f.client.urls, [class_media_url(1)])

    def test_class_icons_dedup_across_characters_of_the_same_class(self):
        f = make_fetcher({class_media_url(3): (200, icon_payload(626000))})
        first, second = f.class_icon(3, {"unchanged": False}), f.class_icon(3, {"unchanged": False})
        self.assertEqual((first, second), (f"{RENDER}626000.jpg",) * 2)
        self.assertEqual(f.client.urls, [class_media_url(3)])

    def test_404_and_poisoned_values_store_null(self):
        f = make_fetcher({class_media_url(2):
                          (200, {"assets": [{"key": "icon", "value": "https://evil.tld/y.jpg"}]})})
        self.assertIsNone(f.class_icon(2, {"unchanged": False}))
        self.assertIsNone(f.class_icon(5, {"unchanged": False}))
        self.assertEqual(f.icon_failures, 2)

    def test_missing_or_bool_id_sends_nothing(self):
        f = make_fetcher({})
        self.assertIsNone(f.class_icon(None, {"unchanged": False}))
        self.assertIsNone(f.class_icon(False, {"unchanged": False}))
        self.assertEqual(f.client.urls, [])


# --------------------------------------------------------------------------- #
# 10.3 — the probe found nothing for races, so the sync invents nothing
# --------------------------------------------------------------------------- #
class NoInventedRaceIcon(unittest.TestCase):
    def test_no_race_media_route_is_configured(self):
        self.assertFalse(any("RACE" in name and "MEDIA" in name for name in dir(config)),
                         f"a race media path appeared in config: "
                         f"{[n for n in dir(config) if 'RACE' in n]}")

    def test_no_race_icon_lookup_method_or_write_site_exists(self):
        self.assertFalse(hasattr(fetch.Fetcher, "race_icon"))
        # The key is legal in the schema (so a future patch needs no new gate) and
        # written by nothing: an assertion about the write sites, not the key list,
        # is what actually proves the sync cannot invent the value.
        source = Path(fetch.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"raceIconUrl"', source)

    def test_enrich_never_requests_race_media_or_writes_the_key(self):
        f, out = enrich_beef_pho()
        self.assertNotIn("raceIconUrl", out)
        joined = " ".join(f.client.urls)
        self.assertNotIn("/data/wow/media/playable-race", joined)
        self.assertNotIn("/playable-race/", joined)   # no sub-resource fallback either


# --------------------------------------------------------------------------- #
# The whole pass, over §9/§10-shaped payloads
# --------------------------------------------------------------------------- #
def enrich_beef_pho(table_extra=None, profile=None):
    base = f"{API}/profile/wow/character/beef-pho"
    ns = f"?namespace={config.NS_PROFILE}"
    table = {
        f"{base}/equipment{ns}": (200, {"equipped_items": [
            {"slot": {"name": {"en_US": "Chest"}}, "name": {"en_US": "Robe"},
             "level": {"value": 26}, "item": {"id": 127}, "quality": {"type": "UNCOMMON"}}]}),
        f"{base}/specializations{ns}": (200, {"specialization_groups": [{"is_active": True}]}),
        f"{base}/statistics{ns}": (200, {"health": 857}),
        f"{base}/professions{ns}": (200, {"primaries": [
            {"profession": {"id": 186, "name": "Mining"},
             "tiers": [{"skill_points": 127, "max_skill_points": 225,
                        "tier": {"id": 2946, "name": "Mining"}, "known_recipes": []}]},
            {"profession": {"id": 164, "name": "Blacksmithing"}, "tiers": []}],
            "secondaries": [{"profession": {"id": 185, "name": "Cooking"}, "tiers": []}]}),
        f"{base}/reputations{ns}": (200, {"reputations": [
            {"faction": {"id": 577, "name": {"en_US": "Darkspear Trolls"}},
             "standing": {"raw": 10382, "value": 1382, "max": 3000, "tier": 3,
                          "name": {"en_US": "Neutral"}}}]}),
        f"{base}/character-media{ns}": (200, {"assets": [
            {"key": "avatar", "value": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar.jpg"}]}),
        f"{base}/achievements{ns}": (200, {"total_points": 7}),
        f"{base}/pvp-summary{ns}": (200, {"honorable_kills": 0, "pvp_rank": 0}),
        class_media_url(1): (200, icon_payload(626008)),
        profession_media_url(186): (200, icon_payload(136248)),
        profession_media_url(164): (200, icon_payload(136241)),
        profession_media_url(185): (200, icon_payload(133971)),
    }
    table.update(table_extra or {})
    f = make_fetcher(table)
    w = {"rec": {"id": "1", "name": "Beef Pho"},
         "profile": profile if profile is not None else {
             "name": "Beef", "surname": "Pho", "level": 20, "equipped_item_level": 12,
             "race": {"id": 2, "name": {"en_US": "Orc"}},
             "character_class": {"id": 1, "name": {"en_US": "Warrior"}}},
         "entry": {"href": PROFILE_HREF}, "profile_url": PROFILE_HREF, "unchanged": False}
    return f, f.enrich(w)


class Phase4aEnrichIntegration(unittest.TestCase):
    def test_identity_and_icons_land_next_to_the_existing_keys(self):
        f, out = enrich_beef_pho()
        self.assertEqual(set(out) - set(artifacts.CHARACTER_KEYS), set())
        self.assertEqual(out["race"], {"id": 2, "name": "Orc"})
        self.assertEqual(out["class"], {"id": 1, "name": "Warrior"})
        self.assertEqual(out["classIconUrl"], f"{RENDER}626008.jpg")
        self.assertEqual([e["iconUrl"] for e in out["professions"]["primaries"]],
                         [f"{RENDER}136248.jpg", f"{RENDER}136241.jpg"])
        self.assertEqual([e["iconUrl"] for e in out["professions"]["secondaries"]],
                         [f"{RENDER}133971.jpg"])
        # untouched v2 semantics: the Phase 4a keys are additive, nothing else moved
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["level"], 20)
        self.assertEqual(out["itemLevel"], 12)
        self.assertEqual(out["achievementPoints"], 7)
        self.assertEqual(out["gear"][0]["name"], "Robe")
        self.assertEqual(f.feature_failures, [])
        self.assertEqual(f.warnings, [])

    def test_one_class_icon_request_covers_every_character_of_that_class(self):
        f, _ = enrich_beef_pho()
        # same run, second Warrior: the URL dedup already has it
        w = {"rec": {"id": "2", "name": "Someone Else"},
             "profile": {"name": "X", "surname": "Y", "level": 10,
                         "race": {"id": 2, "name": {"en_US": "Orc"}},
                         "character_class": {"id": 1, "name": {"en_US": "Warrior"}}},
             "entry": {"href": PROFILE_HREF}, "profile_url": PROFILE_HREF, "unchanged": False}
        second = f.enrich(w)
        self.assertEqual(second["classIconUrl"], f"{RENDER}626008.jpg")
        self.assertEqual(f.client.urls.count(class_media_url(1)), 1)

    def test_missing_class_media_stores_null_and_keeps_the_row(self):
        f, out = enrich_beef_pho(table_extra={class_media_url(1): (404, None)})
        self.assertIn("classIconUrl", out)
        self.assertIsNone(out["classIconUrl"])
        self.assertEqual(out["race"], {"id": 2, "name": "Orc"})

    def test_poisoned_class_icon_is_not_published(self):
        f, out = enrich_beef_pho(table_extra={
            class_media_url(1): (200, {"assets": [{"key": "icon",
                                                  "value": "https://attacker.tld/beacon.png"}]})})
        self.assertIsNone(out["classIconUrl"])
        self.assertNotIn("attacker.tld", str(out))

    def test_malformed_profile_refs_are_reported_and_the_keys_stay_absent(self):
        f, out = enrich_beef_pho(profile={"name": "Beef", "surname": "Pho", "level": 20,
                                          "race": "Orc", "character_class": {"id": 1}})
        self.assertNotIn("race", out)
        self.assertNotIn("class", out)
        # no class ref means no class-icon request either: nothing to look up
        self.assertNotIn("classIconUrl", out)
        self.assertEqual(f.client.urls.count(class_media_url(1)), 0)
        self.assertEqual(sum("malformed race" in w for w in f.warnings), 1)
        self.assertEqual(sum("malformed character_class" in w for w in f.warnings), 1)
        self.assertNotIn("Orc", str(out))    # the payload value is reported, not dumped

    def test_class_icon_lookup_never_happens_without_a_static_namespace(self):
        f = make_fetcher({})
        f.static_ns = None
        w = {"rec": {"id": "7"}, "profile": {"name": "A", "surname": "B",
                                             "character_class": {"id": 1, "name": {"en_US": "Warrior"}}},
             "entry": {"href": PROFILE_HREF}, "profile_url": PROFILE_HREF, "unchanged": False}
        out = f.enrich(w)
        self.assertIsNone(out["classIconUrl"])
        # the character's own sub-docs still fetch; only the icon route stays silent,
        # because it exists in the static namespace and nothing else (§10)
        self.assertEqual([u for u in f.client.urls if "/data/wow/media/" in u], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
