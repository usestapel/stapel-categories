"""i18n error keys of stapel-categories.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_EXPECTED_LIST = "error.400.categories_expected_list"
ERR_400_DUPLICATE_SLUG = "error.400.categories_duplicate_slug"
ERR_400_DATABASE_ERROR = "error.400.categories_database_error"
ERR_400_CATEGORY_NOT_DELETED = "error.400.categories_not_deleted"
ERR_400_INVALID_CONVERSION = "error.400.categories_invalid_conversion"
ERR_400_CONFIG_REQUIRED = "error.400.categories_config_required"

STAPEL_CATEGORIES_ERRORS = {
    ERR_400_EXPECTED_LIST: "Expected a list of objects",
    ERR_400_DUPLICATE_SLUG: "A feature with slug '{slug}' already exists",
    ERR_400_DATABASE_ERROR: "Database error while applying changes",
    ERR_400_CATEGORY_NOT_DELETED: "Category is not deleted",
    ERR_400_INVALID_CONVERSION: "Invalid type conversion (only select<->string is supported)",
    ERR_400_CONFIG_REQUIRED: "A config object is required",
}

register_service_errors(STAPEL_CATEGORIES_ERRORS)

__all__ = [
    "STAPEL_CATEGORIES_ERRORS",
    "ERR_400_EXPECTED_LIST",
    "ERR_400_DUPLICATE_SLUG",
    "ERR_400_DATABASE_ERROR",
    "ERR_400_CATEGORY_NOT_DELETED",
    "ERR_400_INVALID_CONVERSION",
    "ERR_400_CONFIG_REQUIRED",
]
