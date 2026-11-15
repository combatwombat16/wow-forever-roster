"""Makes ``tools/`` a package so stdlib discovery finds the Phase 2 tests.

Without this ``__init__.py``, ``python3 -m unittest discover`` skips the
directory: since Python 3.11 unittest no longer imports test modules out of
namespace packages, so a directory without ``__init__.py`` is never searched.
With it, both forms work from a clean checkout, offline and with no install:

    python3 -m unittest discover -s tools -p "test_*"     # from the repo root
    python3 -m unittest discover -p "test_*"              # from tools/

The module files themselves stay runnable directly
(``python3 tools/tests/test_bnet_phase2.py``); they import ``bnet`` absolutely
after putting ``tools/`` on ``sys.path``, so packaging changes nothing about them.

``redirect_artifacts`` / ``restore_artifacts`` live here because the schema-v3 sidecar is
THREE paths, and a test that redirects two of them writes into the repository. The manifest and the
map are files, but ``data/character-data/`` is a directory the writer prunes: with ``DETAIL_DIR``
still pointing at the checkout, a unit test whose fake characters carry one ok row prunes every real
detail file it does not name — deleting published gear lists from the working tree as a side effect
of a test run. One helper, all three names, and the names come out of ``bnet.config`` so a rename
moves the tests with it rather than leaving a stale literal behind.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))        # tools/

from bnet import config          # noqa: E402  (stdlib-only package; imported after the path fix)

# Everything ``bnet.artifacts`` writes, by constant name. ``ROSTER_FILE`` is included so a caller
# that drives a whole run redirects the additive roster writer too.
ARTIFACT_PATHS = ("MAP_FILE", "DATA_FILE", "DETAIL_DIR", "ROSTER_FILE")


def redirect_artifacts(base, *, names=ARTIFACT_PATHS):
    """Move the named artifact paths under ``base``, keeping each one's own name.

    Returns what ``restore_artifacts`` needs. Every test that reaches ``write_pair``, ``write_data``
    or ``write_roster`` — which is every test that drives ``Sync.run_sync`` or ``Sync.run_lookup`` —
    calls this in ``setUp`` and restores in ``tearDown``, so no unit test can write the checkout.
    """
    base = Path(base)
    saved = {}
    for name in names:
        saved[name] = getattr(config, name)
        setattr(config, name, base / Path(getattr(config, name)).name)
    return saved


def restore_artifacts(saved):
    """Undo :func:`redirect_artifacts`, exactly and in any order."""
    for name, value in saved.items():
        setattr(config, name, value)
