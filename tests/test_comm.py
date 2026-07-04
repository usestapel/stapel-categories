"""comm surface: the ``categories.features`` Function and the
``category.changed`` Action, exercised in-process with schema validation ON
(see conftest ``VALIDATE_SCHEMAS``)."""
import pytest
from stapel_core.comm import call, emit, subscribe_action

from stapel_categories.models import Category, CategoryFeature, Feature


@pytest.fixture
def category_with_feature(db):
    category = Category.objects.create(name="Electronics", slug="electronics")
    feature = Feature.objects.create(
        slug="color", name="Color", config={"type": "string"}, mandatory=True
    )
    CategoryFeature.objects.create(category=category, feature=feature, order=0)
    return category, feature


@pytest.mark.django_db
class TestFeaturesFunction:
    def test_resolves_feature_defs(self, category_with_feature):
        category, feature = category_with_feature
        result = call("categories.features", {"category_id": category.pk})

        assert result["category_id"] == category.pk
        assert result["revision"] == category.revision
        assert len(result["features"]) == 1
        fdef = result["features"][0]
        assert fdef["slug"] == "color"
        assert fdef["mandatory"] is True
        # config is merged with the type's defaults by stapel-attributes
        assert fdef["config"]["type"] == "string"

    def test_includes_inherited_features(self, category_with_feature):
        parent, parent_feature = category_with_feature
        child = Category.objects.create(name="Phones", slug="phones", tn_parent=parent)
        own = Feature.objects.create(slug="storage", name="Storage", config={"type": "int"})
        CategoryFeature.objects.create(category=child, feature=own, order=0)

        result = call("categories.features", {"category_id": child.pk})
        slugs = [f["slug"] for f in result["features"]]
        # own feature first, then inherited parent feature
        assert "storage" in slugs and "color" in slugs

    def test_missing_category_raises_lookup(self, db):
        # call() wraps the handler's LookupError in FunctionCallError; the
        # original is preserved as __cause__.
        from stapel_core.comm.exceptions import FunctionCallError

        with pytest.raises(FunctionCallError) as excinfo:
            call("categories.features", {"category_id": 999999})
        assert isinstance(excinfo.value.__cause__, LookupError)

    def test_schema_rejects_bad_payload(self, db):
        # category_id must be an integer — schema validation (VALIDATE_SCHEMAS)
        # rejects a string.
        with pytest.raises(Exception):
            call("categories.features", {"category_id": "not-an-int"})


@pytest.mark.django_db
class TestCategoryChangedAction:
    def test_emitted_on_category_save(self):
        received = []
        subscribe_action("category.changed", lambda event: received.append(event.payload))

        category = Category.objects.create(name="Toys", slug="toys")

        assert any(p["category_id"] == category.pk for p in received)
        payload = next(p for p in received if p["category_id"] == category.pk)
        assert payload["revision"] == category.revision

    def test_emitted_on_feature_save_for_each_category(self, category_with_feature):
        category, feature = category_with_feature
        received = []
        subscribe_action("category.changed", lambda event: received.append(event.payload))

        # Saving the feature must invalidate every category referencing it.
        feature.name = "Colour"
        feature.save()

        assert any(p["category_id"] == category.pk for p in received)

    def test_payload_matches_schema(self):
        # emit directly to prove the committed schema accepts the shape.
        received = []
        subscribe_action("category.changed", lambda event: received.append(event.payload))
        emit("category.changed", {"category_id": 1, "revision": 2})
        assert {"category_id": 1, "revision": 2} in received

    def test_exactly_one_event_per_category_save(self):
        received = []
        subscribe_action("category.changed", lambda event: received.append(event.payload))

        category = Category.objects.create(name="Games", slug="games")

        # A single root-category save announces itself exactly once (the
        # copy_parent_features signal does nothing for a root).
        mine = [p for p in received if p["category_id"] == category.pk]
        assert len(mine) == 1

    def test_failing_emit_rolls_back_the_mutation(self, monkeypatch):
        # The outbox guarantee: emit runs inside save()'s atomic block, so if
        # it raises the mutation MUST roll back — never a committed row with no
        # announcement (which would strand every downstream cache).
        def boom(*args, **kwargs):
            raise RuntimeError("comm backend down")

        monkeypatch.setattr("stapel_core.comm.emit", boom)

        before = Category.objects.count()
        with pytest.raises(RuntimeError):
            Category.objects.create(name="Doomed", slug="doomed")

        assert Category.objects.count() == before
        assert not Category.objects.filter(slug="doomed").exists()
