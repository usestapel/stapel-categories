"""`children_as`: the resolved read, the derivation, and the tree endpoint.

Three things are pinned here, and they are pinned against each other:

* what a READER is told — never `auto`, `null` on a leaf, and no query per
  row to say it;
* what the DERIVATION decides, per signal, and that it never touches a value
  somebody authored;
* what `GET /tree/` returns, including the keys it must never carry;
* what the catalogue FIXTURE carries, since a value that does not survive the
  round trip is a decision the next image forgets.
"""
import io
import tempfile

import pytest
from django.core.management import call_command
from django.test.utils import CaptureQueriesContext
from django.db import connection

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl

from stapel_categories.management.commands.derive_children_as import (
    AXIS_LABEL_KEYS,
    axis_label_for,
    derive,
    jaccard,
    vocabulary_group,
)
from stapel_categories.models import Category, CategoryFeature, Feature

from .test_catalog_load import (
    _CatalogTestCase,
    _export,
    _read,
    _read_json,
    _wipe_db,
    _write_json,
)

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


def make_feature(slug: str) -> Feature:
    return Feature.objects.create(
        name=f"feature.{slug}", slug=slug, config={"type": "string"}
    )


def link(category: Category, *slugs: str) -> None:
    """Set a category's OWN feature links to exactly *slugs*.

    Replaces rather than adds: `copy_parent_features` stamps the parent's
    links onto every new child, so a test that only added would be measuring
    the fixture instead of the case.
    """
    CategoryFeature.objects.filter(category=category).delete()
    for order, slug in enumerate(slugs):
        feature = Feature.objects.filter(slug=slug).first() or make_feature(slug)
        CategoryFeature.objects.create(category=category, feature=feature, order=order)


class TestResolvedValue:
    def test_a_leaf_answers_null(self):
        leaf = Category.objects.create(name="Leaf", slug="leaf")

        assert leaf.resolved_children_as is None

    def test_auto_with_children_falls_back_to_tiles(self):
        parent = Category.objects.create(name="Parent", slug="parent")
        Category.objects.create(name="Child", slug="child", tn_parent=parent)

        assert Category.objects.get(pk=parent.pk).resolved_children_as == "tiles"

    def test_the_derivation_cache_answers_for_auto(self):
        parent = Category.objects.create(name="Parent", slug="parent-2")
        Category.objects.create(name="Child", slug="child-2", tn_parent=parent)
        Category.objects.filter(pk=parent.pk).update(children_as_derived="chips")

        assert Category.objects.get(pk=parent.pk).resolved_children_as == "chips"

    def test_an_authored_value_beats_the_cache(self):
        parent = Category.objects.create(
            name="Parent", slug="parent-3", children_as="tiles"
        )
        Category.objects.create(name="Child", slug="child-3", tn_parent=parent)
        Category.objects.filter(pk=parent.pk).update(children_as_derived="chips")

        assert Category.objects.get(pk=parent.pk).resolved_children_as == "tiles"

    def test_an_authored_value_on_a_leaf_is_still_null(self):
        """`children_as` describes children; with none there is nothing to say."""
        leaf = Category.objects.create(
            name="Leaf", slug="leaf-2", children_as="chips"
        )

        assert leaf.resolved_children_as is None


class TestSerializedValue:
    def test_auto_never_reaches_a_reader(self, api_client):
        parent = Category.objects.create(name="Parent", slug="parent-4")
        Category.objects.create(name="Child", slug="child-4", tn_parent=parent)

        rows = api_client.get(f"{BASE}/categories/roots/").json()

        assert [row["children_as"] for row in rows] == ["tiles"]

    def test_a_leaf_serializes_null(self, api_client):
        Category.objects.create(name="Leaf", slug="leaf-3")

        rows = api_client.get(f"{BASE}/categories/roots/").json()

        assert rows[0]["children_as"] is None

    def test_the_key_costs_no_query_per_row(self, api_client):
        """The read is three columns already on the row, so a bigger page is
        the same number of queries — the point of not deriving on read."""
        def count_queries(root_count: int) -> int:
            Category.objects.all().delete()
            for index in range(root_count):
                root = Category.objects.create(
                    name=f"Root {index}", slug=f"n-root-{index}"
                )
                Category.objects.create(
                    name=f"Kid {index}", slug=f"n-kid-{index}", tn_parent=root
                )
            with CaptureQueriesContext(connection) as ctx:
                response = api_client.get(f"{BASE}/categories/roots/")
                assert response.status_code == 200
                assert len(response.json()) == root_count
            return len(ctx)

        small = count_queries(2)
        large = count_queries(12)

        assert small == large


class TestJaccard:
    def test_two_empty_sets_are_identical(self):
        assert jaccard(set(), set()) == 1.0

    def test_disjoint_sets_are_zero(self):
        assert jaccard({"a"}, {"b"}) == 0.0

    def test_the_partial_case(self):
        assert jaccard({"a", "b", "c"}, {"a", "b", "d"}) == 0.5


class TestVocabulary:
    def test_a_matching_child_set_names_its_group(self):
        assert vocabulary_group(["Куплю", "Продам", "Сдам"]) == "transaction"

    def test_one_matching_name_among_others_is_not_a_partition(self):
        """The signal is about the SET: a shelf with one 'Новые' on it is a
        shelf, and reading a single name as a partition hides the rest."""
        assert vocabulary_group(["Новые", "Ноутбуки", "Планшеты"]) is None

    def test_a_single_child_is_not_a_partition(self):
        assert vocabulary_group(["Продам"]) is None

    def test_case_and_spacing_do_not_matter(self):
        assert vocabulary_group(["ДЛЯ  МАЛЬЧИКОВ", "для девочек"]) == "childrens-gender"


