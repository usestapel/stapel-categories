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

    def test_carries_title_badge_translate_flags(self, db):
        # These flags MUST cross the comm boundary: stapel-attributes'
        # dto_to_dao reads them off the FeatureDef to build the title/badge
        # projections — omitting them yields empty features_title/badges
        # downstream (listings integration bug).
        category = Category.objects.create(name="Cars", slug="cars")
        feature = Feature.objects.create(
            slug="brand",
            name="Brand",
            config={"type": "string"},
            show_at_title=True,
            show_as_badge=True,
            translate="title",
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        fdef = call("categories.features", {"category_id": category.pk})["features"][0]
        assert fdef["show_at_title"] is True
        assert fdef["show_as_badge"] is True
        assert fdef["translate"] == "title"

    def test_carries_the_visibility_axis(self, db):
        # The disclosure decision MUST cross the boundary: stapel-listings
        # stamps it onto every stored value at write time, and a definition
        # arriving without it stamps `public` — which publishes the VIN.
        category = Category.objects.create(name="Cars", slug="cars")
        feature = Feature.objects.create(
            slug="vin",
            name="VIN",
            config={"type": "string", "maxLength": 17},
            mandatory=True,
            visibility="owner",
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        fdef = call("categories.features", {"category_id": category.pk})["features"][0]
        assert fdef["visibility"] == "owner"
        # Still required, still validated, still stored — only never published.
        assert fdef["mandatory"] is True

    def test_visibility_feeds_the_attributes_stamp(self, db):
        # End-to-end: the resolved payload, run through the attributes engine,
        # stamps the stored value, which is what every read path downstream
        # redacts on. A dropped axis yields an unstamped (public) DAO here.
        from stapel_attributes import normalize_to_dao

        category = Category.objects.create(name="Cars", slug="cars")
        feature = Feature.objects.create(
            slug="vin", name="VIN", config={"type": "string"}, visibility="owner"
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        configs = call("categories.features", {"category_id": category.pk})["features"]
        dao = normalize_to_dao(configs, {"vin": {"type": "string", "value": "JT2SW22N"}})
        assert dao["vin"]["visibility"] == "owner"

    def test_flags_feed_attributes_title_projection(self, db):
        # End-to-end: the resolved payload, run through the attributes engine,
        # yields a NON-empty title projection because the flags survived.
        from stapel_attributes import normalize_to_dao

        category = Category.objects.create(name="Phones", slug="phones")
        feature = Feature.objects.create(
            slug="color", name="Color", config={"type": "string"}, show_at_title=True
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        configs = call("categories.features", {"category_id": category.pk})["features"]
        dao = normalize_to_dao(configs, {"color": {"type": "string", "value": "red"}})
        assert dao["color"]["title"] is True

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
class TestPathFunction:
    """``categories.path`` — the ancestry provider stapel-search declared by
    canonical name before anything answered it (``search.W006``)."""

    def test_root_answers_a_single_segment(self, db):
        root = Category.objects.create(name="Electronics", slug="electronics")
        assert call("categories.path", {"category_ids": [root.pk]}) == {
            str(root.pk): [str(root.pk)]
        }

    def test_descendant_answers_root_first_and_itself_last(self, db):
        root = Category.objects.create(name="Electronics", slug="electronics")
        mid = Category.objects.create(name="Phones", slug="phones", tn_parent=root)
        leaf = Category.objects.create(name="Smart", slug="smart", tn_parent=mid)

        result = call("categories.path", {"category_ids": [leaf.pk]})
        assert result[str(leaf.pk)] == [str(root.pk), str(mid.pk), str(leaf.pk)]

    def test_batch_is_one_query_and_keys_are_strings(self, db):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        root = Category.objects.create(name="Electronics", slug="electronics")
        a = Category.objects.create(name="Phones", slug="phones", tn_parent=root)
        b = Category.objects.create(name="Laptops", slug="laptops", tn_parent=root)

        with CaptureQueriesContext(connection) as captured:
            result = call("categories.path", {"category_ids": [a.pk, b.pk]})
        assert len(captured) == 1, [q["sql"] for q in captured]
        assert set(result) == {str(a.pk), str(b.pk)}

    def test_unknown_id_is_absent_not_guessed(self, db):
        root = Category.objects.create(name="Electronics", slug="electronics")
        result = call("categories.path", {"category_ids": [root.pk, 999999]})
        assert set(result) == {str(root.pk)}

    def test_empty_and_non_numeric_ids_answer_empty(self, db):
        assert call("categories.path", {"category_ids": []}) == {}
        assert call("categories.path", {"category_ids": ["electronics"]}) == {}

    def test_schema_rejects_a_bare_id(self, db):
        with pytest.raises(Exception):
            call("categories.path", {"category_id": 1})


@pytest.mark.django_db
class TestBySlugFunction:
    """``categories.by_slug`` — ``categories.path`` in the other namespace.

    The contract is stapel-search 0.14.3's (``CATEGORY_SLUG_FUNCTION``): a
    flat mapping of slug -> id ancestry, absence for an unknown slug, an
    inactive row answering and a soft-deleted one not, and no error of its
    own — the consumer turns absence into its 400, so anything else here
    would turn a typo into an outage or an outage into a 400.
    """

    def test_root_answers_a_single_segment(self, db):
        root = Category.objects.create(name="Transport", slug="transport")
        assert call("categories.by_slug", {"slugs": ["transport"]}) == {
            "transport": [str(root.pk)]
        }

    def test_descendant_answers_root_first_and_itself_last(self, db):
        root = Category.objects.create(name="Transport", slug="transport")
        leaf = Category.objects.create(
            name="Cars", slug="avtomobili", tn_parent=root
        )
        result = call("categories.by_slug", {"slugs": ["avtomobili"]})
        assert result["avtomobili"] == [str(root.pk), str(leaf.pk)]

    def test_several_slugs_at_once_in_one_query(self, db):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        root = Category.objects.create(name="Transport", slug="transport")
        leaf = Category.objects.create(
            name="Cars", slug="avtomobili", tn_parent=root
        )

        with CaptureQueriesContext(connection) as captured:
            result = call(
                "categories.by_slug", {"slugs": ["transport", "avtomobili"]}
            )
        assert len(captured) == 1, [q["sql"] for q in captured]
        assert result == {
            "transport": [str(root.pk)],
            "avtomobili": [str(root.pk), str(leaf.pk)],
        }

    def test_unknown_slug_is_absent_not_an_error(self, db):
        Category.objects.create(name="Transport", slug="transport")
        result = call(
            "categories.by_slug", {"slugs": ["transport", "no-such-slug"]}
        )
        assert set(result) == {"transport"}

    def test_inactive_answers_and_deleted_does_not(self, db):
        retired = Category.objects.create(
            name="Pagers", slug="pagers", active=False
        )
        Category.objects.create(name="Fax", slug="fax", deleted=True)

        result = call("categories.by_slug", {"slugs": ["pagers", "fax"]})
        assert result == {"pagers": [str(retired.pk)]}

    def test_empty_batch_answers_empty(self, db):
        assert call("categories.by_slug", {"slugs": []}) == {}

    def test_schema_rejects_a_bare_slug(self, db):
        with pytest.raises(Exception):
            call("categories.by_slug", {"slug": "transport"})


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
        # The outbox guarantee: publish_category_changed joins save()'s atomic
        # block via mutate_and_emit, so a failing emit MUST roll the mutation
        # back — never a committed row with no announcement (which would
        # strand every downstream cache). Fail emit at the delivery seam
        # (tests run OUTBOX_ENABLED=False; with the outbox on, the failing
        # outbox write behaves the same — covered in stapel-core).
        def boom(event):
            raise RuntimeError("comm backend down")

        monkeypatch.setattr("stapel_core.comm.actions.deliver", boom)

        before = Category.objects.count()
        with pytest.raises(RuntimeError):
            Category.objects.create(name="Doomed", slug="doomed")

        assert Category.objects.count() == before
        assert not Category.objects.filter(slug="doomed").exists()

    def test_swallowed_emit_failure_still_cannot_commit_the_row(self, monkeypatch):
        # C1 adversarial: even a caller that swallows the emit failure must
        # not end up with a committed-but-unannounced category — the emit
        # failure already sank the save's atomic block it was part of (and
        # with the outbox on, core additionally marks the transaction
        # rollback-only).
        def boom(event):
            raise RuntimeError("comm backend down")

        monkeypatch.setattr("stapel_core.comm.actions.deliver", boom)

        from django.db import transaction

        with transaction.atomic():
            try:
                Category.objects.create(name="Doomed", slug="doomed2")
            except RuntimeError:
                pass  # the C1 anti-pattern — swallow and hope

        assert not Category.objects.filter(slug="doomed2").exists()


@pytest.fixture
def clothing_tree(db):
    """The three-parent case a type-ahead has to get right.

    «Шорты» is not one category. It is a leaf under men's, under women's and
    under children's clothing, and the only thing that tells them apart in a
    dropdown is the ancestor path.
    """
    clothes = Category.objects.create(name="Одежда", slug="odezhda")
    kids = Category.objects.create(name="Детям", slug="detyam")
    tree = {}
    for parent, name, slug in (
        (clothes, "Мужская одежда", "muzhskaya-odezhda"),
        (clothes, "Женская одежда", "zhenskaya-odezhda"),
        (kids, "Детская одежда", "detskaya-odezhda"),
    ):
        branch = Category.objects.create(name=name, slug=slug, tn_parent=parent)
        tree[slug] = Category.objects.create(
            name="Шорты", slug=f"{slug}-shorty", tn_parent=branch
        )
    tree["одежда"] = clothes
    tree["детям"] = kids
    return tree


@pytest.mark.django_db
class TestSuggestFunction:
    """``categories.suggest`` — names in, nodes with their ancestry out."""

    def _paths(self, result):
        return {tuple(row["path"]) for row in result["categories"]}

    def test_one_word_answers_every_parent_path(self, clothing_tree):
        result = call("categories.suggest", {"terms": ["шорты"]})

        assert self._paths(result) == {
            ("Одежда", "Мужская одежда", "Шорты"),
            ("Одежда", "Женская одежда", "Шорты"),
            ("Детям", "Детская одежда", "Шорты"),
        }
        assert all(row["depth"] == 3 for row in result["categories"])
        assert all(row["match"] == "exact" for row in result["categories"])

    def test_path_ids_are_the_ancestry_as_ids(self, clothing_tree):
        result = call("categories.suggest", {"terms": ["шорты"]})
        row = next(
            r for r in result["categories"] if r["path"][1] == "Мужская одежда"
        )
        leaf = clothing_tree["muzhskaya-odezhda"]
        assert row["id"] == leaf.pk
        assert row["path_ids"] == [
            str(clothing_tree["одежда"].pk),
            str(leaf.tn_parent.pk),
            str(leaf.pk),
        ]
        assert len(row["path_ids"]) == len(row["path"])

    def test_the_four_match_kinds_are_graded_apart(self, clothing_tree):
        """The grade is the caller's ranking key, so each kind has to be earned.

        «Сифоны» is the live case for the last one: transliterating «iphone»
        yields «ифон», which sits inside «сифоны» and inside nothing else in
        a 3583-node catalogue — so on the stand it was the ONE suggestion a
        buyer typing «iphone» got. A mid-word hit and a word-boundary hit are
        different evidence and this is where they stop sorting the same.
        """
        Category.objects.create(name="Сифоны", slug="sifony")

        result = call("categories.suggest", {"terms": ["одежда"]})
        by_name = {row["name"]: row for row in result["categories"]}
        assert by_name["Одежда"]["match"] == "exact"
        assert by_name["Мужская одежда"]["match"] == "word"

        assert [
            (row["name"], row["match"])
            for row in call("categories.suggest", {"terms": ["ифон"]})["categories"]
        ] == [("Сифоны", "substring")]

        graded = call("categories.suggest", {"terms": ["одеж"]})["categories"]
        assert (graded[0]["name"], graded[0]["match"]) == ("Одежда", "prefix")
        assert {(row["name"], row["match"]) for row in graded[1:]} == {
            ("Детская одежда", "word"),
            ("Женская одежда", "word"),
            ("Мужская одежда", "word"),
        }

    def test_the_result_cap_keeps_the_best_matches_not_the_shallowest(self, db):
        """A deep exact hit must survive a cap a shallow mid-word hit would fill.

        The caller does the ranking, and it can only rank what it was given:
        capping by depth alone dropped «Мужская одежда › Шорты» before
        `/suggest` ever saw it while keeping three nodes that merely contain
        the word.
        """
        root = Category.objects.create(name="Спорт", slug="sport")
        for index in range(3):
            Category.objects.create(
                name=f"Брюки и шорты {index}", slug=f"bryuki-{index}", tn_parent=root
            )
        branch = Category.objects.create(name="Одежда", slug="odezhda", tn_parent=root)
        deep = Category.objects.create(name="Шорты", slug="shorty", tn_parent=branch)

        result = call("categories.suggest", {"terms": ["шорты"], "limit": 1})

        assert [row["id"] for row in result["categories"]] == [deep.pk]
        assert result["categories"][0]["match"] == "exact"

    def test_yo_folds_to_ye(self, db):
        Category.objects.create(name="Одежда для беременных", slug="pregnancy")
        Category.objects.create(name="Ёлки", slug="yolki")
        result = call("categories.suggest", {"terms": ["елки"]})
        assert [row["name"] for row in result["categories"]] == ["Ёлки"]

    def test_inactive_test_and_deleted_are_excluded(self, db):
        Category.objects.create(name="Шорты", slug="live")
        Category.objects.create(name="Шорты off", slug="off", active=False)
        Category.objects.create(name="Шорты test", slug="test", is_test=True)
        gone = Category.objects.create(name="Шорты gone", slug="gone")
        gone.deleted = True
        gone.save()

        result = call("categories.suggest", {"terms": ["шорты"]})
        assert [row["slug"] for row in result["categories"]] == ["live"]

    def test_a_retired_ancestor_hides_a_live_leaf(self, db):
        retired = Category.objects.create(name="Архив", slug="arhiv", active=False)
        Category.objects.create(name="Шорты", slug="shorty", tn_parent=retired)
        assert call("categories.suggest", {"terms": ["шорты"]})["categories"] == []

    def test_no_match_and_empty_terms_answer_empty(self, clothing_tree):
        assert call("categories.suggest", {"terms": ["квадрокоптер"]})["categories"] == []
        assert call("categories.suggest", {"terms": []})["categories"] == []

    def test_query_count_does_not_grow_with_the_answer(self, clothing_tree):
        """The N+1 gate: three matches and six cost the same two queries."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as three:
            call("categories.suggest", {"terms": ["шорты"]})

        for slug in ("muzhskaya-odezhda", "zhenskaya-odezhda", "detskaya-odezhda"):
            Category.objects.create(
                name="Шорты пляжные",
                slug=f"{slug}-beach",
                tn_parent=clothing_tree[slug].tn_parent,
            )
        with CaptureQueriesContext(connection) as six:
            result = call("categories.suggest", {"terms": ["шорты"]})

        assert len(result["categories"]) == 6
        # Two: the fingerprint aggregate, then the tree read the new rows
        # invalidated. Neither grows with the number of matches.
        assert len(six) == len(three) == 2, [q["sql"] for q in six]

    def test_an_unchanged_tree_costs_one_query(self, clothing_tree):
        """The index is cached under the tree's revision, not per call."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        call("categories.suggest", {"terms": ["шорты"]})
        with CaptureQueriesContext(connection) as warm:
            call("categories.suggest", {"terms": ["одежда"]})
        assert len(warm) == 1, [q["sql"] for q in warm]

    def test_a_new_category_is_suggestable_immediately(self, clothing_tree):
        """A revision-keyed cache must not outlive the tree it describes."""
        call("categories.suggest", {"terms": ["велосипед"]})
        Category.objects.create(name="Велосипеды", slug="velosipedy")
        result = call("categories.suggest", {"terms": ["велосипед"]})
        assert [row["name"] for row in result["categories"]] == ["Велосипеды"]

    def test_limit_caps_and_the_cap_is_deterministic(self, clothing_tree):
        first = call("categories.suggest", {"terms": ["шорты"], "limit": 2})
        second = call("categories.suggest", {"terms": ["шорты"], "limit": 2})
        assert len(first["categories"]) == 2
        assert first == second

    def test_schema_rejects_an_unknown_key(self, db):
        with pytest.raises(Exception):
            call("categories.suggest", {"q": "шорты"})


@pytest.mark.django_db
class TestNamesFunction:
    """``categories.names`` — ids in, display names out.

    stapel-search's goods-driven suggest rows carry category path IDS and had
    no fleet Function to resolve them to display names (``categories.path``
    answers id-paths, ``categories.suggest`` matches terms — neither answers
    "what is 163 called?"). The consumer is wired against exactly this shape:
    ``{"ids": [...]}`` -> ``{"names": {"<id>": {"name", "slug"}}}`` with
    STRING keys (a JSON round trip must not change key types), deleted and
    unknown ids simply absent.
    """

    def test_resolves_ids_to_name_and_slug(self, db):
        electronics = Category.objects.create(name="Electronics", slug="electronics")
        phones = Category.objects.create(
            name="Phones", slug="phones", tn_parent=electronics
        )
        result = call("categories.names", {"ids": [electronics.pk, str(phones.pk)]})
        assert result["names"] == {
            str(electronics.pk): {"name": "Electronics", "slug": "electronics"},
            str(phones.pk): {"name": "Phones", "slug": "phones"},
        }

    def test_deleted_and_unknown_ids_are_omitted(self, db):
        live = Category.objects.create(name="Live", slug="live")
        dead = Category.objects.create(name="Dead", slug="dead", deleted=True)
        result = call("categories.names", {"ids": [live.pk, dead.pk, 999999]})
        assert set(result["names"]) == {str(live.pk)}

    def test_inactive_rows_still_resolve(self, db):
        # A listing can sit in a category that was later retired; its rows
        # still need a caption. Only DELETED rows are gone.
        retired = Category.objects.create(name="Retired", slug="retired", active=False)
        result = call("categories.names", {"ids": [retired.pk]})
        assert result["names"][str(retired.pk)]["name"] == "Retired"

    def test_non_numeric_ids_answer_empty_not_error(self, db):
        assert call("categories.names", {"ids": ["electronics"]}) == {"names": {}}
        assert call("categories.names", {"ids": []}) == {"names": {}}

    def test_schema_caps_the_batch_at_200(self, db):
        with pytest.raises(Exception):
            call("categories.names", {"ids": list(range(201))})

    def test_schema_rejects_an_unknown_key(self, db):
        with pytest.raises(Exception):
            call("categories.names", {"category_ids": [1]})


@pytest.fixture
def cascade_tree(db):
    """The storefront cascade, one of every state a rung has to handle.

    Two live roots (priorities apart), a retired root and a deleted root that
    must not be rungs at all; under ``vehicles`` a live branch, a live leaf
    and a retired child; under ``cars`` one live and one retired grandchild —
    the pair that tells a truthful ``children_count`` from a raw one.
    """
    vehicles = Category.objects.create(
        name="Vehicles", slug="vehicles", tn_priority=10
    )
    property_ = Category.objects.create(
        name="Property", slug="property", tn_priority=5
    )
    retired_root = Category.objects.create(
        name="Retired root", slug="retired-root", active=False
    )
    Category.objects.create(name="Gone root", slug="gone-root", deleted=True)
    cars = Category.objects.create(
        name="Cars", slug="cars", tn_parent=vehicles, tn_priority=2
    )
    bikes = Category.objects.create(
        name="Bikes", slug="bikes", tn_parent=vehicles, tn_priority=1
    )
    Category.objects.create(
        name="Retired child", slug="retired-child", tn_parent=vehicles, active=False
    )
    Category.objects.create(
        name="Gone child", slug="gone-child", tn_parent=vehicles, deleted=True
    )
    sedans = Category.objects.create(name="Sedans", slug="sedans", tn_parent=cars)
    Category.objects.create(
        name="Retired sedan", slug="retired-sedan", tn_parent=cars, active=False
    )
    return {
        "vehicles": vehicles, "property": property_, "retired_root": retired_root,
        "cars": cars, "bikes": bikes, "sedans": sedans,
    }


@pytest.mark.django_db
class TestChildrenFunction:
    """``categories.children`` — one rung of the cascade, as a person sees it.

    svc-agent walks the tree the way a buyer walks the storefront: "give me
    the children of node X", null for the top. The consumer is wired against
    exactly this shape: ``{"parent_id": <int|null>}`` ->
    ``{"parent_id": <int|null>, "children": [{id, slug, name,
    children_count}]}`` where ``children_count`` counts ACTIVE children (0
    means leaf, so the walker needs no second call to know it hit bottom).
    """

    def test_null_parent_answers_the_active_roots(self, cascade_tree):
        result = call("categories.children", {"parent_id": None})

        assert result["parent_id"] is None
        assert [row["slug"] for row in result["children"]] == [
            "vehicles", "property"
        ]

    def test_absent_parent_means_the_roots_too(self, cascade_tree):
        assert call("categories.children", {}) == call(
            "categories.children", {"parent_id": None}
        )

    def test_children_of_a_mid_node(self, cascade_tree):
        vehicles = cascade_tree["vehicles"]
        result = call("categories.children", {"parent_id": vehicles.pk})

        assert result["parent_id"] == vehicles.pk
        by_slug = {row["slug"]: row for row in result["children"]}
        assert set(by_slug) == {"cars", "bikes"}
        cars = by_slug["cars"]
        assert cars["id"] == cascade_tree["cars"].pk
        assert cars["name"] == "Cars"
        # Two grandchildren exist, ONE is active — the count a walker plans
        # its next rung on must be the count of rungs it will actually get.
        assert cars["children_count"] == 1

    def test_a_leaf_answers_an_empty_list(self, cascade_tree):
        leaf = cascade_tree["bikes"]
        result = call("categories.children", {"parent_id": leaf.pk})
        assert result == {"parent_id": leaf.pk, "children": []}

    def test_inactive_children_are_excluded_from_list_and_counts(
        self, cascade_tree
    ):
        result = call("categories.children", {"parent_id": cascade_tree["vehicles"].pk})
        slugs = {row["slug"] for row in result["children"]}
        assert "retired-child" not in slugs and "gone-child" not in slugs
        # And a live child leading only to retired rungs reads as a leaf.
        assert {
            row["slug"]: row["children_count"] for row in result["children"]
        } == {"cars": 1, "bikes": 0}

    def test_missing_parent_raises_lookup(self, db):
        from stapel_core.comm.exceptions import FunctionCallError

        with pytest.raises(FunctionCallError) as excinfo:
            call("categories.children", {"parent_id": 999999})
        assert isinstance(excinfo.value.__cause__, LookupError)

    def test_an_inactive_parent_raises_lookup_like_a_missing_one(
        self, cascade_tree
    ):
        # A retired node is not a rung: "no such node" and "retired node"
        # answer the same, and both are distinct from a leaf's empty list.
        from stapel_core.comm.exceptions import FunctionCallError

        with pytest.raises(FunctionCallError) as excinfo:
            call(
                "categories.children",
                {"parent_id": cascade_tree["retired_root"].pk},
            )
        assert isinstance(excinfo.value.__cause__, LookupError)

    def test_ordering_matches_the_http_tree_view(self, db):
        # The HTTP children/roots views order by (-tn_priority, id) — the
        # cascade a person sees. Same rungs, same order, or the agent and
        # the buyer walk two different storefronts.
        root = Category.objects.create(name="Root", slug="root")
        low = Category.objects.create(
            name="Low", slug="low", tn_parent=root, tn_priority=1
        )
        high = Category.objects.create(
            name="High", slug="high", tn_parent=root, tn_priority=9
        )
        tie_a = Category.objects.create(
            name="Tie A", slug="tie-a", tn_parent=root, tn_priority=5
        )
        tie_b = Category.objects.create(
            name="Tie B", slug="tie-b", tn_parent=root, tn_priority=5
        )

        result = call("categories.children", {"parent_id": root.pk})
        assert [row["id"] for row in result["children"]] == [
            high.pk, min(tie_a.pk, tie_b.pk), max(tie_a.pk, tie_b.pk), low.pk
        ]

    def test_names_go_through_the_display_translator_seam(self, cascade_tree):
        # The module stores translation keys; a raw key in a cascade would
        # hand the agent "categories.electronics" as a rung caption. Same
        # seam categories.names and categories.suggest render through.
        from django.test import override_settings

        with override_settings(
            STAPEL_CATEGORIES={
                "DISPLAY_TRANSLATOR": (
                    "stapel_categories.tests.test_extension_points.loud_translator"
                ),
            }
        ):
            result = call("categories.children", {"parent_id": None})
        assert [row["name"] for row in result["children"]] == [
            "T(Vehicles)", "T(Property)"
        ]

    def test_schema_rejects_an_unknown_key(self, db):
        with pytest.raises(Exception):
            call("categories.children", {"category_id": 1})

    def test_schema_rejects_a_string_parent(self, db):
        with pytest.raises(Exception):
            call("categories.children", {"parent_id": "vehicles"})


@pytest.mark.django_db
class TestAMalformedIdIsALookupError:
    """An id that is not an id must come back as ``LookupError``.

    ``Category.pk`` is an ``AutoField``, so ``objects.get(pk="32/149/163")``
    raises ``ValueError`` — not ``DoesNotExist``. It therefore walked straight
    past ``features_function``'s own ``except`` clause and surfaced as an
    unhandled fault, when every caller in the fleet is written against the
    ``LookupError`` this module's docstrings promise: stapel-listings'
    re-projection counts it as ``category_unresolved`` and its publish path
    turns it into a 400.

    That promise cannot be conditional on ``VALIDATE_SCHEMAS`` being on — the
    payload schemas do type ``category_id`` as an integer, but a contract that
    only holds while a runtime flag is set is not a contract. These call the
    handlers directly, past the schema, which is exactly the condition being
    pinned.

    Three drafts on one live stand (243/244/245) carry
    ``category_id = "32/149/163"`` — a search PATH in the id column — which is
    how the shape got measured. stapel-listings 0.19.0 stops the write; this
    stops the read from being a 500.
    """

    MALFORMED = ["32/149/163", "not-an-id", "", None, 1.5, [163]]

    @pytest.mark.parametrize("category_id", MALFORMED)
    def test_features_answers_lookup_error(self, db, category_id):
        from stapel_categories.functions import features_function

        with pytest.raises(LookupError):
            features_function({"category_id": category_id})

    @pytest.mark.parametrize("parent_id", ["32/149/163", "not-an-id", 1.5])
    def test_children_answers_lookup_error(self, db, parent_id):
        """The same hazard, the same answer: ``categories.children`` resolved
        its ``parent_id`` with a bare ``filter(pk=…).exists()``."""
        from stapel_categories.functions import children_function

        with pytest.raises(LookupError):
            children_function({"parent_id": parent_id})

    def test_children_still_answers_the_roots_for_a_null_parent(self, db):
        """The regression guard: ``None`` means "the roots", and it must not
        be swept up as a malformed id."""
        from stapel_categories.functions import children_function

        Category.objects.create(name="Root", slug="root-for-null-parent")

        result = children_function({"parent_id": None})

        assert [c["slug"] for c in result["children"]] == ["root-for-null-parent"]

    def test_a_real_id_still_resolves(self, category_with_feature):
        from stapel_categories.functions import features_function

        category, _ = category_with_feature
        assert features_function({"category_id": category.pk})["features"]
