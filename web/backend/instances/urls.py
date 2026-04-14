from rest_framework.routers import DefaultRouter
from django.urls import path

from . import views

router = DefaultRouter()
router.register("", views.InstanceViewSet, basename="instance")

urlpatterns = [
    path("jobs/<str:job_id>/", views.JobStatusView.as_view(), name="instance-job-status"),
]
urlpatterns += router.urls
