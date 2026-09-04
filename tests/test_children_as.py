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
import tempfile

import pytest
from django.core.management import call_command
from django.test.utils import CaptureQueriesContext
from django.db import connection

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl

from stapel_categories.management.commands.derive_children_as import (
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
        parent_line = next(line for line in lines if line.endswith(" kvartiry"))
        assert "chips" in parent_line
        assert "vocabulary>structure" in parent_line
        assert "transaction" in parent_line
        branch_line = next(
            line for line in lines if line.endswith("kvartiry/kvartiry-sell")
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
            "catalog_icon", "children", "children_as", "id", "name", "path", "slug",
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
