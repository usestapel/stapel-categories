"""Children a category has without rows: expanded values and links.

Two ways a node's child list is more than "the rows whose parent I am", both
additive and both empty by default:

* :data:`Category.children_expand_by` — the node's children ARE the value set
  of one of its own features. A branch that would be four hundred rows named
  after four hundred option codes is one column instead, and the values keep
  one home. The reads answer with VIRTUAL children: a display name and the
  ``{feature: value}`` filter this node already answers, no id and no slug,
  nothing written.
* :class:`CategoryLink` — a pointer drawn among the children, leading to
  another node. Everything but the position belongs to the target.

Both are assembled here, in one place, so the tree endpoint and the children
listing cannot disagree about what a child list is.

**Where the values come from.** A closed option set (``select`` and anything
else whose resolved config carries ``options``) is read straight off the
feature's config — the same read :mod:`stapel_categories.effective` merges.
A referential type (``ref_select``/``ref_hierarchical_select``, whose config
carries an ``optionsRef``) keeps its terms in a vocabulary service, and the
resolver protocol stapel-attributes declares is four questions about ONE
code — it cannot list a level. So a referential expansion is valid, and it
yields values only when the registered resolver also offers the optional
reader :func:`resolver.terms(vocabulary, level)` (stapel-vocabularies' ORM
resolver is the intended provider). Without it the node answers with no
virtual children rather than with a guess.
"""
from .models import Category, CategoryLink

#: Config key holding an inline closed option set.
_OPTIONS_KEY = "options"
#: Config key pointing at one level of an external vocabulary.
_OPTIONS_REF_KEY = "optionsRef"


def _config(feature) -> dict:
    config = feature.get_config_with_defaults()
    return config if isinstance(config, dict) else {}


def _options_ref(config: dict):
    """``(vocabulary, level)`` of a referential config, or ``None``."""
    ref = config.get(_OPTIONS_REF_KEY)
    if not ref:
        return None
    if isinstance(ref, dict):
        vocabulary, level = ref.get("vocabulary"), ref.get("level")
    else:  # a hand-built config still carries the dataclass
        vocabulary = getattr(ref, "vocabulary", None)
        level = getattr(ref, "level", None)
    if not vocabulary:
        return None
    return str(vocabulary), str(level or "")


def _inline_options(config: dict) -> list:
    """``[(value, label)]`` of an inline option set — empty when there is none."""
    options = config.get(_OPTIONS_KEY)
    if not isinstance(options, list):
        return []
    pairs = []
    for option in options:
        if isinstance(option, dict):
            value = option.get("value")
            label = option.get("label") or value
        else:
            value = label = option
        if value in (None, ""):
            continue
        pairs.append((str(value), str(label)))
    return pairs


def _vocabulary_terms(vocabulary: str, level: str) -> list:
    """``[(code, label)]`` of a vocabulary level, through the optional reader.

    ``VocabularyResolver`` declares four questions about one code and no
    listing — deliberately, since listing belongs to the HTTP surface a
    typeahead talks to. A resolver MAY offer ``terms(vocabulary, level)``
    returning ``[(code, label)]``; one that does not simply yields nothing,
    and the node draws no virtual children.
    """
    try:
        from stapel_attributes.vocabularies import get_vocabulary_resolver
    except ImportError:  # pragma: no cover - attributes is a hard dependency
        return []
    resolver = get_vocabulary_resolver()
    reader = getattr(resolver, "terms", None)
    if reader is None:
        return []
    pairs = []
    for term in reader(vocabulary, level) or []:
        code, label = (term if isinstance(term, (tuple, list)) else (term, term))[:2]
        if code in (None, ""):
            continue
        pairs.append((str(code), str(label or code)))
    return pairs


def expansion_feature(category):
    """The feature ``children_expand_by`` names, or ``None``.

    Resolved over :meth:`Category.get_all_features` — own links first, then
    ancestors' — which is what this module already means by "the features
    this category has".
    """
    slug = (category.children_expand_by or "").strip()
    if not slug:
        return None
    for feature in category.get_all_features():
        if (feature.slug or "").strip() == slug:
            return feature
    return None


def expansion_values(category) -> list:
    """``[(value, label)]`` this category's children expand to."""
    feature = expansion_feature(category)
    if feature is None:
        return []
    config = _config(feature)
    inline = _inline_options(config)
    if inline:
        return inline
    ref = _options_ref(config)
    if ref is None:
        return []
    return _vocabulary_terms(*ref)


