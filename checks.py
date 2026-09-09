"""Django system checks for stapel-categories configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily (a broken
*unused* dotted path must not block deploys).
"""
from django.core import checks

#: A category whose ``children_expand_by`` cannot produce children.
EXPAND_BY_ID = "stapel_categories.E001"


@checks.register(checks.Tags.database)
def check_children_expand_by(app_configs, **kwargs):
    """Every ``children_expand_by`` names a feature that can be enumerated.

    W-level, not E: the catalogue still serves. A node whose expansion is
    broken answers with an empty child list — a page with nothing on it, in
    one corner of the tree — and refusing to start the whole service over one
    mis-typed slug in a three-thousand-row catalogue trades an empty page for
    an outage.

    Registered under ``Tags.database`` because it reads the table: the check
    framework runs database-tagged checks only where a database is expected,
    and the read is guarded anyway — this runs before ``migrate`` too, when
    the column it filters on does not exist yet.
    """
    from django.db import Error as DatabaseError

    from .branching import expansion_errors

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
