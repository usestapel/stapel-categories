"""URL patterns — no global prefix here, the host project mounts them:

    path("categories/", include("stapel_categories.urls"))
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CategoryTreeView, CategoryViewSet, FeatureViewSet

router = DefaultRouter()
router.register(r"categories", CategoryViewSet, basename="category")
router.register(r"features", FeatureViewSet, basename="feature")

urlpatterns = [
    # Beside the router, not inside it: the nested tree is a read of the
    # whole catalogue, not an action on the `categories` collection, and the
    # storefront asks for it by the short name `/tree/`.
    path("tree/", CategoryTreeView.as_view(), name="category-tree"),
    *router.urls,
]
