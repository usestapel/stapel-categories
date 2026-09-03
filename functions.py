"""comm surface of stapel-categories.

Every Function/Action carries a JSON schema in ``schemas/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops. Other modules call by name, no import of this package needed:

    from stapel_core.comm import call

    call("categories.features", {"category_id": 42})
    # -> {"category_id": 42, "revision": 7, "features": [ {slug, config, ...} ]}

    call("categories.path", {"category_ids": [42]})
    # -> {"42": ["7", "19", "42"]}

    call("categories.suggest", {"terms": ["шорты", "shorty"], "limit": 50})
    # -> {"categories": [{id, slug, name, path, path_ids, depth, match}]}

    call("categories.names", {"ids": [42, "7"]})
    # -> {"names": {"42": {"name": ..., "slug": ...}, "7": {...}}}

    call("categories.children", {"parent_id": None})
    # -> {"parent_id": None, "children": [{id, slug, name, children_count}]}

``categories.features`` returns the *resolved* feature definitions for a
category (own + inherited, config merged with type defaults). stapel-listings
calls it to validate listing attribute values against the category schema
WITHOUT importing this module; the payload is cacheable by ``revision``.
Mutations emit ``category.changed`` (see events.py) for cache invalidation.

``categories.path`` answers root->leaf ancestry for a batch of categories.
This module owns the tree, so it is the only place that can answer it
without re-deriving the hierarchy from the outside; the canonical name was
declared by stapel-search before a provider existed
(``STAPEL_SEARCH["CATEGORY_PATH_FUNCTION"]``), and without an answer a
search index degrades to a single path segment — a filter on a parent
category finds none of its descendants.

``categories.names`` resolves a batch of ids to display names + slugs — the
question the other two skirt (``path`` answers id-paths, ``suggest`` answers
terms). stapel-search's goods-driven suggest rows carry bare path ids and
need captions; absence marks a deleted/unknown id, string keys survive JSON.

``categories.suggest`` matches category NAMES for a type-ahead. It is the
counterpart of ``categories.path`` for the other direction — text in, nodes
out — and it exists here rather than in the caller for the same reason:
names, ancestry and the retired/test/soft-deleted state of every node are
this module's, and a consumer re-deriving any of them from a projection is
the seam defect the comm surface exists to prevent. What it deliberately
does NOT own is the query language: terms arrive already folded and already
expanded (synonyms, transliteration) by whoever asked, because a second
normalizer here would be a second answer to "what did the user mean".

``categories.children`` answers one rung of the cascade — the children of a
node, null for the top — the way a person walks the storefront: same order
as the tree HTTP views, active rungs only, each child carrying its own
count of active children so a walker knows a leaf without a second call.
It exists for callers that walk the tree over comm rather than HTTP
(svc-agent descending the catalogue rung by rung), and the other three
Functions each answer an adjacent question but not this one: ``path`` goes
up, ``suggest`` goes by name, ``names`` captions ids the caller already has.
"""
import json
import unicodedata
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist
from stapel_core.comm import function

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"

# Bounded retries for the consistent (revision, features) snapshot read below.
_FEATURES_SNAPSHOT_RETRIES = 3

#: Hard ceiling on how many matching categories ``categories.suggest``
#: returns, whatever the caller asks for. A one-letter term matches most of
#: a wide catalogue, and the caller's next step is to count live listings per
#: candidate — so the cap is what keeps a typo from turning a type-ahead into
#: a catalogue dump.
_SUGGEST_MAX_RESULTS = 200

#: Cache key prefix of the folded name index, completed by a fingerprint
#: of the tree's revision state so a mutation retires the entry rather
#: than waiting out a TTL.
_SUGGEST_INDEX_CACHE_PREFIX = "stapel_categories:suggest-index:"

#: How a name matched a term, best first. The ORDER is the contract — the
#: caller ranks on it (``stapel_search.suggest``) and the index cap below
#: keeps by it, so a value's position in this tuple is behaviour, not
#: documentation.
SUGGEST_MATCH_KINDS: tuple[str, ...] = ("exact", "prefix", "word", "substring")

def _is_word_char(char: str) -> bool:
    """Whether *char* continues a word.

    Everything that is not alphanumeric — a space, a comma, a hyphen, a
    bracket — opens one, which is what makes «Брюки и шорты» a word-boundary
    hit for «шорты» and «Сифоны» only a mid-word one for «ифон».
    """
    return char.isalnum()


