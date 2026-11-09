#!/usr/bin/env python3
"""Offline unit tests for the Phase B sync side: media.fullRender.

Phase B (roster-ui-tasks.md) derives the gear paper doll's full-body render from the
stored avatar by swapping ``-avatar.jpg`` for ``-main-raw.png``, and publishes it only
after the render host answers that exact URL with HTTP 200. Two halves are pinned
here, because they fail differently:

* the DERIVATION (``fetch.derive_full_render_url``) is pure — an absent avatar, an
  ``avatarSd``, a renamed asset or an off-host value all yield None, so the sync can
  never publish a render URL it invented;
* the VALIDATION (``Fetcher.full_render_url``) is the one request that path costs,
  and only 200 publishes. 404, 5xx, a 3xx (the opener never follows one, so a
  redirect is a refusal the caller must see), a transport error and a budget stop are
  all ``media.fullRender: null`` — never a placeholder and never the previous URL.

The transport itself (``http.Client.head``) is tested against a fake ``send`` so the
security shape is pinned offline too: a real HEAD, host-pinned to the render host,
with no credential attached and no token minted for it.

Stdlib unittest only: no network, no credentials, writes redirected to a temp dir.

Run: python3 -m unittest discover -s tools -p "test_*"
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/

from bnet import artifacts, config, fetch, http  # noqa: E402

STATIC = "static-1.60.1_70399-forever-us"
API = config.API_BASE
RENDER = "https://render.worldofwarcraft.com"
AVATAR = f"{RENDER}/forever-us/character/69/249157-avatar.jpg"
FULL_RENDER = f"{RENDER}/forever-us/character/69/249157-main-raw.png"
PROFILE_HREF = f"{API}/profile/wow/character/beef-pho?namespace={config.NS_PROFILE}"


class Resp:
    """The slice of ``http.Resp`` the Fetcher reads."""

    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    @property
    def json(self):
        return self._payload


class TableClient:
    """Answers GETs and HEADs from one {url: (status, payload)} table.

    Misses are 404, like the gateway. The two verbs record separately so a test can
    say which method each URL saw, and the recorded order is the send order."""

    def __init__(self, table=None, heads=None):
        self.table = dict(table or {})
        self.heads = dict(heads or {})
        self.urls = []
        self.head_urls = []
        self.tokens_minted = 1
        self.requests_made = 0
        self.revalidations = 0
        self.status_counts = {}

    def get(self, url):
        self.urls.append(url)
        self.requests_made += 1
        return Resp(*self.table.get(url, (404, None)))

    def head(self, url):
        self.head_urls.append(url)
        self.requests_made += 1
        return Resp(*self.heads.get(url, (404, None)))


def make_fetcher(table=None, heads=None):
    cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
    f = fetch.Fetcher(TableClient(table, heads), cache)
    f.static_ns = STATIC
    return f


# --------------------------------------------------------------------------- #
# 1. The derivation is pure, and a miss is None — never a guessed URL
# --------------------------------------------------------------------------- #
class DeriveFullRenderUrl(unittest.TestCase):
    def test_happy_path_swaps_only_the_trailing_suffix(self):
        self.assertEqual(fetch.derive_full_render_url(AVATAR), FULL_RENDER)

    def test_the_swap_is_config_declared_not_hardcoded_here(self):
        """config is the one place a patch can move the asset names; the derivation
        must follow it, and this test is what proves it is read from there."""
        with mock.patch.object(config, "AVATAR_ASSET_SUFFIX", "-avatar.jpg"), \
                mock.patch.object(config, "FULL_RENDER_ASSET_SUFFIX", "-main-raw.webp"):
            self.assertEqual(fetch.derive_full_render_url(AVATAR),
                             f"{RENDER}/forever-us/character/69/249157-main-raw.webp")

    def test_avatar_sd_is_not_an_avatar(self):
        sd = AVATAR.replace("-avatar.jpg", "-avatar-sd.jpg")
        self.assertIsNone(fetch.derive_full_render_url(sd))

    def test_missing_suffix_is_none_not_a_guess(self):
        for value in (f"{RENDER}/forever-us/character/69/249157-main-raw.png",
                      f"{RENDER}/forever-us/character/69/249157.jpg",
                      f"{RENDER}/forever-us/character/69/avatar.jpg-x",
                      ""):
            self.assertIsNone(fetch.derive_full_render_url(value), value)

    def test_absent_avatar_is_none(self):
        for value in (None, 0, False, [], {}, {"avatar": AVATAR}):
            self.assertIsNone(fetch.derive_full_render_url(value), repr(value))

    def test_off_host_avatar_yields_no_publishable_render(self):
        """H2: the derived URL clears the same validator as any other artifact value,
        so a poisoned host upstream cannot become a hotlinked render."""
        self.assertIsNone(fetch.derive_full_render_url("https://attacker.tld/beacon-avatar.jpg"))

    def test_the_swap_is_the_suffix_only_not_a_path_rewrite(self):
        derived = fetch.derive_full_render_url(f"{RENDER}/forever-us/character/1/9-avatar.jpg")
        self.assertEqual(derived, f"{RENDER}/forever-us/character/1/9-main-raw.png")


# --------------------------------------------------------------------------- #
# 2. HEAD validation: only an explicit 200 publishes
# --------------------------------------------------------------------------- #
class FullRenderProbe(unittest.TestCase):
    def test_head_200_publishes_the_derived_url(self):
        f = make_fetcher(heads={FULL_RENDER: (200, None)})
        self.assertEqual(f.full_render_url(AVATAR, "1"), FULL_RENDER)
        self.assertEqual(f.client.head_urls, [FULL_RENDER])
        self.assertEqual((f.render_validated, f.render_failures), (1, 0))

    def test_head_404_and_500_publish_null(self):
        for status in (404, 500, 503, 403, 301, 302, 307, 204):
            with self.subTest(status=status):
                f = make_fetcher(heads={FULL_RENDER: (status, None)})
                self.assertIsNone(f.full_render_url(AVATAR, "1"))
                self.assertEqual(f.render_validated, 0)
                self.assertEqual(f.render_failures, 1)

    def test_a_redirect_is_a_refusal_not_a_hop(self):
        """NO_REDIRECT_OPENER means the 3xx arrives as a status: taking it would put
        an unvalidated URL into a publicly served artifact."""
        f = make_fetcher(heads={FULL_RENDER: (302, None)})
        self.assertIsNone(f.full_render_url(AVATAR, "1"))
        self.assertEqual(f.client.head_urls, [FULL_RENDER])       # asked once, did not follow

    def test_transport_error_publishes_null(self):
        f = make_fetcher()
        f.client.head = lambda url: Resp(None, None)
        self.assertIsNone(f.full_render_url(AVATAR, "1"))
        self.assertEqual(f.render_failures, 1)

    def test_no_avatar_sends_no_request_at_all(self):
        f = make_fetcher(heads={FULL_RENDER: (200, None)})
        self.assertIsNone(f.full_render_url(None, "1"))
        self.assertEqual(f.client.head_urls, [])
        self.assertEqual((f.render_validated, f.render_failures), (0, 0))

    def test_no_suffix_sends_no_request_at_all(self):
        f = make_fetcher(heads={FULL_RENDER: (200, None)})
        self.assertIsNone(f.full_render_url(AVATAR.replace("-avatar.jpg", "-avatar-sd.jpg"), "1"))
        self.assertEqual(f.client.head_urls, [])

    def test_a_refused_url_is_reported_and_publishes_null(self):
        """H1 stays fatal-to-nothing: an UnsafeUrl from the transport costs the render,
        not the character."""
        f = make_fetcher()

        def refuse(url):
            raise http.UnsafeUrl("refused")

        f.client.head = refuse
        self.assertIsNone(f.full_render_url(AVATAR, "1"))
        self.assertEqual(f.render_failures, 1)
        self.assertTrue(any("asset allowlist" in line for line in f.warnings))

    def test_budget_exhaustion_propagates_like_every_other_send(self):
        f = make_fetcher()

        def capped(url):
            raise http.BudgetExhausted("request cap (2500) reached")

        f.client.head = capped
        with self.assertRaises(http.BudgetExhausted):
            f.full_render_url(AVATAR, "1")


# --------------------------------------------------------------------------- #
# 3. The media enrichment path: the key is always written, url or explicit null
# --------------------------------------------------------------------------- #
class EnrichMediaIntegration(unittest.TestCase):
    def media_payload(self):
        return {"assets": [
            {"key": "avatar", "value": AVATAR},
            {"key": "avatar-sd", "value": AVATAR.replace("-avatar.jpg", "-avatar-sd.jpg")}]}

    def media_url(self):
        return (f"{API}/profile/wow/character/beef-pho/character-media"
                f"?namespace={config.NS_PROFILE}")

    def base_table(self):
        return {self.media_url(): (200, self.media_payload())}

    def enrich(self, table=None, heads=None):
        f = make_fetcher(self.base_table() if table is None else table, heads)
        w = {"rec": {"id": "1", "name": "Beef Pho"},
             "profile": {"name": "Beef", "surname": "Pho", "level": 20},
             "entry": {"href": PROFILE_HREF}, "profile_url": PROFILE_HREF, "unchanged": False}
        return f, f.enrich(w)

    def test_validated_render_lands_in_the_media_block(self):
        f, out = self.enrich(heads={FULL_RENDER: (200, None)})
        self.assertEqual(out["media"]["fullRender"], FULL_RENDER)
        self.assertEqual(out["media"]["avatar"], AVATAR)
        self.assertEqual(set(out["media"]), set(artifacts.MEDIA_KEYS))
        self.assertEqual(set(out) - set(artifacts.CHARACTER_KEYS), set())

    def test_refused_render_is_an_explicit_null_and_a_note_not_a_feature_failure(self):
        f, out = self.enrich()                      # HEAD 404: nothing in the heads table
        self.assertIn("fullRender", out["media"])
        self.assertIsNone(out["media"]["fullRender"])
        self.assertEqual(f.render_failures, 1)
        # A null is a published answer, not a fault: the refusal never joins the
        # "optional feature block not stored" list (the other blocks are absent from
        # this minimal fixture, hence the filter rather than an empty-list assert).
        self.assertFalse([line for line in f.feature_failures if "render" in line])
        self.assertEqual(out["media"]["avatar"], AVATAR)   # the row itself is untouched

    def test_no_media_block_means_no_full_render_key(self):
        """The convention's other half: absence of the whole block still means "no
        character-media payload", and is reported as one."""
        f, out = self.enrich(table={})
        self.assertNotIn("media", out)
        self.assertTrue(any("character-media" in line for line in f.feature_failures))

    def test_a_poisoned_avatar_cannot_produce_a_render(self):
        f, out = self.enrich(
            table={self.media_url():
                   (200, {"assets": [{"key": "avatar", "value": "https://attacker.tld/beacon-avatar.jpg"}]})},
            heads={FULL_RENDER: (200, None)})
        self.assertIsNone(out["media"]["avatar"])
        self.assertIsNone(out["media"]["fullRender"])
        self.assertEqual(f.client.head_urls, [])
        self.assertEqual(f.render_validated, 0)
        self.assertIn(self.media_url(), f.client.urls)      # the media doc was read…
        self.assertNotIn("attacker.tld", " ".join(f.client.urls + f.client.head_urls))   # …never followed

    def test_media_keys_match_the_writer_schema_constant(self):
        """The gate in .pi/scripts/verify_sync_artifacts.py mirrors this tuple; a new
        media key has to be declared in artifacts.py, not just emitted."""
        self.assertEqual(set(artifacts.MEDIA_KEYS), {"avatar", "avatarSd", "fullRender"})


