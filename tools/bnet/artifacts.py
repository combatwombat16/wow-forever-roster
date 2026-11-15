"""Map/data artifact writers: the schemas of record, plus the H4 mass-flip floor.

Provenance: ``write_json``, the fixed map-entry field set (``map_entry`` /
``ok_entry``) and the H4 floor all come from ``tools/sync_bnet.py``; the
``candidates`` list, the ``ambiguous`` verdict and the ``triggeredBy`` provenance
key are the Phase 2 lifecycle additions of docs/refactor-design.md §3.

This module is the ONLY writer of ``data/battle-net-map.json``, of the schema-v3 character
sidecar (``data/character-data.json`` — the manifest — and every ``data/character-data/<id>.json``
detail document beside it), of the roster it appends pending rows to
(``write_roster``, Phase C: additive only, and only when a row was actually added),
and of the reference artifacts beside them
(``data/talent-trees.json`` since T1.3, ``data/class-icons.json`` and
``data/profession-icons.json`` since Phase 5b). Every key either module emits is
listed in ``MAP_ENTRY_KEYS`` / ``CANDIDATE_KEYS`` / ``CHARACTER_KEYS`` (the whole record a
``Fetcher.enrich`` builds, which is what every caller hands this module) /
``MANIFEST_ROW_KEYS`` + ``DETAIL_DOC_KEYS`` (the two files that record is SPLIT across) /
``TALENT_TREES_DOC_KEYS`` / ``ICON_MAP_DOC_KEYS`` so a reviewer can diff two runs line by line and
the verify script can assert exact key sets.
"""

import json
import re
import os

from . import config, fetch, http

SCHEMA_VERSION = 2

# The character sidecar's own schema version, deliberately its OWN constant rather than
# ``SCHEMA_VERSION``: schema v3 (docs/refactor-design.md §3) is a change to the LAYOUT of
# ``data/character-data.json`` and its new ``data/character-data/`` directory, and nothing else.
# The map and the roster keep ``SCHEMA_VERSION`` (2) — bumping them by association would tell
# every reader "the identity contract changed" when it did not.
DATA_SCHEMA_VERSION = 3

# ``data/characters.json`` envelope keys. The roster keeps its own schemaVersion rather than
# inheriting the artifact constant by name because a hand-edit writes this shape too; the two
# happen to be 2 today (verify_sync_artifacts.py gates both).
ROSTER_DOC_KEYS = ("schemaVersion", "characters")

# Status vocabulary (v2). ``ambiguous`` is new: multiple plausible resolutions,
# slug null, candidates populated. ``needs_review`` survives for the roster-side
# conflicts (duplicate normalized names, one slug claimed by two ids) and the
# user "not my character" report path (design §2).
STATUS_VOCAB = ("ok", "ambiguous", "not_found", "needs_review", "fetch_error")

MAP_DOC_KEYS = ("schemaVersion", "updatedAt", "entries")
MAP_ENTRY_KEYS = ("rosterName", "status", "slug", "href", "bnetName", "surname",
                  "matchedForm", "mappedVia", "note", "candidates")
CANDIDATE_KEYS = ("slug", "name", "surname", "matchedForm", "href")

DATA_DOC_KEYS = ("schemaVersion", "syncedAt", "triggeredBy", "characters")
# Phase 1 modal rework (T1.1–T1.5) is ADDITIVE: every key the sidecar already
# published keeps its exact meaning, and the five new keys (stats, talents,
# professions, reputations, media) are optional whole blocks — present only when
# the payload yielded data, exactly like guild/progress.
CHARACTER_KEYS = ("status", "bnet", "level", "guild", "activeSpec", "itemLevel",
                  "gear", "progress", "achievementPoints", "fetchedAt",
                  "stats", "talents", "professions", "reputations", "media",
                  # Phase 4a (notes §10): identity straight off the profile payload,
                  # plus the class icon the probe proved exists. raceIconUrl is a legal
                  # key that NOTHING writes: §10 found no race media route on any
                  # namespace, so the sidecar omits it instead of publishing a
                  # permanent null, and the verify gate warns if a row ever grows one.
                  "race", "class", "classIconUrl", "raceIconUrl")
