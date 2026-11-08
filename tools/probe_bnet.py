#!/usr/bin/env python3
"""Phase 1 endpoint probe for the Battle.net "WoW: Forever" API.

Freezes the live API contract (URL templates, namespaces, JSON paths, auth
semantics, caching headers) that the Phase 2 sync workflow and the Phase 3 UI
are written against, so that no later phase has to guess a path.

Credentials come from BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET. They are
never printed, logged, or written to the findings file: every string that could
carry one passes through redact() before it is stored or printed.

Usage:
    set -a && . ./.env && set +a && python3 tools/probe_bnet.py [extra_name ...]

Exit codes:
    0  probe completed (a 404 is a finding, not a failure)
    1  could not start or crashed midway; the partial findings file is still written
"""

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
from pathlib import Path

# --------------------------------------------------------------------------- #
# Contract config. Every Blizzard path and namespace lives in this block and
# nowhere else: the game is new, the endpoints are undocumented, and a patch
# can move any of them, so Phase 2+ must have exactly one place to re-verify.
# --------------------------------------------------------------------------- #
TOKEN_URL = "https://us.battle.net/oauth/token"
API_BASE = "https://us.api.blizzard.com"
NS_PROFILE = "profile-forever-us"
NS_DYNAMIC = "dynamic-forever-us"
CHARACTER_PATH = "/profile/wow/character/{name}"
SUB_RESOURCES = ("equipment", "achievements", "statistics", "summary")
DYNAMIC_INDEXES = (
    ("playable_class index", "/data/wow/playable-class/index"),
    ("realm index", "/data/wow/realm/index"),
    ("ruleset index", "/data/wow/ruleset/index"),
    ("region index", "/data/wow/region/index"),
)
CHARACTER_SEARCH_PATH = "/data/wow/character-search/index"
CONTROL_BOGUS_NAME = "zzzz-not-a-real-name-xyz"
CONTROL_UNROUTED_PATH = "/profile/wow/not-routed-xyz-123"
CONTROL_BAD_NAMESPACE = "profile-forever-us-typo"
# Keys that hold resource URLs without ending in `href`; the media/icon form is
# a Phase 3 deliverable, so `avatar_url` must be collected too.
URL_VALUE_KEYS = ("href", "avatar_url", "original_url", "icon_url", "url")

REQUEST_SPACING_S = 0.25      # Blizzard allows 100 req/s; we stay near 1 req/s
REQUEST_BUDGET = 300          # hard stop so a runaway href walk cannot burn quota
HTTP_TIMEOUT_S = 30
MAX_ATTEMPTS_PER_REQUEST = 4
MAX_TOKEN_MINTS = 3           # initial + one 401 refresh + one spare
MAX_LOOKUP_FOLLOWS = 12       # static-ns playable_class / playable_race resolution
SAMPLE_BUDGET = 12            # trimmed sample payloads kept in the raw JSON

USER_AGENT = "wow-forever-roster/1.0 (phase-1 endpoint probe; read-only)"
SECRET_ENV = ("BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET")

# First matching pattern wins, so the narrow rules must precede the broad ones.
KIND_PATTERNS = (
    ("item_media", r"/data/wow/media/item"),
    ("progression", r"/progression"),
    ("mythic_keystone", r"/mythic[-_]?keystone"),
    ("encounters", r"/encounters"),
    ("specializations", r"/specializations"),
    ("achievements_statistics", r"/achievements/statistics"),
    ("pvp", r"/pvp"),
    ("playable_class", r"/playable-class"),
    ("playable_race", r"/playable-race"),
    ("guild", r"/guild"),
    ("realm", r"/realm"),
    ("mount", r"/mount"),
    ("achievement", r"/achievement"),
    # render.* hosts serve icons/models/portraits; gear items each carry their own href
    ("media", r"/(media|icons?|originals|portraits)\b|character-media|render\."),
)
# Discovered hrefs are followed once per URL, up to the per-kind cap: gear
# payloads carry one media href per equipped item, so an uncapped walk burns
# the whole budget on icons.
FOLLOW_CAPS = {
    "progression": 6,
    "mythic_keystone": 6,
    "pvp": 6,
    "encounters": 4,
    "specializations": 2,
    "achievements_statistics": 2,
    "media": 2,
    "item_media": 1,
    # The guild doc links roster/activity/achievements; the roster is a candidate
    # mapping source for Phase 2, so the whole family is worth 4 requests.
    "guild": 4,
}
# The retail `/progression*` route family plus the raid-locks surface: tried once
# per resolved character even when unlinked, because "raid progress does not
# exist for Forever" has to be evidenced by a 404, not by absence of a link.
UNLINKED_SUBRESOURCES = ("progression", "progression-raiders", "encounters/raids", "encounters/dungeons",
                        "mythic-keystone-profile/season-current", "pvp")
# Reference docs that only ever appear as hrefs inside other payloads; Phase 2
# needs to know which namespace each actually resolves in.
REFERENCE_URL_MARKERS = ("/playable-specialization/", "/mythic-keystone/period/", "/data/wow/item/",
                        "/achievement-category/")
MAX_REFERENCE_DOCS = 8
EXTRA_INDEXES = (("mythic_keystone period index", "/data/wow/mythic-keystone/period/index"),)
LOCALE_KEY_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}$")

# Vocabularies used to locate the real JSON path of a datum. The probe reports
# paths it observed in a 200 payload, never paths it assumed.
KEY_FIELD_TERMS = {
    "canonical_name": ("name",),
    "surname": ("surname", "first_name", "family_name"),
    "id": ("id",),
    "level": ("level", "hlevel"),
    "gender": ("gender",),
    "active_spec": ("active_spec_name", "active_spec"),
    "experience": ("experience",),
    "last_login_time": ("last_login_time", "last_played_timestamp"),
    "updated_timestamp": ("last_updated_timestamp", "updated_at", "modified_timestamp", "updated"),
    "guild": ("guild",),
    "self_href": ("href",),
    "average_ilvl": ("average_item_level", "equipped_item_quantity", "ilvl"),
    "achievement_points": ("total_quantity", "total_available_quantity", "total_points"),
    "icon": ("icon",),
}
TIER2_PATH_TERMS = r"raid|boss|keystone|season|rating|bracket|progress|criteria|encounter"

