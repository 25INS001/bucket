"""storage/views.py — the object metadata and presigning API.

Three routes, and the whole design rests on one idea: bucket never touches
object bytes. It records metadata, mints a presigned URL, and lets the client
talk to Garage directly. So the tests that matter are about what goes into the
signature and how versions are allocated.

Note what is *not* tested here, because it does not exist: authentication. The
views are plain APIView subclasses and settings.py sets no
REST_FRAMEWORK.DEFAULT_PERMISSION_CLASSES, so DRF's default of AllowAny
applies. test_auth_posture.py covers that explicitly.
"""

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.django_db]

OBJECTS = "/api/objects/"
LATEST = "/api/objects/latest/"


# --------------------------------------------------------------------------- #
# POST /api/objects/ — register and presign an upload
# --------------------------------------------------------------------------- #

def test_upload_registration_returns_an_id_and_url(api, fake_s3):
    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "firmware.bin", "content_type": "application/octet-stream"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert uuid.UUID(body["object_id"])
    assert body["upload_url"].startswith("https://")


def test_upload_creates_a_metadata_row(api, fake_s3):
    from storage.models import ObjectMetadata

    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "firmware.bin"},
        content_type="application/json",
    )
    obj = ObjectMetadata.objects.get(id=resp.json()["object_id"])
    assert obj.original_filename == "firmware.bin"
    assert obj.bucket == "uploads"


def test_a_new_object_is_not_marked_uploaded(api, fake_s3):
    """The row is written before the bytes exist. Marking it uploaded here would
    let a download be issued for an object that was never PUT."""
    from storage.models import ObjectMetadata

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "a.bin"}, content_type="application/json")
    assert ObjectMetadata.objects.get(id=resp.json()["object_id"]).is_uploaded is False


def test_the_object_key_is_generated_not_taken_from_the_client(api, fake_s3):
    """generate_object_key() builds `objects/<uuid>.<ext>`.

    The client names the file; it does not choose where the file lands. If the
    filename reached the key directly, a caller could write over any object by
    naming it.
    """
    from storage.models import ObjectMetadata

    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "../../etc/passwd"},
        content_type="application/json",
    )
    key = ObjectMetadata.objects.get(id=resp.json()["object_id"]).object_key
    assert key.startswith("objects/")
    assert ".." not in key, f"a traversal in the filename reached the object key: {key}"


def test_the_key_keeps_the_original_extension(api, fake_s3):
    from storage.models import ObjectMetadata

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "model.onnx"}, content_type="application/json")
    assert ObjectMetadata.objects.get(id=resp.json()["object_id"]).object_key.endswith(".onnx")


def test_a_filename_without_an_extension_still_produces_a_key(api, fake_s3):
    from storage.models import ObjectMetadata

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "README"}, content_type="application/json")
    key = ObjectMetadata.objects.get(id=resp.json()["object_id"]).object_key
    assert key.startswith("objects/") and not key.endswith(".")


def test_object_keys_are_unique_across_identical_filenames(api, fake_s3):
    from storage.models import ObjectMetadata

    ids = [
        api.post(OBJECTS, {"bucket": "uploads", "filename": "same.bin"}, content_type="application/json").json()["object_id"]
        for _ in range(3)
    ]
    keys = {ObjectMetadata.objects.get(id=i).object_key for i in ids}
    assert len(keys) == 3, "two objects were given the same key — one would overwrite the other"


def test_the_upload_url_is_signed_for_the_generated_key(api, fake_s3):
    from storage.models import ObjectMetadata

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "a.bin"}, content_type="application/json")
    obj = ObjectMetadata.objects.get(id=resp.json()["object_id"])

    signed = fake_s3.last("put_object")
    assert signed["params"]["Key"] == obj.object_key
    assert signed["params"]["Bucket"] == "uploads"