# --------------------------------------------------------------------------- #
# Schema v3: the manifest row and the per-character detail document
# --------------------------------------------------------------------------- #
# WHY A SPLIT (design §3): the single sidecar grew with the guild — 529 KB for 17 resolved
# characters on 2026-11-14, ~31 KB of it per character — while every roster CARD reads four
# scalars out of it (level, itemLevel, guild, fetchedAt) on every repaint. So the row was cut on
# the line the UI already draws: the four scalars plus the identity the modal header shows stay
# inline, and the heavy per-character payload moves to a file the modal asks for when it opens.
# NO field changes meaning and no value is produced here: both halves are slices of the same
# ``Fetcher.enrich`` record, keyed by the same constants, written by the same ``write_json``.
#
#   MANIFEST_ROW_KEYS — ``data/character-data.json``'s ``characters[id]``. ``detail`` is present
#     for an ``ok`` row and ABSENT (never null, never "") for every other status: a row that was
#     never enriched has no detail file to name, and a null would be a path the browser fetches.
#   DETAIL_DOC_KEYS — ``data/character-data/<id>.json``. It carries NO ``status`` and none of the
#     manifest's own fields; ``js/data.js`` re-pins ``status`` from the manifest after the merge,
#     so a detail file cannot re-lifecycle a row the manifest calls needs_review.
MANIFEST_ROW_KEYS = ("status", "fetchedAt", "level", "itemLevel", "guild", "activeSpec",
                     "classIconUrl", "race", "class", "detail")
# The manifest fields, in the order they are written, minus the derived ``detail`` pointer.
MANIFEST_INLINE_KEYS = tuple(k for k in MANIFEST_ROW_KEYS if k != "detail")
DETAIL_DOC_KEYS = ("schemaVersion", "id", "bnet", "gear", "progress", "achievementPoints",
                   "stats", "talents", "professions", "reputations", "media")
# The detail document's body: DETAIL_DOC_KEYS without its own envelope pair. This is also the
# filter ``load_previous_data`` uses when it folds a detail file back into the record the
# enrichment loop works on — ``schemaVersion``/``id`` are file metadata, never row fields.
DETAIL_BODY_KEYS = tuple(k for k in DETAIL_DOC_KEYS if k not in ("schemaVersion", "id"))
# Every key of the pre-v3 record must land in exactly one of the two halves. Nothing here can
# invent or drop a value: the assertion is in ``split_characters``, and
# tools/tests/test_sidecar_v3.py pins it against the committed v2 rows.
SIDECAR_PARTITION = (MANIFEST_INLINE_KEYS, DETAIL_BODY_KEYS)

BNET_KEYS = ("name", "surname", "href")
# media (T1.5 + Phase B): the two avatar renders character-media serves, plus the
# full-body render the gear paper doll is drawn around. CONVENTION, stated once here,
# PERMITTED AND SHAPE-CHECKED in .pi/scripts/verify_sync_artifacts.py — the gate admits the
# key and validates every value it carries, and deliberately does NOT gate PRESENCE:
#
#   * ``fullRender`` is OPTIONAL in the committed artifact — every row written before
#     Phase B simply has no such key, and the gate must keep accepting that, so a
#     presence rule is exactly what it cannot be;
#   * a media block written FROM Phase B on is written WITH the key by this package
#     (bnet.fetch.enrich sets it unconditionally), as a render-host URL or as an explicit
#     null. Null is a published statement ("derived and HEAD-checked this run, nothing
#     served"), never a placeholder URL — and the gate checks the shape of a value that is
#     there, not the fact that it is;
#   * a row with no character-media payload has no media block, so it has no
#     fullRender key either — the absence of the block, not a null, says "we never
#     got media".
MEDIA_KEYS = ("avatar", "avatarSd", "fullRender")
# race / class: {id: int, name: str} read off the profile (§3.1), and a profession
# entry now carries the §10 icon next to its id and name.
IDENTITY_KEYS = ("id", "name")
# gear keeps slot/name/ilvl/iconUrl and gains the T1.1 tooltip fields; no
# gem/socket key exists because the API carries none (notes §9.4, amendment 2).
GEAR_KEYS = ("slot", "name", "ilvl", "iconUrl",
             "itemId", "quality", "itemClass", "itemSubclass", "enchants", "stats")
# data/talent-trees.json (T1.3): shared spec tree geometry, version-pinned to
# the static namespace it was fetched from, keyed by spec id.
TALENT_TREES_DOC_KEYS = ("staticNamespace", "updatedAt", "specs")
TALENT_TREE_KEYS = ("treeId", "classId", "className", "specId", "specName", "name", "nodes")
TALENT_NODE_KEYS = ("id", "displayRow", "displayCol", "rawPositionX", "rawPositionY",
                    "unlocks", "lockedBy", "ranks")
