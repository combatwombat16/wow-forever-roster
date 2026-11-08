"""Name → Battle.net identity: candidate forms, verdicts, trusted slugs.

Provenance: the frozen §2/§7.1 name and URL helpers (``norm_slug``,
``lookup_candidates``, ``character_url``, ``slug_from_href``,
``member_norm_key``) and the two resolution passes (direct GET ladder, guild
lever) come from ``tools/sync_bnet.py``.

Two behaviour changes land here with Phase 2 (docs/refactor-design.md §3):

* **No auto-pick.** Any pass that finds more than one distinct profile emits
  ``status: "ambiguous"`` with every plausible profile in ``candidates``; the
  old class-based tie-break that silently chose one is gone. Exactly one hit
  keeps the unchanged ``ok`` semantics.
* **Trusted slugs.** A roster row whose ``validation.state == "validated"`` or
  that carries ``validation.confirmedSlug`` is never re-resolved: the recorded
  slug is fetched directly and the map keeps it with ``note: "user-confirmed"``.
"""

import re
import urllib.parse

from . import artifacts, config, fetch, http, roster


# --------------------------------------------------------------------------- #
# Name / URL helpers (the frozen §2/§7.1 contract)
# --------------------------------------------------------------------------- #
def norm_slug(name):
    """Amendment 1 canonical form: lowercase, spaces to hyphens."""
    return "-".join(str(name).lower().split())


def lookup_candidates(name):
    """Ordered direct-GET forms for one roster name.

    Routes are lowercase-only and a capital letter 404s exactly like a missing
    character (notes §2), so the lowercase hyphenated form comes first and the
    legacy spellings are only fallbacks; the matched form is recorded per id.

    Phase 2 walks the whole list instead of stopping at the first 200, because
    "exactly one distinct profile" is now the precondition for an ``ok`` verdict
    — stopping early is precisely the auto-pick that was removed.
    """
    out, seen = [], set()

    def add(form, value):
        if value and value not in seen:
            seen.add(value)
            out.append({"form": form, "value": value})

    if " " in str(name):
        tokens = str(name).split()
        add("lower_hyphen", "-".join(tokens).lower())
        add("hyphen", "-".join(tokens))
        add("lower_removed", "".join(tokens).lower())
        add("space_encoded", str(name))
    else:
        add("lower", str(name).lower())
        add("as_is", str(name))
    return out


def character_url(name_or_slug):
    return (f"{config.API_BASE}/profile/wow/character/"
            f"{urllib.parse.quote(str(name_or_slug), safe='')}?namespace={config.NS_PROFILE}")


def slug_from_href(href):
    match = re.search(r"/profile/wow/character/([^/?]+)", href or "")
    return urllib.parse.unquote(match.group(1)) if match else None


def member_norm_key(member):
    """Normalized identity of a guild-roster member (notes §7.2 field list)."""
    if not isinstance(member, dict):
        return None
    ch = member.get("character")
    if not isinstance(ch, dict):
        return None
    name = ch.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    surname = ch.get("surname")
    if isinstance(surname, str) and surname.strip():
        return f"{name.strip().lower()}-{surname.strip().lower()}"
    return name.strip().lower()


def member_slug(member):
    ch = member.get("character") if isinstance(member.get("character"), dict) else {}
    slug = ch.get("slug")
    return slug if isinstance(slug, str) and slug.strip() else None


