"""
Tests for feature editor enhancements.

Tests cover:
1. Inherit action from ADD features
2. CREATE action for new root features
3. Feature type editing for root features
"""
from django.test import TestCase
from stapel_categories.models import Category, Feature
from stapel_categories.feature_editor import apply_feature_editor_changes, FeatureEditorItem


def _apply(category, items):
    """Apply against the category's current revision (base_revision is required)."""
    rev = Category.objects.values_list("revision", flat=True).get(pk=category.pk)
    apply_feature_editor_changes(category, items, base_revision=rev)


class InheritFromAddActionTestCase(TestCase):
    """Test that inherit action works with features from ADD action."""

    def setUp(self):
        """Set up test data."""
        # Create root category
        self.root_category = Category.objects.create(
            name="Root Category",
            slug="root-category",
            tn_parent=None
        )

        # Create a root feature
        self.root_feature = Feature.objects.create(
            name="Size",
            slug="size",
            tn_parent=None,
            config={"type": "select", "options": [{"value": "s", "label": "Small"}]}
        )

    def test_inherit_from_add_action(self):
        """Test that we can inherit from a feature that was added via ADD action."""
        # First, add the feature to the category
        items = [
            FeatureEditorItem(
                action="add",
                order=0,
                feature={
                    "id": self.root_feature.pk,
                    "name": self.root_feature.name,
                    "slug": self.root_feature.slug,
                    "config": self.root_feature.config,
                    "mandatory": False,
                    "show_as_badge": False,
                    "show_at_title": False,
                }
            )
        ]

        _apply(self.root_category, items)

        # Verify the feature was added
        self.assertEqual(self.root_category.features.count(), 1)

        # Now create a child category
        child_category = Category.objects.create(
            name="Child Category",
            slug="child-category",
            tn_parent=self.root_category
        )

        # The child inherits features via get_all_features(), not directly
        all_features = child_category.get_all_features()
        self.assertEqual(all_features.count(), 1)

        # Now test inherit action from the child category to customize the inherited feature
        items = [
            FeatureEditorItem(
                action="inherit",
                order=0,
                feature={
                    "id": self.root_feature.pk,
                    "name": "Size (Custom)",
                    "slug": "size",
                    "config": {"type": "select", "options": [{"value": "m", "label": "Medium"}]},
                    "mandatory": True,
                    "show_as_badge": True,
                    "show_at_title": False,
                }
            )
        ]

        _apply(child_category, items)

        # Verify a new feature was created
        new_features = Feature.objects.filter(tn_parent=self.root_feature)
        self.assertEqual(new_features.count(), 1)

        new_feature = new_features.first()
        self.assertEqual(new_feature.name, "Size (Custom)")
        self.assertEqual(new_feature.slug, "size")
        self.assertTrue(new_feature.mandatory)
        self.assertTrue(new_feature.show_as_badge)
        self.assertEqual(new_feature.config["options"][0]["value"], "m")


class CreateActionTestCase(TestCase):
    """Test CREATE action for new root features."""

    def setUp(self):
        """Set up test data."""
        self.category = Category.objects.create(
            name="Test Category",
            slug="test-category",
            tn_parent=None
        )

    def test_create_new_root_feature(self):
        """Test creating a new root feature via CREATE action."""
        items = [
            FeatureEditorItem(
                action="create",
                order=0,
                feature={
                    "name": "Brand",
                    "slug": "brand",
                    "config": {"type": "string", "max": 100},
                    "mandatory": False,
                    "show_as_badge": False,
                    "show_at_title": True,
                }
            )
        ]

        _apply(self.category, items)

        # Verify the feature was created
        new_feature = Feature.objects.filter(slug="brand", tn_parent__isnull=True).first()
        self.assertIsNotNone(new_feature)
        self.assertEqual(new_feature.name, "Brand")
        self.assertFalse(new_feature.mandatory)
        self.assertTrue(new_feature.show_at_title)
        self.assertEqual(new_feature.config["type"], "string")

        # Verify it was added to the category
        self.assertIn(new_feature, self.category.features.all())

    def test_create_propagates_to_descendants(self):
        """Test that CREATE action propagates to descendant categories."""
        # Create child category
        child_category = Category.objects.create(
            name="Child Category",
            slug="child-category",
            tn_parent=self.category
        )

        items = [
            FeatureEditorItem(
                action="create",
                order=0,
                feature={
                    "name": "Material",
                    "slug": "material",
                    "config": {"type": "string"},
                    "mandatory": False,
                    "show_as_badge": False,
                    "show_at_title": False,
                }
            )
        ]

        _apply(self.category, items)

        # Verify the feature was created and added to both categories
        new_feature = Feature.objects.filter(slug="material").first()
        self.assertIsNotNone(new_feature)
        self.assertIn(new_feature, self.category.features.all())
        self.assertIn(new_feature, child_category.features.all())


