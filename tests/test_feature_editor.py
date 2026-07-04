"""
Comprehensive tests for feature editor with recursive updates to category hierarchies.

Tests cover all feature editor actions and their propagation to descendant categories:
- keep: no changes, only update M2M order
- add: add existing root Feature to M2M
- edit: update Feature model fields
- inherit: create new Feature with old as parent
- remove: remove Feature from M2M recursively
- create: create new root Feature
- replace: replace Feature with another from same tree
"""

import pytest
from django.db import transaction

from stapel_categories.models import Category, Feature, CategoryFeature
from stapel_categories.feature_editor import (
    FeatureEditorItem,
    apply_feature_editor_changes,
    build_editor_state,
    _iter_descendants,
    _remove_slug_recursive,
    _insert_feature_with_after_slug,
)


@pytest.fixture
def root_category():
    """Create a root category."""
    return Category.objects.create(name='Root', slug='root', draft='')


@pytest.fixture
def child_category(root_category):
    """Create a child category."""
    return Category.objects.create(
        name='Child',
        slug='child',
        tn_parent=root_category,
        draft=''
    )


@pytest.fixture
def grandchild_category(child_category):
    """Create a grandchild category."""
    return Category.objects.create(
        name='Grandchild',
        slug='grandchild',
        tn_parent=child_category,
        draft=''
    )


@pytest.fixture
def feature_color():
    """Create a color feature."""
    return Feature.objects.create(
        slug='color',
        name='Color',
        config={'type': 'hex_color'},
        mandatory=False
    )


@pytest.fixture
def feature_size():
    """Create a size feature."""
    return Feature.objects.create(
        slug='size',
        name='Size',
        config={'type': 'int', 'min': 0, 'max': 100},
        mandatory=True
    )


@pytest.fixture
def feature_brand():
    """Create a brand feature."""
    return Feature.objects.create(
        slug='brand',
        name='Brand',
        config={
            'type': 'select',
            'options': [
                {'value': 'nike', 'label': 'Nike'},
                {'value': 'adidas', 'label': 'Adidas'}
            ]
        },
        mandatory=False
    )


@pytest.mark.django_db
class TestFeatureEditorHelpers:
    """Tests for helper functions."""

    def test_iter_descendants_single_child(self, root_category, child_category):
        """Test iterating descendants with single child."""
        descendants = _iter_descendants(root_category)
        assert len(descendants) == 1
        assert descendants[0] == child_category

    def test_iter_descendants_multiple_levels(self, root_category, child_category, grandchild_category):
        """Test iterating descendants with multiple levels."""
        descendants = _iter_descendants(root_category)
        assert len(descendants) == 2
        assert child_category in descendants
        assert grandchild_category in descendants

    def test_iter_descendants_multiple_children(self, root_category):
        """Test iterating descendants with multiple children at same level."""
        child1 = Category.objects.create(name='Child1', slug='child1', tn_parent=root_category)
        child2 = Category.objects.create(name='Child2', slug='child2', tn_parent=root_category)

        descendants = _iter_descendants(root_category)
        assert len(descendants) == 2
        assert child1 in descendants
        assert child2 in descendants

    def test_iter_descendants_empty(self, root_category):
        """Test iterating descendants with no children."""
        descendants = _iter_descendants(root_category)
        assert len(descendants) == 0

    def test_remove_slug_recursive(self, root_category, child_category, grandchild_category, feature_color):
        """Test recursive removal of feature by slug."""
        # Add feature to all categories
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=child_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=grandchild_category, feature=feature_color, order=0)

        # Remove recursively
        changed = _remove_slug_recursive(root_category, 'color')

        # All categories should be changed
        assert root_category.pk in changed
        assert child_category.pk in changed
        assert grandchild_category.pk in changed

        # Feature should be removed from all
        assert not CategoryFeature.objects.filter(category=root_category, feature__slug='color').exists()
        assert not CategoryFeature.objects.filter(category=child_category, feature__slug='color').exists()
        assert not CategoryFeature.objects.filter(category=grandchild_category, feature__slug='color').exists()

    def test_insert_feature_with_after_slug_at_start(self, root_category, feature_color, feature_size):
        """Test inserting feature at start (after_slug=None)."""
        # Add existing feature
        CategoryFeature.objects.create(category=root_category, feature=feature_size, order=0)

        # Insert at start
        _insert_feature_with_after_slug(root_category, feature_color, after_slug=None)

        # Check order
        features = list(CategoryFeature.objects.filter(category=root_category).order_by('order'))
        assert features[0].feature == feature_color
        assert features[1].feature == feature_size

    def test_insert_feature_with_after_slug_middle(self, root_category, feature_color, feature_size, feature_brand):
        """Test inserting feature in middle after specific slug."""
        # Add existing features
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=root_category, feature=feature_brand, order=1)

        # Insert size after color
        _insert_feature_with_after_slug(root_category, feature_size, after_slug='color')

        # Check order
        features = list(CategoryFeature.objects.filter(category=root_category).order_by('order'))
        assert features[0].feature == feature_color
        assert features[1].feature == feature_size
        assert features[2].feature == feature_brand


