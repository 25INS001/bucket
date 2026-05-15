# Generated for CI/CD and Kubernetes runtime migrations.

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ObjectMetadata",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("bucket", models.CharField(max_length=128)),
                ("object_key", models.TextField(unique=True)),
                ("original_filename", models.CharField(max_length=255)),
                ("version", models.PositiveIntegerField(default=1)),
                ("size", models.BigIntegerField(blank=True, null=True)),
                ("content_type", models.CharField(blank=True, max_length=128, null=True)),
                ("checksum", models.CharField(blank=True, max_length=128, null=True)),
                ("is_uploaded", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-version"],
                "unique_together": {("bucket", "original_filename", "version")},
            },
        ),
    ]
