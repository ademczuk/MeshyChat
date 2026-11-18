"""The card route. Mounted from `meshychat/urls.py`; tests/test_every_url_module_is_mounted.py fails
if it is not."""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/cards", views.cards, name="cards"),
    path("v1/cards/usage", views.card_usage, name="card_usage"),
]
