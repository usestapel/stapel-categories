from django.apps import AppConfig


class CategoriesConfig(AppConfig):
    name = "stapel_categories"
    label = "categories"
    verbose_name = "Category tree"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm functions/actions, system checks,
        # error-key registration. Keep each in its own module.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401
