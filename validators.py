"""Category-level validators.

Value/config validation of individual features is owned by
stapel-attributes; this module holds only the *tree-structural* rule that a
category may not carry two features sharing the same root (they are two
versions of the same inherited feature — the host must pick one).
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


__all__ = ["validate_features"]