class TestDerivation:
    @pytest.fixture
    def cars(self):
        """A partition the SCHEMA signal sees: the same template, split."""
        root = Category.objects.create(name="Cars", slug="cars")
        new = Category.objects.create(name="New", slug="cars-new", tn_parent=root)
        used = Category.objects.create(name="Used", slug="cars-used", tn_parent=root)
        link(root)
        link(new, "make", "model", "year", "body")
        link(used, "make", "model", "year", "mileage")
        return root, [new, used]

    @pytest.fixture
    def electronics(self):
        """Real subcategories: the schemas diverge and the names say nothing."""
        root = Category.objects.create(name="Electronics", slug="electronics")
        phones = Category.objects.create(
            name="Phones", slug="phones", tn_parent=root
        )
        fridges = Category.objects.create(
            name="Fridges", slug="fridges", tn_parent=root
        )
        link(root)
        link(phones, "make", "memory", "screen")
        link(fridges, "volume", "no_frost", "colour")
        return root, [phones, fridges]

    @pytest.fixture
    def realty(self):
        """A partition only the NAMES see: schemas are far apart."""
        root = Category.objects.create(name="Flats", slug="flats")
        buy = Category.objects.create(name="Куплю", slug="flats-buy", tn_parent=root)
        rent = Category.objects.create(name="Сдам", slug="flats-rent", tn_parent=root)
        link(root)
        link(buy, "rooms", "area", "floor")
        link(rent, "deposit", "term", "furnished", "pets")
        return root, [buy, rent]

    @staticmethod
    def links_for(*categories):
        return {
            category.pk: set(
                CategoryFeature.objects.filter(category=category).values_list(
                    "feature__slug", flat=True
                )
            )
            for category in categories
        }

    def test_the_schema_signal_fires_on_an_overlapping_template(self, cars):
        root, children = cars

        decision, signal, overlap, _ = derive(
            root, children, self.links_for(root, *children)
        )

        assert decision == "chips"
        assert signal == "schema"
        assert overlap >= 0.5

    def test_diverging_schemas_stay_tiles(self, electronics):
        root, children = electronics

        decision, signal, overlap, _ = derive(
            root, children, self.links_for(root, *children)
        )

        assert decision == "tiles"
        assert signal == "none"
        assert overlap < 0.5

    def test_the_vocabulary_signal_fires_where_the_schema_one_does_not(self, realty):
        root, children = realty

        decision, signal, overlap, group = derive(
            root, children, self.links_for(root, *children)
        )

        assert decision == "chips"
        assert signal == "vocabulary"
        assert group == "transaction"
        assert overlap < 0.5

    def test_a_child_with_children_of_its_own_is_a_branch(self, cars):
        """The veto: with the names silent, schema overlap alone cannot make
        a chip row of a shelf whose children have children."""
        root, children = cars
        grandchild = Category.objects.create(
            name="Sedans", slug="sedans", tn_parent=children[0]
        )

        decision, signal, _, _ = derive(
            root,
            children,
            self.links_for(root, *children),
            branch_pks={children[0].pk},
        )

        assert (decision, signal) == ("tiles", "structure")
        assert grandchild.tn_parent_id == children[0].pk

    @pytest.fixture
    def real_estate(self):
        """The live shape the first rule got wrong.

        Four children that spell one transaction partition, two of which are
        branches of their own. The names carry the set; the branches are
        parents in their own right.
        """
        root = Category.objects.create(name="Квартиры", slug="kvartiry")
        sell = Category.objects.create(
            name="Продам", slug="kvartiry-sell", tn_parent=root
        )
        rent_out = Category.objects.create(
            name="Сдам", slug="kvartiry-rent-out", tn_parent=root
        )
        buy = Category.objects.create(
            name="Куплю", slug="kvartiry-buy", tn_parent=root
        )
        rent = Category.objects.create(
            name="Сниму", slug="kvartiry-rent", tn_parent=root
        )
        Category.objects.create(
            name="Вторичка", slug="kvartiry-sell-resale", tn_parent=sell
        )
        Category.objects.create(
            name="Новостройка", slug="kvartiry-sell-new", tn_parent=sell
        )
        Category.objects.create(
            name="Длительно", slug="kvartiry-rent-out-long", tn_parent=rent_out
        )
        link(root)
        link(sell, "rooms", "area", "floor", "price")
        link(rent_out, "rooms", "area", "deposit", "term")
        link(buy, "rooms", "budget")
        link(rent, "rooms", "term", "pets", "furnished")
        return root, [sell, rent_out, buy, rent]

    def test_the_vocabulary_beats_the_structure_veto(self, real_estate):
        """Продам/Сдам having children of their own does not stop the four
        from being one partition — it makes each of them a parent too."""
        root, children = real_estate

        decision, signal, _, group = derive(
            root,
            children,
            self.links_for(root, *children),
            branch_pks={children[0].pk, children[1].pk},
        )

        assert (decision, signal, group) == (
            "chips", "vocabulary>structure", "transaction",
        )

    def test_a_vocabulary_child_with_children_is_decided_on_its_own(
        self, real_estate
    ):
        """The branch under a chip row is derived by the same rules, not
        inherited from the parent's decision."""
        root, children = real_estate
        sell = children[0]
        grandchildren = list(
            Category.objects.filter(tn_parent=sell).order_by("slug")
        )
        link(grandchildren[0], "floor", "area")
        link(grandchildren[1], "deadline", "developer")

        decision, signal, _, _ = derive(
            sell, grandchildren, self.links_for(sell, *grandchildren), branch_pks=set()
        )

        assert (decision, signal) == ("tiles", "none")

    def test_an_unmodelled_split_is_chips(self):
        """Nothing anywhere carries a schema — the children are not diverging
        in one, they are a bare split of the parent's own page."""
        root = Category.objects.create(name="Toys", slug="toys")
        boys = Category.objects.create(
            name="Для мальчиков", slug="toys-boys", tn_parent=root
        )
        girls = Category.objects.create(
            name="Для девочек", slug="toys-girls", tn_parent=root
        )
        link(root)
        link(boys)
        link(girls)

        decision, signal, _, group = derive(
            root, [boys, girls], self.links_for(root, boys, girls)
        )

        assert (decision, signal, group) == ("chips", "empty-schema", "childrens-gender")

    def test_without_the_attribute_engine_the_names_still_decide(self, realty):
        """The degrade path: no schema comparison, vocabulary alone."""
        root, children = realty

        decision, signal, overlap, group = derive(
            root,
            children,
            {},
            schema_signal=False,
        )

        assert (decision, signal, group) == ("chips", "vocabulary", "transaction")
        assert overlap is None

    def test_without_the_attribute_engine_an_unnamed_split_is_tiles(self, cars):
        """`New`/`Used` in English is not in the vocabulary — and with the
        schema signal gone there is nothing else, so the safe answer wins."""
        root, children = cars

        decision, signal, _, _ = derive(root, children, {}, schema_signal=False)

        assert (decision, signal) == ("tiles", "none")


