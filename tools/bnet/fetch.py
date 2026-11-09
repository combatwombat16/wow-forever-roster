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


def extract_identity(profile):
    """The profile's own race / class references (§3.1) -> (block, malformed_keys).

    The data is already in the fetched profile, so it costs zero requests. The
    class ref is keyed ``character_class``, NOT ``playable_class`` (§3.1) — reading
    the retail key would silently publish nothing. A ref that carries an id but no
    name (or the reverse) is reported by its payload key rather than stored
    half-populated or dumped, because the sidecar gate below the header pins
    ``{id: int, name: str}`` and a one-sided object would render "null" in the UI.
    """
    if not isinstance(profile, dict):
        return {}, ["profile"]
    block, malformed = {}, []
    for side, payload_key in (("race", "race"), ("class", "character_class")):
        node = profile.get(payload_key)
        if node is None:
            continue                      # absent is not malformed: the ref simply is not there
        ref = _id_name(node)
        if ref and ref["id"] is not None and ref["name"]:
            block[side] = ref
        else:
            malformed.append(payload_key)
    return block, malformed


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
                        # Phase 4a: the profession icon is one static-ns request per
                        # distinct profession id (§10), so the Fetcher fills it and it
                        # starts null like the talent/tree icons — a pure extractor
                        # must not pretend to know a URL it cannot look up.
                        "iconUrl": None,
                        "tiers": tiers})
        return out

    primaries, secondaries = side("primaries"), side("secondaries")
    if not primaries and not secondaries:
        return None
    return {"primaries": primaries, "secondaries": secondaries}


def derive_full_render_url(avatar_url):
    """media.avatar -> the full-body render URL | None. Pure, and never a guess.

    Phase B (roster-ui-tasks.md): the armory page loads the character's full render
    at the avatar's own path with ``-main-raw.png`` where the avatar has
    ``-avatar.jpg`` (owner-verified 2026-11-09: 200, image/png, 1600x1200). The
    match is anchored to the END of the URL, so:

    * ``…/249157-avatar.jpg``     -> ``…/249157-main-raw.png``
    * ``…/249157-avatar-sd.jpg``  -> None (an ``avatarSd``, not an ``avatar``)
    * ``…/249157-something.jpg``  -> None (Blizzard renamed the asset; do not invent)
    * None / a non-string         -> None

    The result is passed back through the H2 icon validator even though its input is
    already a validated artifact value: the rule is that no URL enters a committed
    artifact without clearing ``safe_artifact_url``, and a derivation is not exempt.
    Being derived is exactly why it must be checked — the caller still has to HEAD it
    before it is publishable (``Fetcher.full_render_url``).
    """
    if not isinstance(avatar_url, str) or not avatar_url.endswith(config.AVATAR_ASSET_SUFFIX):
        return None
    stem = avatar_url[:-len(config.AVATAR_ASSET_SUFFIX)]
    return safe_artifact_url(stem + config.FULL_RENDER_ASSET_SUFFIX, "iconUrl")


def normalize_media(payload):
    """character-media/1 (§9.5) -> {avatar, avatarSd} | None.

    Forever serves exactly ``main-raw``, ``main-sd-raw``, ``avatar``,
    ``avatar-sd`` — there is no inset/headshot key (amendment 3), so T1.5
    targets these two. Values go through the H2 icon validator like every other
    URL written into a committed artifact.

    No ``fullRender`` here on purpose: validating the derived render costs a HEAD, so
    the Fetcher adds the key after the probe (``Fetcher.full_render_url``), and this
    extractor stays free of I/O for the offline tests."""
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


