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

    def test_the_committed_roster_still_satisfies_the_widened_gate(self):
        document = json.loads(config.ROSTER_FILE.read_text(encoding="utf-8"))
        for rec in document["characters"]:
            self.assertTrue(set(rec) <= self.gate.ROSTER_KEYS, rec.get("id"))
            self.assertTrue(self.gate.validation_state_ok((rec.get("validation") or {})
                                                          .get("state")), rec.get("id"))


class VerifyGateRosterRowRules(unittest.TestCase):
    """Review C1, end to end: the shipped CI gate still enforces the two per-row validation rules.

    ``.pi/scripts/verify_sync_artifacts.py`` resolves ``ROOT`` from its own location, so the
    fixture is a copy of the repository under a temp dir with the mutation applied to that copy's
    ``data/`` documents, and the gate is then run exactly as ``pages.yml`` / ``sync-bnet.yml`` run
    it. The copy carries no ``.git``, which is the gate's own documented skip path for the
    additive-vs-HEAD guard (it prints a WARN and is never counted as a pass), so a fixture that
    appends a row does not trip a rule these tests are not about.
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
        self.gate = self.tree / ".pi" / "scripts" / "verify_sync_artifacts.py"

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
