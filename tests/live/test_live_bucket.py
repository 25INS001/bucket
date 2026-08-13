"""Black-box HTTP against a running bucket-service.

This is the whole API surface now. The Django service kept most of its coverage
in a hermetic layer driven by Django's test client and a fake boto3 — neither
of which exists after the C++ port, so the behaviour those tests described has
moved here, where it is asserted over the wire instead.

That is a real trade: these need the stack up, so they do not gate CI the way
the in-process layer did. What they buy is that every assertion is about the
deployed service rather than about a test double. The presigner keeps a
hermetic layer of its own in tests/unit — the part that can be wrong silently
is still checked with nothing running.

    BUCKET_BASE_URL=http://localhost:8000 \\
    BUCKET_TEST_TOKEN=<a valid access token> \\
    pytest --live tests/live
"""

import os
import uuid

import pytest
import requests

pytestmark = pytest.mark.live

OBJECTS = "/api/objects/"
LATEST = "/api/objects/latest/"
TIMEOUT = 15


@pytest.fixture(scope="session")
def token():
    """A user access token. Every route requires one since the port."""
    value = os.getenv("BUCKET_TEST_TOKEN")
    if not value:
        pytest.skip("BUCKET_TEST_TOKEN is not set; every route needs a credential")
    return value


@pytest.fixture(scope="session")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def service_up(live_base_url):
    try:
        requests.get(f"{live_base_url}{LATEST}", timeout=10)
    except requests.exceptions.RequestException as exc:
        pytest.fail(f"bucket is not reachable at {live_base_url}: {exc}")
    return True


def register(base, auth, filename, **extra):
    body = {"bucket": "uploads", "filename": filename}
    body.update(extra)
    return requests.post(f"{base}{OBJECTS}", headers=auth, json=body, timeout=TIMEOUT)


# --------------------------------------------------------------------------- #
# Authentication
#
# The Python service had none: AllowAny behind a public /bucket/ prefix. The
# previous version of this file asserted that, with a note saying that if it
# ever started returning 401 the posture needed revisiting. It does now.
# --------------------------------------------------------------------------- #

def test_anonymous_access_is_refused(live_base_url, service_up):
    resp = requests.get(f"{live_base_url}{LATEST}",
                        params={"bucket": "uploads", "filename": "nope"}, timeout=TIMEOUT)
    assert resp.status_code == 401, (
        f"bucket answered {resp.status_code} without a credential; every route "
        "is meant to sit behind JwtAuthFilter"
    )


def test_anonymous_cannot_mint_an_upload_url(live_base_url, service_up):
    """The one that mattered: an open POST here signs a PUT into any bucket."""
    resp = requests.post(f"{live_base_url}{OBJECTS}",
                         json={"bucket": "uploads", "filename": "anon.bin"}, timeout=TIMEOUT)
    assert resp.status_code == 401


def test_a_malformed_authorization_header_is_refused(live_base_url, service_up):
    resp = requests.get(f"{live_base_url}{LATEST}", headers={"Authorization": "Basic abc123"},
                        params={"bucket": "uploads", "filename": "nope"}, timeout=TIMEOUT)
    assert resp.status_code == 401


def test_a_garbage_token_is_refused(live_base_url, service_up):
    resp = requests.get(f"{live_base_url}{LATEST}", headers={"Authorization": "Bearer not.a.token"},
                        params={"bucket": "uploads", "filename": "nope"}, timeout=TIMEOUT)
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def test_latest_validates_parameters(live_base_url, service_up, auth):
    assert requests.get(f"{live_base_url}{LATEST}", headers=auth,
                        timeout=TIMEOUT).status_code == 400


@pytest.mark.parametrize("body", [{}, {"bucket": "uploads"}, {"filename": "a.bin"}],
                         ids=["empty", "no-filename", "no-bucket"])
