"""Payload fetching and the H2 write-side validators.

Provenance: extracted verbatim from ``tools/sync_bnet.py`` (the ``Sync``
class's fetch plumbing: ``note_revision``, ``capture_static_ns``,
``sub_payload``, ``icon_for``, ``guild_members``, ``class_names``,
``member_class_matches`` and ``enrich``) plus the ``loc`` / ``compact`` /
``extract_icon`` payload helpers and the H2 URL validators ``safe_artifact_url``
and ``canonical_href``.

Two H2 boundaries matter here:

* ``safe_artifact_url`` is the single gate for any URL written into a committed
  artifact — a poisoned href or icon value is stored as ``null``, never as the
  attacker's string;
* ``canonical_href`` validates the profile self-link before it is used, so the
  old bare http-prefix acceptance (which persisted anything) is gone.

``Fetcher`` owns the run's HTTP plumbing: cache reuse, drift tripwires, guild
and class caches, icon de-duplication and the sidecar enrichment pass.
"""

import json
import urllib.parse
from collections import Counter

from . import config, http


# --------------------------------------------------------------------------- #
# H2 validators
# --------------------------------------------------------------------------- #
def safe_artifact_url(value, kind):
    """H2: every URL written into battle-net-map.json / character-data.json goes
    through this shared validator (kind "href" or "iconUrl").

    Returning None (never the raw string) is what keeps a poisoned response href
    out of a committed, publicly served artifact that the next nightly would
    happily re-fetch with a Bearer token attached."""
    pattern = config.ARTIFACT_URL_RES.get(kind)
    if pattern is None:
        raise ValueError(f"no URL validator registered for artifact field {kind!r}")
    return value if isinstance(value, str) and pattern.match(value) else None