TALENT_RANK_KEYS = ("rank", "talentId", "spellId", "spellName", "description", "castTime", "iconUrl")
# data/class-icons.json and data/profession-icons.json (Phase 5b, notes §11): the
# talent-tree envelope reused. The keys are Blizzard's own class / profession
# names (bnet.fetch.icon_map_key is what decides that a name may be a key at all),
# and an entry exists only when its icon resolved on the render host, so iconUrl is
# never null here — unlike the per-character icon fields.
ICON_MAP_DOC_KEYS = ("staticNamespace", "updatedAt", "icons")
ICON_ENTRY_KEYS = ("id", "iconUrl")



def dumps(payload):
    """THE artifact serialisation: redacted, indented, key order preserved, newline-terminated.

    Split out of ``write_json`` so a reader (``tools/migrate_v3.py``, the tests) can ask "what
    bytes would this document produce?" without writing them — the one way to compare a planned
    artifact with a committed one without a second copy of the formatting rules that could drift."""
    return json.dumps(http.redact(payload), ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write_json(path, payload):
    path.write_text(dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Map entry builders (fixed field set)
# --------------------------------------------------------------------------- #
def candidate_entry(*, slug, name=None, surname=None, matched_form=None, href=None):
    """One plausible resolution. H2: the href is validated like any other write."""
    return {
        "slug": slug if isinstance(slug, str) and slug else None,
        "name": name if isinstance(name, str) and name else None,
        "surname": surname if isinstance(surname, str) and surname else None,
        "matchedForm": matched_form if isinstance(matched_form, str) and matched_form else None,
        "href": fetch.safe_artifact_url(href, "href"),
    }


def map_entry(rec, status, *, slug=None, href=None, bnet_name=None, surname=None,
              matched_form=None, mapped_via=None, note=None, candidates=None):
    """Fixed field set so a reviewer can diff two runs line by line.

    Every map row is written through here, so this is the H2 write site for hrefs:
    anything that is not a Blizzard realmless profile URL is stored as null.
    ``candidates`` is mandatory in schema v2 and empty whenever the status makes
    candidate enumeration not applicable."""
    return {
        "rosterName": rec["name"],
        "status": status,
        "slug": slug,
        "href": fetch.safe_artifact_url(href, "href") if href is not None else None,
        "bnetName": bnet_name,
        "surname": surname,
        "matchedForm": matched_form,
        "mappedVia": mapped_via,
        "note": note,
        "candidates": list(candidates or []),
    }


def ok_entry(rec, *, slug, href, payload, matched_form, mapped_via, note=None):
    # name/surname come from the payload, never the roster guess (task 2.3).
    return map_entry(rec, "ok", slug=slug, href=href,
                     bnet_name=payload.get("name") if isinstance(payload.get("name"), str) else None,
                     surname=payload.get("surname") if isinstance(payload.get("surname"), str) else None,
                     matched_form=matched_form, mapped_via=mapped_via, note=note)


def ambiguous_entry(rec, *, candidates, note, matched_via=None):
    """Design §3: multiple plausible resolutions — never auto-pick, never a slug.

    ``href``/``bnetName``/``surname`` stay null so no downstream consumer can
    mistake an unresolved id for a mapped identity; the plausible profiles live
    in ``candidates`` for the reviewer to choose from."""
    return map_entry(rec, "ambiguous", slug=None, href=None, bnet_name=None, surname=None,
                     matched_form=None, mapped_via=matched_via, note=note, candidates=candidates)


# --------------------------------------------------------------------------- #
# Readers (previous run state)
# --------------------------------------------------------------------------- #
def load_previous_map(path=None):
    """Previous entries. schemaVersion 1 documents are read as-is: the v1 shape
    differs only by the missing ``candidates`` key, and every consumer below uses
    ``.get()`` so the transition needs no back-fill here."""
    path = path or config.MAP_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! previous battle-net-map.json unreadable ({type(exc).__name__}); rebuilding it whole")
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}


# --------------------------------------------------------------------------- #
# Provenance (design §3): who caused this run
# --------------------------------------------------------------------------- #
def triggered_by(env=None):
    """"schedule" for the nightly cron, the GitHub login for a dispatch, else
    "manual" (a maintainer at a shell).

    GITHUB_EVENT_NAME is checked first: on a scheduled run GitHub also sets
    GITHUB_ACTOR, and a login must never be recorded as the cause of a cron run
    (nor the reverse — the point of the field is provenance)."""
    env = env if env is not None else os.environ
    if env.get("GITHUB_EVENT_NAME", "").strip() == "schedule":
        return "schedule"
    actor = env.get("GITHUB_ACTOR", "").strip()
    return actor or "manual"


# --------------------------------------------------------------------------- #
# Readers used by the incremental (lookup) write path
# --------------------------------------------------------------------------- #
def load_previous_talent_trees(path=None):
    """The committed data/talent-trees.json document, or {} when absent/unreadable.

    Trees are version-pinned static docs (§9.3.2): when the pinned static
    namespace still matches, a re-run reuses them and spends zero requests; a
    patch (new static ns) makes the Fetcher ignore the old geometry wholesale."""
    path = path or config.TALENT_TREES_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! previous talent-trees.json unreadable ({type(exc).__name__}); rebuilding it whole")
        return {}
    return raw if isinstance(raw, dict) else {}


def write_talent_trees(static_namespace, specs, *, dry_run):
    """Write data/talent-trees.json (never in dry-run) and return the document.

    Merging rather than replacing means a lookup run for one character cannot
    erase the trees every other spec already depends on; when the namespace
    changed the pinned old geometry is worthless, so it is dropped, not merged."""
    payload_specs = {}
    previous = load_previous_talent_trees()
    if previous.get("staticNamespace") == static_namespace and isinstance(previous.get("specs"), dict):
        payload_specs.update({str(k): v for k, v in previous["specs"].items() if isinstance(v, dict)})
    payload_specs.update({str(k): v for k, v in (specs or {}).items() if isinstance(v, dict)})

    def sort_key(spec_id):
        return (0, int(spec_id)) if spec_id.isdigit() else (1, 0)

    payload = {"staticNamespace": static_namespace,
               "updatedAt": http.iso_now(),
               "specs": {k: payload_specs[k] for k in sorted(payload_specs, key=sort_key)}}
    if not dry_run:
        write_json(config.TALENT_TREES_FILE, payload)
    return payload


def load_previous_icon_map(path):
    """A previous icon-map document (class or profession), or {} when absent/unreadable.

    Same contract as ``load_previous_talent_trees``: the pinned static namespace is
    what decides whether the old entries are still worth anything, and this reader
    only hands the document over — ``write_icon_map`` makes that call."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! previous {path.name} unreadable ({type(exc).__name__}); rebuilding it whole")
        return {}
    return raw if isinstance(raw, dict) else {}


def write_icon_map(path, static_namespace, icons, *, dry_run):
    """Write one static icon-map artifact (never in dry-run) and return the document.

    Envelope identical to ``write_talent_trees`` on purpose: the site already knows
    how to treat "pinned to this static namespace, updated at this time", and the
    merge rule is the same one — entries are carried forward by NAME only while the
    pinned namespace is unchanged, and a patch drops the old map instead of merging
    geometry from two versions. Merging rather than replacing means a run whose
    index read came back short cannot erase icons the site is already rendering
    (bnet.fetch.Fetcher.icon_map builds the map from the whole index, so in a healthy
    run the merge only adds names the index no longer lists).

    Every value — fresh or carried — goes through ``fetch.icon_entry``, so an
    off-host URL in a hand-edited committed file cannot survive a re-write.
    """
    payload_icons = {}
    previous = load_previous_icon_map(path)
    if previous.get("staticNamespace") == static_namespace and isinstance(previous.get("icons"), dict):
        for name, value in previous["icons"].items():
            entry = fetch.icon_entry(value)
            key = fetch.icon_map_key(name)
            if entry and key:
                payload_icons[key] = entry
    for name, value in (icons or {}).items():
        entry = fetch.icon_entry(value)
        key = fetch.icon_map_key(name)
        if entry and key:
            payload_icons[key] = entry
    payload = {"staticNamespace": static_namespace,
               "updatedAt": http.iso_now(),
               "icons": {k: payload_icons[k] for k in sorted(payload_icons, key=lambda n: (n.casefold(), n))}}
    if not dry_run:
        write_json(path, payload)
    return payload


# The id charset detail_ref accepts, and the shape js/data.js characterDetailPath accepts for a
# detail basename. Kept next to the rule so the two cannot drift apart silently.
DETAIL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def detail_ref(rid):
    """The ``detail`` string for one roster id: ``character-data/<id>.json``, and nothing else.

    THE rule, in one place. It is built from ``config.DETAIL_DIR_NAME`` and the id — never from
    caller-supplied text — and the id is refused unless it is a single safe path segment, so the
    result cannot be ``..``, ``%2e%2e``, a backslash path, an absolute URL or another character's
    file. ``roster.py`` only requires a non-empty string id, and the auto-adder's ids happen to be
    13 digits; "happens to be" is not a guard, and this value is used to build a FILESYSTEM PATH as
    well as a URL, so the charset is checked here rather than trusted upstream. ``js/data.js``
    ``characterDetailPath`` rebuilds the same string from the same two parts and refuses a row whose
    authored value disagrees; the gate compares the committed value against this same shape.
    Three readers, one rule.
    """
    text = str(rid)
    if not DETAIL_ID_RE.fullmatch(text):
        raise ValueError(f"roster id {text!r} is not a single safe path segment "
                         f"([A-Za-z0-9_-]{{1,64}}); refusing to build a detail path from it")
    return f"{config.DETAIL_DIR_NAME}/{text}.json"


def detail_file(rid, detail_dir=None):
    """The filesystem path a detail document is written to (and read from)."""
    return (detail_dir or config.DETAIL_DIR) / f"{rid}.json"


def manifest_row(rec, rid=None):
    """One manifest row out of a full sidecar record: the list-view fields, and a pointer.

    Values are copied, never recomputed, and a key the record does not carry is OMITTED rather
    than written as null — that is the pre-v3 rule for the optional blocks (guild/progress) and it
    keeps a non-ok row minimal, exactly as ``sync.run_sync`` builds it today. The key order is the
    constant's, so two runs that agree on values agree on bytes.

    ``rid`` is the roster id this row is keyed by, because that is what the ``detail`` pointer is
    built from; a bare ``manifest_row(rec)`` reads it off the record (``rec["id"]``), which is the
    shape the migration and the tests hand over. It is NEVER read off the record when the caller
    knows better: the key the row is stored under is the identity, and a record that claimed a
    different one would otherwise publish a pointer to another character's file.

    ``detail`` is written for a row the run actually enriched (``status == "ok"``) and left off
    otherwise: a not_found/ambiguous/fetch_error row has no detail document, and an absent key is
    what ``js/data.js`` reads as "nothing to fetch" (a null would be fetched as the literal string
    "null" by a browser joining it onto the sidecar directory).
    """
    rec = rec if isinstance(rec, dict) else {}
    row = {key: rec[key] for key in MANIFEST_INLINE_KEYS if key in rec}
    if rec.get("status") == "ok":
        ident = rid if rid is not None else rec.get("id")
        if ident is None:
            raise ValueError("manifest_row: an ok record needs an id to point its detail file at "
                             "(pass the roster id, or carry one on the record)")
        row["detail"] = detail_ref(ident)
    return row


def detail_document(rid, rec):
    """One ``data/character-data/<id>.json`` document out of a full sidecar record.

    ``id`` is the roster id and equals the filename stem, which is what lets the gate (and the
    browser, which rebuilds the path from the id it already has) prove a document is the one its
    manifest row pointed at rather than somebody else's. ``status`` and every manifest-owned field
    are excluded by construction: the manifest is authoritative for them, and
    ``js/data.js`` re-pins ``status`` over whatever a detail file claims."""
    rec = rec if isinstance(rec, dict) else {}
    doc = {"schemaVersion": DATA_SCHEMA_VERSION, "id": str(rid)}
    for key in DETAIL_BODY_KEYS:
        if key in rec:
            doc[key] = rec[key]
    return doc


def split_characters(characters):
    """``{id: full record}`` -> ``({id: manifest row}, {id: detail document})``.

    The partition is asserted, not assumed: a record key that belongs to neither half is a value
    this writer would silently drop, so it is refused here — at the single write site, on the way
    to disk — rather than discovered in a diff after a nightly. (``raceIconUrl`` is the one
    historical key outside both halves; §10 proved no race-media route exists, so nothing has ever
    written it, and the gate keeps warning if a row ever grows one.)
    """
    rows, details, orphaned = {}, {}, set()
    covered = set(MANIFEST_INLINE_KEYS) | set(DETAIL_BODY_KEYS)
    for rid, rec in (characters or {}).items():
        if not isinstance(rec, dict):
            rows[rid] = {"status": None, "fetchedAt": None}
            continue
        orphaned |= {k for k in rec if k not in covered}
        rows[rid] = manifest_row(rec, rid)
        if rec.get("status") == "ok":
            details[rid] = detail_document(rid, rec)
    if orphaned:
        raise ValueError(f"sidecar record key(s) {sorted(orphaned)} belong to neither the manifest "
                         "nor the detail document: bnet.artifacts would drop them on write, so the "
                         "write is refused (add the key to MANIFEST_ROW_KEYS or DETAIL_DOC_KEYS)")
    return rows, details


def manifest_document(rows, *, triggered_by_value, synced_at=None):
    """The manifest envelope: the four v2 keys, with ``schemaVersion`` at v3."""
    return {"schemaVersion": DATA_SCHEMA_VERSION, "syncedAt": synced_at or http.iso_now(),
            "triggeredBy": triggered_by_value, "characters": rows}


def read_detail_document(rid, detail_dir=None):
    """One committed detail document, or None when it is absent/unreadable/not an object.

    Read-side mirror of the writer, and deliberately id-derived rather than pointer-derived: the
    caller hands an id, so a manifest row poisoned into pointing elsewhere cannot make this read a
    different character's file."""
    path = detail_file(rid, detail_dir)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! previous detail file {path.name} unreadable ({type(exc).__name__}); "
              "treating this character as having no detail")
        return None
    return doc if isinstance(doc, dict) else None