# --------------------------------------------------------------------------- #
# Resolution passes
# --------------------------------------------------------------------------- #
class Resolver:
    """Owns the two mapping passes. Shares one ``Fetcher`` with the enrich pass so
    the cache, the drift tripwires and the request accounting stay in one place."""

    def __init__(self, f):
        self.f = f

    # -- trusted (user-confirmed) ------------------------------------------- #
    def resolve_trusted(self, rec, w, prev, slug):
        """Fetch data for a slug a human already confirmed; never re-resolve it.

        The row is mapped by identity, not by inference, so the map keeps the
        confirmed slug and carries ``note: "user-confirmed"`` (design §3). A 404
        on a confirmed slug is reported, never repaired by re-resolving the name:
        a renamed or deleted character is a human finding, not a sync decision.
        """
        url = character_url(slug)
        resp = self.f.client.get(url)
        if resp.status in (200, 304):
            payload = resp.json
            if payload is not None:
                href = fetch.canonical_href(payload) or url
                self.f.note_revision("profile", resp.headers)
                self.f.capture_static_ns(payload)
                w["profile"] = payload
                w["profile_url"] = url
                w["unchanged"] = resp.status == 304
                w["entry"] = artifacts.ok_entry(
                    rec, slug=slug_from_href(href) or slug_from_href(url) or slug, href=href,
                    payload=payload, matched_form=(prev or {}).get("matchedForm") or "confirmed_slug",
                    mapped_via="confirmed", note="user-confirmed")
                # Nothing else on this name may be looked up, and the previous
                # spellings stay untried: that is the point of the trust rule.
                self.f.saved_requests += max(0, len(lookup_candidates(rec["name"])) - 1)
                return
            w["entry"] = artifacts.map_entry(rec, "fetch_error", slug=slug, note="user-confirmed slug returned 200 with an unparseable body")
            return
        if resp.status == 404:
            w["entry"] = artifacts.map_entry(rec, "not_found", slug=slug,
                                             note=f"user-confirmed slug {slug} returned 404; not re-resolving")
            return
        if resp.status is None:
            w["entry"] = artifacts.map_entry(rec, "fetch_error", slug=slug,
                                             note="user-confirmed slug: transport error (no HTTP status)")
            return
        w["entry"] = artifacts.map_entry(rec, "fetch_error", slug=slug,
                                         note=f"user-confirmed slug: HTTP {resp.status}")

    # -- tier 1: direct GET ladder ------------------------------------------- #
    def resolve(self, rec, w, prev):
        """Map one roster row: trusted slug, then the revalidation fast path, then
        the full candidate walk. Leaves ``w["entry"]`` None for the guild lever."""
        if roster.is_trusted(rec):
            slug = roster.trusted_slug(rec, prev)
            if slug:
                return self.resolve_trusted(rec, w, prev, slug)
            # A validated row that carries neither a confirmedSlug nor a mapped
            # slug in the previous map has nothing trustworthy to fetch. Guessing
            # here would be the exact behaviour the trust rule forbids.
            w["entry"] = artifacts.map_entry(
                rec, "needs_review",
                note="validated without confirmedSlug and no previously mapped slug; refusing to re-resolve")
            return

        if (isinstance(prev, dict) and prev.get("status") == "ok"
                and isinstance(prev.get("href"), str) and http.allowed_api_url(prev["href"])):
            # H2: a href persisted by an older, more trusting run is still response
            # data. It is re-fetched only while it points at a Blizzard origin; a
            # poisoned value drops to the constructed-URL ladder below instead.
            prev_slug = slug_from_href(prev["href"])
            if prev_slug and prev_slug == norm_slug(rec["name"]):
                resp = self.f.client.get(prev["href"])
                if resp.status in (200, 304) and resp.json is not None:
                    payload = resp.json
                    self.f.note_revision("profile", resp.headers)
                    self.f.capture_static_ns(payload)
                    w["profile"] = payload
                    w["profile_url"] = prev["href"]
                    w["unchanged"] = resp.status == 304
                    w["entry"] = artifacts.ok_entry(rec, slug=prev_slug, href=prev["href"], payload=payload,
                                                    matched_form=prev.get("matchedForm") or "lower_hyphen",
                                                    mapped_via=prev.get("mappedVia") or "direct")
                    # An already-mapped id needs one revalidating GET instead of
                    # the whole candidate ladder — that is most of run 2's saving.
                    saved = max(0, len(lookup_candidates(rec["name"])) - 1)
                    self.f.saved_requests += saved
                    self.f.skipped["lookups"] += saved
                    return
                self.f.warnings.append(f"id {rec['id']} was ok last run but got HTTP {resp.status}; re-resolving")

        # Full candidate walk. Every form is tried even after a hit so a second,
        # different profile under a different spelling cannot be silently ignored.
        hits = []           # one entry per distinct slug, first form wins
        seen_slugs = set()
        transport_note = None
        for cand in lookup_candidates(rec["name"]):
            url = character_url(cand["value"])
            resp = self.f.client.get(url)
            if resp.status == 304 and resp.json is None:
                # Revalidation hit but the cached body was pruned: probe on.
                continue
            if resp.status in (200, 304):
                # A 304 IS a hit: the document exists and the cached body is current.
                payload = resp.json
                if payload is None:
                    w["entry"] = artifacts.map_entry(rec, "fetch_error",
                                                     note=f"200 with unparseable body on form {cand['form']}")
                    return
                href = fetch.canonical_href(payload) or url
                self.f.note_revision("profile", resp.headers)
                self.f.capture_static_ns(payload)
                slug = slug_from_href(href) or slug_from_href(url) or norm_slug(rec["name"])
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                hits.append({"slug": slug, "href": href, "payload": payload, "form": cand["form"]})
                continue
            if resp.status is None:
                transport_note = "transport error (no HTTP status) during lookup"
                if not hits:
                    w["entry"] = artifacts.map_entry(rec, "fetch_error", note=transport_note)
                    return
                self.f.warnings.append(f"id {rec['id']}: {transport_note} after a hit; keeping the resolved profile")
                break
            if resp.status != 404:
                if not hits:
                    w["entry"] = artifacts.map_entry(rec, "fetch_error",
                                                     note=f"HTTP {resp.status} on lookup form {cand['form']}")
                    return
                self.f.warnings.append(f"id {rec['id']}: HTTP {resp.status} on form {cand['form']} after a hit; "
                                       "keeping the resolved profile")
                break
            # 404: routed-and-not-found or unrouted; the ladder tries the next form.

        if not hits:
            return                      # guild lever, then not_found
        if len(hits) == 1:
            hit = hits[0]
            w["profile"] = hit["payload"]
            w["profile_url"] = hit["href"]
            w["entry"] = artifacts.ok_entry(rec, slug=hit["slug"], href=hit["href"], payload=hit["payload"],
                                            matched_form=hit["form"], mapped_via="direct")
            return
        # More than one distinct profile answered: never pick one (design §3).
        w["entry"] = artifacts.ambiguous_entry(
            rec,
            candidates=[artifacts.candidate_entry(slug=h["slug"],
                                                  name=h["payload"].get("name") if isinstance(h["payload"], dict) else None,
                                                  surname=h["payload"].get("surname") if isinstance(h["payload"], dict) else None,
                                                  matched_form=h["form"], href=h["href"]) for h in hits],
            note=f"{len(hits)} distinct profiles matched the name forms {[h['form'] for h in hits]}; "
                 "never auto-pick between them",
            matched_via="direct")

    # -- tier 2: guild roster lever ------------------------------------------- #
    def guild_lever(self, work, ordered_ids):
        """Tier 2 (amendments 4): resolve remaining ids through guild rosters
        discovered from already-resolved characters. One-directional: guild
        members who are not in the tracker are ignored, never added."""
        unresolved = [work[rid] for rid in ordered_ids if work[rid]["entry"] is None]
        if not unresolved:
            return
        guild_slugs = sorted({
            w["profile"]["guild"]["slug"]
            for w in work.values()
            if isinstance(w.get("profile"), dict)
            and isinstance(w["profile"].get("guild"), dict)
            and isinstance(w["profile"]["guild"].get("slug"), str)
        })
        if not guild_slugs:
            return
        claims = {}
        for guild_slug in guild_slugs:
            members = self.f.guild_members(guild_slug)
            if members is None:
                self.f.guild_fetch_failures.add(guild_slug)
                continue
            for member in members:
                key = member_norm_key(member)
                if key:
                    claims.setdefault(key, []).append((guild_slug, member))
        by_norm = {}
        for w in unresolved:
            by_norm.setdefault(norm_slug(w["rec"]["name"]), []).append(w)
        for norm, ws in by_norm.items():
            hits = claims.get(norm, [])
            if not hits:
                continue
            if len(ws) != 1:
                for w in ws:
                    w["entry"] = artifacts.map_entry(w["rec"], "needs_review",
                                                     note=f"guild match for {norm} is ambiguous across "
                                                          f"roster ids {', '.join(x['rec']['id'] for x in ws)}")
                continue
            w = ws[0]
            distinct = {member_slug(m) for _, m in hits if member_slug(m)}
            if len(distinct) > 1:
                # Phase 2: the class tie-break that used to auto-pick here is gone.
                w["entry"] = artifacts.ambiguous_entry(
                    w["rec"],
                    candidates=self.guild_candidates(w["rec"], hits),
                    note=f"guild roster(s) {', '.join(sorted({g for g, _ in hits}))} are ambiguous for this "
                         f"name ({len(distinct)} members match); never auto-pick between them",
                    matched_via="guild_roster")
                self.f.warnings.append(f"id {w['rec']['id']}: {len(distinct)} guild-roster members match "
                                       f"{norm}; verdict ambiguous, nothing picked")
                continue
            chosen = hits[0][1]
            ch = chosen.get("character") or {}
            key_node = ch.get("key") if isinstance(ch.get("key"), dict) else None
            # H1/H2: a guild-roster member's key.href is untrusted response data.
            # Use it only when it names a Blizzard origin; otherwise build the §7.1
            # URL ourselves from the member slug, and let the choke point be the
            # backstop rather than the only line of defence.
            member_href = key_node.get("href") if isinstance(key_node, dict) \
                and isinstance(key_node.get("href"), str) and http.allowed_api_url(key_node["href"]) else None
            href = http.set_ns(member_href, config.NS_PROFILE) if member_href else character_url(ch.get("slug") or norm)
            try:
                resp = self.f.client.get(href)
            except http.UnsafeUrl as exc:
                # Per character finding, never an abort of the lever pass.
                w["entry"] = artifacts.map_entry(w["rec"], "fetch_error",
                                                 note=f"guild-roster profile link refused: {http.redact(str(exc))[:140]}")
                continue
            if resp.status in (200, 304) and resp.json is not None:
                payload = resp.json
                self.f.note_revision("profile", resp.headers)
                self.f.capture_static_ns(payload)
                w["profile"] = payload
                w["profile_url"] = href
                self.f.skipped["lookups"] += 1
                self.f.saved_requests += 1
                w["entry"] = artifacts.ok_entry(w["rec"], slug=ch.get("slug") or slug_from_href(fetch.canonical_href(payload) or href),
                                                href=fetch.canonical_href(payload) or href, payload=payload,
                                                matched_form="guild_roster", mapped_via="guild_roster")
            elif resp.status is None or resp.status not in (200, 404):
                w["entry"] = artifacts.map_entry(w["rec"], "fetch_error",
                                                 note=f"HTTP {resp.status} fetching the profile found in the guild roster")
            # a 404 here leaves it unresolved; it ends as not_found below.

    def guild_candidates(self, rec, hits):
        """Plausible profiles for a name several guild members share.

        Ordered by class match (roster class is known to be wrong sometimes —
        notes §8 — so it may order the list and nothing more), then by slug so the
        order is stable across runs and diffable. No profile GET is spent here:
        the guild payload already carries the identity fields, and the reviewer
        fetches the real one when they confirm it."""
        try:
            class_names = self.f.class_names()
        except (http.BudgetExhausted, http.UnsafeUrl):
            class_names = {}
        seen, out = set(), []
        for _, member in sorted(hits, key=lambda gm: (not self.f.member_class_matches(rec, gm[1]),
                                                      member_slug(gm[1]) or "")):
            slug = member_slug(member)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            ch = member.get("character") or {}
            out.append(artifacts.candidate_entry(
                slug=slug,
                name=ch.get("name") if isinstance(ch.get("name"), str) else None,
                surname=ch.get("surname") if isinstance(ch.get("surname"), str) else None,
                matched_form="guild_roster",
                href=character_url(slug)))
        return out

    # -- maintainer lookup mode ------------------------------------------------ #
    def lookup_name(self, name, seeds):
        """Resolve one free-form name for a maintainer, reporting every candidate.

        Returns ``{"status", "payload", "href", "matched", "via", "candidates"}``;
        the orchestrator decides whether the map may be updated (only an
        unambiguous hit may touch it) and prints the report. ``seeds`` is the list
        of already-mapped profile hrefs used to discover guild rosters — a guild
        slug is only discoverable from a resolved member (notes §7.2).
        """
        # Tier 1 first: the direct GET ladder, walking every form so a second
        # distinct profile cannot be hidden by stopping at the first 200.
        direct, fatal = self.lookup_direct(name)
        if fatal:
            return {"status": fatal, "payload": None, "href": None,
                    "matched": None, "via": None, "candidates": []}
        if len(direct) == 1:
            hit = direct[0]
            return {"status": f"resolved via direct GET (form {hit['form']}, HTTP {hit['status']})",
                    "payload": hit["payload"], "href": hit["href"],
                    "matched": hit["form"], "via": "direct", "candidates": []}
        if len(direct) > 1:
            return {"status": f"ambiguous: {len(direct)} distinct profiles matched the direct lookup forms "
                              f"{[d['form'] for d in direct]}",
                    "payload": None, "href": None, "matched": None, "via": "direct",
                    "candidates": [artifacts.candidate_entry(
                        slug=d["slug"],
                        name=d["payload"].get("name") if isinstance(d["payload"], dict) else None,
                        surname=d["payload"].get("surname") if isinstance(d["payload"], dict) else None,
                        matched_form=d["form"], href=d["href"]) for d in direct]}

        guild_slugs = set()
        for seed_href in seeds[:3]:
            resp = self.f.client.get(seed_href)
            seed_payload = resp.json
            if isinstance(seed_payload, dict):
                self.f.capture_static_ns(seed_payload)
                guild = seed_payload.get("guild")
                if isinstance(guild, dict) and isinstance(guild.get("slug"), str):
                    guild_slugs.add(guild["slug"])
        hits = []
        for guild_slug in sorted(guild_slugs):
            for member in self.f.guild_members(guild_slug) or []:
                if member_norm_key(member) == norm_slug(name):
                    hits.append((guild_slug, member))
        distinct = sorted({member_slug(m) for _, m in hits if member_slug(m)})
        if len(distinct) == 1:
            ch = hits[0][1].get("character") or {}
            key_node = ch.get("key") if isinstance(ch.get("key"), dict) else None
            # H1/H2 lookup mode: the roster-supplied key href is only trusted as
            # far as the allowlist; otherwise the URL is constructed here.
            member_href = key_node.get("href") if isinstance(key_node, dict) \
                and isinstance(key_node.get("href"), str) and http.allowed_api_url(key_node["href"]) else None
            guess = http.set_ns(member_href, config.NS_PROFILE) if member_href else character_url(ch.get("slug") or norm_slug(name))
            resp = self.f.client.get(guess)
            if resp.status in (200, 304) and resp.json is not None:
                payload = resp.json
                self.f.note_revision("profile", resp.headers)
                self.f.capture_static_ns(payload)
                return {"status": f"resolved via guild roster {hits[0][0]}",
                        "payload": payload,
                        "href": fetch.canonical_href(payload) or guess,
                        "matched": "guild_roster", "via": "guild_roster", "candidates": []}
            return {"status": f"guild roster {hits[0][0]} matched, but its profile GET returned HTTP {resp.status}",
                    "payload": None, "href": None, "matched": None, "via": None, "candidates": []}
        if len(distinct) > 1:
            # Never auto-pick: the maintainer gets the list, the map gets nothing.
            return {"status": f"ambiguous: {len(distinct)} guild-roster members match this normalized name",
                    "payload": None, "href": None, "matched": None, "via": "guild_roster",
                    "candidates": self.guild_candidates({"name": name}, hits)}
        if guild_slugs:
            return {"status": "no match: direct lookup 404ed and no reachable guild roster lists this name",
                    "payload": None, "href": None, "matched": None, "via": None, "candidates": []}
        return {"status": "no match, and no already-mapped character exposed a guild roster to search",
                "payload": None, "href": None, "matched": None, "via": None, "candidates": []}

    def lookup_direct(self, name):
        """Direct GET walk for one name. Returns ``(distinct_hits, stop_status)``.

        ``stop_status`` is non-None when the walk had to stop early (transport
        error or an unexpected status), exactly like the pre-refactor lookup.
        """
        hits, seen, stop = [], set(), None
        for cand in lookup_candidates(name):
            url = character_url(cand["value"])
            resp = self.f.client.get(url)
            # 304 carries the cached body: a revalidation is a hit, not a miss.
            if resp.status in (200, 304) and resp.json is not None:
                payload = resp.json
                href = fetch.canonical_href(payload) or url
                self.f.note_revision("profile", resp.headers)
                self.f.capture_static_ns(payload)
                slug = slug_from_href(href) or slug_from_href(url) or norm_slug(name)
                if slug not in seen:
                    seen.add(slug)
                    hits.append({"slug": slug, "href": href, "payload": payload,
                                 "form": cand["form"], "status": resp.status})
                continue
            if resp.status is None or resp.status not in (404, 200, 304):
                stop = f"HTTP {resp.status} on form {cand['form']}; stopped the candidate walk"
                break
        return hits, stop

    # -- post-pass ------------------------------------------------------------ #
    def dedupe_slugs(self, work, ordered_ids):
        """Two roster ids resolving to the same Battle.net slug must both become
        needs_review — collapsing them into one ok row is the failure mode the
        id-keyed map exists to prevent (notes §2)."""
        by_slug = {}
        for rid in ordered_ids:
            e = work[rid]["entry"]
            if e and e["status"] == "ok" and e.get("slug"):
                by_slug.setdefault(e["slug"], []).append(rid)
        for slug, ids in by_slug.items():
            if len(ids) < 2:
                continue
            for rid in ids:
                w = work[rid]
                others = ", ".join(i for i in ids if i != rid)
                w["entry"] = artifacts.map_entry(w["rec"], "needs_review",
                                                 note=f"slug {slug} also claimed by id {others}; "
                                                      "never auto-pick between them")
                # Identity is unsafe here: keep the sidecar row minimal instead of
                # publishing enriched data under an ambiguous mapping.
                w["profile"] = None
                self.f.warnings.append(f"slug collision {slug}: ids {', '.join(ids)} demoted to needs_review")
