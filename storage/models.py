import uuid
from django.db import models

class ObjectMetadata(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    bucket = models.CharField(max_length=128)

    object_key = models.TextField(unique=True)

    original_filename = models.CharField(
        max_length=255, null=True, blank=True
    )

    size = models.BigIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=128, null=True, blank=True)
    checksum = models.CharField(max_length=128, null=True, blank=True)

    is_uploaded = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
