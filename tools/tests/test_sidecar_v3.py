#!/usr/bin/env python3
"""Offline unit tests for schema v3: the manifest + per-character detail split.

The sidecar used to be ONE document that grew with the guild (529 KB for 17 resolved characters on
2026-11-14). Schema v3 splits each enriched record across two committed surfaces with no change to
any field's meaning:

  * ``data/character-data.json`` — the MANIFEST. ``characters[id]`` keeps the nine list-view fields
    every roster card reads, plus ``detail``, a pointer to the other half;
  * ``data/character-data/<id>.json`` — the DETAIL document: ``bnet``, ``gear``, ``progress``,
    ``achievementPoints``, ``stats``, ``talents``, ``professions``, ``reputations``, ``media``. It
    carries NO ``status`` and none of the manifest fields.

Three layers, because they fail differently:

  * ``bnet.artifacts`` (unit): ``manifest_row`` / ``detail_document`` / ``split_characters`` — the
    partition itself, the ``detail`` string, the absent-not-null rule, the refusal to drop a key
    that belongs to neither half, the write ORDER (detail files, prunings, manifest LAST), and
    ``load_previous_data`` being the exact inverse of the split (otherwise a one-character lookup
    republishes a manifest whose detail it just threw away);
  * ``tools/migrate_v3.py`` (script): deterministic, idempotent, offline, and it uses those writer
    functions rather than a second implementation — pinned by reproducing the committed files and
    by the NO-DATA-LOSS walk over the committed v2 rows at HEAD;
  * ``.pi/scripts/verify_sync_artifacts.py`` (the shipped CI gate, run as the nightly runs it, on a
    copy of the repository): coverage, the ``detail`` EQUALITY rule (compared to the id-derived
    path, never pattern-matched), the orphan rule, the no-``status`` rule, and the per-file budgets.

Stdlib unittest only: no network, no credentials, every write redirected to a temp dir. The
``redirect_artifacts`` helper covers ``config.DETAIL_DIR`` for exactly the reason documented in
``tools/tests/__init__.py`` — a partial redirect deletes published detail files from the checkout.

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))          # tools/

from bnet import artifacts, config                                       # noqa: E402
from tests import redirect_artifacts, restore_artifacts                   # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FETCHED_AT = "2026-11-15T00:00:00Z"
API = config.API_BASE
PROFILE_HREF = f"{API}/profile/wow/character/beef-pho?namespace={config.NS_PROFILE}"


def full_record(rid="1", status="ok", **extra):
    """One enriched sidecar record in the shape ``Fetcher.enrich`` hands the writer."""
    rec = {"status": status, "fetchedAt": FETCHED_AT}
    if status == "ok":
        rec.update({
            "bnet": {"name": "Beef", "surname": "Pho", "href": PROFILE_HREF},
            "race": {"id": 6, "name": "Tauren"},
            "class": {"id": 11, "name": "Druid"},
            "classIconUrl": "https://render.worldofwarcraft.com/forever-us/icons/56/625999.jpg",
            "level": 60, "itemLevel": 250, "guild": "Flowmasters", "activeSpec": "Balance",
            "gear": [{"slot": "Head", "name": "Helm", "ilvl": 250, "iconUrl": None,
                      "itemId": 1, "quality": "epic", "itemClass": None, "itemSubclass": None,
                      "enchants": [], "stats": []}],
            "progress": {"raid": {"raidProgress": []}},
            "achievementPoints": 100,
            "stats": {"strength": {"base": 10, "effective": 12}},
            "talents": [{"specId": 102, "specName": "Balance", "spentPoints": 31, "talents": []}],
            "professions": {"primaries": [{"id": 164, "name": "Blacksmithing", "tiers": [],
                                           "iconUrl": None}], "secondaries": []},
            "reputations": [{"factionId": 1, "factionName": "Darnassus",
                             "standing": {"raw": "Honored", "value": 6000, "max": 12000,
                                          "tier": 4, "name": "Honored"}}],
            "media": {"avatar": "https://render.worldofwarcraft.com/forever-us/character/1/9-avatar.jpg",
                      "avatarSd": None, "fullRender": None},
        })
    rec.update(extra)
    return rec


# --------------------------------------------------------------------------- #
# 1. The split itself
# --------------------------------------------------------------------------- #
class ManifestRowShape(unittest.TestCase):
    def test_manifest_row_keys_are_the_constant_and_the_record_order(self):
        row = artifacts.manifest_row(full_record(), "1")
        self.assertEqual(set(row), set(artifacts.MANIFEST_ROW_KEYS) - {"raceIconUrl"})
        self.assertEqual(list(row), list(artifacts.MANIFEST_ROW_KEYS))

    def test_detail_is_the_id_derived_path_and_nothing_else(self):
        self.assertEqual(artifacts.manifest_row(full_record(), "42")["detail"],
                         "character-data/42.json")
        self.assertEqual(artifacts.detail_ref("42"), f"{config.DETAIL_DIR_NAME}/42.json")

    def test_a_non_ok_row_omits_the_detail_key_entirely(self):
        for status in ("not_found", "ambiguous", "needs_review", "fetch_error"):
            with self.subTest(status=status):
                row = artifacts.manifest_row({"status": status, "fetchedAt": FETCHED_AT}, "9")
                self.assertNotIn("detail", row)          # absent — not null, not ""

    def test_an_absent_optional_field_is_omitted_not_nulled(self):
        rec = full_record()
        del rec["guild"], rec["activeSpec"]
        row = artifacts.manifest_row(rec, "1")
        self.assertNotIn("guild", row)
        self.assertNotIn("activeSpec", row)
        self.assertEqual(row["level"], 60)               # present fields are copied verbatim

    def test_manifest_row_uses_the_id_it_is_given_not_one_the_record_claims(self):
        """The key the row is stored under is the identity; a record that claims another one must
        not be able to aim its pointer at somebody else's file."""
        row = artifacts.manifest_row({**full_record(), "id": "2"}, "1")
        self.assertEqual(row["detail"], "character-data/1.json")

    def test_an_ok_record_with_no_id_anywhere_refuses_rather_than_pointing_at_none(self):
        rec = full_record()
        with self.assertRaises(ValueError):
            artifacts.manifest_row(rec)

    def test_detail_document_keys_and_id(self):
        doc = artifacts.detail_document("7", full_record())
        self.assertEqual(set(doc), set(artifacts.DETAIL_DOC_KEYS))
        self.assertEqual(list(doc)[:2], ["schemaVersion", "id"])
        self.assertEqual(doc["schemaVersion"], artifacts.DATA_SCHEMA_VERSION)
        self.assertEqual(doc["id"], "7")
        self.assertNotIn("status", doc)
        for manifest_key in artifacts.MANIFEST_INLINE_KEYS:
            self.assertNotIn(manifest_key, doc)

    def test_split_covers_every_key_of_the_record_it_is_given(self):
        rec = full_record()
        rows, details = artifacts.split_characters({"1": rec})
        covered = set(rows["1"]) | set(details["1"]) | {"detail"}
        self.assertEqual({k for k in rec if k not in covered}, set())

    def test_a_key_outside_both_halves_is_refused_rather_than_dropped(self):
        """The one guarantee the split has to keep: no value silently disappears between the record
        and the two files. ``raceIconUrl`` is the historical exception (§10 proved no race media
        route exists, so nothing writes it) and it trips this same refusal if a write site ever
        grows one — the honest moment to widen both halves, not to lose the value quietly."""
        with self.assertRaises(ValueError) as caught:
            artifacts.split_characters({"1": {**full_record(), "raceIconUrl": "https://x"}})
        self.assertIn("raceIconUrl", str(caught.exception))
        with self.assertRaises(ValueError):
            artifacts.split_characters({"1": {**full_record(), "equipped_items": []}})

    def test_a_non_ok_row_gets_a_manifest_row_and_no_detail_document(self):
        rows, details = artifacts.split_characters({"1": {"status": "not_found", "fetchedAt": FETCHED_AT}})
        self.assertEqual(rows["1"], {"status": "not_found", "fetchedAt": FETCHED_AT})
        self.assertEqual(details, {})

    def test_the_reader_and_the_writer_share_one_detail_path_rule(self):
        """The rule is asserted against the SHIPPED reader, not restated here.

        js/data.js accepts a manifest ``detail`` value only when it matches its own literal shape
        AND its captured stem equals the row's id, then fetches a path it BUILDS from
        ``DETAIL_SUBDIR`` and the id. So the check is: the string ``artifacts`` emits satisfies that
        regex and that equality, and the directory name the two share is the same constant.
        """
        source = (REPO / "js" / "data.js").read_text(encoding="utf-8")
        reader = re.search(r"=/^(\[A-Za-z0-9_-\]\+\\/\[A-Za-z0-9_-\]\{1,64\})\\?\.\$?"
                           r"?\$?\/\.exec", source)     # never matches; kept off the assertion path
        self.assertIsNone(reader)
        pattern = re.search(r"const match = /(.*?)/\.exec\(raw\.trim\(\)\);", source)
        self.assertIsNotNone(pattern, "js/data.js's detail-shape regex was not found")
        js_regex = re.compile("^" + pattern.group(1).replace("{1,64}", "{1,64}") + "$")
        subdir = re.search(r"const DETAIL_SUBDIR = '([^']+)'", source)
        self.assertIsNotNone(subdir, "js/data.js declares no DETAIL_SUBDIR")
        self.assertEqual(subdir.group(1), config.DETAIL_DIR_NAME)
        for rid in ("1", "1791928493125", "abc-def_9"):
            emitted = artifacts.detail_ref(rid)
            match = js_regex.match(emitted)
            self.assertIsNotNone(match, f"the shipped reader would refuse {emitted!r}")
            self.assertEqual(match.group(1), rid, "the reader's stem must equal the roster id")
        self.assertIn("DETAIL_DIR + DETAIL_SUBDIR + '/' + encodeURIComponent(String(id)) + '.json'",
                      source, "the reader builds its request from the directory name and the id")


