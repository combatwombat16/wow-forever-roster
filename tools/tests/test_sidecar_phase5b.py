#!/usr/bin/env python3
"""Offline unit tests for the Phase 5b static icon maps (P5.B, notes §11).

Fixtures mirror the two index documents exactly as the §11 sweep recorded them —
``playable-class/index`` (playable-class/1, nine entries, ``classes[].name`` a locale
map) and ``profession/index`` (profession/2, thirteen entries, primaries and
secondaries in one flat list) — plus the ``/data/wow/media/<kind>/{id}`` media docs
of §10. Both halves of the contract are pinned here: the envelope the UI team builds
against (``{staticNamespace, updatedAt, icons}``, keyed by Blizzard's own name, value
``{id, iconUrl}``), and everything the sync must refuse to publish: a 404'd icon, an
off-host asset, a poisoned carried-over entry, and an index name that would break a
JS consumer's object (``constructor``, ``__proto__``).

Stdlib unittest only: no network, no credentials, and every write goes to a temp
directory (both the cache and the two artifact paths are redirected).

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/

from bnet import artifacts, config, fetch, http, sync  # noqa: E402

STATIC = "static-1.60.1_70399-forever-us"
OTHER_NS = "static-1.61.0_99999-forever-us"
API = config.API_BASE
RENDER = "https://render.worldofwarcraft.com/forever-us/icons/56/"
CLASS_INDEX = f"{API}/data/wow/playable-class/index?namespace={STATIC}"
PROFESSION_INDEX = f"{API}/data/wow/profession/index?namespace={STATIC}"


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

    def get(self, url):
        self.urls.append(url)
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


def localized(name):
    """Both index documents localize their names (§11); only the per-character
    payload of §9.2 uses plain strings, and loc() accepts either."""
    return {"en_US": name, "de_DE": name}


def class_index(entries):
    return {"classes": [{"id": i, "name": localized(n),
                         "key": {"href": f"{API}/data/wow/playable-class/{i}"}} for i, n in entries]}


def profession_index(entries):
    return {"professions": [{"id": i, "name": localized(n),
                             "key": {"href": f"{API}/data/wow/profession/{i}"}} for i, n in entries]}


NINE_CLASSES = [(1, "Warrior"), (2, "Paladin"), (3, "Hunter"), (4, "Rogue"), (5, "Priest"),
                (7, "Shaman"), (8, "Mage"), (9, "Warlock"), (11, "Druid")]
THIRTEEN_PROFESSIONS = [(197, "Tailoring"), (185, "Cooking"), (165, "Leatherworking"),
                        (393, "Skinning"), (333, "Enchanting"), (186, "Mining"),
                        (164, "Blacksmithing"), (202, "Engineering"), (356, "Fishing"),
                        (171, "Alchemy"), (182, "Herbalism"), (129, "First Aid"),
                        (2933, "Test Profession [DNT]")]


def full_table():
    """The healthy §11 run: two indexes plus one media doc per id."""
    table = {CLASS_INDEX: (200, class_index(NINE_CLASSES)),
             PROFESSION_INDEX: (200, profession_index(THIRTEEN_PROFESSIONS))}
    for class_id, _name in NINE_CLASSES:
        table[class_media_url(class_id)] = (200, icon_payload(626000 + class_id))
    for profession_id, _name in THIRTEEN_PROFESSIONS:
        table[profession_media_url(profession_id)] = (200, icon_payload(136000 + profession_id))
    return table


# --------------------------------------------------------------------------- #
# The key gate: an index name becomes an ARTIFACT KEY the browser looks up
# --------------------------------------------------------------------------- #
class IconMapKeyGate(unittest.TestCase):
    def test_ordinary_names_pass_including_blizzard_oddities(self):
        for name in ("Warrior", "First Aid", "Test Profession [DNT]", "Tauren"):
            self.assertEqual(fetch.icon_map_key(name), name)

    def test_surrounding_whitespace_is_trimmed_but_nothing_is_truncated(self):
        self.assertEqual(fetch.icon_map_key("  Warrior  "), "Warrior")
        self.assertIsNone(fetch.icon_map_key("x" * (config.ICON_MAP_KEY_MAX + 1)),
                          "an over-long name must be refused, never cut down to a key "
                          "that answers for a different name")

    def test_empty_and_non_string_names_are_refused(self):
        for value in ("", "   ", None, 11, ["Warrior"], True):
            self.assertIsNone(fetch.icon_map_key(value))

    def test_control_characters_and_escapes_are_refused(self):
        for value in ("War\nrior", "War\u0000rior", "Druid\x1b[31m", "Acolyte\x7f"):
            self.assertIsNone(fetch.icon_map_key(value))

    def test_a_name_that_is_a_url_is_refused(self):
        # The committed maps are scanned for foreign hosts; refusing a scheme here
        # means the artifact can never carry one in a key.
        for value in ("Warrior https://attacker.tld/beacon", "http://evil.tld", "render.x://y"):
            self.assertIsNone(fetch.icon_map_key(value))

    def test_object_prototype_names_are_refused_case_insensitively(self):
        for value in ("__proto__", "constructor", "Constructor", "toString", "ToString",
                      "hasOwnProperty", "prototype", "__defineGetter__", "valueOf"):
            self.assertIsNone(fetch.icon_map_key(value), f"{value!r} must never be a map key")


class IconEntryGate(unittest.TestCase):
    def test_a_clean_entry_survives_and_is_rebuilt_from_two_keys_only(self):
        entry = fetch.icon_entry({"id": 1, "iconUrl": f"{RENDER}626008.jpg", "note": "extra"})
        self.assertEqual(entry, {"id": 1, "iconUrl": f"{RENDER}626008.jpg"})

    def test_off_host_and_null_urls_are_refused(self):
        for url in (None, "", f"http://render.worldofwarcraft.com/{'x'}",
                    "https://render.worldofwarcraft.com.evil.tld/a.jpg",
                    "https://attacker.tld/beacon.jpg"):
            self.assertIsNone(fetch.icon_entry({"id": 1, "iconUrl": url}))

    def test_bool_is_not_an_id_and_non_dicts_are_not_entries(self):
        self.assertIsNone(fetch.icon_entry({"id": True, "iconUrl": f"{RENDER}1.jpg"}))
        self.assertIsNone(fetch.icon_entry({"id": "1", "iconUrl": f"{RENDER}1.jpg"}))
        for value in (None, "Warrior", [], 1):
            self.assertIsNone(fetch.icon_entry(value))


# --------------------------------------------------------------------------- #
# Index payload -> (entries, refused)
# --------------------------------------------------------------------------- #
class IconIndexExtractor(unittest.TestCase):
    def test_locale_map_names_are_read_and_order_is_blizzards(self):
        entries, refused = fetch.normalize_icon_index(class_index(NINE_CLASSES), "classes")
        self.assertEqual(refused, [])
        self.assertEqual([e["name"] for e in entries], [n for _i, n in NINE_CLASSES])
        self.assertEqual([e["id"] for e in entries], [i for i, _n in NINE_CLASSES])

    def test_plain_string_names_work_too(self):
        payload = {"professions": [{"id": 185, "name": "Cooking"}]}
        entries, refused = fetch.normalize_icon_index(payload, "professions")
        self.assertEqual(entries, [{"id": 185, "name": "Cooking"}])
        self.assertEqual(refused, [])

    def test_missing_list_is_refused_not_crashing(self):
        for payload in (None, "index", {}, {"classes": "Warrior"}, {"classes": {"1": "Warrior"}}):
            entries, refused = fetch.normalize_icon_index(payload, "classes")
            self.assertEqual(entries, [])
            self.assertEqual(len(refused), 1)

    def test_hostile_and_broken_entries_are_refused_while_good_ones_land(self):
        payload = {"classes": ["not-an-object",
                               {"id": 1, "name": localized("Warrior")},
                               {"id": None, "name": localized("Idless")},
                               {"id": True, "name": localized("Boolclass")},
                               {"id": 2, "name": localized("constructor")},
                               {"id": 3, "name": localized("__proto__")},
                               {"id": 4, "name": localized("toString")},
                               {"id": 5, "name": localized("evil\nFaked sync line")},
                               {"id": 6, "name": localized("x" * 200)},
                               {"id": 7, "name": None},
                               {"id": 66, "name": localized("Paladin https://evil.tld/x")},
                               {"id": 8, "name": localized("  Rogue  ")}]}
        entries, refused = fetch.normalize_icon_index(payload, "classes")
        self.assertEqual(entries, [{"id": 1, "name": "Warrior"}, {"id": 8, "name": "Rogue"}])
        self.assertEqual(len(refused), 10)
        joined = " ".join(refused)
        for never in ("constructor", "__proto__", "toString"):
            self.assertIn(never, joined)      # the refusal names what it refused

    def test_refused_description_survives_the_log_boundary(self):
        _entries, refused = fetch.normalize_icon_index(
            {"classes": [{"id": 1, "name": localized("forged\nFATAL at https://attacker.tld/beacon")}]},
            "classes")
        self.assertEqual(len(refused), 1)
        line = sync.log_safe(http.redact(" ".join(refused)))
        self.assertNotIn("attacker.tld", line)      # H2: the host never reaches a log
        self.assertEqual(len(line.splitlines()), 1)  # and it cannot forge a second line


# --------------------------------------------------------------------------- #
# The map pass: index + one media doc per id, name-keyed, merge, refusals
# --------------------------------------------------------------------------- #
class ClassIconMap(unittest.TestCase):
    def test_every_class_lands_keyed_by_blizzards_own_name(self):
        f = make_fetcher(full_table())
        icons, index_total, carried = f.class_icon_map({})
        self.assertEqual(index_total, 9)
        self.assertEqual(carried, 0)
        self.assertEqual(sorted(icons), sorted(n for _i, n in NINE_CLASSES))
        self.assertEqual(icons["Warrior"], {"id": 1, "iconUrl": f"{RENDER}{626001}.jpg"})
        self.assertEqual(icons["Druid"], {"id": 11, "iconUrl": f"{RENDER}{626011}.jpg"})
        self.assertEqual(f.icon_map_failures, [])
        # one index GET plus one media GET per class, nothing else
        self.assertEqual(len(f.client.urls), 10)

    def test_profession_map_is_one_flat_list_primaries_and_secondaries_alike(self):
        f = make_fetcher(full_table())
        icons, index_total, _carried = f.profession_icon_map({})
        self.assertEqual(index_total, 13)
        self.assertEqual(sorted(icons), sorted(n for _i, n in THIRTEEN_PROFESSIONS))
        self.assertIn("Cooking", icons)        # secondary
        self.assertIn("Blacksmithing", icons)  # primary
        self.assertIn("Test Profession [DNT]", icons)   # the index lists it, so the map does
        self.assertEqual(icons["Mining"], {"id": 186, "iconUrl": f"{RENDER}{136186}.jpg"})

    def test_404_media_omits_the_entry_and_reports_it(self):
        table = full_table()
        table[class_media_url(4)] = (404, None)
        f = make_fetcher(table)
        icons, index_total, _carried = f.class_icon_map({})
        self.assertEqual(index_total, 9)
        self.assertNotIn("Rogue", icons)
        self.assertEqual(len(icons), 8)
        self.assertEqual(len(f.icon_map_failures), 1)
        line = f.icon_map_failures[0]
        self.assertIn("Rogue", line)
        self.assertIn("id 4", line)
        self.assertIn("omitted", line)
        self.assertEqual(f.icon_failures, 1)

    def test_poisoned_asset_is_refused_and_never_published(self):
        table = full_table()
        table[class_media_url(1)] = (200, {"assets": [{"key": "icon",
                                                       "value": "https://attacker.tld/beacon.jpg"}]})
        f = make_fetcher(table)
        icons, _total, _carried = f.class_icon_map({})
        self.assertNotIn("Warrior", icons)
        self.assertNotIn("attacker.tld", json.dumps(icons))
        self.assertEqual(len(f.icon_map_failures), 1)
        self.assertIn("Warrior", f.icon_map_failures[0])

    def test_failed_index_read_yields_an_empty_map_and_touches_no_media(self):
        table = full_table()
        table[CLASS_INDEX] = (404, None)
        f = make_fetcher(table)
        icons, index_total, carried = f.class_icon_map({})
        self.assertEqual((icons, index_total, carried), ({}, 0, 0))
        self.assertEqual([u for u in f.client.urls if "/media/" in u], [])
        self.assertIn("committed map left untouched", f.icon_map_failures[0])

    def test_without_a_static_namespace_nothing_is_sent(self):
        f = make_fetcher(full_table())
        f.static_ns = None
        self.assertEqual(f.class_icon_map({}), ({}, 0, 0))
        self.assertEqual(f.client.urls, [])
        self.assertIn("static namespace unknown", f.icon_map_failures[0])

    def test_one_index_get_and_one_media_get_per_id_for_the_whole_run(self):
        f = make_fetcher(full_table())
        f.class_icon_map({})
        f.class_icon_map({})                       # second call in the same run
        self.assertEqual(f.client.urls.count(CLASS_INDEX), 2)
        self.assertEqual(f.client.urls.count(class_media_url(1)), 1)   # icon_for dedup


class MergeByName(unittest.TestCase):
    def prior(self, namespace=STATIC, icons=None):
        return {"staticNamespace": namespace, "updatedAt": "2026-11-09T00:00:00Z",
                "icons": icons if icons is not None else
                {n: {"id": i, "iconUrl": f"{RENDER}old{i}.jpg"} for i, n in NINE_CLASSES}}

    def test_matching_namespace_carries_the_map_and_spends_zero_media_requests(self):
        f = make_fetcher(full_table())
        icons, _total, carried = f.class_icon_map(self.prior())
        self.assertEqual(carried, 9)
        self.assertEqual(len(icons), 9)
        self.assertEqual(icons["Warrior"]["iconUrl"], f"{RENDER}old1.jpg")
        self.assertEqual([u for u in f.client.urls if "/media/" in u], [])
        self.assertEqual(f.saved_requests, 9)

    def test_a_different_namespace_drops_the_old_map_and_refetches_everything(self):
        f = make_fetcher(full_table())
        icons, _total, carried = f.class_icon_map(self.prior(namespace=OTHER_NS))
        self.assertEqual(carried, 0)
        self.assertEqual(icons["Warrior"]["iconUrl"], f"{RENDER}{626001}.jpg")
        self.assertEqual(len([u for u in f.client.urls if "/media/" in u]), 9)
        self.assertEqual(sum("pinned to" in w for w in f.warnings), 1)

    def test_a_carried_entry_is_only_reused_when_the_id_still_matches(self):
        prior = self.prior(icons={"Warrior": {"id": 999, "iconUrl": f"{RENDER}old1.jpg"}})
        f = make_fetcher(full_table())
        icons, _total, carried = f.class_icon_map(prior)
        self.assertEqual(carried, 0)
        self.assertEqual(icons["Warrior"], {"id": 1, "iconUrl": f"{RENDER}{626001}.jpg"})
        self.assertIn(class_media_url(1), f.client.urls)

    def test_a_poisoned_carried_entry_is_refetched_not_trusted(self):
        prior = self.prior(icons={"Warrior": {"id": 1, "iconUrl": "https://attacker.tld/beacon.jpg"}})
        f = make_fetcher(full_table())
        icons, _total, _carried = f.class_icon_map(prior)
        self.assertEqual(icons["Warrior"]["iconUrl"], f"{RENDER}{626001}.jpg")
        self.assertNotIn("attacker.tld", json.dumps(icons))

    def test_a_prior_name_the_index_no_longer_lists_is_carried_by_the_writer(self):
        """The talent-tree rule: a short index read must not erase a live map."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "class-icons.json"
            committed = self.prior(icons={"Warrior": {"id": 1, "iconUrl": f"{RENDER}626001.jpg"},
                                          "Deathknight": {"id": 6, "iconUrl": f"{RENDER}626006.jpg"}})
            path.write_text(json.dumps(committed), encoding="utf-8")
            doc = artifacts.write_icon_map(path, STATIC, {"Paladin": {"id": 2,
                                                                     "iconUrl": f"{RENDER}626003.jpg"}},
                                           dry_run=False)
            self.assertEqual(sorted(doc["icons"]), ["Deathknight", "Paladin", "Warrior"])