def match_kind(folded_name: str, terms: list[str]) -> str:
    """The BEST way any of *terms* matches *folded_name*.

    Four kinds, and the distinction between the last two is the one that
    was missing: transliterating «iphone» yields «ифон», which is a
    substring of «сифоны» and of nothing else in a 3583-node catalogue —
    so the single suggestion a buyer got for «iphone» was a plumbing trap.
    A word-boundary hit («Брюки и **шорты**») and a hit buried inside a
    word («С**ифон**ы») are not the same evidence and must not sort the
    same, and only the module that owns the names can tell them apart.

    ``exact`` is separate from ``prefix`` for the reason «шорты» was
    ranked third behind two «Брюки и шорты»: when nothing in the catalogue
    has any listings yet — the state a freshly imported board is in — count
    cannot break the tie and the node the buyer literally typed has to.
    """
    best = len(SUGGEST_MATCH_KINDS)
    for term in terms:
        if not term:
            continue
        position = folded_name.find(term)
        if position < 0:
            continue
        if folded_name == term:
            return "exact"
        if position == 0:
            rank = 1
        elif not _is_word_char(folded_name[position - 1]):
            rank = 2
        else:
            rank = 3
        best = min(best, rank)
    return SUGGEST_MATCH_KINDS[best] if best < len(SUGGEST_MATCH_KINDS) else "substring"


def _schema(name: str) -> dict:
    """Load a committed contract — one source of truth, no inline copy."""
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ── an id that is not an id ─────────────────────────────────────────────────
#
# ``Category.pk`` is an ``AutoField``. ``objects.get(pk="32/149/163")`` — a
# search PATH where an id belongs, which is what three drafts on one live
# stand carried — raises ``ValueError``, not ``DoesNotExist``, so it walked
# straight past the provider's own ``except`` and surfaced as an unhandled
# fault. Every caller in the fleet is written against the ``LookupError`` the
# docstrings here promise: stapel-listings' re-projection counts it as
# ``category_unresolved``, its publish path turns it into a 400.
#
# The payload schemas do type these ids as integers, but that only holds while
# ``VALIDATE_SCHEMAS`` is on, and a contract conditional on a runtime flag is
# not a contract. So the resolution goes through these two, and there are two
# rather than one because "not found" and "not a rung" are different answers.
#
# ``path``/``names`` already had a private form of this — a `.isdigit()` filter
# on the incoming list. Three treatments of one hazard is why two of the five
# providers did not have it at all.


def _resolve_category(queryset, category_id):
    """One row, or ``LookupError`` — for EVERY way an id can fail to be one."""
    try:
        return queryset.get(pk=category_id)
    except (ObjectDoesNotExist, ValueError, TypeError):
        raise LookupError(f"category {category_id} not found") from None


def _category_exists(queryset, category_id) -> bool:
    """Whether the id names a row. A malformed id names none; it is not a crash."""
    try:
        return queryset.filter(pk=category_id).exists()
    except (ValueError, TypeError):
        return False



@function("categories.features", schema=_schema("categories.features"))
def features_function(payload: dict) -> dict:
    """Resolve the feature schema for a category.

    Payload: ``{"category_id": <int>}``. Returns
    ``{"category_id": int, "revision": int, "features": [FeatureDef]}`` where
    each FeatureDef is ``{id, slug, name, mandatory, config}`` — ``config``
    is merged with its type's defaults. Raises ``LookupError`` (missing
    category) so callers can distinguish "no such category" from "no
    features".
    """
    from .models import Category

    category_id = payload["category_id"]
    category = _resolve_category(Category.objects, category_id)

    # M-6: revision and features must come from ONE snapshot. Under READ
    # COMMITTED the row read (revision) and feature_defs() (its own SELECTs)
    # are separate statements, so a concurrent apply committing between them
    # yields a torn pair — e.g. an old revision with new features, which a
    # consumer would then cache forever under the stale revision. Read the
    # revision on both sides of feature_defs() and retry until it is stable,
    # so the returned (revision, features) pair is internally consistent.
    for _ in range(_FEATURES_SNAPSHOT_RETRIES):
        revision_before = (
            Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        )
        features = category.feature_defs()
        revision_after = (
            Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        )
        if revision_before == revision_after:
            break
        category.refresh_from_db()
    else:
        # Never converged (constant churn) — return the last consistent read of
        # the revision paired with those features; refresh once more so at least
        # revision_after describes the same read.
        revision_after = (
            Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        )

    return {
        "category_id": category.pk,
        "revision": revision_after,
        "features": features,
    }