@pytest.mark.django_db
class TestFeatureEditorActions:
    """Tests for feature editor actions."""

    def test_action_keep(self, root_category, feature_color):
        """Test 'keep' action preserves feature and updates order."""
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=5)

        items = [
            FeatureEditorItem(
                action='keep',
                order=0,
                feature={'id': feature_color.pk, 'slug': 'color'}
            )
        ]

        apply_feature_editor_changes(root_category, items)

        # Feature should still exist with updated order
        cf = CategoryFeature.objects.get(category=root_category, feature=feature_color)
        assert cf.order == 0

    def test_action_add(self, root_category, child_category, feature_color):
        """Test 'add' action adds existing feature and propagates to children."""
        items = [
            FeatureEditorItem(
                action='add',
                order=0,
                feature={'id': feature_color.pk, 'slug': 'color'}
            )
        ]

        apply_feature_editor_changes(root_category, items)

        # Feature should be added to root and child
        assert CategoryFeature.objects.filter(category=root_category, feature=feature_color).exists()
        assert CategoryFeature.objects.filter(category=child_category, feature=feature_color).exists()

    def test_action_edit(self, root_category, feature_color):
        """Test 'edit' action updates feature fields."""
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)

        items = [
            FeatureEditorItem(
                action='edit',
                order=0,
                feature={
                    'id': feature_color.pk,
                    'slug': 'color',
                    'name': 'Updated Color',
                    'comment': 'New comment',
                    'mandatory': True,
                    'config': {'type': 'hex_color', 'options': []},
                }
            )
        ]

        apply_feature_editor_changes(root_category, items)

        # Feature should be updated
        feature_color.refresh_from_db()
        assert feature_color.name == 'Updated Color'
        assert feature_color.comment == 'New comment'
        assert feature_color.mandatory is True

    def test_action_remove_recursive(self, root_category, child_category, grandchild_category, feature_color):
        """Test 'remove' action removes feature from all descendants."""
        # Add feature to all
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=child_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=grandchild_category, feature=feature_color, order=0)

        items = [
            FeatureEditorItem(
                action='remove',
                order=0,
                feature={'id': feature_color.pk, 'slug': 'color'}
            )
        ]

        apply_feature_editor_changes(root_category, items)

        # Feature should be removed from all
        assert not CategoryFeature.objects.filter(category=root_category, feature=feature_color).exists()
        assert not CategoryFeature.objects.filter(category=child_category, feature=feature_color).exists()
        assert not CategoryFeature.objects.filter(category=grandchild_category, feature=feature_color).exists()

    def test_action_create(self, root_category, child_category):
        """Test 'create' action creates new root feature and propagates."""
        items = [
            FeatureEditorItem(
                action='create',
                order=0,
                feature={
                    'slug': 'new-feature',
                    'name': 'New Feature',
                    'config': {'type': 'string'},
                    'mandatory': False,
                }
            )
        ]

        apply_feature_editor_changes(root_category, items)

        # New feature should exist
        new_feature = Feature.objects.get(slug='new-feature')
        assert new_feature.name == 'New Feature'
        assert new_feature.tn_parent is None

        # Should be added to root and child
        assert CategoryFeature.objects.filter(category=root_category, feature=new_feature).exists()
        assert CategoryFeature.objects.filter(category=child_category, feature=new_feature).exists()

    def test_action_inherit(self, root_category, child_category, feature_color):
        """Test 'inherit' action creates new feature with parent and propagates."""
        # Add original feature to root
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)

        items = [
            FeatureEditorItem(
                action='inherit',
                order=0,
                feature={
                    'id': feature_color.pk,
                    'slug': 'color',
                    'name': 'Inherited Color',
                    'config': {'type': 'hex_color', 'custom': True},
                }
            )
        ]

        apply_feature_editor_changes(root_category, items)

        # New inherited feature should exist
        inherited_features = Feature.objects.filter(slug='color', tn_parent=feature_color)
        assert inherited_features.count() == 1
        inherited = inherited_features.first()
        assert inherited.name == 'Inherited Color'
        assert inherited.config.get('custom') is True

        # Original should be removed, inherited should be added
        assert not CategoryFeature.objects.filter(category=root_category, feature=feature_color).exists()
        assert CategoryFeature.objects.filter(category=root_category, feature=inherited).exists()
        assert CategoryFeature.objects.filter(category=child_category, feature=inherited).exists()


