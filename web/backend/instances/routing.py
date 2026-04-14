from django.urls import path

from . import consumers

websocket_urlpatterns = [
    path("ws/jobs/", consumers.JobsConsumer.as_asgi()),
    path("ws/screen/<int:instance_id>/", consumers.ScreenConsumer.as_asgi()),
    path("ws/input/<int:instance_id>/", consumers.InputConsumer.as_asgi()),
]