@function("categories.path", schema=_schema("categories.path"))
def path_function(payload: dict) -> dict:
    """Root->leaf ancestry for a batch of categories.

    Payload: ``{"category_ids": [<id>, ...]}``. Returns
    ``{"<id>": ["<root_id>", ..., "<id>"]}`` — one flat mapping, ids as
    strings on both sides so a JSON round trip cannot change the key type.
    An id with no row is simply absent (the ``projections.read()``
    convention), which is what lets a consumer tell "no such category" from
    "a root category" — the latter answers a one-element path.

    Segments are IDS, not slugs. The consumer (stapel-search) feeds the last
    segment of a requested path straight back into ``categories.features``,
    whose payload is typed as an integer id; slugs would silently fail that
    call and take the facet plan down with it.

    One query, no tree walk: django-treenode denormalizes the ancestry into
    ``tn_ancestors_pks``, so this is a read of a column the tree already
    maintains rather than a second hierarchy of our own.
    """
    from treenode.utils import split_pks

    from .models import Category

    wanted = {str(value) for value in (payload.get("category_ids") or [])}
    numeric = [value for value in wanted if value.lstrip("-").isdigit()]
    if not numeric:
        return {}
    rows = Category.objects.filter(pk__in=numeric).values_list(
        "pk", "tn_ancestors_pks"
    )
    return {
        str(pk): [*split_pks(ancestors), str(pk)] for pk, ancestors in rows
    }


@function("categories.names", schema=_schema("categories.names"))
def names_function(payload: dict) -> dict:
    """Display names for a batch of category ids.

    Payload: ``{"ids": [163, "149", ...]}`` (ints or strings — the ids
    usually arrive out of a JSON document that already stringified them).
    Returns ``{"names": {"<id>": {"name": ..., "slug": ...}}}`` — keys are
    ids AS STRINGS on the way out too, so a round trip through JSON cannot
    change the key type (the ``categories.path`` rule).

    Exists because the other two Functions answer adjacent questions and
    neither answers this one: ``categories.path`` maps ids to id-paths (the
    caller still holds ids), ``categories.suggest`` matches TERMS (text in,
    nodes out). A consumer holding bare path ids — stapel-search's
    goods-driven suggest rows — had no fleet way to caption them without
    re-deriving names from a projection, which is the seam defect the comm
    surface exists to prevent.

    A deleted row and an unknown id are simply absent (the ``projections.
    read()`` convention: the caller tells "gone" from "root" by absence, and
    a stale id in an old document degrades to no caption, not to an error).
    An INACTIVE row still answers — a listing can sit in a category that was
    retired after publication, and its suggest row still needs a caption.
    Names go through :func:`stapel_categories.translation.translate` (the
    ``DISPLAY_TRANSLATOR`` seam), exactly as ``categories.suggest`` renders
    them — this module stores translation keys, and handing a consumer the
    raw key would put "categories.electronics" in a dropdown.

    The batch is capped by the schema (``maxItems: 200``, validated at the
    call boundary like ``categories.path``'s 1000): a suggest page needs
    tens of captions, and an uncapped batch is a catalogue dump through the
    caption endpoint.
    """
    from .models import Category
    from .translation import translate

    wanted = {str(value) for value in (payload.get("ids") or [])}
    numeric = [value for value in wanted if value.lstrip("-").isdigit()]
    if not numeric:
        return {"names": {}}
    rows = Category.objects.filter(pk__in=numeric, deleted=False).values_list(
        "pk", "name", "slug"
    )
    return {
        "names": {
            str(pk): {"name": translate(name), "slug": slug}
            for pk, name, slug in rows
        }
    }