def load_previous_data(path=None, detail_dir=None):
    """Previous sidecar records, keyed by roster id, in the FULL pre-v3 shape.

    Lookup mode republishes one character at a time, but the sidecar is written as
    a whole document (and the UI joins it to the map by id), so a lookup has to
    carry every other row forward. Under v3 "every other row" is a manifest row PLUS its detail
    file: reading the manifest alone would hand ``write_data`` a record with no gear/stats/talents,
    and the next lookup would publish a manifest whose detail files it had just pruned. So each
    row is folded back together here, which makes the read the exact inverse of
    :func:`split_characters` and a lookup a no-op for every character it did not touch.

    A row with no ``detail`` key (a non-ok row, or a pre-v3 document being read before the
    migration) is returned as it stands. A missing/unreadable file means an empty dict:
    the same "rebuild it whole" behaviour ``load_previous_map`` documents."""
    path = path or config.DATA_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! previous character-data.json unreadable ({type(exc).__name__}); rebuilding it whole")
        return {}
    characters = raw.get("characters") if isinstance(raw, dict) else None
    if not isinstance(characters, dict):
        return {}
    records = {}
    for rid, row in characters.items():
        if not isinstance(row, dict):
            continue
        merged = {k: v for k, v in row.items() if k != "detail"}
        if isinstance(row.get("detail"), str) and row["detail"] == detail_ref(rid):
            doc = read_detail_document(rid, detail_dir)
            if doc:
                for key in DETAIL_BODY_KEYS:
                    if key in doc:
                        merged[key] = doc[key]
        records[rid] = merged
    return records


