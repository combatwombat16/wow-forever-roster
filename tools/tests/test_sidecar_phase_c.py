#!/usr/bin/env python3
"""Offline unit tests for the Phase C sync side: additive guild-member auto-add.

Phase C (roster-ui-tasks.md) makes the nightly a writer of ``data/characters.json`` — the
one data file a human edits by hand — so everything here is pinned around the single
sentence that has to stay true: **the sync may only append.**

Three layers, because they fail differently:

* ``bnet.autoadd.plan`` is pure and holds the interesting invariants — a member the roster
  already lists yields nothing (case- and whitespace-insensitively, mirroring
  ``resolve.member_norm_key`` and ``resolve.norm_slug``), a member it does not yields
  exactly the owner-approved placeholder row, a second run yields nothing at all, and the
  caller's rows are never touched;
* ``bnet.artifacts.roster_append_refusal`` is the write-site guard: append passes, and a
  mutated, removed or reordered row is refused;
* ``Sync.run_sync`` is driven with a fake client so the placement claim is proven rather than
  asserted in a comment — an auto-added id carries a map entry and a sidecar row in the run
  that created it (that is what keeps ``set(map entries) == set(roster ids)`` true in the
  gate), the guild roster is fetched once, and nothing is published in dry-run.

The last group imports the shipped verify gate itself and proves the widened stored-state
vocabulary still REJECTS anything that is not ``validated`` / ``pending`` — a gate widened
without a test goes soft the first time someone copy-pastes it.

``VerifyGateRosterRowRules`` is deliberately heavy, because review C1 was a one-space bug:
``note = v.get("note")`` and ``reported_ids.add(rec["id"])`` had drifted inside
``if "guild" in rec:``, which made the note ceiling and the design §2 reported-id rule dead for
every guild-less row and crashed the whole gate on a row carrying ``guild`` with no
``validation`` block. Importing the module and calling a helper cannot see an indentation
mistake, so those tests copy the repository to a temp dir, mutate its ``data/`` documents and
run ``.pi/scripts/verify_sync_artifacts.py`` the way the nightly's CI step runs it.

That class also carries a fixture invariant of its own: a gate run inside it fails on the
fixture's own mutation and on nothing else. Some of the gate's rules are cross-file mirrors that
only the nightly sync writes, and a committed tree is legitimately inconsistent between a curated
roster edit and the next sync — so ``setUp`` brings its copy into sync consistency by re-running
the SHIPPED writer over the copied data (``sync_consistent_entry`` /
``VerifyGateRosterRowRules.bring_tree_into_sync_consistency``) rather than by patching a list of
field names. A repair that names fields is whack-a-mole: it cost an outage once already.

Stdlib unittest only: no network, no credentials, every write redirected to a temp dir.

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import argparse
import copy
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/

from bnet import artifacts, autoadd, config, fetch, http, resolve, roster, sync  # noqa: E402

API = config.API_BASE
GUILD_SLUG = "flowmasters"
GUILD_NAME = "Flowmasters"
GUILD_URL = (f"{API}{config.GUILD_ROSTER_PATH.format(slug=GUILD_SLUG)}"
             f"?namespace={config.NS_PROFILE}")
SECOND_GUILD_URL = (f"{API}{config.GUILD_ROSTER_PATH.format(slug='heghmoh')}"
                    f"?namespace={config.NS_PROFILE}")


def member(name, surname=None, slug=None, level=20):
    """One ``guild-roster`` member as the API returns it (name/surname kept apart)."""
    ch = {"name": name, "id": 10000 + abs(hash(name)) % 1000, "level": level}
    if surname is not None:
        ch["surname"] = surname
    ch["slug"] = slug or resolve.norm_slug(f"{name} {surname or ''}")
    return {"character": ch, "rank": 0}


def profile(name, surname=None, guild_slug=None, guild_name=None):
    """A profile payload with the self-href the resolver reads, plus the guild node."""
    slug = resolve.norm_slug(f"{name} {surname or ''}")
    payload = {"name": name, "level": 20,
               "_links": {"self": {"href": f"{API}/profile/wow/character/{slug}"
                                      f"?namespace={config.NS_PROFILE}"}}}
    if surname is not None:
        payload["surname"] = surname
    if guild_slug:
        payload["guild"] = {"slug": guild_slug, "name": guild_name or guild_slug, "id": 246033}
    return payload


def load_verify_gate(name="verify_sync_artifacts"):
    """The shipped CI gate as an importable module (it is a script, so it has to be loaded)."""
    path = Path(__file__).resolve().parents[2] / ".pi" / "scripts" / "verify_sync_artifacts.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def doc_row(document, rid):
    """One roster row out of a parsed ``characters.json`` document, by id."""
    return next(r for r in document["characters"] if r["id"] == rid)


def guild_roster(members):
    return {"members": members, "guild": {"slug": GUILD_SLUG, "name": GUILD_NAME, "id": 246033}}


def row(rid, name, **extra):
    rec = {"id": rid, "player": "someone", "name": name, "track": "family",
           "role": "DPS", "roleSec": "None", "race": "Tauren", "class": "Druid",
           "prof1": "Mining", "prof2": "None"}
    rec.update(extra)
    return rec


class FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload
        self.headers = {}

    @property
    def json(self):
        return self._payload


class FakeClient:
    """Answers profile GETs from a {slug: payload} table and any URL from ``extra``.

    Records every URL in send order, which is how the "no double fetch of a guild roster"
    claim is checked rather than asserted."""

    def __init__(self, table=None, extra=None):
        self.table = dict(table or {})
        self.extra = dict(extra or {})
        self.urls = []
        self.tokens_minted = 1
        self.requests_made = 0
        self.revalidations = 0
        self.status_counts = {}

    def get(self, url):
        self.urls.append(url)
        self.requests_made += 1
        if url in self.extra:
            return FakeResp(*self.extra[url])
        slug = resolve.slug_from_href(url)
        if slug in self.table:
            return FakeResp(200, self.table[slug])
        return FakeResp(404, None)

    def mint_token(self, reason):
        return "token"


def plan(roster_records, guilds, seed=1799000000000):
    return autoadd.plan(roster_records, guilds, seed=seed)


def guilds_of(*pairs):
    """``{slug: {"name", "members"}}`` from ``(slug, name, members)`` triples."""
    return {slug: {"name": name, "members": members} for slug, name, members in pairs}


class PlanMatchesExistingRows(unittest.TestCase):
    """A member the roster already lists never produces a row — under any spelling."""

    def test_exact_match_adds_nothing(self):
        rows = [row("1", "Beef Pho")]
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME, [member("Beef", "Pho")]))
        self.assertEqual(plan(rows, guilds), [])

    def test_case_and_whitespace_are_not_new_characters(self):
        rows = [row("1", "  bEEf   pho ")]
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME, [member(" BeEf ", " PHO ")]))
        self.assertEqual(plan(rows, guilds), [])

    def test_surname_less_member_matches_a_single_word_row(self):
        self.assertEqual(plan([row("1", "Huntress")],
                              guilds_of((GUILD_SLUG, GUILD_NAME, [member("Huntress")]))), [])

    def test_a_member_is_matched_against_every_row_not_just_the_first(self):
        rows = [row("1", "Alpha One"), row("2", "Bravo Two"), row("3", "Beef Pho")]
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME, [member("Beef", "Pho")]))
        self.assertEqual(plan(rows, guilds), [])

    def test_the_norm_key_form_matches_even_when_the_row_hyphenates_it(self):
        # mirror resolve.member_norm_key: the lever's own key has to count as a match too.
        rows = [row("1", "tir-rheebeard")]
        self.assertEqual(resolve.member_norm_key(member("Tir", "Rheebeard")), "tir-rheebeard")
        self.assertEqual(plan(rows, guilds_of((GUILD_SLUG, GUILD_NAME,
                                               [member("Tir", "Rheebeard")]))), [])

    def test_an_unnameable_member_is_skipped_not_stored_as_a_blank_row(self):
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME, [
            {}, {"character": {}}, {"character": {"name": "   "}},
            {"character": {"name": None, "surname": "Pho"}}, "not-a-dict"]))
        self.assertEqual(plan([], guilds), [])


class PlanRowSpec(unittest.TestCase):
    """The miss case: exactly the owner-approved placeholder row, and nothing else."""

    def test_a_miss_appends_the_exact_spec(self):
        added = plan([row("1", "Beef Pho")],
                     guilds_of((GUILD_SLUG, GUILD_NAME, [member("Newbie", "Newface")])),
                     seed=1799000000000)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0], {
            "id": "1799000000000",
            "player": "Newbie",                      # placeholder alias, the member's own name
            "name": "Newbie Newface",                # the roster's single-name-field convention
            "track": "alts",
            "role": "None", "roleSec": "None", "prof1": "None", "prof2": "None",
            "guild": "Flowmasters",
            "validation": {"state": "pending"},
        })

    def test_id_is_a_fresh_13_digit_numeric_string_and_never_an_existing_one(self):
        existing = [row("1799000000000", "Beef Pho"), row("1799000000001", "Alpha One")]
        added = plan(existing,
                     guilds_of((GUILD_SLUG, GUILD_NAME,
                                [member("Newbie", "Newface"), member("Other", "Alt")])),
                     seed=1799000000000)      # the seed itself is taken, twice over
        ids = [r["id"] for r in added]
        self.assertTrue(all(len(i) == 13 and i.isdigit() for i in ids), ids)
        self.assertFalse(set(ids) & {"1799000000000", "1799000000001"}, ids)
        self.assertEqual(len(set(ids)), len(ids), ids)

    def test_a_surname_less_new_member_still_gets_a_name_without_a_trailing_space(self):
        added = plan([], guilds_of((GUILD_SLUG, GUILD_NAME, [member("Boomster", None)])))
        self.assertEqual(added[0]["name"], "Boomster")
        self.assertEqual(added[0]["player"], "Boomster")

    def test_race_and_class_are_omitted_rather_than_guessed(self):
        added = plan([], guilds_of((GUILD_SLUG, GUILD_NAME,
                                    [member("Newbie", "Newface")])))[0]
        self.assertNotIn("race", added)
        self.assertNotIn("class", added)

    def test_the_guild_name_falls_back_to_blizzards_own_slug_when_a_profile_omitted_it(self):
        added = plan([], guilds_of((GUILD_SLUG, "", [member("Newbie")])))[0]
        self.assertEqual(added["guild"], GUILD_SLUG)

    def test_field_order_is_the_canonical_one_with_guild_then_validation_last(self):
        added = plan([], guilds_of((GUILD_SLUG, GUILD_NAME, [member("Newbie")])))[0]
        self.assertEqual(list(added), ["id", "player", "name", "track", "role", "roleSec",
                                       "prof1", "prof2", "guild", "validation"])


class PlanIsIdempotentAndAdditive(unittest.TestCase):
    def test_a_second_run_over_its_own_output_adds_nothing(self):
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME,
                            [member("Newbie", "Newface"), member("Beef", "Pho")]))
        first = [row("1", "Beef Pho")]
        added = plan(first, guilds)
        self.assertEqual(len(added), 1)
        self.assertEqual(plan(first + added, guilds), [])
        self.assertEqual(plan(first + added + plan(first + added, guilds), guilds), [])

    def test_the_same_member_visible_in_two_guilds_yields_one_row(self):
        shared = member("Shared", "Char", slug="shared-char")
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME, [shared]),
                           ("heghmoh", "Heghmoh", [shared]))
        added = plan([], guilds)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "Shared Char")

    def test_two_guilds_each_contribute_their_own_rows_ordered_by_slug(self):
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME, [member("Flowie", "Member")]),
                           ("heghmoh", "Heghmoh", [member("Hegh", "Member")]))
        added = plan([], guilds)
        self.assertEqual([r["name"] for r in added], ["Flowie Member", "Hegh Member"])
        self.assertEqual([r["guild"] for r in added], [GUILD_NAME, "Heghmoh"])
        self.assertTrue(all(r["track"] == "alts" for r in added))

    def test_the_input_rows_are_never_mutated_reordered_or_removed(self):
        original = [row("1", "Beef Pho"), row("2", "Alpha One"), row("3", "Bravo Two")]
        before = copy.deepcopy(original)
        guilds = guilds_of((GUILD_SLUG, GUILD_NAME,
                            [member("Beef", "Pho"), member("Newbie"), member("Another")]))
        added = plan(original, guilds)
        self.assertGreater(len(added), 0)
        self.assertEqual(original, before)
        self.assertEqual([r["id"] for r in original], [r["id"] for r in before])
        # ... and the emitted rows are new objects, not aliases of anything handed in.
        self.assertTrue(all(r not in original for r in added))

    def test_no_guilds_discovered_means_no_rows(self):
        self.assertEqual(plan([row("1", "Beef Pho")], {}), [])
        self.assertEqual(plan([row("1", "Beef Pho")], None), [])


class AppendGuard(unittest.TestCase):
    """The write-site contract: append is legal, everything else is refused."""

    ROWS = [row("1", "Beef Pho"), row("2", "Alpha One")]

    def test_appending_passes_and_empty_previous_passes(self):
        appended = self.ROWS + [row("3", "Newbie Newface")]
        self.assertIsNone(artifacts.roster_append_refusal(self.ROWS, appended))
        self.assertIsNone(artifacts.roster_append_refusal([], [row("9", "Whatever")]))

    def test_a_mutated_row_is_refused(self):
        # The comparator, handed two separate object graphs. True of any snapshot implementation
        # — see SyncRunAppendsAndResolves for the test that pins the CALL site (review C3).
        changed = copy.deepcopy(self.ROWS)
        changed[0]["track"] = "flowmasters"
        self.assertIn("would mutate roster row",
                      artifacts.roster_append_refusal(self.ROWS, changed))

    def test_a_reordered_roster_is_refused(self):
        self.assertIn("would mutate roster row",
                      artifacts.roster_append_refusal(self.ROWS, list(reversed(self.ROWS))))

    def test_a_removed_row_is_refused(self):
        self.assertIn("would remove roster rows",
                      artifacts.roster_append_refusal(self.ROWS, self.ROWS[:1]))

    def test_a_rekeyed_row_is_refused_even_with_identical_values(self):
        reordered_keys = [{"id": r["id"], "name": r["name"], **{k: v for k, v in r.items()
                                                                if k not in ("id", "name")}}
                          for r in self.ROWS]
        self.assertEqual(reordered_keys, self.ROWS)          # same values, different key order
        self.assertIn("would mutate roster row",
                      artifacts.roster_append_refusal(self.ROWS, reordered_keys))

    def test_write_roster_emits_the_schema_version_2_envelope(self):
        tmp = Path(tempfile.mkdtemp()) / "characters.json"
        previous = config.ROSTER_FILE
        try:
            config.ROSTER_FILE = tmp
            doc = artifacts.write_roster(self.ROWS, dry_run=True)
            self.assertFalse(tmp.exists(), "dry-run wrote a roster")
            doc = artifacts.write_roster(self.ROWS, dry_run=False)
            self.assertEqual(list(doc), ["schemaVersion", "characters"])
            self.assertEqual(doc["schemaVersion"], 2)
            self.assertEqual(json.loads(tmp.read_text(encoding="utf-8")), doc)
        finally:
            config.ROSTER_FILE = previous


class SyncRunAppendsAndResolves(unittest.TestCase):
    """The placement claim, tested: an auto-added id is a full citizen of its own run.

    Post-write gate shape checked for the new-row case: the map and the sidecar cover
    exactly the roster ids (including the appended one), the new map row's ``rosterName``
    matches the appended row, an ``ok`` new row carries slug/href/bnetName/mappedVia, and a
    non-``ok`` one carries a note — the four gate clauses a late placement would break.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.roster_file = self.tmp / "characters.json"
        self.map_file = self.tmp / "battle-net-map.json"
        self.data_file = self.tmp / "character-data.json"
        self._files = (config.ROSTER_FILE, config.MAP_FILE, config.DATA_FILE)
        config.ROSTER_FILE, config.MAP_FILE, config.DATA_FILE = (
            self.roster_file, self.map_file, self.data_file)
        self._step_summary = __import__("os").environ.pop("GITHUB_STEP_SUMMARY", None)

    def tearDown(self):
        config.ROSTER_FILE, config.MAP_FILE, config.DATA_FILE = self._files
        if self._step_summary is not None:
            __import__("os").environ["GITHUB_STEP_SUMMARY"] = self._step_summary

    def run_sync_with(self, records, prev_entries, table, extra=None, dry_run=False,
                      mutate=None):
        """Drive one ``run_sync``. ``mutate`` is a hook for the C3 pair of tests: it is called
        with the very roster list the run holds, from ``resolver.dedupe_slugs`` — the last step
        before ``artifacts.roster_append_refusal`` reads its snapshot — which is exactly the
        window an in-run bug would mutate a row in."""
        args = argparse.Namespace(dry_run=dry_run, lookup=None, force=False,
                                  state_dir=self.tmp / "state")
        s = sync.Sync(args)
        cache = http.Cache(self.tmp / "state", dry_run=dry_run)
        cache.load()
        client = FakeClient(table, extra)
        f = fetch.Fetcher(client, cache)
        s.cache, s.client, s.f, s.resolver = cache, client, f, resolve.Resolver(f)
        f.enrich = lambda w: {"status": "ok", "fetchedAt": http.iso_now(), "level": 20}
        if mutate is not None:
            original_dedupe = s.resolver.dedupe_slugs

            def dedupe_then_mutate(work, ids):
                original_dedupe(work, ids)
                mutate(records)

            s.resolver.dedupe_slugs = dedupe_then_mutate
        out = io.StringIO()
        with redirect_stdout(out):
            code = s.run_sync(records, prev_entries)
        return code, out.getvalue(), client

    def one_known_member_and_one_visible_stranger(self):
        return ([row("1", "Alpha One")],
                {},                                     # no previous map
                {"alpha-one": profile("Alpha", "One", GUILD_SLUG, GUILD_NAME),
                 "newbie-newface": profile("Newbie", "Newface", GUILD_SLUG, GUILD_NAME)},
                {GUILD_URL: (200, guild_roster([member("Alpha", "One"),
                                                member("Newbie", "Newface")]))})

    def test_visible_stranger_is_appended_resolved_and_covered_by_both_artifacts(self):
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()
        code, out, client = self.run_sync_with(records, prev, table, extra)
        self.assertEqual(code, 0, out)

        written = json.loads(self.roster_file.read_text(encoding="utf-8"))
        self.assertEqual(written["schemaVersion"], 2)
        self.assertEqual([r["name"] for r in written["characters"]], ["Alpha One",
                                                                     "Newbie Newface"])
        self.assertEqual(written["characters"][0], row("1", "Alpha One"))   # untouched, in place
        new = written["characters"][1]
        self.assertEqual(new["guild"], GUILD_NAME)
        self.assertEqual(new["validation"], {"state": "pending"})

        entries = json.loads(self.map_file.read_text(encoding="utf-8"))["entries"]
        sidecar = json.loads(self.data_file.read_text(encoding="utf-8"))["characters"]
        roster_ids = [r["id"] for r in written["characters"]]
        self.assertEqual(set(entries), set(roster_ids), "map covers exactly the roster ids")
        self.assertEqual(set(sidecar), set(roster_ids), "sidecar covers exactly the roster ids")
        self.assertEqual(entries[new["id"]]["rosterName"], new["name"])
        self.assertEqual(entries[new["id"]]["status"], "ok", entries[new["id"]])
        self.assertEqual(entries[new["id"]]["slug"], "newbie-newface")
        self.assertEqual(entries[new["id"]]["mappedVia"], "guild_roster")
        self.assertTrue(entries[new["id"]]["bnetName"])
        self.assertTrue(entries[new["id"]]["href"].startswith(f"{API}/profile/wow/character/"))

    def test_the_guild_roster_is_fetched_once_across_the_lever_and_the_auto_add(self):
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()
        _code, _out, client = self.run_sync_with(records, prev, table, extra)
        self.assertEqual(client.urls.count(GUILD_URL), 1, client.urls)

    def test_a_second_run_appends_nothing_and_does_not_rewrite_the_roster(self):
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()
        code, _out, _client = self.run_sync_with(records, prev, table, extra)
        self.assertEqual(code, 0)
        first_bytes = self.roster_file.read_bytes()
        grown = json.loads(self.roster_file.read_text(encoding="utf-8"))["characters"]
        prev_entries = artifacts.load_previous_map(self.map_file)

        code, out, client = self.run_sync_with(grown, prev_entries, table, extra)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.roster_file.read_bytes(), first_bytes, "second run mutated the roster")
        self.assertNotIn("pending row(s) appended", out)
        self.assertEqual(client.urls.count(GUILD_URL), 1, client.urls)

    def test_a_new_member_the_api_will_not_answer_still_gets_both_rows_and_a_note(self):
        records, prev, table, _extra = self.one_known_member_and_one_visible_stranger()
        extra = {GUILD_URL: (200, guild_roster([member("Ghost", "Member")]))}
        code, out, _client = self.run_sync_with(records, prev, table, extra)
        self.assertEqual(code, 0, out)
        entries = json.loads(self.map_file.read_text(encoding="utf-8"))["entries"]
        sidecar = json.loads(self.data_file.read_text(encoding="utf-8"))["characters"]
        written = json.loads(self.roster_file.read_text(encoding="utf-8"))["characters"]
        self.assertEqual(len(written), 2)
        new_id = written[1]["id"]
        self.assertEqual(set(entries), {r["id"] for r in written})
        self.assertEqual(set(sidecar), {r["id"] for r in written})
        self.assertNotEqual(entries[new_id]["status"], "ok")
        self.assertTrue(str(entries[new_id]["note"]).strip(), "a non-ok new row needs a note")

    def test_a_dead_guild_roster_adds_nothing_and_is_reported(self):
        records, prev, table, _extra = self.one_known_member_and_one_visible_stranger()
        extra = {GUILD_URL: (503, None)}
        code, out, _client = self.run_sync_with(records, prev, table, extra)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.roster_file.exists(), "a guild that never answered appended rows")
        self.assertIn(GUILD_SLUG, out)

    def test_dry_run_appends_nothing_to_disk(self):
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()
        code, out, _client = self.run_sync_with(records, prev, table, extra, dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertIn("pending row(s) appended", out)
        self.assertFalse(self.roster_file.exists(), "dry-run wrote the roster")
        self.assertFalse(self.map_file.exists(), "dry-run wrote the map")

    def test_an_in_run_edit_of_a_held_row_is_refused(self):
        """Review C3, at the call site: ``sync.py`` snapshots the roster and then keeps working on
        the same row objects, so the snapshot has to be deep. Driven through ``run_sync`` rather
        than by calling the comparator, because the comparator was always correct."""
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()

        def edit_a_held_row(recs):
            recs[0]["track"] = "flowmasters"          # same dict the run resolved and will write

        code, out, _client = self.run_sync_with(records, prev, table, extra,
                                                mutate=edit_a_held_row)
        self.assertEqual(code, 1, out)
        self.assertIn("would mutate roster row", out)
        self.assertFalse(self.roster_file.exists(), "a run that mutated a row wrote characters.json")
        self.assertFalse(self.map_file.exists(), "a run that mutated a row wrote the map")
        self.assertFalse(self.data_file.exists(), "a run that mutated a row wrote the sidecar")

    def test_an_in_run_key_reorder_of_a_held_row_is_refused(self):
        """The second half of the same guard: equal values in a different key order are still a
        rewrite of a row the nightly has no right to touch."""
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()

        def reorder_keys_of_a_held_row(recs):
            recs[0]["name"] = recs[0].pop("name")     # identical value, new key position

        code, out, _client = self.run_sync_with(records, prev, table, extra,
                                                mutate=reorder_keys_of_a_held_row)
        self.assertEqual(code, 1, out)
        self.assertIn("would mutate roster row", out)
        self.assertFalse(self.roster_file.exists(), "a rekeyed run wrote characters.json")

    def test_the_existing_rows_survive_the_run_byte_for_byte(self):
        records, prev, table, extra = self.one_known_member_and_one_visible_stranger()
        records = [row("1", "Alpha One"), row("2", "Bravo Two", track="alts",
                                              validation={"state": "validated",
                                                          "confirmedSlug": "bravo-two"})]
        expected = copy.deepcopy(records)       # run_sync appends to the list it is handed
        table = dict(table, **{"bravo-two": profile("Bravo", "Two")})
        code, out, _client = self.run_sync_with(records, prev, table, extra)
        self.assertEqual(code, 0, out)
        written = json.loads(self.roster_file.read_text(encoding="utf-8"))["characters"]
        self.assertEqual(len(written), 3, out)
        self.assertEqual(written[:2], expected)


class VerifyGateVocabulary(unittest.TestCase):
    """The shipped gate itself: widened for ``pending``, still closed against everything else."""

    @classmethod
    def setUpClass(cls):
        cls.gate = load_verify_gate("verify_sync_artifacts_vocabulary")

    def test_pending_and_validated_and_absent_are_stored_states(self):
        for state in ("validated", "pending", None):
            self.assertTrue(self.gate.validation_state_ok(state), state)

    def test_anything_else_still_fails_the_widened_gate(self):
        for state in ("draft", "awaiting_sync", "VALIDATED", "needs_review", "", "validated ",
                      True, 1, ["pending"]):
            self.assertFalse(self.gate.validation_state_ok(state), repr(state))

    def test_the_optional_guild_field_is_in_the_permitted_key_set(self):
        self.assertIn("guild", self.gate.ROSTER_KEYS)
        self.assertTrue({"id", "player", "name", "track", "role", "roleSec", "race", "class",
                         "prof1", "prof2", "validation"} <= self.gate.ROSTER_KEYS)

    def test_the_gate_reads_the_auto_add_module_as_part_of_the_shipped_sync_surface(self):
        self.assertIn("autoadd.py", self.gate.SYNC_MODULES)

    def test_the_gate_and_the_writer_agree_on_the_map_entry_key_set(self):
        """A divergent key set is a silent under-repair, so it has to crash instead.

        ``VerifyGateRosterRowRules`` re-derives the copied map with ``bnet.artifacts.map_entry`` and
        assigns only over keys the entry already carries, and the gate enforces its OWN literal
        ``MAP_KEYS``. Add a key on one side only and nothing fails: the fixture stops healing the new
        mirror (the gate reports it, the repair leaves it) and the outage class this file exists to
        prevent walks back in. Equality here is what turns that into a red test.
        """
        self.assertEqual(self.gate.MAP_KEYS, set(artifacts.MAP_ENTRY_KEYS),
                         "verify_sync_artifacts.MAP_KEYS and bnet.artifacts.MAP_ENTRY_KEYS diverged")

    def test_the_committed_roster_still_satisfies_the_widened_gate(self):
        document = json.loads(config.ROSTER_FILE.read_text(encoding="utf-8"))
        for rec in document["characters"]:
            self.assertTrue(set(rec) <= self.gate.ROSTER_KEYS, rec.get("id"))
            self.assertTrue(self.gate.validation_state_ok((rec.get("validation") or {})
                                                          .get("state")), rec.get("id"))


def sync_consistent_entry(rec, prev_entry):
    """What ``bnet`` itself would write in ``battle-net-map.json`` for one roster row, offline.

    The gate cross-checks mirrors between ``data/characters.json`` and ``data/battle-net-map.json``
    that only the nightly sync writes. A hand or site edit to the roster is legitimately ahead of
    the map until the next run, and a unit test that dies on that window reds the unit-test step of
    ``sync-bnet.yml`` — the step BEFORE the sync — so the outage cannot heal itself. The fixture
    therefore re-derives the map the way the sync would, by calling the shipped writers
    (``artifacts.map_entry`` / ``artifacts.ok_entry`` / ``resolve.reported_note_entry`` and the
    ``roster`` trust helpers) with the two inputs those writers have when no request is in flight:
    the roster row, and the committed entry standing in for ``prev_entries``. A mirror field the
    writers add later is re-derived here for free, which is the whole point of reusing them.

    WRITTEN from the row (what the writers derive from a roster row plus the previous entry):

    * ``rosterName`` — ``map_entry`` takes it off ``rec["name"]`` on every single write;
    * ``status`` / ``note`` where the row's own trust state forces them without a response:
      ``validated`` with neither a ``confirmedSlug`` nor a previously mapped slug is
      ``needs_review`` + the refusal note (``resolve.Resolver.resolve``), and an open reviewer
      note is ``needs_review`` + that note (``resolve.reported_note_entry``, applied last, exactly
      as ``apply_reported_notes`` runs last, so a report outranks the trust path);
    * ``mappedVia`` / ``note`` for a trusted row that is ``ok`` — ``"confirmed"`` /
      ``"user-confirmed"`` (``resolve.Resolver.resolve_trusted``);
    * ``slug`` for that same row — ``roster.trusted_slug(rec, prev)``, i.e. the ``confirmedSlug``,
      falling back to the slug already in the map (which makes it a no-op when nobody changed it);
    * ``matchedForm`` for that same row — ``prev.matchedForm or "confirmed_slug"``, verbatim out of
      ``resolve_trusted``.

    CARRIED FORWARD from the committed entry, because the writer takes it off a profile payload
    this fixture will never hold (inventing a value here is precisely what the writers refuse to
    do — ``ok_entry`` reads name and surname off the payload, "never the roster guess", task 2.3):

    * ``status`` whenever it is a verdict the resolver reaches by GETting something: the candidate
      ladder, the guild lever, a 404 on a confirmed slug, a transport error, the H4 floor. A row
      the last run could not resolve stays exactly as unresolved as it was — the repair neither
      promotes a ``not_found`` to ``ok`` nor demotes an ``ok``;
    * ``bnetName`` / ``surname`` — payload identity;
    * ``href`` — ``fetch.canonical_href(payload) or character_url(slug)``. The committed href is
      kept while it still names the slug the trust rule wants, and the writer's own fallback
      (``resolve.character_url``, used when a payload carries no self link) replaces it when it no
      longer does, so a repaired row never publishes an identity pointing somewhere else;
    * ``candidates`` — ``reported_note_entry`` carries them and ``ok_entry`` would write ``[]``;
      the committed list is kept, because there is no freshly resolved hit list to put in its
      place and dropping identity data the sync has not actually re-decided is a destructive edit
      no fixture asks for.

    Deliberately NOT re-run: ``resolve.Resolver.dedupe_slugs``. Two roster ids that resolved to one
    slug is a verdict about one run's whole work list, not a mirror of a single row, and applying it
    here would change the status of rows no fixture touched. The gate has its own independent
    name-normalization policy check for that case (``duplicate-normalization id … is not
    auto-mapped``).
    """
    prev = prev_entry if isinstance(prev_entry, dict) else {}
    if roster.is_trusted(rec) and roster.trusted_slug(rec, prev) is None:
        # A validated row with nothing trustworthy to fetch: the resolver refuses to guess, and
        # that refusal needs no network, so the writer's own call is reproduced exactly.
        healed = artifacts.map_entry(
            rec, "needs_review",
            note="validated without confirmedSlug and no previously mapped slug; "
                 "refusing to re-resolve")
    elif roster.is_trusted(rec) and prev.get("status") == "ok":
        slug = roster.trusted_slug(rec, prev)
        href = prev.get("href")
        if resolve.slug_from_href(href) != slug:
            href = resolve.character_url(slug)
        healed = artifacts.ok_entry(
            rec, slug=slug, href=href,
            payload={"name": prev.get("bnetName"), "surname": prev.get("surname")},
            matched_form=prev.get("matchedForm") or "confirmed_slug",
            mapped_via="confirmed", note="user-confirmed")
        healed["candidates"] = prev.get("candidates") or []
    else:
        # Everything the last run decided by asking Blizzard stands: re-derive the row's mirror
        # through the same writer (which is also the H2 href choke point) and hand back the
        # payload-derived fields untouched.
        healed = artifacts.map_entry(rec, prev.get("status"), slug=prev.get("slug"),
                                     href=prev.get("href"), bnet_name=prev.get("bnetName"),
                                     surname=prev.get("surname"),
                                     matched_form=prev.get("matchedForm"),
                                     mapped_via=prev.get("mappedVia"), note=prev.get("note"),
                                     candidates=prev.get("candidates"))
    # The §2 report pass is last in ``run_sync`` too, so an open note outranks the trust path.
    return resolve.reported_note_entry(rec, healed)


class VerifyGateRosterRowRules(unittest.TestCase):
    """Review C1, end to end: the shipped CI gate still enforces the two per-row validation rules.

    ``.pi/scripts/verify_sync_artifacts.py`` resolves ``ROOT`` from its own location, so the
    fixture is a copy of the repository under a temp dir with the mutation applied to that copy's
    ``data/`` documents, and the gate is then run exactly as ``pages.yml`` / ``sync-bnet.yml`` run
    it. The copy carries no ``.git``, which is the gate's own documented skip path for the
    additive-vs-HEAD guard (it prints a WARN and is never counted as a pass), so a fixture that
    appends a row does not trip a rule these tests are not about.

    THE INVARIANT THIS CLASS MAINTAINS: a fixture's gate run fails on that fixture's own mutation
    and on nothing else. Three of its tests hold that honest in both directions — each stales a
    mirror, and each has a twin that leaves it stale and asserts the gate fails on exactly its one
    line. Without the repair, any curated roster edit reds this class; without the stale-side
    assertions, the repair could quietly become "the gate no longer checks mirrors" and nobody would
    notice for four days.

    WHY THE REPAIR REUSES THE WRITER (``bring_tree_into_sync_consistency`` and
    ``sync_consistent_entry``, not a list of field names): the gate enforces cross-file mirrors
    that only the nightly sync writes, and committed data is stale by design in the window between
    a curated edit and the next run. That window reds the unit-test step of ``sync-bnet.yml`` — the
    step that runs BEFORE the sync — which starves the only run that could heal it. Two outages in
    24 hours were the same class of bug under different field names: ``a1c4a6b`` renamed a roster
    row while the map kept the old ``rosterName`` (the gate's ``map entry … rosterName matches
    characters.json``), and ``30cccac`` / ``a8469e5`` / ``d4bd366`` set ``validation.state =
    validated`` plus a ``confirmedSlug`` on three rows while their map entries kept the
    pre-validation ``mappedVia`` and ``note`` (the gate's ``trusted id … ok row is marked confirmed
    / user-confirmed``). The fix for the first instance repaired that one field by name, and the
    second instance walked straight past it. Re-deriving every entry with ``bnet.artifacts`` and
    ``bnet.resolve`` — the code the sync itself runs — means the next ROSTER-DERIVED mirror the
    writer grows is repaired here for free: anything ``map_entry`` / ``ok_entry`` computes from the
    roster row follows the writer automatically.

    WHAT THIS CANNOT FIX, so do not count on it: a verdict only a live request can produce
    (``status``, ``bnetName``, ``surname``, ``candidates`` are carried, never invented), the
    slug-collision pre-pass (``sync.run_sync`` decides across a whole run's work list, not per row),
    and map COVERAGE — an id in the roster with no entry at all. Those still red this class and
    still starve the run that would heal them, because the rule being violated needs information a
    fixture cannot have. The structural answer lives in ``sync-bnet.yml``, not here: a pre-sync step
    must not enforce rules whose subject is the cross-artifact state the sync has not produced yet.
    """

    REPO = Path(__file__).resolve().parents[2]
    # staticmethod(): a plain class attribute holding a function would bind to ``self`` and
    # hand copytree three positional arguments.
    IGNORE = staticmethod(shutil.ignore_patterns(
        ".git", ".env", ".zvec-grep", ".pytest_cache", ".mypy_cache", "__pycache__", "*.pyc",
        "node_modules", "subagents"))
    REPORTED_ID = "9991113335557"      # synthetic, 13 digits, cannot collide with a real row

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tree = self.tmp / "repo"
        shutil.copytree(self.REPO, self.tree, ignore=self.IGNORE)
        self.bring_tree_into_sync_consistency(self.tree)
        self.gate = self.tree / ".pi" / "scripts" / "verify_sync_artifacts.py"

    @staticmethod
    def bring_tree_into_sync_consistency(tree):
        """Bring the copied map's roster-derived mirrors in line with the copied roster.

        For every id present in BOTH documents, hand the roster row and the committed entry to
        ``sync_consistent_entry`` — the shipped writers' own verdict for those two inputs — and keep
        whatever it says. See it for the written-vs-carried field list, and this class' docstring
        for why this is the writer's logic rather than a list of field names.

        Bounded on purpose, because a fixture's baseline must be boring:

        * total over ids in both documents, and it never invents an entry for a roster row the map
          does not list nor prunes an entry the roster no longer has — the coverage rule
          (``set(map ids) == set(roster ids)``) is a gate rule a fixture may be testing;
        * it never adds or removes a key on an entry and never reorders one: a value is only
          assigned over a key that is already there, so an entry that has lost a key stays as
          visibly broken as the gate's own ``map entry … has exactly the v2 key set`` check reports
          it (the same reasoning keeps ``schemaVersion``, ``updatedAt`` and
          ``data/character-data.json`` — including its id coverage and its status mirror — alone);
        * an entry that already agrees with the writers is not rewritten, and a tree that is
          consistent end to end leaves the copied file byte-for-byte untouched;
        * no network. Everything it needs is in the two documents; the fields that are not are
          carried forward rather than guessed.
        """
        records = roster.roster_records(json.loads(
            (tree / "data" / "characters.json").read_text(encoding="utf-8")))
        by_id = {rec["id"]: rec for rec in records if isinstance(rec, dict)}
        path = tree / "data" / "battle-net-map.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = doc.get("entries")
        if not isinstance(entries, dict):
            return doc
        changed = False
        for rid, entry in entries.items():
            rec = by_id.get(rid)
            if rec is None or not isinstance(entry, dict):
                continue
            for key, value in sync_consistent_entry(rec, entry).items():
                if key in entry and entry[key] != value:
                    entry[key] = value
                    changed = True
        if changed:
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return doc

    def inconsistent_entries(self):
        """Every copied map entry the writers would still change, as ``{id: {field: [have, want]}}``.

        The fixture's consistency test: ``sync_consistent_entry`` is a function of the roster row
        and the entry itself, so an entry equal to its own re-derivation is a fixed point — the map
        says everything the roster currently entitles it to say."""
        records = roster.roster_records(json.loads(
            (self.tree / "data" / "characters.json").read_text(encoding="utf-8")))
        by_id = {rec["id"]: rec for rec in records if isinstance(rec, dict)}
        entries = json.loads((self.tree / "data" / "battle-net-map.json")
                             .read_text(encoding="utf-8"))["entries"]
        out = {}
        for rid, entry in entries.items():
            rec = by_id.get(rid)
            if rec is None or not isinstance(entry, dict):
                continue
            for key, value in sync_consistent_entry(rec, entry).items():
                if key in entry and entry[key] != value:
                    out.setdefault(rid, {})[key] = [entry[key], value]
        return out

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_gate(self):
        proc = subprocess.run([sys.executable, str(self.gate)], capture_output=True, text=True)
        return proc, proc.stdout + proc.stderr

    def edit_json(self, name, mutate):
        path = self.tree / "data" / name
        doc = json.loads(path.read_text(encoding="utf-8"))
        mutate(doc)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return doc

    def test_a_row_with_guild_and_no_validation_block_clears_the_gate(self):
        """The shape that aborted the whole script: ``guild`` present, ``validation`` absent.

        ``1791928548067`` is committed with no validation block at all, so this is the everyday
        curation edit a maintainer makes when they file a row under a guild — and the CI step used
        to die on it with ``AttributeError: 'NoneType' object has no attribute 'get'``."""
        target = "1791928548067"

        def add_guild(doc):
            rec = next(r for r in doc["characters"] if r["id"] == target)
            self.assertNotIn("validation", rec, "the fixture row needs to stay validation-free")
            rec["guild"] = GUILD_NAME

        self.edit_json("characters.json", add_guild)
        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output, "the gate crashed on a schema-legal roster row")
        self.assertIn(f"PASS roster {target} guild is absent, null or a string (found str)", output)
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertIn("0 failed", output)

    def test_a_reported_row_without_a_guild_key_still_trips_the_section_2_map_rule(self):
        """A reviewer's open note buys a row immunity from an ``ok`` map identity — guild key or not.

        A pure append (roster row + map entry + sidecar row, nothing existing touched), so the
        coverage and additive rules stay satisfied and the only thing left to catch the fixture is
        the design §2 mirror. With the note read back under ``if v is not None:`` it is caught."""
        def append_a_reported_row(doc):
            row = {"id": self.REPORTED_ID, "player": "Reviewer", "name": "Reported Person",
                   "track": "alts", "role": "DPS", "roleSec": "None", "race": "Tauren",
                   "class": "Druid", "prof1": "None", "prof2": "None",
                   "validation": {"note": "mapped to the wrong character",
                                  "reportedBy": "combatwombat16",
                                  "reportedAt": "2026-01-01T00:00:00Z"}}
            # The whole finding turns on this key being absent: the reported row is an ordinary
            # hand-added row, not one the nightly appended under a guild.
            self.assertNotIn("guild", row)
            doc["characters"].append(row)

        self.edit_json("characters.json", append_a_reported_row)

        def publish_it_as_ok(doc):
            doc["entries"][self.REPORTED_ID] = {
                "rosterName": "Reported Person", "status": "ok", "slug": "reported-person",
                "href": f"{API}/profile/wow/character/reported-person?namespace=profile",
                "bnetName": "Reported", "surname": "Person", "matchedForm": "name+surname",
                "mappedVia": "guild_roster", "note": "", "candidates": [],
            }

        self.edit_json("battle-net-map.json", publish_it_as_ok)
        self.edit_json("character-data.json",
                       lambda doc: doc["characters"].__setitem__(
                           self.REPORTED_ID, {"status": "not_found",
                                              "fetchedAt": http.iso_now()}))
        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 1,
                         "a map that publishes an ok identity for a reported row must not pass")
        self.assertIn(f"FAIL reported id {self.REPORTED_ID} is needs_review in the map, not ok "
                      "(the note rule of §2)", output)
        # Exactly one failure: the append itself is clean, so the §2 mirror is the only thing the
        # fixture violates — a second failure would mean the fixture, not the gate, was the test.
        self.assertEqual([l for l in output.splitlines() if l.startswith("  FAIL ")],
                         [f"  FAIL reported id {self.REPORTED_ID} is needs_review in the map, "
                          "not ok (the note rule of §2)"], output[-2000:])

    def test_a_stale_roster_name_mirror_never_reaches_the_gate(self):
        """The repair itself, guarded: a curated rename must not turn these tests red.

        Stale the mirror for a real id in the copy, run the repair the way ``setUp`` runs
        it, and the gate has to pass with the mirror back in sync — otherwise a routine
        ``characters.json`` edit is free to break the unit-test step again and block the sync.
        """
        target = sorted(json.loads((self.tree / "data" / "battle-net-map.json")
                                   .read_text(encoding="utf-8"))["entries"])[0]
        names = {rec["id"]: rec["name"] for rec in json.loads(
            (self.tree / "data" / "characters.json").read_text(encoding="utf-8"))["characters"]}
        self.assertIn(target, names, "the fixture id has to be a row the gate cross-checks")
        # A fresh copy has to arrive consistent with everything the writers derive for it, not
        # merely with the one mirror field the first fix knew about. Be honest about the strength:
        # this bites when the committed tree is stale (the window this fix exists for) and passes
        # silently when it is already consistent, so it is a floor on the copy, not a proof that
        # setUp calls the repair. The stale-side twins are what prove the repair runs at all.
        self.assertEqual(self.inconsistent_entries(), {},
                         "setUp copied the tree without bringing it into sync consistency first")
        # The injection is the test: a mirror written with the row's own name would heal nothing.
        self.assertNotEqual("Stale Mirror", names[target],
                            "the injected mirror has to disagree with the roster row")

        self.edit_json("battle-net-map.json",
                       lambda doc: doc["entries"][target].__setitem__("rosterName", "Stale Mirror"))
        self.assertEqual(sorted(self.inconsistent_entries()), [target],
                         "the injected staleness is not visible to the repair")
        self.bring_tree_into_sync_consistency(self.tree)
        healed = json.loads((self.tree / "data" / "battle-net-map.json")
                            .read_text(encoding="utf-8"))["entries"][target]
        self.assertEqual(healed["rosterName"], names[target], "the repair did not repair the mirror")

        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 0, output[-2000:])
        # The cross-check on the injected id ran and passed rather than being skipped, and the
        # mirror rule appears nowhere in the failure listing (neither the inline ``FAIL`` line nor
        # the ``FAILED CHECKS:`` echo the nightly log prints at the end).
        self.assertIn(f"PASS map entry {target} rosterName matches characters.json", output)
        self.assertEqual([l for l in output.splitlines()
                          if l.startswith(("  FAIL ", "  - "))
                          and "rosterName matches characters.json" in l], [],
                         "the mirror rule was reported as a failure for a fixture that healed it")
        self.assertEqual([l for l in output.splitlines() if l.startswith("  FAIL ")], [],
                         "a stale mirror leaked into a fixture gate run")

    def test_a_mirrored_roster_name_the_sync_never_wrote_still_fails_the_gate(self):
        """The tripwire itself, pinned on purpose rather than by accident.

        While the committed mirror was stale this rule was "pinned" only by breaking every other
        fixture in the class. The repair takes the baseline back, so assert directly: a map whose
        ``rosterName`` disagrees with the roster row of the same id must exit non-zero on that one
        line and nothing else. A future relaxation of the mirror rule has to pass through here.

        It is also ``setUp``'s own control: the repair runs there and this test still goes red, so
        the repair is a repair and not the mirror rule going soft.
        """
        target = sorted(json.loads((self.tree / "data" / "battle-net-map.json")
                                   .read_text(encoding="utf-8"))["entries"])[0]
        self.edit_json("battle-net-map.json",
                       lambda doc: doc["entries"][target].__setitem__("rosterName", "Stale Mirror"))
        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 1,
                         "a map mirror that stopped matching the roster must not pass the gate")
        self.assertEqual([l for l in output.splitlines() if l.startswith("  FAIL ")],
                         [f"  FAIL map entry {target} rosterName matches characters.json"],
                         output[-2000:])

    def test_a_validation_commit_the_sync_has_not_replayed_is_the_second_mirror_outage(self):
        """``30cccac`` / ``a8469e5`` / ``d4bd366``, pinned as a rule rather than as an outage.

        The site wrote ``{state: "validated", confirmedSlug: <its own slug>}`` on three rows in
        commits that never touched the map, and the gate's design §3 mirror (``trusted id … ok row
        is marked confirmed / user-confirmed``) then failed the unit-test step of the very workflow
        whose next run was the only thing able to rewrite those two fields. The first fix of that
        class repaired ``rosterName`` by name, so this walked straight past it.

        Pinned in both directions, like the ``rosterName`` pair: the same copy, the same mutation,
        with the repair and without it. ``resolve_trusted`` is the only writer of
        ``mappedVia: "confirmed"`` / ``note: "user-confirmed"``, so this asserts against it, not
        against a hand-copied pair of literals.
        """
        target = "1794319268038"        # an ok row the nightly mapped through the guild lever
        committed = json.loads((self.tree / "data" / "battle-net-map.json")
                               .read_text(encoding="utf-8"))["entries"]
        entry, row_before = committed[target], doc_row(
            json.loads((self.tree / "data" / "characters.json").read_text(encoding="utf-8")), target)
        own_slug = entry["slug"]
        # The fixture's own preconditions: a settled identity, a via the reviewer has not signed
        # off yet, and a row the trust rule does not yet cover.
        self.assertEqual(entry["status"], "ok", entry)
        self.assertNotEqual(entry["mappedVia"], "confirmed", entry)
        self.assertNotEqual(entry["note"], "user-confirmed", entry)
        self.assertNotEqual((row_before.get("validation") or {}).get("state"), "validated",
                            row_before)

        sidecar_before = (self.tree / "data" / "character-data.json").read_bytes()
        untouched_before = {rid: value for rid, value in committed.items() if rid != target}

        def validate_the_row(doc):
            rec = doc_row(doc, target)
            rec["validation"] = {"state": "validated", "confirmedSlug": own_slug}

        self.edit_json("characters.json", validate_the_row)

        # (a) No repair, no sync, no healing: the gate has to fail on exactly the §3 confirmation
        # line. The mutation only touches the roster row, so anything else failing here is the
        # fixture's fault rather than the gate's.
        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 1,
                         "a validated row whose map entry still predates the validation "
                         "must not pass")
        self.assertEqual([l for l in output.splitlines() if l.startswith("  FAIL ")],
                         [f"  FAIL trusted id {target} ok row is marked confirmed / user-confirmed"],
                         output[-2000:])
        self.assertEqual(sorted(self.inconsistent_entries()), [target],
                         "the repair does not even see this mirror outage")

        # (b) With the repair: the writer's own verdict, and nothing else in either artifact moved.
        self.bring_tree_into_sync_consistency(self.tree)
        healed = json.loads((self.tree / "data" / "battle-net-map.json")
                            .read_text(encoding="utf-8"))["entries"]
        self.assertEqual(healed[target]["mappedVia"], "confirmed", healed[target])
        self.assertEqual(healed[target]["note"], "user-confirmed", healed[target])
        self.assertEqual(healed[target]["matchedForm"], entry["matchedForm"],
                         "resolve_trusted carries the previous matchedForm")
        for key in ("status", "slug", "href", "bnetName", "surname", "candidates"):
            self.assertEqual(healed[target][key], entry[key],
                             f"the repair moved {key}, which only a profile payload may decide")
        self.assertEqual(healed[target]["slug"], own_slug, "a confirmedSlug is authoritative")
        self.assertEqual({rid: value for rid, value in healed.items() if rid != target},
                         untouched_before, "the repair touched an entry nobody asked it about")
        self.assertEqual((self.tree / "data" / "character-data.json").read_bytes(), sidecar_before,
                         "the repair reached outside battle-net-map.json")
        self.assertEqual(self.inconsistent_entries(), {},
                         "after the repair no entry is left for the writers to change")

        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertIn(f"PASS trusted id {target} ok row is marked confirmed / user-confirmed",
                      output)
        self.assertEqual([l for l in output.splitlines() if l.startswith("  FAIL ")], [],
                         output[-2000:])

    def test_the_240_character_note_ceiling_still_applies_to_a_guild_less_row(self):
        """The other rule that went dead: ``validation.note`` is capped for EVERY row, not just
        the ones the nightly happens to have appended with a guild name."""
        target = "1791928493125"        # a committed, guild-less, validated row
        ceiling = load_verify_gate("verify_sync_artifacts_note_ceiling").REPORT_NOTE_MAX

        def overlong_note(doc):
            rec = doc_row(doc, target)
            self.assertNotIn("guild", rec)
            rec["validation"]["note"] = "x" * (ceiling + 1)

        self.edit_json("characters.json", overlong_note)
        proc, output = self.run_gate()
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.returncode, 1, output[-2000:])
        self.assertIn(f"FAIL roster {target} validation.note is a non-empty string of at most "
                      f"{ceiling} characters", output)


