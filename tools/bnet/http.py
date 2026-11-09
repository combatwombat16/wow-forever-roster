"""HTTP transport: credentials, redaction, host allowlist, conditional GETs.

Provenance: extracted verbatim (behaviour and comments) from
``tools/sync_bnet.py`` and ``tools/probe_bnet.py`` by the Phase 2
modularization. The security invariants recorded in the security review of
2026-11-08 live here and nowhere else:

* H1  every credentialed send is host-pinned to https://us.battle.net or
  https://us.api.blizzard.com and never follows a redirect
  (``_NoRedirectHandler`` / ``NO_REDIRECT_OPENER``);
* H2  no response-derived URL is persisted without ``safe_artifact_url``, and
  no non-Blizzard URL is ever printed or written (``redact_foreign_urls``);
* H3  every derived credential form (base64 Basic pair, percent-encoded
  variants, the minted token) is registered with the ``Redactor`` before the
  header can be echoed back by an endpoint.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import config


class BudgetExhausted(RuntimeError):
    """Raised instead of sending a request past the per-run cap."""


class TokenMintFailed(RuntimeError):
    """Fatal: without a token nothing can be fetched and nothing may be written."""


class UnsafeUrl(RuntimeError):
    """A URL is about to receive credentials, or to be persisted into a committed
    artifact, and is not on the Blizzard allowlist (H1/H2).

    Never fatal for a run by itself: the per-character handlers in the sync
    orchestrator catch it and record a fetch_error row, so one poisoned href
    costs one character."""


# --------------------------------------------------------------------------- #
# Credential hygiene (pattern proven in the Phase 1 probe)
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
        neither raw value. Every derived form this package builds is registered
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
        return url if any(p.match(url) for p in config.PUBLISHED_URL_RES) else "[non-Blizzard URL withheld]"

    return config.FOREIGN_URL_RE.sub(keep, text)


def redact(value):
    if isinstance(value, str):
        return redact_foreign_urls(REDACTOR.mask(value))
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def mask_secrets(value):
    """Probe-grade redaction: credential masking only, no URL withholding.

    The Phase 1 findings file *is* the URL contract (docs/bnet-probe-raw.json
    records every host the API points at), so the probe keeps the exact
    ``redact()`` it shipped with in Phase 1 while sharing this Redactor."""
    if isinstance(value, str):
        return REDACTOR.mask(value)
    if isinstance(value, dict):
        return {str(k): mask_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# URL trust boundaries
# --------------------------------------------------------------------------- #
def allowed_api_url(url):
    """True only for an https request to one of our two Blizzard API origins."""
    return isinstance(url, str) and bool(config.ALLOWED_API_ORIGIN_RE.match(url))


def require_allowed_url(url, what="request"):
    """H1 choke-point assertion: raise rather than send credentials off-allowlist."""
    if not allowed_api_url(url):
        raise UnsafeUrl(f"URL allowlist refused the {what}: {redact(str(url))[:120]}")
    return url


def allowed_asset_url(url):
    """True only for an https request to the Blizzard render host (Phase B HEADs)."""
    return isinstance(url, str) and bool(config.ASSET_ORIGIN_RE.match(url))


def require_allowed_asset_url(url, what="request"):
    """Host-pinned assertion for the ONE request that carries no credential.

    H1 is about where the bearer token goes, and this path has no token to leak —
    but it is still a socket the sync opens on a URL that came out of a response,
    so it is refused off the asset allowlist before a connection is made."""
    if not allowed_asset_url(url):
        raise UnsafeUrl(f"URL allowlist refused the {what}: {redact(str(url))[:120]}")
    return url


def register_mint_forms(client_id, client_secret):
    """Build the Basic pair for the token mint and register every derived form.

    H3: registration happens *before* the request goes out, because the failure
    path prints whatever the endpoint echoed back. Returns the raw pair and the
    base64 value so the caller can build the header."""
    pair = f"{client_id}:{client_secret}"
    basic = base64.b64encode(pair.encode()).decode()
    REDACTOR.register(basic)
    REDACTOR.register(f"Basic {basic}")
    REDACTOR.register(pair)
    REDACTOR.register(urllib.parse.quote(pair, safe=""))
    REDACTOR.register(urllib.parse.quote_plus(pair))
    for raw in (client_id, client_secret):
        REDACTOR.register(urllib.parse.quote(raw, safe=""))
        REDACTOR.register(urllib.parse.quote_plus(raw))
    return pair, basic


def set_ns(url, namespace):
    """Rewrite the namespace query parameter instead of string-building (notes §5)."""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["namespace"] = namespace
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, doseq=True)))


def retry_delay(headers, attempt, cap=120.0):
    """Retry-After may be seconds or an HTTP-date; exponential backoff otherwise."""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    raw = lowered.get("retry-after")
    if raw:
        try:
            return max(1.0, min(float(raw), cap))
        except ValueError:
            pass
    return float(2 ** (attempt - 1))


# --------------------------------------------------------------------------- #
# No-redirect transport (H1)
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


def send(request):
    """Return (status, headers, body, error); an HTTPError status is a result.

    H1: goes through NO_REDIRECT_OPENER, so a 3xx is a result rather than a
    hop — CPython's default opener replays the Authorization header to the
    redirect target, including across a different authority."""
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=config.HTTP_TIMEOUT_S) as resp:
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
        if len(self.urls) > config.MAX_CACHE_URLS:
            keep = sorted(self.urls.items(), key=lambda kv: kv[1].get("fetchedAt", ""), reverse=True)
            self.urls = dict(keep[:config.MAX_CACHE_URLS])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cacheVersion": config.CACHE_VERSION, "schemaRevisions": self.schema_revisions, "urls": self.urls}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(redact(payload), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


class Client:
    """Conditional-GET client for the nightly sync, plus the uncredentialed HEAD."""

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
        if self.tokens_minted >= config.MAX_TOKEN_MINTS:
            raise TokenMintFailed(f"token mint cap ({config.MAX_TOKEN_MINTS}) reached ({reason})")
        self.requests_made += 1
        # H1: the token endpoint is a credentialed send path too — never send the
        # client pair anywhere but the frozen https origin.
        require_allowed_url(config.TOKEN_URL, "token mint")
        # H3: register the derived forms *before* the header can be echoed by the
        # endpoint, or value matching would miss them in the printed failure.
        _pair, basic = register_mint_forms(REDACTOR.client_id, REDACTOR.client_secret)
        request = urllib.request.Request(
            config.TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": config.USER_AGENT,
            },
        )
        status, _headers, raw, error = send(request)
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
        allowlist choke point — every href this package ever copies out of a
        response (profile self links, equipment/specializations/achievements links,
        guild-roster key hrefs, item media hrefs) must pass this line before a
        bearer token is attached. UnsafeUrl is caught per character upstream and
        recorded as a fetch_error, so a poisoned payload cannot exfiltrate and
        cannot abort the run."""
        require_allowed_url(url, "GET")
        if self.requests_made >= config.MAX_REQUESTS:
            raise BudgetExhausted(f"request cap ({config.MAX_REQUESTS}) reached")
        headers = {"Accept": "application/json", "User-Agent": config.USER_AGENT}
        if self.token is None:
            self.mint_token("initial")
        headers["Authorization"] = f"Bearer {self.token}"
        if not self.force:
            ce = self.cache.entry(url)
            if isinstance(ce, dict) and ce.get("lastModified"):
                headers["If-Modified-Since"] = ce["lastModified"]
        reminted = False
        retries = 0
        attempts = 0
        while True:
            attempts += 1
            if attempts > 1 and self.requests_made >= config.MAX_REQUESTS:
                # The 429 backoff and the 401 remint both replay this request, and a
                # replay is a credentialed send like any other: re-check the per-run
                # cap before each retry so MAX_REQUESTS stays a hard ceiling on total
                # requests rather than a ceiling on first attempts only.
                raise BudgetExhausted(f"request cap ({config.MAX_REQUESTS}) reached before retry "
                                      f"(attempt {attempts})")
            request = urllib.request.Request(url, method="GET", headers=dict(headers))
            self.requests_made += 1
            status, hdrs, raw, error = send(request)
            time.sleep(config.REQUEST_SPACING_S)
            if error is not None:
                return Resp(None, None, None)
            if status == 429 and retries < config.MAX_429_RETRIES:
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

    def head(self, url):
        """HEAD the render host: existence proof for an asset URL (Phase B).

        A first-class send path, not a GET with the body thrown away. It shares every
        transport invariant of ``get()``: the same ``send()`` and therefore the same
        ``NO_REDIRECT_OPENER`` (H1: a 3xx is a status to report, never a hop to take
        — and this is exactly the path where a derived URL is most likely to be
        redirected somewhere the caller never asked for), the same
        ``config.HTTP_TIMEOUT_S`` inside ``send()``, the same ``config.USER_AGENT``,
        the same ``REQUEST_SPACING_S`` pacing and the same ``MAX_REQUESTS`` per-run
        cap (a HEAD is a request; the cap counts requests, not credentialed ones).

        The one deliberate difference is the credential. ``ALLOWED_API_ORIGIN_RE``
        pins the bearer token to the two Blizzard API origins and the render host is
        neither of them, so this request carries NO Authorization header and is
        host-pinned to ``config.ASSET_ORIGIN_RE`` instead — the same boundary the
        browser already crosses to load an icon. No token is minted here, no
        conditional header is sent and nothing is cached: a HEAD answers with no
        body, and amendment 3's Last-Modified reuse is a GET mechanism.

        Returns a ``Resp`` with a ``None`` body so the caller reads ``.status``: 200
        is the only publishable answer, and a transport failure is ``Resp(None, ...)``
        exactly as ``get()`` reports one. ``BudgetExhausted`` propagates like any
        other request past the cap — the run is over either way."""
        require_allowed_asset_url(url, "HEAD")
        if self.requests_made >= config.MAX_REQUESTS:
            raise BudgetExhausted(f"request cap ({config.MAX_REQUESTS}) reached")
        request = urllib.request.Request(
            url, method="HEAD",
            headers={"Accept": "*/*", "User-Agent": config.USER_AGENT})
        self.requests_made += 1
        status, hdrs, _body, error = send(request)
        time.sleep(config.REQUEST_SPACING_S)
        if error is not None:
            return Resp(None, None, None)
        if status is not None:
            self.status_counts[str(status)] += 1
        return Resp(status, {str(k).lower(): v for k, v in (hdrs or {}).items()}, None)


