from rest_framework.views import APIView
from rest_framework.response import Response
import boto3, os
from botocore.config import Config
from .models import ObjectMetadata
import uuid
from django.shortcuts import get_object_or_404
from django.db.models import Max
from django.db import IntegrityError, transaction


def generate_object_key(filename: str) -> str:
    ext = filename.split(".")[-1] if "." in filename else ""
    uid = uuid.uuid4()
    return f"objects/{uid}.{ext}" if ext else f"objects/{uid}"


def _required_string(data, name):
    """A non-empty string field, or None.

    Absent, empty and wrong-typed collapse to the same answer. The caller has
    nothing useful to do with any of them, and the request body reaches
    generate_object_key and a CharField, neither of which tolerates a non-string.
    """
    value = data.get(name)
    return value if isinstance(value, str) and value else None


def _optional_string(data, name):
    value = data.get(name)
    return value if isinstance(value, str) and value else None


def _create_next_version(*, bucket, filename, object_key, content_type, size, attempts=5):
    """Allocate the next version for (bucket, filename) and create the row.

    The read and the write have to be one transaction. Previously the
    SELECT MAX(version) sat inside transaction.atomic() and the create() that
    used its result sat outside, so two concurrent uploads of the same file
    both read the same maximum, both computed the same next version, and the
    second violated the (bucket, original_filename, version) unique constraint
    — surfacing as a 500 on a request that was perfectly valid.

    select_for_update locks the existing rows for this (bucket, filename) so
    the second transaction waits and then reads the first one's row. The retry
    loop covers the remaining gap: with no rows yet there is nothing to lock,
    so two uploads of a brand-new filename can still race, and on SQLite —
    which ignores select_for_update outside a transaction it can lock — it is
    the only protection. IntegrityError there means someone else took the
    number, so read again and take the next one.
    """
    for attempt in range(attempts):
        try:
            with transaction.atomic():
                last_version = (
                    ObjectMetadata.objects
                    .select_for_update()
                    .filter(bucket=bucket, original_filename=filename)
                    .aggregate(Max("version"))
                    .get("version__max")
                )
                return ObjectMetadata.objects.create(
                    bucket=bucket,
                    object_key=object_key,
                    original_filename=filename,
                    version=(last_version or 0) + 1,
                    content_type=content_type,
                    size=size,
                )
        except IntegrityError:
            if attempt == attempts - 1:
                raise


s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["GARAGE_ENDPOINT"],
    aws_access_key_id=os.environ["GARAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["GARAGE_SECRET_KEY"],
    region_name=os.environ["GARAGE_REGION"],
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"}, 
    ),
)

class ObjectView(APIView):

    def get(self, request, object_id):
        obj = get_object_or_404(ObjectMetadata, id=object_id)

        if not obj.is_uploaded:
            return Response(
                {"error": "Upload not completed"},
                status=400
            )

        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": obj.bucket,
                "Key": obj.object_key,
                "ResponseContentDisposition": (
                    f'attachment; filename="{obj.original_filename}"'
                ),
            },
            ExpiresIn=300,
        )

        return Response({"download_url": url})

    

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}

        bucket_name = _required_string(data, "bucket")
        filename = _required_string(data, "filename")
        if not bucket_name or not filename:
            return Response(
                {"error": "bucket and filename are required"},
                status=400,
            )

        content_type = _optional_string(data, "content_type")

        size = data.get("size")
        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                return Response(
                    {"error": "size must be a non-negative integer"},
                    status=400,
                )

        object_key = generate_object_key(filename)
        obj = _create_next_version(
            bucket=bucket_name,
            filename=filename,
            object_key=object_key,
            content_type=content_type,
            size=size,
        )

        params = {
            "Bucket": bucket_name,
            "Key": object_key,
        }

        if content_type:
            params["ContentType"] = content_type

        upload_url = s3.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=300,
        )

        return Response({
            "object_id": str(obj.id),
            "upload_url": upload_url,
        })

class LatestObjectView(APIView):

    def get(self, request):
        bucket = request.query_params.get("bucket")
        filename = request.query_params.get("filename")

        if not bucket or not filename:
            return Response(
                {"error": "bucket and filename are required"},
                status=400,
            )

        obj = (
            ObjectMetadata.objects
            .filter(
                bucket=bucket,
                original_filename=filename,
            )
            .order_by("-version")
            .first()
        )

        if not obj:
            return Response(
                {"error": "File not found"},
                status=404,
            )

        download_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": obj.bucket,
                "Key": obj.object_key,
                "ResponseContentDisposition": (
                    f'attachment; filename="{obj.original_filename}"'
                ),
            },
            ExpiresIn=300,
        )

        return Response({
            "object_id": str(obj.id),
            "filename": obj.original_filename,
            "version": obj.version,
            "size": obj.size,
            "content_type": obj.content_type,
            "download_url": download_url,
        })