class DuplicateIndexNames(unittest.TestCase):
    def test_first_entry_wins_and_the_duplicate_is_reported(self):
        payload = class_index([(1, "Warrior"), (666, "Warrior"), (2, "Paladin")])
        f = make_fetcher({CLASS_INDEX: (200, payload),
                          class_media_url(1): (200, icon_payload(626001)),
                          class_media_url(2): (200, icon_payload(626002)),
                          class_media_url(666): (200, icon_payload(626666))})
        icons, index_total, _carried = f.class_icon_map({})
        self.assertEqual(index_total, 3)
        self.assertEqual(icons["Warrior"]["id"], 1)
        self.assertEqual(class_media_url(666) in f.client.urls, False)
        self.assertEqual(len(f.icon_map_failures), 1)
        self.assertIn("duplicate name", f.icon_map_failures[0])


class HostileIndexCannotBreakTheMap(unittest.TestCase):
    """A poisoned index must never turn the artifact into a broken JS object."""

    def hostile_payload(self):
        entries = [(1, "Warrior"), (2, "constructor"), (3, "__proto__"), (4, "toString"),
                   (5, "hasOwnProperty"), (6, "prototype"), (7, "War\nrior"),
                   (8, "Druid")]
        payload = {"classes": [{"id": i, "name": localized(n)} for i, n in entries]}
        table = {CLASS_INDEX: (200, payload)}
        for class_id in (1, 8):
            table[class_media_url(class_id)] = (200, icon_payload(626000 + class_id))
        return table

    def test_prototype_keys_never_reach_the_map_and_the_rest_stay_intact(self):
        f = make_fetcher(self.hostile_payload())
        icons, index_total, _carried = f.class_icon_map({})
        self.assertEqual(index_total, 8)
        self.assertEqual(sorted(icons), ["Druid", "Warrior"])
        for hazard in ("constructor", "__proto__", "toString", "hasOwnProperty", "prototype"):
            self.assertNotIn(hazard, icons)
        self.assertNotIn("War\nrior", icons)
        self.assertEqual(icons["Warrior"], {"id": 1, "iconUrl": f"{RENDER}626001.jpg"})
        self.assertEqual(len(f.icon_map_failures), 6)      # one per refused entry

    def test_a_hazard_name_cannot_be_smuggled_in_by_the_writer_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "class-icons.json"
            path.write_text(json.dumps({"staticNamespace": STATIC, "updatedAt": "2026-11-09T00:00:00Z",
                                        "icons": {"constructor": {"id": 1, "iconUrl": f"{RENDER}1.jpg"},
                                                  "__proto__": {"id": 2, "iconUrl": f"{RENDER}2.jpg"},
                                                  "Warrior": {"id": 1, "iconUrl": f"{RENDER}1.jpg"}}}),
                            encoding="utf-8")
            doc = artifacts.write_icon_map(path, STATIC, {}, dry_run=False)
            self.assertEqual(list(doc["icons"]), ["Warrior"])

    def test_the_written_json_never_renders_a_hazard_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "class-icons.json"
            doc = artifacts.write_icon_map(path, STATIC,
                                           {"constructor": {"id": 1, "iconUrl": f"{RENDER}1.jpg"},
                                            "Warrior": {"id": 1, "iconUrl": f"{RENDER}1.jpg"}},
                                           dry_run=False)
            self.assertNotIn("constructor", path.read_text(encoding="utf-8"))
            self.assertEqual(list(doc["icons"]), ["Warrior"])


