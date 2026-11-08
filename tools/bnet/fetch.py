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


# --------------------------------------------------------------------------- #
# Phase 1 modal-rework extractors (character-tasks.md T1.1–T1.5, notes §9).
# Pure payload -> sidecar functions: no I/O, so the unit tests can pin every
# shape contract offline. The sidecar is additive schema evolution — existing
# keys keep their meaning, and a block whose fetch failed is OMITTED (the
# guild/progress optionality rule), never published as an empty lie.
# --------------------------------------------------------------------------- #
def _number(value):
    """Numeric stat value, bools excluded (bool is a subclass of int in Python,
    and no Battle.net stat is ever a boolean)."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _camel(name):
    """API snake_case keys -> the sidecar's established camelCase convention."""
    head, *tail = str(name).split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _id_name(node):
    """Static-doc reference {id, name(locale|plain)} -> {id, name} | None (§3.2
    item_class / item_subclass; plain names handled by loc() too)."""
    if not isinstance(node, dict):
        return None
    ident = _int(node.get("id"))
    name = loc(node.get("name"))
    if ident is None and not name:
        return None
    return {"id": ident, "name": name}


def extract_item_color(color):
    """stats[].display.color RGBA (§3.2) kept verbatim; None when empty."""
    if not isinstance(color, dict):
        return None
    out = {key: _number(color.get(key)) for key in ("r", "g", "b", "a")}
    return out if any(v is not None for v in out.values()) else None


def extract_item_stats(stats):
    """equipped_items[].stats[] -> [{value, displayString, isEquipBonus, color}].

    Notes §9.4 is the verdict that these numbers exist and that /equipment is
    their ONLY source, and the same sweep proved sockets/gems do not exist — so
    no gem field is ever emitted here (amendment 2)."""
    out = []
    for stat in stats or []:
        if not isinstance(stat, dict):
            continue
        display = stat.get("display") if isinstance(stat.get("display"), dict) else {}
        value = _number(stat.get("value"))
        display_string = loc(display.get("display_string"))
        color = extract_item_color(display.get("color"))
        if value is None and not display_string and color is None:
            continue
        out.append({
            "value": value,
            "displayString": display_string,
            # 24 of 61 sampled items flag bonus stats (§9.4); the flag is the only
            # bonus-list signal the API offers (bonus_list ids decode nowhere).
            "isEquipBonus": stat.get("is_equip_bonus") is True,
            "color": color,
        })
    return out


def extract_enchants(enchantments):
    """equipped_items[].enchantments[].display_string (§3.2) as plain strings."""
    out = []
    for enchantment in enchantments or []:
        if not isinstance(enchantment, dict):
            continue
        text = loc(enchantment.get("display_string"))
        if text:
            out.append(text)
    return out


def normalize_statistics(payload):
    """character-statistics/2 (§3.5, §9.5) -> camelCase sidecar block.

    Returns ``(block | None, skipped_keys)``. Raw Blizzard docs are never
    committed, so an unrecognised value shape is *reported*, not dumped: the
    skipped keys reach the run summary, which is how a game patch adding a stat
    becomes a visible finding instead of silently dropped data (T1.2)."""
    if not isinstance(payload, dict):
        return None, []
    out, skipped = {}, []
    for key, value in payload.items():
        if key in ("character", "_links"):
            continue                      # sub-doc back-reference, not a stat
        if isinstance(value, bool):
            skipped.append(key)
            continue
        if isinstance(value, (int, float)):
            out[_camel(key)] = value
        elif isinstance(value, dict):
            if "base" in value or "effective" in value:
                out[_camel(key)] = {"base": _number(value.get("base")),
                                    "effective": _number(value.get("effective"))}
            elif "value" in value or "rating_bonus" in value or "rating_normalized" in value:
                out[_camel(key)] = {"value": _number(value.get("value")),
                                    "ratingBonus": _number(value.get("rating_bonus")),
                                    "ratingNormalized": _number(value.get("rating_normalized"))}
            elif "id" in value and "name" in value:
                out[_camel(key)] = {"id": _int(value.get("id")),
                                    "name": loc(value.get("name"))}   # power_type
            else:
                skipped.append(key)
        else:
            skipped.append(key)          # §3.5 carries no string/list values
    return (out or None), skipped


