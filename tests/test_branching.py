"""Children that are not rows: expanded values and pointers.

Two additions to what "the level below" means, and the reason each of them
is not a second tree:

* a branch whose children are the VALUES of one of its own features
  (``children_expand_by``) answers with virtual children and creates no rows
  — the option set stays the one place those names live;
* a pointer (``CategoryLink``) puts another node among a category's
  children, positioned by ``order``, with the target's own address and
  breadcrumbs behind it.

What is pinned here is what a 0.21.7 server could not answer: the two shapes
in both public reads, the position a pointer takes, the two refusals of a
mis-authored expansion, and — the one with a live failure behind it — a
catalogue reload that rewrites its own links and leaves the operator's.
"""
import json
import os
import tempfile

import pytest
from django.core.management import call_command

from stapel_categories import catalog_fixtures as cf
from stapel_categories.models import Category, CategoryFeature, CategoryLink, Feature

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


@pytest.fixture
def staff_client(api_client):
    """The operator's own surface — links and the expansion column are staff."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_superuser(
        username="operator", email="operator@example.com", password="x"
    )
    api_client.force_authenticate(user=user)
    return api_client


def make_select(slug: str, values) -> Feature:
    """A feature with a closed option set — the enumerable kind."""
    return Feature.objects.create(
        name=f"feature.{slug}",
        slug=slug,
        config={
            "type": "select",
            "options": [
                {"value": value, "label": f"option.{value}"} for value in values
            ],
        },
    )


def attach(category: Category, feature: Feature, order: int = 0) -> None:
    CategoryFeature.objects.get_or_create(
        category=category, feature=feature, defaults={"order": order}
    )


@pytest.fixture
def expanded():
    """A leaf whose children are the values of its own ``brand`` feature."""
    cars = Category.objects.create(name="Cars", slug="cars")
    used = Category.objects.create(name="Used", slug="used", tn_parent=cars)
    brand = make_select("brand", ["alfa", "beta"])
    attach(used, brand)
    used.children_expand_by = "brand"
    used.save()
    return Category.objects.get(pk=used.pk)


@pytest.fixture
def linked():
    """Two roots and a pointer from the first into the second's subtree."""
    cars = Category.objects.create(name="Cars", slug="cars", tn_priority=10)
    services = Category.objects.create(name="Services", slug="services", tn_priority=5)
    new = Category.objects.create(
        name="New", slug="new", tn_parent=cars, tn_priority=2
    )
    used = Category.objects.create(
        name="Used", slug="used", tn_parent=cars, tn_priority=1
    )
    rent = Category.objects.create(name="Rent", slug="rent", tn_parent=services)
    link = CategoryLink.objects.create(
        source=cars, target=rent, order=1, label="link.rent",
        external_source="importer",
    )
    return {
        "cars": cars, "services": services, "new": new, "used": used,
        "rent": rent, "link": link,
    }


class TestVirtualChildren:
    """A branch that expands one of its features into a level."""

    def test_children_are_the_values_and_no_rows_were_created(
        self, api_client, expanded
    ):
        before = Category.objects.count()

        rows = api_client.get(f"{BASE}/categories/{expanded.pk}/children/").json()

        assert [row["name"] for row in rows] == ["option.alfa", "option.beta"]
        assert all(row["virtual"] is True for row in rows)
        assert all("id" not in row and "slug" not in row for row in rows)
        assert [row["filter"] for row in rows] == [
            {"brand": "alfa"}, {"brand": "beta"}
        ]
        assert Category.objects.count() == before

    def test_the_tree_draws_them_as_the_level_below(self, api_client, expanded):
        roots = api_client.get(f"{BASE}/tree/?depth=3").json()

        used = roots[0]["children"][0]
        assert used["slug"] == "used"
        assert used["children_count"] == 2
        assert used["children_as"] == "tiles"
        assert [child["value"] for child in used["children"]] == ["alfa", "beta"]

    def test_a_feature_with_no_option_set_expands_to_nothing(self, expanded):
        from stapel_categories.branching import virtual_children

        free_text = Feature.objects.create(
            name="feature.note", slug="note", config={"type": "string"}
        )
        attach(expanded, free_text, order=1)
        expanded.children_expand_by = "note"

        assert virtual_children(expanded) == []