# --------------------------------------------------------------------------- #
# 4. The transport: a real HEAD, host-pinned, uncredentialed
# --------------------------------------------------------------------------- #
class HeadTransport(unittest.TestCase):
    def setUp(self):
        self.cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
        self.client = http.Client(self.cache, force=False)
        self.client.token = "super-secret-token"
        self.sent = []

        def fake_send(request):
            self.sent.append(request)
            return (200, {"content-type": "image/png"}, b"", None)

        self.patcher = mock.patch.object(http, "send", fake_send)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.sleep = mock.patch.object(http.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_the_request_is_a_head_with_the_sync_user_agent(self):
        resp = self.client.head(FULL_RENDER)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.sent), 1)
        request = self.sent[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.get_header("User-agent"), config.USER_AGENT)
        self.assertIsNone(request.get_header("Authorization"),
                          "the render host must never see the bearer token")

    def test_no_token_is_minted_for_a_render_probe(self):
        self.client.token = None
        with mock.patch.object(self.client, "mint_token",
                               side_effect=AssertionError("a HEAD must not mint a token")):
            self.assertEqual(self.client.head(FULL_RENDER).status, 200)

    def test_an_off_allowlist_url_is_refused_before_anything_is_sent(self):
        for url in ("https://attacker.tld/beacon.png",
                    "https://render.worldofwarcraft.com.evil.tld/x-main-raw.png",
                    "http://render.worldofwarcraft.com/x-main-raw.png",
                    None):
            with self.subTest(url=url):
                with self.assertRaises(http.UnsafeUrl):
                    self.client.head(url)
        self.assertEqual(self.sent, [])

    def test_a_redirect_is_reported_as_its_status_not_followed(self):
        """send() answers through NO_REDIRECT_OPENER, so the 3xx is the result; HEAD
        must surface it unchanged for the caller to read as a refusal."""
        def fake_3xx(request):
            self.sent.append(request)
            return (302, {"location": "https://elsewhere.tld/render.png"}, b"", None)

        with mock.patch.object(http, "send", fake_3xx):
            resp = self.client.head(FULL_RENDER)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers.get("location"), "https://elsewhere.tld/render.png")
        self.assertEqual(len(self.sent), 1)

    def test_a_transport_error_is_a_status_of_none(self):
        with mock.patch.object(http, "send", lambda request: (None, {}, b"", "URLError: boom")):
            self.assertIsNone(self.client.head(FULL_RENDER).status)

    def test_head_requests_count_against_the_run_cap(self):
        self.client.requests_made = config.MAX_REQUESTS
        with self.assertRaises(http.BudgetExhausted):
            self.client.head(FULL_RENDER)
        self.assertEqual(self.sent, [])

    def test_head_does_not_touch_the_body_cache(self):
        self.client.head(FULL_RENDER)
        self.assertIsNone(self.cache.body(FULL_RENDER))
        self.assertEqual(self.cache.urls, {})

    def test_get_contract_is_unchanged_by_the_new_send_path(self):
        """The old path keeps its semantics: credentialed, conditional, cached."""
        def fake_get(request):
            self.sent.append(request)
            return (200, {"last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
                    b'{"ok": true}', None)

        with mock.patch.object(http, "send", fake_get):
            resp = self.client.get(f"{API}/profile/wow/character/beef-pho")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json, {"ok": True})
        self.assertEqual(self.sent[0].get_method(), "GET")
        self.assertEqual(self.sent[0].get_header("Authorization"), "Bearer super-secret-token")
        self.assertIsNotNone(self.cache.body(f"{API}/profile/wow/character/beef-pho"))


