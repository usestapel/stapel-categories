"""The effective feature schema of a ``chips`` parent.

A ``chips`` parent is a partition of ONE attribute template: its children ask
the same questions of a listing and differ by the value their own name spells
(`Новые`/`С пробегом`, `Куплю`/`Продам`/`Сдам`/`Сниму`). The storefront draws
the parent's own feed page with a chip row, so the parent must answer with a
schema — and until now it answered with its OWN links, which on such a node
are empty: the feed rendered no filters and the composer opened no fields
until a chip was picked.

The rule this module implements: a chips parent that declares no features of
its own has an effective schema equal to the INTERSECTION of its children's
effective schemas — the features present in every child, with the config they
agree on. Where the configs differ the bounds widen (the lower bound is the
lowest, the upper bound the highest, an options list is the union) and the
feature is marked ``divergent`` so a client may hide it until a chip is
picked rather than showing a control that means something different per chip.
A feature only SOME children carry is not in the parent's schema at all: it
appears when its chip is chosen.

Two deliberate limits:

* the intersection is keyed by ``slug``, which is what the whole module
  already means by "the same feature" (``get_all_features`` dedups by slug,
  and an ``inherit`` override is a new row sharing one). Slug-less rows (a
  ``header``) name nothing across children and are left out.
* a parent that declares features of its OWN keeps them, alone — "own only",
  never own + intersection. The two would be a third schema nobody authored,
  and a parent with own links has already had the decision made by hand.

A ``transparent`` node (0.20.4) takes the same rule for the same reason. It
has no page of its own — browsing skips it — but it is still ASKED for a
schema, by a composer walking through it and by any caller of
``categories.features``, and a wrapper's own links are empty by construction.
So a transparent node with no own features answers with its children's
intersection too. That is the whole of the overlap: it draws no chip row, gets
no axis caption, and one authored feature on it makes it "own only" like any
other node.
"""
from .models import (
    CHILDREN_AS_CHIPS,
    CHILDREN_AS_TRANSPARENT,
    Category,
    feature_def_dict,
)

#: The schema is the parent's own (own + inherited) — every node but one of
#: :data:`SCHEMA_FROM_CHILDREN` that declares nothing itself.
EFFECTIVE_FROM_OWN = "own"
#: The schema was intersected from the children.
EFFECTIVE_FROM_CHILDREN = "children"

#: Resolved ``children_as`` values whose node answers with its children's
#: schema rather than its own emptiness. ``chips`` because the parent renders
#: the whole partition's feed (0.20.1); ``transparent`` because the node has
#: no page of its own at all — a composer or a facet plan that walks THROUGH
#: it must not be handed the empty schema of a wrapper nobody browses. For
#: schema purposes ONLY: nothing else about a transparent node behaves like a
#: chip row (no axis caption is written for it, and it draws no chip row).
SCHEMA_FROM_CHILDREN = (CHILDREN_AS_CHIPS, CHILDREN_AS_TRANSPARENT)

#: Config keys whose widest value is the LOWEST one. A key absent from any
#: child's config is unbounded there, and unbounded is wider than any number,
#: so the merged config drops it.
_LOWER_BOUND_KEYS = ("min", "minLength", "minSelected", "minDate", "minDepth")
#: Config keys whose widest value is the HIGHEST one; absent = unbounded.
_UPPER_BOUND_KEYS = ("max", "maxLength", "maxSelected", "maxDate", "maxDepth")
#: Keys that PERMIT something when true — wider is true.
_PERMISSIVE_WHEN_TRUE = ("allowCustom", "allowFuture", "allowPast")
#: Keys that FORBID something when true — wider is false.
_PERMISSIVE_WHEN_FALSE = ("lockUserInput", "lockInput")
#: Keys holding a list of choices — wider is the union.
_OPTION_KEYS = ("options",)


def _option_key(option):
    """Identity of one option inside an ``options`` list."""
    if isinstance(option, dict):
        if "value" in option:
            return ("value", repr(option["value"]))
        return ("obj", repr(sorted(option.items(), key=lambda kv: kv[0])))
    return ("raw", repr(option))


def _union_options(values):
    """Union of several options lists, first appearance order kept."""
    merged = []
    seen = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for option in value:
            key = _option_key(option)
            if key in seen:
                continue
            seen.add(key)
            merged.append(option)
    return merged


def _numeric(values):
    """The values, or None when any of them is missing or not a number."""
    out = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        out.append(value)
    return out


