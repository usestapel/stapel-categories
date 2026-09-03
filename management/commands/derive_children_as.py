"""``derive_children_as`` — decide, per parent, whether its children are
subcategories or a partition of one attribute template.

Why a command and not a property: the decision is a judgement about a whole
catalogue, it is wrong on a minority of nodes whatever the rule, and an
operator has to be able to read it, disagree with it and pin the answer. So
the run PRINTS every parent it looked at with the signal that fired and the
number behind it, writes nothing without ``--apply``, and writes only into
``children_as_derived`` — the authored ``children_as`` column is never
touched, on any run, for any node.

The two signals are independent on purpose, and are reported apart:

``schema``
    The children's OWN feature key sets. A partition of one template is a set
    of children asking the same questions; a shelf of real subcategories is
    not. Pairwise Jaccard over the slug sets, floored at
    :data:`JACCARD_THRESHOLD`.

``vocabulary``
    The children's NAMES. Some partitions are invisible to the schema signal
    because the split is a value the catalogue never modelled as an attribute
    (buy/sell/rent, new/used, boys/girls). Matched against the child SET —
    at least two of the children have to fall in the same vocabulary group,
    and no child may fall outside it — never against a single name, because
    one child called "Новые" beside twenty real subcategories is a
    subcategory that happens to be called that.

The vocabulary is data in this file, deliberately: it is a fact about the
catalogues this fleet imports, not about the model, and putting it in the
model would make every deployment inherit one market's words.

Nothing here reads the source catalogue's own identifiers. A derivation keyed
on an importer's node ids would be a rule about one supplier that silently
did nothing for the next.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from ...models import (
    CHILDREN_AS_AUTO,
    CHILDREN_AS_CHIPS,
    CHILDREN_AS_TILES,
    Category,
)

#: Pairwise overlap at or above which two children are "the same template".
#: 0.5 is the spec's floor and it is not a tuning knob: it is the point at
#: which the children share more keys than they differ by.
JACCARD_THRESHOLD = 0.5

#: Name groups that spell a partition. Each entry is a set of folded name
#: fragments; a parent's children qualify when at least two of them match
#: fragments from ONE group and none of them matches nothing.
PARTITION_VOCABULARY: dict[str, tuple[str, ...]] = {
    "transaction": ("куплю", "продам", "сдам", "сниму", "аренда", "продажа"),
    "condition": ("новые", "новый", "новая", "с пробегом", "б/у", "бу", "подержанные"),
    "childrens-gender": ("для мальчиков", "для девочек"),
    "adult-gender": ("мужская", "женская", "мужские", "женские", "мужской", "женский"),
}

#: How a decision was reached, printed in the report's SIGNAL column.
SIGNAL_STRUCTURE = "structure"
SIGNAL_SCHEMA = "schema"
SIGNAL_EMPTY_SCHEMA = "empty-schema"
SIGNAL_VOCABULARY = "vocabulary"
SIGNAL_NONE = "none"


def fold_name(name: str) -> str:
    """Lowercased, whitespace-collapsed — the form the vocabulary matches."""
    return " ".join((name or "").lower().split())


def jaccard(left: set[str], right: set[str]) -> float:
    """|A n B| / |A u B|; two empty sets are identical, not undefined."""
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def vocabulary_group(children_names: list[str]) -> str | None:
    """The partition group the child SET falls in, or ``None``.

    A group qualifies when every child matches one of its fragments and at
    least two children match — "every" is what makes this a statement about
    the set rather than about a name, and the reason a shelf of real
    subcategories with one "Новые" among them is not a partition.
    """
    folded = [fold_name(name) for name in children_names]
    if len(folded) < 2:
        return None
    for group, fragments in PARTITION_VOCABULARY.items():
        if all(any(fragment in name for fragment in fragments) for name in folded):
            return group
    return None


def own_feature_slugs(category_id: int, links: dict[int, set[str]]) -> set[str]:
    """The feature keys a category carries in its OWN right.

    Read from a prefetched map rather than the row, so a whole catalogue
    costs one query for every category's links instead of one per parent.
    """
    return links.get(category_id, set())


def derive(
    parent,
    children,
    links,
    branch_pks: set[int] | None = None,
    schema_signal: bool = True,
) -> tuple[str, str, float | None, str | None]:
    """``(decision, signal, overlap, group)`` for one parent.

    Order matters and is the spec's: structure first (a child with children
    of its own is a branch, and a branch is never a chip), then the schema
    signal, then names. A parent that satisfies neither signal is ``tiles``
    — the answer that costs a click rather than hiding a branch.

    *branch_pks* is the set of category ids that have children WITHIN THE RUN
    (deleted rows excluded), which is not the same as ``tn_children_count``:
    a child whose only child is soft-deleted is a leaf as far as any reader
    is concerned, and counting it as a branch would keep a real partition on
    tiles forever.

    With *schema_signal* off the feature comparison is skipped entirely and
    the run stands on names alone — see :meth:`Command.handle`.
    """
    branch_pks = branch_pks or set()
    if any(child.pk in branch_pks for child in children):
        return CHILDREN_AS_TILES, SIGNAL_STRUCTURE, None, None

    if not schema_signal:
        group = vocabulary_group([child.name for child in children])
        if group is not None:
            return CHILDREN_AS_CHIPS, SIGNAL_VOCABULARY, None, group
        return CHILDREN_AS_TILES, SIGNAL_NONE, None, None

    sets = [own_feature_slugs(child.pk, links) for child in children]

    if not any(sets) and not own_feature_slugs(parent.pk, links):
        # Nothing anywhere to tell these children apart by: the catalogue has
        # not modelled a schema at this node at all, so the children are not
        # DIVERGING in one — they are a bare split of the parent's page.
        group = vocabulary_group([child.name for child in children])
        return CHILDREN_AS_CHIPS, SIGNAL_EMPTY_SCHEMA, None, group

    overlap = min(
        (
            jaccard(sets[i], sets[j])
            for i in range(len(sets))
            for j in range(i + 1, len(sets))
        ),
        default=1.0,
    )
    group = vocabulary_group([child.name for child in children])

    if overlap >= JACCARD_THRESHOLD:
        return CHILDREN_AS_CHIPS, SIGNAL_SCHEMA, overlap, group
    if group is not None:
        # The names say partition where the schemas do not. Kept as its own
        # signal rather than folded into the number above: an operator
        # reading the report needs to know WHICH evidence carried a node,
        # because these two are wrong about different things.
        return CHILDREN_AS_CHIPS, SIGNAL_VOCABULARY, overlap, group
    return CHILDREN_AS_TILES, SIGNAL_NONE, overlap, None


class Command(BaseCommand):
    help = (
        "Derive `children_as` for categories left on `auto`. Dry run by "
        "default; `--apply` writes the derived values."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the derived values. Without it the run only reports.",
        )
        parser.add_argument(
            "--root",
            type=str,
            default="",
            help="Restrict the run to one subtree, named by the root's slug.",
        )
        parser.add_argument(
            "--only-changed",
            action="store_true",
            help="Report only the parents whose derived value would change.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        root_slug = options["root"]
        only_changed = options["only_changed"]

        categories = Category.objects.filter(deleted=False)
        if root_slug:
            try:
                root = Category.objects.get(slug=root_slug, deleted=False)
            except Category.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"No category with slug {root_slug!r}"))
                return
            categories = categories.filter(
                pk__in=[root.pk, *root.get_descendants_pks()]
            )

        rows = list(categories.order_by("tn_level", "-tn_priority", "id"))
        by_pk = {row.pk: row for row in rows}
        children_by_parent: dict[int, list] = {}
        for row in rows:
            if row.tn_parent_id is not None:
                children_by_parent.setdefault(row.tn_parent_id, []).append(row)

        branch_pks = set(children_by_parent)
        schema_signal = self._schema_signal_available()
        if not schema_signal:
            self.stdout.write(
                self.style.WARNING(
                    "stapel-attributes is not importable — running on the "
                    "name vocabulary alone. Every decision below rests on "
                    "child names; re-run with the attribute engine installed "
                    "for the schema signal."
                )
            )
        links = self._feature_links([row.pk for row in rows]) if schema_signal else {}

        report: list[tuple] = []
        writes: list[tuple[int, str]] = []
        for row in rows:
            children = children_by_parent.get(row.pk)
            if not children:
                continue
            decision, signal, overlap, group = derive(
                row, children, links, branch_pks, schema_signal
            )
            authored = row.children_as != CHILDREN_AS_AUTO
            changed = not authored and row.children_as_derived != decision
            if only_changed and not changed:
                continue
            report.append(
                (self._path(row, by_pk), decision, signal, overlap, group, authored)
            )
            if changed:
                writes.append((row.pk, decision))

        self._print_report(report)

        if not writes:
            self.stdout.write("Nothing to write.")
            return

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — {len(writes)} row(s) would change. "
                    "Re-run with --apply to write them."
                )
            )
            return

        with transaction.atomic():
            for pk, decision in writes:
                # A targeted UPDATE, not `instance.save()`: this column is a
                # derivation cache, and putting a catalogue-wide re-derivation
                # through the revision bump + `category.changed` fanout would
                # invalidate every downstream feature cache in the fleet to
                # record a presentation hint. The `auto` guard is repeated in
                # SQL so a value authored between the read and this write
                # still wins.
                Category.objects.filter(
                    pk=pk, children_as=CHILDREN_AS_AUTO
                ).update(children_as_derived=decision)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(writes)} row(s)."))

    @staticmethod
    def _schema_signal_available() -> bool:
        """Whether the schema signal can be trusted on this install.

        A category's own feature LINKS are rows in this module's own through
        table, but what those links mean — a feature's type, its config, the
        registry that says two keys are the same question — is
        stapel-attributes, and it is not guaranteed present: a deployment
        that stores a bare tree without the attribute engine is a supported
        shape of this library. Rather than compare key sets whose meaning
        nothing on the box can vouch for, the run drops to the name
        vocabulary and says so in its first line.
        """
        from importlib.util import find_spec

        return find_spec("stapel_attributes") is not None

    @staticmethod
    def _feature_links(category_pks: list[int]) -> dict[int, set[str]]:
        """``{category_pk: {feature slug, ...}}`` in one query."""
        from ...models import CategoryFeature

        links: dict[int, set[str]] = {}
        rows = CategoryFeature.objects.filter(
            category_id__in=category_pks
        ).values_list("category_id", "feature__slug")
        for category_id, slug in rows:
            if slug:
                links.setdefault(category_id, set()).add(slug)
        return links

    @staticmethod
    def _path(row, by_pk: dict) -> str:
        """Slug path root->self, the form an operator can act on."""
        segments = []
        for pk in row.get_ancestors_pks():
            ancestor = by_pk.get(int(pk))
            segments.append(ancestor.slug if ancestor else str(pk))
        segments.append(row.slug)
        return "/".join(segments)

    def _print_report(self, report: list[tuple]) -> None:
        if not report:
            self.stdout.write("No parents matched.")
            return
        header = f"{'DECISION':<9} {'SIGNAL':<13} {'OVERLAP':>7}  {'GROUP':<16} PATH"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for path, decision, signal, overlap, group, authored in report:
            overlap_text = "-" if overlap is None else f"{overlap:.2f}"
            suffix = "  [authored — not written]" if authored else ""
            line = (
                f"{decision:<9} {signal:<13} {overlap_text:>7}  "
                f"{(group or '-'):<16} {path}{suffix}"
            )
            self.stdout.write(line)
        self.stdout.write("")
