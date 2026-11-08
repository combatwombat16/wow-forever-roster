#!/usr/bin/env python3
"""Phase 2 nightly Battle.net sync for the WoW: Forever roster.

Resolves every characters.json row (keyed by roster id — amendment 2) against
the forever API, enriches the resolved ones, and writes the two committed
artifacts:

    battle-net-map.json    roster-id -> Battle.net identity map
    character-data.json    normalized sidecar consumed by the site UI

Every URL template, namespace and JSON path is the frozen contract recorded in
docs/bnet-api-notes.md; nothing here requests a path the Phase 1 probe did not
prove. Credentials come from BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET, are
never printed, logged or written, and every string that could carry one passes
through redact() first (pattern proven in tools/probe_bnet.py).

Usage:
    set -a && . ./.env && set +a && python3 tools/sync_bnet.py [options]

Options:
    --lookup NAME   maintainer lookup: GET the name, also try the guild-roster
                    lever, print the full result, update the map if unambiguous
    --force         ignore cached Last-Modified timestamps and refetch
    --dry-run       compute everything but write no files
    --state-dir P   cache directory (default ~/.cache/wow-bnet-sync; the cache
                    deliberately lives outside the repo so no state is committed)

Exit codes:
    0  run completed — not_found / needs_review / fetch_error per character are
       findings, not failures; a partial run still writes both artifacts (2.8)
    1  credentials missing, token mint failed, every character failed to fetch,
       or the mass-flip floor refused the write (H4); in those cases the previous
       artifacts are left untouched (2.8)
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Contract config. Every Blizzard path and namespace lives in this block and
# nowhere else (mirrors tools/probe_bnet.py): the endpoints are undocumented,
# version-pinned and a game patch can move any of them.
# --------------------------------------------------------------------------- #
TOKEN_URL = "https://us.battle.net/oauth/token"
API_BASE = "https://us.api.blizzard.com"
NS_PROFILE = "profile-forever-us"
GUILD_ROSTER_PATH = "/data/wow/guild/{slug}/roster"
PLAYABLE_CLASS_INDEX = "/data/wow/playable-class/index"
# The static namespace is version-pinned and must be re-discovered every run
# from hrefs in fresh payloads (notes §5); plain static-forever-us is 403.
STATIC_NS_RE = re.compile(r"namespace=(static-[A-Za-z0-9._-]+)")

# --- URL trust boundaries (security review 2026-11-08: H1, H2) --------------- #
# Every href in a Blizzard response is untrusted input, so there are two
# separate boundaries, deliberately not one regex:
#   ALLOWED_API_ORIGIN_RE gates anything we attach an Authorization header to
#   (checked at the single Client.get choke point, plus the token mint);
#   PROFILE_HREF_RE / ICON_URL_RE gate what may be WRITTEN into a committed
#   artifact, where a poisoned URL would be re-fetched by every later run.
# Anchored host + mandatory /|? boundary, so https://us.battle.net@evil.tld/ and
# https://us.api.blizzard.com.evil.tld/ both fail closed.
ALLOWED_API_ORIGIN_RE = re.compile(r"^https://(us\.battle\.net|us\.api\.blizzard\.com)(/|\?|$)")
PROFILE_HREF_RE = re.compile(r"^https://us\.api\.blizzard\.com/profile/wow/character/[^/?]+/?(\?.*)?$")
ICON_URL_RE = re.compile(r"^https://render\.worldofwarcraft\.com/")
ARTIFACT_URL_RES = {"href": PROFILE_HREF_RE, "iconUrl": ICON_URL_RE}
# Anything textual (fetch_error notes, warnings, step-summary lines, cached bodies)
# is also withheld from non-Blizzard URLs: a refusal note that quotes the attacker's
# URL would otherwise be committed into a public artifact.
FOREIGN_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
PUBLISHED_URL_RES = (ALLOWED_API_ORIGIN_RE, ICON_URL_RE)

REQUEST_SPACING_S = 0.25      # client pacing; Blizzard allows 100 req/s (notes §1)
MAX_REQUESTS = 2500           # hard per-run cap; a hit stops the run cleanly
HTTP_TIMEOUT_S = 30
MAX_TOKEN_MINTS = 3           # initial + 401 refresh + one spare; tokens last 24 h
MAX_429_RETRIES = 3
USER_AGENT = "wow-forever-roster/2.0 (nightly roster sync; read-only)"

REPO = Path(__file__).resolve().parent.parent
ROSTER_FILE = REPO / "characters.json"
MAP_FILE = REPO / "battle-net-map.json"
DATA_FILE = REPO / "character-data.json"
DEFAULT_STATE_DIR = Path.home() / ".cache" / "wow-bnet-sync"
CACHE_VERSION = 1
MAX_CACHE_URLS = 4000         # LRU prune so months of runs cannot grow the file forever

# Surfaces whose battlenet-schema / -revision headers are logged as the drift
# tripwire (amendment 7); a revision change is reported loudly but never aborts.
DRIFT_SURFACES = ("profile", "equipment", "specializations", "achievements",
                  "statistics", "pvp_summary", "item_media", "guild_roster",
                  "playable_class_index")


class BudgetExhausted(RuntimeError):
    """Raised instead of sending a request past the per-run cap."""


class TokenMintFailed(RuntimeError):
    """Fatal: without a token nothing can be fetched and nothing may be written."""


class UnsafeUrl(RuntimeError):
    """A URL is about to receive credentials, or to be persisted into a committed
    artifact, and is not on the Blizzard allowlist (H1/H2).

    Never fatal for a run by itself: the per-character handlers in run_sync catch
    it and record a fetch_error row, so one poisoned href costs one character."""


# --------------------------------------------------------------------------- #
# Credential hygiene (pattern proven in tools/probe_bnet.py)
# --------------------------------------------------------------------------- #
class Redactor:
    """Credential values stay in memory; this only ever hands out masked text."""

    def __init__(self):
        self.client_id = os.environ.get("BLIZZARD_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip()
        self._values = [v for v in (self.client_id, self.client_secret) if v]

    def register(self, value):
        """H3: add a *derived* credential form to the mask list.

        Value matching alone only catches the raw id and secret. The token mint
        header carries base64(client_id:client_secret), and a token endpoint that
        echoes the request (public in Actions logs!) would print a string holding
        neither raw value. Every derived form this script builds is registered
        here at the moment it is constructed."""
        if isinstance(value, str) and value and value not in self._values:
            self._values.append(value)
        return value

    def note_token(self, token):
        if token:
            self.register(token)

    def mask(self, text):
        if not isinstance(text, str):
            return text
        for value in self._values:
            if value:
                text = text.replace(value, "[REDACTED]")
        text = re.sub(r"(Authorization\s*:\s*)(?:Bearer|Basic)\s+\S+", r"\1[REDACTED]", text, flags=re.I)
        text = re.sub(r"(access_token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]", text, flags=re.I)
        text = re.sub(r"(client_secret['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]", text, flags=re.I)
        return text


REDACTOR = Redactor()


def redact_foreign_urls(text):
    """H2: withhold URLs that are not ours from every printed and persisted string.

    The refusal notes are the leak path: recording "refused https://attacker/x?beacon"
    verbatim would put an attacker URL into battle-net-map.json, which is public and
    which later tooling scrapes for URLs."""
    if not isinstance(text, str):
        return text

    def keep(match):
        url = match.group(0)
        return url if any(p.match(url) for p in PUBLISHED_URL_RES) else "[non-Blizzard URL withheld]"

    return FOREIGN_URL_RE.sub(keep, text)


def redact(value):
    if isinstance(value, str):
        return redact_foreign_urls(REDACTOR.mask(value))
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Name / URL helpers (the frozen §2/§7.1 contract)
# --------------------------------------------------------------------------- #
def norm_slug(name):
    """Amendment 1 canonical form: lowercase, spaces to hyphens."""
    return "-".join(str(name).lower().split())


def lookup_candidates(name):
    """Ordered direct-GET forms for one roster name; the first HTTP 200 wins.

    Routes are lowercase-only and a capital letter 404s exactly like a missing
    character (notes §2), so the lowercase hyphenated form comes first and the
    legacy spellings are only fallbacks; the matched form is recorded per id.
    """
    out, seen = [], set()

    def add(form, value):
        if value and value not in seen:
            seen.add(value)
            out.append({"form": form, "value": value})

    if " " in str(name):
        tokens = str(name).split()
        add("lower_hyphen", "-".join(tokens).lower())
        add("hyphen", "-".join(tokens))
        add("lower_removed", "".join(tokens).lower())
        add("space_encoded", str(name))
    else:
        add("lower", str(name).lower())
        add("as_is", str(name))
    return out


def character_url(name_or_slug):
    return (f"{API_BASE}/profile/wow/character/"
            f"{urllib.parse.quote(str(name_or_slug), safe='')}?namespace={NS_PROFILE}")


def set_ns(url, namespace):
    """Rewrite the namespace query parameter instead of string-building (notes §5)."""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["namespace"] = namespace
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, doseq=True)))


def allowed_api_url(url):
    """True only for an https request to one of our two Blizzard API origins."""
    return isinstance(url, str) and bool(ALLOWED_API_ORIGIN_RE.match(url))


def require_allowed_url(url, what="request"):
    """H1 choke-point assertion: raise rather than send credentials off-allowlist."""
    if not allowed_api_url(url):
        raise UnsafeUrl(f"URL allowlist refused the {what}: {redact(str(url))[:120]}")
    return url


def safe_artifact_url(value, kind):
    """H2: every URL written into battle-net-map.json / character-data.json goes
    through this shared validator (kind "href" or "iconUrl").

    Returning None (never the raw string) is what keeps a poisoned response href
    out of a committed, publicly served artifact that the next nightly would
    happily re-fetch with a Bearer token attached."""
    pattern = ARTIFACT_URL_RES.get(kind)
    if pattern is None:
        raise ValueError(f"no URL validator registered for artifact field {kind!r}")
    return value if isinstance(value, str) and pattern.match(value) else None


def canonical_href(payload):
    """Forever nests the canonical self URL under _links.self rather than href (§3.1).

    H2: a candidate must match the frozen realmless profile URL shape — the old
    bare http-prefix test accepted any attacker string and persisted it, turning
    one poisoned payload into a permanent exfiltration beacon. None means "caller
    falls back to the URL we constructed from the roster name"."""
    if not isinstance(payload, dict):
        return None
    for candidate in (payload.get("href"),
                      payload.get("_links", {}).get("self", {}).get("href")
                      if isinstance(payload.get("_links"), dict) else None):
        if isinstance(candidate, str) and PROFILE_HREF_RE.match(candidate):
            return candidate
    return None


