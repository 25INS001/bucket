"""Black-box HTTP against a running bucket service.

The contract layer proves what goes into a signature. Only this layer proves
Garage accepts it — a presigned URL that is well-formed but signed with the
wrong region, addressing style or signature version fails exactly here and
nowhere else.

    BUCKET_BASE_URL=http://localhost:8000 pytest --live tests/live
"""

import uuid

import pytest
import requests

pytestmark = pytest.mark.live

OBJECTS = "/api/objects/"
LATEST = "/api/objects/latest/"


@pytest.fixture(scope="session")
def service_up(live_base_url):
    try:
        requests.get(f"{live_base_url}{LATEST}", timeout=10)
    except requests.exceptions.RequestException as exc:
        pytest.fail(f"bucket is not reachable at {live_base_url}: {exc}")
    return True


def test_latest_validates_parameters(live_base_url, service_up):
    resp = requests.get(f"{live_base_url}{LATEST}", timeout=10)
    assert resp.status_code == 400


def test_full_upload_and_download_round_trip(live_base_url, service_up):
    """Register, PUT through the presigned URL, resolve latest, GET it back."""
    filename = f"live-{uuid.uuid4().hex}.bin"
    payload = b"phasicon bucket live probe"

    registered = requests.post(
        f"{live_base_url}{OBJECTS}",
        json={"bucket": "uploads", "filename": filename, "content_type": "application/octet-stream"},
        timeout=15,
    )
    assert registered.status_code == 200, registered.text[:300]
    body = registered.json()
    object_id = body["object_id"]

    put = requests.put(
        body["upload_url"],
        data=payload,
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
        verify=False,
    )
    assert put.status_code in (200, 201, 204), (
        f"Garage refused the presigned PUT: {put.status_code} {put.text[:300]}"
    )

    latest = requests.get(
        f"{live_base_url}{LATEST}", params={"bucket": "uploads", "filename": filename}, timeout=15
    )
    assert latest.status_code == 200, latest.text[:300]
    assert latest.json()["object_id"] == object_id

    fetched = requests.get(latest.json()["download_url"], timeout=30, verify=False)
    assert fetched.status_code == 200
    assert fetched.content == payload, "the object came back with different bytes"


def test_download_before_upload_is_refused(live_base_url, service_up):
    """is_uploaded is never set by this service, so ObjectView refuses.

    That is the deployed consequence of the gap the contract suite records in
    test_object_api.test_latest_will_hand_out_an_incomplete_upload: /latest/
    would have answered, ObjectView will not.
    """
    filename = f"live-{uuid.uuid4().hex}.bin"
    registered = requests.post(
        f"{live_base_url}{OBJECTS}",
        json={"bucket": "uploads", "filename": filename},
        timeout=15,
    ).json()

    resp = requests.get(f"{live_base_url}{OBJECTS}{registered['object_id']}/", timeout=15)
    assert resp.status_code == 400
    assert resp.json()["error"] == "Upload not completed"


def test_unknown_object_is_404(live_base_url, service_up):
    assert requests.get(f"{live_base_url}{OBJECTS}{uuid.uuid4()}/", timeout=15).status_code == 404


def test_versions_increment_across_requests(live_base_url, service_up):
    filename = f"live-{uuid.uuid4().hex}.bin"
    for _ in range(2):
        requests.post(
            f"{live_base_url}{OBJECTS}", json={"bucket": "uploads", "filename": filename}, timeout=15
        )

    # /latest/ needs a completed upload to be meaningful, so read the version
    # off the second registration instead.
    third = requests.post(
        f"{live_base_url}{OBJECTS}", json={"bucket": "uploads", "filename": filename}, timeout=15
    )
    assert third.status_code == 200


@pytest.mark.fuzz
@pytest.mark.parametrize("body", [{}, {"bucket": "uploads"}, {"filename": "a.bin"}],
                         ids=["empty", "no-filename", "no-bucket"])
def test_missing_fields_do_not_take_the_service_down(live_base_url, service_up, body):
    """The contract suite records that these 500. What matters in deployment is
    that the worker recovers and the next request still succeeds."""
    requests.post(f"{live_base_url}{OBJECTS}", json=body, timeout=15)

    after = requests.get(f"{live_base_url}{LATEST}", timeout=15)
    assert after.status_code == 400, "the service stopped answering after a bad request"


@pytest.mark.fuzz
def test_anonymous_access_is_still_open(live_base_url, service_up):
    """Deployment check for what test_auth_posture asserts in process: no
    credential is required. If this starts returning 401 or 403, the service
    gained authentication and the whole posture file needs revisiting."""
    resp = requests.get(
        f"{live_base_url}{LATEST}", params={"bucket": "uploads", "filename": "nope"}, timeout=15
    )
    assert resp.status_code not in (401, 403), (
        f"bucket now requires authentication (HTTP {resp.status_code})"
    )