class FeatureTypeEditingTestCase(TestCase):
    """Test feature type editing for root features."""

    def setUp(self):
        """Set up test data."""
        self.category = Category.objects.create(
            name="Test Category",
            slug="test-category-2",
            tn_parent=None
        )

        # Create a root feature
        self.root_feature = Feature.objects.create(
            name="Color",
            slug="color",
            tn_parent=None,
            config={"type": "string"}
        )

        # Add it to category
        self.category.features.add(self.root_feature)

    def test_can_edit_type_for_root_feature(self):
        """Test that we can edit the type for a root feature via EDIT action."""
        items = [
            FeatureEditorItem(
                action="edit",
                order=0,
                feature={
                    "id": self.root_feature.pk,
                    "name": "Color",
                    "slug": "color",
                    "config": {"type": "hex_color", "options": [{"hex": "#FF0000", "simple": "red", "label": "Red"}]},
                    "mandatory": False,
                    "show_as_badge": True,
                    "show_at_title": False,
                }
            )
        ]

        _apply(self.category, items)

        # Verify the feature type was changed
        self.root_feature.refresh_from_db()
        self.assertEqual(self.root_feature.config["type"], "hex_color")
        self.assertTrue(self.root_feature.show_as_badge)

    def test_create_with_type_selection(self):
        """Test that CREATE action allows type selection."""
        items = [
            FeatureEditorItem(
                action="create",
                order=0,
                feature={
                    "name": "Price",
                    "slug": "price",
                    "config": {"type": "int", "min": 0, "max": 1000000},
                    "mandatory": True,
                    "show_as_badge": False,
                    "show_at_title": True,
                }
            )
        ]

        _apply(self.category, items)

        # Verify the feature was created with the selected type
        new_feature = Feature.objects.filter(slug="price").first()
        self.assertIsNotNone(new_feature)
        self.assertEqual(new_feature.config["type"], "int")
        self.assertEqual(new_feature.config["min"], 0)
        self.assertTrue(new_feature.mandatory)


class ReorderDetectionTestCase(TestCase):
    """Test order change detection for KEEP action."""

    def setUp(self):
        """Set up test data."""
        self.category = Category.objects.create(
            name="Test Category",
            slug="test-category-3",
            tn_parent=None
        )

        # Create features
        self.feature1 = Feature.objects.create(name="Feature 1", slug="f1", tn_parent=None)
        self.feature2 = Feature.objects.create(name="Feature 2", slug="f2", tn_parent=None)
        self.feature3 = Feature.objects.create(name="Feature 3", slug="f3", tn_parent=None)

        # Add to category in order
        self.category.features.add(self.feature1, self.feature2, self.feature3)

    def test_reorder_features(self):
        """Test reordering features with KEEP action."""
        # Reorder: f3, f1, f2
        items = [
            FeatureEditorItem(
                action="keep",
                order=0,
                feature={
                    "id": self.feature3.pk,
                    "name": self.feature3.name,
                    "slug": self.feature3.slug,
                    "config": {},
                    "mandatory": False,
                    "show_as_badge": False,
                    "show_at_title": False,
                }
            ),
            FeatureEditorItem(
                action="keep",
                order=1,
                feature={
                    "id": self.feature1.pk,
                    "name": self.feature1.name,
                    "slug": self.feature1.slug,
                    "config": {},
                    "mandatory": False,
                    "show_as_badge": False,
                    "show_at_title": False,
                }
            ),
            FeatureEditorItem(
                action="keep",
                order=2,
                feature={
                    "id": self.feature2.pk,
                    "name": self.feature2.name,
                    "slug": self.feature2.slug,
                    "config": {},
                    "mandatory": False,
                    "show_as_badge": False,
                    "show_at_title": False,
                }
            ),
        ]

        _apply(self.category, items)

        # Verify order using the through model
        from stapel_categories.models import CategoryFeature
        links = CategoryFeature.objects.filter(category=self.category).order_by('order')
        self.assertEqual(links[0].feature.pk, self.feature3.pk)
        self.assertEqual(links[1].feature.pk, self.feature1.pk)
        self.assertEqual(links[2].feature.pk, self.feature2.pk)
