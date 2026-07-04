"""
Tests for translation key collection.
"""

import pytest
from stapel_categories.models import Category, Feature
from stapel_categories.translation_keys import (
    collect_category_translation_keys,
    collect_feature_translation_keys,
    collect_all_catalog_translation_keys,
    _extract_hierarchical_option_keys,
)


@pytest.mark.django_db
class TestCollectCategoryTranslationKeys:
    """Tests for collect_category_translation_keys."""

    def test_collect_translatable_categories(self):
        """Test collecting keys from translatable categories."""
        Category.objects.create(name='category.electronics', slug='electronics', translatable=True)
        Category.objects.create(name='category.vehicles', slug='vehicles', translatable=True)
        Category.objects.create(name='Non Translatable', slug='non-translatable', translatable=False)

        keys = collect_category_translation_keys()

        assert 'category.electronics' in keys
        assert 'category.vehicles' in keys
        assert 'Non Translatable' not in keys
        assert len(keys) == 2

    def test_empty_categories(self):
        """Test with no categories."""
        keys = collect_category_translation_keys()
        assert len(keys) == 0


@pytest.mark.django_db
class TestCollectFeatureTranslationKeys:
    """Tests for collect_feature_translation_keys."""

    def test_collect_feature_names(self):
        """Test collecting feature names based on translate setting."""
        Feature.objects.create(
            name='feature.color',
            translate=Feature.TranslateMode.ALL,
            config={'type': 'string'}
        )
        Feature.objects.create(
            name='feature.size',
            translate=Feature.TranslateMode.TITLE,
            config={'type': 'int'}
        )
        Feature.objects.create(
            name='feature.hidden',
            translate=Feature.TranslateMode.NONE,
            config={'type': 'bool'}
        )

        keys = collect_feature_translation_keys()

        assert 'feature.color' in keys
        assert 'feature.size' in keys
        assert 'feature.hidden' not in keys

    def test_collect_select_options(self):
        """Test collecting options from select type features."""
        Feature.objects.create(
            name='feature.condition',
            translate=Feature.TranslateMode.ALL,
            config={
                'type': 'select',
                'translatable_options': True,
                'options': [
                    {'value': 'new', 'label': 'condition.new'},
                    {'value': 'used', 'label': 'condition.used'},
                ]
            }
        )

        keys = collect_feature_translation_keys()

        assert 'feature.condition' in keys
        assert 'condition.new' in keys
        assert 'condition.used' in keys

    def test_skip_non_translatable_options(self):
        """Test skipping options when translatable_options=False."""
        Feature.objects.create(
            name='feature.test',
            translate=Feature.TranslateMode.ALL,
            config={
                'type': 'select',
                'translatable_options': False,
                'options': [
                    {'value': '1', 'label': 'Option 1'},
                ]
            }
        )

        keys = collect_feature_translation_keys()

        assert 'feature.test' in keys
        assert 'Option 1' not in keys

    def test_skip_options_when_translate_title_only(self):
        """Test not collecting options when translate=TITLE."""
        Feature.objects.create(
            name='feature.test',
            translate=Feature.TranslateMode.TITLE,
            config={
                'type': 'select',
                'options': [
                    {'value': '1', 'label': 'option.one'},
                ]
            }
        )

        keys = collect_feature_translation_keys()

        assert 'feature.test' in keys
        assert 'option.one' not in keys


class TestExtractHierarchicalOptions:
    """Tests for hierarchical option extraction."""

    def test_extract_flat_options(self):
        """Test extracting from flat option list."""
        options = [
            {'value': '1', 'label': 'option.one'},
            {'value': '2', 'label': 'option.two'},
        ]

        keys = _extract_hierarchical_option_keys(options)

        assert 'option.one' in keys
        assert 'option.two' in keys
        assert len(keys) == 2

    def test_extract_nested_options(self):
        """Test extracting from nested options."""
        options = [
            {
                'value': '1',
                'label': 'parent.one',
                'children': [
                    {'value': '1.1', 'label': 'child.one'},
                    {'value': '1.2', 'label': 'child.two'},
                ]
            },
        ]

        keys = _extract_hierarchical_option_keys(options)

        assert 'parent.one' in keys
        assert 'child.one' in keys
        assert 'child.two' in keys
        assert len(keys) == 3


@pytest.mark.django_db
class TestCollectAllKeys:
    """Tests for collect_all_catalog_translation_keys."""

    def test_combined_collection(self):
        """Test collecting from both categories and features."""
        Category.objects.create(name='category.test', slug='test', translatable=True)
        Feature.objects.create(
            name='feature.test',
            translate=Feature.TranslateMode.ALL,
            config={'type': 'string'}
        )

        result = collect_all_catalog_translation_keys()

        assert 'category.test' in result['all_keys']
        assert 'feature.test' in result['all_keys']
        assert result['total_count'] == 2
        assert 'category.test' in [item['key'] for item in result['categories']]
        assert 'feature.test' in [item['key'] for item in result['features']]

    def test_deduplication(self):
        """Test that duplicate keys are deduplicated."""
        # Same key in both category and feature
        Category.objects.create(name='common.key', slug='common-key', translatable=True)
        Feature.objects.create(
            name='common.key',
            translate=Feature.TranslateMode.ALL,
            config={'type': 'string'}
        )

        result = collect_all_catalog_translation_keys()

        # Should only count once in all_keys
        assert result['all_keys'].count('common.key') == 1
        assert result['total_count'] >= 1
        # But should appear in both source lists
        assert 'common.key' in [item['key'] for item in result['categories']]
        assert 'common.key' in [item['key'] for item in result['features']]
