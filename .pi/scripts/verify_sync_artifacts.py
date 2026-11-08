#!/usr/bin/env python3
"""Deterministic checks for the Phase 2 sync artifacts (schema v2, data/ layout).

Exit 0 = every clause holds; exit 1 = each failing clause was printed. The
load-bearing assertions: the map covers exactly the CURRENT roster ids, ok rows
carry real Battle.net identity, ambiguous rows carry candidates and no slug, the
sidecar contains only whitelisted normalized keys (no raw Blizzard blobs), no
credential material appears in any committed surface (including the roster and
the unit-test fixtures), NO root-level copy of characters.json /
battle-net-map.json / character-data.json exists in the worktree (a root copy is
a UI/legacy fork of the roster, not this pipeline's data), the workflow has no
PR/push trigger and an exact permissions block, the pipeline runs the offline
unit tests then the modular ``python3 -m bnet sync`` entrypoint under a job
``timeout-minutes`` and commits under ``data/``, and the sync package reuses
Last-Modified conditional GETs (amendment 3: If-Modified-Since yes, the
If-None-Match flavour of task 2.4 is dead — no response ever carried an ETag).

Phase 2 moved the artifacts to ``data/`` and the implementation from the two
root scripts into the ``tools/bnet/`` package, so the source-level security
checks below read that package. The invariant text was carried over unchanged;
only the file each check reads changed (and the mass-flip floor moved from an
ordering check to a single-writer check inside ``bnet.artifacts``).
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools" / "bnet"
ROSTER = ROOT / "data" / "characters.json"
MAP = ROOT / "data" / "battle-net-map.json"
DATA = ROOT / "data" / "character-data.json"
WORKFLOW = ROOT / ".github" / "workflows" / "sync-bnet.yml"
ENV = ROOT / ".env"

SYNC_MODULES = ("__init__.py", "__main__.py", "config.py", "http.py", "roster.py",
                "resolve.py", "fetch.py", "artifacts.py", "sync.py")
PKG_MODULES = SYNC_MODULES + ("probe.py",)

STATUS_VOCAB = {"ok", "ambiguous", "not_found", "needs_review", "fetch_error"}
MAP_KEYS = {"rosterName", "status", "slug", "href", "bnetName", "surname",
            "matchedForm", "mappedVia", "note", "candidates"}
CANDIDATE_KEYS = {"slug", "name", "surname", "matchedForm", "href"}
MAPPED_VIA = {"direct", "guild_roster", "confirmed"}
ROSTER_FIELDS = {"id", "player", "name", "track", "role", "roleSec", "race", "class", "prof1", "prof2"}
VALIDATION_KEYS = {"state", "confirmedSlug", "reviewedBy", "reviewedAt"}
ROSTER_KEYS = ROSTER_FIELDS | {"validation"}
CHAR_KEYS = {"status", "bnet", "level", "guild", "activeSpec", "itemLevel",
             "gear", "progress", "achievementPoints", "fetchedAt"}
BNET_KEYS = {"name", "surname", "href"}
GEAR_KEYS = {"slot", "name", "ilvl", "iconUrl"}
PROGRESS_KEYS = {"raid", "mythicPlus", "pvp"}
# Keys that only exist in raw Blizzard payloads: their presence would mean the
# sync dumped unnormalized data into the public sidecar.
RAW_BLOB_KEYS = {"_links", "equipped_items", "specialization_groups", "category_progress",
                 "criteria", "enchantments", "sell_price", "header_names", "body_snippet"}
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Security review 2026-11-08: icons are the only third-party asset the site loads,
# and they live on exactly one Blizzard host.
ICON_URL_RE = re.compile(r"^https://render\.worldofwarcraft\.com/")
ARTIFACT_HOSTS = {"us.api.blizzard.com", "us.battle.net", "render.worldofwarcraft.com"}
PROFILE_HREF_RE = re.compile(r"^https://us\.api\.blizzard\.com/profile/wow/character/[^/?]+/?(\?.*)?$")
TOKEN_LITERAL_RE = re.compile(r"(?:Basic|Bearer)\s+(?!\$|\{\{|f\"|f')[A-Za-z0-9+/=_-]{20,}")
MAX_ARTIFACT_BYTES = 500 * 1024

fails = []
passes = 0


def check(cond, msg):
    global passes
    if cond:
        passes += 1
        print(f"  PASS {msg}")
    else:
        fails.append(msg)
        print(f"  FAIL {msg}")


def bail():
    """Exit 1 from an early-return point, printing the tally first.

    Every early return in main() is reached only after a check has already failed
    (unreadable roster, unparsable or wrongly-shaped artifact document). A bare
    ``return`` handed ``None`` to sys.exit, i.e. exit 0, so a forked/poisoned
    artifact set would sail through the CI gate that exists to block it — the
    fork gate below is worthless without this."""
    print(f"\n{passes} passed, {len(fails)} failed")
    for item in fails:
        print(f"  - {item}")
    return 1


def norm_slug(name):
    return "-".join(str(name).lower().split())


def walk_keys(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(str(key))
            walk_keys(value, out)
    elif isinstance(node, list):
        for item in node:
            walk_keys(item, out)


def read(path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def workflow_steps(text):
    """[(name, block_text)] in file order, for the steps list of the last job.

    Dependency-free line scan rather than PyYAML: nothing about a verification
    gate may depend on a package the runner happens to have, and the only fact
    that matters here is the ORDER of the steps (verify before commit).
    """
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^\s*steps:\s*$", ln)]
    if not starts:
        return []
    item_indent, blocks = None, []
    for ln in lines[starts[-1] + 1:]:
        m = re.match(r"^(\s*)-\s", ln)
        if m and (item_indent is None or len(m.group(1)) == item_indent):
            if item_indent is None:
                item_indent = len(m.group(1))
            blocks.append([ln])
        elif blocks:
            blocks[-1].append(ln)
    out = []
    for block in blocks:
        head = next((ln for ln in block if re.match(r"^\s*(?:-\s+)?name:", ln)), block[0])
        out.append((head.split("name:", 1)[1].strip().strip('"').strip("'"), "\n".join(block)))
    return out


def body_of(text, pattern):
    """The text of one def block, for scope-accurate source checks."""
    m = re.search(pattern, text, flags=re.S)
    return m.group(1) if m else ""


def main():
    # --- module sources ------------------------------------------------------ #
    sources = {name: read(PKG / name) for name in PKG_MODULES}
    missing = [n for n, t in sources.items() if t is None]
    check(not missing, f"tools/bnet/ package is complete (missing: {missing})")
    if missing:
        print(f"\n0 passed, {len(missing)} failed")
        return 1
    SYNC_TEXT = "".join(sources[n] for n in SYNC_MODULES)     # the shipped sync surface
    PKG_TEXT = "".join(sources[n] for n in PKG_MODULES)
    CFG, HTTP, ROSTER_MOD, RESOLVE, FETCH, ARTIFACTS, SYNC = (
        sources["config.py"], sources["http.py"], sources["roster.py"],
        sources["resolve.py"], sources["fetch.py"], sources["artifacts.py"], sources["sync.py"])

    # --- fork gate: no root-level copies of the three data files -------------- #
    # Phase 2 moved the roster and both artifacts under data/. A root-level copy of
    # any of these three names in the worktree means this checkout is a UI/legacy
    # fork of the roster: the pre-migration toolchain (and the Phase 3 UI prototype)
    # read and write them at the root, so the sync would publish data/ while a stale
    # duplicate kept being served, and the two copies would diverge silently. The
    # single source of truth is data/<name>; a root copy fails the gate.
    for stale in ("characters.json", "battle-net-map.json", "character-data.json"):
        check(not (ROOT / stale).exists(),
              f"no root-level {stale} in the worktree — its presence means a UI/legacy fork "
              f"of the roster (data/{stale} is the single source of truth)")

    # --- roster baseline ---------------------------------------------------- #
    roster_text = read(ROSTER)
    check(roster_text is not None, "data/characters.json exists")
    if roster_text is None:
        print(f"\n0 passed, 1 failed")
        return 1
    roster_doc = json.loads(roster_text)
    check(isinstance(roster_doc, dict),
          "roster is the v2 object (the v1 bare array is forbidden after the migration commit)")
    roster = roster_doc.get("characters") if isinstance(roster_doc, dict) else None
    check(isinstance(roster, list) and bool(roster), "roster characters is a non-empty list")
    check(set(roster_doc) == {"schemaVersion", "characters"}, "roster top-level keys are exactly schemaVersion/characters")
    check(roster_doc.get("schemaVersion") == 2, "roster schemaVersion == 2")
    for rec in roster or []:
        check(isinstance(rec, dict) and isinstance(rec.get("id"), str) and bool(rec["id"]),
              f"roster record has a string id ({rec.get('id')})")
        check(set(rec) <= ROSTER_KEYS,
              f"roster {rec.get('id')} carries only the ten original fields plus validation")
        v = rec.get("validation")
        if v is not None:
            check(isinstance(v, dict) and set(v) <= VALIDATION_KEYS,
                  f"roster {rec.get('id')} validation block has no unknown keys")
            check(v.get("state") in (None, "validated"),
                  f"roster {rec.get('id')} validation.state is the only stored value (validated)")
    roster_ids = [rec["id"] for rec in roster]
    by_id = {rec["id"]: rec for rec in roster}
    collision_ids = {rid for norm, ids
                     in {n: [r["id"] for r in roster if norm_slug(r["name"]) == n]
                         for n in {norm_slug(r["name"]) for r in roster}}.items()
                     if len(ids) > 1 for rid in ids}
    # the trust rule the sync implements, mirrored here so data and code agree
    trusted_slug = {}
    for rec in roster:
        v = rec.get("validation") or {}
        if v.get("state") == "validated" or v.get("confirmedSlug"):
            trusted_slug[rec["id"]] = v.get("confirmedSlug")

    # --- both artifacts parse ----------------------------------------------- #
    map_text, data_text = read(MAP), read(DATA)
    check(map_text is not None, "data/battle-net-map.json exists")
    check(data_text is not None, "data/character-data.json exists")
    if fails:
        return bail()
    try:
        map_doc = json.loads(map_text)
        data_doc = json.loads(data_text)
        parses = True
    except ValueError as exc:
        parses = False
        check(False, f"artifacts parse as JSON: {exc}")
    if not parses:
        return bail()

    # --- map schema ----------------------------------------------------------- #
    check(map_doc.get("schemaVersion") == 2, "map schemaVersion == 2")
    check(isinstance(map_doc.get("updatedAt"), str) and ISO_RE.match(map_doc["updatedAt"]),
          "map updatedAt is ISO8601 UTC")
    entries = map_doc.get("entries")
    check(isinstance(entries, dict), "map entries is an object")
    if not isinstance(entries, dict):
        return bail()
    check(set(entries) == set(roster_ids),
          f"map covers exactly the current roster ids (map={len(entries)} roster={len(roster_ids)}; "
          f"missing={sorted(set(roster_ids) - set(entries))[:3]} stale={sorted(set(entries) - set(roster_ids))[:3]})")
    for rid, entry in entries.items():
        if not isinstance(entry, dict):
            check(False, f"map entry {rid} is an object")
            continue
        check(set(entry) == MAP_KEYS, f"map entry {rid} has exactly the v2 key set")
        status = entry.get("status")
        check(status in STATUS_VOCAB, f"map entry {rid} status '{status}' in vocabulary")
        check(isinstance(entry.get("candidates"), list), f"map entry {rid} candidates is a list (v2 required)")
        if rid in by_id:
            check(entry.get("rosterName") == by_id[rid]["name"], f"map entry {rid} rosterName matches characters.json")
        # Defence in depth: even a non-ok row must never publish an off-host href.
        check(entry.get("href") is None or PROFILE_HREF_RE.match(entry["href"]) is not None,
              f"entry {rid} href is null or a Blizzard profile URL (no persisted foreign href)")
        for cand in entry.get("candidates") or []:
            check(isinstance(cand, dict) and set(cand) <= CANDIDATE_KEYS,
                  f"map entry {rid} candidate has only slug/name/surname/matchedForm/href")
            if isinstance(cand, dict):
                check(cand.get("href") is None or PROFILE_HREF_RE.match(cand["href"] or "") is not None,
                      f"map entry {rid} candidate href is null or a Blizzard profile URL")
        if status == "ok":
            check(isinstance(entry.get("slug"), str) and bool(entry["slug"]), f"ok entry {rid} has a slug")
            check(isinstance(entry.get("href"), str)
                  and PROFILE_HREF_RE.match(entry["href"]) is not None,
                  f"ok entry {rid} href matches the frozen profile URL shape (H2)")
            check(entry.get("mappedVia") in MAPPED_VIA, f"ok entry {rid} mappedVia is valid")
            check(isinstance(entry.get("bnetName"), str) and bool(entry["bnetName"]),
                  f"ok entry {rid} carries the payload name")
            # Never auto-pick: more than one plausible profile is never an ok row.
            check(len(entry.get("candidates") or []) < 2,
                  f"ok entry {rid} does not auto-pick between multiple candidates")
        elif status == "ambiguous":
            check(entry.get("slug") is None, f"ambiguous entry {rid} keeps slug null (nothing was picked)")
            check(entry.get("href") is None, f"ambiguous entry {rid} publishes no identity href")
            check(len(entry.get("candidates") or []) >= 1,
                  f"ambiguous entry {rid} carries at least one candidate for the reviewer")
        else:
            check(isinstance(entry.get("note"), str) and bool(entry["note"].strip()),
                  f"non-ok entry {rid} carries a human-readable note")
        if rid in collision_ids:
            check(status != "ok", f"duplicate-normalization id {rid} is not auto-mapped (policy)")
        if rid in trusted_slug and status == "ok":
            # Design §3: a confirmedSlug is authoritative; sync never re-resolves it.
            if trusted_slug[rid]:
                check(entry.get("slug") == trusted_slug[rid],
                      f"trusted id {rid} keeps the user-confirmed slug")
            check(entry.get("mappedVia") == "confirmed" and entry.get("note") == "user-confirmed",
                  f"trusted id {rid} ok row is marked confirmed / user-confirmed")

    # --- character-data schema ------------------------------------------------- #
    check(data_doc.get("schemaVersion") == 2, "data schemaVersion == 2")
    check(isinstance(data_doc.get("syncedAt"), str) and ISO_RE.match(data_doc["syncedAt"]),
          "data syncedAt is ISO8601 UTC")
    check(set(data_doc) == {"schemaVersion", "syncedAt", "triggeredBy", "characters"},
          "data top-level keys are exactly schemaVersion/syncedAt/triggeredBy/characters")
    check(isinstance(data_doc.get("triggeredBy"), str) and bool(data_doc["triggeredBy"].strip()),
          f"data triggeredBy is a non-empty provenance string ({data_doc.get('triggeredBy')!r})")
    characters = data_doc.get("characters")
    check(isinstance(characters, dict) and set(characters) == set(roster_ids),
          "data covers exactly the current roster ids")
    if not isinstance(characters, dict):
        return bail()
    for rid, char in characters.items():
        if not isinstance(char, dict):
            check(False, f"data entry {rid} is an object")
            continue
        check(set(char) <= CHAR_KEYS, f"data entry {rid} has no unknown keys")
        status = char.get("status")
        check(status in STATUS_VOCAB, f"data entry {rid} status '{status}' in vocabulary")
        check(isinstance(char.get("fetchedAt"), str) and ISO_RE.match(char["fetchedAt"]),
              f"data entry {rid} fetchedAt is ISO8601")
        if status == "ok":
            bnet = char.get("bnet")
            check(isinstance(bnet, dict) and set(bnet) <= BNET_KEYS and bool(bnet.get("href")),
                  f"ok data entry {rid} has a normalized bnet identity")
            gear = char.get("gear")
            check(isinstance(gear, list), f"ok data entry {rid} gear is a list")
            for item in gear or []:
                if not isinstance(item, dict) or not set(item) <= GEAR_KEYS:
                    check(False, f"data entry {rid} gear item carries only slot/name/ilvl/iconUrl")
                    continue
                icon = item.get("iconUrl")
                # null is the documented outcome when an item-media doc 404s
                # (notes §8: single reference-doc failures are non-fatal). A value
                # off the render host is H2: a persisted attacker URL would be
                # loaded by every visitor's browser and re-fetched nightly.
                check(icon is None or (isinstance(icon, str) and ICON_URL_RE.match(icon)),
                      f"data entry {rid} gear iconUrl matches ^https://render\\.worldofwarcraft\\.com/ or null")
            progress = char.get("progress")
            if progress is not None:
                check(isinstance(progress, dict) and set(progress) <= PROGRESS_KEYS and bool(progress),
                      f"data entry {rid} progress only has data-bearing keys (omit-when-empty)")
    blob_keys = (set(walk_keys(characters, set()) or set()) if characters else set())
    offenders = blob_keys & RAW_BLOB_KEYS
    check(not offenders, f"no raw Blizzard payload keys leaked into the sidecar (found: {sorted(offenders)})")
    # H2: no URL of any kind may reach an artifact unless its host is ours. This
    # scans the raw text, so it also catches a foreign URL smuggled into a note.
    for label, text in (("data/battle-net-map.json", map_text), ("data/character-data.json", data_text)):
        foreign = sorted({u for u in re.findall(r"https?://[^\s\"'<>\\]+", text)
                          if urlparse(u).netloc not in ARTIFACT_HOSTS})
        check(not foreign, f"{label} contains no non-Blizzard URL (found: {foreign[:3]})")
    check(len(map_text) < MAX_ARTIFACT_BYTES and len(data_text) < MAX_ARTIFACT_BYTES,
          f"artifacts are under {MAX_ARTIFACT_BYTES} bytes "
          f"(map={len(map_text)}B data={len(data_text)}B)")

    # --- secret scan ------------------------------------------------------------- #
    secrets = []
    if ENV.exists():
        for line in read(ENV).splitlines():
            _, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if len(value) >= 6:
                secrets.append(value)
    scanned = {f"tools/bnet/{name}": text for name, text in sources.items()}
    # The unit tests run in CI with the credentials in the environment, and the
    # roster is the one data/ file a maintainer edits by hand: both are committed
    # surfaces, so both join the leak scan (a pasted secret in a fixture would
    # otherwise reach main through a path no gate read).
    scanned.update({f"tools/tests/{p.name}": read(p)
                    for p in sorted((ROOT / "tools" / "tests").glob("*.py"))})
    scanned.update({".github/workflows/sync-bnet.yml": read(WORKFLOW),
                    "tools/migrate_v2.py": read(ROOT / "tools" / "migrate_v2.py"),
                    "data/characters.json": roster_text,
                    "data/battle-net-map.json": map_text,
                    "data/character-data.json": data_text})
    for label, text in scanned.items():
        check(text is not None, f"{label} exists for the secret scan")
        if text is None:
            continue
        for secret in secrets:
            check(secret not in text, f"{label} contains no .env secret value")
        # The transport legitimately names the Authorization header; the committed
        # workflow and artifacts must not mention it at all.
        if not label.startswith("tools/"):
            check("Authorization" not in text, f"{label} contains no Authorization literal")
        check(not TOKEN_LITERAL_RE.search(text), f"{label} contains no hardcoded Basic/Bearer token value")

    # --- workflow shape ------------------------------------------------------------ #
    wf = scanned[".github/workflows/sync-bnet.yml"]
    if wf:
        # Comments legitimately discuss why there are no PR triggers; scan only
        # the YAML syntax, never the prose.
        trigger_section = "\n".join(ln for ln in wf.split("jobs:", 1)[0].splitlines()
                                    if not ln.lstrip().startswith("#"))
        check("schedule:" in trigger_section and "0 6 * * *" in trigger_section,
              "workflow schedules nightly at 06:00")
        check("workflow_dispatch:" in trigger_section, "workflow allows manual dispatch")
        check("pull_request" not in trigger_section, "workflow has no pull_request trigger")
        check(not re.search(r"^\s*push:", trigger_section, flags=re.M), "workflow has no push trigger")
        check("lookup_name" in trigger_section and "force" in trigger_section,
              "workflow exposes lookup_name and force inputs")
        perms = re.search(r"^permissions:\n((?:[ \t]+\S.*\n)+)", wf, flags=re.M)
        check(perms is not None and [ln.strip() for ln in perms.group(1).splitlines() if ln.strip()] == ["contents: write"],
              "permissions block is exactly contents: write")
        check("secrets.BLIZZARD_CLIENT_ID" in wf and "secrets.BLIZZARD_CLIENT_SECRET" in wf,
              "both Blizzard secrets are referenced")
        env_lines = [ln for ln in wf.splitlines() if "secrets." in ln]
        check(all(re.match(r"^\s+BLIZZARD_CLIENT_(ID|SECRET):\s*\$\{\{\s*secrets\.", ln) for ln in env_lines),
              "every secrets.* reference is a BLIZZARD_* env assignment")
        check("skip ci]" in wf, "commit message carries [skip ci]")
        check("bnet-sync-bot" in wf, "commit step uses the bot identity")
        check("concurrency" in wf and "sync-bnet" in wf, "workflow declares the sync-bnet concurrency group")
        check("python3 -m bnet sync" in wf, "workflow runs the modular module entrypoint (python3 -m bnet sync)")
        check("PYTHONPATH=tools" in wf or re.search(r"working-directory:\s*tools", wf) is not None,
              "workflow makes the bnet package importable (PYTHONPATH=tools or working-directory: tools)")
        check("tools/sync_bnet.py" not in wf, "workflow no longer invokes the deleted tools/sync_bnet.py")
        add_lines = [ln for ln in wf.splitlines() if re.match(r"^\s*git add ", ln)]
        check(any("data/battle-net-map.json" in ln and "data/character-data.json" in ln for ln in add_lines),
              f"commit step adds both data/ artifacts ({add_lines})")
        check(not re.search(r"git add (?:battle-net-map|character-data)\.json", wf),
              "commit step no longer adds the old root paths")

    # --- untouched machine-owned files ---------------------------------------------- #
    diff = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", "index.html"])
    check(diff.returncode == 0, "index.html is unmodified vs git HEAD (the UI is Phase 3)")

    # --- caching mechanism (amendment 3) --------------------------------------------- #
    check("If-Modified-Since" in SYNC_TEXT, "sync package sends If-Modified-Since")
    check("If-None-Match" not in SYNC_TEXT,
          "sync package never sends If-None-Match (no response has ever carried an ETag)")

    # --- budgets ---------------------------------------------------------------------- #
    check(re.search(r"MAX_REQUESTS\s*=\s*2500", CFG) is not None, "sync per-run cap stays MAX_REQUESTS = 2500")
    check(re.search(r"REQUEST_BUDGET\s*=\s*300", CFG) is not None, "probe budget stays REQUEST_BUDGET = 300")
    check("config.MAX_REQUESTS" in HTTP, "the request cap is enforced from the single config value")

    # --- H1: credentialed requests are host-pinned and never follow redirects -------- #
    origin = re.search(r"ALLOWED_API_ORIGIN_RE\s*=\s*re\.compile\((.*?)\)\n", CFG, flags=re.S)
    allow = origin.group(1) if origin else ""
    check(origin is not None and "https" in allow and "us\\.battle\\.net" in allow
          and "us\\.api\\.blizzard\\.com" in allow and "^" in allow,
          "config defines an anchored https host allowlist for both Blizzard origins")
    get_body = body_of(HTTP, r"\n    def get\(self, url\):(.*?)\n    def ")
    check(get_body != "" and "require_allowed_url(" in get_body,
          "Client.get enforces the host allowlist before any credentialed send")
    mint_body = body_of(HTTP, r"\n    def mint_token\(self, reason\):(.*?)\n    def ")
    check(mint_body != "" and "require_allowed_url(config.TOKEN_URL" in mint_body,
          "token mint (the other credentialed send path) is allowlist-checked too")
    check("urllib.request.urlopen(" not in PKG_TEXT,
          "no request bypasses the hardened opener (no bare urlopen left anywhere in the package)")
    check(re.search(r"class\s+_NoRedirectHandler\(urllib\.request\.HTTPRedirectHandler\)", HTTP) is not None,
          "http defines a redirect-suppressing HTTPRedirectHandler subclass")
    check(re.search(r"def redirect_request\([^)]*\):\s*\n\s+return None", HTTP) is not None,
          "redirect_request returns None, so a 3xx is reported instead of followed with credentials")
    check("NO_REDIRECT_OPENER.open(" in HTTP, "send() dispatches through the no-redirect opener")

    # --- H2: response-derived URLs are validated before they are persisted ----------- #
    check('startswith("http")' not in SYNC_TEXT,
          "canonical_href no longer accepts any string that starts with http")
    href_guard = body_of(FETCH, r"\ndef canonical_href\(payload\):(.*?)\ndef ")
    check(href_guard != "" and "PROFILE_HREF_RE.match(" in href_guard,
          "canonical_href validates against the profile URL shape")
    check('safe_artifact_url(href, "href")' in ARTIFACTS,
          "shared validator gates every href write site (map entry builder)")
    check('safe_artifact_url(icon, "iconUrl")' in FETCH,
          "shared validator gates the iconUrl write site (render host only)")
    check(re.search(r"def redact_foreign_urls\(text\):", HTTP) is not None
          and re.search(r"def redact\(value\):\s*\n\s*if isinstance\(value, str\):\s*\n\s*return redact_foreign_urls\(", HTTP)
          is not None,
          "redact() withholds non-Blizzard URLs from notes, logs and artifacts alike")

    # --- H3: derived credential forms are registered with the redactor --------------- #
    check(re.search(r"class Redactor:.*?\n    def register\(self, value\):", HTTP, flags=re.S) is not None,
          "Redactor exposes a public register() for derived credential forms")
    forms_body = body_of(HTTP, r"\ndef register_mint_forms\(client_id, client_secret\):(.*?)\ndef ")
    check(forms_body != "" and "REDACTOR.register(basic)" in forms_body,
          "the base64 client_id:client_secret pair is registered with the redactor")
    check(forms_body != "" and "quote_plus" in forms_body, "URL-encoded credential forms are registered too")
    check("register_mint_forms(" in mint_body, "sync token mint registers its derived forms")
    probe_mint = body_of(HTTP, r"class ProbeHttp:.*?\n    def mint_token\(self, reason\):(.*?)\n    def ")
    check("register_mint_forms(" in probe_mint, "probe token mint registers its derived forms too")

    # --- H4: mass-flip floor blocks the write ---------------------------------------- #
    floor = re.search(r"ok_prev\s*>=\s*3.*?ok_now\s*\*\s*2\s*<\s*ok_prev", ARTIFACTS, flags=re.S)
    check(floor is not None, "mass-flip floor (ok_prev >= 3 and ok_now * 2 < ok_prev) is present in the writer")
    write_pair = body_of(ARTIFACTS, r"\ndef write_pair\((.*?)\n(?=\ndef |\Z)")
    check(write_pair != "" and "mass_flip" in write_pair
          and write_pair.index("mass_flip") < write_pair.index("write_map("),
          "the floor is evaluated before the artifacts are written (inside the single writer)")
    # The orchestrator still pre-checks so the refusal prints with its run summary,
    # but it asks the artifacts layer for the verdict: the ok_prev/ok_now counting
    # existing twice is how a caller-side floor silently drifts from the real one.
    check("artifacts.mass_flip_refusal(" in SYNC and "ok_prev" not in SYNC,
          "the H4 criterion is computed in exactly one place (sync.py counts no ok rows itself)")
    check("amendment 8" in ARTIFACTS,
          "the floor documents the upstream-404 rationale it exists for (amendment 8)")

    # --- single writer of both artifacts --------------------------------------------- #
    writers = [n for n in PKG_MODULES if n != "artifacts.py"
               and ("config.MAP_FILE" in sources[n] or "config.DATA_FILE" in sources[n])]
    check(not writers, f"artifacts.py is the only module that writes the data artifacts (writers: {writers})")
    check("def write_json(path, payload):" in ARTIFACTS and "http.redact(payload)" in ARTIFACTS,
          "the artifact writer passes every value through redact() before it hits disk")

    # --- lifecycle: trusted slugs are never re-resolved ------------------------------- #
    check(re.search(r"def confirmed_slug\(rec\):", ROSTER_MOD) is not None
          and re.search(r"def is_trusted\(rec\):", ROSTER_MOD) is not None,
          "roster exposes the confirmedSlug / validated trust helpers")
    check("roster.is_trusted(rec)" in RESOLVE and "roster.trusted_slug(rec, prev)" in RESOLVE,
          "resolve consults the trust rule before any name resolution")
    check('note="user-confirmed"' in RESOLVE, "a trusted row keeps the confirmed slug with note user-confirmed")
    check(re.search(r"def roster_records\(document\):", ROSTER_MOD) is not None
          and '"characters"' in ROSTER_MOD,
          "roster reads both the v1 array and the v2 {schemaVersion, characters} envelope")

    # --- lifecycle: ambiguous verdicts exist and are candidate-bearing ---------------- #
    check('def ambiguous_entry(' in ARTIFACTS and '"ambiguous"' in ARTIFACTS,
          "artifacts define the ambiguous verdict (slug null, candidates populated)")
    check("artifacts.ambiguous_entry(" in RESOLVE, "resolve emits ambiguous verdicts instead of picking")
    check("def candidate_entry(" in ARTIFACTS and '"candidates": list(candidates or [])' in ARTIFACTS,
          "every map entry emits the v2 candidates list")

    # --- provenance -------------------------------------------------------------------- #
    check('def triggered_by(' in ARTIFACTS and 'GITHUB_EVENT_NAME' in ARTIFACTS and 'GITHUB_ACTOR' in ARTIFACTS,
          "triggeredBy resolves schedule / GITHUB_ACTOR / manual from the runner environment")
    check("triggered_by_value=artifacts.triggered_by()" in SYNC,
          "the sync run records provenance on the sidecar it writes")

    # --- workflow: verify gate runs between sync and commit -------------------------- #
    if wf:
        steps = workflow_steps(wf)
        joined = [f"{name}\n{body}" for name, body in steps]
        test_at = next((i for i, t in enumerate(joined) if "unittest" in t or "test_bnet_phase2" in t), None)
        sync_at = next((i for i, t in enumerate(joined) if "-m bnet sync" in t), None)
        verify_at = next((i for i, t in enumerate(joined) if "verify_sync_artifacts.py" in t), None)
        commit_at = next((i for i, t in enumerate(joined) if "git commit" in t), None)
        check(test_at is not None, "workflow runs the offline unit tests as a step")
        check(sync_at is not None, "workflow has a step that runs python3 -m bnet sync")
        check(verify_at is not None, "workflow runs .pi/scripts/verify_sync_artifacts.py as a step")
        check(commit_at is not None, "workflow has a commit step")
        check(test_at is not None and sync_at is not None and verify_at is not None and commit_at is not None
              and test_at < sync_at < verify_at < commit_at,
              f"step order is test -> sync -> verify -> commit (got test={test_at} sync={sync_at} "
              f"verify={verify_at} commit={commit_at})")
        if test_at is not None:
            # The unit tests are the one step that must never need a credential:
            # they exercise the redactor/allowlist offline, before the sync step is
            # ever handed the Blizzard secrets.
            check("secrets." not in joined[test_at] and "if:" not in joined[test_at]
                  and "continue-on-error" not in joined[test_at],
                  "the unit-test step is unconditional, skippable-by-nothing and secret-free")
        timeout = re.search(r"^(\s*)timeout-minutes:(\s*)(\d+)\s*$", wf, flags=re.M)
        check(timeout is not None and int(timeout.group(3)) == 15 and len(timeout.group(1)) == 4,
              f"the sync job declares job-level timeout-minutes: 15 so a hung run cannot hold "
              f"the concurrency group forever (found: {timeout.group(0).strip() if timeout else 'none'})")
        if verify_at is not None:
            check("if:" not in joined[verify_at] and "continue-on-error" not in joined[verify_at],
                  "the verify gate cannot be skipped or tolerated (no if:/continue-on-error)")

    print(f"\n{passes} passed, {len(fails)} failed")
    if fails:
        print("FAILED CHECKS:")
        for item in fails:
            print(f"  - {item}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
