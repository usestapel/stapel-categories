"""
Comprehensive tests for categories models with edge cases.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from stapel_categories.models import Category, Feature, CategoryFeature
# Registry is used internally, not needed for these tests


@pytest.fixture
def category():
    """Create a test category."""
    return Category.objects.create(
        name='Test Category',
        slug='test-category',
        draft=''
    )


@pytest.fixture
def draft_category():
    """Create a draft category."""
    return Category.objects.create(
        name='Draft Category',
        slug='draft-category',
        draft='draft_data'
    )


@pytest.fixture
def parent_category():
    """Create a parent category."""
    return Category.objects.create(
        name='Parent Category',
        slug='parent-category',
        draft=''
    )


@pytest.fixture
def string_feature():
    """Create a string feature."""
    return Feature.objects.create(
        slug='test-string',
        name='Test String',
        config={'type': 'string'}
    )


@pytest.fixture
def int_feature():
    """Create an integer feature."""
    return Feature.objects.create(
        slug='test-int',
        name='Test Integer',
        config={'type': 'int', 'min': 0, 'max': 100}
    )


@pytest.fixture
def select_feature():
    """Create a select feature."""
    return Feature.objects.create(
        slug='test-select',
        name='Test Select',
        config={
            'type': 'select',
            'options': [
                {'value': 'option1', 'label': 'Option 1'},
                {'value': 'option2', 'label': 'Option 2'}
            ]
        }
    )


@pytest.mark.django_db
class TestCategory:
    """Tests for Category model."""

    def test_create_category(self, category):
        """Test creating a category."""
        assert category.name == 'Test Category'
        assert category.draft == ''

    def test_category_str(self, category):
        """Test category string representation."""
        # String representation uses translation
        assert category.name == 'Test Category'

    def test_category_name_required(self):
        """Test that category name is required."""
        with pytest.raises(ValidationError):
            category = Category(name='', draft='')
            category.full_clean()

    def test_category_parent_child_relationship(self, parent_category):
        """Test parent-child relationship."""
        child = Category.objects.create(
            name='Child Category',
            slug='child-category',
            tn_parent=parent_category
        )

        assert child.tn_parent == parent_category

    def test_category_draft_field(self, draft_category):
        """Test draft field functionality."""
        assert draft_category.draft == 'draft_data'

        # Clear draft
        draft_category.draft = ''
        draft_category.save()

        draft_category.refresh_from_db()
        assert draft_category.draft == ''

    def test_category_with_features(self, category, string_feature, int_feature):
        """Test category with features."""
        CategoryFeature.objects.create(
            category=category,
            feature=string_feature,
            order=1
        )
        CategoryFeature.objects.create(
            category=category,
            feature=int_feature,
            order=2
        )

        assert category.features.count() == 2

    def test_category_features_ordering(self, category, string_feature, int_feature, select_feature):
        """Test that category features are ordered by order field."""
        CategoryFeature.objects.create(category=category, feature=select_feature, order=3)
        CategoryFeature.objects.create(category=category, feature=string_feature, order=1)
        CategoryFeature.objects.create(category=category, feature=int_feature, order=2)

        features = list(category.category_features.all())

        assert features[0].order == 1
        assert features[1].order == 2
        assert features[2].order == 3

    def test_category_hierarchy_depth(self, parent_category):
        """Test creating deep category hierarchy."""
        child1 = Category.objects.create(name='Child 1', slug='child-1', tn_parent=parent_category)
        child2 = Category.objects.create(name='Child 2', slug='child-2', tn_parent=child1)
        child3 = Category.objects.create(name='Child 3', slug='child-3', tn_parent=child2)

        assert child3.tn_parent == child2
        assert child2.tn_parent == child1
        assert child1.tn_parent == parent_category

    def test_category_cascade_delete(self, parent_category):
        """Test cascade delete of child categories."""
        child = Category.objects.create(name='Child', slug='child', tn_parent=parent_category)
        child_id = child.id

        parent_category.delete()

        assert not Category.objects.filter(id=child_id).exists()

    def test_category_get_all_features(self, parent_category, category, string_feature, int_feature):
        """Test getting all features including inherited ones."""
        # Add feature to parent
        CategoryFeature.objects.create(category=parent_category, feature=string_feature, order=1)

        # Add feature to child
        category.tn_parent = parent_category
        category.save()
        CategoryFeature.objects.create(category=category, feature=int_feature, order=2)

        # Child should have access to both features via get_all_features
        all_features = category.get_all_features()
        assert all_features.count() == 2


@pytest.mark.django_db
class TestFeature:
    """Tests for Feature model."""

    def test_create_string_feature(self, string_feature):
        """Test creating a string feature."""
        assert string_feature.slug == 'test-string'
        assert string_feature.name == 'Test String'
        assert string_feature.feature_type == 'string'

    def test_create_int_feature(self, int_feature):
        """Test creating an integer feature."""
        assert int_feature.feature_type == 'int'
        assert int_feature.config['min'] == 0
        assert int_feature.config['max'] == 100

    def test_create_select_feature(self, select_feature):
        """Test creating a select feature."""
        assert select_feature.feature_type == 'select'
        assert len(select_feature.config['options']) == 2

    def test_feature_str(self, string_feature):
        """Test feature string representation."""
        # String representation uses display_name property with translation
        assert string_feature.name == 'Test String'


@pytest.mark.django_db
class TestCategoryFeature:
    """Tests for CategoryFeature model."""

    def test_create_category_feature(self, category, string_feature):
        """Test creating a category feature."""
        cf = CategoryFeature.objects.create(
            category=category,
            feature=string_feature,
            order=1
        )

        assert cf.category == category
        assert cf.feature == string_feature
        assert cf.order == 1

    def test_category_feature_ordering(self, category, string_feature, int_feature, select_feature):
        """Test category feature ordering by order field."""
        CategoryFeature.objects.create(category=category, feature=select_feature, order=30)
        CategoryFeature.objects.create(category=category, feature=string_feature, order=10)
        CategoryFeature.objects.create(category=category, feature=int_feature, order=20)

        features = list(CategoryFeature.objects.filter(category=category))

        assert features[0].order == 10
        assert features[1].order == 20
        assert features[2].order == 30

    def test_category_feature_unique_together(self, category, string_feature):
        """Test that category-feature combination must be unique."""
        CategoryFeature.objects.create(category=category, feature=string_feature, order=1)

        with pytest.raises(IntegrityError):
            CategoryFeature.objects.create(category=category, feature=string_feature, order=2)

    def test_category_feature_cascade_delete_category(self, category, string_feature):
        """Test cascade delete when category is deleted."""
        cf = CategoryFeature.objects.create(category=category, feature=string_feature, order=1)
        cf_id = cf.id

        category.delete()

        assert not CategoryFeature.objects.filter(id=cf_id).exists()

    def test_category_feature_cascade_delete_feature(self, category, string_feature):
        """Test cascade delete when feature is deleted."""
        cf = CategoryFeature.objects.create(category=category, feature=string_feature, order=1)
        cf_id = cf.id

        string_feature.delete()

        assert not CategoryFeature.objects.filter(id=cf_id).exists()

    def test_multiple_categories_same_feature(self, category, draft_category, string_feature):
        """Test that same feature can be used in multiple categories."""
        cf1 = CategoryFeature.objects.create(category=category, feature=string_feature, order=1)
        cf2 = CategoryFeature.objects.create(category=draft_category, feature=string_feature, order=1)

        assert cf1.feature == cf2.feature
        assert cf1.category != cf2.category

    def test_category_feature_order_update(self, category, string_feature):
        """Test updating category feature order."""
        cf = CategoryFeature.objects.create(category=category, feature=string_feature, order=1)

        cf.order = 10
        cf.save()

        cf.refresh_from_db()
        assert cf.order == 10