# --------------------------------------------------------------------------- #
# Phase 5b static icon maps (character-tasks.md P5.B, notes §11).
# Pure index-payload -> artifact-key functions: no I/O, so the shape contract of
# data/class-icons.json / data/profession-icons.json is pinned offline. The map is
# keyed by the NAME Blizzard gives (the UI has no id to start from: a roster row
# carries a class word, a distribution bar carries a class word), which makes the
# key attacker-influenced input, so it gets stricter rules than a value does.
# --------------------------------------------------------------------------- #
# Names that are Object.prototype property names. A hostile (or merely patched)
# index that named a profession ``constructor`` would hand the UI team an object
# whose ``map[name]`` lookup resolves to a built-in function, and whose
# ``for (const k of …) out[k] = …`` copy silently sets the prototype instead of
# creating an entry. Real Blizzard names never look like this, so refusing the key
# costs nothing and the refusal is reported rather than swallowed.
JS_OBJECT_KEY_HAZARDS = frozenset({
    "__proto__", "constructor", "prototype", "hasownproperty", "isprototypeof",
    "propertyisenumerable", "tolocalestring", "tostring", "valueof",
    "__definegetter__", "__definesetter__", "__lookupgetter__", "__lookupsetter__",
})


def icon_map_key(raw):
    """An icon-map KEY: a printable, bounded, non-hazardous name, else None.

    Deliberately never truncates: a cut-down name would answer lookups for a
    different profession and read as a correct entry. Control characters are
    refused because the artifact is line-oriented text that humans and scripts
    both grep, and a name carrying a scheme is refused because neither a class nor
    a profession has ever been called a URL."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or len(name) > config.ICON_MAP_KEY_MAX:
        return None
    if any(ch < " " or ch == "\x7f" for ch in name):
        return None
    if "://" in name:
        # A class or profession name is never a URL, and the committed maps are
        # scanned for exactly this pattern: refusing it here keeps a hostile index
        # from putting an attacker host into a publicly served artifact.
        return None
    if name.casefold() in JS_OBJECT_KEY_HAZARDS:
        return None
    return name


def icon_entry(value):
    """A candidate map value -> {id, iconUrl} | None, re-validated from scratch.

    Used for BOTH halves of the contract: a fresh fetch and a value carried over
    from the committed file. The carry-over path needs the gate for the same
    reason the cache-reuse path of ``icon_for`` does — a document written before a
    validator existed, or hand-edited since, is not evidence that its URLs are on
    the render host. An entry whose icon is missing is dropped rather than stored
    with a null: a map entry exists precisely because its icon resolved (§11)."""
    if not isinstance(value, dict):
        return None
    ident = _int(value.get("id"))
    url = safe_artifact_url(value.get("iconUrl"), "iconUrl")
    if ident is None or url is None:
        return None
    return {"id": ident, "iconUrl": url}


def normalize_icon_index(index_payload, entries_key):
    """playable-class / profession index (§5, §11) -> (entries, refused_descriptions).

    Names are locale maps in both indexes (§11: the §9.2 plain-string exception is
    the per-character payload, not the dictionary), so ``loc`` reads .en_US and
    still tolerates a plain string. Order is Blizzard's own; an entry without a
    usable int id or a usable key is described for the run summary and dropped —
    never stored under a key the UI cannot look up, and never fatal.
    """
    if not isinstance(index_payload, dict):
        return [], ["index payload was not an object"]
    nodes = index_payload.get(entries_key)
    if not isinstance(nodes, list):
        return [], [f"index carried no {entries_key} list"]
    entries, refused = [], []
    for node in nodes:
        if not isinstance(node, dict):
            refused.append("entry was not an object")
            continue
        ident = _int(node.get("id"))
        raw_name = loc(node.get("name"))
        key = icon_map_key(raw_name)
        if key is None:
            refused.append(f"name {str(raw_name)[:40]!r} is unusable as a key"
                           + ("" if ident is None else f" (id {ident})"))
            continue
        if ident is None:
            refused.append(f"{key!r} carried no integer id")
            continue
        entries.append({"id": ident, "name": key})
    return entries, refused


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
        # Phase B (media.fullRender): the derived full render is probed with one HEAD
        # per enriched character. A refusal is a published null, not an error, so it
        # gets its own tally rather than an entry in feature_failures (which means
        # "a block the sync could not store"); the run summary reports it as a NOTE.
        self.render_validated = 0
        self.render_failures = 0
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
        # Phase 5b (P5.B, notes §11): per-entry refusals for the two static icon
        # maps. Its own list rather than feature_failures because the unit being
        # refused is one index entry (up to 13 in one run), not one character
        # block, and the map's own summary line reports it entry by entry.
        self.icon_map_failures = []

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

    def static_media_icon(self, path, surface, w, **identifiers):
        """One static-namespace media doc -> allowlist-gated icon URL | None.

        The shared shape behind spell_icon for the two Phase 4a routes: the media
        family only answers in the re-discovered static namespace (§9.3.1 spells,
        §10 professions and playable classes), and the ``/data/wow/media/<kind>/{id}``
        prefix form is the one that exists — the ``…/{id}/media`` sub-resource form
        is a 0-byte unrouted 404. ``icon_for`` supplies the URL de-duplication (one
        request per distinct id per run), the cached-304 reuse and the H2
        ``safe_artifact_url`` gate, so a 404 or an off-host asset stores null.

        No budget cap here, unlike spell icons: the id space is bounded by the nine
        playable classes and the handful of profession ids a roster actually uses,
        and the URL de-dup already collapses repeats across characters.
        """
        if not self.static_ns:
            return None
        url = (f"{config.API_BASE}{path.format(**identifiers)}"
               f"?namespace={self.static_ns}")
        return self.icon_for(url, w, surface=surface)

    def full_render_url(self, avatar_url, character_id=None):
        """HEAD-validate the derived full render (Phase B) -> the URL, or None.

        Returns the URL only on an explicit HTTP 200 from the render host. A 3xx is
        a *result* here, not a hop (the opener never follows one), so a redirect away
        from the derived path is a refusal; 404/5xx, a transport error, a refused
        off-allowlist URL, an absent avatar and an avatar whose name does not end in
        the configured suffix are all the same answer. Every one of them publishes
        ``media.fullRender: null`` — the gear doll shows the generic silhouette
        instead of hotlinking a URL this run never saw served.

        Costs one request per enriched character; the request is uncredentialed and
        goes through ``Client.head``, which owns the pacing, the timeout, the
        no-redirect opener and the per-run cap.
        """
        derived = derive_full_render_url(avatar_url)
        if derived is None:
            return None
        try:
            resp = self.client.head(derived)
        except http.UnsafeUrl as exc:
            # The URL is derived from an already-validated avatar, so reaching this
            # line means config moved under the run; report it, publish nothing.
            self.warnings.append(
                f"id {character_id}: full-render HEAD refused by the asset allowlist: "
                f"{http.redact(str(exc))[:120]}")
            self.render_failures += 1
            return None
        if getattr(resp, "status", None) == 200:
            self.render_validated += 1
            return derived
        self.render_failures += 1
        return None

    def profession_icon(self, profession_id, w):
        """Profession icon for a primaries/secondaries entry (notes §9.2, §10)."""
        if _int(profession_id) is None:
            return None
        return self.static_media_icon(config.PROFESSION_MEDIA_PATH, "profession_media",
                                      w, profession_id=profession_id)

    def class_icon(self, class_id, w):
        """Playable-class icon for a character (§10: AVAILABLE).

        There is no ``race_icon`` counterpart on purpose: §10 probed
        ``/data/wow/playable-race/{id}/media``, ``/data/wow/media/playable-race/{id}``
        (eight race ids, four namespaces, plus the media index) and the race doc
        itself, and every one is a 0-byte unrouted 404 — Forever's
        ``playable-race`` document carries no ``media`` key at all. The sidecar
        therefore publishes no ``raceIconUrl`` key rather than a null one, so the UI
        cannot mistake "synced and empty" for "the API has nothing".
        """
        if _int(class_id) is None:
            return None
        return self.static_media_icon(config.PLAYABLE_CLASS_MEDIA_PATH, "playable_class_media",
                                      w, class_id=class_id)

    def note_feature_failure(self, character_id, surface, detail):
        """Record an optional feature block that could not be stored, with its
        exact cause, so the run summary reports it instead of a silent absence."""
        self.feature_failures.append(f"id {character_id}: {surface} — {detail}")

    def note_icon_map_failure(self, kind, detail):
        """One icon-map entry that was refused, with its exact cause (§11).

        The same pattern as note_feature_failure and the same reach: the line is
        printed with the run summary through log_safe(http.redact(line)), so it is
        reported, never committed."""
        self.icon_map_failures.append(f"{kind}: {detail}")

    def icon_map(self, *, kind, index_path, entries_key, index_surface,
                 media_path, media_placeholder, media_surface, prior_document):
        """One static-ns index + one media doc per id -> the committed icon map.

        The shared shape behind class_icon_map and profession_icon_map. Three
        rules, all inherited from the proven precedents:

        * **once per run, cached**: one index GET plus one media GET per distinct
          id, and the URL de-dup in ``icon_for`` means a class/profession whose
          icon the character pass already fetched costs nothing extra;
        * **merge by name with the committed map while the pinned static namespace
          is unchanged** (the talent-tree rule, §9.3.2): a name the previous patch
          already resolved costs zero requests this run, and a patch (new ns)
          makes the old map worthless, so it is dropped rather than merged;
        * **no icon, no entry**: a 404 or an off-host asset omits that key and
          reports one line. The map never carries a null iconUrl, because the whole
          point of an entry is that the icon resolved.

        Returns ``(icons, index_total, carried)``. A failed index read returns an
        empty map, which the caller treats as "leave the committed file alone".
        """
        if not self.static_ns:
            self.note_icon_map_failure(kind, "static namespace unknown this run — map not rebuilt")
            return {}, 0, 0
        prior = prior_document if isinstance(prior_document, dict) else {}
        prior_icons = {}
        if prior:
            if prior.get("staticNamespace") == self.static_ns and isinstance(prior.get("icons"), dict):
                prior_icons = prior["icons"]
            else:
                self.warnings.append(
                    f"{kind}: committed map is pinned to "
                    f"{str(prior.get('staticNamespace'))[:40]!r}, this run discovered {self.static_ns}; "
                    "every icon is refetched and the old entries are dropped, not merged")
        url = f"{config.API_BASE}{index_path}?namespace={self.static_ns}"
        resp = self.client.get(url)
        payload = resp.json if resp.status in (200, 304) else None
        if not isinstance(payload, dict):
            self.note_icon_map_failure(kind, f"{index_path} index returned HTTP {resp.status} — "
                                             "committed map left untouched")
            return {}, 0, 0
        self.note_revision(index_surface, resp.headers)
        # A poisoned index can carry a non-list here ("classes": 5); len() would raise a raw
        # TypeError and break the never-fatal/always-reported contract after the pair is on disk.
        entries_raw = payload.get(entries_key)
        index_total = len(entries_raw) if isinstance(entries_raw, list) else 0
        entries, refused = normalize_icon_index(payload, entries_key)
        for reason in refused:
            self.note_icon_map_failure(kind, f"index entry refused — {reason}")
        icons, carried = {}, 0
        for entry in entries:
            name = entry["name"]
            if name in icons:
                # Two index rows claiming one key: first wins, and the loser is
                # reported, because "which id is Warrior?" must never be decided by
                # whatever a hostile payload appended last.
                self.note_icon_map_failure(kind, f"duplicate name {name!r} (id {entry['id']}) — "
                                                 "first index entry kept")
                continue
            # An id that moved under a stable name means the carried icon belongs to a
            # different doc, so it is refetched rather than reused.
            previous = icon_entry(prior_icons.get(name)) if prior_icons else None
            if previous and previous["id"] == entry["id"]:
                icons[name] = previous
                carried += 1
                self.saved_requests += 1
                self.skipped["icon_maps"] += 1
                continue
            # static_media_icon is the proven §10 shape: it builds the static-ns URL,
            # reuses the cached doc, and hands the value to icon_for, which is where
            # safe_artifact_url lives. Nothing here writes a URL of its own.
            icon = self.static_media_icon(media_path, media_surface, {},
                                          **{media_placeholder: entry["id"]})
            if icon is None:
                self.note_icon_map_failure(kind, f"{name} (id {entry['id']}) — media doc yielded no "
                                                 "allowlisted icon (HTTP 404 or refused host); entry omitted")
                continue
            icons[name] = {"id": entry["id"], "iconUrl": icon}
        return icons, index_total, carried

    def class_icon_map(self, prior_document):
        """data/class-icons.json: every playable class (§5 index + §10.2 prefix route)."""
        return self.icon_map(kind="class icons",
                             index_path=config.PLAYABLE_CLASS_INDEX,
                             entries_key="classes",
                             index_surface="playable_class_index",
                             media_path=config.PLAYABLE_CLASS_MEDIA_PATH,
                             media_placeholder="class_id",
                             media_surface="playable_class_media",
                             prior_document=prior_document)

    def profession_icon_map(self, prior_document):
        """data/profession-icons.json: every profession, primary and secondary alike
        (§11 index + §9.2/§10.1 prefix route — one id 185 Cooking and one id 164
        Blacksmithing answer identically, so no primary/secondary split exists here)."""
        return self.icon_map(kind="profession icons",
                             index_path=config.PROFESSION_INDEX,
                             entries_key="professions",
                             index_surface="profession_index",
                             media_path=config.PROFESSION_MEDIA_PATH,
                             media_placeholder="profession_id",
                             media_surface="profession_media",
                             prior_document=prior_document)

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
        # Phase 4a: race and class identity, straight off this same profile payload
        # (notes §3.1) — zero extra requests. raceIconUrl is never written: §10
        # proved no race-media route exists, and an omitted key is the honest
        # signal. classIconUrl comes from the class doc the probe confirmed.
        identity, malformed_identity = extract_identity(profile)
        out.update(identity)
        for payload_key in malformed_identity:
            self.warnings.append(
                f"id {w.get('rec', {}).get('id')}: profile carried a malformed {payload_key} "
                "reference (no usable id+name) — key omitted, not guessed")
        if "class" in identity:
            out["classIconUrl"] = self.class_icon(identity["class"]["id"], w)
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
            # Phase 4a: one cached static-ns icon per distinct profession id, both
            # arrays (§10 confirmed 200 for a primary and a secondary id alike).
            for entry in professions_block["primaries"] + professions_block["secondaries"]:
                entry["iconUrl"] = self.profession_icon(entry["id"], w)
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
            # Phase B (roster-ui-tasks.md): the gear paper doll hangs on the full-body
            # render, derived from this avatar and HEAD-validated before it is
            # written. The convention (mirrored in bnet.artifacts.MEDIA_KEYS and
            # PERMITTED AND SHAPE-CHECKED — not presence-gated — in
            # .pi/scripts/verify_sync_artifacts.py): this writer always SETS fullRender on
            # every media block it emits, the 200-validated URL or an explicit null, while
            # a row with no character-media payload carries no media key at all. The gate
            # admits the key beside avatar/avatarSd and validates any value it finds (render
            # host, and the exact suffix swap of that row's own avatar); nothing enforces
            # presence, because the rows committed before Phase B legitimately lack it, so
            # this assignment is the only thing that puts the key there. Null is the
            # published answer for "no render", never a placeholder URL.
            media_block["fullRender"] = self.full_render_url(media_block.get("avatar"), rid)
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
