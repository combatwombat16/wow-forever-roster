"""Map/data artifact writers: the schemas of record, plus the H4 mass-flip floor.

Provenance: ``write_json``, the fixed map-entry field set (``map_entry`` /
``ok_entry``) and the H4 floor all come from ``tools/sync_bnet.py``; the
``candidates`` list, the ``ambiguous`` verdict and the ``triggeredBy`` provenance
key are the Phase 2 lifecycle additions of docs/refactor-design.md §3.

This module is the ONLY writer of ``data/battle-net-map.json`` and
``data/character-data.json``. Every key either module emits is listed in
``MAP_ENTRY_KEYS`` / ``CANDIDATE_KEYS`` / ``CHARACTER_KEYS`` so a reviewer can
diff two runs line by line and the verify script can assert exact key sets.
"""

import json
import os

from . import config, fetch, http

SCHEMA_VERSION = 2

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
CHARACTER_KEYS = ("status", "bnet", "level", "guild", "activeSpec", "itemLevel",
                  "gear", "progress", "achievementPoints", "fetchedAt")
BNET_KEYS = ("name", "surname", "href")
GEAR_KEYS = ("slot", "name", "ilvl", "iconUrl")


def write_json(path, payload):
    path.write_text(json.dumps(http.redact(payload), ensure_ascii=False, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8")


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
def load_previous_data(path=None):
    """Previous sidecar rows, keyed by roster id.

    Lookup mode republishes one character at a time, but the sidecar is written as
    a whole document (and the UI joins it to the map by id), so a lookup has to
    carry every other row forward. A missing/unreadable file means an empty dict:
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
    return {k: v for k, v in characters.items() if isinstance(v, dict)} if isinstance(characters, dict) else {}


# --------------------------------------------------------------------------- #
# H4 mass-flip floor
# --------------------------------------------------------------------------- #
def ok_count(entries, ordered_ids):
    """How many of ``ordered_ids`` are ``ok`` in ``entries``.

    Both numbers the floor compares are counted here so the caller's pre-check and
    the writer can never disagree about what "healthy" means for a given pair."""
    return sum(1 for rid in ordered_ids
               if isinstance(entries.get(rid), dict) and entries[rid].get("status") == "ok")


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


def write_data(characters, *, triggered_by_value, dry_run):
    payload = {"schemaVersion": SCHEMA_VERSION, "syncedAt": http.iso_now(),
               "triggeredBy": triggered_by_value, "characters": characters}
    if not dry_run:
        write_json(config.DATA_FILE, payload)
    return payload


def write_pair(entries, characters, *, ordered_ids, prev_entries, triggered_by_value, dry_run):
    """Both artifacts or neither, and never past the H4 floor.

    The floor lives inside the writer rather than in the caller so that no future
    call site can publish a mass-flipped map by forgetting to check first (the
    caller still asks the artifacts layer for the verdict up front in order to
    print the run summary with it — the criterion itself is evaluated in exactly
    one place). The sidecar and the map are always written together because the UI
    joins them by roster id: a half-written pair is a broken site. Both the nightly
    sync and the single-row lookup write through here for that reason.
    """
    refusal = mass_flip_refusal(entries, ordered_ids, prev_entries)
    if refusal:
        return refusal
    write_map(entries, dry_run=dry_run)
    write_data(characters, triggered_by_value=triggered_by_value, dry_run=dry_run)
    return None
