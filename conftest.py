def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.staticfiles",
                "django.contrib.admin",
                "django.contrib.messages",
                "stapel_core.django.users",
                "rest_framework",
                # treenode (django-treenode) registers the tree-cache signals
                # its AppConfig.ready() wires up — required for tn_* fields.
                "treenode",
                # stapel_attributes is an L1 library (no Django app); imported,
                # not installed. Categories depends on its type registry.
                "stapel_categories",
            ],
            AUTH_USER_MODEL="users.User",
            STATIC_URL="/static/",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            USE_TZ=True,
            ROOT_URLCONF="stapel_categories.tests.urls",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                },
            },
            # Synchronous in-process comm with schema validation ON, so the
            # committed contracts in schemas/ are enforced by the tests.
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            STAPEL_COMM={
                "OUTBOX_ENABLED": False,
                "ACTION_TRANSPORT": "inprocess",
                "VALIDATE_SCHEMAS": True,
            },
            MIGRATION_MODULES={
                "users": None,
            },
        )
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_treenode_state():
    """django-treenode keeps two process-global stores that survive the
    per-test transaction rollback: a tree cache (Django cache backend) and a
    WeakSet of live model instances it back-fills tn_* fields into
    (``treenode.memory.__refs__``). Under sqlite ``:memory:`` PKs are reused
    across rolled-back tests, so a stale ref/cache entry from an earlier test
    would shadow a new instance with the same PK — clear both around every
    test so tree lookups are always recomputed from the current DB."""
    from django.core.cache import cache
    from treenode.memory import clear_refs

    from stapel_categories.models import Category, Feature

    def _reset():
        cache.clear()
        clear_refs(Category)
        clear_refs(Feature)

    _reset()
    yield
    _reset()


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()
