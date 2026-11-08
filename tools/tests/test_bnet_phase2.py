#!/usr/bin/env python3
"""Offline unit tests for the Phase 2 lifecycle and security invariants.

Stdlib ``unittest`` only (no pytest dependency in this repo, and the CI gate must
not need a package install). Run from ``tools/``::

    python3 -m unittest discover -s tests
    python3 tools/tests/test_bnet_phase2.py          # from the repo root

Every test uses a fake client: nothing here touches the network, the credentials
or the committed artifacts.
"""

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/

from bnet import artifacts, config, fetch, http, resolve, roster, sync  # noqa: E402


class FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload
        self.headers = {}

    @property
    def json(self):
        return self._payload


def profile(name, surname, slug):
    return {"name": name, "surname": surname, "level": 20,
            "_links": {"self": {"href": f"https://us.api.blizzard.com/profile/wow/character/"
                                        f"{slug}?namespace=profile-forever-us"}}}


class FakeClient:
    """Answers profile GETs from a {slug: payload} table and records every URL."""

    def __init__(self, table, extra=None):
        self.table = table
        self.extra = extra or {}
        self.urls = []
        self.tokens_minted = 1
        self.requests_made = 0
        self.revalidations = 0
        self.status_counts = {}

    def get(self, url):
        from bnet import http as _http
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


def make_sync():
    cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
    client = FakeClient({})
    f = fetch.Fetcher(client, cache)
    return f, resolve.Resolver(f)


def work_for(rec):
    return {"rec": rec, "entry": None, "profile": None, "profile_url": None, "unchanged": False}


class RosterShape(unittest.TestCase):
    def test_v1_array_and_v2_object_read_identically(self):
        v1 = [{"id": "1", "name": "Beef Pho"}]
        v2 = {"schemaVersion": 2, "characters": v1}
        self.assertEqual(roster.roster_records(v1), roster.roster_records(v2))
        with self.assertRaises(ValueError):
            roster.roster_records({"schemaVersion": 3})

    def test_load_roster_reads_the_migrated_file(self):
        records = roster.load_roster(config.ROSTER_FILE)
        self.assertEqual(len(records), 20)
        self.assertTrue(all(r.get("id") for r in records))

    def test_trust_helpers(self):
        self.assertFalse(roster.is_trusted({"id": "1", "name": "x"}))
        confirmed = {"id": "1", "name": "x", "validation": {"confirmedSlug": "beef-pho"}}
        self.assertTrue(roster.is_trusted(confirmed))
        self.assertEqual(roster.trusted_slug(confirmed), "beef-pho")
        validated = {"id": "2", "name": "y", "validation": {"state": "validated"}}
        self.assertTrue(roster.is_trusted(validated))
        self.assertEqual(roster.trusted_slug(validated, {"status": "ok", "slug": "y-slug"}), "y-slug")
        self.assertIsNone(roster.trusted_slug(validated, {"status": "not_found"}))


