"""Django system checks for stapel-categories configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily (a broken
*unused* dotted path must not block deploys).
"""
from django.core import checks

#: A category whose ``children_expand_by`` cannot produce children.
EXPAND_BY_ID = "stapel_categories.E001"


@checks.register(checks.Tags.database)
def check_children_expand_by(app_configs, databases=None, **kwargs):
    """Every ``children_expand_by`` names a feature that can be enumerated.

    W-level, not E: the catalogue still serves. A node whose expansion is
    broken answers with an empty child list — a page with nothing on it, in
    one corner of the tree — and refusing to start the whole service over one
    mis-typed slug in a three-thousand-row catalogue trades an empty page for
    an outage.

    Registered under ``Tags.database`` because it reads the table. The tag is
    not the guard: the registry runs every check and passes ``databases=``;
    ``None`` means no database is expected (plain ``manage.py check``, a
    composite's boot-gate test) and the check must not read. The
    ``DatabaseError`` guard covers the other case: a database is declared
    but the column does not exist yet because this runs before ``migrate``.
    """
    from django.db import Error as DatabaseError

    from .branching import expansion_errors

    if not databases:
        return []

    try:
        found = expansion_errors()
    except DatabaseError:
        # No table, or no column yet (this check runs before `migrate`).
        return []
    return [
        checks.Warning(
            f"{message} — the category will answer with no children.",
            hint=(
                "Attach the feature to the category, give it a closed option "
                "set or a referential type, or clear `children_expand_by`."
            ),
            obj=category,
            id=EXPAND_BY_ID,
        )
        for category, _code, message in found
    ]
