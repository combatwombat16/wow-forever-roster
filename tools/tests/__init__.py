"""Makes ``tools/tests/`` a package so stdlib discovery finds the Phase 2 tests.

Without this ``__init__.py``, ``python3 -m unittest discover`` skips the
directory: since Python 3.11 unittest no longer imports test modules out of
namespace packages, so a directory without ``__init__.py`` is never searched.
With it, both forms work from a clean checkout, offline and with no install:

    python3 -m unittest discover -s tools -p "test_*"     # from the repo root
    python3 -m unittest discover -p "test_*"              # from tools/

The module files themselves stay runnable directly
(``python3 tools/tests/test_bnet_phase2.py``); they import ``bnet`` absolutely
after putting ``tools/`` on ``sys.path``, so packaging changes nothing about them.
"""
