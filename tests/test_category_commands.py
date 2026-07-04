"""
Tests for category command pattern endpoints and revision tracking.

This module tests:
- Slug field on categories
- deleted_ids in pagination responses
- Children endpoint
- Bulk commands endpoint (add/edit/delete/reorder)
- Revision tracking on modifications
- Delete cascade to descendants
- Undelete admin action
"""

from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from rest_framework.test import APIClient
from rest_framework import status

from stapel_categories.models import Category, Feature, CategoryFeature
from stapel_categories.admin import CategoryAdmin


User = get_user_model()


class CategorySlugTests(TestCase):
    """Test slug field on Category model."""

    def test_category_has_slug_field(self):
        """Category model should have a slug field."""
        category = Category.objects.create(
            name="Test Category",
            slug="test-category"
        )
        self.assertEqual(category.slug, "test-category")

    def test_slug_is_unique(self):
        """Slug field should be unique."""
        Category.objects.create(name="Category 1", slug="same-slug")

        with self.assertRaises(Exception):  # IntegrityError
            Category.objects.create(name="Category 2", slug="same-slug")

    def test_slug_is_indexed(self):
        """Slug field should be indexed."""
        # Check model field attributes
        slug_field = Category._meta.get_field('slug')
        self.assertTrue(slug_field.db_index)