class NoAutoPick(unittest.TestCase):
    def test_single_hit_is_ok(self):
        f, r = make_sync()
        slug = "beef-pho"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        w = work_for({"id": "1", "name": "Beef Pho"})
        r.resolve({"id": "1", "name": "Beef Pho"}, w, None)
        self.assertEqual(w["entry"]["status"], "ok")
        self.assertEqual(w["entry"]["slug"], slug)
        self.assertEqual(w["entry"]["candidates"], [])
        self.assertEqual(w["entry"]["mappedVia"], "direct")

    def test_two_forms_one_profile_is_still_ok(self):
        f, r = make_sync()
        slug = "huntress"
        f.client.table = {slug: profile("Huntress", None, slug)}
        w = work_for({"id": "1", "name": "Huntress"})
        r.resolve({"id": "1", "name": "Huntress"}, w, None)
        self.assertEqual(w["entry"]["status"], "ok")
        self.assertEqual(w["entry"]["slug"], slug)

    def test_two_distinct_profiles_are_ambiguous_and_nothing_is_picked(self):
        f, r = make_sync()
        f.client.table = {"beef-pho": profile("Beef", "Pho", "beef-pho"),
                          "beefpho": profile("Beefpho", "Alt", "beefpho")}
        rec = {"id": "1", "name": "Beef Pho"}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "ambiguous")
        self.assertIsNone(w["entry"]["slug"])
        self.assertIsNone(w["entry"]["href"])
        self.assertEqual({c["slug"] for c in w["entry"]["candidates"]}, {"beef-pho", "beefpho"})
        self.assertTrue(all(set(c) <= set(config_artifact_keys()) for c in w["entry"]["candidates"]))
        # the whole ladder was walked: never stop at the first 200
        self.assertEqual(len(f.client.urls), len(resolve.lookup_candidates(rec["name"])))

    def test_guild_lever_multi_member_is_ambiguous(self):
        f, r = make_sync()
        members = [{"character": {"name": "Tir", "surname": "Rheebeard", "slug": "tir-rheebeard",
                                  "playable_class": {"id": 1}}},
                   {"character": {"name": "Tir", "surname": "Rheebeard", "slug": "tir-rheebeard-two",
                                  "playable_class": {"id": 2}}}]
        f.guild_members = lambda slug: members          # one guild roster, two same-name members
        seed = work_for({"id": "0", "name": "Seed"})
        seed["profile"] = {"name": "Seed", "guild": {"slug": "flowmasters"}}
        seed["entry"] = artifacts.ok_entry(seed["rec"], slug="seed",
                                           href="https://us.api.blizzard.com/profile/wow/character/seed?namespace=profile-forever-us",
                                           payload=seed["profile"], matched_form="lower", mapped_via="direct")
        w = work_for({"id": "1", "name": "Tir Rheebeard"})
        work = {"0": seed, "1": w}
        r.guild_lever(work, ["0", "1"])
        self.assertEqual(w["entry"]["status"], "ambiguous")
        self.assertIsNone(w["entry"]["slug"])
        self.assertEqual({c["slug"] for c in w["entry"]["candidates"]},
                         {"tir-rheebeard", "tir-rheebeard-two"})
        # no profile GET was spent choosing between them
        self.assertEqual(f.client.urls, [])

    def test_guild_lever_single_member_still_maps(self):
        f, r = make_sync()
        slug = "tir-rheebeard"
        f.client.table = {slug: profile("Tir", "Rheebeard", slug)}
        f.guild_members = lambda s: [{"character": {"name": "Tir", "surname": "Rheebeard", "slug": slug}}]
        seed = work_for({"id": "0", "name": "Seed"})
        seed["profile"] = {"name": "Seed", "guild": {"slug": "flowmasters"}}
        seed["entry"] = artifacts.map_entry(seed["rec"], "ok")
        w = work_for({"id": "1", "name": "Tir Rheebeard"})
        r.guild_lever({"0": seed, "1": w}, ["0", "1"])
        self.assertEqual(w["entry"]["status"], "ok")
        self.assertEqual(w["entry"]["slug"], slug)
        self.assertEqual(w["entry"]["mappedVia"], "guild_roster")


