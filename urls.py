"""Root URLconf for stapel-categories — v1 canon mount (api-versioning.md §2, §6).

Canon: ``/<mod>/api/v1/...`` — the version segment sits right after ``api/``.
Hosts keep mounting ``include('stapel_categories.urls')`` under their
``.../api/`` prefix; this module contributes the mandatory ``v1/``
sub-prefix. The actual URL set (paths inside unchanged, incl. the DRF
router) lives in ``urls_v1.py``.
"""
from django.urls import include, path

urlpatterns = [
    path('v1/', include('stapel_categories.urls_v1')),
]
