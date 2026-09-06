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

**Precedence decides most contests; only a tie is an ambiguity.** A leaf
offering two candidates for one role is not automatically unresolvable. A
large live catalogue puts a general ``brand`` («Бренд одежды/Производитель»)
on 82 leaves that ALSO carry the catalogue's own ``make_ref_select``, and
there is nothing undecidable about that pair: the canonical ``make*``
spelling is the axis, and ``brand`` is a second, weaker word for it. So the
table carries a TIER per base word, and the strongest tier present in a
category wins:

``make`` > ``make_ref_select`` > ``vendor`` / ``manufacturer`` > ``brand``

— i.e. the base word's tier first, and within one base word the bare
spelling ahead of the vocabulary-backed one (``model`` > ``model_ref_select``
for the same reason). Only a tie WITHIN one tier (``vendor`` AND
``manufacturer``) is a real ambiguity: the reader has no basis to pick, and
whichever the derivation chose, a «more of this make» link built off the
other one sends a buyer to a facet they did not click. A tie stamps NOBODY
in that category and is reported as a warning by ``load_catalog`` and by
``catalog_health``. Silence is the honest answer; a guess is the bug this
whole field exists to end.

**Resolution is per ROW, because that is what a reader reads.** The winner
is decided per category (``by_axis_role`` in stapel-attributes drops a role
two features claim, so stamping both would give those 82 leaves nothing at
all), and the decision lands on the FEATURE ROW that won. A catalogue that
carries a per-category override row for ``brand`` therefore keeps ``brand``
as the make on every leaf where it stands alone, and yields to
``make_ref_select`` on the leaves where both appear. Where one SHARED row
would have to be a make in one category and not-a-make in another, one
column cannot hold both answers: the row is left blank and the category
where it lost is reported as an ambiguity, exactly as before.

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

#: Precedence TIER of each base word within its own role — lower wins. It is
#: read only when one category offers several candidates for one role, and it
#: says which spelling that catalogue means: the canonical ``make`` first, the
#: generic manufacturer words next, and ``brand`` last, because ``brand`` is
#: the word a catalogue also hangs on clothing and appliances beside a real
#: make. A base word absent here sits in the last tier by default, so adding a
#: spelling to :data:`AXIS_ROLE_BY_SLUG` alone can never promote it over the
#: canonical one by accident.
_LAST_TIER = 9
AXIS_ROLE_TIER_BY_SLUG: Dict[str, int] = {
    "make": 0,
    "vendor": 1,
    "manufacturer": 1,
    "brand": 2,
    "model": 0,
    "generation": 0,
    "year": 0,
    "god_vypuska": 1,
    "mileage": 0,
    "kilometrage": 1,
}

#: Stripped from the end of a slug before the table lookup. A vocabulary-
#: backed field carries the type in its name in several live catalogues
#: (``make_ref_select``, ``model_ref_select``), and that is a fact about how
#: the field is BACKED, not about which axis it is.
_REF_SUFFIXES = ("_ref_select", "_select")


def _base_word(slug: Optional[str]) -> Tuple[str, bool]:
    """``(base word, carried a vocabulary suffix)`` — case-folded."""
    folded = (slug or "").strip().lower()
    if not folded:
        return "", False
    for suffix in _REF_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            return folded[: -len(suffix)], True
    return folded, False


def role_for_slug(slug: Optional[str]) -> Optional[str]:
    """The axis role :data:`AXIS_ROLE_BY_SLUG` gives this slug, or ``None``.

    Case-folded, with a vocabulary-backed suffix stripped. A slug the table
    does not know answers ``None`` — this function never guesses beyond the
    table, because a near-miss ("brand_new") stamped as a make is worse than
    an unstamped feature.
    """
    base, _ = _base_word(slug)
    if not base:
        return None
    return AXIS_ROLE_BY_SLUG.get(base)


def precedence_for_slug(slug: Optional[str]) -> Optional[Tuple[int, int]]:
    """How strongly this slug claims its role — lower wins, ``None`` if it
    claims nothing.

    A sort key of ``(tier of the base word, 1 if it carried a vocabulary
    suffix)``, which spells the documented order out in one comparison::

        make (0, 0) < make_ref_select (0, 1) < vendor (1, 0)
                    < manufacturer (1, 0) < brand (2, 0)

    Two slugs with the SAME key are the only real ambiguity: nothing in the
    catalogue says which of them the axis is.
    """
    base, suffixed = _base_word(slug)
    if not base or base not in AXIS_ROLE_BY_SLUG:
        return None
    return (AXIS_ROLE_TIER_BY_SLUG.get(base, _LAST_TIER), 1 if suffixed else 0)


