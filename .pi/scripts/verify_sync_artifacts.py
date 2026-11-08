#!/usr/bin/env python3
"""Deterministic checks for the Phase 2 sync artifacts.

Exit 0 = every clause holds; exit 1 = each failing clause was printed. The
load-bearing assertions: the map covers exactly the CURRENT roster ids, ok
rows carry real Battle.net identity, non-ok rows carry human notes, the
sidecar contains only whitelisted normalized keys (no raw Blizzard blobs),
no credential material appears in any committed surface, the workflow has no
PR/push trigger and an exact permissions block, and the sync script reuses
Last-Modified conditional GETs (amendment 3: If-Modified-Since yes, the
If-None-Match flavour of task 2.4 is dead — no response ever carried an ETag).
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
ROSTER = ROOT / "characters.json"
MAP = ROOT / "battle-net-map.json"
DATA = ROOT / "character-data.json"
SYNC = ROOT / "tools" / "sync_bnet.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sync-bnet.yml"
ENV = ROOT / ".env"

STATUS_VOCAB = {"ok", "not_found", "needs_review", "fetch_error"}
MAP_KEYS = {"rosterName", "status", "slug", "href", "bnetName", "surname",
            "matchedForm", "mappedVia", "note"}
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


def main():
    # --- roster baseline ---------------------------------------------------- #
    roster = json.loads(read(ROSTER))
    roster_ids = [rec["id"] for rec in roster]
    by_id = {rec["id"]: rec for rec in roster}
    collision_ids = {rid for norm, ids
                     in {n: [r["id"] for r in roster if norm_slug(r["name"]) == n]
                         for n in {norm_slug(r["name"]) for r in roster}}.items()
                     if len(ids) > 1 for rid in ids}

    # --- both artifacts parse ----------------------------------------------- #
    map_text, data_text = read(MAP), read(DATA)
    check(map_text is not None, "battle-net-map.json exists")
    check(data_text is not None, "character-data.json exists")
    if fails:
        return
    try:
        map_doc = json.loads(map_text)
        data_doc = json.loads(data_text)
        parses = True
    except ValueError as exc:
        parses = False
        check(False, f"artifacts parse as JSON: {exc}")
    if not parses:
        return

    # --- map schema ----------------------------------------------------------- #
    check(map_doc.get("schemaVersion") == 1, "map schemaVersion == 1")
    check(isinstance(map_doc.get("updatedAt"), str) and ISO_RE.match(map_doc["updatedAt"]),
          "map updatedAt is ISO8601 UTC")
    entries = map_doc.get("entries")
    check(isinstance(entries, dict), "map entries is an object")
    if not isinstance(entries, dict):
        return
    check(set(entries) == set(roster_ids),
          f"map covers exactly the current roster ids (map={len(entries)} roster={len(roster_ids)}; "
          f"missing={sorted(set(roster_ids) - set(entries))[:3]} stale={sorted(set(entries) - set(roster_ids))[:3]})")
    for rid, entry in entries.items():
        if not isinstance(entry, dict):
            check(False, f"map entry {rid} is an object")
            continue
        check(set(entry) <= MAP_KEYS, f"map entry {rid} has no unknown keys")
        status = entry.get("status")
        check(status in STATUS_VOCAB, f"map entry {rid} status '{status}' in vocabulary")
        if rid in by_id:
            check(entry.get("rosterName") == by_id[rid]["name"], f"map entry {rid} rosterName matches characters.json")
        if status == "ok":
            check(isinstance(entry.get("slug"), str) and bool(entry["slug"]), f"ok entry {rid} has a slug")
            check(isinstance(entry.get("href"), str)
                  and PROFILE_HREF_RE.match(entry["href"]) is not None,
                  f"ok entry {rid} href matches the frozen profile URL shape (H2)")
            # Defence in depth: even a non-ok row must never publish an off-host href.
            check(entry.get("href") is None or PROFILE_HREF_RE.match(entry["href"]) is not None,
                  f"entry {rid} href is null or a Blizzard profile URL (no persisted foreign href)")
            check(entry.get("mappedVia") in ("direct", "guild_roster"), f"ok entry {rid} mappedVia is valid")
            check(isinstance(entry.get("bnetName"), str) and bool(entry["bnetName"]),
                  f"ok entry {rid} carries the payload name")
        else:
            check(isinstance(entry.get("note"), str) and bool(entry["note"].strip()),
                  f"non-ok entry {rid} carries a human-readable note")
        if rid in collision_ids:
            check(status != "ok", f"duplicate-normalization id {rid} is not auto-mapped (policy)")

    # --- character-data schema ------------------------------------------------- #
    check(data_doc.get("schemaVersion") == 1, "data schemaVersion == 1")
    check(isinstance(data_doc.get("syncedAt"), str) and ISO_RE.match(data_doc["syncedAt"]),
          "data syncedAt is ISO8601 UTC")
    check(set(data_doc) == {"schemaVersion", "syncedAt", "characters"},
          "data top-level keys are exactly schemaVersion/syncedAt/characters")
    characters = data_doc.get("characters")
    check(isinstance(characters, dict) and set(characters) == set(roster_ids),
          "data covers exactly the current roster ids")
    if not isinstance(characters, dict):
        return
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
    for label, text in (("battle-net-map.json", map_text), ("character-data.json", data_text)):
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
    scanned = {"tools/sync_bnet.py": read(SYNC), "workflow": read(WORKFLOW),
               "battle-net-map.json": map_text, "character-data.json": data_text}
    for label, text in scanned.items():
        check(text is not None, f"{label} exists for the secret scan")
        if text is None:
            continue
        for secret in secrets:
            check(secret not in text, f"{label} contains no .env secret value")
        # The sync script legitimately names the Authorization header; the
        # committed workflow and artifacts must not mention it at all.
        if label != "tools/sync_bnet.py":
            check("Authorization" not in text, f"{label} contains no Authorization literal")
        check(not TOKEN_LITERAL_RE.search(text), f"{label} contains no hardcoded Basic/Bearer token value")

    # --- workflow shape ------------------------------------------------------------ #
    wf = scanned["workflow"]
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

    # --- untouched machine-owned files ---------------------------------------------- #
    diff = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", "characters.json", "index.html"])
    check(diff.returncode == 0, "characters.json and index.html are unmodified vs git HEAD")

    # --- caching mechanism (amendment 3) --------------------------------------------- #
    sync_text = scanned["tools/sync_bnet.py"]
    if sync_text:
        check("If-Modified-Since" in sync_text, "sync script sends If-Modified-Since")
        check("If-None-Match" not in sync_text,
              "sync script never sends If-None-Match (no response has ever carried an ETag)")

    # --- H1: credentialed requests are host-pinned and never follow redirects -------- #
    if sync_text:
        origin = re.search(r"ALLOWED_API_ORIGIN_RE\s*=\s*re\.compile\((.*?)\)\n", sync_text, flags=re.S)
        allow = origin.group(1) if origin else ""
        check(origin is not None and "https" in allow and "us\\.battle\\.net" in allow
              and "us\\.api\\.blizzard\\.com" in allow and "^" in allow,
              "sync script defines an anchored https host allowlist for both Blizzard origins")
        get_body = re.search(r"\n    def get\(self, url\):(.*?)\n    def ", sync_text, flags=re.S)
        check(get_body is not None and "require_allowed_url(" in get_body.group(1),
              "Client.get enforces the host allowlist before any credentialed send")
        mint_body = re.search(r"\n    def mint_token\(self, reason\):(.*?)\n    def ", sync_text, flags=re.S)
        check(mint_body is not None and "require_allowed_url(TOKEN_URL" in mint_body.group(1),
              "token mint (the other credentialed send path) is allowlist-checked too")
        check("urllib.request.urlopen(" not in sync_text,
              "no request bypasses the hardened opener (no bare urlopen left)")
        check(re.search(r"class\s+_NoRedirectHandler\(urllib\.request\.HTTPRedirectHandler\)", sync_text)
              is not None, "sync script defines a redirect-suppressing HTTPRedirectHandler subclass")
        check(re.search(r"def redirect_request\([^)]*\):\s*\n\s+return None", sync_text) is not None,
              "redirect_request returns None, so a 3xx is reported instead of followed with credentials")
        check("NO_REDIRECT_OPENER.open(" in sync_text,
              "_send dispatches through the no-redirect opener")

    # --- H2: response-derived URLs are validated before they are persisted ----------- #
    if sync_text:
        check('startswith("http")' not in sync_text,
              "canonical_href no longer accepts any string that starts with http")
        href_guard = re.search(r"def canonical_href\(payload\):(.*?)\ndef ", sync_text, flags=re.S)
        check(href_guard is not None and "PROFILE_HREF_RE.match(" in href_guard.group(1),
              "canonical_href validates against the profile URL shape")
        check('safe_artifact_url(href, "href")' in sync_text,
              "shared validator gates every href write site")
        check('safe_artifact_url(icon, "iconUrl")' in sync_text,
              "shared validator gates the iconUrl write site (render host only)")
        check(re.search(r"def redact_foreign_urls\(text\):", sync_text) is not None
              and re.search(r"def redact\(value\):\s*\n\s*if isinstance\(value, str\):\s*\n\s*return redact_foreign_urls\(", sync_text)
              is not None,
              "redact() withholds non-Blizzard URLs from notes, logs and artifacts alike")

    # --- H3: derived credential forms are registered with the redactor --------------- #
    if sync_text:
        check(re.search(r"class Redactor:.*?\n    def register\(self, value\):", sync_text, flags=re.S)
              is not None, "Redactor exposes a public register() for derived credential forms")
        check(mint_body is not None and "REDACTOR.register(basic)" in mint_body.group(1),
              "the base64 client_id:client_secret pair is registered with the redactor")
        check(mint_body is not None and "quote_plus" in mint_body.group(1),
              "URL-encoded credential forms are registered too")

    # --- H4: mass-flip floor blocks the write ---------------------------------------- #
    if sync_text:
        floor = re.search(r"ok_prev\s*>=\s*3.*?ok_now\s*\*\s*2\s*<\s*ok_prev", sync_text, flags=re.S)
        check(floor is not None, "mass-flip floor (ok_prev >= 3 and ok_now * 2 < ok_prev) is present")
        check(floor is not None and sync_text.index("if not self.args.dry_run:") > floor.start(),
              "the floor is evaluated before the artifacts are written")
        check("amendment 8" in sync_text,
              "the floor documents the upstream-404 rationale it exists for (amendment 8)")

    # --- workflow: verify gate runs between sync and commit -------------------------- #
    if wf:
        steps = workflow_steps(wf)
        names = [name for name, _body in steps]
        joined = [f"{name}\n{body}" for name, body in steps]
        sync_at = next((i for i, t in enumerate(joined) if "tools/sync_bnet.py" in t), None)
        verify_at = next((i for i, t in enumerate(joined) if "verify_sync_artifacts.py" in t), None)
        commit_at = next((i for i, t in enumerate(joined) if "git commit" in t), None)
        check(sync_at is not None, "workflow has a step that runs tools/sync_bnet.py")
        check(verify_at is not None, "workflow runs .pi/scripts/verify_sync_artifacts.py as a step")
        check(commit_at is not None, "workflow has a commit step")
        check(sync_at is not None and verify_at is not None and commit_at is not None
              and sync_at < verify_at < commit_at,
              f"step order is sync -> verify -> commit (got sync={sync_at} verify={verify_at} commit={commit_at})")
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