# --------------------------------------------------------------------------- #
# The artifact envelope the UI team builds against
# --------------------------------------------------------------------------- #
class IconMapEnvelope(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "class-icons.json"

    def test_envelope_is_exactly_the_talent_tree_shape(self):
        doc = artifacts.write_icon_map(self.path, STATIC,
                                       {"Warrior": {"id": 1, "iconUrl": f"{RENDER}626001.jpg"}},
                                       dry_run=False)
        self.assertEqual(list(doc), list(artifacts.ICON_MAP_DOC_KEYS))
        self.assertEqual(set(doc), {"staticNamespace", "updatedAt", "icons"})
        self.assertEqual(doc["staticNamespace"], STATIC)
        self.assertRegex(doc["updatedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(doc["icons"], {"Warrior": {"id": 1, "iconUrl": f"{RENDER}626001.jpg"}})
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, doc)

    def test_entry_values_carry_id_and_icon_url_and_nothing_else(self):
        doc = artifacts.write_icon_map(self.path, STATIC,
                                       {"Warrior": {"id": 1, "iconUrl": f"{RENDER}1.jpg",
                                                    "note": "https://attacker.tld/beacon"}},
                                       dry_run=False)
        self.assertEqual(set(doc["icons"]["Warrior"]), set(artifacts.ICON_ENTRY_KEYS))
        self.assertNotIn("attacker.tld", self.path.read_text(encoding="utf-8"))

    def test_keys_are_sorted_for_a_stable_diff(self):
        doc = artifacts.write_icon_map(self.path, STATIC,
                                       {n: {"id": i, "iconUrl": f"{RENDER}{i}.jpg"}
                                        for i, n in reversed(NINE_CLASSES)}, dry_run=False)
        self.assertEqual(list(doc["icons"]), sorted([n for _i, n in NINE_CLASSES],
                                                    key=lambda s: (s.casefold(), s)))

    def test_dry_run_writes_nothing_but_still_returns_the_document(self):
        doc = artifacts.write_icon_map(self.path, STATIC,
                                       {"Warrior": {"id": 1, "iconUrl": f"{RENDER}1.jpg"}},
                                       dry_run=True)
        self.assertFalse(self.path.exists())
        self.assertIn("Warrior", doc["icons"])

    def test_an_unreadable_previous_file_is_survived(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(artifacts.load_previous_icon_map(self.path), {})
        doc = artifacts.write_icon_map(self.path, STATIC,
                                       {"Warrior": {"id": 1, "iconUrl": f"{RENDER}1.jpg"}},
                                       dry_run=False)
        self.assertEqual(list(doc["icons"]), ["Warrior"])


# --------------------------------------------------------------------------- #
# The sync orchestration: once per run, both files, never an empty write
# --------------------------------------------------------------------------- #
class SyncWritesBothMaps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.class_path = self.tmp / "class-icons.json"
        self.profession_path = self.tmp / "profession-icons.json"
        self.patcher_class = mock.patch.object(config, "CLASS_ICON_FILE", self.class_path)
        self.patcher_prof = mock.patch.object(config, "PROFESSION_ICON_FILE", self.profession_path)
        self.patcher_class.start()
        self.patcher_prof.start()
        self.addCleanup(self.patcher_class.stop)
        self.addCleanup(self.patcher_prof.stop)

    def make_sync(self, table):
        args = sync.build_parser().parse_args(["--state-dir", str(self.tmp / "cache")])
        s = sync.Sync(args)
        s.f = make_fetcher(table)
        return s

    def test_both_files_land_with_the_pinned_namespace(self):
        s = self.make_sync(full_table())
        lines = s.refresh_icon_maps(dry_run=False)
        self.assertEqual(len(lines), 2)
        for path in (self.class_path, self.profession_path):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(doc), {"staticNamespace", "updatedAt", "icons"})
            self.assertEqual(doc["staticNamespace"], STATIC)
            self.assertTrue(all(set(v) == {"id", "iconUrl"} for v in doc["icons"].values()))
        self.assertEqual(len(json.loads(self.class_path.read_text())["icons"]), 9)
        self.assertEqual(len(json.loads(self.profession_path.read_text())["icons"]), 13)
        self.assertIn("class icons: 9 of 9 index entries landed", " ".join(lines))
        self.assertIn("profession icons: 13 of 13 index entries landed", " ".join(lines))

    def test_second_run_carries_the_committed_maps_and_asks_for_no_media(self):
        s = self.make_sync(full_table())
        s.refresh_icon_maps(dry_run=False)
        s2 = self.make_sync(full_table())
        lines = " ".join(s2.refresh_icon_maps(dry_run=False))
        self.assertIn("9 carried from the committed map", lines)
        self.assertIn("13 carried from the committed map", lines)
        self.assertEqual([u for u in s2.f.client.urls if "/media/" in u], [])

    def test_dry_run_writes_nothing(self):
        s = self.make_sync(full_table())
        lines = " ".join(s.refresh_icon_maps(dry_run=True))
        self.assertFalse(self.class_path.exists())
        self.assertFalse(self.profession_path.exists())
        self.assertIn("dry-run, not written", lines)

    def test_a_run_without_static_ns_writes_nothing_and_says_nothing(self):
        s = self.make_sync(full_table())
        s.f.static_ns = None
        self.assertEqual(s.refresh_icon_maps(dry_run=False), [])
        self.assertFalse(self.class_path.exists())

    def test_a_total_index_failure_leaves_the_committed_file_alone(self):
        s = self.make_sync(full_table())
        s.refresh_icon_maps(dry_run=False)
        before = self.class_path.read_text(encoding="utf-8")
        s2 = self.make_sync({})                      # every route 404s
        lines = " ".join(s2.refresh_icon_maps(dry_run=False))
        self.assertIn("NOT written", lines)
        self.assertEqual(self.class_path.read_text(encoding="utf-8"), before)

    def test_per_entry_refusals_reach_the_run_summary(self):
        table = full_table()
        table[class_media_url(4)] = (404, None)
        table[profession_media_url(356)] = (404, None)
        s = self.make_sync(table)
        s.refresh_icon_maps(dry_run=False)
        summary = " ".join(s.summary_lines({}, 1, 0.0))
        self.assertIn("static icon map entry", summary)
        self.assertIn("Rogue", summary)
        self.assertIn("Fishing", summary)
        landed = json.loads(self.class_path.read_text(encoding="utf-8"))["icons"]
        self.assertNotIn("Rogue", landed)
        self.assertEqual(len(landed), 8)

    def test_the_printed_summary_survives_the_log_boundary(self):
        """emit_summary is the boundary: one printable, redacted line per row."""
        s = self.make_sync(full_table())
        s.f.icon_map_failures.extend(
            ["class icons: forged\nFATAL SECOND LINE",
             "profession icons: refused https://attacker.tld/beacon"])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
            s.emit_summary(s.summary_lines({}, 1, 0.0))
        printed = out.getvalue()
        self.assertNotIn("attacker.tld", printed)           # H2: no foreign host in a log
        self.assertIn("forged FATAL SECOND LINE", printed)   # collapsed onto one line
        self.assertNotIn("\nFATAL", printed)                 # cannot start a fake summary row
        self.assertIn("2 static icon map entry(ies) refused", printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
