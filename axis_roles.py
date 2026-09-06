"""Which feature of a leaf is the MAKE, the MODEL, the GENERATION.

``stapel_attributes.axis`` owns the vocabulary — ``make``, ``model``,
``generation``, ``year``, ``mileage`` — and :attr:`Feature.axis_role` records
one catalogue's answer. This module is the part that guesses it, once, at
import time, from the only evidence a loader has: the slug.

Why guess at all. A storefront's «Найти больше вариантов этой марки» needs
the make feature of the leaf a listing sits in, and an AI descent has to fill
make before model before generation because each narrows the next. Until
``axis_role`` existed, every such consumer kept its own closed table of slugs
— ``{brand, make, make_ref_select, vendor}`` in one storefront — and the
catalogues this fleet imports spell the axis at least five ways
(``Brand``/``Make``/``Vendor``/``Manufacturer``, and the ``_ref_select``
suffix a vocabulary-backed field carries). A fourth spelling arriving in a
new import dropped out of the feature with nothing red anywhere. Moving the
table HERE does not make it a better table; it makes it one table, in the
module that owns the catalogue, whose answer is published with the schema and
can be overridden per feature instead of forked per consumer.

**The rule table is data in this file, deliberately** — the same call
``derive_children_as`` makes about its name vocabulary. It is a fact about
the catalogues this fleet imports, not about the model, and putting it in
``stapel-attributes`` would make every deployment of an L1 library inherit
one market's words.

**The suffix rule.** A vocabulary-backed field is spelled ``make_ref_select``
in one catalogue and ``make`` in the next — the 2026-09-05 import renamed
five features across exactly that seam. So a trailing ``_ref_select`` (or
``_select``) is stripped before matching, and the table lists base words
only. That is what keeps the fourth spelling from being the one nobody added.

**Ambiguity derives nothing.** A category that offers two candidates for one
role — ``brand`` AND ``vendor`` — is a schema the reader cannot resolve:
whichever the derivation picked, a «more of this make» link built off the
other one sends a buyer to a facet they did not click. So neither is stamped,
in that category or in any other (the row is shared), and the pair is
reported as a warning by ``load_catalog`` and by ``catalog_health``. Silence
is the honest answer; a guess is the bug this whole field exists to end.

**Derivation never writes the authored column.** ``axis_role`` is what a
fixture, the admin or ``set_axis_role`` said; ``axis_role_derived`` is this
module's cache, and :attr:`Feature.resolved_axis_role` reads authored first.
One column could not hold both without the next run refusing to touch its own
output.
"""
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from stapel_attributes.axis import GENERATION, MAKE, MILEAGE, MODEL, YEAR

#: Slug → axis role. Base words only: the suffixes in :data:`_REF_SUFFIXES`
#: are stripped before a lookup, so ``make_ref_select`` matches ``make``.
#: Case-folded on both sides.
#:
#: Read as a claim about catalogues, not about language: ``brand``, ``make``,
#: ``vendor`` and ``manufacturer`` are four spellings of one axis in the
#: import corpus of a large classified catalogue this fleet mapped, and
#: ``god_vypuska``/``kilometrage`` are the transliterated and English
#: spellings a Russian-language catalogue arrives with.
AXIS_ROLE_BY_SLUG: Dict[str, str] = {
    # make — the manufacturer axis
    "brand": MAKE,
    "make": MAKE,
    "manufacturer": MAKE,
    "vendor": MAKE,
    # model — narrowed by the make
    "model": MODEL,
    # generation — narrowed by the model
    "generation": GENERATION,
    # year of manufacture
    "year": YEAR,
    "god_vypuska": YEAR,
    # odometer reading
    "mileage": MILEAGE,
    "kilometrage": MILEAGE,
}

#: Stripped from the end of a slug before the table lookup. A vocabulary-
#: backed field carries the type in its name in several live catalogues
#: (``make_ref_select``, ``model_ref_select``), and that is a fact about how
#: the field is BACKED, not about which axis it is.
_REF_SUFFIXES = ("_ref_select", "_select")