REPO = Path(__file__).resolve().parent.parent
ROSTER_FILE = REPO / "characters.json"
OUT_FILE = REPO / "docs" / "bnet-probe-raw.json"


class BudgetExhausted(RuntimeError):
    """Raised instead of sending a request past the run budget."""


# --------------------------------------------------------------------------- #
# Credential hygiene
# --------------------------------------------------------------------------- #
class Redactor:
    """Credential values stay in memory; this only ever hands out masked text."""

    def __init__(self):
        self.client_id = os.environ.get("BLIZZARD_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip()
        self._values = [v for v in (self.client_id, self.client_secret) if v]

    def note_token(self, token):
        if token:
            self._values.append(token)

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


def redact(value):
    if isinstance(value, str):
        return REDACTOR.mask(value)
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def truncate(text, limit=70):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Response:
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
        body_text = (self.body or b"").decode("utf-8", errors="replace")
        return {
            "url": redact(self.url),
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
            "body_snippet": None if self.status == 200 else truncate(redact(body_text), 200),
            "namespaces_in_body": sorted(set(re.findall(r"namespace=([A-Za-z0-9._\-]+)", body_text))),
            "static_ns_in_body": sorted(set(re.findall(r"namespace=(static-[A-Za-z0-9._\-]+)", body_text))),
            "error": redact(self.error) if self.error else None,
        }


class Http:
    def __init__(self, redactor):
        self.redactor = redactor
        self.token = None
        self.requests_made = 0
        self.tokens_minted = 0
        self.status_counts = Counter()
        self.records = []
        self.mint_log = []
        self.stop_reason = None

    def mint_token(self, reason):
        """client_credentials mint; tokens last 24 h so this should run once per run."""
        if self.tokens_minted >= MAX_TOKEN_MINTS:
            raise BudgetExhausted(f"token mint cap ({MAX_TOKEN_MINTS}) reached ({reason})")
        pair = f"{self.redactor.client_id}:{self.redactor.client_secret}"
        basic = base64.b64encode(pair.encode()).decode()
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
        self.requests_made += 1
        started = time.time()
        status, headers, raw, error = self._send(request)
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
            entry.update(ok=False, error=redact(error or (raw or b"").decode("utf-8", errors="replace"))[:300])
            self.mint_log.append(entry)
            raise BudgetExhausted(f"token mint failed with HTTP {status} ({reason}); check credentials")
        # Only metadata is kept: the access token itself never enters the findings file.
        self.token = payload.get("access_token") or ""
        self.redactor.note_token(self.token)
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
        if self.requests_made >= REQUEST_BUDGET:
            self.stop_reason = f"request budget ({REQUEST_BUDGET}) exhausted"
            raise BudgetExhausted(self.stop_reason)
        attempts = 0
        reminted = False
        status = headers = raw = error = None
        started = time.time()
        while True:
            attempts += 1
            headers_out = {"Accept": "application/json", "User-Agent": USER_AGENT}
            if auth:
                if self.token is None:
                    self.mint_token("initial")
                headers_out["Authorization"] = f"Bearer {self.token}"
            if extra_headers:
                headers_out.update(extra_headers)
            request = urllib.request.Request(url, method="GET", headers=headers_out)
            self.requests_made += 1
            started = time.time()
            status, headers, raw, error = self._send(request)
            time.sleep(REQUEST_SPACING_S)
            if status == 429 and attempts < MAX_ATTEMPTS_PER_REQUEST:
                delay = retry_delay(headers, attempts)
                print(f"  ~ 429 from {truncate(url, 90)}; backing off {delay}s")
                time.sleep(delay)
                continue
            if status == 401 and auth and not reminted and self.tokens_minted < MAX_TOKEN_MINTS:
                # A mid-run 401 means the 24 h token lapsed: re-mint once and replay once.
                reminted = True
                self.token = None
                self.mint_token(f"401 refresh during {category}")
                continue
            break
        response = Response(url, category, status, headers, raw, attempts, time.time() - started, error)
        self.status_counts[str(status)] += 1
        self.records.append(response.record())
        return response

    def _send(self, request):
        """Return (status, headers, body, error); an HTTPError status is a result, not a crash."""
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.status, dict(resp.headers), resp.read(), None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception as read_exc:  # body already drained: keep the status, report why
                raw = b""
                print(f"  ! error body unreadable: {type(read_exc).__name__}: {redact(str(read_exc))}")
            return exc.code, dict(exc.headers or {}), raw, None
        except urllib.error.URLError as exc:
            return None, {}, b"", f"URLError: {redact(str(exc.reason))}"
        except (TimeoutError, OSError) as exc:
            return None, {}, b"", f"{type(exc).__name__}: {redact(str(exc))}"


def retry_delay(headers, attempt):
    raw = {str(k).lower(): v for k, v in (headers or {}).items()}.get("retry-after")
    if raw:
        try:
            return max(1.0, min(float(raw), 30.0))
        except ValueError:
            pass  # Retry-After can be an HTTP-date; fall back to exponential
    return float(2 ** (attempt - 1))


# --------------------------------------------------------------------------- #
# URL and name-shape helpers
# --------------------------------------------------------------------------- #
def set_ns(url, namespace):
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["namespace"] = namespace
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, doseq=True)))


def character_url(name, namespace=NS_PROFILE):
    return f"{API_BASE}{CHARACTER_PATH.format(name=urllib.parse.quote(name, safe=''))}?namespace={namespace}"


def lookup_candidates(name):
    """Ordered URL candidates for one roster name.

    Battle.net character names contain no spaces, so a spaced roster name needs
    a form guess and the hypothesis under test is `{name}-{surname}`. Order
    encodes hypothesis strength because the first HTTP 200 wins.
    """
    out, seen = [], set()

    def add(form, value):
        if value and value not in seen:
            seen.add(value)
            out.append({"form": form, "value": value})

    if " " in name:
        tokens = name.split()
        add("space_to_hyphen", "-".join(tokens))
        add("space_removed", "".join(tokens))
        add("space_encoded", name)
        add("space_to_hyphen_lower", "-".join(tokens).lower())
        add("space_removed_lower", "".join(tokens).lower())
    else:
        add("as_is", name)
        add("lower", name.lower())
    return out


