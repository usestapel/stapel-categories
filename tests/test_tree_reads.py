"""The two rungs the server could not answer, and the rule all three share.

A storefront walks the category tree from the top: show the root tiles, then
resolve the slug in the URL, then list that category's children. The third
had an endpoint (``children``) from the first release. The first two did not,
so a client that wanted either had exactly one way to ask — list the whole
table and filter it client-side. On a real catalogue that is hundreds of
kilobytes of JSON to render a row of tiles, and the cold ``/c`` page measured
21 seconds.

What is pinned here:

* ``roots`` returns top-level categories and nothing else;
* ``by-slug`` resolves the storefront's own URL vocabulary to one object;
* and all three reads honour ONE visibility rule, asserted by comparing them
  against each other rather than by restating the filter three times — two of
  these endpoints are new, and the way this drifts is somebody changing one
  copy of a filter that was pasted three times.
"""
import pytest

from stapel_categories.models import Category

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


@pytest.fixture
def anonymous_client(api_client):
    """No credentials — the caller this surface mostly serves."""
    return api_client


@pytest.fixture
def tree():
    """Two roots, two children, one of each in a state that must not show."""
    vehicles = Category.objects.create(name="Vehicles", slug="vehicles", tn_priority=10)
    property_ = Category.objects.create(name="Property", slug="property", tn_priority=5)
    gone_root = Category.objects.create(name="Gone", slug="gone-root", deleted=True)
    cars = Category.objects.create(
        name="Cars", slug="cars", tn_parent=vehicles, tn_priority=2
    )
    bikes = Category.objects.create(
        name="Bikes", slug="bikes", tn_parent=vehicles, tn_priority=1
    )
    gone_child = Category.objects.create(
        name="Gone child", slug="gone-child", tn_parent=vehicles, deleted=True
    )
    return {
        "vehicles": vehicles, "property": property_, "gone_root": gone_root,
        "cars": cars, "bikes": bikes, "gone_child": gone_child,
    }