@function("categories.children", schema=_schema("categories.children"))
def children_function(payload: dict) -> dict:
    """One rung of the category cascade, as the storefront shows it.

    Payload: ``{"parent_id": <int|null>}`` — null (or absent) means the
    active roots. Returns ``{"parent_id": <int|null>, "children": [{id,
    slug, name, children_count}]}`` where ``children_count`` counts the
    child's own ACTIVE children — 0 means leaf, so a walker descending the
    tree rung by rung (svc-agent walking the catalogue the way a buyer
    walks the cascade) knows the bottom without a second call.

    A ``parent_id`` that names no walkable node — unknown, inactive or
    soft-deleted — raises ``LookupError``, the ``categories.features``
    convention: the caller must be able to tell "no such rung" from "a leaf",
    and a leaf is the empty list, never an error.

    Ordering is the tree HTTP views' ordering (``-tn_priority``, then
    ``id`` — ``children``/``roots`` in views.py), so the agent sees the
    rungs in the order a person sees them. Visibility is DELIBERATELY
    narrower than ``visible_categories()`` there: the HTTP reads keep
    inactive rows (a client greys them out on the ``active`` flag the
    serializer ships), but this Function's rows carry no such flag and its
    caller is choosing where to step next — an inactive category is not a
    place the storefront lets a buyer go, so here it is not a rung at all,
    on the row, in the counts, and as a parent. ``is_test`` is not filtered,
    for the reason ``visible_categories()`` states: it is an export filter,
    and a deployment that wants test rows off the storefront retires them
    with ``active``.

    Names go through the ``DISPLAY_TRANSLATOR`` seam exactly as
    ``categories.names`` and ``categories.suggest`` render theirs — this
    module stores translation keys, and a raw key would hand the walker
    «categories.electronics» as a rung caption.

    Two queries flat (parent check + one annotated read), whatever the
    width of the rung.
    """
    from django.db.models import Count, Q

    from .models import Category
    from .translation import translate

    parent_id = payload.get("parent_id")
    walkable = Category.objects.filter(active=True, deleted=False)
    if parent_id is None:
        queryset = walkable.filter(tn_parent__isnull=True)
    else:
        if not _category_exists(walkable, parent_id):
            raise LookupError(f"category {parent_id} not found")
        queryset = walkable.filter(tn_parent_id=parent_id)

    rows = (
        queryset.annotate(
            active_children=Count(
                "tn_children",
                filter=Q(tn_children__active=True, tn_children__deleted=False),
            )
        )
        .order_by("-tn_priority", "id")
        .values_list("pk", "slug", "name", "active_children")
    )
    return {
        "parent_id": parent_id,
        "children": [
            {
                "id": pk,
                "slug": slug,
                "name": translate(name),
                "children_count": count,
            }
            for pk, slug, name, count in rows
        ],
    }


def fold(value: str) -> str:
    """A name (or a query term) in this Function's wire normal form.

    Case-folded, ``ё`` merged into ``е``, Latin diacritics dropped, Cyrillic
    diacritics kept — NFD decomposes ``й`` into ``и`` + breve, and dropping
    that breve merges two different letters, so «мой» would fold into «мои»
    and a Russian catalogue would quietly lose a distinction its readers
    rely on.

    This is the same normal form ``stapel_search.text.fold`` produces, and
    it is restated here rather than imported for the reason the comm surface
    exists at all: the two modules do not import each other and may not even
    run in the same process. The normal form is therefore part of the
    CONTRACT — ``schemas/functions/categories.suggest.json`` says terms
    arrive folded — and a contract that only one end can execute is a
    contract only one end can honour. The caller still owns everything
    *above* folding (synonyms, transliteration): those are query-language
    decisions and there is exactly one of them, in the search module.

    Not done in SQL. ``LOWER()`` is ASCII-only on SQLite, so a database
    case function answers «Шорты» to a Postgres deployment and nothing to a
    SQLite one — the class of divergence that makes a test suite agree with
    a stand that is wrong.
    """
    if not value:
        return ""
    lowered = unicodedata.normalize("NFC", value.casefold()).replace("ё", "е")
    kept: list[str] = []
    base = ""
    for char in unicodedata.normalize("NFD", lowered):
        if unicodedata.combining(char):
            if "Ѐ" <= base <= "ӿ":
                kept.append(char)
            continue
        base = char
        kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept))