class Ambiguity(tuple):
    """One category whose claim on a role the derivation could not settle.

    Either a TIE — two candidates of the same precedence tier — or a category
    whose contest precedence DID settle onto a row that another category needs
    to answer differently (one shared row, two answers). Both read the same way
    to an operator: this category names no axis until you pin one with
    ``set_axis_role`` or drop the duplicate spelling.

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


def _resolve_category(category):
    """What ONE category's schema says about its axes.

    Returns ``(wants, ties, beaten)``:

    * ``wants`` — ``{feature pk: role or None}`` over every candidate this
      category carries: the winner of each role, and an explicit ``None`` for
      every candidate that lost or was tied out. ``None`` is a decision, not
      an absence: this category is saying "that row is not my make".
    * ``ties`` — the roles two same-tier candidates claimed.
    * ``beaten`` — ``{pk: Ambiguity}`` for a row precedence pushed out, kept so
      the contest can be reported IF the row turns out to be shared with a
      category that needs the other answer.

    Resolved with the library's own inheritance
    (``Category.get_all_features``: own + inherited, override-aware, deduped
    by slug), so this can never disagree with the schema the product renders —
    the same rule ``dead_end_leaves`` follows and for the same reason.
    """
    by_role: Dict[str, List[Tuple[Tuple[int, int], str, int]]] = defaultdict(list)
    for feature in category.get_all_features():
        role = role_for_slug(feature.slug)
        if role:
            by_role[role].append((precedence_for_slug(feature.slug), feature.slug, feature.pk))

    wants: Dict[int, Optional[str]] = {}
    ties: List[Ambiguity] = []
    beaten: Dict[int, Ambiguity] = {}
    for role, candidates in by_role.items():
        best = min(key for key, _, _ in candidates)
        top = {slug for key, slug, _ in candidates if key == best}
        contest = Ambiguity(category.slug, role, tuple(slug for _, slug, _ in candidates))
        if len(top) > 1:
            # A tie inside one tier: nothing in the catalogue says which one.
            ties.append(Ambiguity(category.slug, role, tuple(top)))
            for _, _, pk in candidates:
                wants[pk] = None
            continue
        for key, _, pk in candidates:
            if key == best:
                wants[pk] = role
            else:
                wants[pk] = None
                beaten[pk] = contest
    return wants, ties, beaten


def resolve_axis_roles() -> Tuple[Dict[int, Optional[str]], List[Ambiguity]]:
    """``({feature pk: role or None}, ambiguities)`` over every linked feature.

    A pk absent from the map was never a candidate in any category — the
    caller falls back to the slug alone for it.

    Two categories may want opposite things of ONE row (``brand`` is the make
    of a clothing leaf and loses to ``make_ref_select`` on a car leaf). The
    column cannot hold both, so the row is blanked and the category where the
    row lost is reported: precedence resolves what a per-category override row
    makes resolvable and never invents an answer where it does not.
    """
    wanted: Dict[int, set] = defaultdict(set)
    ambiguities: List[Ambiguity] = []
    beaten_by_pk: Dict[int, List[Ambiguity]] = defaultdict(list)
    for category in _candidate_categories():
        wants, ties, beaten = _resolve_category(category)
        ambiguities.extend(ties)
        for pk, role in wants.items():
            wanted[pk].add(role)
        for pk, contest in beaten.items():
            beaten_by_pk[pk].append(contest)

    resolved: Dict[int, Optional[str]] = {}
    for pk, roles in wanted.items():
        if len(roles) == 1:
            resolved[pk] = next(iter(roles))
        else:
            resolved[pk] = None
            ambiguities.extend(beaten_by_pk.get(pk, ()))
    return resolved, sorted(set(ambiguities))


def find_ambiguities() -> List[Ambiguity]:
    """Every ``(category, role, slugs)`` a derivation must refuse to answer."""
    return resolve_axis_roles()[1]


def derive_axis_roles(apply: bool = False) -> Tuple[Dict[str, str], List[Ambiguity]]:
    """Decide ``axis_role_derived`` for every live feature.

    Returns ``({slug: role}, ambiguities)`` — the slug map is the report's, and
    says which spellings this catalogue derives SOMEWHERE, not that every row
    under the slug carries the role (a per-category override row that lost its
    leaf's contest does not). With ``apply`` the decisions are written — by a
    queryset ``update()``, so no revision bumps and no ``category.changed``
    storm follow a re-derivation; the same choice ``derive_children_as
    --apply`` makes for its own cache.
    """
    from .models import Feature

    resolved, ambiguities = resolve_axis_roles()

    decided: Dict[str, str] = {}
    rows = Feature.objects.filter(deleted=False, is_test=False).values_list(
        "pk", "slug", "axis_role_derived"
    )
    writes: Dict[str, List[int]] = defaultdict(list)
    for pk, slug, current in rows:
        # A row no category links is decided by its slug alone: there is no
        # schema it could contradict.
        role = resolved[pk] if pk in resolved else role_for_slug(slug)
        role = role or ""
        if role:
            decided[slug] = role
        if current != role:
            writes[role].append(pk)

    if apply:
        for role, pks in writes.items():
            Feature.objects.filter(pk__in=pks).update(axis_role_derived=role)

    return decided, ambiguities
