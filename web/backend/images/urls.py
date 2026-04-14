from django.urls import path

from . import views

urlpatterns = [
    path("", views.list_images, name="image-list"),
    path("build/", views.build_image, name="image-build"),
    path("build/status/", views.build_status, name="image-build-status"),
    path("spoof/", views.spoof_image, name="image-spoof"),
    path("spoof/status/", views.spoof_status, name="image-spoof-status"),
    path("<path:image_id>/", views.remove_image, name="image-delete"),
]