class TestDeriveCommand:
    @pytest.fixture
    def catalogue(self):
        root = Category.objects.create(name="Cars", slug="cars")
        new = Category.objects.create(name="New", slug="cars-new", tn_parent=root)
        used = Category.objects.create(name="Used", slug="cars-used", tn_parent=root)
        link(root)
        link(new, "make", "model", "year", "body")
        link(used, "make", "model", "year", "mileage")

        other = Category.objects.create(name="Electronics", slug="electronics")
        phones = Category.objects.create(name="Phones", slug="phones", tn_parent=other)
        fridges = Category.objects.create(
            name="Fridges", slug="fridges", tn_parent=other
        )
        link(other)
        link(phones, "make", "memory", "screen")
        link(fridges, "volume", "no_frost", "colour")
        return {"cars": root, "electronics": other}

    def test_a_dry_run_writes_nothing(self, catalogue, capsys):
        call_command("derive_children_as")

        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "cars" in out
        assert Category.objects.get(slug="cars").children_as_derived == ""

    def test_the_report_names_the_signal_and_the_overlap(self, catalogue, capsys):
        call_command("derive_children_as")

        lines = [
            line for line in capsys.readouterr().out.splitlines() if " cars" in line
        ]
        assert len(lines) == 1
        assert "chips" in lines[0]
        assert "schema" in lines[0]
        assert "0.60" in lines[0]

    def test_apply_writes_the_derived_column_only(self, catalogue):
        call_command("derive_children_as", "--apply")

        cars = Category.objects.get(slug="cars")
        assert cars.children_as == "auto"
        assert cars.children_as_derived == "chips"
        assert cars.resolved_children_as == "chips"
        assert Category.objects.get(slug="electronics").children_as_derived == "tiles"

    def test_an_authored_value_is_never_overwritten(self, catalogue, capsys):
        Category.objects.filter(slug="cars").update(children_as="tiles")

        call_command("derive_children_as", "--apply")

        cars = Category.objects.get(slug="cars")
        assert cars.children_as == "tiles"
        assert cars.children_as_derived == ""
        assert cars.resolved_children_as == "tiles"

    def test_an_authored_row_is_reported_as_such(self, catalogue, capsys):
        Category.objects.filter(slug="cars").update(children_as="tiles")

        call_command("derive_children_as")

        line = next(
            line for line in capsys.readouterr().out.splitlines() if " cars" in line
        )
        assert "authored" in line

    def test_a_second_run_is_a_no_op(self, catalogue, capsys):
        call_command("derive_children_as", "--apply")
        capsys.readouterr()

        call_command("derive_children_as", "--apply")

        assert "Nothing to write." in capsys.readouterr().out

    def test_a_leaf_is_not_in_the_report(self, catalogue, capsys):
        call_command("derive_children_as")

        out = capsys.readouterr().out
        assert "cars/cars-new" not in out

    def test_the_report_names_the_override_and_the_branch_gets_its_own_line(
        self, capsys
    ):
        """A partition whose children have children: the parent is chips with
        the override named, and each branch child is derived on its own."""
        root = Category.objects.create(name="Квартиры", slug="kvartiry")
        sell = Category.objects.create(
            name="Продам", slug="kvartiry-sell", tn_parent=root
        )
        Category.objects.create(name="Сдам", slug="kvartiry-rent", tn_parent=root)
        resale = Category.objects.create(
            name="Вторичка", slug="kvartiry-resale", tn_parent=sell
        )
        new_build = Category.objects.create(
            name="Новостройка", slug="kvartiry-new", tn_parent=sell
        )
        link(root)
        link(sell, "rooms", "area", "price")
        link(resale, "floor", "area")
        link(new_build, "deadline", "developer")

        call_command("derive_children_as", "--apply")

        lines = capsys.readouterr().out.splitlines()
        parent_line = next(line for line in lines if " kvartiry " in f"{line} ")
        assert "chips" in parent_line
        assert "vocabulary>structure" in parent_line
        assert "transaction" in parent_line
        branch_line = next(
            line for line in lines if "kvartiry/kvartiry-sell" in line
        )
        assert "tiles" in branch_line
        assert Category.objects.get(slug="kvartiry").resolved_children_as == "chips"
        assert (
            Category.objects.get(slug="kvartiry-sell").resolved_children_as == "tiles"
        )

    def test_root_restricts_the_run(self, catalogue):
        call_command("derive_children_as", "--apply", "--root", "cars")

        assert Category.objects.get(slug="cars").children_as_derived == "chips"
        assert Category.objects.get(slug="electronics").children_as_derived == ""


class TestTreeEndpoint:
    @pytest.fixture
    def tree(self):
        root = Category.objects.create(name="Vehicles", slug="vehicles", tn_priority=10)
        quiet = Category.objects.create(name="Quiet", slug="quiet", tn_priority=1)
        cars = Category.objects.create(
            name="Cars", slug="cars", tn_parent=root, tn_priority=5
        )
        bikes = Category.objects.create(
            name="Bikes", slug="bikes", tn_parent=root, tn_priority=9
        )
        sedans = Category.objects.create(name="Sedans", slug="sedans", tn_parent=cars)
        Category.objects.create(name="Coupe", slug="coupe", tn_parent=sedans)
        Category.objects.create(
            name="Hidden", slug="hidden", tn_parent=root, deleted=True
        )
        return {"root": root, "quiet": quiet, "cars": cars, "bikes": bikes}

    def test_it_nests_three_levels_by_default(self, api_client, tree):
        response = api_client.get(f"{BASE}/tree/")

        assert response.status_code == 200
        rows = response.json()
        vehicles = next(row for row in rows if row["slug"] == "vehicles")
        cars = next(row for row in vehicles["children"] if row["slug"] == "cars")
        assert [row["slug"] for row in cars["children"]] == ["sedans"]
        # Fourth level is past the default depth.
        assert cars["children"][0]["children"] == []

    def test_depth_is_honoured(self, api_client, tree):
        rows = api_client.get(f"{BASE}/tree/?depth=1").json()

        assert [row["slug"] for row in rows] == ["vehicles", "quiet"]
        assert all(row["children"] == [] for row in rows)

    def test_depth_is_capped_not_refused(self, api_client, tree):
        capped = api_client.get(f"{BASE}/tree/?depth=99")
        four = api_client.get(f"{BASE}/tree/?depth=4")

        assert capped.status_code == 200
        assert capped.json() == four.json()

    def test_a_nonsense_depth_falls_back_to_the_default(self, api_client, tree):
        nonsense = api_client.get(f"{BASE}/tree/?depth=banana")
        default = api_client.get(f"{BASE}/tree/")

        assert nonsense.status_code == 200
        assert nonsense.json() == default.json()

    def test_every_level_is_ordered_by_priority(self, api_client, tree):
        rows = api_client.get(f"{BASE}/tree/").json()

        assert [row["slug"] for row in rows] == ["vehicles", "quiet"]
        vehicles = rows[0]
        assert [row["slug"] for row in vehicles["children"]] == ["bikes", "cars"]

    def test_a_deleted_node_is_absent(self, api_client, tree):
        rows = api_client.get(f"{BASE}/tree/").json()

        slugs = {row["slug"] for row in rows[0]["children"]}
        assert "hidden" not in slugs

    def test_path_is_the_id_path_the_search_query_takes(self, api_client, tree):
        rows = api_client.get(f"{BASE}/tree/").json()

        vehicles = rows[0]
        cars = next(row for row in vehicles["children"] if row["slug"] == "cars")
        assert vehicles["path"] == str(tree["root"].pk)
        assert cars["path"] == f"{tree['root'].pk}/{tree['cars'].pk}"

    def test_it_carries_children_as(self, api_client, tree):
        Category.objects.filter(slug="cars").update(children_as="chips")

        rows = api_client.get(f"{BASE}/tree/").json()

        vehicles = rows[0]
        by_slug = {row["slug"]: row for row in vehicles["children"]}
        assert by_slug["cars"]["children_as"] == "chips"
        assert by_slug["bikes"]["children_as"] is None

    def test_a_depth_capped_node_still_says_how_its_children_look(
        self, api_client, tree
    ):
        """The client asks for the next level later; it needs to know now
        whether that level is a destination or a filter."""
        rows = api_client.get(f"{BASE}/tree/?depth=1").json()

        assert rows[0]["children_as"] == "tiles"

    def test_it_is_one_query_whatever_the_tree(self, api_client, tree):
        from django.core.cache import cache

        def count(depth: int) -> int:
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                assert api_client.get(f"{BASE}/tree/?depth={depth}").status_code == 200
            return len(ctx)

        assert count(1) == count(4)

    def test_a_second_call_is_served_from_cache(self, api_client, tree):
        api_client.get(f"{BASE}/tree/")

        with CaptureQueriesContext(connection) as ctx:
            api_client.get(f"{BASE}/tree/")

        # Only the fingerprint aggregate that names the cache entry.
        assert len(ctx) == 1

    def test_an_edit_retires_the_cache_entry(self, api_client, tree):
        assert len(api_client.get(f"{BASE}/tree/?depth=1").json()) == 2

        Category.objects.create(name="Boats", slug="boats")

        assert len(api_client.get(f"{BASE}/tree/?depth=1").json()) == 3

    def test_it_answers_exactly_the_menu_keys(self, api_client, tree):
        rows = api_client.get(f"{BASE}/tree/").json()

        assert sorted(rows[0]) == [
            "catalog_icon", "children", "children_as", "children_axis_label",
            "children_count", "id", "name", "path", "slug",
        ]


