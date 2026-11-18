from django.urls import path

from apps.modelhost import views

urlpatterns = [
    path("v1/modelhost/capabilities", views.capabilities, name="modelhost-capabilities"),
]
