from django.urls import path
from .views import ObjectView, LatestObjectView

urlpatterns = [
    # Upload and Download
    path("objects/", ObjectView.as_view()),                  # POST
    path("objects/<uuid:object_id>/", ObjectView.as_view()), # GET
     path("objects/latest/", LatestObjectView.as_view())
]