class TestProvenanceStaysOff:
    """`external_id`/`external_source` are the source catalogue's own node
    numbering. The gate held on list/detail before `children_as` existed; the
    tree is a third public read and joins the same rule."""

    @pytest.fixture
    def imported(self):
        root = Category.objects.create(
            name="Phones", slug="phones",
            external_id="129639", external_source="somecatalog",
        )
        Category.objects.create(
            name="Smartphones", slug="smartphones", tn_parent=root,
            external_id="129640", external_source="somecatalog",
        )
        return root

    @staticmethod
    def _walk(rows):
        for row in rows:
            yield row
            yield from TestProvenanceStaysOff._walk(row.get("children", []))

    def test_the_tree_carries_no_provenance(self, api_client, imported):
        rows = api_client.get(f"{BASE}/tree/").json()

        nodes = list(self._walk(rows))
        assert len(nodes) == 2
        for node in nodes:
            assert "external_id" not in node
            assert "external_source" not in node

    def test_the_list_carries_no_provenance(self, api_client, imported):
        payload = api_client.get(f"{BASE}/categories/").json()

        for row in payload["results"]:
            assert "external_id" not in row
            assert "external_source" not in row

    def test_the_detail_carries_no_provenance(self, api_client, imported):
        row = api_client.get(f"{BASE}/categories/{imported.pk}/").json()

        assert "external_id" not in row
        assert "external_source" not in row


class TestCatalogFixtureRoundTrip(_CatalogTestCase):
    """`children_as` travels in the catalogue fixture.

    Fixtures are the carrier between a catalogue and the stands that run it,
    and in this fleet they are baked into images: a value that does not
    survive DB -> fixture -> DB is a decision the next container start
    forgets. The AUTHORED column travels; the derivation cache does not —
    `derive_children_as` rebuilds it from the loaded tree.
    """

    def _authored(self, value="chips", slug="electronics"):
        Category.objects.filter(slug=slug).update(children_as=value)

    def test_an_authored_value_is_exported(self):
        self.seed_catalog()
        self._authored()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = {c["slug"]: c for c in _read_json(out, cf.CATEGORIES_FILE)}

            assert records["electronics"]["children_as"] == "chips"

    def test_auto_is_not_written(self):
        """`auto` is what every row says by default, so the key stays absent
        and every content hash already on disk stays valid."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)

            assert all("children_as" not in record for record in records)

    def test_the_derivation_cache_never_travels(self):
        self.seed_catalog()
        Category.objects.filter(slug="electronics").update(
            children_as_derived="chips"
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)

            assert all("children_as_derived" not in record for record in records)
            assert all("children_as" not in record for record in records)

    def test_round_trip_through_a_clean_db_keeps_it(self):
        self.seed_catalog()
        self._authored()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            before = _read(first, cf.CATEGORIES_FILE)
            _wipe_db()

            report = cl.load_catalog(first, seed_if_empty=True)
            assert not report.failed

            loaded = Category.objects.get(slug="electronics")
            assert loaded.children_as == "chips"
            assert loaded.resolved_children_as == "chips"
            _export(second)
            assert _read(second, cf.CATEGORIES_FILE) == before

    def test_a_reload_over_the_live_tree_is_a_no_op(self):
        self.seed_catalog()
        self._authored()
        with tempfile.TemporaryDirectory() as out:
            _export(out)

            report = cl.load_catalog(out)

            assert not report.failed
            assert report.count(cl.UPDATED) == 0
            assert report.count(cl.CREATED) == 0

    def test_a_fixture_side_change_is_applied_on_update(self):
        """Taxonomy, not stand curation: how a node's children are drawn is
        the same wherever the catalogue is loaded, so the fixture owns it."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for record in records:
                if record["slug"] == "electronics":
                    record["children_as"] = "chips"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out)

            assert not report.failed
            assert Category.objects.get(slug="electronics").children_as == "chips"


class TestAxisLabel:
    """`children_axis_label` — the NAME of the axis a chip row splits on.

    A chip row is unreadable without it: «Все | С пробегом | Новые» is a
    set of values, and only the parent can say they are values OF
    something. The column is authored text (a translation key, like
    `name`), the derivation may fill a blank one from the vocabulary group
    it already matched, and what an operator wrote is never overwritten.
    """

    @pytest.fixture
    def realty(self):
        """A partition the NAME vocabulary sees: Куплю | Сдам."""
        root = Category.objects.create(name="Flats", slug="flats")
        buy = Category.objects.create(name="Куплю", slug="flats-buy", tn_parent=root)
        rent = Category.objects.create(name="Сдам", slug="flats-rent", tn_parent=root)
        link(root)
        link(buy, "rooms", "area", "floor")
        link(rent, "deposit", "term", "furnished", "pets")
        return root

    def test_the_default_is_empty(self, db):
        root = Category.objects.create(name="Flats", slug="flats")
        assert root.children_axis_label == ""

    # --- what a reader is told --------------------------------------------

    def test_the_public_read_carries_it_as_stored(self, api_client, db):
        root = Category.objects.create(
            name="Flats", slug="flats", children_axis_label="categories.axis.deal_type"
        )
        Category.objects.create(name="Куплю", slug="flats-buy", tn_parent=root)

        rows = api_client.get(f"{BASE}/categories/").json()["results"]
        row = next(r for r in rows if r["slug"] == "flats")

        # The stored KEY, exactly as `name` travels — the client resolves it.
        assert row["children_axis_label"] == "categories.axis.deal_type"

    def test_the_tree_carries_it(self, api_client, db):
        root = Category.objects.create(
            name="Flats", slug="flats", children_axis_label="categories.axis.deal_type"
        )
        Category.objects.create(name="Куплю", slug="flats-buy", tn_parent=root)

        rows = api_client.get(f"{BASE}/tree/").json()
        node = next(r for r in rows if r["slug"] == "flats")

        assert node["children_axis_label"] == "categories.axis.deal_type"
        assert node["children_as"] == "tiles"  # nobody derived yet — unrelated

    def test_an_unnamed_axis_is_an_empty_string_not_a_missing_key(
        self, api_client, db
    ):
        Category.objects.create(name="Flats", slug="flats")

        rows = api_client.get(f"{BASE}/tree/").json()

        assert rows[0]["children_axis_label"] == ""

    def test_staff_can_author_it(self, db):
        from stapel_categories.serializers import CategoryStaffSerializer

        root = Category.objects.create(name="Flats", slug="flats")
        serializer = CategoryStaffSerializer(
            root, data={"children_axis_label": "catalogue.flats.deal"}, partial=True
        )
        assert serializer.is_valid(), serializer.errors
        serializer.save()

        root.refresh_from_db()
        assert root.children_axis_label == "catalogue.flats.deal"

    def test_the_admin_offers_it(self, db):
        from django.contrib.admin.sites import AdminSite

        from stapel_categories.admin import CategoryAdmin

        fields = set()
        for _, spec in CategoryAdmin(Category, AdminSite()).fieldsets:
            fields.update(spec["fields"])
        assert "children_axis_label" in fields

    # --- what the derivation may fill -------------------------------------

    def test_a_blank_label_takes_the_group_key(self):
        assert axis_label_for("transaction", "") == AXIS_LABEL_KEYS["transaction"]

    def test_authored_text_wins(self):
        assert axis_label_for("transaction", "catalogue.flats.deal") is None

    def test_its_own_previous_key_is_re_derivable(self):
        # The derived values are a closed set, so the command can recognise
        # its own answer and improve it — no cache column needed.
        assert (
            axis_label_for("condition", AXIS_LABEL_KEYS["transaction"])
            == AXIS_LABEL_KEYS["condition"]
        )

    def test_an_unchanged_key_is_not_rewritten(self):
        assert axis_label_for("condition", AXIS_LABEL_KEYS["condition"]) is None

    def test_a_group_with_no_named_axis_names_nothing(self):
        assert axis_label_for(None, "") is None

    def test_both_gender_groups_ask_the_same_question(self):
        assert AXIS_LABEL_KEYS["childrens-gender"] == AXIS_LABEL_KEYS["adult-gender"]

    def test_every_key_is_a_key_not_a_word(self):
        # Hard-coded Russian here would make one market's alphabet the
        # catalogue's; the fleet translates these.
        assert all(
            key.isascii() and "." in key for key in AXIS_LABEL_KEYS.values()
        )