def test_register_requires_bucket_and_filename(live_base_url, service_up, auth, body):
    resp = requests.post(f"{live_base_url}{OBJECTS}", headers=auth, json=body, timeout=TIMEOUT)
    assert resp.status_code == 400
    assert resp.json()["error"] == "bucket and filename are required"


@pytest.mark.parametrize("size", [-1, "12", 1.5, True], ids=["negative", "string", "float", "bool"])
def test_register_rejects_a_bad_size(live_base_url, service_up, auth, size):
    """Present-but-wrong is refused rather than ignored: it means the caller
    believes something the service does not."""
    resp = register(live_base_url, auth, f"size-{uuid.uuid4().hex}.bin", size=size)
    assert resp.status_code == 400, f"size={size!r} was accepted"


@pytest.mark.fuzz
@pytest.mark.parametrize("body", [{}, {"bucket": "uploads"}, [], "a string", 42],
                         ids=["empty", "no-filename", "array", "string", "number"])
def test_a_malformed_body_does_not_take_the_service_down(live_base_url, service_up, auth, body):
    requests.post(f"{live_base_url}{OBJECTS}", headers=auth, json=body, timeout=TIMEOUT)
    after = requests.get(f"{live_base_url}{LATEST}", headers=auth, timeout=TIMEOUT)
    assert after.status_code == 400, "the service stopped answering after a bad request"


# --------------------------------------------------------------------------- #
# Registration and versioning
# --------------------------------------------------------------------------- #

def test_registering_returns_an_id_and_a_signed_url(live_base_url, service_up, auth):
    body = register(live_base_url, auth, f"reg-{uuid.uuid4().hex}.bin").json()
    assert uuid.UUID(body["object_id"])
    assert "X-Amz-Signature=" in body["upload_url"]
    assert "X-Amz-Credential=" in body["upload_url"]


def test_the_object_key_does_not_leak_the_filename(live_base_url, service_up, auth):
    """Keys are random so one cannot be guessed from a name, and so two uploads
    of the same name never collide in the store."""
    filename = f"secret-name-{uuid.uuid4().hex}.bin"
    url = register(live_base_url, auth, filename).json()["upload_url"]
    assert filename not in url


def test_versions_increment_for_the_same_filename(live_base_url, service_up, auth):
    filename = f"ver-{uuid.uuid4().hex}.bin"
    first = register(live_base_url, auth, filename)
    second = register(live_base_url, auth, filename)
    assert first.status_code == second.status_code == 200
    assert first.json()["object_id"] != second.json()["object_id"]

    latest = requests.get(f"{live_base_url}{LATEST}", headers=auth,
                          params={"bucket": "uploads", "filename": filename}, timeout=TIMEOUT)
    assert latest.json()["version"] == 2
    assert latest.json()["object_id"] == second.json()["object_id"]


def test_the_same_filename_in_another_bucket_versions_separately(live_base_url, service_up, auth):
    filename = f"cross-{uuid.uuid4().hex}.bin"
    register(live_base_url, auth, filename)
    other = requests.post(f"{live_base_url}{OBJECTS}", headers=auth,
                          json={"bucket": "artifacts", "filename": filename}, timeout=TIMEOUT)
    assert other.status_code == 200

    latest = requests.get(f"{live_base_url}{LATEST}", headers=auth,
                          params={"bucket": "artifacts", "filename": filename}, timeout=TIMEOUT)
    assert latest.json()["version"] == 1, "versions leaked across buckets"


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

def test_unknown_object_is_404(live_base_url, service_up, auth):
    resp = requests.get(f"{live_base_url}{OBJECTS}{uuid.uuid4()}/", headers=auth, timeout=TIMEOUT)
    assert resp.status_code == 404


def test_a_malformed_uuid_is_404_not_500(live_base_url, service_up, auth):
    """It fails the uuid cast in Postgres; an id that cannot exist is not found."""
    resp = requests.get(f"{live_base_url}{OBJECTS}not-a-uuid/", headers=auth, timeout=TIMEOUT)
    assert resp.status_code == 404