class CategoryPaginationDeletedIdsTests(TestCase):
    """Test deleted_ids in pagination response."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_deleted_ids_included_in_response(self):
        """Pagination response should include deleted_ids."""
        # Create categories with revisions
        Category.objects.create(name="Cat 1", slug="cat-1")
        cat2 = Category.objects.create(name="Cat 2", slug="cat-2")
        cat3 = Category.objects.create(name="Cat 3", slug="cat-3")

        # Get initial revision
        initial_revision = cat3.revision

        # Mark one as deleted
        cat2.deleted = True
        cat2.save()

        # Request categories with min_revision
        response = self.client.get(f'/catalog/api/categories/?min_revision={initial_revision}')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('revisions', response.data)
        self.assertIn('deleted_ids', response.data['revisions'])
        self.assertIn(cat2.id, response.data['revisions']['deleted_ids'])

    def test_deleted_ids_empty_without_min_revision(self):
        """deleted_ids should be empty when min_revision is not provided."""
        Category.objects.create(name="Cat 1", slug="cat-1", deleted=True)

        response = self.client.get('/catalog/api/categories/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['revisions']['deleted_ids'], [])


class CategoryChildrenEndpointTests(TestCase):
    """Test children endpoint for categories."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_children_endpoint_returns_children(self):
        """Children endpoint should return only direct children."""
        parent = Category.objects.create(name="Parent", slug="parent")
        child1 = Category.objects.create(
            name="Child 1",
            slug="child-1",
            tn_parent=parent,
            tn_priority=2
        )
        Category.objects.create(
            name="Child 2",
            slug="child-2",
            tn_parent=parent,
            tn_priority=1
        )

        # Create a grandchild (should not be included)
        Category.objects.create(
            name="Grandchild",
            slug="grandchild",
            tn_parent=child1
        )

        response = self.client.get(f'/catalog/api/categories/{parent.id}/children/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Should be sorted by tn_priority descending (higher priority first)
        self.assertEqual(response.data[0]['name'], "Child 1")  # priority=2
        self.assertEqual(response.data[1]['name'], "Child 2")  # priority=1

    def test_children_endpoint_excludes_deleted(self):
        """Children endpoint should exclude deleted categories."""
        parent = Category.objects.create(name="Parent", slug="parent")
        Category.objects.create(
            name="Child 1",
            slug="child-1",
            tn_parent=parent
        )
        Category.objects.create(
            name="Child 2",
            slug="child-2",
            tn_parent=parent,
            deleted=True
        )

        response = self.client.get(f'/catalog/api/categories/{parent.id}/children/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Child 1")


class BulkCommandsEndpointTests(TestCase):
    """Test bulk commands endpoint."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_add_command_creates_category(self):
        """Add command should create a new category."""
        parent = Category.objects.create(name="Parent", slug="parent")

        data = {
            'categories': [
                {
                    'command': 'add',
                    'name': 'New Category',
                    'slug': 'new-category',
                    'parent_id': parent.id,
                    'priority': 5
                }
            ]
        }

        response = self.client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['created']), 1)

        new_cat = Category.objects.get(slug='new-category')
        self.assertEqual(new_cat.name, 'New Category')
        self.assertEqual(new_cat.tn_parent, parent)
        self.assertEqual(new_cat.tn_priority, 5)
        self.assertGreater(new_cat.revision, 0)

    def test_edit_command_updates_category(self):
        """Edit command should update name and slug."""
        category = Category.objects.create(name="Old Name", slug="old-slug")
        initial_revision = category.revision

        data = {
            'categories': [
                {
                    'id': category.pk,
                    'command': 'edit',
                    'name': 'New Name',
                    'slug': 'new-slug'
                }
            ]
        }

        response = self.client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(category.pk, response.data['updated'])

        category.refresh_from_db()
        self.assertEqual(category.name, 'New Name')
        self.assertEqual(category.slug, 'new-slug')
        self.assertGreater(category.revision, initial_revision)

    def test_delete_command_marks_deleted(self):
        """Delete command should mark category and descendants as deleted."""
        parent = Category.objects.create(name="Parent", slug="parent")
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=parent,
            tn_priority=5
        )
        grandchild = Category.objects.create(
            name="Grandchild",
            slug="grandchild",
            tn_parent=child
        )

        data = {
            'categories': [
                {
                    'id': parent.id,
                    'command': 'delete'
                }
            ]
        }

        response = self.client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(parent.id, response.data['deleted'])
        self.assertIn(child.id, response.data['deleted'])
        self.assertIn(grandchild.id, response.data['deleted'])

        # Verify deleted flags
        parent.refresh_from_db()
        child.refresh_from_db()
        grandchild.refresh_from_db()

        self.assertTrue(parent.deleted)
        self.assertEqual(parent.tn_priority, 0)
        self.assertTrue(child.deleted)
        self.assertTrue(grandchild.deleted)

    def test_reorder_command_updates_priority(self):
        """Reorder command should update tn_priority."""
        category = Category.objects.create(
            name="Category",
            slug="category",
            tn_priority=1
        )
        initial_revision = category.revision

        data = {
            'categories': [
                {
                    'id': category.pk,
                    'command': 'reorder',
                    'priority': 10
                }
            ]
        }

        response = self.client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(category.pk, response.data['updated'])

        category.refresh_from_db()
        self.assertEqual(category.tn_priority, 10)
        self.assertGreater(category.revision, initial_revision)

    def test_keep_command_no_changes(self):
        """Keep command should not modify the category."""
        category = Category.objects.create(name="Category", slug="category")
        initial_revision = category.revision

        data = {
            'categories': [
                {
                    'id': category.pk,
                    'command': 'keep'
                }
            ]
        }

        response = self.client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        category.refresh_from_db()
        self.assertEqual(category.revision, initial_revision)

    def test_multiple_commands_in_single_request(self):
        """Should handle multiple commands in one request."""
        cat1 = Category.objects.create(name="Cat 1", slug="cat-1")
        cat2 = Category.objects.create(name="Cat 2", slug="cat-2")

        data = {
            'categories': [
                {'id': cat1.id, 'command': 'edit', 'name': 'Updated Cat 1'},
                {'command': 'add', 'name': 'Cat 3', 'slug': 'cat-3'},
                {'id': cat2.id, 'command': 'delete'}
            ]
        }

        response = self.client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['updated']), 1)
        self.assertEqual(len(response.data['created']), 1)
        self.assertIn(cat2.id, response.data['deleted'])


class RevisionTrackingTests(TestCase):
    """Test that revisions are updated correctly."""

    def test_create_sets_revision(self):
        """Creating a category should set revision."""
        cat = Category.objects.create(name="Cat", slug="cat")
        self.assertGreater(cat.revision, 0)

    def test_edit_increments_revision(self):
        """Editing a category should increment revision."""
        cat = Category.objects.create(name="Cat", slug="cat")
        initial_revision = cat.revision

        cat.name = "Updated Cat"
        cat.save()

        self.assertGreater(cat.revision, initial_revision)

    def test_delete_increments_revision(self):
        """Marking as deleted should increment revision."""
        cat = Category.objects.create(name="Cat", slug="cat")
        initial_revision = cat.revision

        cat.deleted = True
        cat.save()

        self.assertGreater(cat.revision, initial_revision)


class UndeleteAdminActionTests(TestCase):
    """Test undelete admin action."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = CategoryAdmin(Category, self.site)
        self.factory = RequestFactory()
        self.user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='testpass123'
        )

    def _get_request_with_messages(self):
        """Create a request with messages middleware support."""
        request = self.factory.get('/admin/')
        request.user = self.user
        # Add session and messages middleware
        setattr(request, 'session', 'session')
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)
        return request

    def test_undelete_restores_category(self):
        """Undelete action should restore deleted category."""
        cat = Category.objects.create(name="Cat", slug="cat", deleted=True)

        request = self._get_request_with_messages()

        queryset = Category.objects.filter(id=cat.id)
        self.admin.undelete_branch(request, queryset)

        cat.refresh_from_db()
        self.assertFalse(cat.deleted)

    def test_undelete_restores_descendants(self):
        """Undelete action should restore entire branch."""
        parent = Category.objects.create(name="Parent", slug="parent", deleted=True)
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=parent,
            deleted=True
        )
        grandchild = Category.objects.create(
            name="Grandchild",
            slug="grandchild",
            tn_parent=child,
            deleted=True
        )

        request = self._get_request_with_messages()

        queryset = Category.objects.filter(id=parent.id)
        self.admin.undelete_branch(request, queryset)

        parent.refresh_from_db()
        child.refresh_from_db()
        grandchild.refresh_from_db()

        self.assertFalse(parent.deleted)
        self.assertFalse(child.deleted)
        self.assertFalse(grandchild.deleted)

    def test_undelete_skips_non_deleted(self):
        """Undelete action should skip categories that are not deleted."""
        cat = Category.objects.create(name="Cat", slug="cat", deleted=False)
        initial_revision = cat.revision

        request = self._get_request_with_messages()

        queryset = Category.objects.filter(id=cat.id)
        self.admin.undelete_branch(request, queryset)

        cat.refresh_from_db()
        self.assertFalse(cat.deleted)
        # Revision should not change if category was not deleted
        self.assertEqual(cat.revision, initial_revision)


