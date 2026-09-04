"""The effective schema of a `chips` parent — the intersection of its children.

A `chips` parent renders the feed and the chip row for the whole partition,
so "what can be filtered here" is a question about the CHILDREN. Until this
existed the parent answered with its own links, which on such a node are
empty: the cars page offered no filters and the composer opened no fields
until a chip was picked.

Four things are pinned here, against each other:

* what the intersection IS — every feature all children carry, in the order
  the module already applies, and nothing a single child carries alone;
* what happens where the children DISAGREE — the widest config of theirs and
  a `divergent` flag, never a silent pick of one child's bounds;
* that nothing else moved — a leaf, a `tiles` parent and a `chips` parent
  that declares its own features answer exactly what they answered before;
* that both readers (HTTP and the `categories.features` Function) answer the
  same thing, since the composer reads one and the search plan the other.
"""
import pytest

from stapel_categories.effective import (
    EFFECTIVE_FROM_CHILDREN,
    EFFECTIVE_FROM_OWN,
    effective_features,
    merge_configs,
)
from stapel_categories.functions import features_function
from stapel_categories.models import Category, CategoryFeature, Feature

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"

#: The cars-like fixture: two children of one template sharing this many keys.
SHARED_KEYS = 43


def make_feature(slug: str, config=None, mandatory=False, rules=None) -> Feature:
    return Feature.objects.create(
        name=f"feature.{slug}",
        slug=slug,
        config=config or {"type": "string"},
        mandatory=mandatory,
        rules=rules or [],
    )


def override(root: Feature, config=None, mandatory=False, rules=None) -> Feature:
    """A per-child version of an existing feature, the way `inherit` makes one.

    A root slug is unique, so two children carrying different configs under
    one key means one root and a child Feature per variation — exactly what
    the feature editor's `inherit` action creates.
    """
    return Feature.objects.create(
        name=root.name,
        slug=root.slug,
        tn_parent=root,
        config=config if config is not None else dict(root.config),
        mandatory=mandatory,
        rules=rules or [],
    )


def link(category: Category, *features: Feature) -> None:
    """Set a category's OWN links to exactly *features*, in order.

    Replaces rather than adds: ``copy_parent_features`` stamps the parent's
    links onto every new child, so a test that only added would be measuring
    the fixture instead of the case.
    """
    CategoryFeature.objects.filter(category=category).delete()
    for order, feature in enumerate(features):
        CategoryFeature.objects.create(category=category, feature=feature, order=order)


def reload(category: Category) -> Category:
    """Re-read the row so treenode's tn_* counters are the committed ones."""
    return Category.objects.get(pk=category.pk)


@pytest.fixture
def cars():
    """`Автомобили` (no own features) over `С пробегом` / `Новые`.

    The two children share :data:`SHARED_KEYS` slugs and each carries a few
    of its own — the shape the live catalogue has (Jaccard 0.57).
    """
    parent = Category.objects.create(name="Cars", slug="cars", children_as="chips")
    used = Category.objects.create(name="Used", slug="cars-used", tn_parent=parent)
    new = Category.objects.create(name="New", slug="cars-new", tn_parent=parent)

    shared = [make_feature(f"shared-{index:02d}") for index in range(SHARED_KEYS)]
    used_own = [make_feature(f"used-{index}") for index in range(3)]
    new_own = [make_feature(f"new-{index}") for index in range(2)]

    link(used, *shared, *used_own)
    link(new, *shared, *new_own)
    return reload(parent), reload(used), reload(new)


class TestIntersection:
    def test_the_parent_answers_every_key_both_children_carry(self, cars):
        parent, _used, _new = cars

        features, source = effective_features(parent)

        assert source == EFFECTIVE_FROM_CHILDREN
        assert [f.slug for f in features] == [
            f"shared-{index:02d}" for index in range(SHARED_KEYS)
        ]

    def test_a_key_only_one_child_carries_is_not_in_it(self, cars):
        parent, _used, _new = cars

        features, _ = effective_features(parent)

        slugs = {f.slug for f in features}
        assert not slugs & {"used-0", "used-1", "used-2", "new-0", "new-1"}

    def test_the_order_is_the_one_the_module_already_applies(self, cars):
        """No second ordering: the reference child's own, filtered.

        The composer orders required-bearing blocks first and required first
        inside a block off THIS list — it reads the same list for a chips
        parent as for the leaf under it, so the two cannot order differently.
        """
        parent, used, _new = cars
        reference = [f.slug for f in used.get_all_features()]

        features, _ = effective_features(parent)

        common = {f.slug for f in features}
        assert [f.slug for f in features] == [s for s in reference if s in common]

    def test_a_parent_with_no_children_left_falls_back_to_its_own(self, cars):
        parent, used, new = cars
        Category.objects.filter(pk__in=[used.pk, new.pk]).update(active=False)

        _features, source = effective_features(reload(parent))

        assert source == EFFECTIVE_FROM_OWN


