"""URL map for the file-artifact surface.

Every path below is absolute, with no prefix.

`<str:file_id>` rather than `<uuid:file_id>` is deliberate, for the same reason
`apps/peers/urls.py` gives: the `uuid` converter would not MATCH a malformed id, so the
route would fall through to the resolver's HTML 404. Parsing the id inside the view gives
the application control of the 400 error shape: a malformed id is a 400, not a 404.

ALREADY MOUNTED in `meshychat/urls.py` (`path("", include("apps.files.urls"))`). Nothing
here needs adding there, and `tests/test_files.py` resolves against the PROJECT urlconf
rather than pinning its own - the previous round's route tests all passed while the
application served 404 precisely because each pinned ROOT_URLCONF to its own module.
"""
from django.urls import path

from . import views

urlpatterns = [
    # POST only.
    path("v1/files", views.create, name="files-create"),
    # The more specific paths come FIRST; Django matches in order, and
    # `v1/files/<str:file_id>` would otherwise swallow `v1/files/raw/<token>`.
    path("v1/files/raw/<str:token>", views.raw, name="files-raw"),
    path("v1/files/<str:file_id>/finalize", views.finalize, name="files-finalize"),
    path("v1/files/<str:file_id>/url", views.presign_url, name="files-presign-url"),
    # Direct grants: the owner lets another reader download the file (audit finding 5).
    path("v1/files/<str:file_id>/grants", views.file_grants, name="files-grants"),
    # Pickup receipts (apps/files/grants.py).
    path("v1/files/<str:file_id>/downloads", views.downloads, name="files-downloads"),
    # GET (metadata) and DELETE share one path.
    path("v1/files/<str:file_id>", views.metadata_or_delete, name="files-metadata"),
]
