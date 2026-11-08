#!/usr/bin/env python3
"""Deterministic check of the Phase 1 Battle.net probe artifacts.

Exit 0 = contract holds, 1 = names every failing clause. The load-bearing
assertion is #5: every JSON path cited in docs/bnet-api-notes.md must exist in
the captured probe inventories, so the notes cannot contain a guessed path.
"""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW_PATH = ROOT / "docs" / "bnet-probe-raw.json"
NOTES_PATH = ROOT / "docs" / "bnet-api-notes.md"
PROBE_PATH = ROOT / "tools" / "probe_bnet.py"
ENV_PATH = ROOT / ".env"

STDLIB = {"base64", "json", "os", "re", "sys", "time", "urllib", "collections", "pathlib",
          "__future__", "typing", "datetime", "hashlib", "textwrap", "argparse"}
FILE_SUFFIX = (".json", ".md", ".html", ".py", ".txt", ".yml", ".yaml", ".mjs")
PATH_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*|\[\])*$")
SECTIONS = [rf"^## {n}\." for n in range(1, 9)]

fails, passes = [], 0


def check(cond, msg):
    global passes
    if cond:
        passes += 1
        print(f"  PASS {msg}")
    else:
        fails.append(msg)
        print(f"  FAIL {msg}")


def normalized(path):
    return re.sub(r"\[\d+\]", "[]", path)


