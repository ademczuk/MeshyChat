"""URL map for the board. Four GET reads, all peer-authenticated, and ONE POST: the door
that declares an act (`v1/board/acts`, since 2026-09-21).

`<path:subject>` rather than `<str:subject>` because a subject carries slashes
(`pr/933`, `issue/12`): MESH_SUBJECT_RE admits `/` and `str` stops at the first one.

Mounted from `meshychat/urls.py`. `tests/test_every_url_module_is_mounted.py` fails if it
is not, which is the guard that makes a module like this reachable rather than merely
present.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/board/subjects", views.subjects, name="board-subjects"),
    path("v1/board/subjects/<path:subject>", views.subject_state, name="board-subject"),
    path("v1/board/graph", views.board_graph, name="board-graph"),
    path("v1/board/me", views.my_acts, name="board-me"),
    path("v1/board/acts", views.declare_act, name="board-acts"),
]