class TrustedSlugs(unittest.TestCase):
    def test_confirmed_slug_is_fetched_directly_and_name_is_never_resolved(self):
        f, r = make_sync()
        slug = "some-other-slug"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        rec = {"id": "1", "name": "Beef Pho", "validation": {"confirmedSlug": slug, "state": "validated"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "ok")
        self.assertEqual(w["entry"]["slug"], slug)
        self.assertEqual(w["entry"]["note"], "user-confirmed")
        self.assertEqual(w["entry"]["mappedVia"], "confirmed")
        # only the confirmed slug was requested: no spelling of the roster name
        self.assertEqual(len(f.client.urls), 1)
        self.assertIn(slug, f.client.urls[0])
        self.assertNotIn("beef-pho", "".join(f.client.urls))

    def test_validated_row_keeps_the_mapped_slug(self):
        f, r = make_sync()
        slug = "beef-pho"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        rec = {"id": "1", "name": "Beef Pho", "validation": {"state": "validated"}}
        w = work_for(rec)
        r.resolve(rec, w, {"status": "ok", "slug": slug})
        self.assertEqual(w["entry"]["slug"], slug)
        self.assertEqual(w["entry"]["note"], "user-confirmed")

    def test_validated_without_any_slug_refuses_to_guess(self):
        f, r = make_sync()
        rec = {"id": "1", "name": "Beef Pho", "validation": {"state": "validated"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "needs_review")
        self.assertEqual(f.client.urls, [])

    def test_404_on_a_confirmed_slug_is_reported_not_repaired(self):
        f, r = make_sync()
        rec = {"id": "1", "name": "Beef Pho", "validation": {"confirmedSlug": "gone-slug"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "not_found")
        self.assertEqual(w["entry"]["slug"], "gone-slug")
        self.assertEqual(len(f.client.urls), 1)


class ReportedMismatchNotes(unittest.TestCase):
    """Design §2: a reviewer's ``validation.note`` keeps a row needs_review every run.

    The report is written by the detail modal as ``{note, reportedBy, reportedAt}`` with no
    ``state`` key, so the roster row stays a draft; the map entry is what carries the objection
    into the artifacts, and it has to survive every nightly run until someone validates the row
    (or re-opens it, which deletes the block). Nothing here needs the network: the fake client
    answers the profile GETs the resolution would have made anyway.
    """

    def test_a_note_overrides_an_ok_resolution_and_keeps_the_slug(self):
        f, r = make_sync()
        slug = "beef-pho"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        rec = {"id": "1", "name": "Beef Pho",
               "validation": {"note": "that is the other server's Beefpho", "reportedBy": "octocat"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "ok")          # the sync itself still resolves it
        self.assertEqual(r.apply_reported_notes({"1": w}, ["1"]), ["1"])
        self.assertEqual(w["entry"]["status"], "needs_review")
        self.assertEqual(w["entry"]["note"], "that is the other server's Beefpho")   # passed through
        self.assertEqual(w["entry"]["slug"], slug)             # identity kept, not blanked
        self.assertEqual(w["entry"]["mappedVia"], "direct")
        self.assertEqual(set(w["entry"]), set(artifacts.MAP_ENTRY_KEYS))
        self.assertTrue(any("reviewer note" in line for line in f.warnings))

    def test_a_report_on_a_user_confirmed_row_still_forces_review(self):
        f, r = make_sync()
        slug = "confirmed-then-disputed"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        rec = {"id": "1", "name": "Beef Pho",
               "validation": {"confirmedSlug": slug, "note": "renamed since I confirmed it"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "ok")           # trusted fetch by identity
        r.apply_reported_notes({"1": w}, ["1"])
        self.assertEqual(w["entry"]["status"], "needs_review")
        self.assertEqual(w["entry"]["slug"], slug)
        self.assertEqual(w["entry"]["note"], "renamed since I confirmed it")

    def test_validated_clears_the_rule(self):
        """``state: validated`` is the end of the report: the row is signed off again."""
        f, r = make_sync()
        slug = "beef-pho"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        rec = {"id": "1", "name": "Beef Pho",
               "validation": {"state": "validated", "confirmedSlug": slug, "note": "old report"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(r.apply_reported_notes({"1": w}, ["1"]), [])
        self.assertEqual(w["entry"]["status"], "ok")
        self.assertEqual(w["entry"]["note"], "user-confirmed")

    def test_no_note_or_blank_note_changes_nothing(self):
        f, r = make_sync()
        slug = "beef-pho"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        for validation in (None, {}, {"reportedBy": "octocat"}, {"note": "   "}, {"note": None}):
            rec = {"id": "1", "name": "Beef Pho"}
            if validation is not None:
                rec["validation"] = validation
            w = work_for(rec)
            r.resolve(rec, w, None)
            self.assertEqual(r.apply_reported_notes({"1": w}, ["1"]), [], json.dumps(validation))
            self.assertEqual(w["entry"]["status"], "ok")

    def test_an_ambiguous_row_keeps_its_candidates_so_the_modal_can_still_offer_a_pick(self):
        f, r = make_sync()
        f.client.table = {"beef-pho": profile("Beef", "Pho", "beef-pho"),
                          "beefpho": profile("Beefpho", "Alt", "beefpho")}
        rec = {"id": "1", "name": "Beef Pho", "validation": {"note": "neither of these"}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        self.assertEqual(w["entry"]["status"], "ambiguous")
        r.apply_reported_notes({"1": w}, ["1"])
        self.assertEqual(w["entry"]["status"], "needs_review")
        self.assertIsNone(w["entry"]["slug"])                  # never picked, still not picked
        self.assertEqual({c["slug"] for c in w["entry"]["candidates"]}, {"beef-pho", "beefpho"})
        self.assertEqual(w["entry"]["note"], "neither of these")

    def test_the_rule_is_idempotent_across_runs_and_capped_at_240_chars(self):
        f, r = make_sync()
        slug = "beef-pho"
        f.client.table = {slug: profile("Beef", "Pho", slug)}
        note = "n" * 300
        rec = {"id": "1", "name": "Beef Pho", "validation": {"note": note}}
        w = work_for(rec)
        r.resolve(rec, w, None)
        r.apply_reported_notes({"1": w}, ["1"])
        self.assertEqual(w["entry"]["note"], "n" * roster.REPORT_NOTE_MAX)
        self.assertEqual(len(w["entry"]["note"]), 240)
        # a second run over the row it already produced changes nothing (no note drift, no churn)
        again = dict(w["entry"])
        self.assertIs(resolve.reported_note_entry(rec, again), again)

    def test_lookup_mode_applies_the_same_rule(self):
        """The single-row write path cannot publish an ok row over an open report either."""
        rec = {"id": "1", "name": "Beef Pho", "validation": {"note": "wrong character"}}
        entry = artifacts.ok_entry(rec, slug="beef-pho",
                                   href="https://us.api.blizzard.com/profile/wow/character/beef-pho"
                                        "?namespace=profile-forever-us",
                                   payload={"name": "Beef"}, matched_form="lower", mapped_via="direct")
        after = resolve.reported_note_entry(rec, entry)
        self.assertEqual(after["status"], "needs_review")
        self.assertEqual(after["note"], "wrong character")
        self.assertEqual(after["slug"], "beef-pho")
        self.assertIsNone(resolve.reported_note_entry({"id": "1", "name": "x"}, entry).get("note"))

    def test_reported_note_helper_boundaries(self):
        self.assertIsNone(roster.reported_note({"id": "1", "name": "x"}))
        self.assertEqual(roster.reported_note({"id": "1", "validation": {"note": " a "}}), "a")
        self.assertIsNone(roster.reported_note({"id": "1", "validation": {"state": "validated",
                                                                         "note": "a"}}))
        # a non-string note (hand-edited JSON) is not a report, and never reaches an artifact
        self.assertIsNone(roster.reported_note({"id": "1", "validation": {"note": {"x": 1}}}))


class MassFlipFloorVersusReportedNotes(unittest.TestCase):
    """D2: the H4 floor judges the resolver verdict; the note rule still applies before the write.

    The interaction the reviewer flagged was untested: ``apply_reported_notes`` demotes ``ok`` rows
    to ``needs_review``, and if the floor counted those demotions, notes on more than half of the
    previously-ok rows would make every run refuse to publish (a frozen pipeline, and the report
    silently unapplied in exactly the mass-report case the rule exists for). These tests drive the
    real ``Sync.run_sync`` with a fake client: nothing here touches the network or the committed
    artifacts (both are redirected into a temp directory).
    """

    ROWS = [("r1", "alpha"), ("r2", "bravo"), ("r3", "charlie"), ("r4", "delta")]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.map_file, self.data_file = self.tmp / "battle-net-map.json", self.tmp / "character-data.json"
        self._files = (config.MAP_FILE, config.DATA_FILE)
        config.MAP_FILE, config.DATA_FILE = self.map_file, self.data_file
        self._step_summary = os.environ.pop("GITHUB_STEP_SUMMARY", None)

    def tearDown(self):
        config.MAP_FILE, config.DATA_FILE = self._files
        if self._step_summary is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = self._step_summary

    @classmethod
    def record(cls, rid, name, note=None):
        rec = {"id": rid, "name": name}
        if note is not None:
            rec["validation"] = {"note": note, "reportedBy": "octocat",
                                 "reportedAt": "2026-01-01T00:00:00Z"}
        return rec

    @classmethod
    def published_ok(cls, ids):
        """A previous map with every id ok: the state the floor protects."""
        return {rid: {"status": "ok", "slug": rid, "href":
                      f"https://us.api.blizzard.com/profile/wow/character/{rid}?namespace=profile-forever-us"}
                for rid in ids}

    def run_sync_with(self, records, prev_entries, table):
        args = argparse.Namespace(dry_run=False, lookup=None, force=False,
                                  state_dir=self.tmp / "state")
        s = sync.Sync(args)
        f, resolver = make_sync()                       # FakeClient + real Fetcher/Resolver
        f.client.table = dict(table)
        # Enrichment (gear, progress) is not what the floor is about; the pair still gets a row.
        f.enrich = lambda w: {"status": "ok", "fetchedAt": http.iso_now()}
        s.cache, s.client, s.f, s.resolver = f.cache, f.client, f, resolver
        out = io.StringIO()
        with redirect_stdout(out):
            code = s.run_sync(records, prev_entries)
        return code, out.getvalue()

    def table_for(self, *names):
        return {name: profile(name.capitalize(), None, name) for name in names}

    def test_notes_on_every_previously_ok_row_do_not_trip_the_floor(self):
        prev = self.published_ok([rid for rid, _ in self.ROWS])
        records = [self.record(rid, name, note=f"that is the other server's {name}")
                   for rid, name in self.ROWS]
        code, out = self.run_sync_with(records, prev, self.table_for(*[n for _, n in self.ROWS]))
        self.assertEqual(code, 0, out)
        self.assertNotIn("mass status flip", out)
        self.assertTrue(self.map_file.exists() and self.data_file.exists())
        written = json.loads(self.map_file.read_text())
        self.assertEqual({e["status"] for e in written["entries"].values()}, {"needs_review"})
        self.assertEqual(written["entries"]["r1"]["note"], "that is the other server's alpha")
        self.assertEqual(written["entries"]["r1"]["slug"], "alpha")     # identity kept, not blanked
        # 4 of 4 previously-ok rows demoted by hand: below the floor, and correctly published.
        sidecar = json.loads(self.data_file.read_text())
        self.assertEqual({e["status"] for e in sidecar["characters"].values()}, {"needs_review"})
        self.assertTrue(all("gear" not in row for row in sidecar["characters"].values()),
                        "no enriched sidecar row is published under a mapping a human rejected")

    def test_a_genuine_mass_outage_still_trips_the_floor(self):
        prev = self.published_ok([rid for rid, _ in self.ROWS])
        # Nothing answers this run: exactly the upstream 404 storm the floor exists for.
        self.map_file.write_text('{"sentinel": "previous map"}', encoding="utf-8")
        self.data_file.write_text('{"sentinel": "previous data"}', encoding="utf-8")
        code, out = self.run_sync_with([self.record(rid, name) for rid, name in self.ROWS], prev, {})
        self.assertEqual(code, 1, out)
        self.assertIn("mass status flip", out)
        self.assertIn("4 ok rows published before this run, 0 ok now", out)
        self.assertEqual(json.loads(self.map_file.read_text()), {"sentinel": "previous map"})
        self.assertEqual(json.loads(self.data_file.read_text()), {"sentinel": "previous data"})

    def test_the_floor_decision_is_driven_only_by_resolver_verdicts(self):
        ids = [rid for rid, _ in self.ROWS]
        prev = self.published_ok(ids)
        # Two of four resolve (exactly half: the floor allows), and one of those two carries a
        # reviewer note. The map that lands has a single ok row — below the floor — which is only
        # possible because the decision was taken on the resolver verdict, before the note pass.
        records = [self.record("r1", "alpha", note="wrong character entirely"),
                   self.record("r2", "bravo"),
                   self.record("r3", "charlie"),
                   self.record("r4", "delta")]
        code, out = self.run_sync_with(records, prev, self.table_for("alpha", "bravo"))
        self.assertEqual(code, 0, out)
        written = json.loads(self.map_file.read_text())["entries"]
        self.assertEqual({rid: written[rid]["status"] for rid in ids},
                         {"r1": "needs_review", "r2": "ok", "r3": "not_found", "r4": "not_found"})
        self.assertEqual(sum(1 for e in written.values() if e["status"] == "ok"), 1)

    def test_notes_cannot_rescue_a_run_the_verdicts_failed(self):
        prev = self.published_ok([rid for rid, _ in self.ROWS])
        self.map_file.write_text('{"sentinel": "previous map"}', encoding="utf-8")
        # One row resolves and carries a note; the other three 404. The note is irrelevant to the
        # floor: one ok against four published is below half, so the outage still refuses.
        records = [self.record("r1", "alpha", note="looks right to me"),
                   self.record("r2", "bravo"), self.record("r3", "charlie"), self.record("r4", "delta")]
        code, out = self.run_sync_with(records, prev, self.table_for("alpha"))
        self.assertEqual(code, 1, out)
        self.assertIn("mass status flip", out)
        self.assertIn("1 ok now", out)
        self.assertIn("sentinel", self.map_file.read_text())

    def test_resolver_verdict_snapshot_is_immune_to_later_mutation(self):
        ids = ["1", "2", "3", "4"]
        entries = {"1": artifacts.map_entry({"name": "a"}, "ok"),
                   "2": artifacts.map_entry({"name": "b"}, "ok"),
                   "3": artifacts.map_entry({"name": "c"}, "needs_review", note="reported"),
                   "4": artifacts.map_entry({"name": "d"}, "not_found", note="404")}
        verdict = artifacts.resolver_verdict(entries, ids)
        # The enrichment loop rewrites entries in place; the verdict the floor was decided on
        # cannot move with them, so the pre-check and write_pair keep agreeing on one decision.
        entries["1"].update({"status": "fetch_error", "note": "enrichment skipped"})
        self.assertEqual(artifacts.ok_count(verdict, ids), 2)
        self.assertEqual(artifacts.ok_count(entries, ids), 1)
        prev = {rid: {"status": "ok"} for rid in ids}
        self.assertIsNone(artifacts.mass_flip_refusal(verdict, ids, prev))
        self.assertIsNotNone(artifacts.mass_flip_refusal(entries, ids, prev))
        # An id with no entry at all is not ok, and never raises.
        self.assertEqual(artifacts.ok_count(artifacts.resolver_verdict({}, ["9"]), ["9"]), 0)

    def test_write_pair_takes_the_floor_from_the_snapshot_it_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_file, data_file = Path(tmp) / "m.json", Path(tmp) / "d.json"
            prev = {"1": {"status": "ok"}, "2": {"status": "ok"}, "3": {"status": "ok"}}
            # The write itself is below the floor (0 ok), the snapshot is not (3 ok).
            entries = {rid: artifacts.map_entry({"name": rid}, "needs_review", note="reported")
                       for rid in prev}
            snapshot = {rid: {"status": "ok"} for rid in prev}
            original = (config.MAP_FILE, config.DATA_FILE)
            try:
                config.MAP_FILE, config.DATA_FILE = map_file, data_file
                self.assertIsNotNone(artifacts.write_pair(entries, {rid: {"status": "needs_review"} for rid in prev},
                                                          ordered_ids=list(prev), prev_entries=prev,
                                                          triggered_by_value="manual", dry_run=False))
                self.assertFalse(map_file.exists())
                self.assertIsNone(artifacts.write_pair(entries, {rid: {"status": "needs_review"} for rid in prev},
                                                       ordered_ids=list(prev), prev_entries=prev,
                                                       triggered_by_value="manual", dry_run=False,
                                                       floor_entries=snapshot))
                self.assertEqual(json.loads(map_file.read_text())["entries"]["1"]["status"], "needs_review")
            finally:
                config.MAP_FILE, config.DATA_FILE = original


class LogHygiene(unittest.TestCase):
    """NIT 9: untrusted text is one printable line at the log boundary, verbatim in the artifact."""

    FORGED = "wrong realm\n\x1b[31mFATAL: forged by a reviewer note\x1b[0m"

    def test_log_safe_collapses_newlines_and_ansi_to_one_printable_line(self):
        safe = sync.log_safe(self.FORGED)
        self.assertEqual(safe, "wrong realm FATAL: forged by a reviewer note")
        self.assertNotIn("\n", safe)
        self.assertNotIn("\x1b", safe)
        self.assertEqual(sync.log_safe(None), "")
        self.assertEqual(sync.log_safe("a\r\nb\x00c"), "a b c")
        self.assertEqual(len(sync.log_safe("x" * 900)), sync.LOG_LINE_MAX)

    def test_emit_summary_cannot_be_split_into_a_forged_log_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            sync.Sync.emit_summary(object.__new__(sync.Sync), [f"id 7: {sync.log_safe(self.FORGED)}"])
        printed = out.getvalue().splitlines()
        self.assertTrue(any("forged by a reviewer note" in line for line in printed))
        self.assertFalse([line for line in printed if line.startswith("FATAL")],
                         f"a roster note forged its own log line: {printed}")

    def test_the_committed_note_stays_verbatim(self):
        # Sanitizing is for the log only: the artifact value is the reviewer's text, unchanged.
        entry = artifacts.map_entry({"name": "a"}, "needs_review", note=self.FORGED)
        self.assertEqual(entry["note"], self.FORGED)
        self.assertEqual(roster.reported_note({"id": "1", "validation": {"note": self.FORGED}}),
                         self.FORGED.strip()[:roster.REPORT_NOTE_MAX])


class ArtifactsSchema(unittest.TestCase):
    def test_map_entry_always_carries_candidates(self):
        entry = artifacts.map_entry({"name": "x"}, "not_found", note="404 on all forms")
        self.assertEqual(set(entry), set(artifacts.MAP_ENTRY_KEYS))
        self.assertEqual(entry["candidates"], [])

    def test_candidate_href_is_h2_validated(self):
        cand = artifacts.candidate_entry(slug="evil", href="https://attacker.tld/beacon")
        self.assertIsNone(cand["href"])

    def test_map_entry_href_is_h2_validated(self):
        entry = artifacts.ok_entry({"name": "x"}, slug="x", href="http://us.api.blizzard.com/x",
                                   payload={"name": "x"}, matched_form="lower", mapped_via="direct")
        self.assertIsNone(entry["href"])

    def test_triggered_by_precedence(self):
        self.assertEqual(artifacts.triggered_by({"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "octocat"}),
                         "schedule")
        self.assertEqual(artifacts.triggered_by({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "octocat"}),
                         "octocat")
        self.assertEqual(artifacts.triggered_by({}), "manual")

    def test_mass_flip_floor(self):
        self.assertIsNone(artifacts.mass_flip_error(4, 4))
        self.assertIsNone(artifacts.mass_flip_error(4, 2))      # exactly half is allowed
        self.assertIsNotNone(artifacts.mass_flip_error(4, 1))
        self.assertIsNone(artifacts.mass_flip_error(2, 0))      # below ok_prev >= 3: nothing to protect
        self.assertIn("amendment 8", artifacts.mass_flip_error(4, 1))

    def test_write_pair_blocks_below_the_floor_and_writes_schema_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_file = Path(tmp) / "battle-net-map.json"
            data_file = Path(tmp) / "character-data.json"
            prev = {"1": {"status": "ok"}, "2": {"status": "ok"},
                    "3": {"status": "ok"}, "4": {"status": "ok"}}
            entries = {rid: artifacts.map_entry({"name": rid}, "not_found", note="404") for rid in prev}
            chars = {rid: {"status": "not_found", "fetchedAt": http.iso_now()} for rid in prev}
            original = (config.MAP_FILE, config.DATA_FILE)
            try:
                config.MAP_FILE, config.DATA_FILE = map_file, data_file
                refusal = artifacts.write_pair(entries, chars, ordered_ids=list(prev), prev_entries=prev,
                                               triggered_by_value="manual", dry_run=False)
                self.assertIsNotNone(refusal)
                self.assertFalse(map_file.exists() and data_file.exists())

                ok_prev = {"1": {"status": "not_found"}}
                entries = {rid: artifacts.ok_entry({"name": rid}, slug=rid,
                                                   href=f"https://us.api.blizzard.com/profile/wow/character/{rid}"
                                                        f"?namespace=profile-forever-us",
                                                   payload={"name": rid}, matched_form="lower",
                                                   mapped_via="direct") for rid in prev}
                chars = {rid: {"status": "ok", "fetchedAt": http.iso_now()} for rid in prev}
                self.assertIsNone(artifacts.write_pair(entries, chars, ordered_ids=list(prev),
                                                       prev_entries=ok_prev, triggered_by_value="schedule",
                                                       dry_run=False))
                written_map = json.loads(map_file.read_text())
                written_data = json.loads(data_file.read_text())
                self.assertEqual(written_map["schemaVersion"], 2)
                self.assertEqual(set(written_map), {"schemaVersion", "updatedAt", "entries"})
                self.assertEqual(set(written_map["entries"]["1"]), set(artifacts.MAP_ENTRY_KEYS))
                self.assertEqual(set(written_data), {"schemaVersion", "syncedAt", "triggeredBy", "characters"})
                self.assertEqual(written_data["triggeredBy"], "schedule")
            finally:
                config.MAP_FILE, config.DATA_FILE = original

    def test_lookup_seeds_and_reports_ambiguity(self):
        f, r = make_sync()
        f.client.table = {"beef-pho": profile("Beef", "Pho", "beef-pho"),
                          "beefpho": profile("Beefpho", "Alt", "beefpho")}
        result = r.lookup_name("Beef Pho", [])
        self.assertEqual(result["via"], "direct")
        self.assertTrue(result["status"].startswith("ambiguous"))
        self.assertIsNone(result["payload"])
        self.assertEqual(len(result["candidates"]), 2)


class SecurityInvariants(unittest.TestCase):
    def test_allowlist_accepts_only_the_two_blizzard_origins(self):
        self.assertTrue(http.allowed_api_url("https://us.api.blizzard.com/x"))
        self.assertTrue(http.allowed_api_url("https://us.battle.net/oauth/token"))
        for bad in ("https://us.api.blizzard.com.evil.tld/x", "https://evil.tld/",
                    "http://us.api.blizzard.com/x", "https://us.battle.net@evil.tld/",
                    "https://us.api.blizzard.preprod.com/x"):
            self.assertFalse(http.allowed_api_url(bad), bad)
        with self.assertRaises(http.UnsafeUrl):
            http.require_allowed_url("https://evil.tld/", "GET")

    def test_no_redirect_handler_refuses_to_replay(self):
        self.assertIsNone(http._NoRedirectHandler().redirect_request(
            None, None, 302, "Found", {}, "https://evil.tld/"))
        self.assertIsInstance(http.NO_REDIRECT_OPENER.open, object)
        handlers = {type(h).__name__ for h in http.NO_REDIRECT_OPENER.handlers}
        self.assertIn("_NoRedirectHandler", handlers)
        self.assertNotIn("HTTPRedirectHandler", handlers)

    def test_derived_credential_forms_are_masked(self):
        red = http.Redactor()
        red.client_id, red.client_secret = "cid value", "sec ret"
        red._values = ["cid value", "sec ret"]
        http.REDACTOR = red
        try:
            pair, basic = http.register_mint_forms("cid value", "sec ret")
            secret_b64 = basic
            for form in (pair, basic, f"Basic {basic}", "cid%20value", "sec%20ret",
                         "cid+value", "sec+ret"):
                self.assertNotIn(form, redact_all(form), form)
            self.assertNotIn(secret_b64, redact_all(f"header echoed {secret_b64}"))
            self.assertIn("[REDACTED]", redact_all(f"echoed {secret_b64}"))
        finally:
            http.REDACTOR = http.Redactor()

    def test_foreign_urls_are_withheld_but_blizzard_hosts_survive(self):
        kept = "https://us.api.blizzard.com/profile/wow/character/x?namespace=profile-forever-us"
        icon = "https://render.worldofwarcraft.com/forever-us/icons/56/1.jpg"
        self.assertEqual(http.redact(kept), kept)
        self.assertEqual(http.redact(icon), icon)
        self.assertNotIn("attacker.tld", http.redact("refused https://attacker.tld/beacon?x=1"))
        nested = http.redact({"note": "see https://attacker.tld/b"})
        self.assertIn("withheld", nested["note"])
        self.assertNotIn("attacker.tld", json.dumps(nested))

    def test_icon_validator_only_accepts_the_render_host(self):
        self.assertIsNone(fetch.safe_artifact_url("https://evil.tld/i.jpg", "iconUrl"))
        self.assertEqual(fetch.safe_artifact_url("https://render.worldofwarcraft.com/a.jpg", "iconUrl"),
                         "https://render.worldofwarcraft.com/a.jpg")
        with self.assertRaises(ValueError):
            fetch.safe_artifact_url("x", "unknownField")

    def test_canonical_href_rejects_non_profile_shapes(self):
        self.assertIsNone(fetch.canonical_href({"href": "https://evil.tld/profile/wow/character/x"}))
        self.assertIsNone(fetch.canonical_href({"href": "https://us.api.blizzard.com/data/wow/item/1"}))
        good = "https://us.api.blizzard.com/profile/wow/character/x?namespace=profile-forever-us"
        self.assertEqual(fetch.canonical_href({"_links": {"self": {"href": good}}}), good)

    def test_no_bare_urlopen_and_no_if_none_match_on_the_sync_surface(self):
        sync_surface = "".join((Path(config.__file__).parent / name).read_text()
                               for name in ("__init__.py", "__main__.py", "config.py", "http.py",
                                            "roster.py", "resolve.py", "fetch.py", "artifacts.py", "sync.py"))
        self.assertNotIn("urllib.request.urlopen(", sync_surface)
        self.assertNotIn("If-None-Match", sync_surface)
        self.assertIn("If-Modified-Since", sync_surface)

    def test_probe_requests_are_host_pinned(self):
        client = http.ProbeHttp()
        with self.assertRaises(http.UnsafeUrl):
            client.get("https://evil.tld/profile/wow/character/x", "test")


def redact_all(text):
    return http.redact(text)


def config_artifact_keys():
    return artifacts.CANDIDATE_KEYS


if __name__ == "__main__":
    unittest.main(verbosity=2)
