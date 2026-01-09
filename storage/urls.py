from django.urls import path
from .views import ObjectView

urlpatterns = [
    # Upload and Download
    path("objects/", ObjectView.as_view()),                  # POST
    path("objects/<uuid:object_id>/", ObjectView.as_view()) # GET

]
