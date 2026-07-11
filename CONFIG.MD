# CONFIG.MD — stapel-categories

Config registry for **stapel-categories** (`static-scaffold-and-config.md` §2).
One row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_CATEGORIES` settings dict), what it is for,
whether it is required, and its default.

Every key is read through `categories_settings`
(`stapel_categories.conf.AppSettings`, namespace `STAPEL_CATEGORIES`).
Resolution order per key: `settings.STAPEL_CATEGORIES` dict → a flat Django
setting of the same name → environment variable → the default below.

stapel-categories hard-depends on **stapel-attributes** (the typed feature
engine it stores category schemas with); the scaffolder pulls attributes in
automatically as a headless pip dependency.

## stapel-categories

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| CAROUSEL_CACHE_TIMEOUT | env | seconds the `categories/carousel` response is cached. | no | 300 |
| FEATURE_DISPLAY_CACHE_TIMEOUT | env | seconds an admin feature display-name translation is memoized. | no | 60 |
| DISPLAY_TRANSLATOR | env | dotted path to a `(key) -> str` callable rendering a translation key for admin/`__str__` display (REPLACE). Default is identity. | no | stapel_categories.translation.identity_translator |
