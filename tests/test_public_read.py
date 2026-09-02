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
