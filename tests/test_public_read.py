"""The public-read posture of the catalogue, as a contract.

The category tree and the feature schema are the *navigation* of a storefront:
every page a search engine indexes renders them, and none of that traffic has
a session. That anonymous reads work is not an accident of which permission
class happened to be typed on the two viewsets — it is what the surface is
for, and until now nothing asserted it. Every existing HTTP test in this repo
authenticates a superuser first (``force_authenticate`` in
``test_category_commands.py``), so swapping ``ReadOnlyOrStaff`` for
``IsStaffUser`` would leave the suite green while every catalogue page on the
internet turned into a 401.

Three things pinned here:

* **Reads are open with no credentials at all.** List, retrieve, ``children``
  and the feature surface answer 200 to a client that never authenticated.
* **Writes are not.** ``POST``/``PATCH`` from the same anonymous side are
  refused, and nothing is written.
* **The read costs no cookie.** A ``Set-Cookie`` on these responses would make
  them uncacheable at the edge and start a session per crawler.
"""
import pytest
from stapel_core.django.api.permissions import ReadOnlyOrStaff

from stapel_categories.models import Category, Feature
from stapel_categories.views import CategoryViewSet, FeatureViewSet

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


@pytest.fixture
def anonymous_client(api_client):
    """No credentials whatsoever — never ``force_authenticate``d.

    Every other HTTP test in this repo starts by authenticating a superuser;
    this one deliberately does not, because the stranger is the caller this
    surface mostly serves.
    """
    return api_client


@pytest.fixture
def parent_category():
    return Category.objects.create(name="Vehicles", slug="vehicles")


@pytest.fixture
def child_category(parent_category):
    return Category.objects.create(
        name="Cars", slug="cars", tn_parent=parent_category, tn_priority=1
    )


@pytest.fixture
def feature():
    return Feature.objects.create(
        name="Mileage", slug="mileage", config={"type": "int", "min": 0, "max": 100}
    )


def _set_cookies(response):
    return list(response.cookies.keys())


# --- the permission classes themselves --------------------------------------


def test_catalogue_reads_are_open_to_anyone():
    """The two lines the whole public catalogue rests on.

    Named so a regression to a staff-only permission fails a test that says
    why, rather than a pile of tests that say ``401 != 200``.
    """
    assert CategoryViewSet.permission_classes == [ReadOnlyOrStaff]
    assert FeatureViewSet.permission_classes == [ReadOnlyOrStaff]


# --- categories: read ------------------------------------------------------


def test_anonymous_can_list_categories(anonymous_client, parent_category):
    resp = anonymous_client.get(f"{BASE}/categories/")
    assert resp.status_code == 200, resp.content
    assert parent_category.id in [row["id"] for row in resp.data["results"]]


def test_anonymous_can_retrieve_a_category(anonymous_client, parent_category):
    resp = anonymous_client.get(f"{BASE}/categories/{parent_category.id}/")
    assert resp.status_code == 200, resp.content
    assert resp.data["slug"] == "vehicles"


def test_anonymous_can_read_children(
    anonymous_client, parent_category, child_category
):
    resp = anonymous_client.get(f"{BASE}/categories/{parent_category.id}/children/")
    assert resp.status_code == 200, resp.content
    assert [row["id"] for row in resp.data] == [child_category.id]


# --- features: read --------------------------------------------------------


def test_anonymous_can_list_features(anonymous_client, feature):
    resp = anonymous_client.get(f"{BASE}/features/")
    assert resp.status_code == 200, resp.content
    assert feature.id in [row["id"] for row in resp.data["results"]]


def test_anonymous_can_retrieve_a_feature(anonymous_client, feature):
    resp = anonymous_client.get(f"{BASE}/features/{feature.id}/")
    assert resp.status_code == 200, resp.content
    assert resp.data["slug"] == "mileage"


def test_anonymous_can_read_a_categorys_features(
    anonymous_client, parent_category, feature
):
    from stapel_categories.models import CategoryFeature

    CategoryFeature.objects.create(category=parent_category, feature=feature, order=1)

    resp = anonymous_client.get(f"{BASE}/categories/{parent_category.id}/features/")
    assert resp.status_code == 200, resp.content


# --- no session is started by a read ---------------------------------------


def test_anonymous_reads_set_no_cookie(
    anonymous_client, parent_category, child_category, feature
):
    """Cacheable at the edge, and no session row per crawler."""
    for url in (
        f"{BASE}/categories/",
        f"{BASE}/categories/{parent_category.id}/",
        f"{BASE}/categories/{parent_category.id}/children/",
        f"{BASE}/features/",
        f"{BASE}/features/{feature.id}/",
    ):
        resp = anonymous_client.get(url)
        assert resp.status_code == 200, (url, resp.content)
        assert _set_cookies(resp) == [], (url, resp.cookies)
        assert not resp.has_header("Set-Cookie"), url


# --- writes stay shut ------------------------------------------------------


