"""``python3 -m bnet`` — subcommand dispatch so the CLIs read as designed.

    python3 -m bnet sync  [--lookup NAME] [--force] [--dry-run] [--state-dir P]
    python3 -m bnet probe [extra_name ...]

Run it from ``tools/`` (or with ``PYTHONPATH=tools``); the repository root and
the ``data/`` paths come from ``bnet.config``, not from the working directory.
"""

import sys

from . import probe as probe_module
from . import sync as sync_module

USAGE = """usage: python3 -m bnet <command> [options]

commands:
  sync   nightly Battle.net roster sync (python3 -m bnet sync --help)
  probe  Phase 1 endpoint probe, findings to docs/ (python3 -m bnet probe --help)
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "sync":
        return sync_module.main(rest)
    if command == "probe":
        return probe_module.main(rest)
    print(f"unknown command: {command}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
