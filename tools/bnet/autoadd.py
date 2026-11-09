"""Phase C: additive auto-add of Battle.net-visible guild members into the roster.

``plan`` is pure: it takes the roster rows that are already committed and the guild
rosters this run fetched, and returns the rows to APPEND. It never mutates, removes or
reorders an existing row, and it never invents identity data it was not given — a new
row is a *placeholder* a human curates afterwards (roster-ui-tasks.md Phase C).

Why it is pure while the writer is not: the caller (``bnet.sync``) has to sequence this
against resolution and the H4 floor, and it is the only thing that knows whether the
run is allowed to publish at all. Keeping the decision here — "which members are
missing, and what does their row look like" — means the interesting invariants
(idempotence, never-mutate, the exact row spec) are testable without a transport.

The row spec (owner decision, Phase C):

* ``id``          a fresh 13-digit numeric string, collision-checked against every id
                  already in the roster and against every other id this plan emits;
* ``name``        ``"<name> <surname>"`` — the single-field convention every existing row
                  uses (the API keeps them apart, this roster does not);
* ``player``      the member's own name, as a placeholder alias: the roster's ``player``
                  column is the only account grouping key there is (no Battletag is
                  exposed on any endpoint), and a curated pass renames it;
* ``track``       ``"alts"`` — the catch-all track; a guild roster says nothing about
                  which of the team's two tracks a character belongs to;
* ``guild``       the guild's own name, off the profile that made the guild discoverable
                  (Phase C's optional additive roster field; it is what the guild filter
                  reads for a row that has no sidecar yet);
* ``role`` / ``roleSec`` / ``prof1`` / ``prof2``
                  ``"None"`` — the literal this file already uses for "empty" in those
                  columns (``js/roster.js`` hides exactly that value);
* ``race`` / ``class``
                  DELIBERATELY ABSENT. A guild-roster member carries race and class ids,
                  not names, and mapping ids to the site's frozen vocabulary would be a
                  second lookup per member. An unknown is recorded as an unknown; the
                  curated pass fills both from the resolved profile.
* ``validation``  ``{"state": "pending"}`` — the one stored state besides ``validated``.
                  It says "the nightly wrote this, nobody has reviewed it". The UI's
                  ``deriveLifecycleState`` only recognises ``validated``, so ``pending``
                  is inert there and the row keeps deriving from the map status.

Matching mirrors ``resolve.member_norm_key`` (case-insensitive, trimmed ``(name,
surname)``) and, because a roster row keeps both halves in one string, the resolver's own
``norm_slug(row name)`` too: a member is already present when EITHER canonical form is a
form any existing row carries. That is deliberately the wider test — adding a second row
for a character the tracker already lists is the failure mode this feature cannot afford,
since sync never removes a row.
"""

import time

from . import resolve

# Ids in this roster are 13-digit numeric strings (the tracker's own epoch-millisecond
# shape). Kept as a length + isdigit rule rather than a range so the check cannot drift.
ID_DIGITS = 13
# A bounded probe: 10k consecutive collisions means the seed is nonsense (or the roster is
# enormous), and raising is better than an unbounded loop that hangs the nightly.
ID_PROBE_MAX = 10000

AUTO_TRACK = "alts"
AUTO_STATE = "pending"
# The literal this roster already uses for "no value" in the role / profession columns.
EMPTY_FIELD = "None"


class IdAllocator:
    """Fresh 13-digit numeric-string ids, never one already in use.

    Seeded from the clock (epoch milliseconds, the shape every existing id has) and
    advanced by one per allocation, so a run that adds five members gets five consecutive
    ids and the next run cannot collide with them. The seed is injectable because a test
    must be able to say what the id will be.
    """

    def __init__(self, used, *, seed=None):
        self.used = {i for i in (used or ()) if isinstance(i, str)}
        self.next = int(time.time() * 1000) if seed is None else int(seed)

    def allocate(self):
        candidate = self.next
        for _ in range(ID_PROBE_MAX):
            text = str(candidate)
            if len(text) == ID_DIGITS and text.isdigit() and text not in self.used:
                self.used.add(text)
                self.next = candidate + 1
                return text
            candidate += 1
        raise ValueError(f"no unused {ID_DIGITS}-digit id within {ID_PROBE_MAX} of the seed")


