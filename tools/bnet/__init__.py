"""Battle.net "WoW: Forever" roster pipeline (docs/refactor-design.md §5).

Stdlib-only. Extracted from the pre-refactor ``tools/sync_bnet.py`` and
``tools/probe_bnet.py`` scripts; those files are gone and this package is the
single implementation.

Modules
    config      every URL, namespace, budget and repo path (nothing hardcodes them)
    http        token flow, credential redaction, host allowlist, no-redirect
                transport and the Last-Modified conditional cache
    roster      ``data/characters.json`` v1/v2 reading + lifecycle (validated /
                confirmedSlug) helpers
    autoadd     Phase C: the pure plan of pending rows to APPEND for the Battle.net-visible
                guild members a roster row is not yet known for
    resolve     name -> Battle.net identity, candidate enumeration, ``ambiguous``
                verdicts, trusted-slug handling (never auto-picks)
    fetch       profile / gear / achievement / pvp fetching + H2 href/iconUrl
                validators, and the sidecar enrichment pass
    artifacts   the single writer of ``data/battle-net-map.json`` and
                ``data/character-data.json``, plus the H4 mass-flip floor
    sync        ``python3 -m bnet sync`` orchestrator
    probe       ``python3 -m bnet probe`` Phase 1 endpoint probe (behaviour frozen)

Security posture carried over verbatim from the security review of 2026-11-08:
H1 host allowlist + no-redirect on every credentialed send, H2 URL validation
before any artifact write and withholding of non-Blizzard URLs from every string,
H3 redaction of derived credential forms, H4 mass-flip floor on the artifact pair.
"""

__all__ = ["artifacts", "autoadd", "config", "fetch", "http", "probe", "resolve", "roster", "sync"]