# --------------------------------------------------------------------------- #
# 2. The writer: order, coverage, pruning
# --------------------------------------------------------------------------- #
class SidecarWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = redirect_artifacts(self.tmp)

    def tearDown(self):
        restore_artifacts(self._saved)

    def test_write_data_lays_down_every_detail_file_then_the_manifest(self):
        no_guild = full_record("2")
        no_guild.pop("guild")
        characters = {"1": full_record("1"), "2": no_guild}
        characters["3"] = {"status": "not_found", "fetchedAt": FETCHED_AT}
        payload = artifacts.write_data(characters, triggered_by_value="manual", dry_run=False)

        self.assertEqual(set(payload), {"schemaVersion", "syncedAt", "triggeredBy", "characters"})
        self.assertEqual(payload["schemaVersion"], artifacts.DATA_SCHEMA_VERSION)
        self.assertEqual(set(payload["characters"]), {"1", "2", "3"})
        self.assertTrue((config.DETAIL_DIR / "1.json").is_file())
        self.assertTrue((config.DETAIL_DIR / "2.json").is_file())
        self.assertFalse((config.DETAIL_DIR / "3.json").exists(),
                         "a not_found row has no detail document")
        self.assertEqual(set(payload["characters"]["1"]) - {"detail"},
                         set(artifacts.MANIFEST_INLINE_KEYS))
        self.assertEqual(payload["characters"]["1"]["detail"], "character-data/1.json")
        self.assertNotIn("detail", payload["characters"]["3"])

        written_detail = json.loads((config.DETAIL_DIR / "1.json").read_text(encoding="utf-8"))
        self.assertEqual(set(written_detail), set(artifacts.DETAIL_DOC_KEYS))
        self.assertEqual(written_detail["gear"], full_record()["gear"])   # values, not summaries
        self.assertNotIn("status", written_detail)

    def test_write_order_is_details_then_manifest(self):
        """The manifest is the last write, so a run that dies leaves the PREVIOUS manifest — whose
        files are all still on disk — authoritative."""
        events = []
        real_write = artifacts.write_json

        def spy(path, payload):
            events.append(Path(path).name)
            return real_write(path, payload)

        artifacts.write_json = spy
        try:
            artifacts.write_data({"1": full_record("1")}, triggered_by_value="manual", dry_run=False)
        finally:
            artifacts.write_json = real_write
        self.assertEqual(events, ["1.json", "character-data.json"])

    def test_write_pair_writes_the_map_then_the_details_then_the_manifest(self):
        entries = {"1": artifacts.ok_entry({"name": "Beef Pho"}, slug="beef-pho", href=PROFILE_HREF,
                                            payload={"name": "Beef", "surname": "Pho"},
                                            matched_form="lower_hyphen", mapped_via="direct")}
        events = []
        real_write = artifacts.write_json

        def spy(path, payload):
            events.append(Path(path).name)
            return real_write(path, payload)

        artifacts.write_json = spy
        try:
            self.assertIsNone(artifacts.write_pair(entries, {"1": full_record("1")}, ordered_ids=["1"],
                                                   prev_entries={}, triggered_by_value="manual",
                                                   dry_run=False))
        finally:
            artifacts.write_json = real_write
        self.assertEqual(events, ["battle-net-map.json", "1.json", "character-data.json"])

    def test_a_record_key_outside_both_halves_refuses_before_the_map_moves(self):
        """The split is validated BEFORE either artifact is written, not during.

        ``split_characters`` is the only place that refuses a record key belonging to neither half.
        It used to run inside ``write_data``, which ``write_pair`` calls AFTER ``write_map`` — so the
        first thing a future enrich field wired before ``MANIFEST_ROW_KEYS`` would produce is an
        updated map next to an unmodified manifest and a traceback: "both artifacts or neither" with
        an asterisk. The ordering is the assertion here, not the refusal, which the split's own test
        already covers.
        """
        entries = {"1": artifacts.ok_entry({"name": "Beef Pho"}, slug="beef-pho", href=PROFILE_HREF,
                                            payload={"name": "Beef", "surname": "Pho"},
                                            matched_form="lower_hyphen", mapped_via="direct")}
        broken = dict(full_record("1"))
        broken["raceIconUrl"] = {"id": 1, "name": "Windshaper"}   # §10: a key with no half to live in

        with self.assertRaises(ValueError) as raised:
            artifacts.write_pair(entries, {"1": broken}, ordered_ids=["1"], prev_entries={},
                                 triggered_by_value="manual", dry_run=False)
        self.assertIn("raceIconUrl", str(raised.exception))
        self.assertFalse(config.MAP_FILE.exists(), "a refused pair wrote the map")
        self.assertFalse(config.DATA_FILE.exists(), "a refused pair wrote the manifest")
        self.assertFalse(config.DETAIL_DIR.exists(), "a refused pair wrote detail files")

    def test_detail_ref_refuses_an_id_that_is_not_one_safe_path_segment(self):
        """The id is a FILESYSTEM path component, so it is validated, not trusted.

        ``roster.py`` accepts any non-empty string id and the auto-adder's ids are 13 digits by
        habit; ``detail_ref`` builds a URL AND the path a document is written to, so a hand-typoed
        ``../../x`` would have escaped ``data/character-data/`` at write time. The gate pins this
        charset equal to the one ``js/data.js`` accepts, so the two halves cannot drift.
        """
        for hostile in ("../x", "a/b", "..", "", "a\\b", "a b", "%2e%2e", "x.json/y", "."):
            with self.assertRaises(ValueError, msg=f"detail_ref accepted {hostile!r}"):
                artifacts.detail_ref(hostile)
        self.assertEqual(artifacts.detail_ref("1791919748959"),
                         "character-data/1791919748959.json")
        self.assertEqual(artifacts.detail_ref(1791919748959),
                         "character-data/1791919748959.json", "a numeric id is still one row's id")

    def test_a_refused_pair_writes_no_detail_directory_at_all(self):
        prev = {rid: {"status": "ok"} for rid in ("1", "2", "3", "4")}
        entries = {rid: artifacts.map_entry({"name": rid}, "not_found", note="404") for rid in prev}
        refusal = artifacts.write_pair(entries, {rid: full_record(rid) for rid in prev},
                                       ordered_ids=list(prev), prev_entries=prev,
                                       triggered_by_value="manual", dry_run=False)
        self.assertIsNotNone(refusal)
        self.assertFalse(config.MAP_FILE.exists())
        self.assertFalse(config.DATA_FILE.exists())
        self.assertFalse(config.DETAIL_DIR.exists(),
                         "the floor refused the run, so it published no detail files either")

    def test_dry_run_writes_nothing_anywhere(self):
        payload = artifacts.write_data({"1": full_record("1")}, triggered_by_value="manual", dry_run=True)
        self.assertEqual(payload["schemaVersion"], artifacts.DATA_SCHEMA_VERSION)
        self.assertFalse(config.DATA_FILE.exists())
        self.assertFalse(config.DETAIL_DIR.exists())

    def test_a_demoted_row_has_its_detail_file_pruned(self):
        """A row that leaves ok must not leave its gear behind under a manifest that stopped naming
        it: that file is the orphan the gate fails the tree for."""
        artifacts.write_data({"1": full_record("1"), "2": full_record("2")},
                             triggered_by_value="manual", dry_run=False)
        self.assertTrue((config.DETAIL_DIR / "2.json").exists())
        artifacts.write_data({"1": full_record("1"),
                              "2": {"status": "not_found", "fetchedAt": FETCHED_AT}},
                             triggered_by_value="manual", dry_run=False)
        self.assertFalse((config.DETAIL_DIR / "2.json").exists(),
                         "the demoted id's detail file survived the write")
        self.assertTrue((config.DETAIL_DIR / "1.json").exists())
        manifest = json.loads(config.DATA_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("detail", manifest["characters"]["2"])

    def test_a_removed_row_has_its_detail_file_pruned_too(self):
        artifacts.write_data({"1": full_record("1"), "2": full_record("2")},
                             triggered_by_value="manual", dry_run=False)
        artifacts.write_data({"1": full_record("1")}, triggered_by_value="manual", dry_run=False)
        self.assertFalse((config.DETAIL_DIR / "2.json").exists())

    def test_pruning_leaves_files_it_did_not_write_for_the_gate_to_report(self):
        config.DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        (config.DETAIL_DIR / "not-json.json").write_text("{", encoding="utf-8")
        (config.DETAIL_DIR / "9.json").write_text(json.dumps({"schemaVersion": 3, "id": "other"}),
                                                  encoding="utf-8")
        (config.DETAIL_DIR / "notes.txt").write_text("keep me", encoding="utf-8")
        artifacts.write_data({"1": full_record("1")}, triggered_by_value="manual", dry_run=False)
        for name in ("not-json.json", "9.json", "notes.txt"):
            self.assertTrue((config.DETAIL_DIR / name).exists(),
                            f"pruning deleted {name}, which this run never wrote")

    def test_load_previous_data_is_the_inverse_of_the_split(self):
        """A lookup republishes EVERY other character from what is on disk. Reading the manifest
        alone would hand ``write_data`` rows with no gear and prune the detail files it just failed
        to read, so the read has to fold both halves back into one record."""
        characters = {"1": full_record("1"), "2": full_record("2"),
                      "3": {"status": "not_found", "fetchedAt": FETCHED_AT}}
        artifacts.write_data(characters, triggered_by_value="manual", dry_run=False)
        merged = artifacts.load_previous_data()
        self.assertEqual(set(merged), {"1", "2", "3"})
        for rid in ("1", "2"):
            self.assertEqual(set(merged[rid]), set(full_record(rid)),
                             "the folded record is not the record that was written")
            self.assertEqual(merged[rid]["gear"], full_record(rid)["gear"])
            self.assertNotIn("detail", merged[rid], "the pointer is re-derived, never carried")
        self.assertEqual(merged["3"], {"status": "not_found", "fetchedAt": FETCHED_AT})
        # Round-tripped: writing what the reader produced reproduces the same bytes.
        first = config.DATA_FILE.read_bytes()
        first_detail = (config.DETAIL_DIR / "1.json").read_bytes()
        artifacts.write_data(merged, triggered_by_value="manual", dry_run=False)
        self.assertEqual(config.DATA_FILE.read_bytes(), first)
        self.assertEqual((config.DETAIL_DIR / "1.json").read_bytes(), first_detail)

    def test_load_previous_data_reads_a_v2_document_whole(self):
        """The migration reads the committed v2 file through the same reader, so a pre-split row is
        returned untouched rather than as a row missing its gear."""
        v2 = {"schemaVersion": 2, "syncedAt": FETCHED_AT, "triggeredBy": "schedule",
              "characters": {"1": full_record("1")}}
        config.DATA_FILE.write_text(json.dumps(v2), encoding="utf-8")
        merged = artifacts.load_previous_data()
        self.assertEqual(set(merged["1"]), set(full_record("1")))

    def test_load_previous_data_ignores_a_pointer_that_is_not_the_id_derived_path(self):
        """A manifest row is data. A pointer that is not the id-derived path is not followed —
        the same rule js/data.js applies in the browser and the gate applies to the commit."""
        row = artifacts.manifest_row(full_record(), "1")
        row.pop("gear", None)                       # a real manifest row: the heavy half is a pointer
        row["detail"] = "character-data/../../secrets.json"
        poisoned = {"schemaVersion": 3, "syncedAt": FETCHED_AT, "triggeredBy": "manual",
                    "characters": {"1": row}}
        config.DATA_FILE.write_text(json.dumps(poisoned), encoding="utf-8")
        config.DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        (config.DETAIL_DIR / "1.json").write_text(json.dumps(artifacts.detail_document("1", full_record())),
                                                  encoding="utf-8")
        merged = artifacts.load_previous_data()
        self.assertNotIn("gear", merged["1"], "a row that points elsewhere was still followed")

    def test_a_detail_file_that_is_unreadable_degrades_to_the_manifest_row(self):
        artifacts.write_data({"1": full_record("1")}, triggered_by_value="manual", dry_run=False)
        (config.DETAIL_DIR / "1.json").write_text("{ truncated", encoding="utf-8")
        merged = artifacts.load_previous_data()
        self.assertEqual(set(merged["1"]), set(artifacts.MANIFEST_INLINE_KEYS))


# --------------------------------------------------------------------------- #
# 3. The migration
# --------------------------------------------------------------------------- #
class MigrateV3(unittest.TestCase):
    IGNORE = staticmethod(shutil.ignore_patterns(
        ".git", ".env", ".zvec-grep", ".pytest_cache", ".mypy_cache", "__pycache__", "*.pyc",
        "node_modules", "subagents"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tree = self.tmp / "repo"
        shutil.copytree(REPO, self.tree, ignore=self.IGNORE)
        self.script = self.tree / "tools" / "migrate_v3.py"
        self.manifest = self.tree / "data" / "character-data.json"
        self.details = self.tree / "data" / "character-data"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args):
        proc = subprocess.run([sys.executable, str(self.script), *args],
                              capture_output=True, text=True, cwd=self.tree)
        return proc, proc.stdout + proc.stderr

    def test_check_passes_on_the_migrated_working_tree(self):
        """The suite and the gate both run against THIS checkout, so the tree this change ships with
        has to already be migrated — otherwise the nightly's unit-test step fails before the sync
        that would heal it (the exact outage class test_sidecar_phase_c documents)."""
        proc, output = self.run_script("--check")
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertIn("consistency check passed", output)

    def test_a_v2_sidecar_is_migrated_to_manifest_plus_details(self):
        v2 = {"schemaVersion": 2, "syncedAt": FETCHED_AT, "triggeredBy": "schedule",
              "characters": {"1": full_record("1"),
                             "2": {"status": "not_found", "fetchedAt": FETCHED_AT}}}
        self.manifest.write_text(json.dumps(v2, indent=2), encoding="utf-8")
        shutil.rmtree(self.details, ignore_errors=True)

        proc, output = self.run_script()
        self.assertEqual(proc.returncode, 0, output[-2000:])
        doc = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(doc["schemaVersion"], artifacts.DATA_SCHEMA_VERSION)
        self.assertEqual(doc["syncedAt"], FETCHED_AT, "the migration restated a sync timestamp")
        self.assertEqual(doc["triggeredBy"], "schedule")
        self.assertEqual(set(doc["characters"]["1"]) - {"detail"}, set(artifacts.MANIFEST_INLINE_KEYS))
        self.assertEqual(doc["characters"]["1"]["detail"], "character-data/1.json")
        self.assertNotIn("detail", doc["characters"]["2"])
        detail = json.loads((self.details / "1.json").read_text(encoding="utf-8"))
        self.assertEqual(set(detail), set(artifacts.DETAIL_DOC_KEYS))
        self.assertEqual(detail["gear"], full_record()["gear"])
        self.assertFalse((self.details / "2.json").exists())

    def test_a_second_run_writes_zero_bytes(self):
        before = {p.name: p.read_bytes() for p in sorted(self.details.glob("*.json"))}
        before_manifest = self.manifest.read_bytes()
        proc, output = self.run_script()
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertIn("0 action(s)", output)
        self.assertEqual(self.manifest.read_bytes(), before_manifest)
        self.assertEqual({p.name: p.read_bytes() for p in sorted(self.details.glob("*.json"))}, before)

    def test_check_fails_when_a_detail_file_is_missing(self):
        (self.details / sorted(self.details.glob("*.json"))[0].name).unlink()
        proc, output = self.run_script("--check")
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("NOT v3-consistent", output)

    def test_check_fails_when_an_orphan_detail_file_lands_and_a_run_prunes_it(self):
        """A file no row names is a torn write. --check reports it; a real run deletes it — but only
        when it can prove the file is one of ours (see the unclaimable case below)."""
        orphan = self.details / "9999999999999.json"
        doc = json.loads((self.details / f"{self.ok_id()}.json").read_text(encoding="utf-8"))
        doc["id"] = "9999999999999"
        orphan.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        proc, output = self.run_script("--check")
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("orphan", output)

        proc, output = self.run_script()
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertFalse(orphan.exists(), "the run reported the orphan and then left it published")

    def test_check_fails_on_an_unclaimable_file_the_script_refuses_to_delete(self):
        """The other half of pruning safety: a file whose own ``id`` disagrees with its filename is
        not one this script recognises as its own, so it reports it and keeps its hands off it."""
        source = self.details / f"{self.ok_id()}.json"
        shutil.copy(source, self.details / "9999999999999.json")
        proc, output = self.run_script("--check")
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("unclaimed file in the detail directory", output)
        proc, output = self.run_script()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertTrue((self.details / "9999999999999.json").exists(),
                        "the migration deleted a file it could not prove it wrote")

    def ok_id(self):
        doc = json.loads(self.manifest.read_text(encoding="utf-8"))
        return sorted(rid for rid, row in doc["characters"].items()
                      if isinstance(row, dict) and row.get("status") == "ok")[0]

    def test_check_fails_when_a_manifest_row_is_edited(self):
        doc = json.loads(self.manifest.read_text(encoding="utf-8"))
        rid = sorted(doc["characters"])[0]
        doc["characters"][rid]["level"] = 1
        self.manifest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        proc, output = self.run_script("--check")
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("manifest needs a rewrite", output)

    def test_dry_run_writes_nothing(self):
        self.manifest.write_text(json.dumps({"schemaVersion": 2, "syncedAt": FETCHED_AT,
                                             "triggeredBy": "schedule",
                                             "characters": {"1": full_record("1")}}), encoding="utf-8")
        shutil.rmtree(self.details, ignore_errors=True)
        proc, output = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["schemaVersion"], 2)
        self.assertFalse(self.details.exists())

    def test_it_uses_the_writers_and_not_a_second_implementation(self):
        """A migration with its own copy of the row/document shapes is the thing that drifts from
        the nightly two weeks later, so the script is required to call the split."""
        source = self.script.read_text(encoding="utf-8")
        self.assertIn("artifacts.split_characters", source)
        self.assertIn("artifacts.manifest_document", source)
        self.assertIn("artifacts.dumps", source)
        self.assertIn("artifacts.pruned_detail_files", source)
        self.assertNotIn("def manifest_row", source)
        self.assertNotIn("def detail_document", source)


# --------------------------------------------------------------------------- #
# 4. No data loss: the writer applied to the committed v2 rows IS the migration
# --------------------------------------------------------------------------- #
class NoDataLossAgainstTheCommittedRows(unittest.TestCase):
    """The proof this change lives or dies on.

    For every roster id the committed v2 sidecar (git HEAD, before this change) called ``ok``, the
    union of the manifest row's keys and its detail document's keys must cover every key that row
    had — and any key that lands in NO output file is printed and failed. That is the one claim a
    layout change cannot be allowed to lose: the same bytes, in two places, produced by the sync's
    own writer."""

    # The input is a PINNED snapshot, not a moving reference. This class used to read
    # ``git show HEAD:data/character-data.json``, which was a fine way to prove "the migration lost
    # nothing" right up until the migration landed on main and HEAD became v3 — at which point the
    # proof's own input grew a `detail` key and the test reported the split for dropping it. Same
    # family as the UI contracts that used to rent their expectations from nightly data (bfa9e8b):
    # a check is only worth anything when the thing it compares against cannot move underneath it.
    V2_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "character-data-v2.json"

    @classmethod
    def setUpClass(cls):
        cls.v2 = json.loads(cls.V2_FIXTURE.read_text(encoding="utf-8"))
        if cls.v2.get("schemaVersion") != 2:
            raise AssertionError(f"{cls.V2_FIXTURE.name} is schemaVersion "
                                 f"{cls.v2.get('schemaVersion')!r}; this class proves the v2 -> v3 "
                                 "split loses nothing, so its input has to stay a v2 document")
        cls.rows = cls.v2.get("characters") or {}

    def test_every_v2_key_reaches_the_manifest_or_its_detail_file(self):
        covered_by = {}
        missing = {}
        for rid, rec in self.rows.items():
            if not isinstance(rec, dict):
                continue
            row = artifacts.manifest_row(rec, rid)
            detail = artifacts.detail_document(rid, rec) if rec.get("status") == "ok" else {}
            keys = set(row) | set(detail)
            for key in rec:
                covered_by.setdefault(key, 0)
                if key in keys:
                    covered_by[key] += 1
                else:
                    missing.setdefault(key, []).append(rid)
        # Every value survives, not merely every key name.
        for rid, rec in self.rows.items():
            if not isinstance(rec, dict) or rec.get("status") != "ok":
                continue
            row = artifacts.manifest_row(rec, rid)
            detail = artifacts.detail_document(rid, rec)
            merged = {**row, **detail, "status": rec["status"]}
            for key, value in rec.items():
                if key in ("detail",):
                    continue
                self.assertIn(key, merged, f"id {rid}: key {key!r} reached no output file")
                self.assertEqual(merged[key], value, f"id {rid}: key {key!r} changed value")
        self.assertEqual(missing, {}, f"keys that appear in NO output file: "
                                      f"{{k: v[:5] for k, v in missing.items()}}")

    def test_the_only_key_the_committed_rows_can_lose_is_one_nothing_writes(self):
        """If a v2 key belongs to neither half, it is on the record: the split refuses the write
        rather than dropping it, and this names the key so the fix is deliberate."""
        covered = set(artifacts.MANIFEST_INLINE_KEYS) | set(artifacts.DETAIL_BODY_KEYS)
        strays = sorted({key for rec in self.rows.values() if isinstance(rec, dict)
                         for key in rec if key not in covered})
        self.assertEqual(strays, [], f"v2 rows carry {strays}, which is in neither half: widening "
                                     "MANIFEST_ROW_KEYS or DETAIL_DOC_KEYS is the fix, and the "
                                     "writer already refuses the run rather than losing it")

    def test_the_committed_files_are_a_read_write_fixed_point(self):
        """Byte-level, and deliberately about NOW, not about the migration day.

        This test used to re-derive the tree from the pinned v2 fixture and assert byte equality,
        which proved "migrated by this code path" exactly once: the next nightly rewrote
        ``syncedAt`` and the byte comparison expired on schedule (it did, ~3 h after landing).
        The claim that survives a nightly is the invariant the nightly's unchanged-data path
        depends on: ``load_previous_data -> split_characters -> manifest_document`` over the
        CURRENT files reproduces those CURRENT files byte for byte. If that ever breaks, a sync
        that learned nothing still republishes rewritten bytes — churn, and a lost field hiding
        behind it. Timestamps come from the committed document itself, so the envelope is part of
        the fixed point without renting a value that changes every run.
        """
        merged = artifacts.load_previous_data(REPO / "data" / "character-data.json",
                                              REPO / "data" / "character-data")
        rows, details = artifacts.split_characters(merged)
        current = json.loads((REPO / "data" / "character-data.json").read_text(encoding="utf-8"))
        manifest = artifacts.manifest_document(rows, triggered_by_value=current["triggeredBy"],
                                               synced_at=current["syncedAt"])
        self.assertEqual(artifacts.dumps(manifest),
                         (REPO / "data" / "character-data.json").read_text(encoding="utf-8"),
                         "re-deriving from the reader's own output is not a no-op on the manifest")
        for rid, doc in details.items():
            self.assertEqual(artifacts.dumps(doc),
                             (REPO / "data" / "character-data" / f"{rid}.json").read_text(encoding="utf-8"),
                             f"id {rid}: the committed detail file is not what the writer emits")
        # ok-ness comes from the current records, not the fixture: which characters are resolved
        # is the nightly's answer, and renting it is how this class's sibling tests expired.
        self.assertEqual({rid for rid, rec in merged.items()
                          if isinstance(rec, dict) and rec.get("status") == "ok"}, set(details))


# --------------------------------------------------------------------------- #
# 5. The shipped CI gate, run the way the nightly runs it
# --------------------------------------------------------------------------- #
class VerifyGateSidecarRules(unittest.TestCase):
    """Every v3 rule the gate owns, pinned by breaking it in a copy of the repository.

    Same fixture as ``test_sidecar_phase_c.VerifyGateRosterRowRules`` for the same reason: the gate
    resolves ROOT from its own location, so the only faithful way to run it is a copy of the tree
    with the mutation applied to that copy, executed as a subprocess. Each case asserts the gate
    fails on ITS rule, so a relaxation cannot pass by making the whole section vacuous."""

    IGNORE = staticmethod(shutil.ignore_patterns(
        ".git", ".env", ".zvec-grep", ".pytest_cache", ".mypy_cache", "__pycache__", "*.pyc",
        "node_modules", "subagents"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tree = self.tmp / "repo"
        shutil.copytree(REPO, self.tree, ignore=self.IGNORE)
        self.gate = self.tree / ".pi" / "scripts" / "verify_sync_artifacts.py"
        self.manifest = self.tree / "data" / "character-data.json"
        self.details = self.tree / "data" / "character-data"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_gate(self):
        proc = subprocess.run([sys.executable, str(self.gate)], capture_output=True, text=True)
        return proc, proc.stdout + proc.stderr

    def fails(self, output):
        return [line[len("  FAIL "):] for line in output.splitlines() if line.startswith("  FAIL ")]

    def rewrite_manifest(self, mutate):
        doc = json.loads(self.manifest.read_text(encoding="utf-8"))
        mutate(doc)
        self.manifest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return doc

    def ok_ids(self):
        doc = json.loads(self.manifest.read_text(encoding="utf-8"))
        return sorted(rid for rid, row in doc["characters"].items()
                      if isinstance(row, dict) and row.get("status") == "ok")

    # -- the fixture's own baseline ------------------------------------------- #
    def test_the_migrated_tree_passes_the_gate_clean(self):
        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertIn("0 failed", output)

    def test_the_gate_and_the_writer_agree_on_both_key_sets(self):
        gate = (REPO / ".pi" / "scripts" / "verify_sync_artifacts.py").read_text(encoding="utf-8")
        manifest_keys = set(re.findall(r'"(\w+)"', re.search(
            r"MANIFEST_KEYS = \{(.*?)\}", gate, flags=re.S).group(1)))
        detail_keys = set(re.findall(r'"(\w+)"', re.search(
            r"DETAIL_KEYS = \{(.*?)\}", gate, flags=re.S).group(1)))
        self.assertEqual(manifest_keys, set(artifacts.MANIFEST_ROW_KEYS),
                         "verify_sync_artifacts.MANIFEST_KEYS and bnet.artifacts.MANIFEST_ROW_KEYS diverged")
        self.assertEqual(detail_keys, set(artifacts.DETAIL_DOC_KEYS),
                         "verify_sync_artifacts.DETAIL_KEYS and bnet.artifacts.DETAIL_DOC_KEYS diverged")

    # -- coverage -------------------------------------------------------------- #
    def test_every_ok_row_has_exactly_one_detail_file_and_every_other_row_none(self):
        doc = json.loads(self.manifest.read_text(encoding="utf-8"))
        ok_rows = {rid for rid, row in doc["characters"].items() if row.get("status") == "ok"}
        files = {p.stem for p in self.details.glob("*.json")}
        self.assertEqual(ok_rows, files)
        for rid, row in doc["characters"].items():
            if row.get("status") != "ok":
                self.assertNotIn("detail", row)
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 0, output[-2000:])

    def test_a_missing_detail_file_fails_the_gate(self):
        rid = self.ok_ids()[0]
        (self.details / f"{rid}.json").unlink()
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"detail {rid}: the file its manifest row names", " ".join(self.fails(output)))

    def test_an_orphan_detail_file_fails_the_gate(self):
        rid = self.ok_ids()[0]
        shutil.copy(self.details / f"{rid}.json", self.details / "9999999999999.json")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1)
        joined = " ".join(self.fails(output))
        self.assertIn("holds no file that is not named by exactly one manifest row", joined)
        self.assertIn("9999999999999.json", joined)

    # -- the detail-equality rule ---------------------------------------------- #
    def test_a_row_pointing_at_another_characters_file_fails_the_gate(self):
        """The interesting poisoning, and one no shape check can see: both strings are perfectly
        well-formed paths, and the document on the end is a real Blizzard-derived row."""
        ids = self.ok_ids()
        a, b = ids[0], ids[1]

        def aim_at_somebody_else(doc):
            doc["characters"][a]["detail"] = f"character-data/{b}.json"

        self.rewrite_manifest(aim_at_somebody_else)
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertEqual(self.fails(output),
                         [f"manifest row {a} (ok) points at exactly its own detail file "
                          f"('character-data/{b}.json' != 'character-data/{a}.json')"], output[-2000:])

    def test_every_traversal_shape_is_refused_by_the_same_comparison(self):
        rid = self.ok_ids()[0]
        for bad in ("../character-data.json", "%2e%2e/character-data.json",
                    "character-data\\..\\character-data.json",
                    "https://attacker.tld/beacon.json", "//attacker.tld/x.json",
                    "character-data/other.json", "character-data/character-data/1.json", "",
                    "character-data/" + rid + ".json/", "CHARACTER-DATA/" + rid + ".json"):
            with self.subTest(detail=bad):
                self.rewrite_manifest(lambda doc: doc["characters"][rid].__setitem__("detail", bad))
                proc, output = self.run_gate()
                self.assertEqual(proc.returncode, 1, f"{bad!r} passed the gate\n{output[-1500:]}")
                self.assertTrue(any(f"manifest row {rid} (ok) points at exactly its own detail file" in f
                                    for f in self.fails(output)), f"{bad!r}: {self.fails(output)[:3]}")

    def test_a_non_ok_row_carrying_a_detail_pointer_fails_the_gate(self):
        def hand_a_pointer_to_a_not_found_row(doc):
            target = next(rid for rid, row in doc["characters"].items()
                          if row.get("status") != "ok")
            doc["characters"][target]["detail"] = f"character-data/{target}.json"

        self.rewrite_manifest(hand_a_pointer_to_a_not_found_row)
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertTrue(any("carries no detail pointer" in f for f in self.fails(output)),
                        output[-2000:])

    def test_a_null_detail_fails_too(self):
        def write_null(doc):
            doc["characters"][self.ok_ids()[0]]["detail"] = None

        self.rewrite_manifest(write_null)
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertTrue(any("points at exactly its own detail file" in f for f in self.fails(output)))

    # -- the detail document's own rules --------------------------------------- #
    def test_a_detail_document_carrying_status_fails_the_gate(self):
        rid = self.ok_ids()[0]
        path = self.details / f"{rid}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["status"] = "ok"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        joined = " ".join(self.fails(output))
        self.assertIn(f"detail {rid}: carries NO status", joined)
        self.assertIn("carries none of the manifest-owned fields", joined)

    def test_a_detail_document_carrying_a_manifest_field_fails_the_gate(self):
        rid = self.ok_ids()[0]
        path = self.details / f"{rid}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["level"] = 1
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("'level'", " ".join(self.fails(output)))

    def test_a_detail_document_whose_id_disagrees_with_its_filename_fails(self):
        rid = self.ok_ids()[0]
        path = self.details / f"{rid}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["id"] = self.ok_ids()[1]
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn(f"detail {rid}: its own id equals the roster id", " ".join(self.fails(output)))

    def test_the_relocated_content_checks_still_run_against_the_detail_file(self):
        """The gear, stats, talent, profession, reputation and media rules moved; they did not go
        soft. Each mutation is applied to a DETAIL document and has to be caught there."""
        rid = self.ok_ids()[0]
        path = self.details / f"{rid}.json"
        cases = {
            "gear key": lambda d: d["gear"].append({"slot": "X", "bogus": 1}),
            "gear host": lambda d: d["gear"][0].__setitem__("iconUrl", "https://attacker.tld/i.jpg"),
            "raw blob": lambda d: d.__setitem__("_links", {}),
            "reputation standing": lambda d: d["reputations"][0].__setitem__("standing", {"raw": "x"}),
            "talent group": lambda d: d["talents"][0].__setitem__("bogus", 1),
            "profession key": lambda d: d["professions"]["primaries"][0].__setitem__("bogus", 1),
            "media key": lambda d: d["media"].__setitem__("inset", None),
            "media host": lambda d: d["media"].__setitem__("avatar", "https://attacker.tld/a.jpg"),
            "stat value": lambda d: d["stats"].__setitem__("strength", "10"),
        }
        baseline = path.read_text(encoding="utf-8")
        caught = []
        for label, mutate in cases.items():
            path.write_text(baseline, encoding="utf-8")
            doc = json.loads(baseline)
            mutate(doc)
            path.write_text(json.dumps(doc), encoding="utf-8")
            proc, output = self.run_gate()
            if proc.returncode == 1 and any(f.startswith(f"detail {rid}") or
                                            "raw Blizzard payload keys" in f or
                                            "non-Blizzard URL" in f for f in self.fails(output)):
                caught.append(label)
            else:
                self.fail(f"{label}: a mutated DETAIL document passed the gate ({self.fails(output)[:2]})")
        self.assertEqual(len(caught), len(cases))
        path.write_text(baseline, encoding="utf-8")

    # -- budgets ---------------------------------------------------------------- #
    def test_the_three_budgets_are_named_and_quoted(self):
        gate = (REPO / ".pi" / "scripts" / "verify_sync_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("MAX_MANIFEST_BYTES = 128 * 1024", gate)
        self.assertIn("MAX_DETAIL_BYTES = 96 * 1024", gate)
        self.assertIn("MAX_DETAIL_DIR_BYTES = 4 * 1024 * 1024", gate)
        self.assertIn("MAX_MAP_BYTES = 128 * 1024", gate)
        self.assertNotIn("MAX_ARTIFACT_BYTES", gate)
        # The budgets must EARN their numbers: the block that declares them quotes the measured
        # byte sizes of the shipped artifacts (and why the old single ceiling was outgrown), rather
        # than being a constant nobody can argue with. Pinned as a shape, not as the exact figures,
        # because the nightly rewrites those files every day.
        block = re.search(r"# --- artifact budgets(.*?)MAX_MAP_BYTES", gate, flags=re.S).group(1)
        self.assertIn("measured", block)
        self.assertTrue(re.search(r"\d{1,3},\d{3} B", block),
                        "the budget block quotes no measured byte size")

    def test_a_detail_file_over_its_budget_fails_the_gate(self):
        gate = (REPO / ".pi" / "scripts" / "verify_sync_artifacts.py").read_text(encoding="utf-8")
        budget = int(re.search(r"MAX_DETAIL_BYTES = (\d+) \* (\d+)", gate).group(1)) * int(
            re.search(r"MAX_DETAIL_BYTES = (\d+) \* (\d+)", gate).group(2))
        rid = self.ok_ids()[0]
        path = self.details / f"{rid}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        while len(json.dumps(doc, indent=2)) <= budget:
            doc["gear"].append(dict(doc["gear"][0]))
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertTrue(any(f"detail {rid} is under {budget} bytes" in f for f in self.fails(output)),
                        self.fails(output)[:3])

    def test_the_committed_artifacts_sit_inside_their_budgets(self):
        manifest_bytes = len(self.manifest.read_bytes())
        detail_bytes = [len(p.read_bytes()) for p in self.details.glob("*.json")]
        self.assertLess(manifest_bytes, 128 * 1024)
        self.assertLess(max(detail_bytes), 96 * 1024)
        self.assertLess(sum(detail_bytes), 4 * 1024 * 1024)

    # -- the manifest envelope --------------------------------------------------- #
    def test_a_v2_manifest_fails_the_gate(self):
        self.rewrite_manifest(lambda doc: doc.__setitem__("schemaVersion", 2))
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("data schemaVersion == 3", " ".join(self.fails(output)))

    def test_the_envelope_still_has_exactly_the_four_keys(self):
        self.assertEqual(list(json.loads(self.manifest.read_text(encoding="utf-8"))),
                         ["schemaVersion", "syncedAt", "triggeredBy", "characters"])

    def test_a_manifest_row_with_a_detail_only_key_fails_the_unknown_key_rule(self):
        rid = self.ok_ids()[0]
        self.rewrite_manifest(lambda doc: doc["characters"][rid].__setitem__("gear", []))
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn(f"manifest row {rid} carries only the list-view keys",
                      " ".join(self.fails(output)))

    def test_the_secret_and_foreign_url_scans_reach_every_detail_file(self):
        gate = (REPO / ".pi" / "scripts" / "verify_sync_artifacts.py").read_text(encoding="utf-8")
        self.assertIn('f"data/character-data/{rid}.json": detail_texts[rid]', gate)
        self.assertIn('("data/character-data.json", data_text)]', gate)
        # …and behaviourally: a foreign URL in ONE detail file is a finding.
        rid = self.ok_ids()[0]
        path = self.details / f"{rid}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["reputations"][0]["factionName"] = "beacon at https://evil.tld/x"
        path.write_text(json.dumps(doc), encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn(f"data/character-data/{rid}.json contains no non-Blizzard URL",
                      " ".join(self.fails(output)))

    # -- the workflows ----------------------------------------------------------- #
    def test_the_sync_workflow_stages_the_directory_with_a_reasoned_A(self):
        workflow = (REPO / ".github" / "workflows" / "sync-bnet.yml").read_text(encoding="utf-8")
        self.assertTrue(any(re.match(r"^\s*git add -A data/character-data data/character-data\.json", ln)
                            for ln in workflow.splitlines()), workflow)
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 0, output[-2000:])

    def test_a_directory_add_without_A_fails_the_gate(self):
        workflow = self.tree / ".github" / "workflows" / "sync-bnet.yml"
        text = workflow.read_text(encoding="utf-8").replace(
            "git add -A data/character-data data/character-data.json",
            "git add data/character-data data/character-data.json")
        workflow.write_text(text, encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("stages the detail directory with -A", " ".join(self.fails(output)))

    def test_a_broad_A_still_fails_the_gate(self):
        workflow = self.tree / ".github" / "workflows" / "sync-bnet.yml"
        text = workflow.read_text(encoding="utf-8").replace(
            "git add -A data/character-data data/character-data.json", "git add -A data/")
        workflow.write_text(text, encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("no directory-wide add", " ".join(self.fails(output)))

    def test_pages_yml_copies_the_detail_directory(self):
        pages = (REPO / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        self.assertIn("cp -r data/character-data _site/data/", pages)
        self.assertRegex(pages, r"find _site -type f \| sort")

    def test_pages_yml_without_the_recursive_copy_fails_the_gate(self):
        pages = self.tree / ".github" / "workflows" / "pages.yml"
        text = pages.read_text(encoding="utf-8").replace("cp -r data/character-data _site/data/",
                                                         "cp data/*.json _site/data/again/")
        pages.write_text(text, encoding="utf-8")
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn("copies the per-character detail directory", " ".join(self.fails(output)))


if __name__ == "__main__":
    unittest.main()