class TestDivergence:
    def test_bounds_widen_and_the_feature_is_flagged(self, cars):
        parent, used, new = cars
        mileage = make_feature("mileage", {"type": "int", "min": 0, "max": 500000})
        mileage_used = override(mileage, {"type": "int", "min": 0, "max": 500000})
        mileage_new = override(mileage, {"type": "int", "min": 10, "max": 1000})
        link(used, *list(used.get_all_features()), mileage_used)
        link(new, *list(new.get_all_features()), mileage_new)

        features, _ = effective_features(reload(parent))

        (mileage,) = [f for f in features if f.slug == "mileage"]
        assert mileage.divergent is True
        assert mileage.config["min"] == 0
        assert mileage.config["max"] == 500000

    def test_option_lists_are_unioned(self, cars):
        parent, used, new = cars
        fuel = make_feature("fuel", {"type": "select", "options": []})
        used_fuel = override(
            fuel, {"type": "select", "options": [{"value": "petrol"}, {"value": "lpg"}]}
        )
        new_fuel = override(
            fuel, {"type": "select", "options": [{"value": "petrol"}, {"value": "ev"}]}
        )
        link(used, *list(used.get_all_features()), used_fuel)
        link(new, *list(new.get_all_features()), new_fuel)

        features, _ = effective_features(reload(parent))

        (fuel,) = [f for f in features if f.slug == "fuel"]
        assert [option["value"] for option in fuel.config["options"]] == [
            "petrol",
            "lpg",
            "ev",
        ]
        assert fuel.divergent is True

    def test_a_bound_unbounded_in_one_child_is_unbounded_here(self):
        merged, divergent = merge_configs(
            [{"type": "int", "max": 10}, {"type": "int"}]
        )

        assert divergent is True
        assert "max" not in merged

    def test_required_in_only_one_child_is_not_required_here(self, cars):
        parent, used, new = cars
        vin = make_feature("vin")
        link(used, *list(used.get_all_features()), override(vin, mandatory=True))
        link(new, *list(new.get_all_features()), override(vin, mandatory=False))

        features, _ = effective_features(reload(parent))

        (vin,) = [f for f in features if f.slug == "vin"]
        assert vin.mandatory is False
        assert vin.divergent is True

    def test_children_that_agree_are_not_flagged(self, cars):
        parent, _used, _new = cars

        features, _ = effective_features(parent)

        assert not [f for f in features if getattr(f, "divergent", False)]

    def test_nothing_is_written_back(self, cars):
        """The overlays are in memory; the children keep their own configs."""
        parent, used, new = cars
        seats = make_feature("seats", {"type": "int"})
        link(used, *list(used.get_all_features()), override(seats, {"type": "int", "max": 5}))
        link(new, *list(new.get_all_features()), override(seats, {"type": "int", "max": 9}))

        effective_features(reload(parent))

        stored = sorted(
            Feature.objects.filter(slug="seats", tn_parent__isnull=False).values_list(
                "config", flat=True
            ),
            key=lambda config: config["max"],
        )
        assert [config["max"] for config in stored] == [5, 9]


