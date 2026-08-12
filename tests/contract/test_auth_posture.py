"""Who can call the bucket API.

The answer today is: anyone who can reach the port.

storage/views.py declares plain APIView subclasses with no permission_classes,
and bucket/settings.py has no REST_FRAMEWORK block, so DRF falls back to its
default of AllowAny. Nothing anywhere in this service consults auth-service.

That is not automatically wrong — nginx only exposes /bucket/ and the service
may be intended as internal-only. What would be wrong is for it to become true
by accident, or to stop being noticed. So the posture is asserted explicitly:
if permission classes are added, these tests fail and have to be updated, which
is exactly the conversation worth forcing.
"""

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.django_db]

OBJECTS = "/api/objects/"
LATEST = "/api/objects/latest/"


def test_drf_default_permission_is_not_configured():
    """settings.py sets no REST_FRAMEWORK block at all."""
    from django.conf import settings

    configured = getattr(settings, "REST_FRAMEWORK", {})
    assert "DEFAULT_PERMISSION_CLASSES" not in configured, (
        "a default permission class is now configured — update this file to "
        "assert the new posture rather than the absence of one"
    )


@pytest.mark.parametrize(
    "view_name", ["ObjectView", "LatestObjectView"], ids=lambda v: v
)
def test_views_declare_no_permission_classes(view_name):
    from rest_framework.permissions import AllowAny

    from storage import views

    view = getattr(views, view_name)
    effective = getattr(view, "permission_classes", [AllowAny])
    assert list(effective) in ([AllowAny], []), (
        f"{view_name} now restricts access to {effective}. That is an "
        "improvement — replace this test with one that proves the restriction "
        "actually rejects an anonymous caller."
    )


def test_anonymous_caller_can_register_an_upload(api, fake_s3):
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": "anything.bin"}, content_type="application/json"
    )
    assert resp.status_code == 200, (
        "anonymous upload registration is now refused — the service gained "
        "authentication, so this file needs rewriting"
    )


def test_anonymous_caller_receives_a_working_upload_url(api, fake_s3):
    """The presigned URL is the capability. Handing one to an unauthenticated
    caller grants write access to the bucket for its lifetime."""
    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "x.bin"}, content_type="application/json")
    assert resp.json()["upload_url"]
    assert fake_s3.last("put_object") is not None


def test_anonymous_caller_can_download_any_object_by_id(api, fake_s3, object_factory):
    obj = object_factory()
    resp = api.get(f"{OBJECTS}{obj.id}/")
    assert resp.status_code == 200
    assert resp.json()["download_url"]


def test_anonymous_caller_can_resolve_the_latest_version(api, fake_s3, object_factory):
    object_factory(filename="fw.bin", version=1)
    assert api.get(f"{LATEST}?bucket=uploads&filename=fw.bin").status_code == 200


def test_object_ids_are_unguessable(api, fake_s3, object_factory):
    """With no authentication, the UUID primary key is the only thing standing
    between an outsider and an object. It must be random, not sequential."""
    object_ids = [object_factory(filename=f"f{i}.bin").id for i in range(5)]

    # .version here is the UUID variant, not ObjectMetadata.version. 4 means
    # random; 1 would be time-and-MAC based and therefore predictable.
    assert all(oid.version == 4 for oid in object_ids), (
        "object ids are no longer UUID4 — anything sequential or time-ordered "
        "makes every object enumerable, because nothing else guards them"
    )
    assert len(set(object_ids)) == 5


def test_a_bearer_token_is_neither_required_nor_rejected(api, fake_s3, object_factory):
    """Callers that do send a credential are not penalised for it — the header
    is simply ignored."""
    obj = object_factory()
    with_token = api.get(f"{OBJECTS}{obj.id}/", HTTP_AUTHORIZATION="Bearer anything-at-all")
    assert with_token.status_code == 200


def test_the_bucket_name_is_taken_from_the_caller(api, fake_s3):
    """Combined with no authentication, this means an outsider chooses which
    bucket to be signed into. There is no allowlist of bucket names.
    """
    api.post(
        OBJECTS,
        {"bucket": "some-other-bucket", "filename": "x.bin"},
        content_type="application/json",
    )
    assert fake_s3.last("put_object")["params"]["Bucket"] == "some-other-bucket", (
        "the bucket is now constrained — if there is an allowlist, test it here"
    )
