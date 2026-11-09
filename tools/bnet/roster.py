"""Roster reading: schema v1 (bare array) and v2 (envelope) plus lifecycle rules.

Provenance: the record validation block is lifted verbatim from
``tools/sync_bnet.py::load_roster`` (Phase 2); the v2 envelope and the
lifecycle helpers are new behaviour added by docs/refactor-design.md §2 and §5.

A roster record keeps its ten original fields untouched (``id, player, name,
track, role, roleSec, race, class, prof1, prof2``) and may carry an optional
``validation`` block:

    {"state": "validated", "confirmedSlug": "...", "reviewedBy": "...",
     "reviewedAt": "..."}

Two values are ever stored. ``validated`` is a human sign-off. ``pending`` is written by
nothing but the Phase C auto-add (``bnet.autoadd``) and marks a row appended for a
Battle.net-visible guild member nobody has reviewed; it is deliberately NOT a trust state,
so such a row is still resolved on every run. Every other lifecycle state is derived by
the UI from the Battle.net map status. A record with no ``validation`` is a
draft.

A draft may carry the reviewer's *open report* instead —
``{"note": "...", "reportedBy": "...", "reportedAt": "..."}`` (no ``state`` key, which is
what keeps it a draft). ``reported_note()`` reads it and ``resolve.reported_note_entry()``
turns it into a map ``needs_review`` entry on every sync run until a ``validated`` commit
replaces it (design §2).
"""

import json

from . import config


def roster_records(document):
    """Return the character records of a roster document in either shape.

    v1 is a bare JSON array; v2 is ``{"schemaVersion": 2, "characters": [...]}``.
    Both are accepted for the transition window only (design §8) — sync writes
    nothing here, it only reads, so tolerating v1 costs no schema drift.
    """
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get("characters"), list):
        return document["characters"]
    raise ValueError("characters.json must be a v1 array or a v2 object with a characters list")


def load_roster(path=None):
    """Read and validate the roster. Path defaults to ``data/characters.json``."""
    path = path or config.ROSTER_FILE
    records = roster_records(json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(records, list) or not records:
        raise ValueError("characters.json must contain a non-empty list of characters")
    ids = [rec.get("id") for rec in records]
    if not all(isinstance(i, str) and i for i in ids):
        raise ValueError("every roster record needs a non-empty string id")
    if len(set(ids)) != len(ids):
        raise ValueError("characters.json contains duplicate ids; refusing a run that could not key them")
    for rec in records:
        if not isinstance(rec.get("name"), str) or not rec["name"].strip():
            raise ValueError(f"roster id {rec.get('id')} has no usable name")
    return records


def roster_names(path=None):
    """Names in roster order for the probe. Duplicates are kept on purpose.

    The probe needs one row per roster entry so that a duplicate name mapping
    to two ids stays visible as Phase 2 evidence."""
    names = []
    for record in load_roster(path):
        name = record.get("name") if isinstance(record, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name)
    if not names:
        raise ValueError(f"no character names found in {path or config.ROSTER_FILE}")
    return names


def validation_of(rec):
    v = rec.get("validation") if isinstance(rec, dict) else None
    return v if isinstance(v, dict) else {}


def is_validated(rec):
    """True when the user has locked this row (design §2: read-only)."""
    return validation_of(rec).get("state") == "validated"


def confirmed_slug(rec):
    """The user's chosen slug, authoritative over any future name resolution."""
    slug = validation_of(rec).get("confirmedSlug")
    return slug.strip() if isinstance(slug, str) and slug.strip() else None


# The review form caps the note at 240 characters (``js/detail.js`` REVIEW_NOTE_MAX); the same
# ceiling here means a hand-edited roster cannot put a longer note into a public artifact than
# the UI would ever have written.
REPORT_NOTE_MAX = 240


def reported_note(rec):
    """The reviewer's open "not my character" report (design §2), or None.

    Written by the detail modal as ``validation.note`` alongside ``reportedBy`` /
    ``reportedAt``, and deliberately written *without* a ``state`` key so the row stays a draft.
    A row whose ``state`` is ``validated`` is signed off — its note, if one survived, is history
    rather than an open report, which is exactly what the UI's "re-open for editing" removes.
    Whitespace-only notes are not reports either: they would force ``needs_review`` forever on a
    row nobody actually objected to.
    """
    v = validation_of(rec)
    if v.get("state") == "validated":
        return None
    note = v.get("note")
    if not isinstance(note, str):
        return None
    text = note.strip()
    return text[:REPORT_NOTE_MAX] if text else None


def is_trusted(rec):
    """True when sync must never re-resolve this roster name (design §3).

    Either the row is validated, or the reviewer recorded a confirmedSlug; both
    mean a human has signed off on the identity, so re-resolving could only
    replace a correct mapping with a plausible-looking one."""
    return is_validated(rec) or confirmed_slug(rec) is not None


def trusted_slug(rec, prev_entry=None):
    """The slug to fetch directly for a trusted row, or None when unusable.

    confirmedSlug wins. A validated row without one keeps the slug already in
    the map, which is what the reviewer saw when they validated it."""
    slug = confirmed_slug(rec)
    if slug:
        return slug
    if is_validated(rec) and isinstance(prev_entry, dict):
        existing = prev_entry.get("slug")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
    return None
