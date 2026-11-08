"""``python3 -m bnet sync`` — the nightly Battle.net sync orchestrator.

Provenance: the orchestration, run summary and lookup mode of
``tools/sync_bnet.py`` (Phase 2), re-homed onto the modular package in
docs/refactor-design.md §5. The security invariants were not rewritten, only
moved: redaction/allowlist/no-redirect live in ``bnet.http``, the artifact
schemas and the H4 mass-flip floor in ``bnet.artifacts``, the H2 URL validators
and the enrichment fetches in ``bnet.fetch``.

Artifacts (written through ``bnet.artifacts``, the single writer):

    data/battle-net-map.json      roster-id -> Battle.net identity map (schema v2)
    data/character-data.json      normalized sidecar consumed by the site UI (schema v2)

Credentials come from BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET, are never
printed, logged or written, and every string that could carry one passes
through ``http.redact()`` first.

Usage:
    python3 -m bnet sync [options]                # from tools/, or PYTHONPATH=tools
    set -a && . ./.env && set +a && python3 -m bnet sync

Options:
    --lookup NAME   maintainer lookup: GET the name, also try the guild-roster
                    lever, print the full result, and — only when the name
                    resolves to exactly one profile — flip that one row through the
                    same write_pair as the nightly run, so the map row and its
                    character-data row land together or not at all
    --force         ignore cached Last-Modified timestamps and refetch
    --dry-run       compute everything but write no files
    --state-dir P   cache directory (default ~/.cache/wow-bnet-sync; the cache
                    deliberately lives outside the repo so no state is committed)

Exit codes:
    0  run completed — not_found / needs_review / fetch_error per character are
       findings, not failures; a partial run still writes both artifacts (2.8)
    1  credentials missing, token mint failed, every character failed to fetch,
       or the mass-flip floor refused the write (H4); in those cases the previous
       artifacts are left untouched (2.8)
"""

import argparse
import os
import sys
import time
from collections import Counter

from . import artifacts, config, fetch, http, resolve, roster