def slug_from_href(href):
    match = re.search(r"/profile/wow/character/([^/?]+)", href or "")
    return urllib.parse.unquote(match.group(1)) if match else None


def loc(value):
    """Locale maps are inline with no indirection — read .en_US (notes §7.9)."""
    if isinstance(value, dict):
        if isinstance(value.get("en_US"), str):
            return value["en_US"]
        for v in value.values():
            if isinstance(v, str):
                return v
        return None
    return value if isinstance(value, str) else None


def compact(d):
    """Drop keys the API did not send; omitting beats stubbing (task 2.5)."""
    return {k: v for k, v in d.items() if v is not None}


def member_norm_key(member):
    """Normalized identity of a guild-roster member (notes §7.2 field list)."""
    if not isinstance(member, dict):
        return None
    ch = member.get("character")
    if not isinstance(ch, dict):
        return None
    name = ch.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    surname = ch.get("surname")
    if isinstance(surname, str) and surname.strip():
        return f"{name.strip().lower()}-{surname.strip().lower()}"
    return name.strip().lower()


def extract_icon(media_payload):
    """assets[].key == "icon" -> assets[].value (notes §6); None when absent."""
    if not isinstance(media_payload, dict):
        return None
    for asset in media_payload.get("assets") or []:
        if isinstance(asset, dict) and asset.get("key") == "icon" and isinstance(asset.get("value"), str):
            return asset["value"]
    return None


def retry_delay(headers, attempt):
    """Retry-After may be seconds or an HTTP-date; exponential backoff otherwise."""
    raw = {str(k).lower(): v for k, v in (headers or {}).items()}.get("retry-after")
    if raw:
        try:
            return max(1.0, min(float(raw), 120.0))
        except ValueError:
            pass
    return float(2 ** (attempt - 1))


# --------------------------------------------------------------------------- #
# Cache (amendment 3: no ETags exist anywhere — Last-Modified is the only
# conditional-GET mechanism, so the state file stores Last-Modified per URL)
# --------------------------------------------------------------------------- #
class Cache:
    def __init__(self, directory, dry_run):
        self.path = Path(directory) / "bnet-cache.json"
        self.dry_run = dry_run
        self.urls = {}                # url -> {lastModified, body, fetchedAt}
        self.schema_revisions = {}    # surface -> "<schema>/<revision>"

    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt cache is not worth failing a nightly over; cold-start instead.
            print(f"  ! cache unreadable ({type(exc).__name__}); starting cold: {redact(str(exc))[:160]}")
            return
        if isinstance(raw, dict) and isinstance(raw.get("urls"), dict):
            self.urls = {u: v for u, v in raw["urls"].items()
                         if isinstance(v, dict) and "body" in v}
        if isinstance(raw, dict) and isinstance(raw.get("schemaRevisions"), dict):
            self.schema_revisions = dict(raw["schemaRevisions"])

    def entry(self, url):
        return self.urls.get(url)

    def body(self, url):
        e = self.urls.get(url)
        return e.get("body") if isinstance(e, dict) else None

    def store(self, url, last_modified, body):
        self.urls[url] = {"lastModified": last_modified, "body": body, "fetchedAt": iso_now()}

    def save(self):
        if self.dry_run:
            return
        if len(self.urls) > MAX_CACHE_URLS:
            keep = sorted(self.urls.items(), key=lambda kv: kv[1].get("fetchedAt", ""), reverse=True)
            self.urls = dict(keep[:MAX_CACHE_URLS])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cacheVersion": CACHE_VERSION, "schemaRevisions": self.schema_revisions, "urls": self.urls}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(redact(payload), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """H1: refuse to follow redirects on credentialed traffic.

    CPython replays the Authorization header of the original request when it
    auto-follows a 3xx, including to a different authority, so one poisoned
    Location header would ship the bearer token to that host. Returning None from
    redirect_request makes urllib raise HTTPError instead, which _send reports as
    the 3xx status itself: callers already treat a non-2xx/304 as fetch_error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Built once: defaults minus redirect following. Used for every request, so there