# --------------------------------------------------------------------------- #
# 5. The run summary reports refusals (never a silently missing render)
# --------------------------------------------------------------------------- #
class SummaryReporting(unittest.TestCase):
    def make_sync(self):
        import argparse
        from bnet import sync

        s = sync.Sync.__new__(sync.Sync)
        s.args = argparse.Namespace(dry_run=True, lookup=None, force=False,
                                    state_dir=tempfile.mkdtemp())
        s.cache = http.Cache(Path(tempfile.mkdtemp()), dry_run=True)
        s.client = TableClient({})
        s.f = fetch.Fetcher(s.client, s.cache)
        s.f.static_ns = STATIC
        s.budget_hit = False
        return s

    def test_unvalidated_renders_are_reported_as_a_note(self):
        from io import StringIO
        from contextlib import redirect_stdout

        s = self.make_sync()
        s.f.render_failures = 3
        out = StringIO()
        with redirect_stdout(out):
            s.emit_summary(s.summary_lines({"ok": 1}, 1, 0.0))
        printed = out.getvalue()
        self.assertIn("3 character render(s) not validated", printed)
        self.assertIn("media.fullRender is null", printed)

    def test_a_clean_run_prints_no_render_note(self):
        from io import StringIO
        from contextlib import redirect_stdout

        s = self.make_sync()
        s.f.render_validated = 1
        out = StringIO()
        with redirect_stdout(out):
            s.emit_summary(s.summary_lines({"ok": 1}, 1, 0.0))
        self.assertNotIn("character render(s) not validated", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