class Sync:
    def __init__(self, args):
        self.args = args
        self.cache = http.Cache(args.state_dir, args.dry_run)
        self.cache.load()
        self.client = http.Client(self.cache, args.force)
        self.f = fetch.Fetcher(self.client, self.cache)
        self.resolver = resolve.Resolver(self.f)
        self.budget_hit = False

    # -- run summary ----------------------------------------------------------- #
    def summary_lines(self, statuses, total, runtime_s, extra_lines=None):
        f = self.f
        lines = [
            "=== bnet sync summary ===",
            f"roster entries: {total}",
            "statuses: " + (" ".join(f"{k}={statuses.get(k, 0)}"
                                     for k in ("ok", "ambiguous", "not_found", "needs_review", "fetch_error"))
                            or "none"),
            (f"requests: {self.client.requests_made} (cap {config.MAX_REQUESTS}) | 304 revalidations: "
             f"{self.client.revalidations} | skipped via cache/map: {f.saved_requests} "
             f"(lookups {f.skipped['lookups']}, subresources {f.skipped['subresources']}, "
             f"icons {f.skipped['icons']})"),
            f"tokens minted: {self.client.tokens_minted} | http statuses: "
            + " ".join(f"{k}:{v}" for k, v in sorted(self.client.status_counts.items())),
            f"runtime: {runtime_s:.1f}s",
        ]
        if f.static_ns:
            lines.append(f"static namespace discovered: {f.static_ns}")
        lines.append("schema revisions: " + ", ".join(
            f"{s}={self.cache.schema_revisions[s]}" for s in config.DRIFT_SURFACES
            if s in self.cache.schema_revisions))
        lines.extend(f"DRIFT {line}" for line in f.drift)
        if f.icon_failures:
            lines.append(f"WARNING: {f.icon_failures} item icon fetch(es) failed; those gear rows carry iconUrl null")
        if f.guild_fetch_failures:
            lines.append(f"WARNING: guild roster fetch failed for: {', '.join(sorted(f.guild_fetch_failures))}")
        if self.budget_hit:
            lines.append(f"WARNING: request cap ({config.MAX_REQUESTS}) reached; remaining work skipped this run")
        if statuses.get("fetch_error"):
            lines.append(f"WARNING: partial failure — {statuses['fetch_error']} character(s) ended fetch_error; "
                         "statuses written per character")
        if statuses.get("ambiguous"):
            lines.append(f"NOTE: {statuses['ambiguous']} character(s) ended ambiguous; a reviewer must pick a "
                         "candidate (sync never auto-picks)")
        lines.extend(extra_lines or [])
        return lines

    def emit_summary(self, lines, markdown_heading="## Battle.net sync summary"):
        for line in lines:
            print(http.redact(line))
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if not step_summary:
            return
        markdown = [markdown_heading, ""] + [f"- {http.redact(line)}" for line in lines]
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write("\n".join(markdown) + "\n")
        except OSError as exc:
            print(f"  ! could not append to GITHUB_STEP_SUMMARY: {type(exc).__name__}")

    # -- full sync ------------------------------------------------------------- #
    def run_sync(self, roster_records, prev_entries):
        started = time.monotonic()
        ordered_ids = [rec["id"] for rec in roster_records]
        roster_ids = set(ordered_ids)
        pruned = sorted(i for i in prev_entries if i not in roster_ids)
        collision_ids = {}
        by_norm = {}
        for rec in roster_records:
            by_norm.setdefault(resolve.norm_slug(rec["name"]), []).append(rec["id"])
        for norm, ids in by_norm.items():
            if len(ids) > 1:
                for rid in ids:
                    collision_ids[rid] = ids

        work = {}
        for rid in ordered_ids:
            work[rid] = {"rec": next(r for r in roster_records if r["id"] == rid),
                         "entry": None, "profile": None, "profile_url": None, "unchanged": False}

        for rid in ordered_ids:
            w = work[rid]
            if self.budget_hit:
                w["entry"] = artifacts.map_entry(w["rec"], "fetch_error",
                                                 note="not attempted: request cap reached earlier in this run")
                continue
            if rid in collision_ids and not roster.is_trusted(w["rec"]):
                # A validated/confirmed row is keyed by identity, so a second
                # tracker row with the same spelling cannot invalidate it.
                others = ", ".join(i for i in collision_ids[rid] if i != rid)
                w["entry"] = artifacts.map_entry(w["rec"], "needs_review",
                                                 note=f"duplicate normalization with id {others}")
                continue
            try:
                self.resolver.resolve(w["rec"], w, prev_entries.get(rid))
            except http.BudgetExhausted as exc:
                self.budget_hit = True
                w["entry"] = artifacts.map_entry(w["rec"], "fetch_error", note=str(exc))
            except Exception as exc:
                # 2.8: one character crashing is a finding, never an abort.
                w["entry"] = artifacts.map_entry(w["rec"], "fetch_error",
                                                 note=f"unexpected {type(exc).__name__}: {http.redact(str(exc))[:160]}")

        try:
            self.resolver.guild_lever(work, ordered_ids)
        except http.BudgetExhausted as exc:
            self.budget_hit = True
            self.f.warnings.append(f"guild-lever pass stopped: {http.redact(str(exc))}")
        except http.UnsafeUrl as exc:
            # H1 net: ids the lever did not resolve fall through to not_found
            # below; the run still writes what it legitimately learned.
            self.f.warnings.append(f"guild-lever pass stopped by the URL allowlist: {http.redact(str(exc))}")

        for rid in ordered_ids:
            w = work[rid]
            if w["entry"] is None:
                note = "404 on all forms — likely not opted in (task 0.4)"
                if self.f.guild_fetch_failures:
                    note += f"; guild roster fetch failed for {', '.join(sorted(self.f.guild_fetch_failures))}"
                w["entry"] = artifacts.map_entry(w["rec"], "not_found", note=note)

        self.resolver.dedupe_slugs(work, ordered_ids)

        characters_out = {}
        for rid in ordered_ids:
            w = work[rid]
            entry = w["entry"]
            if entry["status"] != "ok":
                characters_out[rid] = {"status": entry["status"], "fetchedAt": http.iso_now()}
                continue
            if self.budget_hit:
                entry.update({"status": "fetch_error", "note": "enrichment skipped: request cap reached"})
                characters_out[rid] = {"status": "fetch_error", "fetchedAt": http.iso_now()}
                continue
            try:
                characters_out[rid] = self.f.enrich(w)
            except http.BudgetExhausted as exc:
                self.budget_hit = True
                entry.update({"status": "fetch_error", "note": str(exc)})
                characters_out[rid] = {"status": "fetch_error", "fetchedAt": http.iso_now()}
            except Exception as exc:
                w["entry"].update({"status": "fetch_error",
                                   "note": f"enrichment crashed: {type(exc).__name__}: {http.redact(str(exc))[:160]}"})
                characters_out[rid] = {"status": "fetch_error", "fetchedAt": http.iso_now()}
                self.f.warnings.append(f"enrichment crashed for id {rid}: {type(exc).__name__}")

        entries = {rid: work[rid]["entry"] for rid in ordered_ids}
        statuses = Counter(e["status"] for e in entries.values())

        # 2.8 failure policy: no token ever obtained (broken credentials) or every
        # character failed -> nonzero exit, previous artifacts left untouched.
        if self.client.tokens_minted == 0 and self.client.requests_made > 0:
            print("FATAL: no request ever obtained a token (credentials?); "
                  "previous artifacts left untouched")
            return 1
        if statuses["fetch_error"] == len(ordered_ids):
            print("FATAL: every character failed to fetch; previous artifacts left untouched")
            for rid in ordered_ids[:5]:
                print(f"  {rid}: {http.redact(work[rid]['entry']['note'] or '')}")
            return 1

        # H4 mass-flip floor: asked for here so the refusal can be printed with the
        # run summary, enforced again inside artifacts.write_pair (single writer).
        # The criterion itself is not recomputed at this call site: both sides ask
        # the artifacts layer, so the pre-check and the writer cannot drift.
        refusal = artifacts.mass_flip_refusal(entries, ordered_ids, prev_entries)
        if refusal:
            self.emit_summary(self.summary_lines(statuses, len(ordered_ids),
                                                 time.monotonic() - started, [refusal]))
            return 1

        # The write itself is the single-writer path in bnet.artifacts: it re-checks
        # the floor, applies schema v2 and adds the triggeredBy provenance key.
        artifacts.write_pair(entries, characters_out, ordered_ids=ordered_ids,
                             prev_entries=prev_entries,
                             triggered_by_value=artifacts.triggered_by(),
                             dry_run=self.args.dry_run)
        self.cache.save()

        extra = []
        if pruned:
            extra.append(f"pruned stale map rows (roster ids no longer present): {', '.join(pruned)}")
        if self.args.dry_run:
            extra.append("dry-run: battle-net-map.json and character-data.json were NOT written")
        self.emit_summary(self.summary_lines(statuses, len(ordered_ids), time.monotonic() - started, extra))
        return 0

    # -- lookup mode ------------------------------------------------------------- #
    def run_lookup(self, name, roster_records, prev_entries):
        started = time.monotonic()
        norm = resolve.norm_slug(name)
        # H2: never replay a persisted href that left the allowlist.
        seeds = [prev_entries[rec["id"]]["href"] for rec in roster_records
                 if isinstance(prev_entries.get(rec["id"]), dict)
                 and prev_entries[rec["id"]].get("status") == "ok"
                 and isinstance(prev_entries[rec["id"]].get("href"), str)
                 and http.allowed_api_url(prev_entries[rec["id"]]["href"])]
        result = self.resolver.lookup_name(name, seeds)
        status, payload = result["status"], result["payload"]
        href, matched, via = result["href"], result["matched"], result["via"]

        lines = [f"Battle.net lookup: {name} (normalized {norm})"]
        lines.append(f"result: {status}")
        if isinstance(payload, dict):
            lines.append(f"identity: {payload.get('name')} {payload.get('surname') or ''}".rstrip())
            lines.append(f"href: {href}")
            lines.append(f"level: {payload.get('level')}, class id: "
                         f"{(payload.get('character_class') or {}).get('id') if isinstance(payload.get('character_class'), dict) else '?'}, "
                         f"guild: {fetch.loc((payload.get('guild') or {}).get('name')) if isinstance(payload.get('guild'), dict) else 'none'}")
            lines.append(f"top-level keys: {', '.join(sorted(payload))}")
        if result["candidates"]:
            lines.append(f"candidates ({len(result['candidates'])}):")
            for c in result["candidates"]:
                lines.append(f"  - {c['slug']} ({c['name']} {c['surname'] or ''})".rstrip()
                             + f" via {c['matchedForm']}")
        rows = [rec for rec in roster_records if resolve.norm_slug(rec["name"]) == norm]
        trusted_rows = [rec for rec in rows if roster.is_trusted(rec)]
        write_refusal = None
        if isinstance(payload, dict) and len(rows) == 1 and trusted_rows:
            lines.append("map: unchanged — this row is user-confirmed/validated; a lookup never rewrites a trusted slug")
        elif isinstance(payload, dict) and len(rows) == 1 \
                and (prev_entries.get(rows[0]["id"]) or {}).get("status") not in ("ok",):
            entry = artifacts.ok_entry(rows[0], slug=resolve.slug_from_href(href), href=href, payload=payload,
                                       matched_form=matched, mapped_via=via)
            if self.args.dry_run:
                lines.append(f"map: would update id {rows[0]['id']} -> ok ({via}) (dry-run, not written)")
            else:
                # Both artifacts or neither, even for one row: the UI joins map and
                # sidecar by roster id, so publishing the ok map row while
                # character-data still said not_found for that id would render a
                # resolved badge over a grey panel. The lookup therefore builds the
                # sidecar row first and writes through the same write_pair the
                # nightly sync uses (H4 floor included), carrying every other
                # character's previous row forward untouched.
                rid = rows[0]["id"]
                w = {"rec": rows[0], "entry": entry, "profile": payload,
                     "profile_url": href, "unchanged": False}
                char_row = None
                try:
                    char_row = self.f.enrich(w)
                except http.UnsafeUrl:
                    # H1: an off-allowlist href outranks a lookup report. Propagate:
                    # main() refuses the run, writes nothing, never sends the token.
                    raise
                except Exception as exc:
                    lines.append(f"map: NOT updated for id {rid} — the sidecar row could not be built "
                                 f"({type(exc).__name__}: {http.redact(str(exc))[:160]}); "
                                 "both artifacts or neither, so nothing was written")
                if char_row is not None:
                    entries = dict(prev_entries)
                    entries[rid] = entry
                    characters = artifacts.load_previous_data()
                    characters[rid] = char_row
                    write_refusal = artifacts.write_pair(
                        entries, characters,
                        ordered_ids=[rec["id"] for rec in roster_records],
                        prev_entries=prev_entries,
                        triggered_by_value=artifacts.triggered_by(),
                        dry_run=self.args.dry_run)
                    if write_refusal is None:
                        lines.append(f"map: updated id {rid} -> ok ({via}); "
                                     "character-data row written in the same write_pair")
                    else:
                        lines.append(write_refusal)
        elif isinstance(payload, dict) and len(rows) > 1:
            lines.append(f"map: unchanged — {len(rows)} roster rows share the normalized name {norm}; "
                         "never auto-pick between them")
        else:
            lines.append("map: unchanged")

        stats = (f"requests: {self.client.requests_made} | 304 revalidations: {self.client.revalidations} | "
                 f"tokens minted: {self.client.tokens_minted} | runtime: {time.monotonic() - started:.1f}s")
        lines.append(stats)
        markdown = ["## Battle.net lookup summary", ""] + [f"- {http.redact(line)}" for line in lines]
        for line in lines:
            print(http.redact(line))
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            try:
                with open(step_summary, "a", encoding="utf-8") as handle:
                    handle.write("\n".join(markdown) + "\n")
            except OSError as exc:
                print(f"  ! could not append to GITHUB_STEP_SUMMARY: {type(exc).__name__}")
        if not self.args.dry_run:
            self.cache.save()
        if self.client.tokens_minted == 0:
            print("FATAL: lookup never obtained a token")
            return 1
        if write_refusal:
            # H4 refused the pair (already printed above): the previous artifacts
            # stand, and the caller must see that the requested update did not land.
            return 1
        return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m bnet sync",
                                     description="Nightly Battle.net roster sync (Phase 2, modular)")
    parser.add_argument("--lookup", metavar="NAME", default=None,
                        help="maintainer lookup mode for one character name")
    parser.add_argument("--force", action="store_true",
                        help="ignore cached Last-Modified timestamps and refetch")
    parser.add_argument("--dry-run", action="store_true", help="write no files")
    parser.add_argument("--state-dir", default=str(config.DEFAULT_STATE_DIR),
                        help=f"cache directory (default {config.DEFAULT_STATE_DIR})")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not http.REDACTOR.client_id or not http.REDACTOR.client_secret:
        print("FATAL: BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET must both be set in the "
              "environment; values are never printed", file=sys.stderr)
        return 1
    try:
        roster_records = roster.load_roster()
    except (OSError, ValueError) as exc:
        print(f"FATAL: cannot load {config.ROSTER_FILE.relative_to(config.REPO)}: {http.redact(str(exc))}",
              file=sys.stderr)
        return 1
    prev_entries = artifacts.load_previous_map()

    sync = Sync(args)
    try:
        if args.lookup:
            return sync.run_lookup(args.lookup, roster_records, prev_entries)
        return sync.run_sync(roster_records, prev_entries)
    except http.TokenMintFailed as exc:
        # Fatal by policy (2.8): no token means no trustworthy data, so the
        # previous artifacts stay exactly as they are.
        print(f"FATAL: {http.redact(str(exc))} — previous artifacts left untouched", file=sys.stderr)
        return 1
    except http.UnsafeUrl as exc:
        # H1 net: in sync mode every client.get sits behind a per-character handler,
        # so reaching here means lookup mode met a hostile href. Refuse, write
        # nothing, and never send the credential.
        print(f"FATAL: {http.redact(str(exc))} — nothing was written", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
