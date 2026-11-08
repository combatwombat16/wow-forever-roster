"""Single place for every Battle.net contract constant, path and budget.

Provenance: extracted verbatim from the pre-refactor ``tools/sync_bnet.py`` and
``tools/probe_bnet.py`` (Phase 1/2 scripts, both deleted by the Phase 2
modularization in docs/refactor-design.md §5). Nothing else in the package
hardcodes a URL, a namespace or a filesystem path.

The endpoints are undocumented, version-pinned and a game patch can move any of
them, so there must be exactly one place to re-verify them against
docs/bnet-api-notes.md.
"""

import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# Repository layout (docs/refactor-design.md §1): the committed artifacts live
# in data/ from Phase 2 on; this is the only module that knows where they are.
# --------------------------------------------------------------------------- #
REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "data"
ROSTER_FILE = DATA_DIR / "characters.json"
MAP_FILE = DATA_DIR / "battle-net-map.json"
DATA_FILE = DATA_DIR / "character-data.json"
PROBE_OUT_FILE = REPO / "docs" / "bnet-probe-raw.json"
DEFAULT_STATE_DIR = Path.home() / ".cache" / "wow-bnet-sync"

# --------------------------------------------------------------------------- #
# Auth / API contract
# --------------------------------------------------------------------------- #
TOKEN_URL = "https://us.battle.net/oauth/token"
API_BASE = "https://us.api.blizzard.com"
NS_PROFILE = "profile-forever-us"
NS_DYNAMIC = "dynamic-forever-us"
SECRET_ENV = ("BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET")

# --------------------------------------------------------------------------- #
# Sync endpoints (frozen docs/bnet-api-notes.md contract)
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# Sync budgets
# --------------------------------------------------------------------------- #
REQUEST_SPACING_S = 0.25      # client pacing; Blizzard allows 100 req/s (notes §1)
MAX_REQUESTS = 2500           # hard per-run cap; a hit stops the run cleanly
HTTP_TIMEOUT_S = 30
MAX_TOKEN_MINTS = 3           # initial + 401 refresh + one spare; tokens last 24 h
MAX_429_RETRIES = 3
USER_AGENT = "wow-forever-roster/2.0 (nightly roster sync; read-only)"

CACHE_VERSION = 1
MAX_CACHE_URLS = 4000         # LRU prune so months of runs cannot grow the file forever

# Surfaces whose battlenet-schema / -revision headers are logged as the drift
# tripwire (amendment 7); a revision change is reported loudly but never aborts.
DRIFT_SURFACES = ("profile", "equipment", "specializations", "achievements",
                  "statistics", "pvp_summary", "item_media", "guild_roster",
                  "playable_class_index")

# --------------------------------------------------------------------------- #
# Probe endpoints / budgets (tools/bnet/probe.py; Phase 1 contract frozen here)
# --------------------------------------------------------------------------- #
PROBE_USER_AGENT = "wow-forever-roster/1.0 (phase-1 endpoint probe; read-only)"
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

REQUEST_BUDGET = 300          # hard stop so a runaway href walk cannot burn quota
MAX_ATTEMPTS_PER_REQUEST = 4
MAX_LOOKUP_FOLLOWS = 12       # static-ns playable_class / playable_race resolution
SAMPLE_BUDGET = 12            # trimmed sample payloads kept in the raw JSON

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