# --------------------------------------------------------------------------- #
# H4 mass-flip floor
# --------------------------------------------------------------------------- #
def ok_count(entries, ordered_ids):
    """How many of ``ordered_ids`` are ``ok`` in ``entries``.

    Both numbers the floor compares are counted here so the caller's pre-check and
    the writer can never disagree about what "healthy" means for a given pair."""
    return sum(1 for rid in ordered_ids
               if isinstance(entries.get(rid), dict) and entries[rid].get("status") == "ok")


def resolver_verdict(entries, ordered_ids):
    """An immutable snapshot of each entry's status, taken before the reviewer-note pass.

    The H4 floor evaluates the *resolver verdict* rather than the final entries. A reviewer's
    open report is a deliberate human action: if its ``ok`` -> ``needs_review`` demotions counted
    here, a mass report (more than half of the previously-ok rows) would make every later run
    refuse to publish — the pipeline frozen by the very feature that is meant to be advisory — and
    the report would then go unapplied in exactly the case it was written for. A genuine
    Blizzard-side 404 storm still trips the floor, because that is the verdict being measured.

    The snapshot copies the status instead of the entry, so the enrichment loop (which rewrites
    entries in place on a budget stop or a crash) cannot reach back into the verdict the floor was
    decided on, and ``write_pair`` re-checks that same verdict rather than the post-note map.
    """
    verdict = {}
    for rid in ordered_ids:
        entry = entries.get(rid)
        verdict[rid] = {"status": entry.get("status") if isinstance(entry, dict) else None}
    return verdict


