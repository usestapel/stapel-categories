"""A renamed category keeps answering to the slug it had.

A category's slug is its public address (``/c/<slug>``) and its search
address (``?category=<slug>``). A catalogue that re-spells its slugs — the
case this exists for: every node becoming ``<parent-slug>-<own-slug>`` so an
address carries its ancestry — moves every one of those addresses at once,
and each of them is somebody's shared link. What is pinned here:

* the RENAME writes the alias, in the same save, whichever path performs it
  (a plain ``save``, ``load_catalog`` matching by source identity);
* ``by-slug`` answers the retired spelling with ``301`` to the current one,
  query string kept, and never redirects into a row the tree does not show;
* ``categories.by_slug`` (the comm Function search resolves through) answers
  the retired spelling with the ancestry outright, keyed as asked;
* a live row always outranks an alias of the same spelling, so a slug that
  comes back to life stops being an alias.
"""
import json
import os
import tempfile

import pytest
from django.core.management import call_command
from stapel_core.comm import call

from stapel_categories.models import Category, CategorySlugAlias, retired_slug_target

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


@pytest.fixture
def tree():
    transport = Category.objects.create(name="Transport", slug="transport", tn_priority=10)
    cars = Category.objects.create(
        name="Cars", slug="transport-cars", tn_parent=transport, tn_priority=2
    )
    new = Category.objects.create(name="New", slug="new", tn_parent=cars, tn_priority=1)
    return {"transport": transport, "cars": cars, "new": new}


def _rename(category, slug):
    row = Category.objects.get(pk=category.pk)
    row.slug = slug
    row.save()
    return row


class TestTheRenameWritesTheAlias:
    def test_a_saved_rename_keeps_the_old_slug_as_an_alias(self, tree):
        _rename(tree["new"], "transport-cars-new")

        alias = CategorySlugAlias.objects.get(slug="new")
        assert alias.category_id == tree["new"].pk
        assert Category.objects.get(pk=tree["new"].pk).slug == "transport-cars-new"

    def test_a_plain_edit_writes_nothing(self, tree):
        row = Category.objects.get(pk=tree["new"].pk)
        row.name = "Brand new"
        row.save()

        assert not CategorySlugAlias.objects.exists()

    def test_a_deferred_read_still_notices_the_rename(self, tree):
        """``only()`` leaves the slug deferred; save falls back to one read."""
        row = Category.objects.only("pk", "name").get(pk=tree["new"].pk)
        row.slug = "transport-cars-new"
        row.save()

        assert CategorySlugAlias.objects.filter(slug="new").exists()

    def test_renaming_twice_chains_both_old_spellings_to_the_row(self, tree):
        _rename(tree["new"], "transport-cars-new")
        _rename(tree["new"], "transport-cars-brand-new")

        assert {a.slug: a.category_id for a in CategorySlugAlias.objects.all()} == {
            "new": tree["new"].pk,
            "transport-cars-new": tree["new"].pk,
        }

    def test_a_slug_that_comes_back_to_life_stops_being_an_alias(self, tree):
        _rename(tree["new"], "transport-cars-new")
        _rename(tree["new"], "new")

        assert not CategorySlugAlias.objects.filter(slug="new").exists()
        assert CategorySlugAlias.objects.get(slug="transport-cars-new").category_id == tree["new"].pk

    def test_a_new_row_taking_a_retired_slug_outranks_the_alias(self, tree):
        _rename(tree["new"], "transport-cars-new")
        used = Category.objects.create(name="Used", slug="new", tn_parent=tree["cars"])

        assert not CategorySlugAlias.objects.filter(slug="new").exists()
        assert retired_slug_target("new", Category.objects.all()) is None
        assert Category.objects.get(slug="new").pk == used.pk

    def test_deleting_the_category_deletes_its_aliases(self, tree):
        _rename(tree["new"], "transport-cars-new")
        Category.objects.filter(pk=tree["new"].pk).delete()

        assert not CategorySlugAlias.objects.exists()

    def test_load_catalog_s_identity_rename_writes_the_alias_too(self, tree):
        """The path the catalogue migration takes: a fixture row matched by
        ``(external_source, external_id)`` whose slug moved."""
        row = Category.objects.get(pk=tree["new"].pk)
        row.external_source, row.external_id = "src", "165"
        row.save()
        records = [
            {"slug": "transport", "parent_slug": None, "name": "Transport", "features": []},
            {"slug": "transport-cars", "parent_slug": "transport", "name": "Cars", "features": []},
            {
                "slug": "transport-cars-new", "parent_slug": "transport-cars", "name": "New",
                "external_source": "src", "external_id": "165", "features": [],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "categories.json"), "w", encoding="utf-8") as fh:
                json.dump(records, fh)
            with open(os.path.join(directory, "features.json"), "w", encoding="utf-8") as fh:
                json.dump([], fh)
            call_command(
                "load_catalog", dir=directory, on_conflict="fixture-wins", deletions="ignore",
            )

        assert Category.objects.get(pk=tree["new"].pk).slug == "transport-cars-new"
        assert CategorySlugAlias.objects.get(slug="new").category_id == tree["new"].pk