class TestLinkedChildren:
    """A pointer is drawn among the children, at the position it says."""

    def test_it_sits_at_its_order_among_the_real_children(self, api_client, linked):
        rows = api_client.get(f"{BASE}/categories/{linked['cars'].pk}/children/").json()

        # order=1 -> between the two real children, not appended after them.
        assert [row["slug"] for row in rows] == ["new", "rent", "used"]
        assert rows[1]["linked"] is True
        assert rows[1]["name"] == "link.rent"
        assert rows[1]["id"] == linked["rent"].pk
        # Breadcrumbs are the target's: its parent is Services, not Cars.
        assert rows[1]["tn_parent"] == linked["services"].pk
        assert "linked" not in rows[0]

    def test_an_empty_label_falls_back_to_the_targets_name(self, api_client, linked):
        linked["link"].label = ""
        linked["link"].save()

        rows = api_client.get(f"{BASE}/categories/{linked['cars'].pk}/children/").json()

        assert rows[1]["name"] == "Rent"

    def test_the_tree_draws_it_among_the_children_and_counts_it(
        self, api_client, linked
    ):
        roots = api_client.get(f"{BASE}/tree/?depth=3").json()

        cars = next(row for row in roots if row["slug"] == "cars")
        assert [child["slug"] for child in cars["children"]] == [
            "new", "rent", "used"
        ]
        assert cars["children"][1]["linked"] is True
        # A pointer's own subtree is not expanded here — it is a destination.
        assert cars["children"][1]["children"] == []
        assert cars["children_count"] == 3

    def test_a_pointer_at_a_hidden_target_is_not_drawn(self, api_client, linked):
        linked["rent"].deleted = True
        linked["rent"].save()

        rows = api_client.get(f"{BASE}/categories/{linked['cars'].pk}/children/").json()

        assert [row["slug"] for row in rows] == ["new", "used"]

    def test_staff_creates_and_deletes_one(self, staff_client, linked):
        url = f"{BASE}/categories/{linked['services'].pk}/links/"

        created = staff_client.post(
            url,
            {"target": linked["used"].pk, "order": 0, "external_source": "storefront"},
            format="json",
        )
        assert created.status_code == 201, created.content
        assert CategoryLink.objects.filter(
            source=linked["services"], target=linked["used"]
        ).exists()

        gone = staff_client.delete(f"{url}{linked['used'].pk}/")
        assert gone.status_code == 204
        assert not CategoryLink.objects.filter(
            source=linked["services"], target=linked["used"]
        ).exists()

    def test_a_link_to_self_is_refused(self, staff_client, linked):
        response = staff_client.post(
            f"{BASE}/categories/{linked['cars'].pk}/links/",
            {"target": linked["cars"].pk},
            format="json",
        )

        assert response.status_code == 400


class TestExpansionValidation:
    """The two shapes that would answer nothing, refused where they are made."""

    def test_a_branch_cannot_also_expand(self, expanded):
        from stapel_categories.branching import EXPAND_BY_ON_BRANCH, expansion_error

        parent = Category.objects.get(slug="cars")
        parent.children_expand_by = "brand"

        code, message = expansion_error(parent)
        assert code == EXPAND_BY_ON_BRANCH
        assert "real children" in message

    def test_a_feature_the_category_does_not_have_is_refused(self, expanded):
        from stapel_categories.branching import EXPAND_BY_UNUSABLE, expansion_error

        expanded.children_expand_by = "colour"

        code, _message = expansion_error(expanded)
        assert code == EXPAND_BY_UNUSABLE

    def test_the_system_check_reports_both(self, expanded):
        from stapel_categories.checks import check_children_expand_by

        expanded.children_expand_by = "colour"
        expanded.save()
        parent = Category.objects.get(slug="cars")
        parent.children_expand_by = "brand"
        parent.save()

        warnings = check_children_expand_by(None)

        assert {w.id for w in warnings} == {"stapel_categories.E001"}
        assert len(warnings) == 2

    def test_the_staff_write_refuses_it(self, staff_client, expanded):
        response = staff_client.patch(
            f"{BASE}/categories/{expanded.pk}/",
            {"children_expand_by": "colour"},
            format="json",
        )

        assert response.status_code == 400, response.content
        assert Category.objects.get(pk=expanded.pk).children_expand_by == "brand"