def mass_flip_refusal(entries, ordered_ids, prev_entries):
    """The H4 verdict for a candidate write: the fatal message, or None.

    Single source of the criterion: the orchestrator calls this only so it can
    print the refusal with its run summary, and ``write_pair`` calls the very same
    function before touching disk. Neither side re-implements the counting."""
    return mass_flip_error(ok_count(prev_entries, ordered_ids), ok_count(entries, ordered_ids))


def mass_flip_error(ok_prev, ok_now):
    """Return the fatal message when the run is far less healthy than what is
    already published, else None.

    Why this exists: a Blizzard-wide 404 storm is a live upstream bug (amendment
    8: EU Forever character endpoints 404ing since Nov 6, and US can follow), and
    a 404 is the *legitimate* not_found path — exit 0, no fetch_error anywhere.
    The all-fail guard in the orchestrator only looks at fetch_error, so before
    this floor one bad upstream night silently unpublished every enriched row:
    the site went grey and the committed map lost four good identities. Statuses
    are only overwritten when the run is at least half as healthy as what is
    already published; below that the previous artifacts are the more truthful
    document, so we keep them and fail the job loudly instead of writing.
    """
    if ok_prev >= 3 and ok_now * 2 < ok_prev:
        return (f"FATAL: mass status flip — {ok_prev} ok rows published before this run, "
                f"{ok_now} ok now (floor: refuse below half of the previous ok count). "
                "battle-net-map.json and character-data.json were NOT written; suspect "
                "upstream 404 instability (amendment 8), not a real roster change.")
    return None


