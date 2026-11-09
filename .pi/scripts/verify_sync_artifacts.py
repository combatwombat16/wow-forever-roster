#!/usr/bin/env python3
"""Deterministic checks for the Phase 2 sync artifacts (schema v2, data/ layout).

Exit 0 = every clause holds; exit 1 = each failing clause was printed. The
load-bearing assertions: the map covers exactly the CURRENT roster ids, ok rows
carry real Battle.net identity, ambiguous rows carry candidates and no slug, the
sidecar contains only whitelisted normalized keys (no raw Blizzard blobs), no
credential material appears in any committed surface (including the roster and
the unit-test fixtures), NO root-level copy of characters.json /
battle-net-map.json / character-data.json exists in the worktree (a root copy is
a UI/legacy fork of the roster, not this pipeline's data), the sync workflow has no
PR/push trigger and an exact permissions block (and the only other permitted workflow,
pages.yml, is checked separately: secret-free, minimally permitted, Tailwind-built,
deployed through the official Pages actions), the pipeline runs the offline
unit tests then the modular ``python3 -m bnet sync`` entrypoint under a job
``timeout-minutes`` and commits under ``data/``, and the sync package reuses
Last-Modified conditional GETs (amendment 3: If-Modified-Since yes, the
If-None-Match flavour of task 2.4 is dead — no response ever carried an ETag).

Phase 5b added the two static icon maps (``data/class-icons.json`` and
``data/profession-icons.json``): the talent-tree envelope, keyed by Blizzard's own class /
profession name, every value ``{id: int, iconUrl}`` on the render host, duplicate JSON keys and
a byte ceiling checked, and both files joined to the foreign-URL and secret scans. The maps are
name-keyed because the UI joins them by word, so the KEY rules are gated as strictly as the URLs.

Phase 2 moved the artifacts to ``data/`` and the implementation from the two
root scripts into the ``tools/bnet/`` package, so the source-level security
checks below read that package. The invariant text was carried over unchanged;
only the file each check reads changed (and the mass-flip floor moved from an
ordering check to a single-writer check inside ``bnet.artifacts``).
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools" / "bnet"
ROSTER = ROOT / "data" / "characters.json"
MAP = ROOT / "data" / "battle-net-map.json"
DATA = ROOT / "data" / "character-data.json"
TREES = ROOT / "data" / "talent-trees.json"
# Phase 5b (P5.B, notes §11): the two site-wide static icon maps.
CLASS_ICONS = ROOT / "data" / "class-icons.json"
PROFESSION_ICONS = ROOT / "data" / "profession-icons.json"
WORKFLOW = ROOT / ".github" / "workflows" / "sync-bnet.yml"
PAGES_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
ENV = ROOT / ".env"

SYNC_MODULES = ("__init__.py", "__main__.py", "config.py", "http.py", "roster.py",
                "resolve.py", "fetch.py", "artifacts.py", "sync.py")
PKG_MODULES = SYNC_MODULES + ("probe.py",)

STATUS_VOCAB = {"ok", "ambiguous", "not_found", "needs_review", "fetch_error"}
MAP_KEYS = {"rosterName", "status", "slug", "href", "bnetName", "surname",
            "matchedForm", "mappedVia", "note", "candidates"}
CANDIDATE_KEYS = {"slug", "name", "surname", "matchedForm", "href"}
MAPPED_VIA = {"direct", "guild_roster", "confirmed"}
ROSTER_FIELDS = {"id", "player", "name", "track", "role", "roleSec", "race", "class", "prof1", "prof2"}
# The validation block has two legitimate shapes (design §2, Phase 3d): the review verdict
# ``{state: validated, confirmedSlug, reviewedBy, reviewedAt}`` and the reviewer's *open report*
# ``{note, reportedBy, reportedAt}`` written by the detail modal's "report mismatch" action. The
# report carries no ``state`` key — that is what keeps the row a draft — and ``state`` stays the
# only stored lifecycle value (checked below, unchanged).
VALIDATION_KEYS = {"state", "confirmedSlug", "reviewedBy", "reviewedAt",
                   "note", "reportedBy", "reportedAt"}
# Same ceiling as ``bnet.roster.REPORT_NOTE_MAX`` and ``js/detail.js REVIEW_NOTE_MAX``: the form
# caps it, the sync caps it, and a longer note is a hand-edit rather than something the UI wrote.
REPORT_NOTE_MAX = 240
ROSTER_KEYS = ROSTER_FIELDS | {"validation"}
# Phase 1 modal rework (T1.1–T1.5) is additive: the pre-existing keys and their
# meaning are untouched, the five new keys are optional whole blocks. The
# sub-object key sets below mirror bnet.fetch's extractors exactly, so the
# sidecar the UI consumes is schema-checked at every level, not just the top.
CHAR_KEYS = {"status", "bnet", "level", "guild", "activeSpec", "itemLevel",
             "gear", "progress", "achievementPoints", "fetchedAt",
             "stats", "talents", "professions", "reputations", "media",
             # Phase 4a (notes §10): race/class identity off the profile payload, plus
             # the class icon. ADDITIVE again — nothing above changed its meaning.
             "race", "class", "classIconUrl", "raceIconUrl"}
BNET_KEYS = {"name", "surname", "href"}
GEAR_KEYS = {"slot", "name", "ilvl", "iconUrl",
             "itemId", "quality", "itemClass", "itemSubclass", "enchants", "stats"}
GEAR_STAT_KEYS = {"value", "displayString", "isEquipBonus", "color"}
RGBA_KEYS = {"r", "g", "b", "a"}
ID_NAME_KEYS = {"id", "name"}
STAT_VALUE_KEYS = {"base", "effective", "value", "ratingBonus", "ratingNormalized", "id", "name"}
TALENT_SPEC_KEYS = {"specId", "specName", "spentPoints", "talents"}
TALENT_RANK_KEYS = {"rank", "talentId", "spellId", "spellName", "description", "castTime", "iconUrl"}
PROFESSIONS_KEYS = {"primaries", "secondaries"}
# Phase 4a: a profession entry gains iconUrl (notes §10, 200 for a primary AND a
# secondary id). The check stays a subset test, so rows synced before this phase
# still pass and a new row is host-gated on the icon it now carries.
PROFESSION_KEYS = {"id", "name", "tiers", "iconUrl"}
PROFESSION_TIER_KEYS = {"skillPoints", "maxSkillPoints", "tierId", "tierName", "knownRecipes"}
RECIPE_KEYS = {"id", "name"}
REPUTATION_KEYS = {"factionId", "factionName", "standing"}
STANDING_KEYS = {"raw", "value", "max", "tier", "name"}
# Phase B (roster-ui-tasks.md): ``fullRender`` is the derived + HEAD-validated
# full-body render the gear paper doll is drawn around. CONVENTION (mirrored in
# bnet.artifacts.MEDIA_KEYS and written by bnet.Fetcher):
#
#   * the key is OPTIONAL in a committed artifact — every row written before Phase B
#     has no fullRender key at all, so a gate that demanded it would red-line the
#     nightly against data that predates the feature (this checkout's
#     data/character-data.json is exactly that case);
#   * a media block written FROM Phase B on always carries the key: a render-host URL
#     this run saw served with HTTP 200 at the URL derived from that row's own
#     ``avatar``, or an explicit ``null`` saying "derived and probed, nothing served";
#   * so null means "no render" and a MISSING key means "row predates the probe" —
#     the UI draws the generic silhouette for both, and neither is ever a placeholder
#     URL or a guess at one.
MEDIA_KEYS = {"avatar", "avatarSd", "fullRender"}
TREE_KEYS = {"treeId", "classId", "className", "specId", "specName", "name", "nodes"}
TREE_NODE_KEYS = {"id", "displayRow", "displayCol", "rawPositionX", "rawPositionY",
                  "unlocks", "lockedBy", "ranks"}
MAX_TREES_BYTES = 2 * 1024 * 1024
# Phase 5b (notes §11): data/class-icons.json and data/profession-icons.json share
# the talent-tree envelope ({staticNamespace, updatedAt, icons}) and one entry
# shape ({id, iconUrl}). The byte ceiling is generous by design — the observed
# documents are ~1.5 KiB (9 classes, 13 professions) — but a map that grew past
# this is either a hostile index or a sync bug duplicating entries, and the site
# fetches this file on every distribution bar it draws.
MAX_ICON_MAP_BYTES = 64 * 1024
ICON_MAP_DOC_KEYS = {"staticNamespace", "updatedAt", "icons"}
ICON_ENTRY_KEYS = {"id", "iconUrl"}
# Same ceiling as ``bnet.config.ICON_MAP_KEY_MAX``: the name is an artifact KEY,
# so it is bounded rather than truncated.
ICON_MAP_KEY_MAX = 64
# Names an icon map must never be keyed by, whatever an index claims: a JS consumer
# that copies the parsed object with ``out[k] = v`` silently sets the prototype
# instead of creating an entry for ``__proto__`` (and resolves a built-in for
# ``constructor``/``toString``). bnet.fetch.icon_map_key refuses these on the write
# side; this gate is the independent read of the same rule.
JS_OBJECT_KEY_HAZARDS = {"__proto__", "constructor", "prototype", "hasownproperty",
                         "isprototypeof", "propertyisenumerable", "tolocalestring",
                         "tostring", "valueof", "__definegetter__", "__definesetter__",
                         "__lookupgetter__", "__lookupsetter__"}
# A version-pinned Forever static namespace, the shape both maps pin to.
STATIC_NS_PIN_RE = re.compile(r"^static-[A-Za-z0-9._-]+-forever-us$")
PROGRESS_KEYS = {"raid", "mythicPlus", "pvp"}
# Keys that only exist in raw Blizzard payloads: their presence would mean the
# sync dumped unnormalized data into the public sidecar.
RAW_BLOB_KEYS = {"_links", "equipped_items", "specialization_groups", "category_progress",
                 "criteria", "enchantments", "sell_price", "header_names", "body_snippet"}
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Security review 2026-11-08: icons are the only third-party asset the site loads,
# and they live on exactly one Blizzard host.
ICON_URL_RE = re.compile(r"^https://render\.worldofwarcraft\.com/")
ARTIFACT_HOSTS = {"us.api.blizzard.com", "us.battle.net", "render.worldofwarcraft.com"}
PROFILE_HREF_RE = re.compile(r"^https://us\.api\.blizzard\.com/profile/wow/character/[^/?]+/?(\?.*)?$")
TOKEN_LITERAL_RE = re.compile(r"(?:Basic|Bearer)\s+(?!\$|\{\{|f\"|f')[A-Za-z0-9+/=_-]{20,}")
MAX_ARTIFACT_BYTES = 500 * 1024


# --- Phase B: the render-suffix swap, read from bnet.config, not copied ----------
# The two suffixes are the sync's contract (config is the one place a patch can move
# them), so this gate reads them out of that module: a literal copy here would keep
# passing for hours after the shipped code started deriving a different URL.
def _config_string(name):
    """One ``NAME = "value"`` constant from tools/bnet/config.py, or None."""
    try:
        src = (PKG / "config.py").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(rf"^{name}\s*=\s*\"([^\"]*)\"", src, flags=re.M)
    return found.group(1) if found else None


AVATAR_SUFFIX = _config_string("AVATAR_ASSET_SUFFIX")
FULL_RENDER_SUFFIX = _config_string("FULL_RENDER_ASSET_SUFFIX")


def render_sibling(avatar):
    """The full-render URL this avatar implies, derived exactly as bnet.fetch does."""
    if not isinstance(avatar, str) or not AVATAR_SUFFIX or not avatar.endswith(AVATAR_SUFFIX):
        return None
    return avatar[:-len(AVATAR_SUFFIX)] + (FULL_RENDER_SUFFIX or "")

fails = []
passes = 0
warnings = []


def check(cond, msg):
    global passes
    if cond:
        passes += 1
        print(f"  PASS {msg}")
    else:
        fails.append(msg)
        print(f"  FAIL {msg}")


def check_soft(cond, msg, warn_msg=None):
    """WARNING tier: counted and printed either way, but a violation is never fatal.

    Used exactly where a value can leave the site's frozen vocabulary without being
    bad data (notes §10: the API can name a race the roster has never used), so the
    gate surfaces it for a human and still exits 0 — a Blizzard rename is a finding,
    not an outage, and changing the roster's vocabulary is pi-warden's call, not a
    nightly's. warn_msg exists because a violation listed under the pass wording
    ("is inside the vocabulary", warned) reads as a broken gate."""
    global passes
    if cond:
        passes += 1
        print(f"  PASS {msg}")
    else:
        text = warn_msg or msg
        warnings.append(text)
        print(f"  WARN {text}")


def bail():
    """Exit 1 from an early-return point, printing the tally first.

    Every early return in main() is reached only after a check has already failed
    (unreadable roster, unparsable or wrongly-shaped artifact document). A bare
    ``return`` handed ``None`` to sys.exit, i.e. exit 0, so a forked/poisoned
    artifact set would sail through the CI gate that exists to block it — the
    fork gate below is worthless without this."""
    print(f"\n{passes} passed, {len(fails)} failed")
    for item in fails:
        print(f"  - {item}")
    return 1


def norm_slug(name):
    return "-".join(str(name).lower().split())


def walk_keys(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(str(key))
            walk_keys(value, out)
    elif isinstance(node, list):
        for item in node:
            walk_keys(item, out)


def read(path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def duplicate_json_keys(text):
    """Every object key that appears twice at any depth in one JSON document.

    json.loads keeps the last of a duplicated key, so a poisoned or hand-merged
    artifact can hide an entry behind a repeat of the same name — and for the
    name-keyed icon maps that is the whole attack: ``{"Warrior": {…good…},
    "Warrior": {…beacon…}}`` reads as one clean entry. Checked on the raw text at
    every depth, because the parse has already thrown the evidence away."""
    dupes = set()

    def hook(pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                dupes.add(str(key))
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(text, object_pairs_hook=hook)
    except ValueError:
        return set()          # the parse check owns that failure
    return dupes


def icon_map_key_ok(name):
    """The read-side mirror of ``bnet.fetch.icon_map_key``; returns the refusal reason."""
    if not isinstance(name, str) or not name.strip():
        return "is not a non-empty string"
    if len(name) > ICON_MAP_KEY_MAX:
        return f"exceeds the {ICON_MAP_KEY_MAX}-character key ceiling"
    if any(ch < " " or ch == "\x7f" for ch in name):
        return "carries a control character"
    if "://" in name:
        return "carries a URL scheme"
    if name.casefold() in JS_OBJECT_KEY_HAZARDS:
        return "is an Object.prototype property name"
    return None


def check_icon_map(label, path, expected_names, expected_source, floor=9):
    """One Phase 5b icon-map artifact: envelope, keys, values, hosts, size (§11).

    ``expected_names`` is the SOFT coverage set — the class/profession words the site
    actually renders rows for. Absent coverage is a WARNING, not a failure: a patch
    that renames or drops one class is exactly the kind of finding the nightly is for
    (the same reasoning as §10.4.3), while a missing file, a bad host or a stray key
    is a poisoned artifact and fails the gate. ``floor`` is the one hard size rule: the
    map covers the whole index (P5.B: "ALL playable classes"), so a document smaller
    than the classic class count was filtered to the roster by mistake."""
    text = read(path)
    rel = path.relative_to(ROOT)
    check(text is not None, f"{label}: {rel} exists")
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except ValueError as exc:
        check(False, f"{label}: parses as JSON: {exc}")
        return None
    check(isinstance(doc, dict) and set(doc) == ICON_MAP_DOC_KEYS,
          f"{label}: top-level keys are exactly staticNamespace/updatedAt/icons "
          f"(found {sorted(doc) if isinstance(doc, dict) else type(doc).__name__})")
    if not isinstance(doc, dict):
        return text
    namespace = doc.get("staticNamespace")
    check(isinstance(namespace, str) and STATIC_NS_PIN_RE.match(namespace or "") is not None,
          f"{label}: version-pinned to a Forever static namespace ({namespace!r})")
    check(isinstance(doc.get("updatedAt"), str) and ISO_RE.match(doc["updatedAt"]),
          f"{label}: updatedAt is ISO8601 UTC")
    icons = doc.get("icons")
    check(isinstance(icons, dict) and bool(icons),
          f"{label}: icons is a non-empty object "
          f"({len(icons) if isinstance(icons, dict) else type(icons).__name__} entries)")
    if isinstance(icons, dict):
        for name, entry in icons.items():
            reason = icon_map_key_ok(name)
            check(reason is None, f"{label}: key {str(name)[:40]!r} is a usable artifact key"
                                  + (f" ({reason})" if reason else ""))
            check(isinstance(entry, dict) and set(entry) == ICON_ENTRY_KEYS,
                  f"{label}: entry {str(name)[:40]!r} has exactly the keys id/iconUrl")
            if not isinstance(entry, dict):
                continue
            # An entry exists precisely because its icon resolved, so iconUrl is
            # never null here (the per-character fields may be; this map may not).
            check(isinstance(entry.get("id"), int) and not isinstance(entry.get("id"), bool),
                  f"{label}: entry {str(name)[:40]!r} id is an int (not a bool)")
            url = entry.get("iconUrl")
            check(isinstance(url, str) and ICON_URL_RE.match(url) is not None,
                  f"{label}: entry {str(name)[:40]!r} iconUrl matches "
                  f"^https://render\\.worldofwarcraft\\.com/")
        if expected_names:
            missing = sorted(n for n in expected_names if n not in icons)
            check_soft(
                not missing,
                f"{label} cover every name the site renders rows for "
                f"({expected_source}: {len(expected_names) - len(missing)}/{len(expected_names)})",
                warn_msg=f"{label} do NOT cover {missing} from {expected_source}: the UI renders "
                         f"those rows icon-less (no placeholder is invented) — re-run the §11 sweep "
                         f"and check whether the index still lists the name")
    if isinstance(icons, dict):
        check(len(icons) >= floor,
              f"{label}: carries {len(icons)} entries — the whole index, not the guild's own "
              f"subset (floor {floor})")
    dupes = duplicate_json_keys(text)
    check(not dupes, f"{label}: no duplicated JSON key at any depth (found: {sorted(dupes)[:5]})")
    foreign = sorted({u for u in re.findall(r"https?://[^\s\"'<>\\]+", text)
                      if urlparse(u).netloc not in ARTIFACT_HOSTS})
    check(not foreign, f"{label}: contains no non-Blizzard URL (found: {foreign[:3]})")
    check(len(text) < MAX_ICON_MAP_BYTES,
          f"{label}: is under {MAX_ICON_MAP_BYTES} bytes (found {len(text)}B)")
    return text


def workflow_steps(text):
    """[(name, block_text)] in file order, for the steps list of the last job.

    Dependency-free line scan rather than PyYAML: nothing about a verification
    gate may depend on a package the runner happens to have, and the only fact
    that matters here is the ORDER of the steps (verify before commit).
    """
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^\s*steps:\s*$", ln)]
    if not starts:
        return []
    item_indent, blocks = None, []
    for ln in lines[starts[-1] + 1:]:
        m = re.match(r"^(\s*)-\s", ln)
        if m and (item_indent is None or len(m.group(1)) == item_indent):
            if item_indent is None:
                item_indent = len(m.group(1))
            blocks.append([ln])
        elif blocks:
            blocks[-1].append(ln)
    out = []
    for block in blocks:
        head = next((ln for ln in block if re.match(r"^\s*(?:-\s+)?name:", ln)), block[0])
        out.append((head.split("name:", 1)[1].strip().strip('"').strip("'"), "\n".join(block)))
    return out


def permission_pairs(text):
    """[(scope, level)] for every ``permissions:`` block in a workflow file.

    Same dependency-free line scan as ``workflow_steps``: a verification gate may not
    depend on a package the runner lacks. Covers both the top-level and the per-job
    blocks, which is exactly the surface a least-privilege review needs.
    """
    pairs = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not re.match(r"^\s*permissions:\s*$", ln):
            continue
        indent = len(ln) - len(ln.lstrip())
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            m = re.match(r"^\s*([a-z][a-z-]*):\s*(read|write|none)\s*$", nxt)
            if m:
                pairs.append((m.group(1), m.group(2)))
    return pairs


def body_of(text, pattern):
    """The text of one def block, for scope-accurate source checks."""
    m = re.search(pattern, text, flags=re.S)
    return m.group(1) if m else ""


def js_object_keys(text, const_name):
    """String keys of a JS object literal, for the pi-warden vocabulary gate.

    The vocabularies are read out of the modules that own them rather than being
    copied into this script: a copy here would be a second list free to drift, and
    the whole point of the WARNING tier is that a drift shows up as a WARN naming the
    offending value instead of as a red nightly or a silently vacuous check."""
    block = body_of(text or "", rf"(?:export\s+)?const\s+{const_name}\s*=\s*\{{(.*?)\n\}}")
    return set(re.findall(r'^\s*"([^"]+)"\s*:', block, flags=re.M))


CLASS_VOCAB = js_object_keys(read(ROOT / "js" / "util.js"), "CLASS_COLORS")
RACE_VOCAB = js_object_keys(read(ROOT / "js" / "editchar.js"), "FOREVER_RACE_CLASSES")


def main():
    # --- module sources ------------------------------------------------------ #
    sources = {name: read(PKG / name) for name in PKG_MODULES}
    missing = [n for n, t in sources.items() if t is None]
    check(not missing, f"tools/bnet/ package is complete (missing: {missing})")
    if missing:
        print(f"\n0 passed, {len(missing)} failed")
        return 1
    SYNC_TEXT = "".join(sources[n] for n in SYNC_MODULES)     # the shipped sync surface
    PKG_TEXT = "".join(sources[n] for n in PKG_MODULES)
    CFG, HTTP, ROSTER_MOD, RESOLVE, FETCH, ARTIFACTS, SYNC = (
        sources["config.py"], sources["http.py"], sources["roster.py"],
        sources["resolve.py"], sources["fetch.py"], sources["artifacts.py"], sources["sync.py"])

    # --- fork gate: no root-level copies of the three data files -------------- #
    # Phase 2 moved the roster and both artifacts under data/. A root-level copy of
    # any of these three names in the worktree means this checkout is a UI/legacy
    # fork of the roster: the pre-migration toolchain (and the Phase 3 UI prototype)
    # read and write them at the root, so the sync would publish data/ while a stale
    # duplicate kept being served, and the two copies would diverge silently. The
    # single source of truth is data/<name>; a root copy fails the gate.
    for stale in ("characters.json", "battle-net-map.json", "character-data.json"):
        check(not (ROOT / stale).exists(),
              f"no root-level {stale} in the worktree — its presence means a UI/legacy fork "
              f"of the roster (data/{stale} is the single source of truth)")

    # --- roster baseline ---------------------------------------------------- #
    roster_text = read(ROSTER)
    check(roster_text is not None, "data/characters.json exists")
    if roster_text is None:
        print(f"\n0 passed, 1 failed")
        return 1
    roster_doc = json.loads(roster_text)
    check(isinstance(roster_doc, dict),
          "roster is the v2 object (the v1 bare array is forbidden after the migration commit)")
    roster = roster_doc.get("characters") if isinstance(roster_doc, dict) else None
    check(isinstance(roster, list) and bool(roster), "roster characters is a non-empty list")
    check(set(roster_doc) == {"schemaVersion", "characters"}, "roster top-level keys are exactly schemaVersion/characters")
    check(roster_doc.get("schemaVersion") == 2, "roster schemaVersion == 2")
    reported_ids = set()      # rows carrying an open reviewer note; filled below, read per entry
    for rec in roster or []:
        check(isinstance(rec, dict) and isinstance(rec.get("id"), str) and bool(rec["id"]),
              f"roster record has a string id ({rec.get('id')})")
        check(set(rec) <= ROSTER_KEYS,
              f"roster {rec.get('id')} carries only the ten original fields plus validation")
        v = rec.get("validation")
        if v is not None:
            check(isinstance(v, dict) and set(v) <= VALIDATION_KEYS,
                  f"roster {rec.get('id')} validation block has no unknown keys")
            check(v.get("state") in (None, "validated"),
                  f"roster {rec.get('id')} validation.state is the only stored value (validated)")
            note = v.get("note")
            if note is not None:
                check(isinstance(note, str) and 0 < len(note.strip()) <= REPORT_NOTE_MAX,
                      f"roster {rec.get('id')} validation.note is a non-empty string of at most "
                      f"{REPORT_NOTE_MAX} characters")
            # An open report: not validated, and carrying a usable note. The sync turns exactly
            # this into a map needs_review row on every run (bnet.roster.reported_note /
            # bnet.resolve.reported_note_entry), and the map-side mirror below keeps the two
            # documents honest about it.
            if v.get("state") != "validated" and isinstance(note, str) and note.strip():
                reported_ids.add(rec["id"])
    roster_ids = [rec["id"] for rec in roster]
    by_id = {rec["id"]: rec for rec in roster}
    collision_ids = {rid for norm, ids
                     in {n: [r["id"] for r in roster if norm_slug(r["name"]) == n]
                         for n in {norm_slug(r["name"]) for r in roster}}.items()
                     if len(ids) > 1 for rid in ids}
    # the trust rule the sync implements, mirrored here so data and code agree
    trusted_slug = {}
    for rec in roster:
        v = rec.get("validation") or {}
        if v.get("state") == "validated" or v.get("confirmedSlug"):
            trusted_slug[rec["id"]] = v.get("confirmedSlug")

    # --- both artifacts parse ----------------------------------------------- #
    map_text, data_text = read(MAP), read(DATA)
    check(map_text is not None, "data/battle-net-map.json exists")
    check(data_text is not None, "data/character-data.json exists")
    if fails:
        return bail()
    try:
        map_doc = json.loads(map_text)
        data_doc = json.loads(data_text)
        parses = True
    except ValueError as exc:
        parses = False
        check(False, f"artifacts parse as JSON: {exc}")
    if not parses:
        return bail()

    # --- map schema ----------------------------------------------------------- #
    check(map_doc.get("schemaVersion") == 2, "map schemaVersion == 2")
    check(isinstance(map_doc.get("updatedAt"), str) and ISO_RE.match(map_doc["updatedAt"]),
          "map updatedAt is ISO8601 UTC")
    entries = map_doc.get("entries")
    check(isinstance(entries, dict), "map entries is an object")
    if not isinstance(entries, dict):
        return bail()
    check(set(entries) == set(roster_ids),
          f"map covers exactly the current roster ids (map={len(entries)} roster={len(roster_ids)}; "
          f"missing={sorted(set(roster_ids) - set(entries))[:3]} stale={sorted(set(entries) - set(roster_ids))[:3]})")
    for rid, entry in entries.items():
        if not isinstance(entry, dict):
            check(False, f"map entry {rid} is an object")
            continue
        check(set(entry) == MAP_KEYS, f"map entry {rid} has exactly the v2 key set")
        status = entry.get("status")
        check(status in STATUS_VOCAB, f"map entry {rid} status '{status}' in vocabulary")
        check(isinstance(entry.get("candidates"), list), f"map entry {rid} candidates is a list (v2 required)")
        if rid in by_id:
            check(entry.get("rosterName") == by_id[rid]["name"], f"map entry {rid} rosterName matches characters.json")
        # Defence in depth: even a non-ok row must never publish an off-host href.
        check(entry.get("href") is None or PROFILE_HREF_RE.match(entry["href"]) is not None,
              f"entry {rid} href is null or a Blizzard profile URL (no persisted foreign href)")
        for cand in entry.get("candidates") or []:
            check(isinstance(cand, dict) and set(cand) <= CANDIDATE_KEYS,
                  f"map entry {rid} candidate has only slug/name/surname/matchedForm/href")
            if isinstance(cand, dict):
                check(cand.get("href") is None or PROFILE_HREF_RE.match(cand["href"] or "") is not None,
                      f"map entry {rid} candidate href is null or a Blizzard profile URL")
        if status == "ok":
            check(isinstance(entry.get("slug"), str) and bool(entry["slug"]), f"ok entry {rid} has a slug")
            check(isinstance(entry.get("href"), str)
                  and PROFILE_HREF_RE.match(entry["href"]) is not None,
                  f"ok entry {rid} href matches the frozen profile URL shape (H2)")
            check(entry.get("mappedVia") in MAPPED_VIA, f"ok entry {rid} mappedVia is valid")
            check(isinstance(entry.get("bnetName"), str) and bool(entry["bnetName"]),
                  f"ok entry {rid} carries the payload name")
            # Never auto-pick: more than one plausible profile is never an ok row.
            check(len(entry.get("candidates") or []) < 2,
                  f"ok entry {rid} does not auto-pick between multiple candidates")
        elif status == "ambiguous":
            check(entry.get("slug") is None, f"ambiguous entry {rid} keeps slug null (nothing was picked)")
            check(entry.get("href") is None, f"ambiguous entry {rid} publishes no identity href")
            check(len(entry.get("candidates") or []) >= 1,
                  f"ambiguous entry {rid} carries at least one candidate for the reviewer")
        else:
            check(isinstance(entry.get("note"), str) and bool(entry["note"].strip()),
                  f"non-ok entry {rid} carries a human-readable note")
        if rid in collision_ids:
            check(status != "ok", f"duplicate-normalization id {rid} is not auto-mapped (policy)")
        if rid in reported_ids:
            # Design §2, mirrored here exactly as the trust rule above is: a row a reviewer
            # reported must not be published as a settled ``ok`` identity. ``slug`` may survive —
            # the sync keeps the resolved identity so clearing the report costs nothing but a
            # commit — and the note itself is required of every non-ok row above.
            check(status == "needs_review",
                  f"reported id {rid} is needs_review in the map, not {status} (the note rule of §2)")
        if rid in trusted_slug and status == "ok":
            # Design §3: a confirmedSlug is authoritative; sync never re-resolves it.
            if trusted_slug[rid]:
                check(entry.get("slug") == trusted_slug[rid],
                      f"trusted id {rid} keeps the user-confirmed slug")
            check(entry.get("mappedVia") == "confirmed" and entry.get("note") == "user-confirmed",
                  f"trusted id {rid} ok row is marked confirmed / user-confirmed")

    # --- character-data schema ------------------------------------------------- #
    check(data_doc.get("schemaVersion") == 2, "data schemaVersion == 2")
    check(isinstance(data_doc.get("syncedAt"), str) and ISO_RE.match(data_doc["syncedAt"]),
          "data syncedAt is ISO8601 UTC")
    check(set(data_doc) == {"schemaVersion", "syncedAt", "triggeredBy", "characters"},
          "data top-level keys are exactly schemaVersion/syncedAt/triggeredBy/characters")
    check(isinstance(data_doc.get("triggeredBy"), str) and bool(data_doc["triggeredBy"].strip()),
          f"data triggeredBy is a non-empty provenance string ({data_doc.get('triggeredBy')!r})")
    characters = data_doc.get("characters")
    check(isinstance(characters, dict) and set(characters) == set(roster_ids),
          "data covers exactly the current roster ids")
    if not isinstance(characters, dict):
        return bail()
    for rid, char in characters.items():
        if not isinstance(char, dict):
            check(False, f"data entry {rid} is an object")
            continue
        check(set(char) <= CHAR_KEYS, f"data entry {rid} has no unknown keys")
        status = char.get("status")
        check(status in STATUS_VOCAB, f"data entry {rid} status '{status}' in vocabulary")
        check(isinstance(char.get("fetchedAt"), str) and ISO_RE.match(char["fetchedAt"]),
              f"data entry {rid} fetchedAt is ISO8601")
        if status == "ok":
            bnet = char.get("bnet")
            check(isinstance(bnet, dict) and set(bnet) <= BNET_KEYS and bool(bnet.get("href")),
                  f"ok data entry {rid} has a normalized bnet identity")
            gear = char.get("gear")
            check(isinstance(gear, list), f"ok data entry {rid} gear is a list")
            for item in gear or []:
                if not isinstance(item, dict) or not set(item) <= GEAR_KEYS:
                    check(False, f"data entry {rid} gear item carries only the v2 keys "
                                 f"plus the T1.1 tooltip fields")
                    continue
                icon = item.get("iconUrl")
                # null is the documented outcome when an item-media doc 404s
                # (notes §8: single reference-doc failures are non-fatal). A value
                # off the render host is H2: a persisted attacker URL would be
                # loaded by every visitor's browser and re-fetched nightly.
                check(icon is None or (isinstance(icon, str) and ICON_URL_RE.match(icon)),
                      f"data entry {rid} gear iconUrl matches ^https://render\\.worldofwarcraft\\.com/ or null")
                # T1.1 tooltip fields (notes §9.4): id/quality stay scalar, class
                # references are {id, name}, enchants are strings, stats carry the
                # RGBA display colour verbatim. No gem/socket keys exist anywhere
                # (amendment 2), so none may appear.
                check(item.get("itemId") is None or (isinstance(item.get("itemId"), int)
                      and not isinstance(item.get("itemId"), bool)),
                      f"data entry {rid} gear itemId is null or an int")
                check(item.get("quality") is None or isinstance(item.get("quality"), str),
                      f"data entry {rid} gear quality is null or a string enum")
                for ref_key in ("itemClass", "itemSubclass"):
                    ref = item.get(ref_key)
                    check(ref is None or (isinstance(ref, dict) and set(ref) <= ID_NAME_KEYS),
                          f"data entry {rid} gear {ref_key} is null or {{id, name}}")
                enchants = item.get("enchants")
                check(isinstance(enchants, list) and all(isinstance(x, str) for x in enchants),
                      f"data entry {rid} gear enchants is a list of strings")
                gear_stats = item.get("stats") or []
                check(all(isinstance(s, dict) and set(s) <= GEAR_STAT_KEYS for s in gear_stats),
                      f"data entry {rid} gear stats carry only value/displayString/isEquipBonus/color")
                check(all(s.get("color") is None or (isinstance(s.get("color"), dict) and set(s["color"]) <= RGBA_KEYS)
                          for s in gear_stats if isinstance(s, dict)),
                      f"data entry {rid} gear stat colors are null or RGBA")
            progress = char.get("progress")
            if progress is not None:
                check(isinstance(progress, dict) and set(progress) <= PROGRESS_KEYS and bool(progress),
                      f"data entry {rid} progress only has data-bearing keys (omit-when-empty)")
            # --- Phase 1 additive blocks (T1.2–T1.5) ------------------------- #
            stats_block = char.get("stats")
            if stats_block is not None:
                check(isinstance(stats_block, dict) and bool(stats_block),
                      f"data entry {rid} stats is a non-empty object when present")
                bad_stats = sorted(
                    key for key, value in (stats_block or {}).items()
                    if not ((isinstance(value, (int, float)) and not isinstance(value, bool))
                            or (isinstance(value, dict) and bool(value) and set(value) <= STAT_VALUE_KEYS)))
                check(not bad_stats,
                      f"data entry {rid} stats values are numbers or normalized groups (bad: {bad_stats[:5]})")
            talents_block = char.get("talents")
            if talents_block is not None:
                check(isinstance(talents_block, list) and bool(talents_block),
                      f"data entry {rid} talents is a non-empty list when present")
                for spec_group in talents_block or []:
                    check(isinstance(spec_group, dict) and set(spec_group) <= TALENT_SPEC_KEYS,
                          f"data entry {rid} talent group carries only specId/specName/spentPoints/talents")
                    selected = (spec_group.get("talents") or []) if isinstance(spec_group, dict) else []
                    check(all(isinstance(t, dict) and set(t) <= TALENT_RANK_KEYS for t in selected),
                          f"data entry {rid} selected talents carry only the T1.3 fields")
                    check(all(t.get("iconUrl") is None
                              or (isinstance(t.get("iconUrl"), str) and ICON_URL_RE.match(t["iconUrl"]))
                              for t in selected if isinstance(t, dict)),
                          f"data entry {rid} selected-talent iconUrls are null or on the render host")
            professions = char.get("professions")
            if professions is not None:
                check(isinstance(professions, dict) and set(professions) <= PROFESSIONS_KEYS
                      and bool(professions.get("primaries") or professions.get("secondaries")),
                      f"data entry {rid} professions has only primaries/secondaries and carries data")
                for side in ((professions or {}).get("primaries") or []) + ((professions or {}).get("secondaries") or []):
                    check(isinstance(side, dict) and set(side) <= PROFESSION_KEYS,
                          f"data entry {rid} profession carries only id/name/tiers/iconUrl")
                    # Phase 4a (notes §10): the icon is a response-derived URL like
                    # every other one in this artifact, so null (doc 404) or the
                    # render host, never the attacker's string (H2).
                    if isinstance(side, dict):
                        prof_icon = side.get("iconUrl")
                        check(prof_icon is None or (isinstance(prof_icon, str) and ICON_URL_RE.match(prof_icon)),
                              f"data entry {rid} profession iconUrl is null or matches "
                              f"^https://render\\.worldofwarcraft\\.com/")
                    for tier in (side.get("tiers") or []) if isinstance(side, dict) else []:
                        check(isinstance(tier, dict) and set(tier) <= PROFESSION_TIER_KEYS,
                              f"data entry {rid} profession tier carries only the T1.4 fields")
                        for recipe in (tier.get("knownRecipes") or []) if isinstance(tier, dict) else []:
                            check(isinstance(recipe, dict) and set(recipe) <= RECIPE_KEYS,
                                  f"data entry {rid} known recipe carries only id/name")
            reputations = char.get("reputations")
            if reputations is not None:
                check(isinstance(reputations, list) and bool(reputations),
                      f"data entry {rid} reputations is a non-empty list when present")
                for rep in reputations or []:
                    check(isinstance(rep, dict) and set(rep) <= REPUTATION_KEYS,
                          f"data entry {rid} reputation carries only factionId/factionName/standing")
                    standing = rep.get("standing") if isinstance(rep, dict) else None
                    check(isinstance(standing, dict) and set(standing) == STANDING_KEYS,
                          f"data entry {rid} reputation standing keeps raw/value/max/tier/name")
            media = char.get("media")
            if media is not None:
                check(isinstance(media, dict) and set(media) <= MEDIA_KEYS,
                      f"data entry {rid} media carries only avatar/avatarSd/fullRender "
                      f"(no inset/headshot exist)")
                for media_key in (MEDIA_KEYS & set(media or {})):
                    media_url = media[media_key]
                    check(media_url is None or (isinstance(media_url, str) and ICON_URL_RE.match(media_url)),
                          f"data entry {rid} media.{media_key} is null or on the render host")
                # Phase B, read as the convention above says: absent is legal (the row
                # predates the probe), null is a probed refusal, a URL must be the
                # render host AND the exact suffix swap of THIS row's avatar. A
                # fullRender that is not its own avatar's sibling is either a hand-edit
                # or a sync bug pointing every doll at one character's render.
                if "fullRender" in media and media["fullRender"] is not None:
                    render_url = media["fullRender"]
                    check(isinstance(render_url, str) and render_url.endswith(FULL_RENDER_SUFFIX),
                          f"data entry {rid} media.fullRender ends in {FULL_RENDER_SUFFIX!r}")
                    check(isinstance(render_url, str) and isinstance(media.get("avatar"), str)
                          and render_url == render_sibling(media["avatar"]),
                          f"data entry {rid} media.fullRender is the suffix swap of its own avatar")

            # --- Phase 4a additive keys (notes §10) -------------------------- #
            # race/class are read off the already-fetched profile (§3.1), so a row
            # that carries one must carry the complete reference: a half-populated
            # {id: null} would render "null" where the UI needs a badge. The sync
            # omits the key instead (bnet.fetch.extract_identity), and a name that
            # leaves the site's frozen vocabulary is a WARNING, not a failure — a
            # patch that renames a race is a finding for a human, not an outage.
            for id_key, vocab, vocab_name in (("race", RACE_VOCAB, "FOREVER_RACE_CLASSES"),
                                              ("class", CLASS_VOCAB, "CLASS_COLORS")):
                ref = char.get(id_key)
                if ref is None:
                    continue
                check(isinstance(ref, dict) and set(ref) == ID_NAME_KEYS
                      and isinstance(ref.get("id"), int) and not isinstance(ref.get("id"), bool)
                      and isinstance(ref.get("name"), str) and bool(ref["name"].strip()),
                      f"data entry {rid} {id_key} is {{id: int, name: non-empty str}} when present")
                name = ref.get("name") if isinstance(ref, dict) else None
                if isinstance(name, str) and name.strip():
                    check_soft(
                        name in vocab,
                        f"data entry {rid} {id_key} name {name!r} is inside the frozen "
                        f"{vocab_name} vocabulary (pi-warden)",
                        warn_msg=f"data entry {rid} {id_key} name {name!r} is OUTSIDE the frozen "
                                 f"{vocab_name} vocabulary (pi-warden): the UI loses its colour and "
                                 f"filter for it, so this needs a deliberate roster change, not a "
                                 f"silent sync edit")
            # Both icon keys get the same gate, so a future patch that opens the race
            # route needs no new check. §10 says the sync writes no raceIconUrl today,
            # which is asserted separately — as a WARNING, because the correct response
            # to Blizzard shipping a race icon is to widen the sync, not to red-line
            # the nightly that noticed.
            for icon_key in ("classIconUrl", "raceIconUrl"):
                if icon_key in char:
                    icon_value = char[icon_key]
                    check(icon_value is None or (isinstance(icon_value, str) and ICON_URL_RE.match(icon_value)),
                          f"data entry {rid} {icon_key} is null or on the render host")
            check_soft("raceIconUrl" not in char,
                       f"data entry {rid} carries no raceIconUrl, as §10 requires",
                       warn_msg=f"data entry {rid} carries a raceIconUrl although §10 found no race "
                                f"media route on any namespace: re-run the §10 sweep, and only then "
                                f"teach bnet.fetch to write it")
    blob_keys = (set(walk_keys(characters, set()) or set()) if characters else set())
    offenders = blob_keys & RAW_BLOB_KEYS
    check(not offenders, f"no raw Blizzard payload keys leaked into the sidecar (found: {sorted(offenders)})")
    # H2: no URL of any kind may reach an artifact unless its host is ours. This
    # scans the raw text, so it also catches a foreign URL smuggled into a note.
    for label, text in (("data/battle-net-map.json", map_text), ("data/character-data.json", data_text)):
        foreign = sorted({u for u in re.findall(r"https?://[^\s\"'<>\\]+", text)
                          if urlparse(u).netloc not in ARTIFACT_HOSTS})
        check(not foreign, f"{label} contains no non-Blizzard URL (found: {foreign[:3]})")
    check(len(map_text) < MAX_ARTIFACT_BYTES and len(data_text) < MAX_ARTIFACT_BYTES,
          f"artifacts are under {MAX_ARTIFACT_BYTES} bytes "
          f"(map={len(map_text)}B data={len(data_text)}B)")

    # --- data/talent-trees.json (T1.3) -------------------------------------- #
    # Shared spec-tree geometry, version-pinned to the static namespace it was
    # fetched from and keyed by spec id — the one file that must NOT be embedded
    # per character, and whose every iconUrl is H2-checked like the sidecar's.
    trees_text = read(TREES)
    check(trees_text is not None, "data/talent-trees.json exists")
    if trees_text is not None:
        trees_doc = None
        try:
            trees_doc = json.loads(trees_text)
        except ValueError as exc:
            check(False, f"data/talent-trees.json parses as JSON: {exc}")
        if isinstance(trees_doc, dict):
            check(set(trees_doc) == {"staticNamespace", "updatedAt", "specs"},
                  "talent-trees top-level keys are exactly staticNamespace/updatedAt/specs")
            check(isinstance(trees_doc.get("staticNamespace"), str)
                  and trees_doc["staticNamespace"].startswith("static-"),
                  f"talent-trees is version-pinned to the static namespace ({trees_doc.get('staticNamespace')!r})")
            check(isinstance(trees_doc.get("updatedAt"), str) and ISO_RE.match(trees_doc["updatedAt"]),
                  "talent-trees updatedAt is ISO8601 UTC")
            specs = trees_doc.get("specs")
            check(isinstance(specs, dict) and bool(specs),
                  "talent-trees specs is a non-empty object keyed by spec id")
            for spec_id, tree in (specs or {}).items():
                if not isinstance(tree, dict):
                    check(False, f"talent-trees spec {spec_id} is an object")
                    continue
                check(set(tree) <= TREE_KEYS and isinstance(tree.get("nodes"), list) and bool(tree["nodes"]),
                      f"talent-trees spec {spec_id} carries identity plus at least one node")
                check(str(tree.get("specId")) == str(spec_id),
                      f"talent-trees spec {spec_id} key matches its payload specId")
                for node in tree.get("nodes") or []:
                    check(isinstance(node, dict) and set(node) <= TREE_NODE_KEYS,
                          f"talent-trees spec {spec_id} node keys are geometry/prerequisites/ranks only")
                    ranks = (node.get("ranks") or []) if isinstance(node, dict) else []
                    check(all(isinstance(r, dict) and set(r) <= TALENT_RANK_KEYS for r in ranks),
                          f"talent-trees spec {spec_id} node {node.get('id') if isinstance(node, dict) else '?'} "
                          f"ranks carry only the T1.3 fields")
                    check(all(r.get("iconUrl") is None
                              or (isinstance(r.get("iconUrl"), str) and ICON_URL_RE.match(r["iconUrl"]))
                              for r in ranks if isinstance(r, dict)),
                          f"talent-trees spec {spec_id} node rank iconUrls are null or on the render host")
        trees_foreign = sorted({u for u in re.findall(r"https?://[^\s\"'<>\\]+", trees_text)
                                if urlparse(u).netloc not in ARTIFACT_HOSTS})
        check(not trees_foreign, f"data/talent-trees.json contains no non-Blizzard URL (found: {trees_foreign[:3]})")
        check(len(trees_text) < MAX_TREES_BYTES,
              f"data/talent-trees.json is under {MAX_TREES_BYTES} bytes (found {len(trees_text)}B)")

    # --- data/class-icons.json + data/profession-icons.json (P5.B, notes §11) --- #
    # The two site-wide static icon maps. Same envelope as the talent trees, keyed
    # by Blizzard's OWN class / profession name, because the UI joins by word, not
    # by id: a distribution bar, a profession tag and a party slot each start from a
    # name. That makes the key as much of an attack surface as the value, so both
    # halves are gated here and every URL is the same render host the sidecar uses.
    roster_class_words = {rec["class"] for rec in roster if isinstance(rec.get("class"), str)}
    roster_prof_words = {w for rec in roster for w in (rec.get("prof1"), rec.get("prof2"))
                         if isinstance(w, str) and w.strip() and w.strip().casefold() != "none"}
    # Both maps cover the WHOLE index rather than this guild (P5.B), so the size
    # floor is the nine classic classes: nine playable classes (§5) and thirteen
    # professions (§11) are what the two indexes return today.
    class_icons_text = check_icon_map("class icon map", CLASS_ICONS,
                                      roster_class_words | CLASS_VOCAB,
                                      "roster class columns + pi-warden CLASS_COLORS",
                                      floor=9)          # §5: playable-class/index returns 9
    profession_icons_text = check_icon_map("profession icon map", PROFESSION_ICONS,
                                           roster_prof_words, "roster prof1/prof2 columns",
                                           floor=9)     # the roster alone names 9 distinct professions

    # --- secret scan ------------------------------------------------------------- #
    secrets = []
    if ENV.exists():
        for line in read(ENV).splitlines():
            _, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if len(value) >= 6:
                secrets.append(value)
    scanned = {f"tools/bnet/{name}": text for name, text in sources.items()}
    # The unit tests run in CI with the credentials in the environment, and the
    # roster is the one data/ file a maintainer edits by hand: both are committed
    # surfaces, so both join the leak scan (a pasted secret in a fixture would
    # otherwise reach main through a path no gate read).
    scanned.update({f"tools/tests/{p.name}": read(p)
                    for p in sorted((ROOT / "tools" / "tests").glob("*.py"))})
    scanned.update({".github/workflows/sync-bnet.yml": read(WORKFLOW),
                    ".github/workflows/pages.yml": read(PAGES_WORKFLOW),
                    "tools/migrate_v2.py": read(ROOT / "tools" / "migrate_v2.py"),
                    "data/characters.json": roster_text,
                    "data/battle-net-map.json": map_text,
                    "data/character-data.json": data_text,
                    "data/talent-trees.json": trees_text,
                    # Phase 5b: both icon maps are committed, publicly served, and
                    # written from response data, so they join every scan below.
                    "data/class-icons.json": class_icons_text,
                    "data/profession-icons.json": profession_icons_text})
    for label, text in scanned.items():
        check(text is not None, f"{label} exists for the secret scan")
        if text is None:
            continue
        for secret in secrets:
            check(secret not in text, f"{label} contains no .env secret value")
        # The transport legitimately names the Authorization header; the committed
        # workflow and artifacts must not mention it at all.
        if not label.startswith("tools/"):
            check("Authorization" not in text, f"{label} contains no Authorization literal")
        check(not TOKEN_LITERAL_RE.search(text), f"{label} contains no hardcoded Basic/Bearer token value")

    # --- workflow shape ------------------------------------------------------------ #
    wf = scanned[".github/workflows/sync-bnet.yml"]
    if wf:
        # Comments legitimately discuss why there are no PR triggers; scan only
        # the YAML syntax, never the prose.
        trigger_section = "\n".join(ln for ln in wf.split("jobs:", 1)[0].splitlines()
                                    if not ln.lstrip().startswith("#"))
        check("schedule:" in trigger_section and "0 6 * * *" in trigger_section,
              "workflow schedules nightly at 06:00")
        check("workflow_dispatch:" in trigger_section, "workflow allows manual dispatch")
        check("pull_request" not in trigger_section, "workflow has no pull_request trigger")
        check(not re.search(r"^\s*push:", trigger_section, flags=re.M), "workflow has no push trigger")
        check("lookup_name" in trigger_section and "force" in trigger_section,
              "workflow exposes lookup_name and force inputs")
        perms = re.search(r"^permissions:\n((?:[ \t]+\S.*\n)+)", wf, flags=re.M)
        check(perms is not None and [ln.strip() for ln in perms.group(1).splitlines() if ln.strip()] == ["contents: write"],
              "permissions block is exactly contents: write")
        check("secrets.BLIZZARD_CLIENT_ID" in wf and "secrets.BLIZZARD_CLIENT_SECRET" in wf,
              "both Blizzard secrets are referenced")
        env_lines = [ln for ln in wf.splitlines() if "secrets." in ln]
        check(all(re.match(r"^\s+BLIZZARD_CLIENT_(ID|SECRET):\s*\$\{\{\s*secrets\.", ln) for ln in env_lines),
              "every secrets.* reference is a BLIZZARD_* env assignment")
        check("skip ci]" in wf, "commit message carries [skip ci]")
        check("bnet-sync-bot" in wf, "commit step uses the bot identity")
        check("concurrency" in wf and "sync-bnet" in wf, "workflow declares the sync-bnet concurrency group")
        check("python3 -m bnet sync" in wf, "workflow runs the modular module entrypoint (python3 -m bnet sync)")
        check("PYTHONPATH=tools" in wf or re.search(r"working-directory:\s*tools", wf) is not None,
              "workflow makes the bnet package importable (PYTHONPATH=tools or working-directory: tools)")
        check("tools/sync_bnet.py" not in wf, "workflow no longer invokes the deleted tools/sync_bnet.py")
        add_lines = [ln for ln in wf.splitlines() if re.match(r"^\s*git add ", ln)]
        check(any("data/battle-net-map.json" in ln and "data/character-data.json" in ln for ln in add_lines),
              f"commit step adds both data/ artifacts ({add_lines})")
        # P5.B: the nightly must publish the two icon maps too, or the committed
        # files drift from the namespace the pair was written against (and the UI
        # joins them by name on every distribution bar).
        check(any("data/class-icons.json" in ln and "data/profession-icons.json" in ln for ln in add_lines),
              f"commit step adds both Phase 5b icon maps ({add_lines})")
        check(not re.search(r"git add (?:battle-net-map|character-data)\.json", wf),
              "commit step no longer adds the old root paths")

    # --- machine-owned commit scope: sync may commit ONLY data/ artifacts ------------ #
    # (the Phase 2 guard 'index.html unmodified vs HEAD' is obsolete since Phase 3
    # legitimately develops the UI; what must hold is that the sync workflow never
    # commits UI files — only the two data/ artifacts.)
    if wf:
        bad_adds = [ln for ln in wf.splitlines()
                    if re.match(r"^\s*git add ", ln)
                    and re.search(r"index\.html|\bjs/|\bcss/|characters\.json", ln)]
        check(not bad_adds, f"sync workflow never commits UI files or the roster ({bad_adds})")

    # --- caching mechanism (amendment 3) --------------------------------------------- #
    check("If-Modified-Since" in SYNC_TEXT, "sync package sends If-Modified-Since")
    check("If-None-Match" not in SYNC_TEXT,
          "sync package never sends If-None-Match (no response has ever carried an ETag)")

    # --- budgets ---------------------------------------------------------------------- #
    check(re.search(r"MAX_REQUESTS\s*=\s*2500", CFG) is not None, "sync per-run cap stays MAX_REQUESTS = 2500")
    check(re.search(r"REQUEST_BUDGET\s*=\s*300", CFG) is not None, "probe budget stays REQUEST_BUDGET = 300")
    check("config.MAX_REQUESTS" in HTTP, "the request cap is enforced from the single config value")

    # --- H1: credentialed requests are host-pinned and never follow redirects -------- #
    origin = re.search(r"ALLOWED_API_ORIGIN_RE\s*=\s*re\.compile\((.*?)\)\n", CFG, flags=re.S)
    allow = origin.group(1) if origin else ""
    check(origin is not None and "https" in allow and "us\\.battle\\.net" in allow
          and "us\\.api\\.blizzard\\.com" in allow and "^" in allow,
          "config defines an anchored https host allowlist for both Blizzard origins")
    get_body = body_of(HTTP, r"\n    def get\(self, url\):(.*?)\n    def ")
    check(get_body != "" and "require_allowed_url(" in get_body,
          "Client.get enforces the host allowlist before any credentialed send")
    mint_body = body_of(HTTP, r"\n    def mint_token\(self, reason\):(.*?)\n    def ")
    check(mint_body != "" and "require_allowed_url(config.TOKEN_URL" in mint_body,
          "token mint (the other credentialed send path) is allowlist-checked too")
    check("urllib.request.urlopen(" not in PKG_TEXT,
          "no request bypasses the hardened opener (no bare urlopen left anywhere in the package)")
    check(re.search(r"class\s+_NoRedirectHandler\(urllib\.request\.HTTPRedirectHandler\)", HTTP) is not None,
          "http defines a redirect-suppressing HTTPRedirectHandler subclass")
    check(re.search(r"def redirect_request\([^)]*\):\s*\n\s+return None", HTTP) is not None,
          "redirect_request returns None, so a 3xx is reported instead of followed with credentials")
    check("NO_REDIRECT_OPENER.open(" in HTTP, "send() dispatches through the no-redirect opener")

    # --- Phase B: the uncredentialed HEAD path, host-pinned like everything else --- #
    # A derived URL is the one value in this pipeline that no endpoint ever returned,
    # so the probe that turns it into a publishable render is a security-relevant send:
    # it must be a real HEAD, must go out uncredentialed (the token belongs to the two
    # API origins, and the render host is neither), must be pinned to the render host
    # before the socket opens, and must count against the same per-run cap.
    head_body = body_of(HTTP, r"\n    def head\(self, url\):(.*?)\nclass ProbeResponse:")
    check(head_body != "" and 'method="HEAD"' in head_body,
          "Client.head sends an actual HEAD, not a GET with the body discarded")
    check(head_body != "" and "send(request)" in head_body,
          "the HEAD probe goes through send(), so the no-redirect opener owns it")
    check(head_body != "" and "require_allowed_asset_url(" in head_body,
          "the HEAD probe is host-pinned to the asset allowlist before it is sent")
    # Quoted, because the method's own docstring is the place that explains the rule
    # and the word may appear there in prose; a header entry is what would matter.
    check(head_body != "" and '"Authorization"' not in head_body and "Bearer " not in head_body
          and "mint_token" not in head_body,
          "no credential is ever attached to, and no token minted for, a render-host HEAD")
    check(head_body != "" and "config.MAX_REQUESTS" in head_body
          and "config.USER_AGENT" in head_body and "config.REQUEST_SPACING_S" in head_body,
          "the HEAD probe honours the per-run cap, the UA and the pacing like a GET does")
    check(AVATAR_SUFFIX and FULL_RENDER_SUFFIX,
          f"the render-suffix swap is declared in config and read here "
          f"(avatar={AVATAR_SUFFIX!r} -> render={FULL_RENDER_SUFFIX!r})")
    derive_body = body_of(FETCH, r"\ndef derive_full_render_url\(avatar_url\):(.*?)\ndef ")
    check(derive_body != "" and "config.AVATAR_ASSET_SUFFIX" in derive_body
          and "config.FULL_RENDER_ASSET_SUFFIX" in derive_body
          and "safe_artifact_url(" in derive_body,
          "derive_full_render_url is a config-declared suffix swap whose result clears the H2 validator")
    check(derive_body != "" and ".endswith(" in derive_body and "return None" in derive_body,
          "a suffix miss returns None — the derivation cannot invent a URL")
    probe_body = body_of(FETCH, r"\n    def full_render_url\(self, avatar_url, character_id=None\):(.*?)\n    def ")
    check(probe_body != "" and "self.client.head(" in probe_body and "== 200" in probe_body,
          "only an explicit HTTP 200 on the derived URL publishes media.fullRender")
    check('media_block["fullRender"] = self.full_render_url(' in FETCH,
          "media.fullRender is written in the media enrichment path, next to the avatar "
          "it was derived from (URL or explicit null, never a placeholder)")

    # --- H2: response-derived URLs are validated before they are persisted ----------- #
    check('startswith("http")' not in SYNC_TEXT,
          "canonical_href no longer accepts any string that starts with http")
    href_guard = body_of(FETCH, r"\ndef canonical_href\(payload\):(.*?)\ndef ")
    check(href_guard != "" and "PROFILE_HREF_RE.match(" in href_guard,
          "canonical_href validates against the profile URL shape")
    check('safe_artifact_url(href, "href")' in ARTIFACTS,
          "shared validator gates every href write site (map entry builder)")
    check('safe_artifact_url(icon, "iconUrl")' in FETCH,
          "shared validator gates the iconUrl write site (render host only)")
    # --- Phase 4a: the two new icon routes are config-declared and share that gate --- #
    # notes §10: profession and playable-class icons both answer at the
    # /data/wow/media/<kind>/{id} prefix form, so the paths belong in config (one
    # place to re-verify after a patch) and the values go through icon_for, which is
    # the function that applies safe_artifact_url and the cache/dedup rules.
    check('PROFESSION_MEDIA_PATH = "/data/wow/media/profession/{profession_id}"' in CFG,
          "profession icon route is the config-declared /media prefix form (§10)")
    check('PLAYABLE_CLASS_MEDIA_PATH = "/data/wow/media/playable-class/{class_id}"' in CFG,
          "playable-class icon route is the config-declared /media prefix form (§10)")
    check(not re.search(r"RACE_MEDIA_PATH|PLAYABLE_RACE", CFG)
          and '"raceIconUrl"' not in FETCH,
          "no race-media endpoint or write site exists in the shipped code (§10 proved every "
          "shape unrouted, so the sync publishes no raceIconUrl key at all)")
    icon_gate = body_of(FETCH, r"\n    def static_media_icon\(self, path, surface, w, \*\*identifiers\):(.*?)\n    def ")
    check("self.icon_for(" in icon_gate,
          "the new icon routes reach the shared icon_for gate (cache reuse + H2 validator), "
          "instead of each writing a URL of its own")
    check('out["classIconUrl"] = self.class_icon(' in FETCH,
          "the class icon is written where the profile identity was extracted, not elsewhere")
    check('malformed' in body_of(FETCH, r"\ndef extract_identity\(profile\):(.*?)\ndef "),
          "extract_identity reports malformed race/class refs instead of storing or dumping them")
    # --- Phase 5b: the two static icon maps are config-declared and gated ------- #
    # Same discipline as §10's icon routes: paths live in config, the fetch goes
    # through the shared media gate (so safe_artifact_url is the only writer of a
    # URL), refusals are reported per entry, and the artifact envelope is written in
    # exactly one place.
    check('CLASS_ICON_FILE = DATA_DIR / "class-icons.json"' in CFG
          and 'PROFESSION_ICON_FILE = DATA_DIR / "profession-icons.json"' in CFG,
          "both icon-map artifact paths are declared in config (one place to re-verify after a patch)")
    check('PROFESSION_INDEX = "/data/wow/profession/index"' in CFG,
          "the profession dictionary index route is config-declared (§11: static ns, names and ids only)")
    check('"profession_index"' in CFG,
          "the profession index joins the schema-drift tripwires like every other load-bearing surface")
    check(re.search(r"ICON_MAP_KEY_MAX\s*=\s*64", CFG) is not None,
          "the icon-map key ceiling is the single config value (64)")
    check(re.search(r"def write_icon_map\(path, static_namespace, icons, \*, dry_run\):", ARTIFACTS) is not None
          and re.search(r"def load_previous_icon_map\(path\):", ARTIFACTS) is not None,
          "artifacts.py owns the icon-map envelope (single writer, the talent-tree pattern)")
    check("fetch.icon_entry(value)" in ARTIFACTS and "fetch.icon_map_key(name)" in ARTIFACTS,
          "a carried-over icon entry is re-validated (render-host gate + key gate) before it is rewritten")
    icon_map_body = body_of(FETCH, r"\n    def icon_map\(self, \*,(.*?)\n    def ")
    check(icon_map_body != "" and "self.static_media_icon(" in icon_map_body,
          "both icon maps fetch through the shared §10 media gate (cache reuse + H2 validator) "
          "instead of writing a URL of their own")
    check(icon_map_body != "" and icon_map_body.count("note_icon_map_failure") >= 3,
          "every refused icon-map entry gets its own reported line (the feature_failures pattern)")
    check("return {}, 0, 0" in icon_map_body,
          "a failed index read yields an empty map, which the caller never writes over the committed file")
    check(re.search(r"def icon_map_key\(raw\):", FETCH) is not None
          and "JS_OBJECT_KEY_HAZARDS" in FETCH and "config.ICON_MAP_KEY_MAX" in FETCH
          and '"://" in name' in FETCH,
          "an index name passes an artifact-key gate (bounded, printable, no Object.prototype hazard) "
          "before it can become a map key")
    check(re.search(r"def icon_entry\(value\):", FETCH) is not None
          and body_of(FETCH, r"\ndef icon_entry\(value\):(.*?)\ndef ").count("safe_artifact_url") == 1,
          "icon entries are gated by the shared safe_artifact_url validator")
    check("self.refresh_icon_maps(" in SYNC and "if not icons:" in SYNC,
          "the sync rebuilds both maps once per run, after the pair lands, and never writes an empty one")

    check(re.search(r"def redact_foreign_urls\(text\):", HTTP) is not None
          and re.search(r"def redact\(value\):\s*\n\s*if isinstance\(value, str\):\s*\n\s*return redact_foreign_urls\(", HTTP)
          is not None,
          "redact() withholds non-Blizzard URLs from notes, logs and artifacts alike")

    # --- H3: derived credential forms are registered with the redactor --------------- #
    check(re.search(r"class Redactor:.*?\n    def register\(self, value\):", HTTP, flags=re.S) is not None,
          "Redactor exposes a public register() for derived credential forms")
    forms_body = body_of(HTTP, r"\ndef register_mint_forms\(client_id, client_secret\):(.*?)\ndef ")
    check(forms_body != "" and "REDACTOR.register(basic)" in forms_body,
          "the base64 client_id:client_secret pair is registered with the redactor")
    check(forms_body != "" and "quote_plus" in forms_body, "URL-encoded credential forms are registered too")
    check("register_mint_forms(" in mint_body, "sync token mint registers its derived forms")
    probe_mint = body_of(HTTP, r"class ProbeHttp:.*?\n    def mint_token\(self, reason\):(.*?)\n    def ")
    check("register_mint_forms(" in probe_mint, "probe token mint registers its derived forms too")

    # --- H4: mass-flip floor blocks the write ---------------------------------------- #
    floor = re.search(r"ok_prev\s*>=\s*3.*?ok_now\s*\*\s*2\s*<\s*ok_prev", ARTIFACTS, flags=re.S)
    check(floor is not None, "mass-flip floor (ok_prev >= 3 and ok_now * 2 < ok_prev) is present in the writer")
    write_pair = body_of(ARTIFACTS, r"\ndef write_pair\((.*?)\n(?=\ndef |\Z)")
    check(write_pair != "" and "mass_flip" in write_pair
          and write_pair.index("mass_flip") < write_pair.index("write_map("),
          "the floor is evaluated before the artifacts are written (inside the single writer)")
    # The orchestrator still pre-checks so the refusal prints with its run summary,
    # but it asks the artifacts layer for the verdict: the ok_prev/ok_now counting
    # existing twice is how a caller-side floor silently drifts from the real one.
    check("artifacts.mass_flip_refusal(" in SYNC and "ok_prev" not in SYNC,
          "the H4 criterion is computed in exactly one place (sync.py counts no ok rows itself)")
    check("amendment 8" in ARTIFACTS,
          "the floor documents the upstream-404 rationale it exists for (amendment 8)")

    # --- single writer of both artifacts --------------------------------------------- #
    writers = [n for n in PKG_MODULES if n != "artifacts.py"
               and ("config.MAP_FILE" in sources[n] or "config.DATA_FILE" in sources[n])]
    check(not writers, f"artifacts.py is the only module that writes the data artifacts (writers: {writers})")
    check("def write_json(path, payload):" in ARTIFACTS and "http.redact(payload)" in ARTIFACTS,
          "the artifact writer passes every value through redact() before it hits disk")

    # --- lifecycle: trusted slugs are never re-resolved ------------------------------- #
    check(re.search(r"def confirmed_slug\(rec\):", ROSTER_MOD) is not None
          and re.search(r"def is_trusted\(rec\):", ROSTER_MOD) is not None,
          "roster exposes the confirmedSlug / validated trust helpers")
    check("roster.is_trusted(rec)" in RESOLVE and "roster.trusted_slug(rec, prev)" in RESOLVE,
          "resolve consults the trust rule before any name resolution")
    check('note="user-confirmed"' in RESOLVE, "a trusted row keeps the confirmed slug with note user-confirmed")
    check(re.search(r"def roster_records\(document\):", ROSTER_MOD) is not None
          and '"characters"' in ROSTER_MOD,
          "roster reads both the v1 array and the v2 {schemaVersion, characters} envelope")

    # --- lifecycle: a reviewer's note forces needs_review -------------------------------------- #
    note_body = body_of(ROSTER_MOD, r"\ndef reported_note\(rec\):(.*?)\ndef ")
    check(re.search(r"def reported_note\(rec\):", ROSTER_MOD) is not None
          and 'v.get("state") == "validated"' in note_body,
          "roster reads validation.note, and a validated row clears the report")
    entry_body = body_of(RESOLVE, r"\ndef reported_note_entry\(rec, entry\):(.*?)\n# ")
    check(re.search(r"def reported_note_entry\(rec, entry\):", RESOLVE) is not None
          and 'rec, "needs_review"' in entry_body and 'note=note' in entry_body
          and 'slug=entry.get("slug")' in entry_body,
          "resolve forces a reported row to needs_review, note passed through and slug kept")
    check("self.resolver.apply_reported_notes(work, ordered_ids)" in SYNC,
          "the nightly sync applies the reported-note rule on every run")
    check("resolve.reported_note_entry(rows[0], entry)" in SYNC,
          "the single-row lookup path applies the same rule (a lookup is a sync run too)")
    check(re.search(r"REPORT_NOTE_MAX\s*=\s*240", ROSTER_MOD) is not None
          and re.search(r"REVIEW_NOTE_MAX = 240", read(ROOT / "js" / "detail.js") or "") is not None,
          "the note cap is 240 in the sync and in the UI form that writes it")

    # --- lifecycle: ambiguous verdicts exist and are candidate-bearing ---------------- #
    check('def ambiguous_entry(' in ARTIFACTS and '"ambiguous"' in ARTIFACTS,
          "artifacts define the ambiguous verdict (slug null, candidates populated)")
    check("artifacts.ambiguous_entry(" in RESOLVE, "resolve emits ambiguous verdicts instead of picking")
    check("def candidate_entry(" in ARTIFACTS and '"candidates": list(candidates or [])' in ARTIFACTS,
          "every map entry emits the v2 candidates list")

    # --- provenance -------------------------------------------------------------------- #
    check('def triggered_by(' in ARTIFACTS and 'GITHUB_EVENT_NAME' in ARTIFACTS and 'GITHUB_ACTOR' in ARTIFACTS,
          "triggeredBy resolves schedule / GITHUB_ACTOR / manual from the runner environment")
    check("triggered_by_value=artifacts.triggered_by()" in SYNC,
          "the sync run records provenance on the sidecar it writes")

    # --- workflow: verify gate runs between sync and commit -------------------------- #
    if wf:
        steps = workflow_steps(wf)
        joined = [f"{name}\n{body}" for name, body in steps]
        test_at = next((i for i, t in enumerate(joined) if "unittest" in t or "test_bnet_phase2" in t), None)
        sync_at = next((i for i, t in enumerate(joined) if "-m bnet sync" in t), None)
        verify_at = next((i for i, t in enumerate(joined) if "verify_sync_artifacts.py" in t), None)
        commit_at = next((i for i, t in enumerate(joined) if "git commit" in t), None)
        check(test_at is not None, "workflow runs the offline unit tests as a step")
        check(sync_at is not None, "workflow has a step that runs python3 -m bnet sync")
        check(verify_at is not None, "workflow runs .pi/scripts/verify_sync_artifacts.py as a step")
        check(commit_at is not None, "workflow has a commit step")
        check(test_at is not None and sync_at is not None and verify_at is not None and commit_at is not None
              and test_at < sync_at < verify_at < commit_at,
              f"step order is test -> sync -> verify -> commit (got test={test_at} sync={sync_at} "
              f"verify={verify_at} commit={commit_at})")
        if test_at is not None:
            # The unit tests are the one step that must never need a credential:
            # they exercise the redactor/allowlist offline, before the sync step is
            # ever handed the Blizzard secrets.
            check("secrets." not in joined[test_at] and "if:" not in joined[test_at]
                  and "continue-on-error" not in joined[test_at],
                  "the unit-test step is unconditional, skippable-by-nothing and secret-free")
        timeout = re.search(r"^(\s*)timeout-minutes:(\s*)(\d+)\s*$", wf, flags=re.M)
        check(timeout is not None and int(timeout.group(3)) == 15 and len(timeout.group(1)) == 4,
              f"the sync job declares job-level timeout-minutes: 15 so a hung run cannot hold "
              f"the concurrency group forever (found: {timeout.group(0).strip() if timeout else 'none'})")
        if verify_at is not None:
            check("if:" not in joined[verify_at] and "continue-on-error" not in joined[verify_at],
                  "the verify gate cannot be skipped or tolerated (no if:/continue-on-error)")

    # --- pages.yml: the static deploy workflow (design §4) -------------------------- #
    # Phase 4 replaces the Tailwind Play CDN with a Pages deploy. This workflow holds
    # no credential, so its own posture is the reviewable surface: no PR trigger, the
    # three deploy-pages scopes and nothing else, one pinned build step, an explicit
    # allow-list bundle, and the official upload/deploy actions.
    pages = scanned[".github/workflows/pages.yml"]
    if pages is not None:
        # Comments explain why the deploy reacts to a push; scan the YAML only.
        ptrig = "\n".join(ln for ln in pages.split("jobs:", 1)[0].splitlines()
                          if not ln.lstrip().startswith("#"))
        check("pull_request" not in ptrig, "pages.yml has no pull_request trigger")
        check(re.search(r"^\s*push:", ptrig, flags=re.M) is not None
              and re.search(r"branches:\s*\[main\]", ptrig) is not None,
              "pages.yml deploys on push to main (the path the sync's [skip ci] commit skips)")
        check("workflow_run:" in ptrig and 'workflows: ["Sync Battle.net data"]' in ptrig
              and re.search(r"types:\s*\[completed\]", ptrig) is not None
              and len(re.findall(r"branches:\s*\[main\]", ptrig)) >= 2,
              "pages.yml chains off the sync-bnet workflow_run (completed, main)")
        check("workflow_dispatch:" in ptrig, "pages.yml allows manual dispatch")
        check("secrets." not in pages and "secrets:" not in pages,
              "pages.yml references no secret: a static build needs none")
        pairs = set(permission_pairs(pages))
        allowed = {("contents", "read"), ("pages", "write"), ("id-token", "write")}
        check(bool(pairs) and pairs <= allowed,
              f"pages.yml permissions are minimal ({sorted(allowed)} at most; found {sorted(pairs)})")
        check(("pages", "write") in pairs and ("id-token", "write") in pairs,
              "pages.yml grants the pages:write + id-token:write actions/deploy-pages authenticates with")
        check(("contents", "write") not in pairs and "permissions: write" not in pages
              and "read-all" not in pages,
              "pages.yml can never push: no contents:write and no permissions shorthand")
        check(re.search(r"npx --yes tailwindcss@3\.4\.\d+", pages) is not None,
              "pages.yml builds with the pinned v3 Tailwind CLI (v3-style tailwind.config.js)")
        check("-c tailwind.config.js" in pages and "-o css/tailwind.css" in pages
              and "--minify" in pages,
              "the Tailwind step compiles tailwind.config.js to css/tailwind.css with --minify")
        check(re.search(r"wc -c < css/tailwind\.css", pages) is not None and "10240" in pages,
              "a stub-sized stylesheet fails the build instead of deploying an unstyled site")
        check(re.search(r"^\s*path:\s*_site\s*$", pages, flags=re.M) is not None
              and not re.search(r"^\s*path:\s*\.\s*$", pages, flags=re.M),
              "the Pages artifact is the assembled _site bundle, never the repository root")
        # Phase B: assets/silhouettes/*.svg is the one published asset path, and it is
        # published as a flat glob of SVGs only — the allow-list gains that exact shape and
        # nothing wider, so a future `cp -r assets _site/assets` (or any other asset tree)
        # still trips this check.
        copied = [ln.strip() for ln in pages.splitlines() if re.match(r"^\s*cp ", ln)]
        check(bool(copied)
              and all(re.search(r"(index\.html|css/\*\.css|js/\*\.js|data/\*\.json"
                                r"|assets/silhouettes/\*\.svg) _site/", ln) for ln in copied),
              f"the bundle copies only index.html, css/, js/, data/ and "
              f"assets/silhouettes/*.svg ({copied})")
        for forbidden in ("docs", "tools", ".pi", "armory-tasks.md", "pi-warden.md"):
            check(not re.search(rf"cp[^\n]*{re.escape(forbidden)}", pages),
                  f"the bundle never publishes the maintainer-facing {forbidden}/")
        check("actions/upload-pages-artifact@v" in pages and "actions/deploy-pages@v" in pages
              and "actions/configure-pages@v" in pages,
              "deploy goes through the official configure-pages + upload-pages-artifact + deploy-pages actions")
        check(re.search(r"environment:\s*\n\s+name:\s*github-pages", pages) is not None
              and "steps.deployment.outputs.page_url" in pages and "needs: build" in pages,
              "the deploy job waits for build, uses the github-pages environment and exports page_url")
        job_names = re.findall(r"^  ([a-zA-Z][\w-]*):\s*$", pages.split("jobs:", 1)[1], flags=re.M)
        timeouts = re.findall(r"^    timeout-minutes:\s*(\d+)\s*$", pages, flags=re.M)
        check(len(job_names) >= 2 and len(timeouts) == len(job_names),
              f"every pages.yml job declares timeout-minutes (jobs={job_names}, timeouts={timeouts})")
        check("head_sha" in pages and re.search(r"default branch", pages, flags=re.I) is not None,
              "pages.yml documents why a workflow_run deploy takes the default-branch head "
              "(the synced commit) rather than workflow_run.head_sha")
        # Step order across the workflow: compile, then gate the compile, then stage
        # the bundle, then hand it to Pages. A swapped pair (upload before build) would
        # publish the committed stub instead of the generated stylesheet.
        def at(pattern):
            m = re.search(pattern, pages, flags=re.M)
            return pages[:m.start()].count("\n") if m else None
        tw_at, gate_at, stage_at, upload_at, deploy_at = (
            at(r"npx --yes tailwindcss@"), at(r"wc -c < css/tailwind\.css"),
            at(r"mkdir -p _site"), at(r"actions/upload-pages-artifact@"),
            at(r"actions/deploy-pages@"))
        check(None not in (tw_at, gate_at, stage_at, upload_at, deploy_at)
              and tw_at < gate_at < stage_at < upload_at < deploy_at,
              f"step order is tailwind -> size gate -> stage -> upload -> deploy "
              f"(got {tw_at}, {gate_at}, {stage_at}, {upload_at}, {deploy_at})")

    # The vocabulary gate must be able to see anything at all: a silently empty
    # vocabulary would soft-pass every row forever, so it is a hard requirement.
    check(bool(CLASS_VOCAB) and bool(RACE_VOCAB),
          f"pi-warden vocabularies loaded from js/ (classes={len(CLASS_VOCAB)}, "
          f"races={len(RACE_VOCAB)})")
    print(f"\n{passes} passed, {len(fails)} failed")
    if warnings:
        print(f"{len(warnings)} WARNING(s) (advisory, not failures):")
        for item in warnings:
            print(f"  ! {item}")
    if fails:
        print("FAILED CHECKS:")
        for item in fails:
            print(f"  - {item}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
