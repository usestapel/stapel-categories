"""Category-level validators.

Value/config validation of individual features is owned by
stapel-attributes; this module holds the *tree-structural* rule that a
category may not carry two features sharing the same root (they are two
versions of the same inherited feature — the host must pick one), plus the
category-scoped *warnings* that only become answerable once the whole
resolved feature set is known.
"""
from django.core.exceptions import ValidationError


def validate_features(category):
    """Reject a category that has two features from the same root feature."""
    allowed_features = category.features.all() or []
    features_dict = {}
    for feature in allowed_features:
        if feature.root_pk in features_dict:
            raise ValidationError(
                "Multiple features with same root feature "
                f"[{feature.root.display_name}] detected, choose one of: "
                f"[{features_dict[feature.root_pk].display_name}], [{feature.display_name}]"
            )
        features_dict[feature.root_pk] = feature


def feature_warnings(category) -> list:
    """Non-blocking findings over a category's whole resolved feature set.

    A rule condition (or a ref-type's ``optionsRef.parentFeature``) naming a
    slug no feature in the category defines is deliberately NOT an error: the
    same feature is reused across categories with different field sets, where
    the slug simply reads as ``empty``. It is still almost always a typo, so
    it is reported here — the only place with the whole set in hand.

    Returns ``["<slug>: <finding>", ...]``, stable and de-duplicated. Never
    raises: a category with a broken config is reported by
    ``validate_configs_structured`` on the ``validate-configs`` endpoint,
    not smuggled out through the warning channel.
    """
    from stapel_attributes import validate_configs_structured

    defs = category.feature_defs()
    known_slugs = {d["slug"] for d in defs if d.get("slug")}
    found = []
    for result in validate_configs_structured(defs, known_slugs=known_slugs).results:
        for warning in result.warnings or []:
            entry = f"{result.slug}: {warning}"
            if entry not in found:
                found.append(entry)
    return found


__all__ = ["feature_warnings", "validate_features"]
