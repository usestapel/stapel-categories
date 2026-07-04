"""Extension-point tests (library-standard §4 requires them): the
``DISPLAY_TRANSLATOR`` dotted-path seam, the carousel cache-timeout setting,
and delegation of config validation to the stapel-attributes registry."""
import pytest
from django.core.cache import cache
from django.test import override_settings

from stapel_categories.models import Category, Feature
from stapel_categories.translation import translate


def loud_translator(key):
    """Test double for the DISPLAY_TRANSLATOR seam."""
    return f"T({key})"


class TestDisplayTranslatorSeam:
    def test_default_is_identity(self):
        assert translate("category.electronics") == "category.electronics"

    @override_settings(
        STAPEL_CATEGORIES={"DISPLAY_TRANSLATOR": "stapel_categories.tests.test_extension_points.loud_translator"}
    )
    def test_seam_swaps_translator_without_fork(self):
        assert translate("category.electronics") == "T(category.electronics)"

    @pytest.mark.django_db
    @override_settings(
        STAPEL_CATEGORIES={"DISPLAY_TRANSLATOR": "stapel_categories.tests.test_extension_points.loud_translator"}
    )
    def test_category_str_uses_seam(self):
        category = Category.objects.create(name="category.books", slug="books")
        assert str(category) == "T(category.books)"


class TestCarouselCacheTimeoutSetting:
    @pytest.mark.django_db
    @override_settings(STAPEL_CATEGORIES={"CAROUSEL_CACHE_TIMEOUT": 42})
    def test_timeout_is_a_setting(self, client):
        cache.clear()
        Category.objects.create(
            name="Cars", slug="cars", active=True, carousel_enabled=True
        )
        resp = client.get("/catalog/api/categories/carousel/")
        assert resp.status_code == 200
        assert resp["Cache-Control"] == "public, max-age=42"


class TestAttributesDelegation:
    @pytest.mark.django_db
    def test_config_validation_delegates_to_attributes(self):
        # An unknown feature type is rejected by the stapel-attributes
        # registry, surfaced through Feature.clean() — categories does not
        # own the type list.
        from django.core.exceptions import ValidationError

        feature = Feature(slug="mystery", name="Mystery", config={"type": "does-not-exist"})
        with pytest.raises(ValidationError):
            feature.full_clean()

    @pytest.mark.django_db
    def test_get_config_with_defaults_uses_registry(self):
        # The registry fills in type defaults categories never hard-codes.
        feature = Feature.objects.create(slug="w", name="W", config={"type": "string"})
        resolved = feature.get_config_with_defaults()
        assert resolved["type"] == "string"
