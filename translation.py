"""Display-name rendering for categories and features.

This module stores translation **keys** (e.g. ``category.electronics``); it
never owns a translation catalog. ``translate`` runs the key through the
``DISPLAY_TRANSLATOR`` seam (dotted-path, REPLACE) whose default is the
identity function — the key is returned unchanged. A host that wants
resolved names points the seam at its own backend without forking.

``cache_feature_translation`` is the port of legacy-catalog's
``categories/utils.py`` admin display cache (build the "``==1.2 - name
[type] (min=…​)``" label once, memoize per feature+language).
"""
from django.core.cache import cache
from django.utils.translation import get_language


def identity_translator(key):
    """Default ``DISPLAY_TRANSLATOR``: return the translation key unchanged."""
    return key


def translate(key):
    """Render a translation *key* for display via the configured seam."""
    from .conf import categories_settings

    if not key:
        return key
    return categories_settings.DISPLAY_TRANSLATOR(key)


def get_feature_key(feature):
    """Cache key for a feature's memoized admin display label."""
    return f"stapel_categories:feature-display:{feature.pk}:{get_language() or 'en'}"


def translate_feature(feature):
    """Return the cached admin display label for *feature*, building it lazily."""
    entry = cache.get(get_feature_key(feature))
    if entry is None:
        return cache_feature_translation(feature)
    return entry


def cache_feature_translation(feature):
    """Build and cache the admin display label for *feature*.

    Uses the polymorphic ``config`` for type information. Label shape:
    ``<depth-marker><id-path><*> - <caption> [<type>](<summary>)``.
    """
    from .conf import categories_settings

    caption = translate(feature.comment) or translate(feature.name)
    star = "*" if feature.mandatory else ""
    display_id = "=" * feature.ancestors_count + ".".join(
        str(k) for k in (feature.ancestors_pks + [feature.pk])
    )

    feature_type = feature.feature_type
    config_summary = _format_config_summary(feature.config)

    translation = f"{display_id}{star} - {caption} [{feature_type}]{config_summary}"

    cache.set(
        get_feature_key(feature),
        translation,
        timeout=categories_settings.FEATURE_DISPLAY_CACHE_TIMEOUT,
    )
    return translation


def _format_config_summary(config: dict) -> str:
    """Format config as a brief summary string for admin display."""
    if not config:
        return ""

    parts = []
    feature_type = config.get("type", "")

    if feature_type in ("int", "float"):
        if config.get("min") is not None:
            parts.append(f"min={config['min']}")
        if config.get("max") is not None:
            parts.append(f"max={config['max']}")

    elif feature_type == "select":
        options = config.get("options", [])
        if options:
            parts.append(f"{len(options)} options")
        if config.get("multiple"):
            parts.append("multiple")

    elif feature_type == "size_grid":
        grid_type = config.get("gridType")
        if grid_type:
            parts.append(grid_type)

    elif feature_type == "convertible_unit":
        unit_type = config.get("unitType")
        if unit_type:
            parts.append(unit_type)

    if parts:
        return f" ({', '.join(parts)})"
    return ""


__all__ = [
    "identity_translator",
    "translate",
    "get_feature_key",
    "translate_feature",
    "cache_feature_translation",
]