# --------------------------------------------------------------------------- #
# Payload inspection
# --------------------------------------------------------------------------- #
def type_name(node):
    if node is None:
        return "null"
    if isinstance(node, bool):
        return "bool"
    if isinstance(node, int):
        return "int"
    if isinstance(node, float):
        return "float"
    if isinstance(node, str):
        return "str"
    return "unknown"


def is_locale_map(node):
    """Blizzard embeds translations inline: {"en_US": "Neck", "de_DE": ...}.

    Collapsing them keeps an inventory under its leaf cap instead of spending
    12 leaves on every translated label.
    """
    return (isinstance(node, dict) and len(node) >= 3
            and all(LOCALE_KEY_RE.match(str(key)) for key in node))


def path_inventory(node, max_depth=7, max_list_items=2, max_leaves=260):
    """Flatten a payload to {"dotted[0].path": {t: type, v: sample}}.

    Arrays also emit a `[len]` entry, so element shape and array size are both
    recoverable without storing hundreds of achievements.
    """
    inventory = {}

    def walk(current, path, depth):
        if len(inventory) >= max_leaves:
            return
        if is_locale_map(current):
            english = current.get("en_US") or next(iter(current.values()), "")
            inventory[path or "$"] = {
                "t": "localized_string",
                "v": f"{truncate(redact(english), 40)} [read .en_US; {len(current)} locales]",
            }
            return
        if isinstance(current, dict):
            if not current:
                inventory[path or "$"] = {"t": "dict", "v": "{}"}
                return
            if depth >= max_depth:
                inventory[path or "$"] = {"t": "dict", "v": f"<{len(current)} keys: {truncate(', '.join(current), 60)}>"}
                return
            for key, value in current.items():
                walk(value, f"{path}.{key}" if path else str(key), depth + 1)
        elif isinstance(current, list):
            inventory[f"{path}[len]"] = {"t": "int", "v": len(current)}
            for index, value in enumerate(current[:max_list_items]):
                walk(value, f"{path}[{index}]", depth + 1)
        else:
            sample = current if not isinstance(current, str) else truncate(redact(current), 80)
            inventory[path or "$"] = {"t": type_name(current), "v": sample}

    walk(node, "", 0)
    if len(inventory) >= max_leaves:
        inventory["$truncated"] = {"t": "str", "v": f"inventory capped at {max_leaves} leaves"}
    return inventory


def skeleton(node, depth=0, max_depth=4, max_list=3):
    if is_locale_map(node):
        return {"en_US": truncate(redact(node.get("en_US", "")), 50), "_locales": len(node)}
    if isinstance(node, dict):
        if depth >= max_depth:
            return f"<dict {len(node)} keys: {truncate(', '.join(node), 70)}>"
        return {str(k): skeleton(v, depth + 1, max_depth, max_list) for k, v in node.items()}
    if isinstance(node, list):
        items = [skeleton(v, depth + 1, max_depth, max_list) for v in node[:max_list]]
        if len(node) > max_list:
            items.append(f"<+{len(node) - max_list} more of {len(node)}>")
        return items
    if isinstance(node, str):
        return truncate(redact(node), 60)
    return node


def classify(url):
    for kind, pattern in KIND_PATTERNS:
        if re.search(pattern, url, flags=re.I):
            return kind
    return "other"


def discover_links(node, found=None, path=""):
    """Collect every `*href` URL in a payload with the JSON paths it appeared at."""
    if found is None:
        found = {}
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, str) and value.startswith("http") and str(key).lower() in URL_VALUE_KEYS:
                entry = found.setdefault(value, {"kind": classify(value), "json_paths": []})
                if child not in entry["json_paths"]:
                    entry["json_paths"].append(child)
            else:
                discover_links(value, found, child)
    elif isinstance(node, list):
        for index, value in enumerate(node[:8]):
            discover_links(value, found, f"{path}[{index}]")
    return found


def canonical_href(payload):
    """The self URL, which Forever puts under _links.self instead of `href`."""
    if not isinstance(payload, dict):
        return None
    for candidate in (payload.get("href"),
                      payload.get("_links", {}).get("self", {}).get("href") if isinstance(payload.get("_links"), dict) else None):
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None


def slug_from_href(href):
    match = re.search(r"/profile/wow/character/([^/?]+)", href or "")
    return urllib.parse.unquote(match.group(1)) if match else None


def case_verdict(rows):
    """Compress the case-variant probe into the single rule Phase 2 must follow."""
    if not rows:
        return "UNTESTED: no resolved character to re-request"
    per_row = [{v["variant"]: v["status"] for v in row["variants"]} for row in rows]
    lower_only = all(r.get("canonical_self_href") == 200 and r.get("upper") != 200 and r.get("title") != 200
                     for r in per_row)
    if lower_only:
        return ("slug lookup is CASE-SENSITIVE: the lowercase slug returns 200 while the upper/title "
                "variants of the same slug do not, so Phase 2 must lowercase before GETing")
    return f"mixed result, inspect rows: {per_row}"