def write_map(entries, *, dry_run):
    payload = {"schemaVersion": SCHEMA_VERSION, "updatedAt": http.iso_now(), "entries": entries}
    if not dry_run:
        write_json(config.MAP_FILE, payload)
    return payload


def roster_append_refusal(previous, records):
    """The additive-only verdict for a roster write: the fatal message, or None.

    Phase C makes the nightly a writer of ``data/characters.json``, the one data file a
    human edits by hand. Its whole safety property is that it may only APPEND: every row
    that was there when the run started must still be there, at the same index, with the
    same keys in the same order and equal values. Checked before ``write_pair`` rather than
    at the writer, because a roster the sync has no right to write must not be published
    alongside a map and sidecar that were."""
    prev = list(previous or [])
    rows = list(records or [])
    if len(rows) < len(prev):
        return (f"FATAL: auto-add would remove roster rows ({len(prev)} at start of run, "
                f"{len(rows)} now); characters.json was NOT written.")
    for index, before in enumerate(prev):
        after = rows[index]
        if before != after or list(before or []) != list(after or []):
            rid = before.get("id") if isinstance(before, dict) else None
            return (f"FATAL: auto-add would mutate roster row {index} (id {rid}); existing "
                    "rows are never rewritten, so characters.json was NOT written.")
    return None


def write_roster(records, *, dry_run):
    """Write ``data/characters.json`` in its schemaVersion-2 envelope (Phase C).

    Only ever called with the run's rows after :func:`roster_append_refusal` cleared them,
    and only when a row was actually added: with nothing to append the writer is not
    invoked at all, so a steady-state nightly leaves the roster's bytes — including the
    trailing-newline convention of the committed file — completely untouched.
    """
    payload = {"schemaVersion": SCHEMA_VERSION, "characters": list(records or [])}
    if not dry_run:
        write_json(config.ROSTER_FILE, payload)
    return payload


def write_detail_documents(details, *, dry_run):
    """Write every detail document for this run, then report how many bytes landed.

    The directory is created on first use and never removed: an empty ``data/character-data/`` is
    a guild with nobody resolved yet, not a missing artifact, and the gate treats it as such."""
    written = 0
    if dry_run:
        return written
    if details:
        config.DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    for rid, doc in details.items():
        write_json(detail_file(rid), doc)
        written += 1
    return written


def pruned_detail_files(keep_ids, detail_dir=None):
    """Every ``*.json`` in the detail directory that no current ok row names.

    The name rule is the writer's own: a file is a candidate for pruning only when its stem is a
    plausible roster id and its bytes parse as a JSON object with that same ``id``. Anything else —
    a stray ``README``, a truncated write, a document whose ``id`` says another character — is left
    exactly where it is and reported, because deleting a file this module did not write is not part
    of publishing, and an unparseable leftover is precisely what the gate exists to make loud."""
    directory = detail_dir or config.DETAIL_DIR
    if not directory.is_dir():
        return [], []
    wanted = {str(rid) for rid in (keep_ids or ())}
    stale, foreign = [], []
    for path in sorted(directory.glob("*.json")):
        if path.stem in wanted:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            foreign.append(path)
            continue
        if isinstance(doc, dict) and str(doc.get("id")) == path.stem:
            stale.append(path)
        else:
            foreign.append(path)
    return stale, foreign