class TestBySlugRedirects:
    def test_a_retired_slug_is_a_301_to_the_current_one(self, api_client, tree):
        _rename(tree["new"], "transport-cars-new")

        response = api_client.get(f"{BASE}/categories/by-slug/new/")

        assert response.status_code == 301
        assert response["Location"] == f"{BASE}/categories/by-slug/transport-cars-new/"
        assert response["Cache-Control"].startswith("public, max-age=")

    def test_the_query_string_rides_along(self, api_client, tree):
        _rename(tree["new"], "transport-cars-new")

        response = api_client.get(f"{BASE}/categories/by-slug/new/?f.make=toyota")

        assert response["Location"] == (
            f"{BASE}/categories/by-slug/transport-cars-new/?f.make=toyota"
        )

    def test_following_the_redirect_lands_on_the_row(self, api_client, tree):
        _rename(tree["new"], "transport-cars-new")

        response = api_client.get(f"{BASE}/categories/by-slug/new/", follow=True)

        assert response.status_code == 200
        assert response.data["id"] == tree["new"].pk
        assert response.data["slug"] == "transport-cars-new"

    def test_the_live_slug_still_answers_200(self, api_client, tree):
        _rename(tree["new"], "transport-cars-new")

        response = api_client.get(f"{BASE}/categories/by-slug/transport-cars-new/")

        assert response.status_code == 200

    def test_an_alias_of_a_soft_deleted_row_is_a_404_not_a_redirect(self, api_client, tree):
        _rename(tree["new"], "transport-cars-new")
        Category.objects.filter(pk=tree["new"].pk).update(deleted=True)

        response = api_client.get(f"{BASE}/categories/by-slug/new/")

        assert response.status_code == 404

    def test_an_unknown_slug_is_still_a_404(self, api_client, tree):
        response = api_client.get(f"{BASE}/categories/by-slug/nope/")

        assert response.status_code == 404


class TestTheCommFunctionResolvesAliases:
    def test_a_retired_slug_answers_the_ancestry_keyed_as_asked(self, tree):
        _rename(tree["new"], "transport-cars-new")

        result = call("categories.by_slug", {"slugs": ["new", "transport-cars-new"]})

        path = [str(tree["transport"].pk), str(tree["cars"].pk), str(tree["new"].pk)]
        assert result == {"new": path, "transport-cars-new": path}

    def test_a_live_row_outranks_an_alias_of_its_spelling(self, tree):
        _rename(tree["new"], "transport-cars-new")
        used = Category.objects.create(name="Used", slug="new", tn_parent=tree["cars"])

        result = call("categories.by_slug", {"slugs": ["new"]})

        assert result["new"][-1] == str(used.pk)

    def test_an_alias_of_a_soft_deleted_row_is_absent(self, tree):
        _rename(tree["new"], "transport-cars-new")
        Category.objects.filter(pk=tree["new"].pk).update(deleted=True)

        result = call("categories.by_slug", {"slugs": ["new"]})

        assert result == {}