def member_forms(member):
    """The member's trimmed ``(name, surname)``, or None when the entry carries no usable name."""
    ch = member.get("character") if isinstance(member, dict) else None
    if not isinstance(ch, dict):
        return None
    name = ch.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    surname = ch.get("surname")
    surname = surname.strip() if isinstance(surname, str) else ""
    return {"name": name.strip(), "surname": surname}


def member_keys(member):
    """Every canonical spelling under which the roster could already carry this member.

    Two forms on purpose: ``resolve.member_norm_key`` (what the guild lever matches with)
    and ``resolve.norm_slug`` of the joined full name (what the resolver matches a roster
    row with, and the only one that survives a surname carrying stray whitespace).
    """
    forms = member_forms(member)
    if forms is None:
        return set()
    keys = set()
    norm_key = resolve.member_norm_key(member)
    if norm_key:
        keys.add(norm_key)
    joined = resolve.norm_slug(f"{forms['name']} {forms['surname']}")
    if joined:
        keys.add(joined)
    return keys


def row_keys(rec):
    """The canonical forms one roster row is already known under."""
    name = rec.get("name") if isinstance(rec, dict) else None
    if not isinstance(name, str) or not name.strip():
        return set()
    return {resolve.norm_slug(name)}


def guild_name_for(entry, slug):
    """The guild's display name, falling back to Blizzard's own slug when a profile omitted it."""
    name = entry.get("name") if isinstance(entry, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return str(slug)


def build_row(*, forms, guild_name, new_id):
    """One placeholder row in the exact field order every existing row uses.

    The ten canonical fields come first in their existing order (with ``race``/``class``
    absent by design, see the module docstring), then the optional Phase C ``guild``
    field, then the ``validation`` block — the same tail convention the validated rows use.
    """
    full = f"{forms['name']} {forms['surname']}".strip()
    return {
        "id": new_id,
        "player": forms["name"],
        "name": full,
        "track": AUTO_TRACK,
        "role": EMPTY_FIELD,
        "roleSec": EMPTY_FIELD,
        "prof1": EMPTY_FIELD,
        "prof2": EMPTY_FIELD,
        "guild": guild_name,
        "validation": {"state": AUTO_STATE},
    }


def plan(roster_records, guilds, *, seed=None):
    """The rows to APPEND for guild members the roster does not list yet.

    ``roster_records`` is read, never touched. ``guilds`` is
    ``{guild_slug: {"name": <display name>, "members": [<guild roster member>, ...]}}`` —
    whatever this run already fetched; nothing here fetches anything.

    Idempotent by construction: the second run sees the rows the first one appended, so
    every member key is already known and the plan is empty. A member visible in two
    discovered guilds yields ONE row (the first guild that sorts wins the name), and a
    member with no usable name yields nothing at all.
    """
    existing = [rec for rec in (roster_records or []) if isinstance(rec, dict)]
    known = set()
    for rec in existing:
        known |= row_keys(rec)
    allocator = IdAllocator((rec.get("id") for rec in existing), seed=seed)

    candidates = []
    for slug in sorted(guilds or {}):
        entry = guilds[slug] or {}
        name = guild_name_for(entry, slug)
        for member in (entry.get("members") or []):
            keys = member_keys(member)
            if not keys or (keys & known):
                continue
            forms = member_forms(member)
            # Sorted canonical key, so the append order is stable across runs and diffable.
            candidates.append((slug, sorted(keys)[0], name, forms))

    rows = []
    for _slug, _key, name, forms in sorted(candidates, key=lambda c: (c[0], c[1])):
        keys = {resolve.norm_slug(f"{forms['name']} {forms['surname']}")}
        norm_key = resolve.member_norm_key({"character": forms})
        if norm_key:
            keys.add(norm_key)
        if keys & known:
            # Same character visible in a second guild (or a second entry in one roster):
            # one row per member, ever.
            continue
        rows.append(build_row(forms=forms, guild_name=name, new_id=allocator.allocate()))
        known |= keys
    return rows
