"""URL patterns — no global prefix here, the host project mounts them:

    path("categories/", include("stapel_categories.urls"))
"""
from rest_framework.routers import DefaultRouter

from .views import CategoryViewSet, FeatureViewSet

router = DefaultRouter()
router.register(r"categories", CategoryViewSet, basename="category")
router.register(r"features", FeatureViewSet, basename="feature")

urlpatterns = router.urls