class AutoAddTrustBoundary(unittest.TestCase):
    """A pending row is a placeholder, not a signature: it must stay resolvable."""

    def test_pending_is_not_trusted_while_validated_is(self):
        pending = {"id": "1", "name": "Newbie Newface", "validation": {"state": "pending"}}
        self.assertFalse(roster.is_trusted(pending))
        self.assertIsNone(roster.confirmed_slug(pending))
        self.assertIsNone(roster.reported_note(pending))
        self.assertTrue(roster.is_trusted({"id": "2", "name": "x",
                                           "validation": {"state": "validated"}}))



class GuildScopeLimitsTheAutoAdd(unittest.TestCase):
    """Owner request (2026-11-10): this roster keeps ONE guild.

    The scope lives in ``data/guild-config.json`` and the nightly reads it in exactly one place —
    ``Sync.discover_guilds`` — which is the only reason the auto-add ever has a guild to add from.
    Filter there and the excluded guild is never fetched and never planned; filter later and the
    requests are still spent. These tests pin the filter, the name/slug matching it does, the
    "no config means the old behaviour" default, and the summary line that keeps "0 rows added
    because of the scope" distinguishable from "0 rows added because the guild is complete".
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "guild-config.json"

    def write_config(self, text):
        self.cfg.write_text(text, encoding="utf-8")
        return self.cfg

    # ---------- config.included_guild ---------- #

    def test_the_committed_config_names_the_guild_this_roster_keeps(self):
        self.assertEqual(config.included_guild(), "Flowmasters")

    def test_value_is_trimmed_and_a_missing_or_blank_file_is_no_scope(self):
        self.assertEqual(config.included_guild(self.write_config(
            '{"schemaVersion": 1, "includedGuild": "  Twelve Guards  "}')), "Twelve Guards")
        self.assertIsNone(config.included_guild(self.tmp / "absent.json"))
        self.assertIsNone(config.included_guild(self.write_config('{"includedGuild": "   "}')))
        self.assertIsNone(config.included_guild(self.write_config('{"includedGuild": null}')))
        self.assertIsNone(config.included_guild(self.write_config('{"includedGuild": 7}')))
        self.assertIsNone(config.included_guild(self.write_config('not json at all')))
        self.assertIsNone(config.included_guild(self.write_config('["Flowmasters"]')))

    # ---------- sync.same_guild ---------- #

    def test_a_display_name_matches_its_own_slug_form(self):
        self.assertTrue(sync.same_guild("Flowmasters", "flowmasters"))
        self.assertTrue(sync.same_guild("Twelve Guards", "twelve-guards"))
        self.assertTrue(sync.same_guild("  FLOWMASTERS  " "", "flowmasters"))
        self.assertFalse(sync.same_guild("Flowmasters", "Heghmoh"))
        # An empty side is never a match, or a blank config would "match" every guild.
        self.assertFalse(sync.same_guild("", "flowmasters"))
        self.assertFalse(sync.same_guild(None, None))
        self.assertFalse(sync.same_guild("---", "___"))

    # ---------- Sync.discover_guilds ---------- #

    def discover(self, profiles, *, config_text=None):
        s = sync.Sync.__new__(sync.Sync)
        s.guild_scope = None
        s.guild_scope_excluded = set()
        if config_text is not None:
            self.write_config(config_text)
        work = {str(i): {"profile": p} for i, p in enumerate(profiles)}
        return s, s.discover_guilds(work, guild_config_path=self.cfg)

    def test_an_unscoped_run_discovers_every_guild_it_saw(self):
        _s, found = self.discover([profile("Alpha", "One", "flowmasters", "Flowmasters"),
                                   profile("Beta", "Two", "heghmoh", "Heghmoh")])
        self.assertEqual(found, {"flowmasters": "Flowmasters", "heghmoh": "Heghmoh"})

    def test_only_the_included_guild_survives_the_scope(self):
        s, found = self.discover(
            [profile("Alpha", "One", "flowmasters", "Flowmasters"),
             profile("Beta", "Two", "heghmoh", "Heghmoh"),
             profile("Gamma", "Three")],
            config_text='{"schemaVersion": 1, "includedGuild": "Flowmasters"}')
        self.assertEqual(found, {"flowmasters": "Flowmasters"})
        self.assertEqual(s.guild_scope, "Flowmasters")
        self.assertEqual(s.guild_scope_excluded, {"Heghmoh"})

    def test_the_scope_matches_on_slug_when_the_profile_omitted_the_name(self):
        _s, found = self.discover(
            [profile("Alpha", "One", "flowmasters", None),
             profile("Beta", "Two", "heghmoh", "Heghmoh")],
            config_text='{"schemaVersion": 1, "includedGuild": "Flowmasters"}')
        # profile() defaults the display name to the slug, so the slug form is what matched here.
        self.assertEqual(found, {"flowmasters": "flowmasters"})

    def test_a_scope_that_matches_nothing_discovers_nothing(self):
        # Loud by construction: the run then has no guild roster to read, so it adds nobody and the
        # summary names the guilds it passed over instead of implying the roster was complete.
        s, found = self.discover(
            [profile("Alpha", "One", "flowmasters", "Flowmasters")],
            config_text='{"schemaVersion": 1, "includedGuild": "Some Other Guild"}')
        self.assertEqual(found, {})
        self.assertEqual(s.guild_scope_excluded, {"Flowmasters"})

    def test_the_summary_states_the_scope_and_what_it_skipped(self):
        s = sync.Sync.__new__(sync.Sync)
        s.args = argparse.Namespace(dry_run=True, lookup=None, force=False,
                                    state_dir=self.tmp / "state")
        s.cache = http.Cache(self.tmp / "state", dry_run=True)
        s.client = FakeClient({})
        s.f = fetch.Fetcher(s.client, s.cache)
        s.budget_hit = False
        s.guild_scope = "Flowmasters"
        s.guild_scope_excluded = {"Heghmoh"}
        lines = s.summary_lines({"ok": 1}, 1, 0.0)
        scope_lines = [ln for ln in lines if "guild scope" in ln]
        self.assertEqual(len(scope_lines), 1)
        self.assertIn("Flowmasters", scope_lines[0])
        self.assertIn("skipped: Heghmoh", scope_lines[0])
        # And an unscoped run says nothing about a scope it does not have.
        s.guild_scope = None
        self.assertFalse([ln for ln in s.summary_lines({"ok": 1}, 1, 0.0) if "guild scope" in ln])



class GuildNameRuleIsTheSharedTable(unittest.TestCase):
    """One table, three readers: bnet.sync.same_guild, js/state.js guildNameEquals, and the copy the
    artifact gate compares against.

    The table is ``tools/tests/fixtures/guild-name-cases.json``. It exists because the nightly and
    the browser decide the same question — does this name select the included guild? — in two
    languages, and a scope they answer differently is invisible: the run appends members the site
    then never lists, so the roster grows rows nobody can see and no log says so. These cases are
    the rule; the implementations follow it, not the other way round.
    """

    CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "guild-name-cases.json"

    def cases(self):
        doc = json.loads(self.CASES_PATH.read_text(encoding="utf-8"))
        cases = doc.get("cases")
        self.assertIsInstance(cases, list, "the table has a cases list")
        self.assertGreaterEqual(len(cases), 8, "the table is not empty")
        self.assertTrue(any(case["equal"] for case in cases)
                        and any(not case["equal"] for case in cases),
                        "the table pins both answers, so a rule that always returns one still fails")
        return cases

    def test_same_guild_answers_the_table(self):
        for case in self.cases():
            with self.subTest(pair=[case["a"], case["b"]]):
                self.assertEqual(sync.same_guild(case["a"], case["b"]), case["equal"],
                                 case.get("why", ""))

    def test_the_rule_is_symmetric_and_idempotent(self):
        # An equivalence relation, because the chip row dedupes on it: an asymmetric compare would
        # make "which spelling labels the chip" depend on roster order, and a non-idempotent one
        # would make the chip row shrink or grow between repaints of the same data.
        for case in self.cases():
            with self.subTest(pair=[case["b"], case["a"]]):
                self.assertEqual(sync.same_guild(case["b"], case["a"]), case["equal"],
                                 "reversed arguments agree")
            with self.subTest(pair=[case["a"], case["a"]]):
                self.assertEqual(sync.same_guild(case["a"], case["a"]),
                                 bool(str(case["a"]).strip().strip("-?_")),
                                 "a name equals itself unless it squashes to nothing")


if __name__ == "__main__":
    unittest.main()
