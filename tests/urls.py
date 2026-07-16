from django.urls import include, path

# Mounted under the /catalog/api/ prefix the ported legacy-catalog test-suite uses;
# the host chooses its own prefix in production (urls.py carries none).
urlpatterns = [
    path("catalog/api/", include("stapel_categories.urls_v1")),
]