@pytest.mark.django_db
class TestAxisLabelDerivation:
    """`derive_children_as` names the axis of the rows it makes chips."""

    @pytest.fixture
    def realty(self):
        root = Category.objects.create(name="Flats", slug="flats")
        Category.objects.create(name="Куплю", slug="flats-buy", tn_parent=root)
        Category.objects.create(name="Сдам", slug="flats-rent", tn_parent=root)
        link(root)
        link(Category.objects.get(slug="flats-buy"), "rooms", "area", "floor")
        link(Category.objects.get(slug="flats-rent"), "deposit", "term", "pets")
        return root

    def test_a_dry_run_names_nothing(self, realty, capsys):
        call_command("derive_children_as")

        assert "axis:" in capsys.readouterr().out
        realty.refresh_from_db()
        assert realty.children_axis_label == ""

    def test_apply_names_the_axis_of_a_chip_row(self, realty):
        call_command("derive_children_as", "--apply")

        realty.refresh_from_db()
        assert realty.children_as_derived == "chips"
        assert realty.children_axis_label == AXIS_LABEL_KEYS["transaction"]

    def test_it_never_overwrites_authored_text(self, realty):
        Category.objects.filter(pk=realty.pk).update(
            children_axis_label="catalogue.flats.deal"
        )

        call_command("derive_children_as", "--apply")

        realty.refresh_from_db()
        assert realty.children_axis_label == "catalogue.flats.deal"

    def test_a_pinned_chips_parent_still_gets_its_axis_named(self, realty):
        Category.objects.filter(pk=realty.pk).update(children_as="chips")

        call_command("derive_children_as", "--apply")

        realty.refresh_from_db()
        # The authored decision is untouched, and the row it draws is captioned.
        assert realty.children_as == "chips"
        assert realty.children_axis_label == AXIS_LABEL_KEYS["transaction"]

    def test_a_tiles_parent_is_never_captioned(self):
        root = Category.objects.create(name="Electronics", slug="electronics")
        phones = Category.objects.create(name="Phones", slug="phones", tn_parent=root)
        fridges = Category.objects.create(name="Fridges", slug="fridges", tn_parent=root)
        link(root)
        link(phones, "make", "memory", "screen")
        link(fridges, "volume", "no_frost", "colour")

        call_command("derive_children_as", "--apply")

        root.refresh_from_db()
        assert root.children_as_derived == "tiles"
        assert root.children_axis_label == ""

    def test_a_second_run_is_a_no_op(self, realty, capsys):
        call_command("derive_children_as", "--apply")
        capsys.readouterr()

        call_command("derive_children_as", "--apply")

        assert "Nothing to write." in capsys.readouterr().out


class TestAxisLabelFixtureRoundTrip(_CatalogTestCase):
    """The caption travels with the catalogue, like `children_as`.

    A label that does not survive DB -> fixture -> DB is a decision the next
    container start forgets, and an uncaptioned chip row is what the reader
    then gets.
    """

    def test_a_blank_label_is_absent_from_the_record(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)

            # Absent, not `""` — a fixture written before this key existed
            # keeps its content hash.
            assert all("children_axis_label" not in r for r in records)

    def test_a_named_axis_survives_a_round_trip_through_a_clean_db(self):
        self.seed_catalog()
        Category.objects.filter(slug="electronics").update(
            children_as="chips", children_axis_label="categories.axis.condition"
        )
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            before = _read(first, cf.CATEGORIES_FILE)
            _wipe_db()

            report = cl.load_catalog(first, seed_if_empty=True)
            assert not report.failed

            loaded = Category.objects.get(slug="electronics")
            assert loaded.children_axis_label == "categories.axis.condition"
            _export(second)
            assert _read(second, cf.CATEGORIES_FILE) == before

    def test_a_fixture_side_change_is_applied_on_update(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for record in records:
                if record["slug"] == "electronics":
                    record["children_axis_label"] = "categories.axis.condition"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out)

            assert not report.failed
            assert (
                Category.objects.get(slug="electronics").children_axis_label
                == "categories.axis.condition"
            )


class TestAxisLabelSurvivesAReload(_CatalogTestCase):
    """derive → apply → reload → derive is a no-op (0.20.3).

    The step that broke on a live stand: a full ``load_catalog --on-conflict
    fixture-wins`` after the derivation blanked every caption the derivation
    had just written — the fixture on disk predates the derivation and says
    nothing about the column, and an absent key was applied as ``""``. Between
    the two commands every chip row on the stand was uncaptioned, and the next
    ``--apply`` had all of its work to do again.
    """

    def _realty(self):
        root = Category.objects.create(name="Flats", slug="flats")
        buy = Category.objects.create(name="Куплю", slug="flats-buy", tn_parent=root)
        rent = Category.objects.create(name="Сдам", slug="flats-rent", tn_parent=root)
        link(root)
        link(buy, "rooms", "area", "floor")
        link(rent, "deposit", "term", "pets")
        return root

    def test_a_derived_caption_survives_the_reload_that_erased_it(self):
        root = self._realty()
        label = AXIS_LABEL_KEYS["transaction"]
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            call_command("derive_children_as", "--apply")
            root.refresh_from_db()
            assert root.children_axis_label == label
            # Canon predates the derivation: it does not mention the column.
            assert all(
                "children_axis_label" not in record
                for record in _read_json(out, cf.CATEGORIES_FILE)
            )

            report = cl.load_catalog(
                out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                deletions=cl.DELETIONS_IGNORE,
            )

            assert not report.failed
            assert report.kept_unsaid == {"children_axis_label": 1}
            root.refresh_from_db()
            assert root.children_axis_label == label

            # …and the derivation has nothing left to do, which is the only
            # form of "idempotent" that means anything after a reload.
            buf = io.StringIO()
            call_command("derive_children_as", "--apply", stdout=buf)
            assert "Nothing to write." in buf.getvalue()
            root.refresh_from_db()
            assert root.children_axis_label == label


def transparent_wrapper(slug="uslugi", children=2, **kwargs) -> Category:
    """A node authored `transparent`, with *children* children under it."""
    node = Category.objects.create(
        name="Услуги", slug=slug, children_as="transparent", **kwargs
    )
    for index in range(children):
        Category.objects.create(
            name=f"Group {index}", slug=f"{slug}-kid-{index}", tn_parent=node
        )
    return Category.objects.get(pk=node.pk)


