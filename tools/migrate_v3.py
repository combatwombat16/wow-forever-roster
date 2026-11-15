#!/usr/bin/env python3
"""One-shot, offline migration of the character sidecar into the schema-v3 layout.

``docs/refactor-design.md`` §3: ``data/character-data.json`` stops being one document that grows
with the guild and becomes a MANIFEST — one small row per roster id, holding the fields every
roster card reads (``status``, ``fetchedAt``, ``level``, ``itemLevel``, ``guild``, ``activeSpec``,
``classIconUrl``, ``race``, ``class``) plus a ``detail`` pointer — while the heavy half of each row
(``bnet``, ``gear``, ``progress``, ``achievementPoints``, ``stats``, ``talents``, ``professions``,
``reputations``, ``media``) moves to ``data/character-data/<id>.json``, the file the detail modal
asks for when it opens.

WHAT IT DOES NOT DO
  * no network, no credential, no re-parse of a Blizzard payload: every value is copied out of the
    committed sidecar;
  * no second implementation. The rows and documents come from ``bnet.artifacts.manifest_row`` and
    ``bnet.artifacts.detail_document`` — the two functions the nightly writes through — and the
    bytes from ``bnet.artifacts.dumps``. A migration the sync would not have produced is a bug in
    this file, and the no-data-loss test (``tools/tests/test_sidecar_v3.py``) is what catches it;
  * no restated provenance. ``syncedAt`` and ``triggeredBy`` are carried over verbatim: a layout
    change did not re-fetch anything, and rewriting the timestamp would make "did the data change?"
    unanswerable from a diff;
  * no touch to ``data/battle-net-map.json`` or ``data/characters.json``. Those stay schema v2 —
    the split is a serialization change to one artifact (see ``bnet.artifacts.DATA_SCHEMA_VERSION``
    versus ``SCHEMA_VERSION``).

WHAT IT WRITES, IN THIS ORDER (the same order ``bnet.artifacts.write_data`` uses, and for the same
reason): every detail file, then the prunings, then the manifest LAST. The manifest is the only
document that names the others, so an interrupted migration leaves the previous publication — every
file it points at still present — authoritative, and the worst residue is an orphan file the verify
gate refuses.

Idempotent: a run over an already-migrated tree writes zero bytes.

``--check`` performs the same comparison and writes nothing: it exits 1 when the tree is not
already v3-consistent (manifest at v3 with correct rows, one file per ok row, no orphan file, every
document byte-identical to what the writer would emit). That is the mode the gate and CI use.

Usage:
    python3 tools/migrate_v3.py [--dry-run] [--check]

Exit codes: 0 migrated, or already migrated (and for ``--check``, consistent); 1 a problem a human
must resolve — nothing is deleted or overwritten in that case beyond what the plan reports.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))               # tools/

from bnet import artifacts, config     # noqa: E402  (stdlib-only package; no network here either)


class Migration:
    """Plan first, then apply.

    ``plan()`` is pure over the tree — it reads the committed manifest (folding each row back
    together with its detail file, through ``artifacts.load_previous_data``, which is the exact
    inverse of the split) and produces the documents and the byte-level diff. ``apply()`` writes
    what ``plan()`` decided, in the durability order above, and nothing else. ``--check`` is
    therefore not a second set of rules: it is ``plan()`` with the exit code read off the plan.
    """

    def __init__(self, *, check=False, dry_run=False, manifest_path=None, detail_dir=None):
        self.check = check
        self.dry_run = check or dry_run
        self.manifest_path = manifest_path or config.DATA_FILE
        self.detail_dir = detail_dir or config.DETAIL_DIR
        self.actions = []
        self.problems = []
        # Filled by plan(): what a correct tree already holds versus what the writer would emit.
        self.writes = []                # [(path, text)]
        self.stale = []                 # [path] detail files no row names
        self.foreign = []               # [path] unclaimed files this module must not delete
        self.manifest_doc = None
        self.detail_docs = {}
        self.manifest_needs_write = True
        self.raw = {}

    def log(self, text):
        """An action this run took (or would take). Reads as a verb, and says so when it did not."""
        self.actions.append(text)
        print(("[check] would " if self.check else "[dry-run] would " if self.dry_run else "") + text)

    def note(self, text):
        """A reported fact, not an action: never prefixed with "would", because it is not one."""
        print(text)

    # -- plan ------------------------------------------------------------------ #
    def source_document(self):
        """The committed sidecar as the reader sees it: full records, folded from both halves.

        Reading through ``artifacts.load_previous_data`` is what makes this idempotent: a v2
        document hands over whole rows, a v3 one hands over manifest rows re-joined with their
        detail files, and both produce the same ``{id: record}`` the writer consumes. So a second
        run of this script re-derives the bytes it just wrote and finds nothing to do."""
        if not self.manifest_path.exists():
            self.problems.append(f"{self.manifest_path.name}: not found; nothing to migrate")
            return None
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            self.problems.append(f"{self.manifest_path.name}: unparsable JSON ({type(exc).__name__}); "
                                 "nothing was written")
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("characters"), dict):
            self.problems.append(f"{self.manifest_path.name}: no characters object; left untouched")
            return None
        self.raw = raw
        return artifacts.load_previous_data(self.manifest_path, self.detail_dir)

    def plan(self):
        records = self.source_document()
        if records is None:
            return False
        triggered = self.raw.get("triggeredBy")
        if not (isinstance(triggered, str) and triggered.strip()):
            triggered = "schedule"      # the shipped file came from the nightly cron
            self.log(f"added triggeredBy='schedule' to {self.manifest_path.name}")
        synced_at = self.raw.get("syncedAt")
        if not (isinstance(synced_at, str) and synced_at.strip()):
            self.problems.append(f"{self.manifest_path.name}: no syncedAt to carry over; refusing to "
                                 "invent one (the sync writes it, this script never does)")
            return False

        try:
            rows, details = artifacts.split_characters(records)
        except ValueError as exc:
            self.problems.append(str(exc))
            return False
        self.detail_docs = details
        self.manifest_doc = artifacts.manifest_document(rows, triggered_by_value=triggered,
                                                        synced_at=synced_at)
        if self.raw.get("schemaVersion") != artifacts.DATA_SCHEMA_VERSION:
            self.log(f"bumped {self.manifest_path.name}: schemaVersion {self.raw.get('schemaVersion')} "
                     f"-> {artifacts.DATA_SCHEMA_VERSION} ({len(rows)} manifest rows, "
                     f"{len(details)} detail documents)")

        # The detail files: write only what differs, so a second run is a no-op on bytes.
        for rid, doc in details.items():
            path = artifacts.detail_file(rid, self.detail_dir)
            text = artifacts.dumps(doc)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                self.writes.append((path, text))
        self.stale, self.foreign = artifacts.pruned_detail_files(details.keys(), self.detail_dir)

        manifest_text = artifacts.dumps(self.manifest_doc)
        current = self.manifest_path.read_text(encoding="utf-8") if self.manifest_path.exists() else None
        self.manifest_needs_write = current != manifest_text
        return True

    # -- apply ----------------------------------------------------------------- #
    def apply(self):
        if self.manifest_needs_write or self.writes or self.stale:
            if not self.dry_run:
                self.detail_dir.mkdir(parents=True, exist_ok=True)
        for path, text in self.writes:
            if not self.dry_run:
                path.write_text(text, encoding="utf-8")
            self.log(f"{self._verb('write')} {self._rel(path)} ({len(text)} B)")
        for path in self.stale:
            if not self.dry_run:
                path.unlink()
            self.log(f"{self._verb('delete')} {self._rel(path)} (no manifest row names it)")
        for path in self.foreign:
            self.problems.append(f"{self._rel(path)}: an unclaimed file in the detail directory that "
                                 "this script will not delete (unparseable, or its id does not match "
                                 "its filename)")
        if self.manifest_needs_write:
            if not self.dry_run:
                # LAST, exactly as bnet.artifacts.write_data does: every file the new pointer set
                # names is already on disk by the time a reader can see it.
                artifacts.write_json(self.manifest_path, self.manifest_doc)
            self.log(f"{self._verb('write')} {self._rel(self.manifest_path)} "
                     f"({len(artifacts.dumps(self.manifest_doc))} B)")
        elif self.raw.get("schemaVersion") == artifacts.DATA_SCHEMA_VERSION:
            self.note(f"{self._rel(self.manifest_path)} already at schema "
                      f"v{artifacts.DATA_SCHEMA_VERSION}: unchanged, no bytes rewritten")
        if self.check:
            # The exit code IS the check: every pending write the plan found above is a divergence
            # from what the committed writer would produce, which is exactly what "not
            # v3-consistent" means here — a stale or v2 manifest, a missing or byte-drifted detail
            # file, or an orphan this run would prune.
            pending = len(self.writes) + len(self.stale) + (1 if self.manifest_needs_write else 0)
            if pending:
                self.problems.append(
                    f"{pending} pending change(s): the tree is NOT v3-consistent "
                    f"({len(self.writes)} detail file(s) missing or out of date, {len(self.stale)} "
                    f"orphan(s), manifest "
                    f"{'needs a rewrite' if self.manifest_needs_write else 'matches'}); "
                    "run `python3 tools/migrate_v3.py` (or the nightly) and re-check")
        return not self.problems

    def _verb(self, base):
        """Past tense after a real run, the bare infinitive in a report (both read correctly next
        to the "[dry-run] would " prefix ``log`` adds)."""
        if self.dry_run:
            return base
        return {"write": "wrote", "delete": "deleted"}[base]

    def _rel(self, path):
        try:
            return path.relative_to(config.REPO).as_posix()
        except ValueError:
            return path.name

    # -- report ---------------------------------------------------------------- #
    def cross_check(self):
        """Roster/map/id coverage. Findings only; this never repairs."""
        try:
            roster_doc = json.loads(config.ROSTER_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        ids = {rec.get("id") for rec in roster_doc.get("characters", []) if isinstance(rec, dict)}
        covered = set(self.manifest_doc.get("characters", {}))
        ok_rows = {rid for rid, row in self.manifest_doc["characters"].items()
                   if isinstance(row, dict) and row.get("status") == "ok"}
        self.note(f"cross-check: roster={len(ids)} manifest={len(covered)} detail_files={len(self.detail_docs)} "
                 f"ok_rows={len(ok_rows)} manifest-missing={sorted(ids - covered)[:5]} "
                 f"manifest-stale={sorted(covered - ids)[:5]}")
        if covered - ids or ids - covered:
            # A WARNING, deliberately, and not a PROBLEM: this script never adds or drops a row, so
            # a coverage mismatch is a property of the document it was handed, not damage it did.
            # Coverage is ``.pi/scripts/verify_sync_artifacts.py``'s rule (and the nightly's to
            # repair); failing the migration here would refuse the one operation that can turn a
            # half-written v2 artifact into a readable v3 one.
            print(f"WARNING: the manifest does not cover exactly the current roster ids "
                  f"(missing={sorted(ids - covered)[:5]} stale={sorted(covered - ids)[:5]}). "
                  "Re-laying it out cannot fix that: the nightly writes coverage, and the verify "
                  "gate is what refuses to publish without it.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 tools/migrate_v3.py",
                                     description="Split data/character-data.json into a schema-v3 "
                                                 "manifest plus data/character-data/<id>.json detail files")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero unless the tree is ALREADY v3-consistent; write nothing")
    args = parser.parse_args(argv)

    m = Migration(check=args.check, dry_run=args.dry_run)
    planned = m.plan()
    if planned:
        m.apply()
        if not m.problems:
            m.cross_check()

    print()
    if m.problems:
        for problem in m.problems:
            print(f"PROBLEM: {problem}")
        print(f"\n{'consistency check failed' if args.check else 'migration incomplete'}: "
              f"{len(m.problems)} problem(s)")
        return 1
    if args.check:
        print(f"v3 consistency check passed: {len(m.manifest_doc['characters'])} manifest rows, "
              f"{len(m.detail_docs)} detail files, 0 files to write, 0 orphans")
        return 0
    print("migration complete" + (" (dry-run: nothing written)" if args.dry_run else "")
          + f"; {len(m.actions)} action(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