def role_for_slug(slug: Optional[str]) -> Optional[str]:
    """The axis role :data:`AXIS_ROLE_BY_SLUG` gives this slug, or ``None``.

    Case-folded, with a vocabulary-backed suffix stripped. A slug the table
    does not know answers ``None`` — this function never guesses beyond the
    table, because a near-miss ("brand_new") stamped as a make is worse than
    an unstamped feature.
    """
    folded = (slug or "").strip().lower()
    if not folded:
        return None
    for suffix in _REF_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            folded = folded[: -len(suffix)]
            break
    return AXIS_ROLE_BY_SLUG.get(folded)


class Ambiguity(tuple):
    """One category offering two or more candidates for one role.

    A named 3-tuple ``(category_slug, role, slugs)`` — a tuple so a report can
    sort and compare it without importing anything.
    """

    __slots__ = ()

    def __new__(cls, category: str, role: str, slugs: Tuple[str, ...]):
        return super().__new__(cls, (category, role, tuple(sorted(slugs))))

    @property
    def category(self) -> str:
        return self[0]

    @property
    def role(self) -> str:
        return self[1]

    @property
    def slugs(self) -> Tuple[str, ...]:
        return self[2]

    def __str__(self) -> str:
        return f"{self.category}: {self.role} ← {', '.join(self.slugs)}"


def _candidate_categories():
    """Live categories that carry at least one candidate feature.

    Not every category: the ambiguity question is only ever about a category
    holding two features from the table, so the scan starts from the links
    that could produce one. A catalogue of 2900 leaves with cars in a corner
    of it walks the corner.
    """
    from .models import Category, Feature

    candidate_ids = [
        pk
        for pk, slug in Feature.objects.filter(deleted=False, is_test=False).values_list(
            "pk", "slug"
        )
        if role_for_slug(slug)
    ]
    if not candidate_ids:
        return []
    return list(
        Category.objects.filter(
            deleted=False,
            is_test=False,
            category_features__feature_id__in=candidate_ids,
        ).distinct()
    )


def find_ambiguities() -> List[Ambiguity]:
    """Every ``(category, role, slugs)`` a derivation must refuse to answer.

    Resolved with the library's own inheritance
    (``Category.get_all_features``: own + inherited, override-aware, deduped
    by slug), so this can never disagree with the schema the product renders
    — the same rule ``dead_end_leaves`` follows and for the same reason.
    """
    found: List[Ambiguity] = []
    for category in _candidate_categories():
        by_role: Dict[str, set] = defaultdict(set)
        for feature in category.get_all_features():
            role = role_for_slug(feature.slug)
            if role:
                by_role[role].add(feature.slug)
        for role, slugs in by_role.items():
            if len(slugs) > 1:
                found.append(Ambiguity(category.slug, role, tuple(slugs)))
    return sorted(found)


def derive_axis_roles(apply: bool = False) -> Tuple[Dict[str, str], List[Ambiguity]]:
    """Decide ``axis_role_derived`` for every live feature.

    Returns ``({slug: role}, ambiguities)``. With ``apply`` the decisions are
    written — by a queryset ``update()``, so no revision bumps and no
    ``category.changed`` storm follow a re-derivation; the same choice
    ``derive_children_as --apply`` makes for its own cache.

    A slug caught in ANY ambiguity is derived nowhere: the Feature row is
    shared across every category that links it, so a role that is wrong in
    one leaf cannot be right on the row.
    """
    from .models import Feature

    ambiguities = find_ambiguities()
    blocked = {slug for amb in ambiguities for slug in amb.slugs}

    decided: Dict[str, str] = {}
    rows = Feature.objects.filter(deleted=False, is_test=False).values_list(
        "pk", "slug", "axis_role_derived"
    )
    writes: Dict[str, List[int]] = defaultdict(list)
    for pk, slug, current in rows:
        role = role_for_slug(slug)
        if role and slug not in blocked:
            decided[slug] = role
        else:
            role = ""
        if current != role:
            writes[role].append(pk)

    if apply:
        for role, pks in writes.items():
            Feature.objects.filter(pk__in=pks).update(axis_role_derived=role)

    return decided, ambiguities
