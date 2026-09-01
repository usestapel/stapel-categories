"""Models for stapel-categories.

The category tree (django-treenode) plus a parallel ``Feature`` tree whose
``config`` JSONField is a polymorphic, typed-attribute config *validated by
stapel-attributes* — this module owns the tree structure, inheritance and
the M2M ordering; the attribute engine (types, config/DTO/DAO validation,
polymorphic serializers) lives in stapel-attributes and is imported, never
re-implemented.

House rules (docs/library-standard.md §3.8): revision tracking via
stapel-core ``RevisionMixin``; index names <= 30 chars. CDN icons are
decoupled — stored as plain string references/UIDs, no dependency on
stapel-cdn.

Provenance: ported from the legacy catalog's ``categories/models.py``. Fixed
while porting: the latent ``Category.Meta`` bug where a second ``class
Meta`` shadowed the first, silently dropping the ``revision`` index — the
two are now merged into one Meta.
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case, IntegerField, Q, When
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from treenode.models import TreeNodeModel

from stapel_core.comm import mutate_and_emit
from stapel_core.django.models import RevisionMixin

from .translation import cache_feature_translation, translate, translate_feature
from .validators import validate_features


#: Keys a ``hints`` entry may carry — the canon's ``$defs.Hint``.
HINT_KEYS = ("title", "content")


def _validate_hints(hints) -> None:
    """Reject anything that is not ``[{"title": str, "content": str}, ...]``."""
    if not isinstance(hints, list):
        raise ValidationError({"hints": _("Hints must be a list")})
    for index, hint in enumerate(hints):
        if not isinstance(hint, dict):
            raise ValidationError({"hints": _("Hint %(index)d must be an object") % {"index": index}})
        if set(hint) != set(HINT_KEYS):
            raise ValidationError(
                {"hints": _("Hint %(index)d must have exactly 'title' and 'content'") % {"index": index}}
            )
        for key in HINT_KEYS:
            if not isinstance(hint[key], str):
                raise ValidationError(
                    {"hints": _("Hint %(index)d '%(key)s' must be a string") % {"index": index, "key": key}}
                )


class Feature(RevisionMixin, TreeNodeModel):
    """Polymorphic feature with a typed ``config``.

    The ``config`` JSONField carries a ``type`` discriminator; its shape is
    validated by stapel-attributes' open type registry (int, float, string,
    bool, hex_color, select, header, … and any host-registered type). This
    model does not enumerate or validate types itself — it delegates.
    """

    treenode_display_field = "display_name"

    name = models.CharField(max_length=200)
    slug = models.CharField(max_length=100, default="", blank=True)
    # CDN icon reference / UID (e.g. "feature-icons/color"). Decoupled from
    # stapel-cdn: an opaque string, resolved by the host if at all.
    icon = models.CharField(max_length=255, blank=True, default="")
    comment = models.CharField(max_length=200, blank=True)

    # Polymorphic config — type-specific configuration with a 'type'
    # discriminator. Shape validated by stapel-attributes. UI fields
    # (prefix, postfix, postfix1000, placeholder) live inside config.
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Type-specific configuration. Must include 'type'. Validated by stapel-attributes.",
    )

    mandatory = models.BooleanField(default=False)
    show_as_badge = models.BooleanField(default=False)
    show_at_title = models.BooleanField(default=False)

    # Conditional rules over sibling values — a sibling of ``mandatory``, never
    # part of ``config``: a rule is type-independent, while ``config`` is parsed
    # by the per-type serializer. Grammar and evaluator live in
    # stapel-attributes (``rules.py``); this model only stores and validates.
    rules = models.JSONField(
        default=list,
        blank=True,
        help_text="Conditional rules (closed grammar). Validated by stapel-attributes.",
    )

    # Form metadata. Each value is a translation key or a literal, resolved the
    # same way ``name`` is. None of it ever reaches a stored listing value.
    description = models.TextField(
        blank=True, default="", help_text="Help text under the field; translation key or literal."
    )
    example = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Placeholder text; translation key or literal.",
    )
    default = models.JSONField(
        null=True, blank=True,
        help_text="Initial form value in DTO 'value' shape (for a select, a list of option codes).",
    )
    hints = models.JSONField(
        default=list, blank=True,
        help_text="Notices rendered with the field: [{\"title\": ..., \"content\": ...}].",
    )
    group = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Form section; sections order by first appearance.",
    )

    class TranslateMode(models.TextChoices):
        ALL = "all", "All (title + options)"
        TITLE = "title", "Title only"
        NONE = "none", "None"

    translate = models.CharField(
        max_length=10,
        choices=TranslateMode.choices,
        default=TranslateMode.ALL,
        help_text="What to translate: 'all' = title + options, 'title' = title only, 'none' = nothing",
    )

    # Marks a row as test/scratch data. Excluded from ``export_catalog`` (and,
    # transitively, from any CategoryFeature link touching it) so test data
    # never ships as a committed catalog fixture. Not a runtime-visibility gate
    # (see docs/catalog-fixtures-sync.md §5) — only export filters on it.
    is_test = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Test/scratch data — excluded from export_catalog fixtures.",
    )

    @property
    def display_name(self):
        return translate_feature(self)

    @property
    def feature_type(self) -> str:
        """Get the feature type from config."""
        return self.config.get("type", "string")

    def __str__(self):
        return self.display_name

    class Meta(TreeNodeModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(tn_parent__isnull=True) & ~Q(slug=""),
                name="categories_feature_root_slug_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["revision"], name="cat_feature_revision_idx"),
        ]

    def clean(self):
        """Validate the feature configuration and rules via stapel-attributes."""
        from stapel_attributes import validate_feature_config
        from stapel_attributes.rules import parse_rules

        if not self.config:
            self.config = {}

        if "type" not in self.config:
            raise ValidationError({"config": "Config must include 'type' field"})

        try:
            validate_feature_config(self.config)
        except (ValidationError, ValueError) as e:
            raise ValidationError({"config": str(e)})

        if self.rules is None:
            self.rules = []
        try:
            parse_rules(self.rules)
        except ValidationError as e:
            raise ValidationError({"rules": e.messages})

        if self.hints is None:
            self.hints = []
        _validate_hints(self.hints)

        # Slug rules
        slug = (self.slug or "").strip()
        self.slug = slug
        parent = getattr(self, "tn_parent", None)

        if parent:
            # Child must inherit slug and type
            parent_slug = (parent.slug or "").strip()
            if parent_slug:
                if slug and slug != parent_slug:
                    raise ValidationError({"slug": _("Child slug must match parent slug")})
                self.slug = parent_slug
            else:
                if slug:
                    raise ValidationError(
                        {"slug": _("Parent slug is empty; child slug must be empty or match parent")}
                    )
                self.slug = parent_slug

            parent_type = parent.config.get("type") if parent.config else None
            child_type = self.config.get("type") if isinstance(self.config, dict) else None
            if parent_type and child_type and parent_type != child_type:
                raise ValidationError({"config": _("Child config.type must match parent config.type")})
        else:
            # Root feature: slug required and unique among roots
            if not slug:
                raise ValidationError({"slug": _("Slug is required for root features")})
            exists = (
                Feature.objects.filter(tn_parent__isnull=True, slug=slug)
                .exclude(pk=self.pk)
                .exists()
            )
            if exists:
                raise ValidationError({"slug": _("Slug must be unique among root features")})

    def save(self, *args, **kwargs):
        # The feature write and the category.changed fanout emitted by the
        # post_save receiver (emit_category_changed_on_feature_save) commit
        # as ONE transaction — a feature edit is never committed without its
        # cache-invalidation events, nor vice versa (outbox atomicity).
        with mutate_and_emit():
            super().save(*args, **kwargs)
        cache_feature_translation(self)

    def get_config_with_defaults(self) -> dict:
        """Full config with defaults from the feature type (via attributes)."""
        from stapel_attributes import get_feature_type

        try:
            feature_type = get_feature_type(self.feature_type)
            defaults = feature_type.get_default_config()
            return {**defaults, **self.config}
        except ValueError:
            return self.config


class Category(RevisionMixin, TreeNodeModel):
    """Category tree node with an ordered M2M to :class:`Feature`.

    Features define the characteristics settable for listings in this
    category. Categories inherit features from ancestors through the tree.
    Supports revision-based synchronization via ``RevisionMixin``.
    """

    treenode_display_field = "slug"
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=100, unique=True, db_index=True)
    # Identifier this category carries in the source it was imported from
    # (e.g. a source tree node id). Opaque, and NOT unique on its own — two
    # source catalogues may hand out the same id — so the importer's identity
    # is the PAIR (external_source, external_id), see ``external_source``.
    #
    # It is not the fixture's addressing key either: the files still address
    # categories by ``slug`` (`parent_slug` edges, sidecar keys). It IS the
    # re-import identity: ``load_catalog`` matches a fixture row carrying an
    # external identity against the live row with the same identity BEFORE it
    # falls back to the slug, so a source-side rename (which moves the
    # path-derived slug) updates the row in place instead of creating a
    # duplicate next to it.
    external_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="Identifier in the source catalogue this category was imported from.",
    )
    # Which catalogue ``external_id`` belongs to (e.g. "partner-feed"). Blank means
    # "the deployment's only import source" — the value every row written
    # before this field existed carries, and the value a fixture that omits
    # the key matches against, so a single-source catalog never has to set it.
    external_source = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Source catalogue `external_id` belongs to (blank = the single/default source).",
    )
    comment = models.CharField(
        max_length=255, blank=True, default="", help_text="Comment for translators"
    )
    draft = models.TextField(blank=True, default="")

    # CDN icon references (type/name or opaque UID). Decoupled from
    # stapel-cdn: opaque strings, no hard dependency.
    catalog_icon = models.CharField(
        max_length=255, blank=True, default="",
        help_text="CDN catalog icon reference (opaque string, e.g. catalog/asset-name)",
    )
    carousel_icon = models.CharField(
        max_length=255, blank=True, default="",
        help_text="CDN carousel icon reference (opaque string, e.g. carousel/asset-name)",
    )

    carousel_enabled = models.BooleanField(
        default=False, help_text="Whether this category appears in the carousel"
    )
    active = models.BooleanField(default=True, help_text="Whether this category is active")

    translatable = models.BooleanField(
        default=True, help_text="If True, category name is a translation key"
    )

    # Marks a row as test/scratch data. Excluded from ``export_catalog`` (and,
    # transitively, from every CategoryFeature link on it) so test data never
    # ships as a committed catalog fixture. Not a runtime-visibility gate
    # (see docs/catalog-fixtures-sync.md §5) — only export filters on it.
    is_test = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Test/scratch data — excluded from export_catalog fixtures.",
    )

    features = models.ManyToManyField(
        Feature,
        related_name="categories",
        through="CategoryFeature",
        through_fields=("category", "feature"),
        blank=True,
    )

    class Meta:
        # Merged Meta — the ported source had a SECOND ``class Meta`` that
        # shadowed the first, so the revision index was silently dropped.
        verbose_name_plural = "categories"
        indexes = [
            models.Index(fields=["revision"], name="cat_category_revision_idx"),
            # The re-import lookup key: load_catalog resolves a fixture row to
            # a live row by (external_source, external_id) before it tries the
            # slug. Not a UNIQUE constraint — a catalog may legitimately hold
            # two rows carrying the same source id (a hand-split node), and the
            # loader reports that ambiguity rather than letting the DB refuse
            # an unrelated write.
            models.Index(
                fields=["external_source", "external_id"],
                name="cat_category_extid_idx",
            ),
        ]

    def __str__(self):
        return translate(self.name)

    def save(self, *args, **kwargs):
        # The category write, the copy_parent_features side effects and the
        # category.changed event emitted by the post_save receiver commit as
        # ONE transaction — the invalidation event leaves iff the row
        # committed (outbox atomicity; a lost invalidation strands every
        # downstream categories.features cache).
        with mutate_and_emit():
            super().save(*args, **kwargs)

    def clean(self):
        if self.pk:
            validate_features(self)

    def get_all_features(self):
        """All features for this category, including inherited from ancestors.

        Returns a QuerySet ordered by this category's feature order first,
        then ancestors' (nearest ancestor first) — each feature *slug* appears
        once. Deduplication is by ``slug``, not by feature id: an ``inherit``
        override creates a *new* Feature row that shares the parent's slug, so
        the child category ends up linking its override while the ancestor
        still links the original. The version closest to this category wins
        (self beats ancestors, nearer ancestor beats farther), so the resolved
        schema, ``categories.features`` and the value-validation pipeline all
        see the effective override — making the docstring's "each feature slug
        appears once" true (H-1). Slug-less features (e.g. ``header`` rows) are
        never collapsed: they dedup by row id only.
        """
        ordered_ids = []
        seen_slugs = set()
        seen_ids = set()

        def append_from_category(cat):
            for link in cat.category_features.all().order_by("order", "id").select_related("feature"):
                feature = link.feature
                if feature is None:
                    continue
                slug = (feature.slug or "").strip()
                if slug:
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)
                elif feature.pk in seen_ids:
                    continue
                seen_ids.add(feature.pk)
                ordered_ids.append(feature.pk)

        append_from_category(self)
        # Ancestors nearest-first: tn_ancestors_pks is root-first, so reverse it.
        ancestors_by_pk = {str(a.pk): a for a in self.get_ancestors_queryset()}
        for anc_pk in reversed(self.get_ancestors_pks()):
            ancestor = ancestors_by_pk.get(str(anc_pk))
            if ancestor is not None:
                append_from_category(ancestor)

        if not ordered_ids:
            return Feature.objects.none()

        ordering = Case(
            *[When(pk=pk, then=pos) for pos, pk in enumerate(ordered_ids)],
            output_field=IntegerField(),
        )
        return Feature.objects.filter(pk__in=ordered_ids).order_by(ordering)

    def get_feature_schema(self) -> dict:
        """Complete feature schema for this category, keyed by feature ID."""
        schema = {}
        for feature in self.get_all_features():
            schema[str(feature.pk)] = {
                "name": feature.name,
                "slug": feature.slug,
                "mandatory": feature.mandatory,
                "showAsBadge": feature.show_as_badge,
                "showAtTitle": feature.show_at_title,
                "rules": feature.rules or [],
                "description": feature.description,
                "example": feature.example,
                "default": feature.default,
                "hints": feature.hints or [],
                "group": feature.group,
                "config": feature.get_config_with_defaults(),
            }
        return schema

    def feature_defs(self) -> list:
        """Resolved feature definitions for the value-validation pipeline.

        Returns a list of dicts consumable by stapel-attributes'
        ``coerce_feature_defs`` (a superset of ``FeatureDef``'s fields). This
        is what the ``categories.features`` comm Function serializes so
        consumers (stapel-listings) validate values without importing this
        module.

        ``show_at_title`` / ``show_as_badge`` / ``translate`` MUST cross the
        boundary: attributes' ``dto_to_dao`` reads them off the FeatureDef to
        build the title/badge projections — omitting them yields empty
        ``features_title`` / ``features_badges`` downstream. ``rules`` MUST
        cross it for the same reason: requiredness and visibility come from
        ``evaluate_rules``, so a dropped rule set silently reverts the whole
        category to static ``mandatory``. The form metadata crosses so a
        consumer renders help/placeholder/sections without a second call.
        """
        return [
            {
                "id": feature.pk,
                "slug": feature.slug,
                "name": feature.name,
                "mandatory": feature.mandatory,
                "show_at_title": feature.show_at_title,
                "show_as_badge": feature.show_as_badge,
                "translate": feature.translate,
                "rules": feature.rules or [],
                "description": feature.description,
                "example": feature.example,
                "default": feature.default,
                "hints": feature.hints or [],
                "group": feature.group,
                "config": feature.get_config_with_defaults(),
            }
            for feature in self.get_all_features()
        ]


class CategoryFeature(models.Model):
    """Through table storing feature order within a category."""

    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="category_features"
    )
    feature = models.ForeignKey(
        Feature, on_delete=models.CASCADE, related_name="feature_categories"
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = (("category", "feature"),)
        ordering = ["order", "id"]


@receiver(post_save, sender=Category)
def copy_parent_features(sender, instance, created, **kwargs):
    """When a new child category is created, copy the parent's features.

    Copies the parent's M2M feature relationships to the newly created
    child, preserving order.
    """
    if created and instance.tn_parent:
        parent_features = instance.tn_parent.category_features.all().order_by("order", "id")
        for parent_link in parent_features:
            CategoryFeature.objects.create(
                category=instance,
                feature=parent_link.feature,
                order=parent_link.order,
            )


@receiver(post_save, sender=Category)
def emit_category_changed_on_save(sender, instance, **kwargs):  # emit-check: ok — post_save receiver, called by Django's signal dispatcher, not by a call site
    """Emit ``category.changed`` so downstream caches (e.g. listings) invalidate."""
    from .events import publish_category_changed

    publish_category_changed(instance.pk, instance.revision)


@receiver(post_save, sender=Feature)
def emit_category_changed_on_feature_save(sender, instance, **kwargs):  # emit-check: ok — post_save receiver, called by Django's signal dispatcher, not by a call site
    """A feature edit changes every category referencing it — emit for each.

    Cost note: this is an N-fanout (N emits + N outbox rows) synchronous in the
    save's transaction, where N = categories directly referencing this feature.
    N is bounded by the M2M (``distinct`` guards against duplicate rows), but
    the inheritance model lets a shared/root feature sit on many categories, so
    N can be large for a widely-used feature. Over-emitting is safe
    (invalidation is idempotent); if the fanout ever hurts, batch it behind a
    single coalescing event rather than dropping invalidations.
    """
    from .events import publish_category_changed

    for cat in Category.objects.filter(features=instance).only("pk", "revision").distinct():
        publish_category_changed(cat.pk, cat.revision)
