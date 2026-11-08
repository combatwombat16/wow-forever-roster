#!/usr/bin/env python3
"""One-shot migration of the repo-root JSON artifacts into ``data/`` at schema v2.

docs/refactor-design.md §8: Phase 2 lands a single migration commit that moves
``characters.json``, ``character-data.json`` and ``battle-net-map.json`` into
``data/``, converts the roster from the v1 bare array to the v2 envelope, and
bumps the two sidecars to schema v2. This script performs the filesystem half of
that job; the ``git mv`` history recording is the orchestrator's, not the
script's.

What it does, in order, for each of the three files:

1. move a repo-root file into ``data/`` (plain write + delete of the original,
   creating ``data/`` if needed);
2. roster: ``[ ... ]`` -> ``{"schemaVersion": 2, "characters": [ ... ]}`` with no
   per-character ``validation`` blocks invented (design §2: absence == draft);
3. ``battle-net-map.json``: ``schemaVersion`` -> 2 and ``candidates: []`` added to
   every entry (design §3; an existing non-empty list is preserved untouched);
4. ``character-data.json``: ``schemaVersion`` -> 2 and ``triggeredBy`` added if
   absent. ``"schedule"`` is the honest default for the shipped artifact, which
   was produced by the nightly run.

Nothing else changes: every existing value, key order and key set is carried
over. A file that is already in the destination and already at schema v2 is left
byte-for-byte alone, which makes the second run a no-op.

Usage:
    python3 tools/migrate_v2.py [--dry-run]

Exit codes: 0 migrated or already migrated; 1 a conflict or unparsable input
that a human must resolve (nothing is deleted in that case).
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
NAMES = ("characters.json", "character-data.json", "battle-net-map.json")

ROSTER_SCHEMA = 2
SIDECAR_SCHEMA = 2

# Design §3: the map entry's v2 field order. ``candidates`` is appended last so a
# v1 entry keeps its existing line order and only gains one line in the diff.
MAP_ENTRY_ORDER = ("rosterName", "status", "slug", "href", "bnetName", "surname",
                   "matchedForm", "mappedVia", "note", "candidates")


def dumps(doc):
    """Same serialisation the sync writer uses, so a migrated file and a file the
    nightly rewrites differ only in the values that actually changed."""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


class Migration:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.actions = []
        self.problems = []

    def log(self, text):
        self.actions.append(text)
        print(("[dry-run] would " if self.dry_run else "") + text)

    # -- conversions ----------------------------------------------------------- #
    def convert_roster(self, doc, source):
        if isinstance(doc, dict) and doc.get("schemaVersion") == ROSTER_SCHEMA and isinstance(doc.get("characters"), list):
            return doc, False
        if isinstance(doc, list):
            if not doc:
                self.problems.append(f"{source}: refusing to wrap an empty roster array")
                return doc, False
            ids = [rec.get("id") for rec in doc]
            if len({i for i in ids if isinstance(i, str)}) != len(ids):
                self.problems.append(f"{source}: duplicate or non-string ids; fix the roster before migrating")
                return doc, False
            self.log(f"converted {source}: v1 array -> v2 object (schemaVersion {ROSTER_SCHEMA}, "
                     f"{len(doc)} characters, no validation blocks added)")
            return {"schemaVersion": ROSTER_SCHEMA, "characters": doc}, True
        self.problems.append(f"{source}: neither a v1 array nor a v2 object; left untouched")
        return doc, False

    def convert_map(self, doc, source):
        if not isinstance(doc, dict) or not isinstance(doc.get("entries"), dict):
            self.problems.append(f"{source}: no entries object; left untouched")
            return doc, False
        changed = False
        lacking = 0
        out = {"schemaVersion": SIDECAR_SCHEMA,
               "updatedAt": doc.get("updatedAt"),
               "entries": {}}
        if doc.get("schemaVersion") != SIDECAR_SCHEMA:
            self.log(f"bumped {source}: schemaVersion {doc.get('schemaVersion')} -> {SIDECAR_SCHEMA}")
            changed = True
        for rid, entry in doc["entries"].items():
            if not isinstance(entry, dict):
                self.problems.append(f"{source}: entry {rid} is not an object; left untouched")
                return doc, False
            merged = {k: v for k, v in entry.items() if k != "candidates"}
            candidates = entry.get("candidates") if isinstance(entry.get("candidates"), list) else []
            if not isinstance(entry.get("candidates"), list):
                changed = True
                lacking += 1
            row = {k: merged[k] for k in MAP_ENTRY_ORDER if k in merged}
            for k in merged:                      # never drop an unknown key
                row.setdefault(k, merged[k])
            row["candidates"] = candidates
            out["entries"][rid] = row
        if len(out["entries"]) != len(doc["entries"]):
            self.problems.append(f"{source}: entry count changed during conversion")
        if lacking:
            self.log(f"added candidates:[] to {lacking} of {len(out['entries'])} map entries of {source}")
        return out, changed

    def convert_data(self, doc, source):
        if not isinstance(doc, dict) or not isinstance(doc.get("characters"), dict):
            self.problems.append(f"{source}: no characters object; left untouched")
            return doc, False
        changed = doc.get("schemaVersion") != SIDECAR_SCHEMA
        triggered = doc.get("triggeredBy")
        if not (isinstance(triggered, str) and triggered.strip()):
            triggered = "schedule"      # the shipped file came from the nightly cron
            changed = True
        out = {"schemaVersion": SIDECAR_SCHEMA,
               "syncedAt": doc.get("syncedAt"),
               "triggeredBy": triggered,
               "characters": doc["characters"]}
        if doc.get("schemaVersion") != SIDECAR_SCHEMA:
            self.log(f"bumped {source}: schemaVersion {doc.get('schemaVersion')} -> {SIDECAR_SCHEMA}")
        if triggered != doc.get("triggeredBy"):
            self.log(f"added triggeredBy={triggered!r} to {source}")
        return out, changed

    # -- file handling --------------------------------------------------------- #
    def write(self, path, doc):
        text = dumps(doc)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
        if not self.dry_run:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return True

    def migrate(self, name):
        root = REPO / name
        dest = DATA_DIR / name
        converter = {"characters.json": self.convert_roster,
                     "character-data.json": self.convert_data,
                     "battle-net-map.json": self.convert_map}[name]

        if root.exists() and dest.exists():
            if root.read_bytes() == dest.read_bytes():
                if not self.dry_run:
                    root.unlink()
                self.log(f"deleted redundant root copy {name} (identical to data/{name})")
                return
            try:
                a, b = json.loads(root.read_text(encoding="utf-8")), json.loads(dest.read_text(encoding="utf-8"))
            except ValueError as exc:
                self.problems.append(f"{name}: cannot compare root and data/ copies ({type(exc).__name__})")
                return
            if converter(a, f"{name} (root copy)")[0] == converter(b, f"{name} (data copy)")[0]:
                if not self.dry_run:
                    root.unlink()
                self.log(f"deleted root copy {name}: identical to data/{name} after conversion")
                return
            self.problems.append(f"{name}: root and data/ copies differ; resolve by hand, nothing was deleted")
            return

        source = root if root.exists() else dest
        if not source.exists():
            self.problems.append(f"{name}: found in neither the repo root nor data/")
            return

        try:
            doc = json.loads(source.read_text(encoding="utf-8"))
        except ValueError as exc:
            self.problems.append(f"{name}: unparsable JSON ({type(exc).__name__}); left untouched")
            return

        problems_before = len(self.problems)
        converted, _changed = converter(doc, source.relative_to(REPO).as_posix())
        if len(self.problems) > problems_before:
            return                      # this file is refused; later files are unrelated

        if not self.dry_run:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.write(dest, converted):
            self.log(f"wrote data/{name}" + (f" (from root {name})" if source is root else ""))
        elif source is dest:
            self.log(f"data/{name} already at schema v2: unchanged, no bytes rewritten")
        if root.exists() and source is root:
            if not self.dry_run:
                root.unlink()
            self.log(f"deleted root {name}")

    def cross_check(self):
        """Cheap consistency report; findings only, never a rewrite."""
        try:
            roster = json.loads((DATA_DIR / "characters.json").read_text(encoding="utf-8"))
            mapping = json.loads((DATA_DIR / "battle-net-map.json").read_text(encoding="utf-8"))
            data = json.loads((DATA_DIR / "character-data.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        ids = {rec.get("id") for rec in roster.get("characters", [])}
        map_ids = set(mapping.get("entries", {}))
        data_ids = set(data.get("characters", {}))
        self.log(f"cross-check: roster={len(ids)} map={len(map_ids)} data={len(data_ids)} "
                 f"map-missing={sorted(ids - map_ids)[:5]} map-stale={sorted(map_ids - ids)[:5]} "
                 f"data-missing={sorted(ids - data_ids)[:5]}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 tools/migrate_v2.py",
                                     description="Move the roster JSONs into data/ and convert them to schema v2")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write and delete nothing")
    args = parser.parse_args(argv)

    m = Migration(dry_run=args.dry_run)
    for name in NAMES:
        m.migrate(name)
    if not m.problems:
        m.cross_check()

    print()
    if m.problems:
        for problem in m.problems:
            print(f"PROBLEM: {problem}")
        print(f"\nmigration incomplete: {len(m.problems)} problem(s); no file was deleted")
        return 1
    print("migration complete" + (" (dry-run: nothing written)" if args.dry_run else "")
          + f"; {len(m.actions)} action(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