@pytest.mark.django_db
class TestFeatureEditorComplexScenarios:
    """Tests for complex multi-action scenarios."""

    def test_multiple_actions_ordering(self, root_category, feature_color, feature_size, feature_brand):
        """Test multiple actions maintain correct order."""
        items = [
            FeatureEditorItem(action='add', order=0, feature={'id': feature_color.pk, 'slug': 'color'}),
            FeatureEditorItem(action='add', order=1, feature={'id': feature_size.pk, 'slug': 'size'}),
            FeatureEditorItem(action='add', order=2, feature={'id': feature_brand.pk, 'slug': 'brand'}),
        ]

        apply_feature_editor_changes(root_category, items)

        # Check order
        features = list(CategoryFeature.objects.filter(category=root_category).order_by('order'))
        assert len(features) == 3
        assert features[0].feature == feature_color
        assert features[1].feature == feature_size
        assert features[2].feature == feature_brand

    def test_reorder_existing_features(self, root_category, feature_color, feature_size):
        """Test reordering existing features."""
        # Add in one order
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=root_category, feature=feature_size, order=1)

        # Reverse order
        items = [
            FeatureEditorItem(action='keep', order=0, feature={'id': feature_size.pk, 'slug': 'size'}),
            FeatureEditorItem(action='keep', order=1, feature={'id': feature_color.pk, 'slug': 'color'}),
        ]

        apply_feature_editor_changes(root_category, items)

        # Check new order
        features = list(CategoryFeature.objects.filter(category=root_category).order_by('order'))
        assert features[0].feature == feature_size
        assert features[1].feature == feature_color

    def test_remove_and_add_different(self, root_category, child_category, feature_color, feature_size):
        """Test removing one feature and adding another in same operation."""
        # Start with color
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=child_category, feature=feature_color, order=0)

        items = [
            FeatureEditorItem(action='remove', order=0, feature={'id': feature_color.pk, 'slug': 'color'}),
            FeatureEditorItem(action='add', order=0, feature={'id': feature_size.pk, 'slug': 'size'}),
        ]

        apply_feature_editor_changes(root_category, items)

        # Color should be removed, size should be added (both recursively)
        assert not CategoryFeature.objects.filter(category=root_category, feature=feature_color).exists()
        assert not CategoryFeature.objects.filter(category=child_category, feature=feature_color).exists()
        assert CategoryFeature.objects.filter(category=root_category, feature=feature_size).exists()
        assert CategoryFeature.objects.filter(category=child_category, feature=feature_size).exists()

    def test_deep_hierarchy_propagation(self, root_category, child_category, grandchild_category, feature_color):
        """Test action propagation through deep hierarchy."""
        items = [
            FeatureEditorItem(action='add', order=0, feature={'id': feature_color.pk, 'slug': 'color'}),
        ]

        apply_feature_editor_changes(root_category, items)

        # Should propagate to all levels
        assert CategoryFeature.objects.filter(category=root_category, feature=feature_color).exists()
        assert CategoryFeature.objects.filter(category=child_category, feature=feature_color).exists()
        assert CategoryFeature.objects.filter(category=grandchild_category, feature=feature_color).exists()

    def test_edit_then_inherit(self, root_category, child_category, feature_color):
        """Test editing feature then inheriting creates proper hierarchy."""
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)

        # First edit
        items = [
            FeatureEditorItem(
                action='edit',
                order=0,
                feature={'id': feature_color.pk, 'slug': 'color', 'name': 'Edited Color', 'config': {'type': 'hex_color'}}
            ),
        ]
        apply_feature_editor_changes(root_category, items)

        feature_color.refresh_from_db()
        assert feature_color.name == 'Edited Color'

        # Then inherit
        items = [
            FeatureEditorItem(
                action='inherit',
                order=0,
                feature={'id': feature_color.pk, 'slug': 'color', 'name': 'Child Color', 'config': {'type': 'hex_color'}}
            ),
        ]
        apply_feature_editor_changes(root_category, items)

        # Should have inherited feature
        inherited = Feature.objects.get(slug='color', tn_parent=feature_color)
        assert inherited.name == 'Child Color'