# is no code path where a 3xx can carry credentials onward.
NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body

    @property
    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError:
            return None


class Client:
    def __init__(self, cache, force):
        self.cache = cache
        # --force ignores cached timestamps entirely: no conditional headers and
        # no cached-body reuse, but fresh responses still land in the cache.
        self.force = force
        self.token = None
        self.requests_made = 0
        self.tokens_minted = 0
        self.revalidations = 0
        self.status_counts = Counter()

    def mint_token(self, reason):
        if self.tokens_minted >= MAX_TOKEN_MINTS:
            raise TokenMintFailed(f"token mint cap ({MAX_TOKEN_MINTS}) reached ({reason})")
        self.requests_made += 1
        # H1: the token endpoint is a credentialed send path too — never send the
        # client pair anywhere but the frozen https origin.
        require_allowed_url(TOKEN_URL, "token mint")
        pair = f"{REDACTOR.client_id}:{REDACTOR.client_secret}"
        basic = base64.b64encode(pair.encode()).decode()
        # H3: register the derived forms *before* the header can be echoed by the
        # endpoint, or value matching would miss them in the printed failure.
        REDACTOR.register(basic)
        REDACTOR.register(f"Basic {basic}")
        REDACTOR.register(pair)
        REDACTOR.register(urllib.parse.quote(pair, safe=""))
        for raw in (REDACTOR.client_id, REDACTOR.client_secret):
            REDACTOR.register(urllib.parse.quote(raw, safe=""))
            REDACTOR.register(urllib.parse.quote_plus(raw))
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        status, _headers, raw, error = self._send(request)
        payload = None
        if status == 200:
            try:
                payload = json.loads((raw or b"").decode("utf-8"))
            except ValueError as exc:
                raise TokenMintFailed(f"token endpoint returned non-JSON on 200 ({reason}): "
                                      f"{type(exc).__name__}") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            detail = error or (raw or b"").decode("utf-8", errors="replace")[:200]
            raise TokenMintFailed(f"token mint failed with HTTP {status} ({reason}): {redact(detail)}")
        self.token = payload["access_token"]
        REDACTOR.note_token(self.token)
        self.tokens_minted += 1
        return self.token

    def get(self, url):
        """Conditional GET. Returns 200 (fresh body) or 304 with the cached body
        already attached, so callers read .json the same way either way.

        H1: this is the single credentialed GET path, so it is also the single
        allowlist choke point — every href this script ever copies out of a
        response (profile self links, equipment/specializations/achievements links,
        guild-roster key hrefs, item media hrefs) must pass this line before a
        bearer token is attached. UnsafeUrl is caught per character upstream and
        recorded as a fetch_error, so a poisoned payload cannot exfiltrate and
        cannot abort the run."""
        require_allowed_url(url, "GET")
        if self.requests_made >= MAX_REQUESTS:
            raise BudgetExhausted(f"request cap ({MAX_REQUESTS}) reached")
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token is None:
            self.mint_token("initial")
        headers["Authorization"] = f"Bearer {self.token}"
        if not self.force:
            ce = self.cache.entry(url)
            if isinstance(ce, dict) and ce.get("lastModified"):
                headers["If-Modified-Since"] = ce["lastModified"]
        reminted = False
        retries = 0
        while True:
            request = urllib.request.Request(url, method="GET", headers=dict(headers))
            self.requests_made += 1
            status, hdrs, raw, error = self._send(request)
            time.sleep(REQUEST_SPACING_S)
            if error is not None:
                return Resp(None, None, None)
            if status == 429 and retries < MAX_429_RETRIES:
                retries += 1
                time.sleep(retry_delay(hdrs, retries))
                continue
            if status == 401 and not reminted:
                # A mid-run 401 means the 24 h token lapsed: re-mint once, replay once.
                reminted = True
                self.token = None
                self.mint_token("401 refresh")
                headers["Authorization"] = f"Bearer {self.token}"
                continue
            break
        if status is not None:
            self.status_counts[str(status)] += 1
        lowered = {str(k).lower(): v for k, v in (hdrs or {}).items()}
        if status == 304:
            self.revalidations += 1
            ce = self.cache.entry(url)
            body = json.dumps(ce["body"]).encode("utf-8") if isinstance(ce, dict) and ce.get("body") is not None else b""
            if lowered.get("last-modified") and isinstance(ce, dict):
                ce["lastModified"] = lowered["last-modified"]
            return Resp(304, lowered, body)
        if status == 200:
            try:
                parsed = json.loads((raw or b"").decode("utf-8"))
            except ValueError:
                parsed = None
            if parsed is not None:
                self.cache.store(url, lowered.get("last-modified"), parsed)
        return Resp(status, lowered, raw)

    def _send(self, request):
        """Return (status, headers, body, error); an HTTPError status is a result.

        H1: goes through NO_REDIRECT_OPENER, so a 3xx is a result rather than a
        hop — CPython's default opener replays the Authorization header to the
        redirect target, including across a different authority."""
        try:
            with NO_REDIRECT_OPENER.open(request, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.status, dict(resp.headers), resp.read(), None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception as read_exc:
                raw = b""
                print(f"  ! error body unreadable: {type(read_exc).__name__}: {redact(str(read_exc))[:160]}")
            return exc.code, dict(exc.headers or {}), raw, None
        except urllib.error.URLError as exc:
            return None, {}, b"", f"URLError: {redact(str(exc.reason))}"
        except (TimeoutError, OSError) as exc:
            return None, {}, b"", f"{type(exc).__name__}: {redact(str(exc))}"


# --------------------------------------------------------------------------- #
# Map entry builders
# --------------------------------------------------------------------------- #
def map_entry(rec, status, *, slug=None, href=None, bnet_name=None, surname=None,
              matched_form=None, mapped_via=None, note=None):
    """Fixed field set so a reviewer can diff two runs line by line.

    Every map row is written through here, so this is the H2 write site for hrefs:
    anything that is not a Blizzard realmless profile URL is stored as null."""
    return {
        "rosterName": rec["name"],
        "status": status,
        "slug": slug,
        "href": safe_artifact_url(href, "href") if href is not None else None,
        "bnetName": bnet_name,
        "surname": surname,
        "matchedForm": matched_form,
        "mappedVia": mapped_via,
        "note": note,
    }


def ok_entry(rec, *, slug, href, payload, matched_form, mapped_via):
    # name/surname come from the payload, never the roster guess (task 2.3).
    return map_entry(rec, "ok", slug=slug, href=href,
                     bnet_name=payload.get("name") if isinstance(payload.get("name"), str) else None,
                     surname=payload.get("surname") if isinstance(payload.get("surname"), str) else None,
                     matched_form=matched_form, mapped_via=mapped_via)


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
class Sync:
    def __init__(self, args):
        self.args = args
        self.cache = Cache(args.state_dir, args.dry_run)
        self.cache.load()
        self.rev_prev = dict(self.cache.schema_revisions)
        self.client = Client(self.cache, args.force)
        self.static_ns = None
        self._guild_cache = {}
        self._class_names = None
        self.guild_fetch_failures = set()
        self.icon_urls = {}          # item media URL -> icon URL (in-run dedup)
        self.icon_failures = 0
        self.drift = []
        self._drift_seen = set()
        self.warnings = []
        self.skipped = Counter()
        self.saved_requests = 0
        self.budget_hit = False

    # -- tripwires / helpers ------------------------------------------------ #
    def note_revision(self, surface, headers):
        schema = headers.get("battlenet-schema") if headers else None
        revision = headers.get("battlenet-schema-revision") if headers else None
        if not schema:
            return
        pair = f"{schema}/{revision}"
        previous = self.rev_prev.get(surface)
        self.cache.schema_revisions[surface] = pair
        if previous and previous != pair and ("drift", surface, pair) not in self._drift_seen:
            self._drift_seen.add(("drift", surface, pair))
            self.drift.append(f"SCHEMA REVISION CHANGE on {surface}: was {previous}, now {pair} — "
                              "docs/bnet-api-notes.md field tables need re-probing")

    def capture_static_ns(self, payload):
        if self.static_ns:
            return
        match = STATIC_NS_RE.search(json.dumps(payload))
        if match:
            self.static_ns = match.group(1)

    def sub_payload(self, w, surface, url):
        """Sub-resource payload with the shared-timestamp reuse rule.

        notes §7.3: every document of one character carries the same
        Last-Modified value, so a 304 on the profile means the sub-resources
        cannot have changed either — their cached bodies are reused instead of
        burning a request each.
        """
        if w.get("unchanged"):
            body = self.cache.body(url)
            if body is not None:
                self.saved_requests += 1
                self.skipped["subresources"] += 1
                return body
        resp = self.client.get(url)
        if resp.status in (200, 304):
            self.note_revision(surface, resp.headers)
            return resp.json
        return None

    def icon_for(self, media_url, w):
        if media_url in self.icon_urls:
            return self.icon_urls[media_url]
        if w.get("unchanged"):
            body = self.cache.body(media_url)
            if body is not None:
                # H2: the cache can hold a value cached before this validator
                # existed, so the reuse path validates too.
                icon = safe_artifact_url(extract_icon(body), "iconUrl")
                self.icon_urls[media_url] = icon
                self.saved_requests += 1
                self.skipped["icons"] += 1
                return icon
        resp = self.client.get(media_url)
        icon = None
        if resp.status in (200, 304):
            self.note_revision("item_media", resp.headers)
            # H2: an asset value is response data; only the Blizzard render host is
            # publishable, anything else counts as a failed icon and stores null.
            icon = safe_artifact_url(extract_icon(resp.json), "iconUrl")
        if icon is None:
            # notes §8: a single reference-doc failure is explicitly non-fatal;
            # the gear row survives with iconUrl null.
            self.icon_failures += 1
        self.icon_urls[media_url] = icon
        return icon

    def guild_members(self, guild_slug):
        """One roster fetch per guild slug per run (cached 10800 s server-side)."""
        if guild_slug in self._guild_cache:
            return self._guild_cache[guild_slug]
        url = (f"{API_BASE}{GUILD_ROSTER_PATH.format(slug=urllib.parse.quote(guild_slug, safe=''))}"
               f"?namespace={NS_PROFILE}")
        resp = self.client.get(url)
        members = None
        if resp.status in (200, 304):
            self.note_revision("guild_roster", resp.headers)
            payload = resp.json
            if isinstance(payload, dict) and isinstance(payload.get("members"), list):
                members = payload["members"]
        self._guild_cache[guild_slug] = members
        return members

    def class_names(self):
        """id -> name from the static ns; dynamic class index is dead (amendment 5)."""
        if self._class_names is not None:
            return self._class_names
        self._class_names = {}
        if not self.static_ns:
            return self._class_names
        url = f"{API_BASE}{PLAYABLE_CLASS_INDEX}?namespace={self.static_ns}"
        resp = self.client.get(url)
        if resp.status in (200, 304):
            self.note_revision("playable_class_index", resp.headers)
            payload = resp.json or {}
            for cls in payload.get("classes") or []:
                if isinstance(cls, dict) and cls.get("id") is not None:
                    self._class_names[cls["id"]] = loc(cls.get("name"))
        return self._class_names

    def member_class_matches(self, rec, member):
        """Class is only ever a tiebreak — the roster class is known to be wrong
        sometimes (notes §8: morc theorc listed Shaman, API says Hunter), so a
        mismatch can only prune an already-ambiguous set, never override a
        unique name match."""
        names = self.class_names()
        if not names:
            return False
        ch = member.get("character") if isinstance(member.get("character"), dict) else {}
        member_class = ch.get("playable_class")
        member_class_id = member_class.get("id") if isinstance(member_class, dict) else None
        roster_class = str(rec.get("class") or "").casefold()
        api_class = names.get(member_class_id)
        return bool(api_class) and api_class.casefold() == roster_class

    # -- mapping passes ------------------------------------------------------ #
    def resolve(self, rec, w, prev):
        """Tier 1 direct GET ladder; leaves w["entry"] None for the guild lever."""
        if (isinstance(prev, dict) and prev.get("status") == "ok"
                and isinstance(prev.get("href"), str) and allowed_api_url(prev["href"])):
            # H2: a href persisted by an older, more trusting run is still response
            # data. It is re-fetched only while it points at a Blizzard origin; a
            # poisoned value drops to the constructed-URL ladder below instead.
            prev_slug = slug_from_href(prev["href"])
            if prev_slug and prev_slug == norm_slug(rec["name"]):
                resp = self.client.get(prev["href"])
                if resp.status in (200, 304) and resp.json is not None:
                    payload = resp.json
                    self.note_revision("profile", resp.headers)
                    self.capture_static_ns(payload)
                    w["profile"] = payload
                    w["profile_url"] = prev["href"]
                    w["unchanged"] = resp.status == 304
                    w["entry"] = ok_entry(rec, slug=prev_slug, href=prev["href"], payload=payload,
                                          matched_form=prev.get("matchedForm") or "lower_hyphen",
                                          mapped_via=prev.get("mappedVia") or "direct")
                    # An already-mapped id needs one revalidating GET instead of
                    # the whole candidate ladder — that is most of run 2's saving.
                    saved = max(0, len(lookup_candidates(rec["name"])) - 1)
                    self.saved_requests += saved
                    self.skipped["lookups"] += saved
                    return
                self.warnings.append(f"id {rec['id']} was ok last run but got HTTP {resp.status}; re-resolving")
        for cand in lookup_candidates(rec["name"]):
            url = character_url(cand["value"])
            resp = self.client.get(url)
            if resp.status == 304 and resp.json is None:
                # Revalidation hit but the cached body was pruned: probe on.
                continue
            if resp.status in (200, 304):
                # A 304 IS a hit: the document exists and the cached body is current.
                payload = resp.json
                if payload is None:
                    w["entry"] = map_entry(rec, "fetch_error",
                                           note=f"200 with unparseable body on form {cand['form']}")
                    return
                href = canonical_href(payload) or url
                self.note_revision("profile", resp.headers)
                self.capture_static_ns(payload)
                w["profile"] = payload
                w["profile_url"] = url
                w["entry"] = ok_entry(rec, slug=slug_from_href(href) or slug_from_href(url) or norm_slug(rec["name"]),
                                      href=href, payload=payload, matched_form=cand["form"], mapped_via="direct")
                return
            if resp.status is None:
                w["entry"] = map_entry(rec, "fetch_error", note="transport error (no HTTP status) during lookup")
                return
            if resp.status != 404:
                w["entry"] = map_entry(rec, "fetch_error", note=f"HTTP {resp.status} on lookup form {cand['form']}")
                return
            # 404: routed-and-not-found or unrouted; the ladder tries the next form.

    def guild_lever(self, work, ordered_ids):
        """Tier 2 (amendments 4): resolve remaining ids through guild rosters
        discovered from already-resolved characters. One-directional: guild
        members who are not in the tracker are ignored, never added."""
        unresolved = [work[rid] for rid in ordered_ids if work[rid]["entry"] is None]
        if not unresolved:
            return
        guild_slugs = sorted({
            w["profile"]["guild"]["slug"]
            for w in work.values()
            if isinstance(w.get("profile"), dict)
            and isinstance(w["profile"].get("guild"), dict)
            and isinstance(w["profile"]["guild"].get("slug"), str)
        })
        if not guild_slugs:
            return
        claims = {}
        for guild_slug in guild_slugs:
            members = self.guild_members(guild_slug)
            if members is None:
                self.guild_fetch_failures.add(guild_slug)
                continue
            for member in members:
                key = member_norm_key(member)
                if key:
                    claims.setdefault(key, []).append((guild_slug, member))
        by_norm = {}
        for w in unresolved:
            by_norm.setdefault(norm_slug(w["rec"]["name"]), []).append(w)
        for norm, ws in by_norm.items():
            hits = claims.get(norm, [])
            if not hits:
                continue
            if len(ws) != 1:
                for w in ws:
                    w["entry"] = map_entry(w["rec"], "needs_review",
                                           note=f"guild match for {norm} is ambiguous across "
                                                f"roster ids {', '.join(x['rec']['id'] for x in ws)}")
                continue
            w = ws[0]
            chosen = None
            distinct = {m.get("character", {}).get("slug") for _, m in hits}
            if len(distinct) == 1:
                chosen = hits[0][1]
            else:
                filtered = [(g, m) for g, m in hits if self.member_class_matches(w["rec"], m)]
                if len({m.get("character", {}).get("slug") for _, m in filtered}) == 1:
                    chosen = filtered[0][1]
                    self.warnings.append(f"id {w['rec']['id']}: guild match disambiguated by class "
                                         f"among {len(distinct)} members of {filtered[0][0]}")
            if chosen is None:
                involved = ", ".join(sorted({g for g, _ in hits}))
                w["entry"] = map_entry(w["rec"], "needs_review",
                                       note=f"guild roster(s) {involved} are ambiguous for this "
                                            f"name ({len(distinct)} members match)")
                continue
            ch = chosen.get("character") or {}
            key_node = ch.get("key") if isinstance(ch.get("key"), dict) else None
            # H1/H2: a guild-roster member's key.href is untrusted response data.
            # Use it only when it names a Blizzard origin; otherwise build the §7.1
            # URL ourselves from the member slug, and let the choke point be the
            # backstop rather than the only line of defence.
            member_href = key_node.get("href") if isinstance(key_node.get("href"), str) \
                and allowed_api_url(key_node["href"]) else None
            href = set_ns(member_href, NS_PROFILE) if member_href else character_url(ch.get("slug") or norm)
            try:
                resp = self.client.get(href)
            except UnsafeUrl as exc:
                # Per character finding, never an abort of the lever pass.
                w["entry"] = map_entry(w["rec"], "fetch_error",
                                       note=f"guild-roster profile link refused: {redact(str(exc))[:140]}")
                continue
            if resp.status in (200, 304) and resp.json is not None:
                payload = resp.json
                self.note_revision("profile", resp.headers)
                self.capture_static_ns(payload)
                w["profile"] = payload
                w["profile_url"] = href
                self.skipped["lookups"] += 1
                self.saved_requests += 1
                w["entry"] = ok_entry(w["rec"], slug=ch.get("slug") or slug_from_href(canonical_href(payload) or href),
                                      href=canonical_href(payload) or href, payload=payload,
                                      matched_form="guild_roster", mapped_via="guild_roster")
            elif resp.status is None or resp.status not in (200, 404):
                w["entry"] = map_entry(w["rec"], "fetch_error",
                                       note=f"HTTP {resp.status} fetching the profile found in the guild roster")
            # a 404 here leaves it unresolved; it ends as not_found below.

    def dedupe_slugs(self, work, ordered_ids):
        """Two roster ids resolving to the same Battle.net slug must both become
        needs_review — collapsing them into one ok row is the failure mode the
        id-keyed map exists to prevent (notes §2)."""
        by_slug = {}
        for rid in ordered_ids:
            e = work[rid]["entry"]
            if e and e["status"] == "ok" and e.get("slug"):
                by_slug.setdefault(e["slug"], []).append(rid)
        for slug, ids in by_slug.items():
            if len(ids) < 2:
                continue
            for rid in ids:
                w = work[rid]
                others = ", ".join(i for i in ids if i != rid)
                w["entry"] = map_entry(w["rec"], "needs_review",
                                       note=f"slug {slug} also claimed by id {others}; "
                                            "never auto-pick between them")
                # Identity is unsafe here: keep the sidecar row minimal instead of
                # publishing enriched data under an ambiguous mapping.
                w["profile"] = None
                self.warnings.append(f"slug collision {slug}: ids {', '.join(ids)} demoted to needs_review")

    # -- enrichment ----------------------------------------------------------- #
    def enrich(self, w):
        entry, profile = w["entry"], w["profile"]
        href = entry["href"]
        href_base = href.split("?")[0]

        def link(key, path):
            node = profile.get(key)
            base = node.get("href") if isinstance(node, dict) and isinstance(node.get("href"), str) \
                else f"{href_base}/{path}"
            return set_ns(base, NS_PROFILE)

        static_ns = self.static_ns
        if not static_ns:
            match = STATIC_NS_RE.search(json.dumps(profile))
            if match:
                self.static_ns = static_ns = match.group(1)

        out = {"status": "ok"}
        bnet = {}
        if isinstance(profile.get("name"), str):
            bnet["name"] = profile["name"]
        if isinstance(profile.get("surname"), str):
            bnet["surname"] = profile["surname"]
        # H2 write site: the sidecar is served to every visitor; no off-allowlist
        # href is ever published, whatever the payload claimed.
        bnet["href"] = safe_artifact_url(href, "href")
        out["bnet"] = bnet
        if isinstance(profile.get("level"), int):
            out["level"] = profile["level"]
        guild = profile.get("guild")
        if isinstance(guild, dict):
            guild_name = loc(guild.get("name"))
            if guild_name:
                out["guild"] = guild_name
        spec_payload = self.sub_payload(w, "specializations", link("specializations", "specializations"))
        if isinstance(spec_payload, dict):
            for group in spec_payload.get("specialization_groups") or []:
                if not (isinstance(group, dict) and group.get("is_active") is True):
                    continue
                for spec in group.get("specializations") or []:
                    spec_name = loc(spec.get("specialization_name")) if isinstance(spec, dict) else None
                    if spec_name:
                        out["activeSpec"] = spec_name
                        break
                if "activeSpec" in out:
                    break
        if isinstance(profile.get("equipped_item_level"), int):
            out["itemLevel"] = profile["equipped_item_level"]
        equipment = self.sub_payload(w, "equipment", link("equipment", "equipment"))
        gear = []
        if isinstance(equipment, dict):
            for item in equipment.get("equipped_items") or []:
                if not isinstance(item, dict):
                    continue
                slot_node = item.get("slot") if isinstance(item.get("slot"), dict) else {}
                level_node = item.get("level") if isinstance(item.get("level"), dict) else {}
                icon = None
                media = item.get("media") if isinstance(item.get("media"), dict) else {}
                media_key = media.get("key") if isinstance(media.get("key"), dict) else {}
                if isinstance(media_key.get("href"), str) and static_ns:
                    icon = self.icon_for(set_ns(media_key["href"], static_ns), w)
                gear.append({
                    "slot": loc(slot_node.get("name")),
                    "name": loc(item.get("name")),
                    "ilvl": level_node.get("value") if isinstance(level_node.get("value"), int) else None,
                    "iconUrl": safe_artifact_url(icon, "iconUrl"),
                })
        out["gear"] = gear
        achievements = self.sub_payload(w, "achievements", link("achievements", "achievements"))
        if isinstance(achievements, dict) and isinstance(achievements.get("total_points"), int):
            out["achievementPoints"] = achievements["total_points"]
        elif isinstance(profile.get("achievement_points"), int):
            out["achievementPoints"] = profile["achievement_points"]
        # /statistics carries no sidecar field (notes §3.5 is combat stats); the
        # GET exists so the schema drift tripwire covers the surface (amendment 7).
        self.sub_payload(w, "statistics", link("statistics", "statistics"))
        pvp = self.sub_payload(w, "pvp_summary", link("pvp_summary", "pvp-summary"))
        if isinstance(pvp, dict):
            kills = pvp.get("honorable_kills") if isinstance(pvp.get("honorable_kills"), int) else None
            rank = pvp.get("pvp_rank") if isinstance(pvp.get("pvp_rank"), int) else None
            # progress keys appear only where data exists (notes §4: raid/M+ carry
            # no entries for any sampled character, so those keys stay omitted).
            if (kills or 0) > 0 or (rank or 0) > 0:
                out["progress"] = {"pvp": {"honorableKills": kills or 0, "pvpRank": rank or 0}}
        fetched_at = iso_now()
        if w.get("unchanged"):
            ce = self.cache.entry(w["profile_url"])
            if isinstance(ce, dict) and isinstance(ce.get("fetchedAt"), str):
                fetched_at = ce["fetchedAt"]
        out["fetchedAt"] = fetched_at
        return out

    # -- run summary ----------------------------------------------------------- #
    def summary_lines(self, statuses, total, runtime_s, extra_lines=None):
        lines = [
            "=== bnet sync summary ===",
            f"roster entries: {total}",
            "statuses: " + (" ".join(f"{k}={statuses.get(k, 0)}"
                                     for k in ("ok", "not_found", "needs_review", "fetch_error")) or "none"),
            (f"requests: {self.client.requests_made} (cap {MAX_REQUESTS}) | 304 revalidations: "
             f"{self.client.revalidations} | skipped via cache/map: {self.saved_requests} "
             f"(lookups {self.skipped['lookups']}, subresources {self.skipped['subresources']}, "
             f"icons {self.skipped['icons']})"),
            f"tokens minted: {self.client.tokens_minted} | http statuses: "
            + " ".join(f"{k}:{v}" for k, v in sorted(self.client.status_counts.items())),
            f"runtime: {runtime_s:.1f}s",
        ]
        if self.static_ns:
            lines.append(f"static namespace discovered: {self.static_ns}")
        lines.append("schema revisions: " + ", ".join(
            f"{s}={self.cache.schema_revisions[s]}" for s in DRIFT_SURFACES
            if s in self.cache.schema_revisions))
        lines.extend(f"DRIFT {line}" for line in self.drift)
        if self.icon_failures:
            lines.append(f"WARNING: {self.icon_failures} item icon fetch(es) failed; those gear rows carry iconUrl null")
        if self.guild_fetch_failures:
            lines.append(f"WARNING: guild roster fetch failed for: {', '.join(sorted(self.guild_fetch_failures))}")
        if self.budget_hit:
            lines.append(f"WARNING: request cap ({MAX_REQUESTS}) reached; remaining work skipped this run")
        if statuses.get("fetch_error"):
            lines.append(f"WARNING: partial failure — {statuses['fetch_error']} character(s) ended fetch_error; "
                         "statuses written per character")
        lines.extend(extra_lines or [])
        return lines

    def emit_summary(self, lines, markdown_heading="## Battle.net sync summary"):
        for line in lines:
            print(redact(line))
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if not step_summary:
            return
        markdown = [markdown_heading, ""] + [f"- {redact(line)}" for line in lines]
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write("\n".join(markdown) + "\n")
        except OSError as exc:
            print(f"  ! could not append to GITHUB_STEP_SUMMARY: {type(exc).__name__}")

    # -- full sync ------------------------------------------------------------- #
    def run_sync(self, roster, prev_entries):
        started = time.monotonic()
        ordered_ids = [rec["id"] for rec in roster]
        roster_ids = set(ordered_ids)
        pruned = sorted(i for i in prev_entries if i not in roster_ids)
        collision_ids = {}
        by_norm = {}
        for rec in roster:
            by_norm.setdefault(norm_slug(rec["name"]), []).append(rec["id"])
        for norm, ids in by_norm.items():
            if len(ids) > 1:
                for rid in ids:
                    collision_ids[rid] = ids

        work = {}
        for rid in ordered_ids:
            work[rid] = {"rec": next(r for r in roster if r["id"] == rid),
                         "entry": None, "profile": None, "profile_url": None, "unchanged": False}

        for rid in ordered_ids:
            w = work[rid]
            if self.budget_hit:
                w["entry"] = map_entry(w["rec"], "fetch_error",
                                       note="not attempted: request cap reached earlier in this run")
                continue
            if rid in collision_ids:
                others = ", ".join(i for i in collision_ids[rid] if i != rid)
                w["entry"] = map_entry(w["rec"], "needs_review",
                                       note=f"duplicate normalization with id {others}")
                continue
            try:
                self.resolve(w["rec"], w, prev_entries.get(rid))
            except BudgetExhausted as exc:
                self.budget_hit = True
                w["entry"] = map_entry(w["rec"], "fetch_error", note=str(exc))
            except Exception as exc:
                # 2.8: one character crashing is a finding, never an abort.
                w["entry"] = map_entry(w["rec"], "fetch_error",
                                       note=f"unexpected {type(exc).__name__}: {redact(str(exc))[:160]}")

        try:
            self.guild_lever(work, ordered_ids)
        except BudgetExhausted as exc:
            self.budget_hit = True
            self.warnings.append(f"guild-lever pass stopped: {redact(str(exc))}")
        except UnsafeUrl as exc:
            # H1 net: ids the lever did not resolve fall through to not_found
            # below; the run still writes what it legitimately learned.
            self.warnings.append(f"guild-lever pass stopped by the URL allowlist: {redact(str(exc))}")

        for rid in ordered_ids:
            w = work[rid]
            if w["entry"] is None:
                note = "404 on all forms — likely not opted in (task 0.4)"
                if self.guild_fetch_failures:
                    note += f"; guild roster fetch failed for {', '.join(sorted(self.guild_fetch_failures))}"
                w["entry"] = map_entry(w["rec"], "not_found", note=note)

        self.dedupe_slugs(work, ordered_ids)

        characters_out = {}
        for rid in ordered_ids:
            w = work[rid]
            entry = w["entry"]
            if entry["status"] != "ok":
                characters_out[rid] = {"status": entry["status"], "fetchedAt": iso_now()}
                continue
            if self.budget_hit:
                entry.update({"status": "fetch_error", "note": "enrichment skipped: request cap reached"})
                characters_out[rid] = {"status": "fetch_error", "fetchedAt": iso_now()}
                continue
            try:
                characters_out[rid] = self.enrich(w)
            except BudgetExhausted as exc:
                self.budget_hit = True
                entry.update({"status": "fetch_error", "note": str(exc)})
                characters_out[rid] = {"status": "fetch_error", "fetchedAt": iso_now()}
            except Exception as exc:
                w["entry"].update({"status": "fetch_error",
                                   "note": f"enrichment crashed: {type(exc).__name__}: {redact(str(exc))[:160]}"})
                characters_out[rid] = {"status": "fetch_error", "fetchedAt": iso_now()}
                self.warnings.append(f"enrichment crashed for id {rid}: {type(exc).__name__}")

        entries = {rid: work[rid]["entry"] for rid in ordered_ids}
        statuses = Counter(e["status"] for e in entries.values())

        # 2.8 failure policy: no token ever obtained (broken credentials) or every
        # character failed -> nonzero exit, previous artifacts left untouched.
        if self.client.tokens_minted == 0 and self.client.requests_made > 0:
            print("FATAL: no request ever obtained a token (credentials?); "
                  "previous artifacts left untouched")
            return 1
        if statuses["fetch_error"] == len(ordered_ids):
            print("FATAL: every character failed to fetch; previous artifacts left untouched")
            for rid in ordered_ids[:5]:
                print(f"  {rid}: {redact(work[rid]['entry']['note'] or '')}")
            return 1

        # H4 mass-flip floor. Why this exists: a Blizzard-wide 404 storm is a live
        # upstream bug (amendment 8: EU Forever character endpoints 404ing since
        # Nov 6, and US can follow), and a 404 is the *legitimate* not_found path —
        # exit 0, no fetch_error anywhere. The 2.8 all-fail guard above only looks
        # at fetch_error, so before this floor one bad upstream night silently
        # unpublished every enriched row: the site went grey and the committed map
        # lost four good identities. Statuses are only overwritten when the run is
        # at least half as healthy as what is already published; below that the
        # previous artifacts are the more truthful document, so we keep them and
        # fail the job loudly instead of writing.
        ok_prev = sum(1 for rid in ordered_ids
                      if isinstance(prev_entries.get(rid), dict)
                      and prev_entries[rid].get("status") == "ok")
        ok_now = statuses.get("ok", 0)
        if ok_prev >= 3 and ok_now * 2 < ok_prev:
            fatal = (f"FATAL: mass status flip — {ok_prev} ok rows published before this run, "
                     f"{ok_now} ok now (floor: refuse below half of the previous ok count). "
                     "battle-net-map.json and character-data.json were NOT written; suspect "
                     "upstream 404 instability (amendment 8), not a real roster change.")
            self.emit_summary(self.summary_lines(statuses, len(ordered_ids),
                                                 time.monotonic() - started, [fatal]))
            return 1

        if not self.args.dry_run:
            write_json(MAP_FILE, {"schemaVersion": 1, "updatedAt": iso_now(), "entries": entries})
            write_json(DATA_FILE, {"schemaVersion": 1, "syncedAt": iso_now(), "characters": characters_out})
        self.cache.save()

        extra = []
        if pruned:
            extra.append(f"pruned stale map rows (roster ids no longer present): {', '.join(pruned)}")
        if self.args.dry_run:
            extra.append("dry-run: battle-net-map.json and character-data.json were NOT written")
        self.emit_summary(self.summary_lines(statuses, len(ordered_ids), time.monotonic() - started, extra))
        return 0

    # -- lookup mode ------------------------------------------------------------- #
    def run_lookup(self, name, roster, prev_entries):
        started = time.monotonic()
        norm = norm_slug(name)
        status = None
        payload = None
        href = None
        matched = None
        via = None
        for cand in lookup_candidates(name):
            url = character_url(cand["value"])
            resp = self.client.get(url)
            # 304 carries the cached body: a revalidation is a hit, not a miss.
            if resp.status in (200, 304) and resp.json is not None:
                payload = resp.json
                href = canonical_href(payload) or url
                matched = cand["form"]
                via = "direct"
                self.note_revision("profile", resp.headers)
                self.capture_static_ns(payload)
                status = f"resolved via direct GET (form {cand['form']}, HTTP {resp.status})"
                break
            if resp.status is None or resp.status not in (404, 200, 304):
                status = f"HTTP {resp.status} on form {cand['form']}; stopped the candidate walk"
                break
        if payload is None:
            # Guild-roster lever, seeded from already-mapped profiles because a
            # guild slug is only discoverable from a resolved member (notes §7.2).
            seeds = [prev_entries[rec["id"]]["href"] for rec in roster
                     if isinstance(prev_entries.get(rec["id"]), dict)
                     and prev_entries[rec["id"]].get("status") == "ok"
                     and isinstance(prev_entries[rec["id"]].get("href"), str)
                     # H2: never replay a persisted href that left the allowlist.
                     and allowed_api_url(prev_entries[rec["id"]]["href"])]
            guild_slugs = set()
            for seed_href in seeds[:3]:
                resp = self.client.get(seed_href)
                seed_payload = resp.json
                if isinstance(seed_payload, dict):
                    self.capture_static_ns(seed_payload)
                    guild = seed_payload.get("guild")
                    if isinstance(guild, dict) and isinstance(guild.get("slug"), str):
                        guild_slugs.add(guild["slug"])
            hits = []
            for guild_slug in sorted(guild_slugs):
                for member in self.guild_members(guild_slug) or []:
                    if member_norm_key(member) == norm:
                        hits.append((guild_slug, member))
            distinct = {m.get("character", {}).get("slug") for _, m in hits if isinstance(m.get("character"), dict)}
            if len(distinct) == 1:
                ch = hits[0][1].get("character") or {}
                key_node = ch.get("key") if isinstance(ch.get("key"), dict) else None
                # H1/H2 lookup mode: the roster-supplied key href is only trusted as
                # far as the allowlist; otherwise the URL is constructed here.
                member_href = key_node.get("href") if isinstance(key_node, dict) \
                    and isinstance(key_node.get("href"), str) and allowed_api_url(key_node["href"]) else None
                guess = set_ns(member_href, NS_PROFILE) if member_href else character_url(ch.get("slug") or norm)
                resp = self.client.get(guess)
                if resp.status in (200, 304) and resp.json is not None:
                    payload = resp.json
                    href = canonical_href(payload) or guess
                    matched = "guild_roster"
                    via = "guild_roster"
                    self.note_revision("profile", resp.headers)
                    self.capture_static_ns(payload)
                    status = f"resolved via guild roster {hits[0][0]}"
                else:
                    status = f"guild roster {hits[0][0]} matched, but its profile GET returned HTTP {resp.status}"
            elif len(distinct) > 1:
                status = f"ambiguous: {len(distinct)} guild-roster members match this normalized name"
            elif guild_slugs:
                status = "no match: direct lookup 404ed and no reachable guild roster lists this name"
            else:
                status = "no match, and no already-mapped character exposed a guild roster to search"

        lines = [f"Battle.net lookup: {name} (normalized {norm})"]
        lines.append(f"result: {status}")
        if isinstance(payload, dict):
            lines.append(f"identity: {payload.get('name')} {payload.get('surname') or ''}".rstrip())
            lines.append(f"href: {href}")
            lines.append(f"level: {payload.get('level')}, class id: "
                         f"{(payload.get('character_class') or {}).get('id') if isinstance(payload.get('character_class'), dict) else '?'}, "
                         f"guild: {loc((payload.get('guild') or {}).get('name')) if isinstance(payload.get('guild'), dict) else 'none'}")
            lines.append(f"top-level keys: {', '.join(sorted(payload))}")
        rows = [rec for rec in roster if norm_slug(rec["name"]) == norm]
        if isinstance(payload, dict) and len(rows) == 1 \
                and (prev_entries.get(rows[0]["id"]) or {}).get("status") not in ("ok",):
            entry = ok_entry(rows[0], slug=slug_from_href(href), href=href, payload=payload,
                             matched_form=matched, mapped_via=via)
            if self.args.dry_run:
                lines.append(f"map: would update id {rows[0]['id']} -> ok ({via}) (dry-run, not written)")
            else:
                entries = dict(prev_entries)
                entries[rows[0]["id"]] = entry
                write_json(MAP_FILE, {"schemaVersion": 1, "updatedAt": iso_now(), "entries": entries})
                lines.append(f"map: updated id {rows[0]['id']} -> ok ({via})")
        elif isinstance(payload, dict) and len(rows) > 1:
            lines.append(f"map: unchanged — {len(rows)} roster rows share the normalized name {norm}; "
                         "never auto-pick between them")
        else:
            lines.append("map: unchanged")

        stats = (f"requests: {self.client.requests_made} | 304 revalidations: {self.client.revalidations} | "
                 f"tokens minted: {self.client.tokens_minted} | runtime: {time.monotonic() - started:.1f}s")
        lines.append(stats)
        markdown = ["## Battle.net lookup summary", ""] + [f"- {redact(line)}" for line in lines]
        for line in lines:
            print(redact(line))
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            try:
                with open(step_summary, "a", encoding="utf-8") as handle:
                    handle.write("\n".join(markdown) + "\n")
            except OSError as exc:
                print(f"  ! could not append to GITHUB_STEP_SUMMARY: {type(exc).__name__}")
        if not self.args.dry_run:
            self.cache.save()
        if self.client.tokens_minted == 0:
            print("FATAL: lookup never obtained a token")
            return 1
        return 0


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def write_json(path, payload):
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8")


def load_roster():
    records = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("characters.json must be a non-empty JSON array")
    ids = [rec.get("id") for rec in records]
    if not all(isinstance(i, str) and i for i in ids):
        raise ValueError("every roster record needs a non-empty string id")
    if len(set(ids)) != len(ids):
        raise ValueError("characters.json contains duplicate ids; refusing a run that could not key them")
    for rec in records:
        if not isinstance(rec.get("name"), str) or not rec["name"].strip():
            raise ValueError(f"roster id {rec.get('id')} has no usable name")
    return records


def load_previous_map():
    if not MAP_FILE.exists():
        return {}
    try:
        raw = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! previous battle-net-map.json unreadable ({type(exc).__name__}); rebuilding it whole")
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nightly Battle.net roster sync (Phase 2)")
    parser.add_argument("--lookup", metavar="NAME", default=None,
                        help="maintainer lookup mode for one character name")
    parser.add_argument("--force", action="store_true",
                        help="ignore cached Last-Modified timestamps and refetch")
    parser.add_argument("--dry-run", action="store_true", help="write no files")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                        help=f"cache directory (default {DEFAULT_STATE_DIR})")
    args = parser.parse_args(argv)

    if not REDACTOR.client_id or not REDACTOR.client_secret:
        print("FATAL: BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET must both be set in the "
              "environment; values are never printed", file=sys.stderr)
        return 1
    try:
        roster = load_roster()
    except (OSError, ValueError) as exc:
        print(f"FATAL: cannot load characters.json: {redact(str(exc))}", file=sys.stderr)
        return 1
    prev_entries = load_previous_map()

    sync = Sync(args)
    try:
        if args.lookup:
            return sync.run_lookup(args.lookup, roster, prev_entries)
        return sync.run_sync(roster, prev_entries)
    except TokenMintFailed as exc:
        # Fatal by policy (2.8): no token means no trustworthy data, so the
        # previous artifacts stay exactly as they are.
        print(f"FATAL: {redact(str(exc))} — previous artifacts left untouched", file=sys.stderr)
        return 1
    except UnsafeUrl as exc:
        # H1 net: in sync mode every client.get sits behind a per-character handler,
        # so reaching here means lookup mode met a hostile href. Refuse, write
        # nothing, and never send the credential.
        print(f"FATAL: {redact(str(exc))} — nothing was written", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
