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

Provenance: ported from legacy-catalog ``categories/models.py``. Fixed
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
        """Validate the feature configuration via stapel-attributes."""
        from stapel_attributes import validate_feature_config

        if not self.config:
            self.config = {}

        if "type" not in self.config:
            raise ValidationError({"config": "Config must include 'type' field"})

        try:
            validate_feature_config(self.config)
        except (ValidationError, ValueError) as e:
            raise ValidationError({"config": str(e)})

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
        ``features_title`` / ``features_badges`` downstream.
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
def emit_category_changed_on_save(sender, instance, **kwargs):
    """Emit ``category.changed`` so downstream caches (e.g. listings) invalidate."""
    from .events import publish_category_changed

    publish_category_changed(instance.pk, instance.revision)


@receiver(post_save, sender=Feature)
def emit_category_changed_on_feature_save(sender, instance, **kwargs):
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
