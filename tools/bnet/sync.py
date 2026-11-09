"""``python3 -m bnet sync`` — the nightly Battle.net sync orchestrator.

Provenance: the orchestration, run summary and lookup mode of
``tools/sync_bnet.py`` (Phase 2), re-homed onto the modular package in
docs/refactor-design.md §5. The security invariants were not rewritten, only
moved: redaction/allowlist/no-redirect live in ``bnet.http``, the artifact
schemas and the H4 mass-flip floor in ``bnet.artifacts``, the H2 URL validators
and the enrichment fetches in ``bnet.fetch``.

Since Phase C the run is also an ADDITIVE writer of ``data/characters.json``
(``bnet.autoadd`` decides which Battle.net-visible guild members are missing and
``artifacts.write_roster`` writes the schemaVersion-2 envelope): existing rows are never
mutated, reordered or removed, the append is proven against the rows as this run found
them before anything is published, and with nothing to add the roster is not written at
all.

Artifacts (written through ``bnet.artifacts``, the single writer):

    data/battle-net-map.json      roster-id -> Battle.net identity map (schema v2)
    data/character-data.json      normalized sidecar consumed by the site UI (schema v2)
    data/talent-trees.json        spec talent-tree geometry, pinned to the static
                                  namespace and keyed by spec id (T1.3) — shared
                                  reference data, so it joins neither pair above
    data/class-icons.json         every playable class icon, keyed by Blizzard's own
    data/profession-icons.json    class/profession name and pinned to the static
                                  namespace (P5.B, notes §11) — the same
                                  shared-reference category as the trees above

Credentials come from BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET, are never
printed, logged or written, and every string that could carry one passes
through ``http.redact()`` first.

Usage:
    python3 -m bnet sync [options]                # from tools/, or PYTHONPATH=tools
    set -a && . ./.env && set +a && python3 -m bnet sync

Options:
    --lookup NAME   maintainer lookup: GET the name, also try the guild-roster
                    lever, print the full result, and — whenever exactly one
                    untrusted roster row carries the normalized name — flip that
                    one row through the same write_pair as the nightly run: a
                    resolved name publishes ok (with the enriched sidecar row), a
                    multi-hit publishes the ambiguous verdict with every candidate
                    (the review modal renders the committed map, so a Refresh that
                    only printed the list left the user waiting for nightly), and a
                    definite miss publishes not_found. An unresolved verdict never
                    fetches gear: its sidecar row gets the same minimal treatment
                    the nightly gives every non-ok id. The map row and its
                    character-data row land together or not at all.
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
import re
import sys
import time
from collections import Counter

from . import artifacts, autoadd, config, fetch, http, resolve, roster


# --------------------------------------------------------------------------- #
# Log hygiene
# --------------------------------------------------------------------------- #
# CSI sequences (colour, cursor movement): everything from ESC [ plus the parameter/intermediate
# bytes through the final byte.
ANSI_RE = re.compile(r"\x1b\[[0-9:;<=>?]*[ -/]*[@-~]")
LOG_LINE_MAX = 300


def log_safe(value):
    """One printable line, for stdout and for GITHUB_STEP_SUMMARY.

    A roster note, a candidate name or an exception message is untrusted text. The committed
    artifact field stays verbatim (and capped at roster.REPORT_NOTE_MAX), but the log is read by a
    maintainer as line-oriented text: a value carrying a newline could start a fresh line that
    looks like the sync's own output (a forged "FATAL" or a fake summary row), and an ANSI
    sequence could recolour or overwrite what came before it. So every control character becomes a
    space, runs of whitespace collapse, and the result is truncated. Applied at the log boundary
    only — never to an artifact field.
    """
    text = ANSI_RE.sub("", str(value if value is not None else ""))
    text = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in text)
    return " ".join(text.split())[:LOG_LINE_MAX]


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
        if f.render_failures:
            # Phase B: a render this run could not validate is a published null, not a
            # failed fetch — so it is a NOTE, and it never shares the sub-failure line.
            lines.append(f"NOTE: {f.render_failures} character render(s) not validated this run; "
                         "media.fullRender is null for those rows (the UI shows the silhouette)")
        if f.sub_failures:
            # The exact status per refused surface (T1.7: an endpoint failure is a
            # recorded error, never a silently missing feature block).
            lines.append("WARNING: sub-resource fetch failures: "
                         + "; ".join(f"{surface}=HTTP {status}" for surface, status in f.sub_failures[:12])
                         + (" …" if len(f.sub_failures) > 12 else ""))
        if f.feature_failures:
            lines.append(f"WARNING: {len(f.feature_failures)} optional feature block(s) not stored: "
                         + "; ".join(f.feature_failures[:10])
                         + (" …" if len(f.feature_failures) > 10 else ""))
        if f.icon_map_failures:
            # Per entry, exactly like feature_failures: an omitted class or profession
            # icon is a finding with a cause, never a silently shorter map (notes §11).
            lines.append(f"WARNING: {len(f.icon_map_failures)} static icon map entry(ies) refused: "
                         + "; ".join(f.icon_map_failures[:12])
                         + (" …" if len(f.icon_map_failures) > 12 else ""))
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
            print(log_safe(http.redact(line)))
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if not step_summary:
            return
        markdown = [markdown_heading, ""] + [f"- {log_safe(http.redact(line))}" for line in lines]
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write("\n".join(markdown) + "\n")
        except OSError as exc:
            print(f"  ! could not append to GITHUB_STEP_SUMMARY: {type(exc).__name__}")

    # -- static reference artifacts (talent trees, Phase 5b icon maps) ---------- #
    def refresh_icon_maps(self, dry_run):
        """Rebuild the two Phase 5b static icon maps and write them (notes §11).

        Called only after a run has published its pair, for the same reason the
        talent trees are written there: a refused or crashed run must not leave a
        half-updated reference dataset behind. Each map is one index GET plus one
        media GET per distinct id, and entries are carried over from the committed
        file while the pinned static namespace is unchanged, so a same-patch nightly
        costs the two index GETs and no media request at all (§11.4.2). Refusals are
        reported per entry through f.icon_map_failures.

        A map that produced no entry at all is NOT written: the committed file stays
        as published, because "the index was unreachable this hour" must not reach
        the site as "this game has no classes".
        """
        if not self.f.static_ns:
            return []
        lines = []
        for label, build, path in (("class", self.f.class_icon_map, config.CLASS_ICON_FILE),
                                   ("profession", self.f.profession_icon_map,
                                    config.PROFESSION_ICON_FILE)):
            try:
                icons, index_total, carried = build(artifacts.load_previous_icon_map(path))
            except http.BudgetExhausted as exc:
                # The request cap outranks reference data: keep the committed maps.
                self.budget_hit = True
                lines.append(f"{label} icons: NOT rebuilt — request cap reached "
                             f"({http.redact(str(exc))[:120]})")
                break
            except http.UnsafeUrl as exc:
                lines.append(f"{label} icons: NOT rebuilt — the URL allowlist refused the route "
                             f"({http.redact(str(exc))[:120]})")
                continue
            if not icons:
                lines.append(f"{label} icons: NOT written — no index entry resolved an icon this run; "
                             f"previous {path.name} left untouched")
                continue
            document = artifacts.write_icon_map(path, self.f.static_ns, icons, dry_run=dry_run)
            lines.append(f"{label} icons: {len(icons)} of {index_total} index entries landed "
                         f"({carried} carried from the committed map, "
                         f"{len(document['icons'])} published) pinned to {self.f.static_ns}"
                         + (" (dry-run, not written)" if dry_run else ""))
        return lines

    # -- Phase C: additive guild-member auto-add -------------------------------- #
    def discover_guilds(self, work):
        """``{guild slug: display name}`` for every guild this run made discoverable.

        A guild slug is only ever discoverable from a resolved member's profile (notes
        §7.2: there is no guild search), so the profiles the resolver already fetched are
        the whole source and this reads nothing."""
        found = {}
        for w in work.values():
            profile = w.get("profile")
            guild = profile.get("guild") if isinstance(profile, dict) else None
            if not isinstance(guild, dict):
                continue
            slug = guild.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            name = guild.get("name")
            name = name.strip() if isinstance(name, str) else ""
            # A profile that carries the name wins over one that omitted it, whatever the
            # iteration order was; the slug is still enough to fetch the roster.
            if slug not in found or (not found[slug] and name):
                found[slug] = name
        return found

    def auto_add(self, roster_records, ordered_ids, work):
        """Append one pending row per visible guild member the roster does not list yet.

        Fed by the guild rosters this run already fetched: ``Fetcher.guild_members`` caches
        per run, so the members read here are the payload the guild-lever pass above
        resolved against — no roster is fetched twice. Each new id then goes through that
        same lever, which resolves it from the very member that justified the row (one
        profile GET, which the lever spends for any guild-found member anyway), so a new row
        carries a real map verdict — and therefore a map row and a sidecar row — in the run
        that created it.

        Placement is deliberate: after the resolver's passes (that is what produces the
        guild data) and before the ``not_found`` fallback, the slug dedupe, the H4 floor and
        the write, so a new id is a full citizen of this run's output and the
        ``set(map entries) == set(roster ids)`` gate still holds with the row present.

        Returns ``(rows added, summary lines)``; the caller owns whether anything is
        published (and refuses the whole run if the append is not purely additive).
        """
        lines = []
        discovered = self.discover_guilds(work)
        if not discovered:
            return [], lines
        guilds = {}
        for slug in sorted(discovered):
            try:
                members = self.f.guild_members(slug)
            except http.BudgetExhausted as exc:
                self.budget_hit = True
                lines.append(f"auto-add: stopped at the request cap ({http.redact(str(exc))[:120]})")
                break
            except http.UnsafeUrl as exc:
                lines.append(f"auto-add: skipped {slug}, the allowlist refused its roster route "
                             f"({http.redact(str(exc))[:120]})")
                continue
            if members is None:
                # Already reported by the run summary's guild-fetch warning; it must never
                # read as "this guild has no new members", so nothing is added from it.
                self.f.guild_fetch_failures.add(slug)
                continue
            guilds[slug] = {"name": discovered[slug], "members": members}
        rows = autoadd.plan(roster_records, guilds)
        if not rows:
            return [], lines
        for row in rows:
            roster_records.append(row)
            ordered_ids.append(row["id"])
            work[row["id"]] = {"rec": row, "entry": None, "profile": None,
                               "profile_url": None, "unchanged": False}
        try:
            self.resolver.guild_lever(work, [r["id"] for r in rows])
        except http.BudgetExhausted as exc:
            self.budget_hit = True
            lines.append(f"auto-add: resolution stopped at the request cap "
                         f"({http.redact(str(exc))[:120]})")
        except http.UnsafeUrl as exc:
            lines.append(f"auto-add: resolution stopped by the URL allowlist "
                         f"({http.redact(str(exc))[:120]})")
        lines.append(f"auto-add: {len(rows)} pending row(s) appended for Battle.net-visible guild "
                     f"member(s) the roster did not list: "
                     + ", ".join(f"{r['name']} ({r['id']})" for r in rows[:8])
                     + (" …" if len(rows) > 8 else ""))
        return rows, lines

    # -- full sync ------------------------------------------------------------- #
    def run_sync(self, roster_records, prev_entries):
        started = time.monotonic()
        # The rows as this run found them, kept so the append below can be proven purely
        # additive before anything is written (bnet.artifacts.roster_append_refusal).
        roster_before = [rec for rec in roster_records]
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

        # Phase C: visible guild members the tracker does not list yet become pending rows
        # here, and are resolved through the lever above before the verdicts below are read.
        auto_added, auto_lines = self.auto_add(roster_records, ordered_ids, work)

        for rid in ordered_ids:
            w = work[rid]
            if w["entry"] is None:
                note = "404 on all forms — likely not opted in (task 0.4)"
                if self.f.guild_fetch_failures:
                    note += f"; guild roster fetch failed for {', '.join(sorted(self.f.guild_fetch_failures))}"
                w["entry"] = artifacts.map_entry(w["rec"], "not_found", note=note)

        self.resolver.dedupe_slugs(work, ordered_ids)

        # H4 mass-flip floor, evaluated on the RESOLVER verdict and BEFORE the reviewer-note
        # pass (artifacts.resolver_verdict). The floor exists for a Blizzard-wide 404 storm, and
        # only this snapshot separates that from a mass "report mismatch", which says nothing
        # about upstream health. The order matters twice over: the note rule runs after this
        # check and still before the write, so the published map keeps every reported row at
        # needs_review (the gate in .pi/scripts/verify_sync_artifacts.py), while user-driven
        # demotions can neither freeze the nightly pipeline nor hide a real outage from the floor.
        resolver_verdict = artifacts.resolver_verdict(
            {rid: work[rid]["entry"] for rid in ordered_ids}, ordered_ids)
        verdict_statuses = Counter(e["status"] for e in resolver_verdict.values())
        # The roster may only ever GROW (Phase C). Checked before anything is written, so a
        # run that somehow lost, reordered or rewrote a row publishes nothing at all — the
        # map, the sidecar and the roster are one atomic publication or none.
        append_refusal = artifacts.roster_append_refusal(roster_before, roster_records)
        if append_refusal:
            statuses_now = {rid: work[rid]["entry"]["status"] for rid in ordered_ids
                            if isinstance(work[rid].get("entry"), dict)}
            self.emit_summary(self.summary_lines(statuses_now, len(ordered_ids),
                                                 time.monotonic() - started,
                                                 [append_refusal] + auto_lines))
            return 1

        refusal = artifacts.mass_flip_refusal(resolver_verdict, ordered_ids, prev_entries)
        if refusal:
            # Printed with the run summary as before; the statuses shown are the ones the floor
            # was decided on, and nothing was enriched for a run that is about to refuse.
            self.emit_summary(self.summary_lines(verdict_statuses, len(ordered_ids),
                                                 time.monotonic() - started, [refusal] + auto_lines))
            return 1

        # A reviewer's open report outranks whatever this run resolved: the row stays
        # needs_review until a validated commit clears it (design §2). Still deliberately before
        # the enrichment loop, so no fully enriched sidecar row is published under a mapping a
        # human has rejected — the map row keeps the slug it resolved to, the sidecar row stays
        # minimal.
        self.resolver.apply_reported_notes(work, ordered_ids)

        # Talent-tree definitions are version-pinned static data (§9.3.2): seed
        # the cache from the committed document so a run on the same patch re-
        # fetches nothing. static_ns is already discovered at this point (the
        # resolution loop above ran first).
        self.f.seed_talent_trees(artifacts.load_previous_talent_trees())

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
                # A map note can be a reviewer's own text (the note rule passes it through), so it
                # is printed as one sanitized line rather than verbatim.
                print(f"  {rid}: {log_safe(http.redact(work[rid]['entry']['note'] or ''))}")
            return 1

        # The write itself is the single-writer path in bnet.artifacts: it re-checks the floor,
        # applies schema v2 and adds the triggeredBy provenance key. The floor is handed the same
        # resolver-verdict snapshot the pre-check above used, so the writer cannot recompute the
        # decision from the post-note map and deadlock a run the verdict cleared (and a refusal
        # here still costs nothing: nothing has been written).
        write_refusal = artifacts.write_pair(entries, characters_out, ordered_ids=ordered_ids,
                                             prev_entries=prev_entries,
                                             triggered_by_value=artifacts.triggered_by(),
                                             dry_run=self.args.dry_run,
                                             floor_entries=resolver_verdict)
        if write_refusal:
            self.emit_summary(self.summary_lines(statuses, len(ordered_ids),
                                                 time.monotonic() - started, [write_refusal] + auto_lines))
            return 1
        self.cache.save()

        extra = []
        if auto_added:
            # Written last and only now: the pair is on disk, the floor cleared and the
            # append was proven additive, so the roster can grow to cover the ids the pair
            # already publishes. With nothing to append this writer is never called, and the
            # committed roster's bytes stay exactly as they were.
            artifacts.write_roster(roster_records, dry_run=self.args.dry_run)
            extra.extend(auto_lines
                         + [f"roster: {len(roster_records)} row(s) written to "
                            f"{config.ROSTER_FILE.name}" + (" (dry-run, not written)" if self.args.dry_run else "")])

        if self.f.static_ns and self.f.talent_trees:
            # Written only after the pair landed: a run that refused or crashed
            # never publishes half a reference dataset.
            artifacts.write_talent_trees(self.f.static_ns, self.f.talent_trees, dry_run=self.args.dry_run)
            extra.append(f"talent trees: {len(self.f.talent_trees)} spec(s) pinned to {self.f.static_ns}"
                         + (" (dry-run, not written)" if self.args.dry_run else ""))
        extra.extend(self.refresh_icon_maps(self.args.dry_run))
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
        # A lookup publishes through the same write_pair as the nightly run whenever the verdict
        # can be attributed to exactly one untrusted roster row: resolved -> ok, several distinct
        # profiles -> ambiguous (the candidates are what the review modal renders, and the modal
        # reads the committed map, so printing them and writing nothing left the Refresh flow dead
        # for ambiguous characters until a nightly happened to cover them), and a definite miss ->
        # not_found. Everything else stays report-only: a trusted row is never rewritten by a
        # lookup, two roster rows sharing one normalized name cannot be told apart by the profile,
        # and an HTTP anomaly that stopped the walk (or a guild member whose profile GET failed)
        # says nothing trustworthy about this character's identity — writing it as not_found would
        # let one transient 5xx erase a mapped ok row that only the nightly's full pass may judge.
        verdict_entry = None
        if len(rows) == 1 and not trusted_rows:
            if isinstance(payload, dict):
                if (prev_entries.get(rows[0]["id"]) or {}).get("status") not in ("ok",):
                    verdict_entry = artifacts.ok_entry(rows[0], slug=resolve.slug_from_href(href), href=href,
                                                       payload=payload, matched_form=matched, mapped_via=via)
            elif result["candidates"] and str(result.get("status") or "").startswith("ambiguous"):
                verdict_entry = artifacts.ambiguous_entry(
                    rows[0], candidates=list(result["candidates"]),
                    note=str(result["status"]), matched_via=via)
            elif str(result.get("status") or "").startswith("no match"):
                verdict_entry = artifacts.map_entry(rows[0], "not_found", note=str(result["status"]))
        if len(rows) == 1 and trusted_rows:
            lines.append("map: unchanged — this row is user-confirmed/validated; a lookup never rewrites a trusted slug")
        elif len(rows) > 1:
            lines.append(f"map: unchanged — {len(rows)} roster rows share the normalized name {norm}; "
                         "never auto-pick between them")
        elif verdict_entry is not None:
            rid = rows[0]["id"]
            # The same rule as the nightly run — a lookup is a sync run too: an open reviewer note
            # keeps the map row needs_review (carrying the identity or the candidates across, so
            # the review modal still has something to offer). Here the ok sidecar row is still
            # written from the fresh profile, because a lookup is an explicit maintainer action
            # and the pair must stay joinable by id; the map, not the sidecar, is what the UI
            # derives lifecycle from.
            # `verdict_entry` is what this run actually resolved, kept for the H4 floor: the note
            # is applied after the verdict and before the write, exactly as in the nightly path.
            entry = verdict_entry
            entry = resolve.reported_note_entry(rows[0], entry)
            verdict_via = entry.get("mappedVia") or via or "no match"
            if self.args.dry_run:
                # entry["status"] not a literal "ok": a reviewer note on this row keeps it
                # needs_review even though the lookup itself resolved cleanly.
                lines.append(f"map: would update id {rid} -> {entry['status']} ({verdict_via}) (dry-run, not written)")
            else:
                # Both artifacts or neither, even for one row: the UI joins map and
                # sidecar by roster id, so publishing the ok map row while
                # character-data still said not_found for that id would render a
                # resolved badge over a grey panel. The lookup therefore builds the
                # sidecar row first and writes through the same write_pair the
                # nightly sync uses (H4 floor included), carrying every other
                # character's previous row forward untouched.
                char_row = None
                if isinstance(payload, dict):
                    w = {"rec": rows[0], "entry": entry, "profile": payload,
                         "profile_url": href, "unchanged": False}
                    # Same talent-tree seeding as the nightly run; the static ns
                    # is known because the lookup above fetched a profile.
                    self.f.seed_talent_trees(artifacts.load_previous_talent_trees())
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
                else:
                    # ambiguous / not_found: the same minimal sidecar row the nightly gives every
                    # non-ok id. Gear is never fetched under an identity nobody resolved — an
                    # enriched row would publish character data under a mapping (or a missing
                    # one) the reviewer has not confirmed.
                    char_row = {"status": entry["status"], "fetchedAt": http.iso_now()}
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
                        dry_run=self.args.dry_run,
                        floor_entries=artifacts.resolver_verdict(
                            {**prev_entries, rid: verdict_entry},
                            [rec["id"] for rec in roster_records]))
                    if write_refusal is None:
                        lines.append(f"map: updated id {rid} -> {entry['status']} ({verdict_via}); "
                                     "character-data row written in the same write_pair")
                        if self.f.static_ns and self.f.talent_trees:
                            artifacts.write_talent_trees(self.f.static_ns, self.f.talent_trees,
                                                         dry_run=self.args.dry_run)
                            lines.append(f"talent trees: {len(self.f.talent_trees)} spec(s) pinned to "
                                         f"{self.f.static_ns}")
                        # A lookup is a full run for one character, and the icon maps
                        # are run-wide reference data: rebuilding them here (free on an
                        # unchanged namespace) keeps a maintainer-initiated run from
                        # publishing a sidecar whose icons the maps cannot resolve.
                        lines.extend(self.refresh_icon_maps(self.args.dry_run))
                    else:
                        lines.append(write_refusal)
        else:
            lines.append("map: unchanged")

        stats = (f"requests: {self.client.requests_made} | 304 revalidations: {self.client.revalidations} | "
                 f"tokens minted: {self.client.tokens_minted} | runtime: {time.monotonic() - started:.1f}s")
        lines.append(stats)
        # The same single log boundary as the nightly run: one printable, redacted line per
        # summary row on stdout and in the step summary (the lookup report quotes a name, a note
        # and an href, all of them untrusted).
        self.emit_summary(lines, "## Battle.net lookup summary")
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
        # The message quotes a roster id or a JSON error, so it goes through the same one-line
        # sanitizer as every other log write.
        print(f"FATAL: cannot load {config.ROSTER_FILE.relative_to(config.REPO)}: "
              f"{log_safe(http.redact(str(exc)))}", file=sys.stderr)
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
        print(f"FATAL: {log_safe(http.redact(str(exc)))} — previous artifacts left untouched", file=sys.stderr)
        return 1
    except http.UnsafeUrl as exc:
        # H1 net: in sync mode every client.get sits behind a per-character handler,
        # so reaching here means lookup mode met a hostile href. Refuse, write
        # nothing, and never send the credential.
        print(f"FATAL: {log_safe(http.redact(str(exc)))} — nothing was written", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