def find_paths(inventory, terms, max_depth=4):
    """Inventory paths whose final key is in `terms`, array index stripped."""
    hits = []
    for path in inventory:
        if path == "$truncated":
            continue
        if path.count(".") > max_depth:
            continue
        leaf = path.split(".")[-1].split("[")[0]
        if leaf in terms:
            hits.append(path)
    return sorted(hits)


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #
class Probe:
    def __init__(self, extra_names=()):
        self.http = Http(REDACTOR)
        self.names = load_roster_names()
        self.extra_names = [n for n in extra_names if n not in self.names]
        self.started = time.time()
        self.sections = []          # every observed response, for cross-cutting derivation
        self.link_pool = {}         # url -> {kind, json_paths}
        self.followed = set()
        self.follow_counts = Counter()
        self.lookup_follows = 0
        self.static_ns_counts = Counter()
        self.characters = []
        self.name_rows = []
        self.controls = []
        self.indexes = []
        self.static_lookups = []
        self.etag_probe = {}
        self.case_rows = []
        self.warnings = []
        self.samples_written = 0
        self.matrix = Counter()
        self.results = {}

    # -- plumbing ---------------------------------------------------------- #
    def fetch(self, url, category, **kwargs):
        try:
            response = self.http.get(url, category, **kwargs)
        except BudgetExhausted:
            raise
        self.matrix[f"{category} | {response.status}"] += 1
        record = response.record()
        for static_ns in record["static_ns_in_body"]:
            self.static_ns_counts[static_ns] += 1
        return response

    def observe(self, label, url, category, *, inventory_limit=160, sample=False, follow=True, auth=True,
                extra_headers=None):
        response = self.fetch(url, category, auth=auth, extra_headers=extra_headers)
        payload = response.json  # parsed first so a bad-body note lands in the record below
        entry = {"label": label, **response.record()}
        if payload is not None:
            entry["inventory"] = path_inventory(payload, max_leaves=inventory_limit)
            links = discover_links(payload, {})
            entry["link_kinds"] = dict(Counter(v["kind"] for v in links.values()))
            entry["links"] = {
                redact(u): {"kind": v["kind"], "json_paths": v["json_paths"][:3]} for u, v in sorted(links.items())
            }
            for url_key, meta in links.items():
                pooled = self.link_pool.setdefault(url_key, {"kind": meta["kind"], "json_paths": []})
                pooled["json_paths"] = sorted(set(pooled["json_paths"] + meta["json_paths"]))[:4]
            if sample and self.samples_written < SAMPLE_BUDGET:
                entry["sample"] = skeleton(payload)
                self.samples_written += 1
            if follow:
                entry["followed_links"] = self.follow_links(links, category)
        self.sections.append(entry)
        return response, entry

    def follow_links(self, links, source_category):
        """Follow progression/M+/PvP hrefs once each; media hrefs only a few times."""
        followed = []
        for url, meta in sorted(links.items()):
            kind = meta["kind"]
            if url in self.followed:
                continue
            if kind not in FOLLOW_CAPS or self.follow_counts[kind] >= FOLLOW_CAPS[kind]:
                continue
            self.follow_counts[kind] += 1
            self.followed.add(url)
            response, entry = self.observe(f"linked {kind}", url, f"B/{kind}", inventory_limit=120, sample=True)
            entry["source"] = source_category
            followed.append(entry)
        return followed

    # -- phases ------------------------------------------------------------ #
    def run(self):
        try:
            self.phase_controls()
        except BudgetExhausted as exc:
            self.warnings.append(f"controls incomplete: {redact(str(exc))}")
        try:
            self.phase_indexes()
        except BudgetExhausted as exc:
            self.warnings.append(f"indexes incomplete: {redact(str(exc))}")
        self.phase_names()
        try:
            self.phase_case_sensitivity()
        except BudgetExhausted as exc:
            self.warnings.append(f"case-sensitivity probe incomplete: {redact(str(exc))}")
        try:
            self.phase_static_lookups()
        except BudgetExhausted as exc:
            self.warnings.append(f"static-ns lookups incomplete: {redact(str(exc))}")
        try:
            self.phase_etag()
        except BudgetExhausted as exc:
            self.warnings.append(f"etag revalidation skipped: {redact(str(exc))}")
        return self.build()

    def phase_controls(self):
        """Re-verify 401-vs-404 routed/not-routed semantics before trusting any 404."""
        specs = (
            ("bogus character name, valid token", character_url(CONTROL_BOGUS_NAME), {}, True),
            ("unrouted path, valid token", f"{API_BASE}{CONTROL_UNROUTED_PATH}?namespace={NS_PROFILE}", {}, True),
            ("routed path, NO token", character_url("shiftybeef"), {}, False),
            ("routed path, bogus token", character_url("shiftybeef"), {"Authorization": "Bearer not-a-real-token"}, False),
        )
        for label, url, headers, auth in specs:
            try:
                _, entry = self.observe(label, url, "E/control", inventory_limit=40, follow=False, auth=auth,
                                        extra_headers=headers or None)
            except BudgetExhausted:
                return
            self.controls.append(entry)
        # An unknown namespace must be observed with a bearer token attached by hand:
        # going through the auth path would let a 401 here trigger the re-mint retry and
        # mask the raw status this control exists to record.
        if self.http.token:
            try:
                _, entry = self.observe(
                    "routed path, unknown namespace",
                    character_url("shiftybeef", CONTROL_BAD_NAMESPACE),
                    "E/control", inventory_limit=40, follow=False, auth=False,
                    extra_headers={"Authorization": f"Bearer {self.http.token}"})
            except BudgetExhausted:
                return
            self.controls.append(entry)

    def phase_indexes(self):
        for label, path in DYNAMIC_INDEXES:
            try:
                _, entry = self.observe(label, f"{API_BASE}{path}?namespace={NS_DYNAMIC}", "C/dynamic-index",
                                        inventory_limit=120, follow=False)
            except BudgetExhausted:
                break
            self.indexes.append(entry)
        for label, path in EXTRA_INDEXES:
            try:
                _, entry = self.observe(label, f"{API_BASE}{path}?namespace={NS_DYNAMIC}", "C/extra-index",
                                       inventory_limit=60, follow=False)
            except BudgetExhausted:
                return
            self.indexes.append(entry)
        try:
            _, entry = self.observe("character-search index (re-confirm 404)",
                                    f"{API_BASE}{CHARACTER_SEARCH_PATH}?namespace={NS_PROFILE}",
                                    "C/character-search", inventory_limit=40, follow=False)
        except BudgetExhausted:
            return
        self.indexes.append(entry)

    def phase_names(self):
        stop = False
        for name in self.names + self.extra_names:
            if stop:
                self.name_rows.append({"roster_name": name, "single_token": " " not in name, "attempts": [],
                                       "status": "skipped_budget", "resolved_via_form": None})
                continue
            row = {"roster_name": name, "single_token": " " not in name, "attempts": [],
                   "resolved_via_form": None, "status": None}
            for candidate in lookup_candidates(name):
                url = character_url(candidate["value"])
                try:
                    response = self.fetch(url, "A/profile")
                except BudgetExhausted as exc:
                    self.warnings.append(f"budget stop while probing {name}: {redact(str(exc))}")
                    stop = True
                    break
                payload = response.json if response.status == 200 else None
                attempt = {"form": candidate["form"], "candidate": candidate["value"],
                           "encoded_url": redact(url), "status": response.status,
                           "etag": response.headers.get("etag")}
                if payload is not None:
                    attempt["canonical_name_in_payload"] = payload.get("name")
                    row["attempts"].append(attempt)
                    row["resolved_via_form"] = candidate["form"]
                    row["status"] = 200
                    self.probe_character(name, payload, candidate, response)
                    break
                row["attempts"].append(attempt)
            if row["status"] is None:
                row["status"] = row["attempts"][-1]["status"] if row["attempts"] else None
            self.name_rows.append(row)

    def probe_character(self, roster_name, payload, candidate, response):
        links = discover_links(payload, {})
        inventory = path_inventory(payload, max_leaves=200)
        char = {
            "roster_name": roster_name,
            "matched_form": candidate["form"],
            "matched_candidate": candidate["value"],
            "request_url": redact(character_url(candidate["value"])),
            "status": response.status,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "cache_control": response.headers.get("cache-control"),
            # Forever nests the canonical URL under _links.self rather than `href`.
            "self_href": canonical_href(payload),
            "slug": slug_from_href(canonical_href(payload) or ""),
            "top_level_keys": {str(k): type_name(v) for k, v in payload.items()},
            "profile_inventory": inventory,
            "field_paths": {},
            "link_kinds": dict(Counter(v["kind"] for v in links.values())),
            "profile_links": {redact(u): {"kind": v["kind"], "json_paths": v["json_paths"][:3]}
                              for u, v in sorted(links.items())},
            "sub_endpoints": [],
        }
        for field, terms in KEY_FIELD_TERMS.items():
            paths = find_paths(inventory, terms)
            if paths:
                char["field_paths"][field] = {p: inventory[p] for p in paths[:6]}

        if self.samples_written < SAMPLE_BUDGET:
            char["profile_sample"] = skeleton(payload)
            self.samples_written += 1

        try:
            char["profile_linked_followed"] = self.follow_links(links, f"A/profile[{roster_name}]")
        except BudgetExhausted as exc:
            self.warnings.append(f"link follow stopped at {roster_name}: {redact(str(exc))}")

        base = (char["self_href"] or character_url(candidate["value"])).split("?")[0]
        for resource in UNLINKED_SUBRESOURCES:
            try:
                # `U/` keeps these apart from the kind-labelled `B/` follows: the
                # `/pvp` route and the `/pvp-summary` link would otherwise share a row.
                _, entry = self.observe(f"{roster_name}: {resource} (unlinked)",
                                       f"{base}/{resource}?namespace={NS_PROFILE}",
                                       f"U/{resource}", inventory_limit=60, follow=False)
            except BudgetExhausted as exc:
                self.warnings.append(f"unlinked probe stopped at {roster_name}/{resource}: {redact(str(exc))}")
                break
            entry["href_linked_in_profile"] = False
            char["sub_endpoints"].append(entry)
        for resource in SUB_RESOURCES:
            url = f"{base}/{resource}?namespace={NS_PROFILE}"
            linked = any(re.search(rf"/{resource}$", u.split("?")[0]) for u in links)
            try:
                _, entry = self.observe(f"{roster_name}: {resource}", url, f"B/{resource}",
                                        inventory_limit=160, sample=resource == "equipment")
            except BudgetExhausted as exc:
                self.warnings.append(f"sub-resource probe stopped at {roster_name}/{resource}: {redact(str(exc))}")
                break
            entry["href_linked_in_profile"] = linked
            char["sub_endpoints"].append(entry)
        self.characters.append(char)

    def phase_case_sensitivity(self):
        """Freeze the normalization rule Phase 2 must apply to a roster name.

        The name phase can show a mixed-case slug 404 while its lowercase form
        returns 200; re-hitting one resolved slug in three cases separates a
        genuinely case-sensitive route from a transient 404.
        """
        rows = []
        for char in self.characters:
            slug = char.get("slug")
            if not slug:
                continue
            record = {"roster_name": char["roster_name"], "canonical_slug": slug, "variants": []}
            for label, variant in (("canonical_self_href", None), ("upper", slug.upper()), ("title", slug.title())):
                url = char["self_href"] if variant is None else character_url(variant)
                try:
                    response = self.fetch(url, "F/case-variant")
                except BudgetExhausted:
                    break
                payload = response.json if response.status == 200 else None
                record["variants"].append({"variant": label, "slug": variant or slug, "status": response.status,
                                           "canonical_name_in_payload": payload.get("name") if payload else None})
            rows.append(record)
        self.case_rows = rows

    def phase_static_lookups(self):
        """Re-discover the version-pinned static ns from hrefs, then prove lookups there."""
        candidates = self.static_ns_counts.most_common()
        chosen = candidates[0][0] if candidates else None
        if not chosen:
            # Unpinned `static-forever-us` is reported as 403 upstream; probing it records
            # whether the route exists, which is the difference between "no href to read"
            # and "static namespace itself is unavailable".
            self.warnings.append("no version-pinned static namespace found in any response body; "
                                 "probing unpinned static-forever-us for evidence instead")
            try:
                _, entry = self.observe("unpinned static-forever-us class index",
                                       f"{API_BASE}/data/wow/playable-class/index?namespace=static-forever-us",
                                       "D/static-unpinned", inventory_limit=40, follow=False)
                self.static_lookups.append(entry)
            except BudgetExhausted as exc:
                self.warnings.append(f"unpinned static probe skipped: {redact(str(exc))}")
            return
        for label, path in (("playable_class index", "/data/wow/playable-class/index"),
                            ("playable_race index", "/data/wow/playable-race/index")):
            try:
                _, entry = self.observe(f"{label} @{chosen}", f"{API_BASE}{path}?namespace={chosen}",
                                        "D/static-index", inventory_limit=60, follow=False)
            except BudgetExhausted:
                break
            self.static_lookups.append(entry)
        self.phase_reference_docs(chosen)
        wanted = self.pick_lookup_targets()
        docs = []
        for kind, url in wanted:
            if self.lookup_follows >= MAX_LOOKUP_FOLLOWS:
                break
            self.lookup_follows += 1
            probe_url = set_ns(url, chosen)
            try:
                response, entry = self.observe(f"{kind} doc @{chosen}", probe_url, f"D/{kind}",
                                               inventory_limit=80, follow=False)
            except BudgetExhausted:
                break
            docs.append((kind, response, entry))
            self.static_lookups.append(entry)
            payload = response.json
            if response.status != 200 or not isinstance(payload, dict):
                continue
            localized = self.find_localized_name_hrefs(payload)
            for href in localized:
                if self.lookup_follows >= MAX_LOOKUP_FOLLOWS:
                    break
                self.lookup_follows += 1
                try:
                    lresponse, lentry = self.observe(f"{kind} localized name", href, "D/localized-name",
                                                     inventory_limit=30, follow=False)
                except BudgetExhausted:
                    break
                lpayload = lresponse.json
                entry["localized_name"] = {
                    "href": redact(href),
                    "status": lresponse.status,
                    "value": lpayload.get("name") if isinstance(lpayload, dict) else None,
                }
                docs.append((kind, lresponse, lentry))
                self.static_lookups.append(lentry)

    def phase_reference_docs(self, static_ns):
        """Re-resolve href-only references (specialization, item, keystone period,
        achievement category) in the discovered static namespace.

        A 200 profile hands out a keystone period href pointing at
        dynamic-forever-us that 404s there, so "which namespace does this live
        in" cannot be answered from the href alone.
        """
        picked, per_marker = [], {}
        for url in sorted(self.link_pool):
            marker = next((m for m in REFERENCE_URL_MARKERS if m in url), None)
            if not marker or per_marker.get(marker, 0) >= 2:
                continue
            per_marker[marker] = per_marker.get(marker, 0) + 1
            picked.append(url)
        for url in picked[:MAX_REFERENCE_DOCS]:
            try:
                _, entry = self.observe(f"reference doc @{static_ns}", set_ns(url, static_ns),
                                       "H/reference-static", inventory_limit=50, follow=False)
            except BudgetExhausted:
                break
            self.static_lookups.append(entry)

    def pick_lookup_targets(self):
        """Distinct playable_class / playable_race hrefs, preferring ones our roster hit."""
        targets, seen = [], set()
        for char in self.characters:
            for url, meta in sorted((char.get("profile_links") or {}).items()):
                if meta["kind"] in ("playable_class", "playable_race") and url not in seen:
                    seen.add(url)
                    targets.append((meta["kind"], url))
        if len(targets) >= 2:
            return targets
        for url, meta in sorted(self.link_pool.items()):
            if meta["kind"] in ("playable_class", "playable_race") and url not in seen:
                seen.add(url)
                targets.append((meta["kind"], url))
        prefer_class = [t for t in targets if t[0] == "playable_class"]
        prefer_race = [t for t in targets if t[0] == "playable_race"]
        return (prefer_class[:4] + prefer_race[:4])

    def find_localized_name_hrefs(self, payload):
        """`name` fields that are objects carrying an href (Blizzard localized-string docs)."""
        found = []

        def walk(node):
            if len(found) >= 2:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in ("name", "gender_name", "class") and isinstance(value, dict):
                        href = value.get("href")
                        if isinstance(href, str) and href.startswith("http"):
                            found.append(href)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for value in node[:4]:
                    walk(value)

        walk(payload)
        return found

    def phase_etag(self):
        """Phase 2 only saves quota if conditional GETs work; prove both flavours."""
        probes = []
        etag_target = next((c for c in self.characters if c.get("etag")), None) \
            or next((s for s in self.sections if s.get("etag")), None)
        if etag_target:
            url = set_ns(etag_target.get("request_url") or etag_target.get("url"), NS_PROFILE)
            try:
                response = self.fetch(url, "G/etag-revalidate", extra_headers={"If-None-Match": etag_target["etag"]})
                probes.append({"kind": "If-None-Match", "source": etag_target.get("label", "character profile"),
                               "url": redact(url), "sent_value": etag_target["etag"], "status": response.status,
                               "body_bytes": len(response.body or b""), "supported": response.status == 304})
            except BudgetExhausted as exc:
                self.warnings.append(f"If-None-Match probe skipped: {redact(str(exc))}")
        else:
            probes.append({"kind": "If-None-Match", "attempted": False,
                           "reason": "no response in this run carried an ETag header"})
        lm_target = next((c for c in self.characters if c.get("last_modified")), None) \
            or next((s for s in self.sections if s.get("last_modified")), None)
        if lm_target:
            url = set_ns(lm_target.get("request_url") or lm_target.get("url"), NS_PROFILE)
            try:
                response = self.fetch(url, "G/modified-since-revalidate",
                                      extra_headers={"If-Modified-Since": lm_target["last_modified"]})
                probes.append({"kind": "If-Modified-Since", "source": lm_target.get("label", "character profile"),
                               "url": redact(url), "sent_value": lm_target["last_modified"],
                               "status": response.status, "body_bytes": len(response.body or b""),
                               "supported": response.status == 304})
            except BudgetExhausted as exc:
                self.warnings.append(f"If-Modified-Since probe skipped: {redact(str(exc))}")
        else:
            probes.append({"kind": "If-Modified-Since", "attempted": False,
                           "reason": "no response in this run carried a Last-Modified header"})
        self.etag_probe = {"probes": probes,
                           "conditional_requests_supported": any(p.get("supported") for p in probes)}

    # -- conclusions ------------------------------------------------------- #
    def build(self):
        resolved = [r for r in self.name_rows if r["status"] == 200]
        spaced = [r for r in self.name_rows if not r["single_token"]]
        spaced_ok = [r for r in spaced if r["status"] == 200]
        by_form = Counter(r["resolved_via_form"] for r in resolved)
        hyphen_first_try = [r["roster_name"] for r in spaced_ok
                            if r["attempts"] and r["attempts"][0]["form"] == "space_to_hyphen"]
        surname_rows = []
        for char in self.characters:
            inventory = char["profile_inventory"]
            surname_paths = find_paths(inventory, set(KEY_FIELD_TERMS["surname"]))
            surname_rows.append({
                "roster_name": char["roster_name"],
                "payload_name": next((inventory[p]["v"] for p in find_paths(inventory, {"name"}, max_depth=0)), None),
                "surname_field_present": bool(surname_paths),
                "surname_paths": {p: inventory[p] for p in surname_paths},
                "self_href": char["self_href"],
            })
        if spaced_ok and all(r["resolved_via_form"].startswith("space_to_hyphen") for r in spaced_ok):
            verdict = "CONFIRMED: {name}-{surname} (space -> hyphen) resolved every spaced name that resolved at all"
        elif spaced_ok:
            verdict = f"PARTIAL: spaced names resolved via {dict(Counter(r['resolved_via_form'] for r in spaced_ok))}"
        elif spaced:
            verdict = "UNTESTED: no spaced roster name returned 200, so the hyphen hypothesis is neither confirmed nor refuted"
        else:
            verdict = "NOT APPLICABLE: roster has no spaced names"

        tier2 = {}
        for entry in self.sections:
            category = str(entry.get("category", ""))
            kind = None
            for candidate in ("achievements_statistics", "progression", "mythic_keystone", "encounters",
                              "specializations", "item_media", "guild", "pvp", "statistics", "achievements"):
                if candidate in category or candidate in str(entry.get("label", "")).lower():
                    kind = candidate
                    break
            if kind is None:
                continue
            evidence = tier2.setdefault(kind, {"endpoints": [], "statuses": Counter(), "matching_paths": []})
            evidence["statuses"][str(entry["status"])] += 1
            evidence["endpoints"].append({"url": entry["url"], "status": entry["status"],
                                          "label": entry["label"]})
            for path, meta in (entry.get("inventory") or {}).items():
                if re.search(TIER2_PATH_TERMS, path, flags=re.I):
                    evidence["matching_paths"].append(f"{path} = {truncate(meta['v'], 60)}")
            evidence["matching_paths"] = sorted(set(evidence["matching_paths"]))[:80]
        for evidence in tier2.values():
            evidence["statuses"] = dict(evidence["statuses"])
        tier2_verdicts = {}
        for kind, evidence in tier2.items():
            if any(e["status"] == 200 for e in evidence["endpoints"]):
                tier2_verdicts[kind] = "present (at least one 200)"
            elif any(e["status"] in (401, 404) for e in evidence["endpoints"]):
                tier2_verdicts[kind] = "no data returned (401/404 only)"
            else:
                tier2_verdicts[kind] = "inconclusive (transport error)"
        if not tier2:
            tier2_verdicts["none_observed"] = "no Tier-2 endpoint was reachable: no character profile resolved"

        media_urls, media_hosts = [], Counter()
        for entry in self.sections + self.characters:
            for url, meta in (entry.get("links") or entry.get("profile_links") or {}).items():
                if meta.get("kind") in ("media", "item_media") or "render." in url:
                    media_urls.append(url)
                    if "://" in url:
                        media_hosts[url.split("/")[2]] += 1
            if str(entry.get("category", "")).endswith("media") and entry.get("status") == 200:
                media_urls.append(entry["url"])
                for path, meta in (entry.get("inventory") or {}).items():
                    if meta.get("t") == "str" and str(meta.get("v", "")).startswith("http"):
                        media_urls.append(str(meta["v"]))
                        if "render." in str(meta["v"]):
                            media_hosts[str(meta["v"]).split("/")[2]] += 1
        etag_records = [r for r in self.http.records if r["status"] == 200]
        return {
            "meta": {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "script": "tools/probe_bnet.py",
                "python": sys.version.split()[0],
                "api_base": API_BASE,
                "token_url": TOKEN_URL,
                "namespaces": {"profile": NS_PROFILE, "dynamic": NS_DYNAMIC},
                "roster_source": str(ROSTER_FILE.relative_to(REPO)),
                "roster_names_probed": len(self.name_rows),
                "requests_made": self.http.requests_made,
                "request_budget": REQUEST_BUDGET,
                "tokens_minted": self.http.tokens_minted,
                "http_status_counts": dict(self.http.status_counts),
                "duration_s": round(time.time() - self.started, 1),
                "stop_reason": self.http.stop_reason,
                "credential_note": "client_id/client_secret/access_token are never written to this file; "
                                   "all strings pass through redact()",
            },
            "auth": {
                "token_url": TOKEN_URL,
                "grant_type": "client_credentials",
                "credential_env_vars": list(SECRET_ENV),
                "mints": self.http.mint_log,
                "control_semantics": {c["label"]: c["status"] for c in self.controls},
                "controls": self.controls,
            },
            "name_resolution": {
                "hypothesis_under_test": "{name}-{surname} (space -> hyphen) is the canonical lookup form",
                "candidate_order": [c["form"] for c in lookup_candidates("Two Words")],
                "counts": {
                    "roster_names": len(self.name_rows),
                    "spaced_names": len(spaced),
                    "resolved_200": len(resolved),
                    "not_found": len([r for r in self.name_rows if r["status"] == 404]),
                    "other": len([r for r in self.name_rows if r["status"] not in (200, 404)]),
                },
                "resolved_by_form": dict(by_form),
                "spaced_names_resolved": [r["roster_name"] for r in spaced_ok],
                "spaced_names_resolved_on_first_try_hyphen": hyphen_first_try,
                "surname_fields": surname_rows,
                "verdict": verdict,
                "results": self.name_rows,
            },
            "characters": self.characters,
            "indexes": self.indexes,
            "static_ns": {
                "observed_current": self.static_ns_counts.most_common(1)[0][0] if self.static_ns_counts else None,
                "candidates_seen": dict(self.static_ns_counts),
                "discovery_method": "regex namespace=(static-...) over every response body and href; "
                                    "never hardcoded in Phase 2 either",
                "lookups": self.static_lookups,
            },
            "tier2": {"verdicts": tier2_verdicts, "evidence": tier2},
            "case_sensitivity": {
                "question": "is the profile slug case-sensitive, and is the lowercase form stable?",
                "results": self.case_rows,
                "verdict": case_verdict(self.case_rows),
            },
            "media": {
                "hosts_seen": dict(media_hosts),
                "example_urls": sorted(set(media_urls))[:8],
                "followed": dict(self.follow_counts),
                "follow_caps": dict(FOLLOW_CAPS),
                "note": "media hrefs are collected from every payload but only a capped number are fetched, "
                        "because each gear item carries its own",
            },
            "caching": dict(
                self.etag_probe,
                responses_total=len(self.http.records),
                responses_200=len(etag_records),
                responses_200_with_etag=sum(1 for r in etag_records if r["etag"]),
                responses_200_with_last_modified=sum(1 for r in etag_records if r["last_modified"]),
                observed_header_names=sorted({name for r in self.http.records for name in r.get("header_names", [])}),
            ),
            "endpoint_matrix": [
                {"endpoint": key.split(" | ")[0], "status": key.split(" | ")[1], "count": count}
                for key, count in sorted(self.matrix.items())
            ],
            "request_log": self.http.records,
            "warnings": self.warnings,
        }

    def summary(self):
        results = self.results
        meta = results["meta"]
        names = results["name_resolution"]
        lines = [
            "",
            "=" * 78,
            "Battle.net WoW: Forever - Phase 1 endpoint probe",
            "=" * 78,
            f"requests {meta['requests_made']}/{meta['request_budget']}   "
            f"tokens minted {meta['tokens_minted']}   duration {meta['duration_s']}s   "
            f"stop: {meta['stop_reason'] or 'none'}",
            f"http statuses: {meta['http_status_counts']}",
            "",
            "NAME RESOLUTION",
            "-" * 78,
        ]
        for row in self.name_rows:
            attempts = ", ".join(f"{a['form']}={a['status']}" for a in row["attempts"]) or row["status"]
            lines.append(f"  {row['roster_name']:<24} {str(row['status']):<15} {truncate(attempts, 40)}")
        lines += [
            "",
            f"  resolved {names['counts']['resolved_200']}/{names['counts']['roster_names']} "
            f"(spaced {len(names['spaced_names_resolved'])}/{names['counts']['spaced_names']}), "
            f"by form {names['resolved_by_form']}",
            f"  verdict: {names['verdict']}",
        ]
        if names["counts"]["resolved_200"] == 0:
            lines += [
                "",
                "  !!! LOUD: ZERO of the roster names resolved. Every profile GET returned 404",
                "  !!! (or the run stopped early). That is a valid Phase 1 contract result, but it",
                "  !!! means there is NO payload evidence for any per-character path. Either no",
                "  !!! member opted in / logged in post-launch, or the route changed. Read the",
                "  !!! control rows below before trusting these 404s.",
            ]
        lines += ["", "ENDPOINT MATRIX", "-" * 78]
        for row in results["endpoint_matrix"]:
            lines.append(f"  {row['endpoint']:<32} {row['status']:<6} x{row['count']}")
        lines += ["", "CONTROLS (401 vs 404 routed semantics)", "-" * 78]
        for label, status in results["auth"]["control_semantics"].items():
            lines.append(f"  {label:<34} {status}")
        lines += ["", "TIER 2 (raid / M+ / PvP)", "-" * 78]
        for kind, verdict in results["tier2"]["verdicts"].items():
            evidence = results["tier2"]["evidence"].get(kind)
            detail = f"statuses={evidence['statuses']} paths={len(evidence['matching_paths'])}" if evidence else ""
            lines.append(f"  {kind:<20} {verdict:<34} {detail}")
        lines += ["", "NAMESPACES", "-" * 78,
                  f"  static ns observed: {results['static_ns']['observed_current']}",
                  f"  all static candidates: {results['static_ns']['candidates_seen']}",
                  f"  static-ns lookups: {len(results['static_ns']['lookups'])} "
                  f"({dict(Counter(str(l['status']) for l in results['static_ns']['lookups']))})"]
        cache = results["caching"]
        lines += ["", "CACHING / CONDITIONAL REQUESTS", "-" * 78,
                  f"  200 responses: {cache['responses_200']}, with ETag: {cache['responses_200_with_etag']}, "
                  f"with Last-Modified: {cache['responses_200_with_last_modified']}"]
        for probe in cache.get("probes", []):
            outcome = (f"-> {probe['status']} (supported={probe.get('supported')})"
                       if probe.get("attempted", True) else f"skipped: {probe.get('reason')}")
            lines.append(f"  {probe['kind']:<20} {outcome}")
        lines += ["", "CASE SENSITIVITY", "-" * 78, f"  {results['case_sensitivity']['verdict']}"]
        for row in results["case_sensitivity"]["results"]:
            variants = ", ".join(f"{v['variant']}({v['slug']})={v['status']}" for v in row["variants"])
            lines.append(f"  {row['roster_name']:<20} {variants}")
        lines += ["", "MEDIA", "-" * 78, f"  hosts: {results['media']['hosts_seen']}"]
        for url in results["media"]["example_urls"][:3]:
            lines.append(f"  {truncate(url, 100)}")
        if self.warnings:
            lines += ["", "WARNINGS", "-" * 78]
            for warning in self.warnings:
                lines.append(f"  {warning}")
        lines += ["", f"wrote {OUT_FILE.relative_to(REPO)}", ""]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def load_roster_names():
    if not ROSTER_FILE.is_file():
        sys.exit(f"FATAL: roster file not found at {ROSTER_FILE}")
    try:
        records = json.loads(ROSTER_FILE.read_text())
    except ValueError as exc:
        sys.exit(f"FATAL: {ROSTER_FILE} is not valid JSON: {type(exc).__name__}: {redact(str(exc))}")
    if not isinstance(records, list):
        sys.exit(f"FATAL: {ROSTER_FILE} is not a JSON array")
    names = []
    for record in records:
        name = record.get("name") if isinstance(record, dict) else None
        # Duplicates are kept: characters.json can hold the same character name under
        # two roster ids, and the verifier requires one row per roster entry so that
        # duplicate name -> slug mapping stays visible as Phase 2 needs_review evidence.
        if isinstance(name, str) and name.strip():
            names.append(name)
    if not names:
        sys.exit(f"FATAL: no character names found in {ROSTER_FILE}")
    return names


def main(argv):
    missing = [env for env in SECRET_ENV if not os.environ.get(env, "").strip()]
    if missing:
        sys.exit(
            "FATAL: missing credential env var(s): " + ", ".join(missing)
            + "\n  load them first:  set -a && . ./.env && set +a && python3 tools/probe_bnet.py"
        )
    probe = Probe(argv[1:])
    exit_code = 0
    try:
        probe.results = probe.run()
    except Exception as exc:  # keep the partial findings file; report loudly and fail
        print(f"FATAL: probe aborted with {type(exc).__name__}: {redact(str(exc))}")
        probe.warnings.append(f"aborted: {type(exc).__name__}: {redact(str(exc))}")
        probe.results = probe.build()
        exit_code = 1
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(redact(probe.results), indent=2) + "\n")
    print(probe.summary())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