class TestTransparentValue:
    """`transparent` — browsing SKIPS this node (0.20.4).

    Its children appear where it would, and its own page is its parent's. The
    tree is unchanged: the node keeps its id, its path and its place as the
    target of a listing. Authored only — the collapse is a judgement read off
    a census, and no signal on a tree can make it.
    """

    def test_it_resolves_verbatim(self):
        assert transparent_wrapper().resolved_children_as == "transparent"

    def test_it_beats_the_derivation_cache(self):
        node = transparent_wrapper(slug="uslugi-2")
        Category.objects.filter(pk=node.pk).update(children_as_derived="chips")

        assert Category.objects.get(pk=node.pk).resolved_children_as == "transparent"

    def test_a_childless_node_is_still_null(self):
        """Nothing to show in its place, so the honest answer is the leaf's."""
        leaf = Category.objects.create(
            name="Leaf", slug="leaf-transparent", children_as="transparent"
        )

        assert leaf.resolved_children_as is None

    def test_the_public_read_carries_it(self, api_client):
        transparent_wrapper(slug="uslugi-3")

        rows = api_client.get(f"{BASE}/categories/roots/").json()

        assert [row["children_as"] for row in rows] == ["transparent"]

    def test_the_staff_serializer_can_author_it(self):
        from stapel_categories.serializers import CategoryStaffSerializer

        node = transparent_wrapper(slug="uslugi-4")
        serializer = CategoryStaffSerializer(
            node, data={"children_as_authored": "transparent"}, partial=True
        )

        assert serializer.is_valid(), serializer.errors
        serializer.save()
        assert Category.objects.get(pk=node.pk).children_as == "transparent"

    def test_the_tree_carries_it(self, api_client):
        root = Category.objects.create(name="Root", slug="tr-root")
        wrapper = Category.objects.create(
            name="Offer", slug="tr-offer", tn_parent=root, children_as="transparent"
        )
        Category.objects.create(name="Group", slug="tr-group", tn_parent=wrapper)

        rows = api_client.get(f"{BASE}/tree/").json()

        offer = rows[0]["children"][0]
        assert offer["children_as"] == "transparent"
        # The tree is UNCHANGED: the skipped node is still a node, with its
        # own path — only the presentation of it is a client's business.
        assert offer["slug"] == "tr-offer"
        assert offer["path"] == f"{root.pk}/{wrapper.pk}"
        assert [kid["slug"] for kid in offer["children"]] == ["tr-group"]


class TestTransparentIsNeverDerived:
    def test_the_derivation_leaves_it_alone(self, capsys):
        root = Category.objects.create(name="Услуги", slug="uslugi-d")
        wrapper = Category.objects.create(
            name="Предложение услуг", slug="uslugi-offer",
            tn_parent=root, children_as="transparent",
        )
        first = Category.objects.create(
            name="Ремонт", slug="uslugi-repair", tn_parent=wrapper
        )
        second = Category.objects.create(
            name="Уборка", slug="uslugi-clean", tn_parent=wrapper
        )
        link(wrapper)
        link(first, "price", "area")
        link(second, "price", "area")

        call_command("derive_children_as", "--apply")

        row = Category.objects.get(pk=wrapper.pk)
        assert row.children_as == "transparent"
        assert row.children_as_derived == ""
        assert row.resolved_children_as == "transparent"

    def test_the_report_names_the_authored_value(self, capsys):
        transparent_wrapper(slug="uslugi-r")

        call_command("derive_children_as")

        line = next(
            line
            for line in capsys.readouterr().out.splitlines()
            if " uslugi-r" in f"{line} "
        )
        assert "authored transparent" in line

    def test_no_axis_caption_is_written_for_it(self):
        """A skipped node draws no chip row, so it has no axis to name."""
        root = Category.objects.create(name="Квартиры", slug="flats-t")
        Category.objects.create(name="Куплю", slug="flats-t-buy", tn_parent=root)
        Category.objects.create(name="Продам", slug="flats-t-sell", tn_parent=root)
        Category.objects.filter(pk=root.pk).update(children_as="transparent")

        call_command("derive_children_as", "--apply")

        assert Category.objects.get(pk=root.pk).children_axis_label == ""


class TestTransparentFixtureRoundTrip(_CatalogTestCase):
    """It travels like any other authored value — see TestCatalogFixtureRoundTrip.

    A collapse applied from the census and lost at the next image build is a
    census the engineer gets to walk twice.
    """

    def test_it_survives_a_round_trip_through_a_clean_db(self):
        self.seed_catalog()
        Category.objects.filter(slug="electronics").update(children_as="transparent")
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            before = _read(first, cf.CATEGORIES_FILE)
            records = {c["slug"]: c for c in _read_json(first, cf.CATEGORIES_FILE)}
            assert records["electronics"]["children_as"] == "transparent"
            _wipe_db()

            report = cl.load_catalog(first, seed_if_empty=True)

            assert not report.failed
            loaded = Category.objects.get(slug="electronics")
            assert loaded.children_as == "transparent"
            assert loaded.resolved_children_as == "transparent"
            _export(second)
            assert _read(second, cf.CATEGORIES_FILE) == before

    def test_a_reload_over_the_live_tree_is_a_no_op(self):
        self.seed_catalog()
        Category.objects.filter(slug="electronics").update(children_as="transparent")
        with tempfile.TemporaryDirectory() as out:
            _export(out)

            report = cl.load_catalog(out)

            assert not report.failed
            assert report.count(cl.UPDATED) == 0
            assert report.count(cl.CREATED) == 0

    def test_a_fixture_side_change_is_applied_on_update(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for record in records:
                if record["slug"] == "electronics":
                    record["children_as"] = "transparent"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out)

            assert not report.failed
            assert (
                Category.objects.get(slug="electronics").children_as == "transparent"
            )


