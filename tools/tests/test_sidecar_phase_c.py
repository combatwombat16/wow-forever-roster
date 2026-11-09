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

Stdlib unittest only: no network, no credentials, every write redirected to a temp dir.

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import argparse
import copy
import importlib.util
import io
import json
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

    def run_sync_with(self, records, prev_entries, table, extra=None, dry_run=False):
        args = argparse.Namespace(dry_run=dry_run, lookup=None, force=False,
                                  state_dir=self.tmp / "state")
        s = sync.Sync(args)
        cache = http.Cache(self.tmp / "state", dry_run=dry_run)
        cache.load()
        client = FakeClient(table, extra)
        f = fetch.Fetcher(client, cache)
        s.cache, s.client, s.f, s.resolver = cache, client, f, resolve.Resolver(f)
        f.enrich = lambda w: {"status": "ok", "fetchedAt": http.iso_now(), "level": 20}
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
        path = (Path(__file__).resolve().parents[2] / ".pi" / "scripts"
                / "verify_sync_artifacts.py")
        spec = importlib.util.spec_from_file_location("verify_sync_artifacts", path)
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)

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


class AutoAddTrustBoundary(unittest.TestCase):
    """A pending row is a placeholder, not a signature: it must stay resolvable."""

    def test_pending_is_not_trusted_while_validated_is(self):
        pending = {"id": "1", "name": "Newbie Newface", "validation": {"state": "pending"}}
        self.assertFalse(roster.is_trusted(pending))
        self.assertIsNone(roster.confirmed_slug(pending))
        self.assertIsNone(roster.reported_note(pending))
        self.assertTrue(roster.is_trusted({"id": "2", "name": "x",
                                           "validation": {"state": "validated"}}))



if __name__ == "__main__":
    unittest.main()