def test_latest_for_an_unknown_file_is_404(live_base_url, service_up, auth):
    resp = requests.get(f"{live_base_url}{LATEST}", headers=auth,
                        params={"bucket": "uploads", "filename": f"missing-{uuid.uuid4().hex}"},
                        timeout=TIMEOUT)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Upload completion is decided by the object store
#
# The `is_uploaded` column was never written by anything — not here and not in
# the Python service — so the download path refused every request it was given.
# It asks S3 now, which cannot drift out of step with reality.
# --------------------------------------------------------------------------- #

def test_download_before_upload_is_refused(live_base_url, service_up, auth):
    registered = register(live_base_url, auth, f"pending-{uuid.uuid4().hex}.bin").json()
    resp = requests.get(f"{live_base_url}{OBJECTS}{registered['object_id']}/",
                        headers=auth, timeout=TIMEOUT)
    assert resp.status_code == 404
    assert resp.json()["error"] == "Upload not completed"


def test_latest_reports_an_unuploaded_version_as_not_downloadable(live_base_url, service_up, auth):
    """The Python service handed out a signed URL here regardless, pointing at
    an object that was never uploaded."""
    filename = f"pending-{uuid.uuid4().hex}.bin"
    register(live_base_url, auth, filename)

    body = requests.get(f"{live_base_url}{LATEST}", headers=auth,
                        params={"bucket": "uploads", "filename": filename},
                        timeout=TIMEOUT).json()
    assert body["downloadable"] is False
    assert "download_url" not in body


def test_full_upload_and_download_round_trip(live_base_url, service_up, auth):
    """Register, PUT through the presigned URL, resolve latest, GET it back.

    The only layer that proves the object store accepts our signature: a URL
    signed with the wrong region, addressing style or algorithm is well-formed
    and fails exactly here.
    """
    filename = f"live-{uuid.uuid4().hex}.bin"
    payload = b"phasicon bucket live probe"

    registered = register(live_base_url, auth, filename,
                          content_type="application/octet-stream")
    assert registered.status_code == 200, registered.text[:300]
    body = registered.json()

    put = requests.put(body["upload_url"], data=payload,
                       headers={"Content-Type": "application/octet-stream"},
                       timeout=30, verify=False)
    assert put.status_code in (200, 201, 204), (
        f"the object store refused the presigned PUT: {put.status_code} {put.text[:300]}"
    )

    latest = requests.get(f"{live_base_url}{LATEST}", headers=auth,
                          params={"bucket": "uploads", "filename": filename}, timeout=TIMEOUT)
    assert latest.status_code == 200, latest.text[:300]
    assert latest.json()["object_id"] == body["object_id"]
    assert latest.json()["downloadable"] is True

    fetched = requests.get(latest.json()["download_url"], timeout=30, verify=False)
    assert fetched.status_code == 200
    assert fetched.content == payload, "the object came back with different bytes"


def test_downloading_by_id_restores_the_original_filename(live_base_url, service_up, auth):
    """The stored key is a uuid, so without content-disposition every download
    arrives called something meaningless."""
    filename = f"named-{uuid.uuid4().hex}.txt"
    body = register(live_base_url, auth, filename, content_type="text/plain").json()
    requests.put(body["upload_url"], data=b"named", headers={"Content-Type": "text/plain"},
                 timeout=30, verify=False)

    download = requests.get(f"{live_base_url}{OBJECTS}{body['object_id']}/",
                            headers=auth, timeout=TIMEOUT)
    assert download.status_code == 200
    assert f'filename="{filename}"' in download.json()["download_url"] or \
           "response-content-disposition" in download.json()["download_url"]

    fetched = requests.get(download.json()["download_url"], timeout=30, verify=False)
    assert fetched.status_code == 200
    assert filename in fetched.headers.get("Content-Disposition", "")