def test_anonymous_cannot_create_a_category(anonymous_client):
    resp = anonymous_client.post(
        f"{BASE}/categories/", {"name": "Injected", "slug": "injected"}, format="json"
    )
    assert resp.status_code in (401, 403), resp.content
    assert not Category.objects.filter(slug="injected").exists()


def test_anonymous_cannot_patch_a_category(anonymous_client, parent_category):
    resp = anonymous_client.patch(
        f"{BASE}/categories/{parent_category.id}/", {"name": "Renamed"}, format="json"
    )
    assert resp.status_code in (401, 403), resp.content
    parent_category.refresh_from_db()
    assert parent_category.name == "Vehicles"


def test_anonymous_write_is_401_where_a_challenge_exists(
    monkeypatch, api_client, parent_category
):
    """A fleet mounts ``JWTCookieAuthentication`` (stapel-core), which offers a
    ``WWW-Authenticate: Bearer`` challenge, so DRF answers **401** and not 403.

    That is the difference between "sign in" and "you signed in and still may
    not", and 401 is what the live surface returns. Set on the view rather
    than through ``settings.REST_FRAMEWORK``: DRF binds
    ``authentication_classes`` as a class attribute at import time, so a
    settings override arrives too late for an already-imported viewset — the
    test would pass for the wrong reason.
    """
    from stapel_core.django.jwt.authentication import JWTCookieAuthentication

    monkeypatch.setattr(
        CategoryViewSet, "authentication_classes", [JWTCookieAuthentication]
    )

    created = api_client.post(
        f"{BASE}/categories/", {"name": "Injected", "slug": "injected"}, format="json"
    )
    assert created.status_code == 401, created.content

    patched = api_client.patch(
        f"{BASE}/categories/{parent_category.id}/", {"name": "Renamed"}, format="json"
    )
    assert patched.status_code == 401, patched.content

    assert not Category.objects.filter(slug="injected").exists()


# --- the public projection is a frozen key set ------------------------------
#
# The rows above answer to strangers, so WHICH keys they carry is a disclosure
# decision, not a serializer detail. The stand that imported a competitor's
# catalogue shipped every row with `external_id`/`external_source` — the source
# catalogue's own node ids, readable by anyone with curl. Provenance is an
# operator fact: it stays in the Django admin and on the staff-gated write
# serializers, and it never rides the anonymous read surface.
#
# The set is asserted EXACTLY (sorted keys == the frozen list) rather than
# "does not contain the two leaked keys": the next leak will not be called
# external-anything, and an exact contract makes adding a public field a
# conscious act — extend this list in the same commit, with the same "who may
# read this?" question answered.

PUBLIC_CATEGORY_KEYS = sorted([
    "id", "name", "slug",
    "catalog_icon", "carousel_icon", "carousel_enabled",
    "active", "translatable",
    # A presentation hint about this node's own children — how a storefront
    # should draw the level below. Answered "who may read this?" with
    # everyone: it is derived from the shape of the public tree the same
    # reader can already walk, and carries nothing about where the tree came
    # from. The RESOLVED value only; the authoring column, `auto` included,
    # stays on the staff serializer.
    "children_as",
    # The caption of the axis that chip row splits on — the same disclosure
    # answer as the hint above: it describes the public tree's own shape and
    # a reader cannot draw the row without it.
    "children_axis_label",
    # The child set a reader can actually FETCH, and its size — live rows
    # only. `tn_children_pks` below is treenode's raw structure column and
    # counts soft-deleted and retired rows; it stays for the sync feed, but a
    # client rule (leaf-ness, a one-child wrapper check) reads these two.
    # Same disclosure answer as the hint above: they describe the shape of the
    # public tree the same reader can already walk.
    "children_pks", "children_count",
    "features",
    "tn_parent", "tn_priority", "tn_ancestors_pks", "tn_children_pks",
    "revision", "deleted",
])


@pytest.fixture
def imported_category():
    """A row as ``load_catalog`` writes it — provenance stamped."""
    return Category.objects.create(
        name="Phones", slug="phones",
        external_id="129639", external_source="somecatalog",
        carousel_enabled=True, active=True,
    )


def _rows(resp):
    data = resp.data
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data if isinstance(data, list) else [data]


def test_public_category_payload_is_the_frozen_key_set(
    anonymous_client, parent_category, imported_category
):
    imported_category.tn_parent = parent_category
    imported_category.save()
    urls = (
        f"{BASE}/categories/",                                    # list
        f"{BASE}/categories/{imported_category.id}/",             # detail
        f"{BASE}/categories/{parent_category.id}/children/",      # children
        f"{BASE}/categories/carousel/",                           # carousel
        f"{BASE}/categories/roots/",                              # roots
        f"{BASE}/categories/by-slug/{imported_category.slug}/",   # by-slug
    )
    for url in urls:
        resp = anonymous_client.get(url)
        assert resp.status_code == 200, (url, resp.content)
        rows = _rows(resp)
        assert rows, url
        for row in rows:
            assert sorted(row.keys()) == PUBLIC_CATEGORY_KEYS, url