class TestSetChildrenAsCommand:
    """`set_children_as` — apply a census list without the admin (0.20.4)."""

    @pytest.fixture
    def catalogue(self):
        root = Category.objects.create(name="Услуги", slug="uslugi")
        wrapper = Category.objects.create(
            name="Предложение услуг", slug="predlozhenie", tn_parent=root
        )
        Category.objects.create(name="Ремонт", slug="remont", tn_parent=wrapper)
        other = Category.objects.create(name="Авто", slug="avto")
        Category.objects.create(name="Новые", slug="avto-new", tn_parent=other)
        return {"root": root, "wrapper": wrapper, "other": other}

    def test_it_sets_the_authored_value(self, catalogue, capsys):
        call_command(
            "set_children_as", "--path", "uslugi/predlozhenie",
            "--value", "transparent",
        )

        row = Category.objects.get(slug="predlozhenie")
        assert row.children_as == "transparent"
        assert row.resolved_children_as == "transparent"
        assert "set" in capsys.readouterr().out

    def test_it_prints_what_changed(self, catalogue, capsys):
        call_command(
            "set_children_as", "--path", "uslugi/predlozhenie",
            "--value", "transparent",
        )

        out = capsys.readouterr().out
        assert "uslugi/predlozhenie" in out
        assert "auto -> transparent" in out
        assert "Wrote 1 of 1" in out

    def test_a_second_run_writes_nothing(self, catalogue, capsys):
        args = ("--path", "uslugi/predlozhenie", "--value", "transparent")
        call_command("set_children_as", *args)
        before = Category.objects.get(slug="predlozhenie").revision
        capsys.readouterr()

        call_command("set_children_as", *args)

        out = capsys.readouterr().out
        assert "unchanged" in out
        assert "Nothing to write" in out
        # Idempotent all the way down: no revision bump, so no downstream
        # cache in the fleet is invalidated by a re-run of the same list.
        assert Category.objects.get(slug="predlozhenie").revision == before

    def test_several_paths_in_one_run(self, catalogue, capsys):
        call_command(
            "set_children_as",
            "--path", "uslugi/predlozhenie", "--path", "avto",
            "--value", "chips",
        )

        assert Category.objects.get(slug="predlozhenie").children_as == "chips"
        assert Category.objects.get(slug="avto").children_as == "chips"

    def test_a_list_from_a_file(self, catalogue, tmp_path, capsys):
        listing = tmp_path / "census.txt"
        listing.write_text(
            "# the services wrapper\nuslugi/predlozhenie\n\navto\n", encoding="utf-8"
        )

        call_command(
            "set_children_as", "--paths-from", str(listing), "--value", "transparent"
        )

        assert Category.objects.get(slug="predlozhenie").children_as == "transparent"
        assert Category.objects.get(slug="avto").children_as == "transparent"

    def test_every_authored_value_is_settable(self, catalogue):
        for value in ("tiles", "chips", "auto", "transparent"):
            call_command(
                "set_children_as", "--path", "uslugi/predlozhenie", "--value", value
            )
            assert Category.objects.get(slug="predlozhenie").children_as == value

    def test_a_bad_value_is_refused(self, catalogue):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command(
                "set_children_as", "--path", "uslugi/predlozhenie", "--value", "nope"
            )

    def test_a_typo_writes_nothing_at_all(self, catalogue, capsys):
        """Every path resolves before any of them is written: a list with one
        bad line must not leave half the census applied."""
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command(
                "set_children_as",
                "--path", "uslugi/predlozhenie", "--path", "uslugi/nosuch",
                "--value", "transparent",
            )

        assert Category.objects.get(slug="predlozhenie").children_as == "auto"

    def test_a_bare_slug_resolves(self, catalogue):
        """`Category.slug` is unique, so the last segment already names it."""
        call_command("set_children_as", "--path", "predlozhenie", "--value", "tiles")

        assert Category.objects.get(slug="predlozhenie").children_as == "tiles"

    def test_a_path_that_no_longer_matches_the_tree_is_refused(self, catalogue):
        """A census pasted from a stale report must not apply itself to a node
        that has been re-parented since."""
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="does not match the tree"):
            call_command(
                "set_children_as", "--path", "avto/predlozhenie", "--value", "tiles"
            )

        assert Category.objects.get(slug="predlozhenie").children_as == "auto"

    def test_a_dry_run_writes_nothing(self, catalogue, capsys):
        call_command(
            "set_children_as", "--path", "uslugi/predlozhenie",
            "--value", "transparent", "--dry-run",
        )

        assert "Dry run" in capsys.readouterr().out
        assert Category.objects.get(slug="predlozhenie").children_as == "auto"

    def test_the_write_invalidates_downstream_caches(self, catalogue):
        """A full save, not a targeted UPDATE: a reader's answer changed."""
        before = Category.objects.get(slug="predlozhenie").revision

        call_command(
            "set_children_as", "--path", "uslugi/predlozhenie",
            "--value", "transparent",
        )

        assert Category.objects.get(slug="predlozhenie").revision != before


class TestSetChildrenAsAxisLabel:
    """``--axis-label``/``--clear-axis-label`` — the authoring side of
    ``children_axis_label`` (0.20.6). Until now the column had no command of
    its own: an authored caption («Тип жилья» over Новостройка/Вторичка) had
    to be edited as fixture data, and ``derive_children_as`` only ever fills a
    blank column or improves its own previous key.
    """

    @pytest.fixture
    def catalogue(self):
        root = Category.objects.create(name="Услуги", slug="uslugi-al")
        wrapper = Category.objects.create(
            name="Предложение услуг", slug="predlozhenie-al", tn_parent=root
        )
        Category.objects.create(name="Ремонт", slug="remont-al", tn_parent=wrapper)
        other = Category.objects.create(name="Авто", slug="avto-al")
        Category.objects.create(name="Новые", slug="avto-new-al", tn_parent=other)
        return {"root": root, "wrapper": wrapper, "other": other}

    def test_it_writes_the_authored_label(self, catalogue, capsys):
        call_command(
            "set_children_as", "--path", "uslugi-al/predlozhenie-al",
            "--axis-label", "Тип жилья",
        )

        row = Category.objects.get(slug="predlozhenie-al")
        assert row.children_axis_label == "Тип жилья"
        out = capsys.readouterr().out
        assert "set" in out
        assert "Тип жилья" in out

    def test_a_second_run_writes_nothing(self, catalogue, capsys):
        args = ("--path", "predlozhenie-al", "--axis-label", "Тип жилья")
        call_command("set_children_as", *args)
        before = Category.objects.get(slug="predlozhenie-al").revision
        capsys.readouterr()

        call_command("set_children_as", *args)

        out = capsys.readouterr().out
        assert "unchanged" in out
        assert "Nothing to write" in out
        assert Category.objects.get(slug="predlozhenie-al").revision == before

    def test_clear_blanks_an_authored_label(self, catalogue):
        Category.objects.filter(slug="predlozhenie-al").update(
            children_axis_label="Тип жилья"
        )

        call_command(
            "set_children_as", "--path", "predlozhenie-al", "--clear-axis-label"
        )

        assert Category.objects.get(slug="predlozhenie-al").children_axis_label == ""

    def test_clear_on_an_already_blank_label_is_a_no_op(self, catalogue, capsys):
        call_command(
            "set_children_as", "--path", "predlozhenie-al", "--clear-axis-label"
        )

        assert "Nothing to write" in capsys.readouterr().out

    def test_value_and_axis_label_write_together_in_one_save(self, catalogue):
        call_command(
            "set_children_as", "--path", "predlozhenie-al",
            "--value", "chips", "--axis-label", "Тип жилья",
        )

        row = Category.objects.get(slug="predlozhenie-al")
        assert row.children_as == "chips"
        assert row.children_axis_label == "Тип жилья"

    def test_axis_label_and_clear_axis_label_are_mutually_exclusive(self, catalogue):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command(
                "set_children_as", "--path", "predlozhenie-al",
                "--axis-label", "x", "--clear-axis-label",
            )

    def test_neither_value_nor_axis_label_is_refused(self, catalogue):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("set_children_as", "--path", "predlozhenie-al")

    def test_a_dry_run_writes_nothing(self, catalogue, capsys):
        call_command(
            "set_children_as", "--path", "predlozhenie-al",
            "--axis-label", "Тип жилья", "--dry-run",
        )

        assert "Dry run" in capsys.readouterr().out
        assert Category.objects.get(slug="predlozhenie-al").children_axis_label == ""

    def test_an_authored_label_survives_a_derive_run(self):
        """The rule ``derive_children_as`` already holds
        (:func:`axis_label_for`: authored text always wins) exercised end to
        end through the command that now authors it, on a parent the
        vocabulary signal WOULD otherwise have captioned itself: a
        `Куплю`/`Сдам` split, named by hand instead."""
        root = Category.objects.create(name="Flats", slug="flats-al")
        buy = Category.objects.create(
            name="Куплю", slug="flats-al-buy", tn_parent=root
        )
        rent = Category.objects.create(
            name="Сдам", slug="flats-al-rent", tn_parent=root
        )
        link(root)
        link(buy, "rooms", "area", "floor")
        link(rent, "deposit", "term", "furnished", "pets")

        call_command(
            "set_children_as", "--path", "flats-al", "--axis-label", "Тип сделки"
        )
        call_command("derive_children_as", "--apply")

        row = Category.objects.get(slug="flats-al")
        # The derivation still decides `chips` off the vocabulary — only the
        # CAPTION is untouched.
        assert row.children_as_derived == "chips"
        assert row.children_axis_label == "Тип сделки"