def test_the_upload_url_expires(api, fake_s3):
    api.post(OBJECTS, {"bucket": "uploads", "filename": "a.bin"}, content_type="application/json")
    expires = fake_s3.last("put_object")["expires_in"]
    assert expires and 0 < expires <= 3600, f"upload URL expiry of {expires}s is not a sane bound"


def test_content_type_is_bound_into_the_signature_when_given(api, fake_s3):
    """Signing the content type stops a caller from uploading something other
    than what they declared."""
    api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "a.txt", "content_type": "text/plain"},
        content_type="application/json",
    )
    assert fake_s3.last("put_object")["params"]["ContentType"] == "text/plain"


def test_content_type_is_omitted_when_not_given(api, fake_s3):
    api.post(OBJECTS, {"bucket": "uploads", "filename": "a.bin"}, content_type="application/json")
    assert "ContentType" not in fake_s3.last("put_object")["params"]


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #

def test_the_first_version_is_one(api, fake_s3):
    from storage.models import ObjectMetadata

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "fw.bin"}, content_type="application/json")
    assert ObjectMetadata.objects.get(id=resp.json()["object_id"]).version == 1


def test_versions_increment_per_bucket_and_filename(api, fake_s3):
    from storage.models import ObjectMetadata

    versions = []
    for _ in range(3):
        resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "fw.bin"}, content_type="application/json")
        versions.append(ObjectMetadata.objects.get(id=resp.json()["object_id"]).version)
    assert versions == [1, 2, 3]


def test_versioning_is_scoped_to_the_bucket(api, fake_s3):
    from storage.models import ObjectMetadata

    api.post(OBJECTS, {"bucket": "uploads", "filename": "fw.bin"}, content_type="application/json")
    resp = api.post(OBJECTS, {"bucket": "other", "filename": "fw.bin"}, content_type="application/json")
    assert ObjectMetadata.objects.get(id=resp.json()["object_id"]).version == 1


def test_versioning_is_scoped_to_the_filename(api, fake_s3):
    from storage.models import ObjectMetadata

    api.post(OBJECTS, {"bucket": "uploads", "filename": "a.bin"}, content_type="application/json")
    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "b.bin"}, content_type="application/json")
    assert ObjectMetadata.objects.get(id=resp.json()["object_id"]).version == 1


def test_a_gap_in_versions_does_not_reuse_a_number(api, fake_s3, object_factory):
    """Deleting v2 must not make the next upload v2 again — unique_together
    would reject it, and a download URL for the old v2 would be ambiguous."""
    from storage.models import ObjectMetadata

    object_factory(filename="fw.bin", version=1)
    v2 = object_factory(filename="fw.bin", version=2)
    object_factory(filename="fw.bin", version=3)
    v2.delete()

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "fw.bin"}, content_type="application/json")
    assert ObjectMetadata.objects.get(id=resp.json()["object_id"]).version == 4


# --------------------------------------------------------------------------- #
# GET /api/objects/<id>/ — download
# --------------------------------------------------------------------------- #

def test_download_returns_a_url(api, fake_s3, object_factory):
    obj = object_factory()
    resp = api.get(f"{OBJECTS}{obj.id}/")
    assert resp.status_code == 200
    assert resp.json()["download_url"].startswith("https://")


def test_download_is_signed_for_the_stored_key(api, fake_s3, object_factory):
    obj = object_factory()
    api.get(f"{OBJECTS}{obj.id}/")
    signed = fake_s3.last("get_object")
    assert signed["params"]["Key"] == obj.object_key
    assert signed["params"]["Bucket"] == obj.bucket


def test_download_sets_the_original_filename_on_the_response(api, fake_s3, object_factory):
    """The stored key is an opaque UUID, so without this the browser saves the
    file under a meaningless name."""
    obj = object_factory(filename="firmware-v3.bin")
    api.get(f"{OBJECTS}{obj.id}/")
    disposition = fake_s3.last("get_object")["params"]["ResponseContentDisposition"]
    assert 'filename="firmware-v3.bin"' in disposition