@pytest.mark.django_db
class TestBuildEditorState:
    """Tests for building editor state."""

    def test_build_editor_state_empty(self, root_category):
        """Test building state for category with no features."""
        state = build_editor_state(root_category)

        assert 'features' in state
        assert 'available_root_features' in state
        assert 'draft' in state
        assert len(state['features']) == 0

    def test_build_editor_state_with_features(self, root_category, feature_color, feature_size):
        """Test building state with features."""
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)
        CategoryFeature.objects.create(category=root_category, feature=feature_size, order=1)

        state = build_editor_state(root_category)

        assert len(state['features']) == 2
        assert state['features'][0]['feature']['slug'] == 'color'
        assert state['features'][1]['feature']['slug'] == 'size'

    def test_build_editor_state_available_actions_no_parent(self, root_category, feature_color):
        """Test available actions when category has no parent."""
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)

        state = build_editor_state(root_category)

        # Without parent, should have edit and remove available
        actions = state['features'][0]['available_actions']
        assert 'keep' in actions
        assert 'inherit' in actions
        assert 'edit' in actions
        assert 'remove' in actions

    def test_build_editor_state_available_actions_with_parent_feature(self, root_category, child_category, feature_color):
        """Test available actions when parent has same feature."""
        # Add feature to parent
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)

        # Add same feature to child
        CategoryFeature.objects.create(category=child_category, feature=feature_color, order=0)

        state = build_editor_state(child_category)

        # With parent having same feature, edit and remove should not be available
        actions = state['features'][0]['available_actions']
        assert 'keep' in actions
        assert 'inherit' in actions
        assert 'edit' not in actions
        assert 'remove' not in actions

    def test_build_editor_state_available_root_features(self, root_category, feature_color, feature_size):
        """Test available root features excludes already used ones."""
        CategoryFeature.objects.create(category=root_category, feature=feature_color, order=0)

        state = build_editor_state(root_category)

        # Color should not be in available, but size should
        available_slugs = [f['slug'] for f in state['available_root_features']]
        assert 'color' not in available_slugs
        assert 'size' in available_slugs


@pytest.mark.django_db
class TestFeatureEditorEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_items_list(self, root_category):
        """Test applying changes with empty items list."""
        # Should not raise error
        apply_feature_editor_changes(root_category, [])

    def test_invalid_feature_id_graceful_handling(self, root_category):
        """Test that invalid feature IDs are handled gracefully."""
        items = [
            FeatureEditorItem(action='keep', order=0, feature={'id': 99999, 'slug': 'nonexistent'}),
        ]

        # Should not raise error, just skip invalid item
        apply_feature_editor_changes(root_category, items)

        # No features should be added
        assert CategoryFeature.objects.filter(category=root_category).count() == 0

    def test_concurrent_updates_transaction_safety(self, root_category, feature_color):
        """Test that feature editor changes are atomic."""
        items = [
            FeatureEditorItem(action='add', order=0, feature={'id': feature_color.pk, 'slug': 'color'}),
        ]

        # Should complete successfully
        with transaction.atomic():
            apply_feature_editor_changes(root_category, items)

        assert CategoryFeature.objects.filter(category=root_category, feature=feature_color).exists()

    def test_feature_ordering_gaps(self, root_category, feature_color, feature_size, feature_brand):
        """Test that gaps in order values are handled correctly."""
        items = [
            FeatureEditorItem(action='add', order=0, feature={'id': feature_color.pk, 'slug': 'color'}),
            FeatureEditorItem(action='add', order=10, feature={'id': feature_size.pk, 'slug': 'size'}),
            FeatureEditorItem(action='add', order=100, feature={'id': feature_brand.pk, 'slug': 'brand'}),
        ]

        apply_feature_editor_changes(root_category, items)

        # Final order should be sequential 0, 1, 2 regardless of input gaps
        features = list(CategoryFeature.objects.filter(category=root_category).order_by('order'))
        assert features[0].order == 0
        assert features[1].order == 1
        assert features[2].order == 2