class TestFixtures:
    """The three new pieces of catalogue content, exported and reloaded."""

    def test_the_new_category_keys_travel(self, expanded):
        expanded.children_axis_tag = "BrandTag"
        expanded.save()

        _features, categories, _state = cf.build_catalog()

        record = next(r for r in categories if r["slug"] == "used")
        assert record["children_axis_tag"] == "BrandTag"
        assert record["children_expand_by"] == "brand"
        # Written only when set — a plain node keeps the hash it had.
        assert "children_axis_tag" not in next(
            r for r in categories if r["slug"] == "cars"
        )

    def test_links_export_to_their_own_file(self, linked):
        records = cf.build_links()

        assert records == [{
            "source": "cars", "target": "rent", "order": 1,
            "label": "link.rent", "external_source": "importer",
        }]

    def test_a_reload_rewrites_its_own_links_and_keeps_the_operators(self, linked):
        """The discipline ``children_as`` already follows, one table over.

        An importer owns the links it stamped its name on. A link an operator
        authored in the admin carries ``storefront``, is in no fixture, and
        has to survive a re-import — the failure this pins is the one where a
        catalogue reload silently takes an operator's curation with it.
        """
        operator_link = CategoryLink.objects.create(
            source=linked["cars"], target=linked["services"],
            order=0, external_source="storefront",
        )

        with tempfile.TemporaryDirectory() as out:
            call_command("export_catalog", out=out)
            # The importer drops its link and re-emits the file.
            with open(os.path.join(out, cf.LINKS_FILE), "w", encoding="utf-8") as fh:
                fh.write(cf.canonical_json([{
                    "source": "cars", "target": "new", "order": 0,
                    "label": "", "external_source": "importer",
                }]))
            call_command("load_catalog", dir=out)

        assert CategoryLink.objects.filter(pk=operator_link.pk).exists()
        assert not CategoryLink.objects.filter(
            source=linked["cars"], target=linked["rent"]
        ).exists()
        assert CategoryLink.objects.filter(
            source=linked["cars"], target=linked["new"],
            external_source="importer",
        ).exists()

    def test_a_directory_without_the_file_touches_no_link(self, linked):
        """Silence is not an instruction to delete."""
        with tempfile.TemporaryDirectory() as out:
            call_command("export_catalog", out=out)
            os.remove(os.path.join(out, cf.LINKS_FILE))
            call_command("load_catalog", dir=out)

        assert CategoryLink.objects.filter(pk=linked["link"].pk).exists()

    def test_a_link_end_this_catalogue_lacks_is_an_error(self, linked):
        from stapel_categories.catalog_load import load_catalog

        with tempfile.TemporaryDirectory() as out:
            call_command("export_catalog", out=out)
            with open(os.path.join(out, cf.LINKS_FILE), "w", encoding="utf-8") as fh:
                fh.write(cf.canonical_json([{
                    "source": "cars", "target": "nowhere", "order": 0,
                    "label": "", "external_source": "importer",
                }]))
            report = load_catalog(out)

        assert report.errors == 1
        assert report.failed

    def test_an_endpoint_is_resolved_by_external_id_first(self, linked):
        """The id survives a source-side rename the slug column has moved."""
        from stapel_categories.catalog_load import _resolve_link_endpoint

        linked["rent"].external_id = "77"
        linked["rent"].save()

        assert _resolve_link_endpoint("no-such-slug", "77") == linked["rent"]

    def test_the_round_trip_of_a_link_is_byte_stable(self, linked):
        with tempfile.TemporaryDirectory() as out:
            call_command("export_catalog", out=out)
            with open(os.path.join(out, cf.LINKS_FILE), encoding="utf-8") as fh:
                first = fh.read()
            call_command("load_catalog", dir=out)
            call_command("export_catalog", out=out, force=True)
            with open(os.path.join(out, cf.LINKS_FILE), encoding="utf-8") as fh:
                second = fh.read()

        assert json.loads(first) == json.loads(second)