def prune_detail_files(keep_ids, *, dry_run):
    """Delete the detail files no current row names, and report what could not be reconciled.

    Pruning is part of the publication rather than housekeeping: a row demoted from ``ok`` to
    ``not_found`` must not leave its gear on the server under a manifest that now says the identity
    is unresolved (the site would still render it for anyone who guessed the URL, and the gate would
    fail the tree as a torn write). It runs BEFORE the manifest is written, so the manifest stays
    the last thing to change — see :func:`write_data`."""
    stale, foreign = pruned_detail_files(keep_ids)
    for path in stale:
        if not dry_run:
            path.unlink()
    for path in foreign:
        print(f"  ! left {path.name} in place: it is not a detail document this run wrote "
              "(unparseable, or its id does not match its filename); the verify gate reports it "
              "as an orphan")
    return [path.name for path in stale]


def write_data(characters, *, triggered_by_value, dry_run, synced_at=None):
    """Publish the schema-v3 sidecar: every detail file first, the manifest LAST.

    The order is the whole point and is shared with ``write_pair``'s H4 floor: the manifest is the
    single document the site reads at boot and the only one that names the others, so a run that
    dies partway leaves the PREVIOUS manifest authoritative.

    Be exact about what that buys, because the sentence "the previous manifest is authoritative" is
    NOT the same claim as "the tree is intact". The prune keeps only this run's ok ids, so a row that
    just lost ``ok`` has its file deleted here while the manifest still on disk calls itself ``ok``
    and points at it. A crash in that window therefore leaves a TORN tree, not a merely orphaned one:
    the old manifest naming a file that is gone. That is accepted deliberately — it never reaches a
    publication, because the commit runs only after ``.pi/scripts/verify_sync_artifacts.py`` exits 0
    and its directory rule refuses exactly this state, so CI pushes nothing and a local run is left
    loudly failing. The alternative ordering (prune after the manifest) is worse in the case that
    matters: it would PUBLISH a tree with an orphan, and it would keep a demoted character's gear on
    the server until the following night, which is the outcome prune_detail_files exists to prevent.

    ``synced_at`` exists for the migration (``tools/migrate_v3.py``): a layout change must not
    restate a sync timestamp it did not earn, or "did the data change?" stops being answerable from
    a diff. A sync run leaves it out and gets ``http.iso_now()``, as always."""
    rows, details = split_characters(characters)
    payload = manifest_document(rows, triggered_by_value=triggered_by_value, synced_at=synced_at)
    if not dry_run:
        write_detail_documents(details, dry_run=False)
        prune_detail_files(details.keys(), dry_run=False)
        write_json(config.DATA_FILE, payload)
    return payload


def write_pair(entries, characters, *, ordered_ids, prev_entries, triggered_by_value, dry_run,
               floor_entries=None):
    """Both artifacts or neither, and never past the H4 floor.

    The floor lives inside the writer rather than in the caller so that no future
    call site can publish a mass-flipped map by forgetting to check first (the
    caller still asks the artifacts layer for the verdict up front in order to
    print the run summary with it — the criterion itself is evaluated in exactly
    one place). The sidecar and the map are always written together because the UI
    joins them by roster id: a half-written pair is a broken site. Both the nightly
    sync and the single-row lookup write through here for that reason.

    Since schema v3 "the pair" is three things on disk (the map, ``data/character-data/``, the
    manifest) and the ORDER is what keeps a half-run survivable: the map, then every detail file,
    then the prunings, then the manifest LAST. Nothing a crashed run leaves behind can make the
    browser ask for a file that is not there, because the manifest is the last write; the damage it
    CAN leave is an orphan detail file, or a detail file the previous manifest still names that a
    prune already removed, and both are directory-rule failures in
    ``.pi/scripts/verify_sync_artifacts.py`` rather than something that reaches production.

    ``floor_entries`` is what the floor evaluates when given: the caller's ``resolver_verdict``
    snapshot. It exists because ``entries`` is the map as published — after the reviewer-note pass
    — and a human demotion must not be able to trip the outage floor (see ``resolver_verdict``).
    Omitting it keeps the strict behaviour: the floor judges the write itself.
    """
    refusal = mass_flip_refusal(entries if floor_entries is None else floor_entries,
                                ordered_ids, prev_entries)
    if refusal:
        return refusal
    # Validate the sidecar BEFORE touching either artifact. split_characters is the only place that
    # refuses a record key belonging to neither half, and it used to run inside write_data — after
    # write_map had already landed, which turned "both artifacts or neither" into "the map moved and
    # the manifest did not, plus a traceback". A future enrich field wired before the key constants
    # now fails with nothing published instead.
    split_characters(characters)
    write_map(entries, dry_run=dry_run)
    write_data(characters, triggered_by_value=triggered_by_value, dry_run=dry_run)
    return None