def merge_configs(configs: list[dict]) -> tuple[dict, bool]:
    """Widest config the children agree to, and whether they disagreed.

    Compared and merged on the DEFAULTS-RESOLVED config, not the stored one:
    two children spelling the same shape differently (one omitting
    ``allowCustom``, one writing its default) are not a divergence, and a
    client reading the merged config gets the same keys it gets from a leaf.
    """
    first = configs[0]
    if all(config == first for config in configs[1:]):
        return dict(first), False

    merged = dict(first)
    keys = set()
    for config in configs:
        keys.update(config)

    for key in keys:
        values = [config.get(key) for config in configs]
        if all(value == values[0] for value in values[1:]) and all(
            key in config for config in configs
        ):
            continue
        if key in _OPTION_KEYS:
            merged[key] = _union_options(values)
        elif key in _LOWER_BOUND_KEYS or key in _UPPER_BOUND_KEYS:
            numbers = _numeric(values)
            if numbers is None:
                merged.pop(key, None)  # unbounded somewhere = unbounded here
            else:
                merged[key] = min(numbers) if key in _LOWER_BOUND_KEYS else max(numbers)
        elif key in _PERMISSIVE_WHEN_TRUE:
            merged[key] = any(bool(value) for value in values)
        elif key in _PERMISSIVE_WHEN_FALSE:
            merged[key] = all(bool(value) for value in values)
        elif key in first:
            merged[key] = first[key]
        else:
            # Only some children declare it and it is not a bound we can
            # widen — the reference child says nothing, so neither do we.
            merged.pop(key, None)
    return merged, True


def _partition_children(category):
    """The children an effective schema is intersected from.

    The same visibility rule and the same order as ``GET /children/`` — the
    chip row a storefront draws IS that list, so the schema must be the
    intersection of exactly what the reader can pick.
    """
    from .views import visible_categories

    return list(
        visible_categories()
        .filter(tn_parent=category)
        .order_by("-tn_priority", "id")
    )


def effective_source(category) -> str:
    """Where this category's schema comes from — one row read plus one count.

    ``EFFECTIVE_FROM_CHILDREN`` only for a node in
    :data:`SCHEMA_FROM_CHILDREN` with no own links: an authored own schema
    wins outright ("own only", see the module docstring).
    """
    if category.resolved_children_as not in SCHEMA_FROM_CHILDREN:
        return EFFECTIVE_FROM_OWN
    if category.category_features.exists():
        return EFFECTIVE_FROM_OWN
    return EFFECTIVE_FROM_CHILDREN


def effective_features(category) -> tuple[list, str]:
    """Feature rows for a category's effective schema, and where they came from.

    Returns ``(features, source)``. On the children path the rows are the
    reference child's own :class:`~stapel_categories.models.Feature` instances
    with two IN-MEMORY overlays — ``config`` widened to what every child
    accepts and ``mandatory`` true only where every child requires it — plus a
    ``divergent`` attribute. They are never saved; nothing here writes.

    Order is the one the module already applies (``get_all_features`` on the
    reference child: the category's own feature order first, then ancestors'),
    restricted to the intersection. A composer that orders required-bearing
    blocks first reads the same list it reads for a leaf.
    """
    source = effective_source(category)
    if source == EFFECTIVE_FROM_OWN:
        return list(category.get_all_features()), source

    children = _partition_children(category)
    if not children:
        # A node whose children are all retired presents nothing to
        # intersect; its own (inherited) schema is the honest answer.
        return list(category.get_all_features()), EFFECTIVE_FROM_OWN

    per_child = []
    for child in children:
        by_slug = {}
        for feature in child.get_all_features():
            slug = (feature.slug or "").strip()
            if slug:
                by_slug.setdefault(slug, feature)
        per_child.append(by_slug)

    reference, others = per_child[0], per_child[1:]
    common = [
        slug for slug in reference if all(slug in sibling for sibling in others)
    ]

    features = []
    for slug in common:
        variants = [by_slug[slug] for by_slug in per_child]
        configs = [variant.get_config_with_defaults() for variant in variants]
        merged, config_diverged = merge_configs(configs)

        feature = variants[0]
        mandatory = [bool(variant.mandatory) for variant in variants]
        rules = [variant.rules or [] for variant in variants]

        feature.config = merged
        feature.mandatory = all(mandatory)
        feature.divergent = (
            config_diverged
            or any(value != mandatory[0] for value in mandatory[1:])
            or any(value != rules[0] for value in rules[1:])
        )
        features.append(feature)
    return features, EFFECTIVE_FROM_CHILDREN


def effective_feature_defs(category) -> tuple[list[dict], str]:
    """:meth:`Category.feature_defs` over the effective schema.

    Same dicts ``feature_defs`` builds, so a consumer feeds them to
    ``coerce_feature_defs`` without knowing which node answered, plus
    ``divergent: true`` on a feature whose children disagree.
    """
    features, source = effective_features(category)
    defs = []
    for feature in features:
        item = feature_def_dict(feature)
        if getattr(feature, "divergent", False):
            item["divergent"] = True
        defs.append(item)
    return defs, source


def effective_revision(category, source: str) -> int:
    """The revision the effective schema is cacheable by.

    On the own path it is the category's own — unchanged. On the children
    path the schema is a fact about the CHILDREN, and a child's edit bumps
    the child's revision and not the parent's, so a consumer caching on the
    parent's number alone would hold a stale intersection forever. The
    number reported is the max over the parent and the rows it intersected —
    the same "revision names the state" fingerprint the roots and tree caches
    use, widened to the rows this read actually touched.
    """
    from django.db.models import Max, Q

    if source == EFFECTIVE_FROM_OWN:
        return Category.objects.values_list("revision", flat=True).get(pk=category.pk)
    aggregate = Category.objects.filter(
        Q(pk=category.pk) | Q(tn_parent=category)
    ).aggregate(revision=Max("revision"))
    return aggregate["revision"] or 0