def canonical_href(payload):
    """Forever nests the canonical self URL under _links.self rather than href (§3.1).

    H2: a candidate must match the frozen realmless profile URL shape — the old
    bare http-prefix test accepted any attacker string and persisted it, turning
    one poisoned payload into a permanent exfiltration beacon. None means "caller
    falls back to the URL we constructed from the roster name"."""
    if not isinstance(payload, dict):
        return None
    for candidate in (payload.get("href"),
                      payload.get("_links", {}).get("self", {}).get("href")
                      if isinstance(payload.get("_links"), dict) else None):
        if isinstance(candidate, str) and config.PROFILE_HREF_RE.match(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def loc(value):
    """Locale maps are inline with no indirection — read .en_US (notes §7.9)."""
    if isinstance(value, dict):
        if isinstance(value.get("en_US"), str):
            return value["en_US"]
        for v in value.values():
            if isinstance(v, str):
                return v
        return None
    return value if isinstance(value, str) else None


def extract_icon(media_payload):
    """assets[].key == "icon" -> assets[].value (notes §6); None when absent."""
    if not isinstance(media_payload, dict):
        return None
    for asset in media_payload.get("assets") or []:
        if isinstance(asset, dict) and asset.get("key") == "icon" and isinstance(asset.get("value"), str):
            return asset["value"]
    return None


class Fetcher:
    """Client + cache + caches + counters shared by the resolve and enrich passes."""

    def __init__(self, client, cache):
        self.client = client
        self.cache = cache
        self.rev_prev = dict(cache.schema_revisions)
        self.static_ns = None
        self._guild_cache = {}
        self._class_names = None
        self.guild_fetch_failures = set()
        self.icon_urls = {}          # item media URL -> icon URL (in-run dedup)
        self.icon_failures = 0
        self.drift = []
        self._drift_seen = set()
        self.warnings = []
        self.skipped = Counter()
        self.saved_requests = 0

    # -- tripwires / helpers ------------------------------------------------ #
    def note_revision(self, surface, headers):
        schema = headers.get("battlenet-schema") if headers else None
        revision = headers.get("battlenet-schema-revision") if headers else None
        if not schema:
            return
        pair = f"{schema}/{revision}"
        previous = self.rev_prev.get(surface)
        self.cache.schema_revisions[surface] = pair
        if previous and previous != pair and ("drift", surface, pair) not in self._drift_seen:
            self._drift_seen.add(("drift", surface, pair))
            self.drift.append(f"SCHEMA REVISION CHANGE on {surface}: was {previous}, now {pair} — "
                              "docs/bnet-api-notes.md field tables need re-probing")

    def capture_static_ns(self, payload):
        if self.static_ns:
            return
        match = config.STATIC_NS_RE.search(json.dumps(payload))
        if match:
            self.static_ns = match.group(1)

    def sub_payload(self, w, surface, url):
        """Sub-resource payload with the shared-timestamp reuse rule.

        notes §7.3: every document of one character carries the same
        Last-Modified value, so a 304 on the profile means the sub-resources
        cannot have changed either — their cached bodies are reused instead of
        burning a request each.
        """
        if w.get("unchanged"):
            body = self.cache.body(url)
            if body is not None:
                self.saved_requests += 1
                self.skipped["subresources"] += 1
                return body
        resp = self.client.get(url)
        if resp.status in (200, 304):
            self.note_revision(surface, resp.headers)
            return resp.json
        return None

    def icon_for(self, media_url, w):
        if media_url in self.icon_urls:
            return self.icon_urls[media_url]
        if w.get("unchanged"):
            body = self.cache.body(media_url)
            if body is not None:
                # H2: the cache can hold a value cached before this validator
                # existed, so the reuse path validates too.
                icon = safe_artifact_url(extract_icon(body), "iconUrl")
                self.icon_urls[media_url] = icon
                self.saved_requests += 1
                self.skipped["icons"] += 1
                return icon
        resp = self.client.get(media_url)
        icon = None
        if resp.status in (200, 304):
            self.note_revision("item_media", resp.headers)
            # H2: an asset value is response data; only the Blizzard render host is
            # publishable, anything else counts as a failed icon and stores null.
            icon = safe_artifact_url(extract_icon(resp.json), "iconUrl")
        if icon is None:
            # notes §8: a single reference-doc failure is explicitly non-fatal;
            # the gear row survives with iconUrl null.
            self.icon_failures += 1
        self.icon_urls[media_url] = icon
        return icon

    def guild_members(self, guild_slug):
        """One roster fetch per guild slug per run (cached 10800 s server-side)."""
        if guild_slug in self._guild_cache:
            return self._guild_cache[guild_slug]
        url = (f"{config.API_BASE}{config.GUILD_ROSTER_PATH.format(slug=urllib.parse.quote(guild_slug, safe=''))}"
               f"?namespace={config.NS_PROFILE}")
        resp = self.client.get(url)
        members = None
        if resp.status in (200, 304):
            self.note_revision("guild_roster", resp.headers)
            payload = resp.json
            if isinstance(payload, dict) and isinstance(payload.get("members"), list):
                members = payload["members"]
        self._guild_cache[guild_slug] = members
        return members

    def class_names(self):
        """id -> name from the static ns; dynamic class index is dead (amendment 5)."""
        if self._class_names is not None:
            return self._class_names
        self._class_names = {}
        if not self.static_ns:
            return self._class_names
        url = f"{config.API_BASE}{config.PLAYABLE_CLASS_INDEX}?namespace={self.static_ns}"
        resp = self.client.get(url)
        if resp.status in (200, 304):
            self.note_revision("playable_class_index", resp.headers)
            payload = resp.json or {}
            for cls in payload.get("classes") or []:
                if isinstance(cls, dict) and cls.get("id") is not None:
                    self._class_names[cls["id"]] = loc(cls.get("name"))
        return self._class_names

    def member_class_matches(self, rec, member):
        """Class is only ever a hint, never a tie-break (design §5 removes auto-pick).

        The roster class is known to be wrong sometimes (notes §8: morc theorc
        listed Shaman, API says Hunter), so class can never choose a mapping; it
        only orders ``candidates`` so the reviewer sees the plausible one first."""
        names = self.class_names()
        if not names:
            return False
        ch = member.get("character") if isinstance(member.get("character"), dict) else {}
        member_class = ch.get("playable_class")
        member_class_id = member_class.get("id") if isinstance(member_class, dict) else None
        roster_class = str(rec.get("class") or "").casefold()
        api_class = names.get(member_class_id)
        return bool(api_class) and api_class.casefold() == roster_class

    # -- enrichment ----------------------------------------------------------- #
    def enrich(self, w):
        """Build the character-data sidecar row for a resolved profile."""
        entry, profile = w["entry"], w["profile"]
        href = entry["href"]
        href_base = href.split("?")[0]

        def link(key, path):
            node = profile.get(key)
            base = node.get("href") if isinstance(node, dict) and isinstance(node.get("href"), str) \
                else f"{href_base}/{path}"
            return http.set_ns(base, config.NS_PROFILE)

        static_ns = self.static_ns
        if not static_ns:
            match = config.STATIC_NS_RE.search(json.dumps(profile))
            if match:
                self.static_ns = static_ns = match.group(1)

        out = {"status": "ok"}
        bnet = {}
        if isinstance(profile.get("name"), str):
            bnet["name"] = profile["name"]
        if isinstance(profile.get("surname"), str):
            bnet["surname"] = profile["surname"]
        # H2 write site: the sidecar is served to every visitor; no off-allowlist
        # href is ever published, whatever the payload claimed.
        bnet["href"] = safe_artifact_url(href, "href")
        out["bnet"] = bnet
        if isinstance(profile.get("level"), int):
            out["level"] = profile["level"]
        guild = profile.get("guild")
        if isinstance(guild, dict):
            guild_name = loc(guild.get("name"))
            if guild_name:
                out["guild"] = guild_name
        spec_payload = self.sub_payload(w, "specializations", link("specializations", "specializations"))
        if isinstance(spec_payload, dict):
            for group in spec_payload.get("specialization_groups") or []:
                if not (isinstance(group, dict) and group.get("is_active") is True):
                    continue
                for spec in group.get("specializations") or []:
                    spec_name = loc(spec.get("specialization_name")) if isinstance(spec, dict) else None
                    if spec_name:
                        out["activeSpec"] = spec_name
                        break
                if "activeSpec" in out:
                    break
        if isinstance(profile.get("equipped_item_level"), int):
            out["itemLevel"] = profile["equipped_item_level"]
        equipment = self.sub_payload(w, "equipment", link("equipment", "equipment"))
        gear = []
        if isinstance(equipment, dict):
            for item in equipment.get("equipped_items") or []:
                if not isinstance(item, dict):
                    continue
                slot_node = item.get("slot") if isinstance(item.get("slot"), dict) else {}
                level_node = item.get("level") if isinstance(item.get("level"), dict) else {}
                icon = None
                media = item.get("media") if isinstance(item.get("media"), dict) else {}
                media_key = media.get("key") if isinstance(media.get("key"), dict) else {}
                if isinstance(media_key.get("href"), str) and static_ns:
                    icon = self.icon_for(http.set_ns(media_key["href"], static_ns), w)
                gear.append({
                    "slot": loc(slot_node.get("name")),
                    "name": loc(item.get("name")),
                    "ilvl": level_node.get("value") if isinstance(level_node.get("value"), int) else None,
                    "iconUrl": safe_artifact_url(icon, "iconUrl"),
                })
        out["gear"] = gear
        achievements = self.sub_payload(w, "achievements", link("achievements", "achievements"))
        if isinstance(achievements, dict) and isinstance(achievements.get("total_points"), int):
            out["achievementPoints"] = achievements["total_points"]
        elif isinstance(profile.get("achievement_points"), int):
            out["achievementPoints"] = profile["achievement_points"]
        # /statistics carries no sidecar field (notes §3.5 is combat stats); the
        # GET exists so the schema drift tripwire covers the surface (amendment 7).
        self.sub_payload(w, "statistics", link("statistics", "statistics"))
        pvp = self.sub_payload(w, "pvp_summary", link("pvp_summary", "pvp-summary"))
        if isinstance(pvp, dict):
            kills = pvp.get("honorable_kills") if isinstance(pvp.get("honorable_kills"), int) else None
            rank = pvp.get("pvp_rank") if isinstance(pvp.get("pvp_rank"), int) else None
            # progress keys appear only where data exists (notes §4: raid/M+ carry
            # no entries for any sampled character, so those keys stay omitted).
            if (kills or 0) > 0 or (rank or 0) > 0:
                out["progress"] = {"pvp": {"honorableKills": kills or 0, "pvpRank": rank or 0}}
        fetched_at = http.iso_now()
        if w.get("unchanged"):
            ce = self.cache.entry(w["profile_url"])
            if isinstance(ce, dict) and isinstance(ce.get("fetchedAt"), str):
                fetched_at = ce["fetchedAt"]
        out["fetchedAt"] = fetched_at
        return out