def virtual_children(category) -> list:
    """The virtual child rows of an expanded category, in the value order.

    A row is a display ``name``, the ``value`` it stands for (the option code
    — a stable key, which a translated name is not) and the ``filter`` pair
    this node already answers. No ``href``: the address is the storefront's
    own filter URL on this category, and this module does not know that
    routing. No ``id`` and no ``slug``, because there is no row.
    """
    slug = (category.children_expand_by or "").strip()
    if not slug:
        return []
    return [
        {
            "name": label,
            "value": value,
            "virtual": True,
            "filter": {slug: value},
        }
        for value, label in expansion_values(category)
    ]


def links_by_source(category_ids) -> dict:
    """``{source_id: [CategoryLink, ...]}`` for the given sources, in order.

    ONE query for a whole tree read. A link whose target a reader cannot see
    is dropped here rather than at render time: a pointer into a retired or
    soft-deleted branch is a dead tile, and the child list must only offer
    what the same reader can fetch.
    """
    ids = list(category_ids)
    if not ids:
        return {}
    from .views import visible_categories

    targets = CategoryLink.objects.filter(source_id__in=ids).values("target_id")
    visible = set(
        visible_categories().filter(pk__in=targets).values_list("pk", flat=True)
    )
    out: dict = {}
    rows = (
        CategoryLink.objects.filter(source_id__in=ids, target_id__in=visible)
        .select_related("target")
        .order_by("order", "id")
    )
    for link in rows:
        out.setdefault(link.source_id, []).append(link)
    return out


def insert_links(children: list, links, render) -> list:
    """Place each rendered link at its ``order`` index among *children*.

    ``order`` is a position, not a sort key alongside ``tn_priority``: the
    real children are already in the reader's own order and a link says where
    in that row it belongs. Applied in ascending order, clamped to the end,
    so the result is stable and a link nobody positioned lands first.
    """
    out = list(children)
    for link in sorted(links, key=lambda item: (item.order, item.pk)):
        index = min(int(link.order or 0), len(out))
        out.insert(index, render(link))
    return out


def link_name(link) -> str:
    """What a pointer is called — its own label, else the target's name."""
    return link.label or link.target.name


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: ``children_expand_by`` names a feature this category does not have, or one
#: whose values cannot be enumerated.
EXPAND_BY_UNUSABLE = "expand_by_unusable"
#: A category cannot both HAVE children and derive them.
EXPAND_BY_ON_BRANCH = "expand_by_on_branch"


def expansion_error(category):
    """``(code, message)`` refusing this category's expansion, or ``None``.

    Two refusals, and both are about a node that would answer nothing:

    * the named feature is not attached to this category, or its type has no
      closed value set to enumerate (a free-text field has no children);
    * the category has real children. Either a branch or an expansion, never
      both — two child lists on one node is a level nobody can address.
    """
    slug = (category.children_expand_by or "").strip()
    if not slug:
        return None
    if category.pk and Category.objects.filter(
        tn_parent_id=category.pk, deleted=False
    ).exists():
        return (
            EXPAND_BY_ON_BRANCH,
            f"category '{category.slug}' has real children, so it cannot "
            f"also expand them from feature '{slug}'",
        )
    if not category.pk:
        # A row being created has no feature links yet — copy_parent_features
        # runs on the first save — so the feature half is checked on the next
        # write rather than refusing a legitimate create.
        return None
    feature = expansion_feature(category)
    if feature is None:
        return (
            EXPAND_BY_UNUSABLE,
            f"category '{category.slug}' has no feature '{slug}' to expand "
            "its children from",
        )
    config = _config(feature)
    if not _inline_options(config) and _options_ref(config) is None:
        return (
            EXPAND_BY_UNUSABLE,
            f"feature '{slug}' has neither a closed option set nor a "
            f"referential type, so category '{category.slug}' cannot expand "
            "its children from it",
        )
    return None


def expansion_errors() -> list:
    """``(category, code, message)`` for every category that cannot expand."""
    out = []
    for category in Category.objects.filter(deleted=False).exclude(
        children_expand_by=""
    ):
        error = expansion_error(category)
        if error is not None:
            out.append((category, *error))
    return out
