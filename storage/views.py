from rest_framework.views import APIView
from rest_framework.response import Response
import boto3, os
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["GARAGE_ENDPOINT"],
    aws_access_key_id=os.environ["GARAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["GARAGE_SECRET_KEY"],
    region_name=os.environ["GARAGE_REGION"],
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},  # 🔥 THIS LINE
    ),
)

class ObjectView(APIView):

    def get(self, request, bucket_name, key):
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket_name,
                "Key": key,
            },
            ExpiresIn=300,
        )
        url = url.replace("http://localhost:8088", "http://localhost:8088/s3", 1)
        print("Get",url)
        return Response({"download_url": url})
    def put(self, request, bucket_name, key):
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket_name,
                "Key": key,
            },
            ExpiresIn=300,
        )
        url = url.replace("http://localhost:8088", "http://localhost:8088/s3", 1)
        print("Post",url)
        return Response({"upload_url": url})
