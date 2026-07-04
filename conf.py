"""Settings namespace for stapel-categories.

All configuration is read through ``categories_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_CATEGORIES`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior.
"""
from stapel_core.conf import AppSettings

categories_settings = AppSettings(
    "STAPEL_CATEGORIES",
    defaults={
        # Seconds the ``categories/carousel`` response is cached in the
        # Django cache backend.
        "CAROUSEL_CACHE_TIMEOUT": 300,
        # Seconds an admin feature display-name translation is memoized.
        "FEATURE_DISPLAY_CACHE_TIMEOUT": 60,
        # Dotted path to a callable ``(key: str) -> str`` used to render a
        # translation key for admin/``__str__`` display (single strategy,
        # REPLACE semantics). Default is identity: this module stores
        # translation *keys* and does not resolve them — a host that wants
        # resolved names points this at its translation backend (e.g. a
        # wrapper over the ``translate.resolve`` comm Function).
        "DISPLAY_TRANSLATOR": "stapel_categories.translation.identity_translator",
    },
    import_strings=("DISPLAY_TRANSLATOR",),
)

__all__ = ["categories_settings"]