def _suggest_index() -> dict:
    """Every category as ``{id: {slug, name, folded, ancestors, hidden}}``.

    Built from ONE read of the tree and cached under a fingerprint of the
    tree's own revision state, so the common case is a single cheap
    aggregate and no fetch at all. The fingerprint is ``(max revision,
    row count)``: every mutation goes through ``RevisionMixin`` and bumps a
    revision, and the count catches the one thing a maximum cannot — a row
    disappearing.

    Whole-tree rather than a per-term query because the match has to happen
    in Python (see :func:`fold`), and because the ancestry and the ancestor
    NAMES are needed for every hit anyway. A category tree is a bounded
    object — hundreds here, ~3k for a full imported marketplace catalogue — and
    reading it whole once per revision is cheaper than the three round trips
    a per-term query would still need to render one row.
    """
    from django.core.cache import cache
    from django.db.models import Count, Max
    from treenode.utils import split_pks

    from .conf import categories_settings
    from .models import Category

    fingerprint = Category.objects.aggregate(
        revision=Max("revision"), rows=Count("pk")
    )
    key = (
        f"{_SUGGEST_INDEX_CACHE_PREFIX}"
        f"{fingerprint['revision'] or 0}:{fingerprint['rows'] or 0}"
    )
    cached = cache.get(key)
    if cached is not None:
        return cached

    index: dict[str, dict] = {}
    for row in Category.objects.values(
        "pk", "slug", "name", "tn_ancestors_pks", "active", "is_test", "deleted"
    ):
        index[str(row["pk"])] = {
            "slug": row["slug"],
            "name": row["name"],
            "folded": fold(row["name"]),
            "ancestors": split_pks(row["tn_ancestors_pks"]),
            "hidden": not row["active"] or row["is_test"] or row["deleted"],
        }
    cache.set(key, index, categories_settings.SUGGEST_INDEX_CACHE_TIMEOUT)
    return index


@function("categories.suggest", schema=_schema("categories.suggest"))
def suggest_function(payload: dict) -> dict:
    """Categories whose NAME matches one of *terms*, with their ancestry.

    Payload: ``{"terms": ["шорты", "shorty"], "limit": 50}``. Returns
    ``{"categories": [{id, slug, name, path, path_ids, depth, match}]}``,
    where ``path`` is display names root->leaf and ``path_ids`` the same
    ancestry as ids. Both travel together because a dropdown row needs the
    first to render and the second to navigate, and deriving one from the
    other outside this module means a second call and a second chance to
    disagree with the tree.

    Terms are OR-ed and matched against the folded name. HOW they matched
    is reported per row as one of :data:`SUGGEST_MATCH_KINDS` — ``exact``,
    ``prefix``, ``word`` (the term starts a word inside the name) or
    ``substring`` (it starts mid-word) — and the caller ranks on it
    together with the live listing count, which only the caller has. The
    kind is graded here rather than there because grading it needs the
    stored name, and the caller is handed terms and rows, never names.

    **Visibility is inherited.** A category is excluded when it is
    ``active=False``, ``is_test`` or soft-deleted — and also when any
    ANCESTOR is, because a live leaf hanging under a retired branch is not
    reachable in the catalogue, and offering it navigates a buyer into a
    page that is not there.

    At most two queries, whatever the size of the answer, and one of them
    only on a cold cache — see :func:`_suggest_index`.
    """
    from .translation import translate

    terms = [fold(str(term)) for term in (payload.get("terms") or [])]
    terms = [term for term in terms if term]
    if not terms:
        return {"categories": []}

    try:
        limit = int(payload.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, _SUGGEST_MAX_RESULTS))

    index = _suggest_index()

    hits: list[tuple[int, int, int, str, str]] = []
    for pk, entry in index.items():
        if entry["hidden"]:
            continue
        folded = entry["folded"]
        if not any(term in folded for term in terms):
            continue
        if any(index.get(ancestor, {}).get("hidden") for ancestor in entry["ancestors"]):
            continue
        kind = match_kind(folded, terms)
        # Deterministic under the cap, and the cap keeps the BEST matches:
        # match kind first, then shallower, then by id. Keeping the
        # shallowest alone meant a deep exact hit could be dropped before the
        # caller — which does the ranking — ever saw it, and a cap over an
        # unordered scan hands back a different subset on every call for the
        # same word.
        hits.append(
            (SUGGEST_MATCH_KINDS.index(kind), len(entry["ancestors"]), int(pk), kind, pk)
        )

    out = []
    for _, _, _, kind, pk in sorted(hits)[:limit]:
        entry = index[pk]
        path_ids = [*entry["ancestors"], pk]
        out.append(
            {
                "id": int(pk),
                "slug": entry["slug"],
                "name": translate(entry["name"]),
                # An ancestor missing from the index cannot happen for a
                # consistent tree; if it ever does, the id is a truthful
                # segment and an empty string would silently shorten the path.
                "path": [
                    translate(index.get(segment, {}).get("name", segment))
                    for segment in path_ids
                ],
                "path_ids": path_ids,
                "depth": len(path_ids),
                "match": kind,
            }
        )
    return {"categories": out}