class TestNothingElseMoved:
    def test_a_leaf_answers_its_own(self):
        leaf = Category.objects.create(name="Leaf", slug="leaf")
        link(leaf, make_feature("colour"))

        features, source = effective_features(reload(leaf))

        assert source == EFFECTIVE_FROM_OWN
        assert [f.slug for f in features] == ["colour"]

    def test_a_tiles_parent_answers_its_own(self):
        parent = Category.objects.create(name="Shelf", slug="shelf", children_as="tiles")
        Category.objects.create(name="Kid", slug="shelf-kid", tn_parent=parent)
        link(parent, make_feature("brand"))

        features, source = effective_features(reload(parent))

        assert source == EFFECTIVE_FROM_OWN
        assert [f.slug for f in features] == ["brand"]

    def test_a_chips_parent_with_own_features_keeps_them_alone(self, cars):
        """"Own only" — never own PLUS the intersection.

        The two together would be a third schema nobody authored, and a
        parent carrying its own links has already had the decision made by
        hand.
        """
        parent, _used, _new = cars
        link(parent, make_feature("own-key"))

        features, source = effective_features(reload(parent))

        assert source == EFFECTIVE_FROM_OWN
        assert [f.slug for f in features] == ["own-key"]


class TestHttpRead:
    def test_the_effective_schema_comes_back_with_its_source(self, api_client, cars):
        parent, _used, _new = cars

        response = api_client.get(f"{BASE}/categories/{parent.pk}/features/")

        assert response.status_code == 200
        assert response["X-Effective-From"] == "children"
        assert [item["slug"] for item in response.json()] == [
            f"shared-{index:02d}" for index in range(SHARED_KEYS)
        ]

    def test_divergent_rides_on_the_feature_it_describes(self, api_client, cars):
        parent, used, new = cars
        mileage = make_feature("mileage", {"type": "int"})
        link(used, *list(used.get_all_features()), override(mileage, {"type": "int", "max": 9}))
        link(new, *list(new.get_all_features()), override(mileage, {"type": "int", "max": 99}))

        payload = api_client.get(f"{BASE}/categories/{parent.pk}/features/").json()

        by_slug = {item["slug"]: item for item in payload}
        assert by_slug["mileage"]["divergent"] is True
        assert by_slug["mileage"]["config"]["max"] == 99
        # Absent, not false: an agreeing feature reads as it always did.
        assert "divergent" not in by_slug["shared-00"]

    def test_a_leaf_reads_exactly_as_before(self, api_client):
        leaf = Category.objects.create(name="Leaf", slug="leaf-http")
        link(leaf, make_feature("colour"))

        response = api_client.get(f"{BASE}/categories/{leaf.pk}/features/")

        assert response["X-Effective-From"] == "own"
        (payload,) = response.json()
        assert payload["slug"] == "colour"
        assert "divergent" not in payload


class TestFunctionRead:
    def test_it_answers_what_the_http_read_answers(self, api_client, cars):
        parent, _used, _new = cars

        result = features_function({"category_id": parent.pk})
        http = api_client.get(f"{BASE}/categories/{parent.pk}/features/").json()

        assert result["effective_from"] == "children"
        assert [f["slug"] for f in result["features"]] == [i["slug"] for i in http]

    def test_a_leaf_still_says_own(self):
        leaf = Category.objects.create(name="Leaf", slug="leaf-fn")
        link(leaf, make_feature("colour"))

        result = features_function({"category_id": leaf.pk})

        assert result["effective_from"] == "own"
        assert result["revision"] == reload(leaf).revision

    def test_the_revision_covers_the_children_it_intersected(self, cars):
        """A child's edit must move the number a consumer caches by.

        The intersection is a fact about the children; a child's save bumps
        the CHILD's revision and not the parent's, so a parent-only number
        would leave a consumer holding a stale schema forever.
        """
        parent, used, _new = cars
        before = features_function({"category_id": parent.pk})["revision"]

        used.name = "Used, edited"
        used.save()

        after = features_function({"category_id": parent.pk})["revision"]
        assert after > before

    def test_divergent_crosses_the_comm_boundary(self, cars):
        parent, used, new = cars
        doors = make_feature("doors", {"type": "int"})
        link(used, *list(used.get_all_features()), override(doors, {"type": "int", "max": 3}))
        link(new, *list(new.get_all_features()), override(doors, {"type": "int", "max": 5}))

        result = features_function({"category_id": parent.pk})

        by_slug = {f["slug"]: f for f in result["features"]}
        assert by_slug["doors"]["divergent"] is True
        assert by_slug["doors"]["config"]["max"] == 5
        assert "divergent" not in by_slug["shared-00"]