# --------------------------------------------------------------------------- #
# Probe transport (Phase 1 tool, behaviour unchanged except where noted)
# --------------------------------------------------------------------------- #
class ProbeResponse:
    def __init__(self, url, category, status, headers, body, attempts, elapsed, error=None):
        self.url = url
        self.category = category
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.attempts = attempts
        self.elapsed = elapsed
        self.error = error

    @property
    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            # A 200 with a non-JSON body is itself a contract finding, not a crash.
            self.error = self.error or f"non-JSON body: {type(exc).__name__}"
            return None

    def record(self):
        """One row of the findings request log. Field set unchanged from Phase 1."""
        body_text = (self.body or b"").decode("utf-8", errors="replace")
        return {
            "url": mask_secrets(self.url),
            "category": self.category,
            "status": self.status,
            "etag": self.headers.get("etag"),
            "last_modified": self.headers.get("last-modified"),
            "content_type": self.headers.get("content-type"),
            "cache_control": self.headers.get("cache-control"),
            "retry_after": self.headers.get("retry-after"),
            # Blizzard echoes the schema it served; that is the version Phase 2's
            # field paths are pinned to.
            "battlenet_schema": self.headers.get("battlenet-schema"),
            "battlenet_schema_revision": self.headers.get("battlenet-schema-revision"),
            "battlenet_namespace": self.headers.get("battlenet-namespace"),
            "rate_limit_headers": {k: v for k, v in self.headers.items() if "rate" in k},
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed, 3),
            "body_bytes": len(self.body or b""),
            # Header names (values unchanged) document the caching surface: Phase 2
            # can only use conditional GETs for headers Blizzard actually sends.
            "header_names": sorted(self.headers),
            # Error bodies distinguish "character not found" from "namespace unknown"
            # from "route missing", which a bare status code cannot.
            "body_snippet": None if self.status == 200 else truncate(mask_secrets(body_text), 200),
            "namespaces_in_body": sorted(set(re.findall(r"namespace=([A-Za-z0-9._\-]+)", body_text))),
            "static_ns_in_body": sorted(set(re.findall(r"namespace=(static-[A-Za-z0-9._\-]+)", body_text))),
            "error": mask_secrets(self.error) if self.error else None,
        }