def normalize_reputations(payload):
    """character-reputations/1 (§9.1) -> [{factionId, factionName, standing}].

    The standing tuple is stored whole because the bar uses ``value/max`` (same
    tier) while ``raw`` is cumulative and exceeds ``max`` on any tier > 1 — a
    renderer given only one pair would draw wrong bars for 4 of 11 sampled rows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("reputations"), list):
        return None
    out = []
    for reputation in payload["reputations"]:
        if not isinstance(reputation, dict):
            continue
        faction = reputation.get("faction") if isinstance(reputation.get("faction"), dict) else {}
        standing = reputation.get("standing") if isinstance(reputation.get("standing"), dict) else {}
        faction_id, faction_name = _int(faction.get("id")), loc(faction.get("name"))
        if faction_id is None and not faction_name:
            continue
        out.append({
            "factionId": faction_id,
            "factionName": faction_name,
            "standing": {"raw": _number(standing.get("raw")),
                         "value": _number(standing.get("value")),
                         "max": _number(standing.get("max")),
                         "tier": _number(standing.get("tier")),
                         "name": loc(standing.get("name"))},
        })
    return out or None


def _recipe_entry(recipe):
    """known_recipes[] -> {id, name}. §9.2: names are PLAIN strings here; a
    future locale map or a bare string entry is normalized defensively."""
    if isinstance(recipe, str):
        return {"id": None, "name": recipe} if recipe else None
    if not isinstance(recipe, dict):
        return None
    recipe_id, name = _int(recipe.get("id")), loc(recipe.get("name"))
    if recipe_id is None and not name:
        return None
    return {"id": recipe_id, "name": name}


def normalize_professions(payload):
    """character-professions/1 (§9.2) -> {primaries, secondaries} | None.

    ``max_skill_points`` varies per profession and tier, so both bounds are kept
    per tier; the block is omitted (not emptied) when the payload carries no
    profession at all — the optional-block rule of T1.4."""
    if not isinstance(payload, dict):
        return None

    def side(key):
        out = []
        for entry in payload.get(key) or []:
            if not isinstance(entry, dict):
                continue
            profession = entry.get("profession") if isinstance(entry.get("profession"), dict) else {}
            tiers = []
            for tier in entry.get("tiers") or []:
                if not isinstance(tier, dict):
                    continue
                tiers.append({
                    "skillPoints": _number(tier.get("skill_points")),
                    "maxSkillPoints": _number(tier.get("max_skill_points")),
                    "tierId": _int((tier.get("tier") or {}).get("id")) if isinstance(tier.get("tier"), dict) else None,
                    "tierName": loc((tier.get("tier") or {}).get("name")) if isinstance(tier.get("tier"), dict) else None,
                    "knownRecipes": [r for r in (_recipe_entry(x) for x in tier.get("known_recipes") or []) if r],
                })
            out.append({"id": _int(profession.get("id")),
                        "name": loc(profession.get("name")),
                        "tiers": tiers})
        return out

    primaries, secondaries = side("primaries"), side("secondaries")
    if not primaries and not secondaries:
        return None
    return {"primaries": primaries, "secondaries": secondaries}


def normalize_media(payload):
    """character-media/1 (§9.5) -> {avatar, avatarSd} | None.

    Forever serves exactly ``main-raw``, ``main-sd-raw``, ``avatar``,
    ``avatar-sd`` — there is no inset/headshot key (amendment 3), so T1.5
    targets these two. Values go through the H2 icon validator like every other
    URL written into a committed artifact."""
    if not isinstance(payload, dict):
        return None
    wanted = {"avatar": "avatar", "avatar-sd": "avatarSd"}
    found = {}
    for asset in payload.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        side = wanted.get(asset.get("key"))
        if side:
            found[side] = safe_artifact_url(asset.get("value"), "iconUrl")
    if not found:
        return None
    return {"avatar": found.get("avatar"), "avatarSd": found.get("avatarSd")}


def _spell_tooltip_fields(tooltip):
    """{spell, description, cast_time} of a spell_tooltip (§9.3) -> flat fields.

    power_cost was {} on every sampled talent and range/cooldown appear on some;
    none of those render in Phase 2's tooltip, so they are not captured — the
    tooltip/description T1.3 asks for is spellName/description/castTime."""
    tooltip = tooltip if isinstance(tooltip, dict) else {}
    spell = tooltip.get("spell") if isinstance(tooltip.get("spell"), dict) else {}
    return {
        # THE trap of §9.3.1: the icon route takes spell.id, not talent.id.
        "spellId": _int(spell.get("id")),
        "spellName": loc(spell.get("name")),
        "description": loc(tooltip.get("description")),
        "castTime": loc(tooltip.get("cast_time")),
    }


def normalize_selected_talents(spec_payload):
    """character-specializations/1 -> selected talents grouped per spec (§9.3).

    A group can carry SEVERAL specializations (multi-spec is real: Arms+Protection
    on beef-pho), so every spec of every group is iterated — unlike the older
    ``specializations[0]``-only activeSpec read, which stays untouched. A group
    without specializations is the "no spec chosen" case and yields nothing."""
    if not isinstance(spec_payload, dict):
        return None
    out = []
    for group in spec_payload.get("specialization_groups") or []:
        if not isinstance(group, dict):
            continue
        for spec in group.get("specializations") or []:
            if not isinstance(spec, dict):
                continue
            talents = []
            for talent in spec.get("talents") or []:
                if not isinstance(talent, dict):
                    continue
                talent_node = talent.get("talent") if isinstance(talent.get("talent"), dict) else {}
                fields = _spell_tooltip_fields(talent.get("spell_tooltip"))
                talents.append({"rank": _number(talent.get("talent_rank")),
                                # the id shared with talent-tree rank tooltips
                                # (§9.3.2), i.e. what lights a tree node up
                                "talentId": _int(talent_node.get("id")),
                                "iconUrl": None, **fields})
            if not talents:
                continue                  # nothing selected in this spec to store
            spec_node = spec.get("specialization") if isinstance(spec.get("specialization"), dict) else {}
            out.append({"specId": _int(spec_node.get("id")),
                        "specName": loc(spec.get("specialization_name")),
                        "spentPoints": _number(spec.get("spent_points")),
                        "talents": talents})
    return out or None


def normalize_talent_tree(payload):
    """talent-tree/2 spec document (§9.3.2) -> geometry + ranks; iconUrl stays
    None here — filling icons costs one request per spell, so the Fetcher does
    that and caps it. The doc is stored ONCE per spec in data/talent-trees.json
    (it is 128–171 KB raw and identical for every character on the patch)."""
    if not isinstance(payload, dict):
        return None
    nodes = []
    for node in payload.get("spec_talent_nodes") or []:
        if not isinstance(node, dict):
            continue
        ranks = []
        for rank in node.get("ranks") or []:
            if not isinstance(rank, dict):
                continue
            tooltip = rank.get("tooltip") if isinstance(rank.get("tooltip"), dict) else {}
            talent_node = tooltip.get("talent") if isinstance(tooltip.get("talent"), dict) else {}
            ranks.append({"rank": _number(rank.get("rank")),
                          "talentId": _int(talent_node.get("id")),
                          "iconUrl": None, **_spell_tooltip_fields(tooltip.get("spell_tooltip"))})
        nodes.append({
            "id": _int(node.get("id")),
            "displayRow": _number(node.get("display_row")),
            "displayCol": _number(node.get("display_col")),
            "rawPositionX": _number(node.get("raw_position_x")),
            "rawPositionY": _number(node.get("raw_position_y")),
            # prerequisite graph (§9.3.2): node ids only, non-ints dropped
            "unlocks": [x for x in node.get("unlocks") or [] if isinstance(x, int) and not isinstance(x, bool)],
            "lockedBy": [x for x in node.get("locked_by") or [] if isinstance(x, int) and not isinstance(x, bool)],
            "ranks": ranks,
        })
    if not nodes:
        return None                       # a tree without nodes is a failed doc
    playable_class = payload.get("playable_class") if isinstance(payload.get("playable_class"), dict) else {}
    spec = payload.get("playable_specialization") if isinstance(payload.get("playable_specialization"), dict) else {}
    return {"treeId": _int(payload.get("id")),
            "classId": _int(playable_class.get("id")),
            "className": loc(playable_class.get("name")),
            "specId": _int(spec.get("id")),
            "specName": loc(spec.get("name")),
            "name": loc(payload.get("name")),
            "nodes": nodes}


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
        self.icon_urls = {}          # media URL -> icon URL (in-run dedup)
        self.icon_failures = 0
        self.drift = []
        self._drift_seen = set()
        self.warnings = []
        self.skipped = Counter()
        self.saved_requests = 0
        # Phase 1 modal rework (T1.1–T1.5): spell-icon accounting, the shared
        # talent-tree cache (spec id -> tree doc), and the explicit findings for
        # optional blocks a fetch refused — a failed block must be reportable,
        # never a silently absent feature.
        self.spell_icon_lookups = 0
        self.spell_icon_capped = False
        self.talent_trees = {}
        self.feature_failures = []
        self.sub_failures = []

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
        # Keep the exact status so a missing feature block can be reported with
        # its real cause instead of "no data" (§8: one reference failure is
        # non-fatal, but it is never invisible).
        self.sub_failures.append((surface, resp.status))
        return None

    def icon_for(self, media_url, w, surface="item_media"):
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
            self.note_revision(surface, resp.headers)
            # H2: an asset value is response data; only the Blizzard render host is
            # publishable, anything else counts as a failed icon and stores null.
            icon = safe_artifact_url(extract_icon(resp.json), "iconUrl")
        if icon is None:
            # notes §8: a single reference-doc failure is explicitly non-fatal;
            # the gear row survives with iconUrl null.
            self.icon_failures += 1
        self.icon_urls[media_url] = icon
        return icon

    def spell_icon(self, spell_id, w):
        """Spell icon for a talent (§9.3.1): one request per distinct spell id at
        ``/data/wow/media/spell/{spell_id}`` in the STATIC namespace only — the
        route 404s in dynamic/profile, and it wants the spell id, never the
        talent id. Returns None (never raises) above the per-run cap."""
        if _int(spell_id) is None or not self.static_ns:
            return None
        url = (f"{config.API_BASE}{config.SPELL_MEDIA_PATH.format(spell_id=spell_id)}"
               f"?namespace={self.static_ns}")
        if url in self.icon_urls:
            return self.icon_urls[url]      # deduped spell ids never touch the cap
        if self.spell_icon_lookups >= config.MAX_SPELL_ICON_LOOKUPS:
            if not self.spell_icon_capped:
                self.spell_icon_capped = True
                self.warnings.append(
                    f"spell icon lookup cap ({config.MAX_SPELL_ICON_LOOKUPS}) reached; "
                    "remaining talent icons stay null this run")
            return None
        self.spell_icon_lookups += 1
        return self.icon_for(url, w, surface="spell_media")

    def note_feature_failure(self, character_id, surface, detail):
        """Record an optional feature block that could not be stored, with its
        exact cause, so the run summary reports it instead of a silent absence."""
        self.feature_failures.append(f"id {character_id}: {surface} — {detail}")

    def seed_talent_trees(self, document):
        """Carry previously captured spec trees forward when the pinned static
        namespace is unchanged (§9.3.2: the tree doc is version data, so a re-run
        on the same patch re-fetches nothing)."""
        if not isinstance(document, dict):
            return
        specs = document.get("specs")
        if document.get("staticNamespace") == self.static_ns and isinstance(specs, dict):
            for spec_id, tree in specs.items():
                if isinstance(tree, dict):
                    self.talent_trees.setdefault(str(spec_id), tree)

    def talent_tree_for(self, character_id, spec_id, w):
        """Spec talent-tree definition via playable-specialization -> its
        ``spec_talent_tree.key.href`` (§9.3.2), fetched at most once per spec per
        cache generation and stored in ``self.talent_trees`` (written out by the
        sync as data/talent-trees.json). Failures are recorded per character."""
        if _int(spec_id) is None or not self.static_ns:
            return None
        key = str(spec_id)
        if key in self.talent_trees:
            return self.talent_trees[key]
        spec_url = (f"{config.API_BASE}{config.PLAYABLE_SPECIALIZATION_PATH.format(spec_id=spec_id)}"
                    f"?namespace={self.static_ns}")
        resp = self.client.get(spec_url)
        payload = resp.json if resp.status in (200, 304) else None
        if not isinstance(payload, dict):
            self.note_feature_failure(character_id, f"talent-tree(spec {spec_id})",
                                      f"playable-specialization fetch returned HTTP {resp.status}")
            return None
        self.note_revision("playable_specialization", resp.headers)
        # §9.3.2: playable-specialization/2 nests the tree reference the same way
        # the profile nests its guild reference — spec_talent_tree.key.href.
        tree_ref = payload.get("spec_talent_tree") if isinstance(payload.get("spec_talent_tree"), dict) else {}
        key_node = tree_ref.get("key") if isinstance(tree_ref.get("key"), dict) else {}
        tree_href = key_node.get("href")
        # H1: the href is untrusted response data; only a Blizzard-origin URL may
        # ever receive the bearer token (set_ns rewrites the query, never rebuilds).
        if not (isinstance(tree_href, str) and http.allowed_api_url(tree_href)):
            self.note_feature_failure(character_id, f"talent-tree(spec {spec_id})",
                                      "playable-specialization carried no usable spec_talent_tree href")
            return None
        tree_resp = self.client.get(http.set_ns(tree_href, self.static_ns))
        tree = normalize_talent_tree(tree_resp.json if tree_resp.status in (200, 304) else None)
        if tree is None:
            self.note_feature_failure(character_id, f"talent-tree(spec {spec_id})",
                                      f"talent-tree doc returned HTTP {tree_resp.status} or no nodes")
            return None
        self.note_revision("talent_tree", tree_resp.headers)
        for node in tree["nodes"]:
            for rank in node["ranks"]:
                rank["iconUrl"] = self.spell_icon(rank["spellId"], w)
        self.talent_trees[key] = tree
        return tree

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
        rid = w.get("rec", {}).get("id")
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
        else:
            self.note_feature_failure(rid, "specializations",
                                      "fetch failed (no payload) — talents and talent trees skipped")
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
                item_node = item.get("item") if isinstance(item.get("item"), dict) else {}
                quality_node = item.get("quality") if isinstance(item.get("quality"), dict) else {}
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
                    # T1.1 tooltip fields from the SAME payload (§9.4: /equipment
                    # is the only source that carries stat numbers, so this costs
                    # zero extra requests). No gem/socket keys are ever emitted —
                    # they do not exist in this API.
                    "itemId": _int(item_node.get("id")),
                    "quality": quality_node.get("type") if isinstance(quality_node.get("type"), str) else None,
                    "itemClass": _id_name(item.get("item_class")),
                    "itemSubclass": _id_name(item.get("item_subclass")),
                    "enchants": extract_enchants(item.get("enchantments")),
                    "stats": extract_item_stats(item.get("stats")),
                })
        out["gear"] = gear

        # T1.2 (/statistics): fetched by every pre-Phase-1 run and thrown away as
        # a schema tripwire; now normalized into the sidecar. Unrecognised value
        # shapes are reported, never dumped raw and never dropped in silence.
        statistics_payload = self.sub_payload(w, "statistics", link("statistics", "statistics"))
        stats_block, stats_skipped = normalize_statistics(statistics_payload)
        if stats_block:
            out["stats"] = stats_block
        else:
            self.note_feature_failure(rid, "statistics", "fetch failed or carried no known stat (no payload)")
        if stats_skipped:
            shown = ", ".join(stats_skipped[:8]) + (" …" if len(stats_skipped) > 8 else "")
            self.warnings.append(f"id {rid}: statistics carried {len(stats_skipped)} unrecognised "
                                 f"key(s) ({shown}) — re-probe the contract (notes §3.5)")

        # T1.3: selected talents per spec (spell icons resolved via spell.id).
        talents = normalize_selected_talents(spec_payload)
        if talents:
            for talent_group in talents:
                for talent in talent_group["talents"]:
                    talent["iconUrl"] = self.spell_icon(talent["spellId"], w)
            out["talents"] = talents
            # Tree geometry for exactly the specs in use — stored once per spec
            # in data/talent-trees.json, never duplicated per character.
            for talent_group in talents:
                self.talent_tree_for(rid, talent_group["specId"], w)

        # T1.4: whole blocks optional, mirroring the guild/progress rule; a
        # refused or broken fetch is a recorded finding, an empty payload simply
        # omits the block.
        professions = self.sub_payload(w, "professions", link("professions", "professions"))
        professions_block = normalize_professions(professions)
        if professions_block:
            out["professions"] = professions_block
        elif professions is None:
            self.note_feature_failure(rid, "professions", "fetch failed (no payload)")
        reputations = self.sub_payload(w, "reputations", link("reputations", "reputations"))
        reputations_block = normalize_reputations(reputations)
        if reputations_block:
            out["reputations"] = reputations_block
        elif reputations is None:
            self.note_feature_failure(rid, "reputations", "fetch failed (no payload)")

        # T1.5: avatar renders for the modal header (§9.5: no inset/headshot on
        # Forever; values are finished render-host URLs, H2-validated inside).
        media_payload = self.sub_payload(w, "character_media", link("media", "character-media"))
        media_block = normalize_media(media_payload)
        if media_block:
            out["media"] = media_block
        else:
            self.note_feature_failure(rid, "character-media", "no avatar/avatar-sd asset served")

        achievements = self.sub_payload(w, "achievements", link("achievements", "achievements"))
        if isinstance(achievements, dict) and isinstance(achievements.get("total_points"), int):
            out["achievementPoints"] = achievements["total_points"]
        elif isinstance(profile.get("achievement_points"), int):
            out["achievementPoints"] = profile["achievement_points"]
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
