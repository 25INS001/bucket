from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import ObjectMetadata
from .views import generate_object_key


@override_settings(DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage")
class ObjectApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_generate_object_key_preserves_extension_and_uses_objects_prefix(self):
        key = generate_object_key("firmware.bin")

        self.assertTrue(key.startswith("objects/"))
        self.assertTrue(key.endswith(".bin"))

    @patch("storage.views.s3.generate_presigned_url", return_value="https://s3.example/upload")
    def test_create_object_metadata_and_presigned_upload(self, mock_presign):
        response = self.client.post(
            "/api/objects/",
            {"bucket": "uploads", "filename": "firmware.bin", "content_type": "application/octet-stream", "size": 128},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("object_id", response.data)
        self.assertEqual(response.data["upload_url"], "https://s3.example/upload")
        self.assertEqual(ObjectMetadata.objects.count(), 1)
        mock_presign.assert_called_once()

    @patch("storage.views.s3.generate_presigned_url", return_value="https://s3.example/download")
    def test_latest_object_requires_bucket_and_filename(self, mock_presign):
        response = self.client.get("/api/objects/latest/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "bucket and filename are required")
        mock_presign.assert_not_called()

    @patch("storage.views.s3.generate_presigned_url", return_value="https://s3.example/download")
    def test_latest_object_returns_highest_version(self, mock_presign):
        ObjectMetadata.objects.create(bucket="uploads", object_key="objects/old.bin", original_filename="firmware.bin", version=1, is_uploaded=True)
        latest = ObjectMetadata.objects.create(bucket="uploads", object_key="objects/new.bin", original_filename="firmware.bin", version=2, is_uploaded=True)

        response = self.client.get("/api/objects/latest/?bucket=uploads&filename=firmware.bin")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["object_id"], str(latest.id))
        self.assertEqual(response.data["version"], 2)
        self.assertEqual(response.data["download_url"], "https://s3.example/download")