class ProbeHttp:
    """Recording GET client used by ``python3 -m bnet probe``.

    Behaviour is the Phase 1 client; the one deliberate change is that the
    control-path credential pair now goes through ``register_mint_forms`` so the
    probe enjoys the same H3 derived-form masking the sync gained."""

    def __init__(self):
        self.token = None
        self.requests_made = 0
        self.tokens_minted = 0
        self.status_counts = Counter()
        self.records = []
        self.mint_log = []
        self.stop_reason = None

    def mint_token(self, reason):
        """client_credentials mint; tokens last 24 h so this should run once per run."""
        if self.tokens_minted >= config.MAX_TOKEN_MINTS:
            raise BudgetExhausted(f"token mint cap ({config.MAX_TOKEN_MINTS}) reached ({reason})")
        # H1: the token endpoint is a credentialed send path too.
        require_allowed_url(config.TOKEN_URL, "token mint")
        # H3: base64 Basic pair + percent-encoded variants join the mask list
        # before the mint request can be echoed.
        _pair, basic = register_mint_forms(REDACTOR.client_id, REDACTOR.client_secret)
        request = urllib.request.Request(
            config.TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": config.PROBE_USER_AGENT,
            },
        )
        self.requests_made += 1
        started = time.time()
        status, headers, raw, error = send(request)
        entry = {
            "reason": reason,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": status,
            "elapsed_s": round(time.time() - started, 3),
        }
        payload = None
        if status == 200:
            try:
                payload = json.loads((raw or b"").decode("utf-8"))
            except ValueError as exc:
                entry.update(ok=False, error=f"unparseable token response: {type(exc).__name__}")
                self.mint_log.append(entry)
                raise BudgetExhausted("token endpoint returned non-JSON on 200; cannot continue") from exc
        if payload is None:
            entry.update(ok=False, error=mask_secrets(error or (raw or b"").decode("utf-8", errors="replace"))[:300])
            self.mint_log.append(entry)
            raise BudgetExhausted(f"token mint failed with HTTP {status} ({reason}); check credentials")
        # Only metadata is kept: the access token itself never enters the findings file.
        self.token = payload.get("access_token") or ""
        REDACTOR.note_token(self.token)
        self.tokens_minted += 1
        entry.update(
            ok=True,
            token_type=payload.get("token_type"),
            expires_in=payload.get("expires_in"),
            scope=payload.get("scope"),
            token_length=len(self.token),
        )
        self.mint_log.append(entry)
        return self.token

    def get(self, url, category, *, auth=True, extra_headers=None):
        if self.requests_made >= config.REQUEST_BUDGET:
            self.stop_reason = f"request budget ({config.REQUEST_BUDGET}) exhausted"
            raise BudgetExhausted(self.stop_reason)
        # H1: the probe follows hrefs out of live payloads, so every send it makes
        # is host-pinned; an off-allowlist href is refused before a token is attached.
        require_allowed_url(url, f"probe GET ({category})")
        attempts = 0
        reminted = False
        status = headers = raw = error = None
        started = time.time()
        while True:
            attempts += 1
            if attempts > 1 and self.requests_made >= config.REQUEST_BUDGET:
                # Same rule as the sync client: a 429 replay or a 401 remint replay is
                # a send, so the budget is re-checked before every retry instead of
                # only before the first attempt. Backoff and remint behaviour otherwise
                # stay exactly as they were.
                self.stop_reason = (f"request budget ({config.REQUEST_BUDGET}) exhausted "
                                    f"before retry (attempt {attempts})")
                raise BudgetExhausted(self.stop_reason)
            headers_out = {"Accept": "application/json", "User-Agent": config.PROBE_USER_AGENT}
            if auth:
                if self.token is None:
                    self.mint_token("initial")
                headers_out["Authorization"] = f"Bearer {self.token}"
            if extra_headers:
                headers_out.update(extra_headers)
            request = urllib.request.Request(url, method="GET", headers=headers_out)
            self.requests_made += 1
            started = time.time()
            status, headers, raw, error = send(request)
            time.sleep(config.REQUEST_SPACING_S)
            if status == 429 and attempts < config.MAX_ATTEMPTS_PER_REQUEST:
                delay = retry_delay(headers, attempts, cap=30.0)
                print(f"  ~ 429 from {truncate(url, 90)}; backing off {delay}s")
                time.sleep(delay)
                continue
            if status == 401 and auth and not reminted and self.tokens_minted < config.MAX_TOKEN_MINTS:
                # A mid-run 401 means the 24 h token lapsed: re-mint once and replay once.
                reminted = True
                self.token = None
                self.mint_token(f"401 refresh during {category}")
                continue
            break
        response = ProbeResponse(url, category, status, headers, raw, attempts, time.time() - started, error)
        self.status_counts[str(status)] += 1
        self.records.append(response.record())
        return response


def truncate(text, limit=70):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
