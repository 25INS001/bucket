from rest_framework.views import APIView
from rest_framework.response import Response
import boto3, os
from botocore.config import Config
from .models import ObjectMetadata
import uuid
from django.shortcuts import get_object_or_404
from django.db.models import Max
from django.db import transaction


def generate_object_key(filename: str) -> str:
    ext = filename.split(".")[-1] if "." in filename else ""
    uid = uuid.uuid4()
    return f"objects/{uid}.{ext}" if ext else f"objects/{uid}"


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
        bucket_name = request.data["bucket"]
        filename = request.data["filename"]
        content_type = request.data.get("content_type")
        size = request.data.get("size")

        object_key = generate_object_key(filename)
        with transaction.atomic():
            last_version = (
                ObjectMetadata.objects
                .filter(
                    bucket=bucket_name,
                    original_filename=filename
                )
                .aggregate(Max("version"))
                .get("version__max")
            )

        next_version = (last_version or 0) + 1

        obj = ObjectMetadata.objects.create(
            bucket=bucket_name,
            object_key=object_key,
            original_filename=filename,
            version=next_version, 
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