class CategoryFeatureCopyTests(TestCase):
    """Test automatic feature copying when creating child categories."""

    def test_child_category_copies_parent_features(self):
        """When creating a child category, parent's features should be copied."""
        # Create parent category
        parent = Category.objects.create(name="Parent", slug="parent")

        # Create features
        feature1 = Feature.objects.create(
            name="Feature 1",
            slug="feature-1",
            config={"type": "string"}
        )
        feature2 = Feature.objects.create(
            name="Feature 2",
            slug="feature-2",
            config={"type": "int"}
        )

        # Add features to parent with specific order
        CategoryFeature.objects.create(category=parent, feature=feature1, order=1)
        CategoryFeature.objects.create(category=parent, feature=feature2, order=2)

        # Create child category
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=parent
        )

        # Check that child has the same features with the same order
        child_features = list(child.category_features.all().order_by('order'))
        self.assertEqual(len(child_features), 2)
        self.assertEqual(child_features[0].feature, feature1)
        self.assertEqual(child_features[0].order, 1)
        self.assertEqual(child_features[1].feature, feature2)
        self.assertEqual(child_features[1].order, 2)

    def test_child_without_parent_has_no_features(self):
        """Root categories should not auto-copy any features."""
        # Create category without parent
        category = Category.objects.create(name="Root", slug="root")

        # Should have no features
        self.assertEqual(category.category_features.count(), 0)

    def test_feature_copy_preserves_order(self):
        """Feature order should be preserved when copying."""
        parent = Category.objects.create(name="Parent", slug="parent")

        # Create 5 features with different orders
        features = []
        for i in range(5):
            feature = Feature.objects.create(
                name=f"Feature {i}",
                slug=f"feature-{i}",
                config={"type": "string"}
            )
            features.append(feature)
            CategoryFeature.objects.create(
                category=parent,
                feature=feature,
                order=i * 10  # Orders: 0, 10, 20, 30, 40
            )

        # Create child
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=parent
        )

        # Verify order is preserved
        child_links = list(child.category_features.all().order_by('order'))
        for i, link in enumerate(child_links):
            self.assertEqual(link.feature, features[i])
            self.assertEqual(link.order, i * 10)

    def test_add_command_copies_parent_features(self):
        """Bulk add command should trigger feature copying."""
        from rest_framework.test import APIClient

        client = APIClient()
        user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='testpass123'
        )
        client.force_authenticate(user=user)

        # Create parent with features
        parent = Category.objects.create(name="Parent", slug="parent")
        feature = Feature.objects.create(
            name="Test Feature",
            slug="test-feature",
            config={"type": "bool"}
        )
        CategoryFeature.objects.create(category=parent, feature=feature, order=0)

        # Use bulk add command to create child
        data = {
            'categories': [{
                'command': 'add',
                'name': 'New Child',
                'slug': 'new-child',
                'parent_id': parent.id,
                'priority': 0
            }]
        }

        response = client.post('/catalog/api/categories/bulk-commands/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find created child
        child = Category.objects.get(slug='new-child')

        # Verify features were copied
        self.assertEqual(child.category_features.count(), 1)
        self.assertEqual(child.category_features.first().feature, feature)


class DeletedChildrenEndpointTests(TestCase):
    """Test deleted children endpoint and undelete functionality."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_deleted_children_endpoint_returns_deleted_only(self):
        """Deleted children endpoint should return only deleted children."""
        parent = Category.objects.create(name="Parent", slug="parent")
        Category.objects.create(
            name="Active Child",
            slug="active-child",
            tn_parent=parent
        )
        Category.objects.create(
            name="Deleted Child",
            slug="deleted-child",
            tn_parent=parent,
            deleted=True
        )

        response = self.client.get(f'/catalog/api/categories/{parent.id}/deleted-children/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Deleted Child")

    def test_deleted_children_endpoint_excludes_grandchildren(self):
        """Deleted children endpoint should not include deleted grandchildren."""
        parent = Category.objects.create(name="Parent", slug="parent")
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=parent,
            deleted=True
        )
        Category.objects.create(
            name="Grandchild",
            slug="grandchild",
            tn_parent=child,
            deleted=True
        )

        response = self.client.get(f'/catalog/api/categories/{parent.id}/deleted-children/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], child.id)

    def test_undelete_endpoint_restores_category(self):
        """Undelete endpoint should restore deleted category."""
        category = Category.objects.create(
            name="Category",
            slug="category",
            deleted=True
        )

        response = self.client.post(f'/catalog/api/categories/{category.pk}/undelete/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('restored', response.data)
        self.assertIn(category.pk, response.data['restored'])

        category.refresh_from_db()
        self.assertFalse(category.deleted)

    def test_undelete_endpoint_restores_descendants(self):
        """Undelete endpoint should restore entire branch."""
        parent = Category.objects.create(
            name="Parent",
            slug="parent",
            deleted=True
        )
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=parent,
            deleted=True
        )
        grandchild = Category.objects.create(
            name="Grandchild",
            slug="grandchild",
            tn_parent=child,
            deleted=True
        )

        response = self.client.post(f'/catalog/api/categories/{parent.id}/undelete/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['restored']), 3)
        self.assertIn(parent.id, response.data['restored'])
        self.assertIn(child.id, response.data['restored'])
        self.assertIn(grandchild.id, response.data['restored'])

        parent.refresh_from_db()
        child.refresh_from_db()
        grandchild.refresh_from_db()

        self.assertFalse(parent.deleted)
        self.assertFalse(child.deleted)
        self.assertFalse(grandchild.deleted)

    def test_undelete_endpoint_rejects_non_deleted(self):
        """Undelete endpoint should reject non-deleted categories."""
        category = Category.objects.create(
            name="Category",
            slug="category",
            deleted=False
        )

        response = self.client.post(f'/catalog/api/categories/{category.pk}/undelete/')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

    def test_undelete_increments_revision(self):
        """Undelete should increment revision for restored categories."""
        category = Category.objects.create(
            name="Category",
            slug="category",
            deleted=True
        )
        child = Category.objects.create(
            name="Child",
            slug="child",
            tn_parent=category,
            deleted=True
        )

        initial_parent_revision = category.revision
        initial_child_revision = child.revision

        response = self.client.post(f'/catalog/api/categories/{category.pk}/undelete/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        category.refresh_from_db()
        child.refresh_from_db()

        # Parent revision should be incremented
        self.assertGreater(category.revision, initial_parent_revision)
        # Child revision should also be incremented
        self.assertGreater(child.revision, initial_child_revision)