class TestLiveChildren:
    """The children a reader can FETCH — not treenode's structure columns.

    On a live stand a services root read `tn_children_pks: "68,67,221"` while
    `GET /65/children/` returned one row: 67 and 68 were soft-deleted, and
    treenode's denormalised columns count every row that hangs off a node.
    Every client rule built on that column — leaf-ness, child counts, the
    one-child wrapper check — was counting ghosts.
    """

    @pytest.fixture
    def parent(self):
        root = Category.objects.create(name="Услуги", slug="lc-root")
        Category.objects.create(name="A", slug="lc-a", tn_parent=root)
        Category.objects.create(name="B", slug="lc-b", tn_parent=root)
        Category.objects.create(name="C", slug="lc-c", tn_parent=root)
        return Category.objects.get(pk=root.pk)

    def _row(self, api_client, pk):
        return api_client.get(f"{BASE}/categories/{pk}/").json()

    def test_a_soft_deleted_child_leaves_the_count(self, api_client, parent):
        Category.objects.filter(slug="lc-a").update(deleted=True)
        Category.objects.filter(slug="lc-b").update(deleted=True)

        row = self._row(api_client, parent.pk)

        assert row["children_count"] == 1
        assert row["children_pks"] == [Category.objects.get(slug="lc-c").pk]

    def test_the_raw_treenode_column_still_carries_the_ghosts(
        self, api_client, parent
    ):
        """The defect, pinned: the two keys disagree, and only one is right.

        `tn_children_pks` stays on the payload for the revision-sync feed's
        consumers, so a test that did not say this would leave the reader no
        way to tell which key to believe.
        """
        Category.objects.filter(slug="lc-a").update(deleted=True)

        row = self._row(api_client, parent.pk)

        ghost = Category.objects.get(slug="lc-a").pk
        assert str(ghost) in row["tn_children_pks"]
        assert ghost not in row["children_pks"]

    def test_a_retired_child_that_structures_nothing_leaves_the_count(
        self, api_client, parent
    ):
        """The same rule as every other read: `visible_categories()`."""
        Category.objects.filter(slug="lc-a").update(active=False)

        row = self._row(api_client, parent.pk)

        assert row["children_count"] == 2

    def test_a_retired_child_holding_a_live_one_up_stays(self, api_client, parent):
        """…and only that rule: a retired row an active one hangs from is
        still served by `/children/`, so it is still counted here."""
        retired = Category.objects.get(slug="lc-a")
        Category.objects.create(name="Live", slug="lc-a-live", tn_parent=retired)
        Category.objects.filter(pk=retired.pk).update(active=False)

        row = self._row(api_client, parent.pk)

        assert row["children_count"] == 3

    def test_the_count_is_what_children_returns(self, api_client, parent):
        """The contract, stated as one assertion: a count that disagrees with
        the list under it is the defect this replaces."""
        Category.objects.filter(slug="lc-a").update(deleted=True)
        Category.objects.filter(slug="lc-b").update(active=False)

        row = self._row(api_client, parent.pk)
        children = api_client.get(f"{BASE}/categories/{parent.pk}/children/").json()

        assert row["children_count"] == len(children)
        assert row["children_pks"] == [child["id"] for child in children]

    def test_a_node_whose_children_are_all_deleted_is_a_leaf(self, parent):
        Category.objects.filter(tn_parent=parent).update(deleted=True)

        assert Category.objects.get(pk=parent.pk).resolved_children_as is None

    def test_the_serialized_hint_follows(self, api_client, parent):
        Category.objects.filter(tn_parent=parent).update(deleted=True)

        assert self._row(api_client, parent.pk)["children_as"] is None

    def test_an_authored_value_does_not_survive_the_last_child(self, parent):
        """`children_as` describes children; with none left there are none."""
        Category.objects.filter(pk=parent.pk).update(children_as="transparent")
        Category.objects.filter(tn_parent=parent).update(deleted=True)

        assert Category.objects.get(pk=parent.pk).resolved_children_as is None

    def test_the_keys_cost_no_query_per_row(self, api_client):
        """One prefetch for the page, not one query per row — the property
        `children_as` was written to hold and these two must not break."""
        def count_queries(root_count: int) -> int:
            Category.objects.all().delete()
            for index in range(root_count):
                root = Category.objects.create(
                    name=f"Root {index}", slug=f"q-root-{index}"
                )
                Category.objects.create(
                    name=f"Kid {index}", slug=f"q-kid-{index}", tn_parent=root
                )
            with CaptureQueriesContext(connection) as ctx:
                response = api_client.get(f"{BASE}/categories/roots/")
                assert response.status_code == 200
                assert [row["children_count"] for row in response.json()] == [
                    1
                ] * root_count
            return len(ctx)

        assert count_queries(2) == count_queries(12)


class TestTreeLiveChildren:
    @pytest.fixture
    def tree(self):
        root = Category.objects.create(name="Root", slug="tlc-root")
        first = Category.objects.create(name="A", slug="tlc-a", tn_parent=root)
        Category.objects.create(name="B", slug="tlc-b", tn_parent=root)
        Category.objects.create(name="A1", slug="tlc-a1", tn_parent=first)
        return root

    def test_the_node_says_how_many_children_it_has(self, api_client, tree):
        rows = api_client.get(f"{BASE}/tree/").json()

        assert rows[0]["children_count"] == 2

    def test_a_soft_deleted_child_drops_out_of_the_count(self, api_client, tree):
        Category.objects.filter(slug="tlc-b").update(deleted=True)

        rows = api_client.get(f"{BASE}/tree/").json()

        assert rows[0]["children_count"] == 1
        assert [row["slug"] for row in rows[0]["children"]] == ["tlc-a"]

    def test_a_depth_capped_node_still_says_it_has_children(self, api_client, tree):
        """`children` is empty at the cap; the count is what says to ask."""
        rows = api_client.get(f"{BASE}/tree/?depth=2").json()

        first = next(row for row in rows[0]["children"] if row["slug"] == "tlc-a")
        assert first["children"] == []
        assert first["children_count"] == 1
        assert first["children_as"] == "tiles"

    def test_a_node_whose_children_are_all_deleted_is_a_leaf(self, api_client, tree):
        Category.objects.filter(slug="tlc-a1").update(deleted=True)

        rows = api_client.get(f"{BASE}/tree/?depth=2").json()

        first = next(row for row in rows[0]["children"] if row["slug"] == "tlc-a")
        assert first["children_count"] == 0
        assert first["children_as"] is None

    def test_it_is_a_constant_number_of_queries_whatever_the_depth(
        self, api_client, tree
    ):
        from django.core.cache import cache

        def count(depth: int) -> int:
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                assert api_client.get(f"{BASE}/tree/?depth={depth}").status_code == 200
            return len(ctx)

        assert count(1) == count(4)