def test_provenance_stays_on_the_staff_surfaces(imported_category):
    """Operators keep the fact; strangers lose it.

    The admin changeform and the staff-gated bulk serializer still carry
    ``external_id``/``external_source`` — de-duplicating a re-import needs
    them, and both surfaces sit behind staff authentication.
    """
    from django.contrib.admin.sites import AdminSite

    from stapel_categories.admin import CategoryAdmin
    from stapel_categories.serializers import CategoryBulkSerializer

    admin_fields = set()
    for _, spec in CategoryAdmin(Category, AdminSite()).fieldsets:
        admin_fields.update(spec["fields"])
    assert {"external_id", "external_source"} <= admin_fields
    assert {"external_id", "external_source"} <= set(
        CategoryBulkSerializer.Meta.fields
    )


# --- the sync feed and the public catalogue are two readers ----------------
#
# Д88, from a live stand: `GET /categories/?page=1` returned 174 rows named
# `smoke-1787331903`, `authz-1787369370`, `storefront-…` — every acceptance
# run the fleet had ever done, to anyone with curl and no credentials.
#
# The cause is a collision of two contracts on one URL. The flat list is the
# revision-SYNC feed: it must serve retired and soft-deleted rows, because
# that is how a consumer converges on a retirement. The same URL is also the
# catalogue a storefront reads. The sync contract won, and the catalogue
# leaked the fixtures.
#
# They are separated here rather than in the data, because the rows are
# legitimately inactive and a syncing consumer is legitimately entitled to
# them. What is not legitimate is a stranger getting the same answer.


@pytest.fixture
def retired_fixtures(parent_category, child_category):
    """One live branch, plus the shape a smoke run leaves behind."""
    return [
        Category.objects.create(name="Smoke 1787331903", slug="smoke-1787331903", active=False),
        Category.objects.create(name="Authz 1787369370", slug="authz-1787369370", active=False),
        Category.objects.create(name="Gone", slug="gone-1787369371", deleted=True),
    ]


def _slugs(response):
    body = response.json()
    return {row["slug"] for row in (body.get("results") or body.get("items") or [])}


def test_an_anonymous_list_serves_the_catalogue_not_the_fixtures(
    anonymous_client, retired_fixtures, parent_category, child_category
):
    response = anonymous_client.get(f"{BASE}/categories/")

    assert response.status_code == 200
    slugs = _slugs(response)
    assert "vehicles" in slugs and "cars" in slugs
    assert not {s for s in slugs if s.startswith(("smoke-", "authz-", "gone-"))}


def test_no_row_an_anonymous_list_serves_is_inactive(
    anonymous_client, retired_fixtures, parent_category, child_category
):
    """The acceptance criterion, asserted on the FLAG rather than on slugs.

    A slug assertion passes the day somebody renames the smoke fixtures.
    """
    body = anonymous_client.get(f"{BASE}/categories/").json()
    rows = body.get("results") or body.get("items") or []
    assert rows
    assert [r for r in rows if r["active"] is False] == []
    assert [r for r in rows if r["deleted"]] == []


def test_a_staff_sync_still_sees_every_retirement(
    api_client, django_user_model, retired_fixtures, parent_category
):
    """Convergence, which is the reason the feed serves these at all.

    A consumer that cannot see a retirement cannot apply it, and silently
    truncating its feed would be a worse defect than the leak: it would keep
    a retired category forever and never learn why.
    """
    staff = django_user_model.objects.create(username="ops", is_staff=True)
    api_client.force_authenticate(staff)

    slugs = _slugs(api_client.get(f"{BASE}/categories/"))
    assert {"smoke-1787331903", "authz-1787369370", "gone-1787369371"} <= slugs


def test_a_service_principal_syncs_without_being_staff(
    anonymous_client, retired_fixtures, parent_category
):
    """A fleet service is the other legitimate sync reader, and it holds no
    user at all — `IsServiceRequest` is the fleet's word for it."""
    response = anonymous_client.get(
        f"{BASE}/categories/", HTTP_X_API_KEY="test-service-key"
    )

    assert {"smoke-1787331903", "authz-1787369370"} <= _slugs(response)


def test_the_public_list_and_the_tree_reads_agree(
    anonymous_client, retired_fixtures, parent_category, child_category
):
    """One catalogue, whichever door a client came through.

    The three tree reads share `visible_categories()`; before this the flat
    list did not, which is exactly how the two answers drifted apart.
    """
    listed = _slugs(anonymous_client.get(f"{BASE}/categories/"))
    roots = {r["slug"] for r in anonymous_client.get(f"{BASE}/categories/roots/").data}

    assert roots <= listed
    assert not {s for s in roots if s.startswith(("smoke-", "authz-"))}