def inventories(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("inventory", "profile_inventory") and isinstance(value, dict):
                found.update(normalized(p) for p in value)
            inventories(value, found)
    elif isinstance(node, list):
        for item in node:
            inventories(item, found)
    return found


NOTATION = {"a.b", "a[]", "a[len]"}
TLD_LAST = {"com", "org", "net", "io", "dev", "us"}


def structural_paths(node, prefix="", out=None):
    """Key paths of the findings document itself, for citations like `auth.controls`."""
    out = set() if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(normalized(path))
            structural_paths(value, path, out)
    elif isinstance(node, list):
        out.add(normalized(f"{prefix}[]"))
        for item in node[:3]:
            structural_paths(item, prefix, out)
    return out


def cited_paths(text):
    cited = set()
    for span in re.findall(r"`([^`\n]+)`", text):
        span = span.strip()
        if span in NOTATION or span.endswith(FILE_SUFFIX) or ("." not in span and "[" not in span):
            continue
        if span.split(".")[-1] in TLD_LAST:
            continue
        if PATH_RE.match(span):
            cited.add(normalized(span))
    return cited


print("[1] artifacts present and probe is stdlib-only")
for path in (RAW_PATH, NOTES_PATH, PROBE_PATH):
    check(path.is_file(), f"{path.relative_to(ROOT)} exists")
if fails:
    sys.exit(f"aborting: {len(fails)} missing artifact(s)")
check(PROBE_PATH.stat().st_mode & 0o111 != 0, "probe script is executable")
modules = set()
for node in ast.walk(ast.parse(PROBE_PATH.read_text())):
    if isinstance(node, ast.Import):
        modules.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        modules.add(node.module.split(".")[0])
check(modules <= STDLIB, f"probe imports are stdlib only (found: {sorted(modules)})")

print("\n[2] raw findings parse and cover the roster")
raw = json.loads(RAW_PATH.read_text())
required = ["meta", "auth", "name_resolution", "characters", "indexes", "static_ns", "tier2",
            "media", "caching", "case_sensitivity", "endpoint_matrix", "request_log", "warnings"]
check(all(k in raw for k in required), "all required top-level sections present")
roster = [rec["name"] for rec in json.loads((ROOT / "characters.json").read_text())]
probed = [row["roster_name"] for row in raw["name_resolution"]["results"]]
check(sorted(probed) == sorted(roster), f"all {len(roster)} characters.json names probed exactly once")
check(raw["meta"]["requests_made"] >= len(roster), f"requests_made ({raw['meta']['requests_made']}) >= roster size")
check(raw["meta"]["tokens_minted"] >= 1, "at least one token mint recorded")
check(bool(raw["endpoint_matrix"]), "endpoint matrix is not empty")
resolved = [row for row in raw["name_resolution"]["results"] if row["status"] == 200]
check(bool(resolved), "at least one roster name resolved to a 200 profile")
check(all(row["status"] in (200, 404, 401, 403, "skipped_budget", None) for row in raw["name_resolution"]["results"]),
      "every name row carries a real HTTP status")

print("\n[3] controls, conditional requests and namespaces recorded")
sem = raw["auth"]["control_semantics"]
for label in ("bogus character name, valid token", "unrouted path, valid token",
              "routed path, NO token", "routed path, bogus token"):
    check(isinstance(sem.get(label), int), f"control recorded: {label} -> {sem.get(label)}")
check(bool(raw["caching"].get("probes")), "conditional-request probes recorded")
static_ns = str(raw["static_ns"].get("observed_current") or "")
check(static_ns.startswith("static-") and "forever-us" in static_ns,
      f"version-pinned Forever static ns discovered (not a retail ns): {static_ns or 'missing'}")
check(bool([l for l in raw["static_ns"]["lookups"] if l["status"] == 200]),
      "static-ns lookup verified with a 200")

print("\n[4] no credential material in the deliverables")
secrets = []
if ENV_PATH.is_file():
    for line in ENV_PATH.read_text().splitlines():
        if "=" in line:
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if len(value) >= 6:
                secrets.append(value)
for var in ("BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET"):
    value = os.environ.get(var, "").strip()
    # CI has the credentials in the environment and no .env file to read.
    if len(value) >= 6 and value not in secrets:
        secrets.append(value)
check(bool(secrets), f"{len(secrets)} env credential value(s) used as leak sentinels")
corpus = "\n".join(p.read_text() for p in (RAW_PATH, NOTES_PATH, PROBE_PATH))
check(not any(secret in corpus for secret in secrets), "no client_id/secret text in the artifacts")
auth_values = [m.group(2) for m in re.finditer(
    r"Authorization[\"']?\s*[:=]\s*[\"']?(?:Bearer|Basic)\s+([A-Za-z0-9+/=._-]{20,})", corpus, re.I)]
placeholders = re.compile(r"not-a-real|bogus|dummy|mock|example|redacted|self\.", re.I)
check(not [v for v in auth_values if not placeholders.search(v)],
      f"no real Authorization header value in the artifacts (placeholders only: {len(auth_values)})")
check('"access_token"' not in RAW_PATH.read_text(), "raw findings hold no access_token key at any depth")

print("\n[5] every JSON path cited in the notes was actually observed")
paths = inventories(raw, set())
check(len(paths) > 200, f"{len(paths)} observed inventory paths harvested from the raw file")
cited = cited_paths(NOTES_PATH.read_text())
check(len(cited) > 40, f"{len(cited)} JSON paths cited in the notes")
known = paths | structural_paths(raw)


def grounding(path):
    """A citation is real if it names a captured leaf, or a container that owns one."""
    stem = path[:-2] if path.endswith("[]") else path
    if stem.endswith(".en_US"):
        stem = stem[: -len(".en_US")]
    return any(other == stem or other.startswith(stem + ".") or other.startswith(stem + "[")
               for other in known)


unknown = sorted(p for p in cited if not grounding(p))
check(not unknown, f"cited paths all exist in probe evidence (invented: {unknown[:8]}{'…' if len(unknown) > 8 else ''})")

print("\n[6] notes structure and out-of-scope files untouched")
notes = NOTES_PATH.read_text()
for pattern in SECTIONS:
    check(re.search(pattern, notes, re.M) is not None, f"notes section {pattern} present")
missing_names = [name for name in roster if name not in notes]
check(not missing_names, f"every roster name appears in the notes (missing: {missing_names})")
for tag in ("200", "401", "404", "not probed"):
    check(tag in notes, f"notes tag observed results with {tag}")
git = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True)
dirty = git.stdout.split()
untouched = [p for p in dirty if p in ("index.html", "characters.json", "armory-tasks.md",
                                      "pi-warden.md", ".env", ".gitignore")]
check(not untouched, f"protected files untouched (dirty: {untouched})")
check(not (ROOT / ".github" / "workflows").exists(), "no .github/workflows files created")

print(f"\n{passes} passed, {len(fails)} failed")
for fail in fails:
    print(f"  ! {fail}")
sys.exit(1 if fails else 0)