def test_download_url_expires(api, fake_s3, object_factory):
    obj = object_factory()
    api.get(f"{OBJECTS}{obj.id}/")
    expires = fake_s3.last("get_object")["expires_in"]
    assert expires and 0 < expires <= 3600


def test_download_of_an_incomplete_upload_is_refused(api, fake_s3, object_factory):
    """is_uploaded guards against handing out a URL for bytes that never arrived."""
    obj = object_factory(uploaded=False)
    resp = api.get(f"{OBJECTS}{obj.id}/")
    assert resp.status_code == 400
    assert resp.json()["error"] == "Upload not completed"


def test_download_of_an_incomplete_upload_does_not_sign_anything(api, fake_s3, object_factory):
    obj = object_factory(uploaded=False)
    api.get(f"{OBJECTS}{obj.id}/")
    assert fake_s3.signed == []


def test_download_of_an_unknown_id_is_404(api, fake_s3):
    assert api.get(f"{OBJECTS}{uuid.uuid4()}/").status_code == 404


def test_download_with_a_malformed_id_is_404(api, fake_s3):
    """The URL converter is <uuid:object_id>, so a non-UUID never matches."""
    assert api.get(f"{OBJECTS}not-a-uuid/").status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/objects/latest/
# --------------------------------------------------------------------------- #

def test_latest_requires_bucket_and_filename(api, fake_s3):
    for query in ("", "?bucket=uploads", "?filename=fw.bin"):
        resp = api.get(f"{LATEST}{query}")
        assert resp.status_code == 400, f"{query!r} returned {resp.status_code}"
        assert resp.json()["error"] == "bucket and filename are required"


def test_latest_returns_the_highest_version(api, fake_s3, object_factory):
    object_factory(filename="fw.bin", version=1)
    object_factory(filename="fw.bin", version=3)
    object_factory(filename="fw.bin", version=2)

    body = api.get(f"{LATEST}?bucket=uploads&filename=fw.bin").json()
    assert body["version"] == 3


def test_latest_returns_the_full_descriptor(api, fake_s3, object_factory):
    obj = object_factory(filename="fw.bin", version=1, size=2048, content_type="application/wasm")
    body = api.get(f"{LATEST}?bucket=uploads&filename=fw.bin").json()
    assert body["object_id"] == str(obj.id)
    assert body["filename"] == "fw.bin"
    assert body["size"] == 2048
    assert body["content_type"] == "application/wasm"
    assert body["download_url"].startswith("https://")


def test_latest_is_scoped_to_the_bucket(api, fake_s3, object_factory):
    object_factory(filename="fw.bin", bucket="uploads", version=5)
    object_factory(filename="fw.bin", bucket="other", version=1)

    body = api.get(f"{LATEST}?bucket=other&filename=fw.bin").json()
    assert body["version"] == 1


def test_latest_for_an_unknown_file_is_404(api, fake_s3):
    resp = api.get(f"{LATEST}?bucket=uploads&filename=nope.bin")
    assert resp.status_code == 404
    assert resp.json()["error"] == "File not found"


@pytest.mark.defect
def test_latest_will_hand_out_an_incomplete_upload(api, fake_s3, object_factory):
    """LatestObjectView does not check is_uploaded, but ObjectView does.

    Register an upload and never PUT the bytes, and /latest/ still returns a
    download URL for it — pointing at an object that does not exist in Garage.
    The two views disagree about the same invariant.
    """
    object_factory(filename="fw.bin", version=1, uploaded=True)
    object_factory(filename="fw.bin", version=2, uploaded=False)

    body = api.get(f"{LATEST}?bucket=uploads&filename=fw.bin").json()
    assert body["version"] == 2, (
        "/latest/ now skips incomplete uploads — it agrees with ObjectView, so "
        "this test should become an assertion that version 1 is returned"
    )