class TestRoots:
    def test_it_returns_only_top_level_categories(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        assert response.status_code == 200
        slugs = [row["slug"] for row in response.data]
        assert slugs == ["vehicles", "property"]

    def test_a_child_never_appears_among_the_roots(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        slugs = {row["slug"] for row in response.data}
        assert "cars" not in slugs and "bikes" not in slugs

    def test_a_deleted_root_is_not_a_root(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        assert "gone-root" not in {row["slug"] for row in response.data}

    def test_it_sorts_by_priority_descending(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        priorities = [row["tn_priority"] for row in response.data]
        assert priorities == sorted(priorities, reverse=True)

    def test_it_is_open_to_a_client_with_no_credentials(self, anonymous_client, tree):
        assert anonymous_client.get(f"{BASE}/categories/roots/").status_code == 200

    def test_it_is_not_paginated(self, anonymous_client, tree):
        """A catalogue's roots are tens of rows; a page envelope would be noise."""
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        assert isinstance(response.data, list)

    def test_it_carries_a_cache_control_header(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        assert "max-age=" in response["Cache-Control"]
        assert response["Cache-Control"].startswith("public")

    def test_an_empty_catalogue_is_an_empty_list_not_an_error(
        self, anonymous_client
    ):
        response = anonymous_client.get(f"{BASE}/categories/roots/")

        assert response.status_code == 200
        assert response.data == []


class TestRootsCacheIsRetiredByAnEdit:
    """A TTL alone would make an edit invisible until the clock ran out."""

    def test_a_new_root_appears_without_waiting_out_the_timeout(
        self, anonymous_client, tree
    ):
        first = anonymous_client.get(f"{BASE}/categories/roots/")
        assert "jobs" not in {row["slug"] for row in first.data}

        Category.objects.create(name="Jobs", slug="jobs")

        second = anonymous_client.get(f"{BASE}/categories/roots/")
        assert "jobs" in {row["slug"] for row in second.data}

    def test_a_deleted_root_disappears_without_waiting_either(
        self, anonymous_client, tree
    ):
        first = anonymous_client.get(f"{BASE}/categories/roots/")
        assert "property" in {row["slug"] for row in first.data}

        tree["property"].delete()

        second = anonymous_client.get(f"{BASE}/categories/roots/")
        assert "property" not in {row["slug"] for row in second.data}


class TestBySlug:
    def test_it_resolves_a_slug_to_one_category(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/by-slug/vehicles/")

        assert response.status_code == 200
        assert response.data["slug"] == "vehicles"
        assert response.data["id"] == tree["vehicles"].id

    def test_it_returns_an_object_not_a_list_of_one(self, anonymous_client, tree):
        """`slug` is unique — this is an alternate primary key, not a search."""
        response = anonymous_client.get(f"{BASE}/categories/by-slug/vehicles/")

        assert isinstance(response.data, dict)

    def test_a_child_resolves_too(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/by-slug/cars/")

        assert response.status_code == 200
        assert response.data["id"] == tree["cars"].id

    def test_an_unknown_slug_is_a_404(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/by-slug/nope/")

        assert response.status_code == 404

    def test_a_deleted_category_is_a_404_not_a_row(self, anonymous_client, tree):
        """It answers 404 because the tree does not show it."""
        response = anonymous_client.get(f"{BASE}/categories/by-slug/gone-root/")

        assert response.status_code == 404

    def test_the_404_carries_a_localizable_error_key(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/by-slug/nope/")

        body = response.json()
        assert "categories_slug_not_found" in str(body)

    def test_it_is_open_to_a_client_with_no_credentials(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/by-slug/vehicles/")

        assert response.status_code == 200

    def test_it_carries_a_cache_control_header(self, anonymous_client, tree):
        response = anonymous_client.get(f"{BASE}/categories/by-slug/vehicles/")

        assert response["Cache-Control"].startswith("public")

    def test_the_id_route_still_works(self, anonymous_client, tree):
        """by-slug is an addition; it must not shadow the numeric detail route."""
        response = anonymous_client.get(
            f"{BASE}/categories/{tree['vehicles'].id}/"
        )

        assert response.status_code == 200
        assert response.data["slug"] == "vehicles"


class TestOneVisibilityRule:
    """The three tree reads must agree about which catalogue they are showing.

    Asserted by comparing the endpoints against each other, not by restating
    the filter — a restated filter is a fourth copy that can drift too.
    """

    def test_a_root_visible_in_roots_resolves_by_slug(self, anonymous_client, tree):
        roots = anonymous_client.get(f"{BASE}/categories/roots/")

        for row in roots.data:
            resolved = anonymous_client.get(
                f"{BASE}/categories/by-slug/{row['slug']}/"
            )
            assert resolved.status_code == 200, row["slug"]

    def test_a_child_visible_in_children_resolves_by_slug(
        self, anonymous_client, tree
    ):
        children = anonymous_client.get(
            f"{BASE}/categories/{tree['vehicles'].id}/children/"
        )

        for row in children.data:
            resolved = anonymous_client.get(
                f"{BASE}/categories/by-slug/{row['slug']}/"
            )
            assert resolved.status_code == 200, row["slug"]

    def test_deleted_is_hidden_by_all_three(self, anonymous_client, tree):
        roots = anonymous_client.get(f"{BASE}/categories/roots/")
        children = anonymous_client.get(
            f"{BASE}/categories/{tree['vehicles'].id}/children/"
        )
        by_slug = anonymous_client.get(f"{BASE}/categories/by-slug/gone-child/")

        assert "gone-root" not in {r["slug"] for r in roots.data}
        assert "gone-child" not in {r["slug"] for r in children.data}
        assert by_slug.status_code == 404

    def test_inactive_is_shown_by_all_three_as_children_always_has(
        self, anonymous_client, tree
    ):
        """`active` is a flag on the row, not a visibility gate on the tree.

        Hiding an inactive category here would open a hole under the live
        categories beneath it. The serializer ships `active`, so a client
        that wants to grey one out can.
        """
        inactive_root = Category.objects.create(
            name="Quiet", slug="quiet", active=False
        )
        inactive_child = Category.objects.create(
            name="Quiet child", slug="quiet-child",
            tn_parent=tree["vehicles"], active=False,
        )

        roots = anonymous_client.get(f"{BASE}/categories/roots/")
        children = anonymous_client.get(
            f"{BASE}/categories/{tree['vehicles'].id}/children/"
        )
        by_slug = anonymous_client.get(f"{BASE}/categories/by-slug/quiet/")

        assert "quiet" in {r["slug"] for r in roots.data}
        assert "quiet-child" in {r["slug"] for r in children.data}
        assert by_slug.status_code == 200
        assert by_slug.data["active"] is False
        assert inactive_root.id and inactive_child.id  # created, not filtered away

    def test_is_test_is_shown_by_all_three_it_is_an_export_filter(
        self, anonymous_client, tree
    ):
        """`is_test` excludes a row from committed fixtures, not from the API.

        Pinned so that "hide test rows" is a deliberate decision made in one
        place (`visible_categories`) if it is ever wanted, rather than an
        accident of which endpoint somebody wrote last.
        """
        Category.objects.create(name="Scratch", slug="scratch", is_test=True)

        roots = anonymous_client.get(f"{BASE}/categories/roots/")
        by_slug = anonymous_client.get(f"{BASE}/categories/by-slug/scratch/")

        assert "scratch" in {r["slug"] for r in roots.data}
        assert by_slug.status_code == 200

    def test_children_still_carries_a_cache_header_like_the_new_two(
        self, anonymous_client, tree
    ):
        response = anonymous_client.get(
            f"{BASE}/categories/{tree['vehicles'].id}/children/"
        )

        assert response["Cache-Control"].startswith("public")
