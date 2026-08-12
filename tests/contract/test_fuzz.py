"""Hostile input against the bucket API.

ObjectView.post used to index the parsed body directly:

    bucket_name = request.data["bucket"]
    filename    = request.data["filename"]

DRF does not turn a KeyError into a 400 — it propagates, and the request 500s.
So "a required field is missing" and "the body is the wrong shape" were the same
bug class, and a wrong-typed value was a third variant of it: a non-string
filename raised inside generate_object_key, a non-integer size raised inside the
INSERT after the row was half-built.

All three now resolve to a 400 before anything is written or signed. The tests
below still cover each variant separately, because they failed in three
different places and a partial regression would only bring one of them back.
"""

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.fuzz, pytest.mark.django_db]

OBJECTS = "/api/objects/"
LATEST = "/api/objects/latest/"

HOSTILE_STRINGS = [
    pytest.param("' OR '1'='1", id="sql-tautology"),
    pytest.param("'; DROP TABLE storage_objectmetadata; --", id="sql-drop"),
    pytest.param("<script>alert(1)</script>", id="xss"),
    pytest.param("../../../../etc/passwd", id="path-traversal"),
    pytest.param("..\\..\\windows", id="windows-traversal"),
    pytest.param("%2e%2e%2f", id="encoded-traversal"),
    pytest.param("a" * 5000, id="very-long"),
    pytest.param("🙂" * 200, id="astral-plane"),
    pytest.param("file\r\nInjected: 1", id="crlf"),
    pytest.param("{{7*7}}", id="template-injection"),
]


def assert_no_server_error(resp, what):
    assert resp.status_code < 500, (
        f"{what} produced HTTP {resp.status_code} — an unhandled exception "
        "reached the WSGI layer"
    )


# --------------------------------------------------------------------------- #
# Missing and malformed bodies
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "body",
    [{}, {"bucket": "uploads"}, {"filename": "a.bin"},
     {"bucket": "uploads", "filename": ""}, {"bucket": "", "filename": "a.bin"}],
    ids=["empty", "no-filename", "no-bucket", "blank-filename", "blank-bucket"],
)
def test_missing_required_field_is_a_400(api, fake_s3, body):
    """These used to read request.data['bucket'] directly. DRF does not
    translate a KeyError, so a missing field was a 500."""
    resp = api.post(OBJECTS, body, content_type="application/json")
    assert_no_server_error(resp, f"POST with body {body}")
    assert resp.status_code == 400, f"body {body} returned {resp.status_code}"
    assert resp.json()["error"] == "bucket and filename are required"


@pytest.mark.parametrize(
    "raw,description",
    [
        (b"not json", "plain text"),
        (b"{", "truncated"),
    ],
    ids=["text", "truncated"],
)
def test_unparseable_bodies_are_rejected_cleanly(api, fake_s3, raw, description):
    """DRF's JSON parser raises ParseError, which it does render as a 400."""
    resp = api.post(OBJECTS, raw, content_type="application/json")
    assert resp.status_code == 400, f"a {description} body returned {resp.status_code}"


def test_an_empty_body_is_a_missing_field_not_a_parse_error(api, fake_s3):
    """An empty body parses to {} rather than failing, so it is handled by the
    missing-field path rather than by DRF's parser."""
    resp = api.post(OBJECTS, b"", content_type="application/json")
    assert_no_server_error(resp, "POST with an empty body")
    assert resp.status_code == 400


@pytest.mark.parametrize("raw", [b"[]", b'"str"', b"123"], ids=["array", "string", "number"])
def test_non_object_bodies_are_a_400(api, fake_s3, raw):
    """A body that parses but is not an object used to be indexed into."""
    resp = api.post(OBJECTS, raw, content_type="application/json")
    assert_no_server_error(resp, "POST with a non-object body")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Hostile field content
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
def test_hostile_filename_never_escapes_the_key_namespace(api, fake_s3, hostile):
    """Whatever the filename, the generated key stays under objects/<uuid>."""
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": hostile}, content_type="application/json"
    )
    assert_no_server_error(resp, f"POST with filename={hostile[:40]!r}")

    if resp.status_code == 200:
        key = fake_s3.last("put_object")["params"]["Key"]
        assert key.startswith("objects/"), f"filename produced key {key[:80]!r}"
        assert ".." not in key, f"traversal reached the key: {key[:80]!r}"
        assert "\r" not in key and "\n" not in key


@pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
def test_hostile_bucket_name(api, fake_s3, hostile):
    resp = api.post(
        OBJECTS, {"bucket": hostile, "filename": "a.bin"}, content_type="application/json"
    )
    assert_no_server_error(resp, f"POST with bucket={hostile[:40]!r}")


@pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
def test_hostile_latest_query_parameters(api, fake_s3, hostile):
    resp = api.get(LATEST, {"bucket": hostile, "filename": hostile})
    assert_no_server_error(resp, f"GET /latest/ with {hostile[:40]!r}")
    assert resp.status_code in (400, 404), f"unexpected {resp.status_code}"


def test_sql_injection_does_not_widen_the_latest_lookup(api, fake_s3, object_factory):
    """The ORM parameterises, so a tautology matches nothing rather than everything."""
    object_factory(filename="real.bin", version=1)
    resp = api.get(LATEST, {"bucket": "uploads", "filename": "' OR '1'='1"})
    assert resp.status_code == 404, "a SQL tautology matched a row"


@pytest.mark.parametrize("value", [[], {}, {"$ne": None}], ids=["list", "dict", "operator"])
def test_container_typed_filename_is_a_400(api, fake_s3, value):
    """These used to survive by accident rather than by design.

    generate_object_key does `"." in filename`, which is a valid membership test
    on a list or a dict, so a container filename produced an extensionless key
    and a stored row instead of raising. It is now rejected like any other
    non-string.
    """
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": value}, content_type="application/json"
    )
    assert_no_server_error(resp, f"POST with filename={value!r}")
    assert resp.status_code == 400, f"filename={value!r} returned {resp.status_code}"


@pytest.mark.parametrize("value", [12345, True, None], ids=["int", "bool", "null"])
def test_scalar_non_string_filename_is_a_400(api, fake_s3, value):
    """generate_object_key runs `"." in filename` then filename.split("."), so a
    scalar non-string used to raise before anything validated it."""
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": value}, content_type="application/json"
    )
    assert_no_server_error(resp, f"POST with filename={value!r}")
    assert resp.status_code == 400, f"filename={value!r} returned {resp.status_code}"


def test_a_negative_size_is_rejected(api, fake_s3):
    """size is a BigIntegerField, so a negative value used to store without
    complaint. Nothing reads it for a decision today, which is exactly why a
    wrong value would have gone unnoticed."""
    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "a.bin", "size": -1},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "size must be a non-negative integer"


def test_a_zero_size_is_allowed(api, fake_s3):
    """Zero is a legitimate size — an empty file — and must not be confused
    with absent by a truthiness check."""
    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "empty.bin", "size": 0},
        content_type="application/json",
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("value", ["abc", [], {}, True], ids=["str", "list", "dict", "bool"])
def test_wrong_typed_size_is_a_400(api, fake_s3, value):
    """size used to go straight into a BigIntegerField and raise inside the
    INSERT, so the request 500d after the row was half-built."""
    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "a.bin", "size": value},
        content_type="application/json",
    )
    assert_no_server_error(resp, f"POST with size={value!r}")
    assert resp.status_code == 400, f"size={value!r} returned {resp.status_code}"


def test_a_rejected_request_creates_no_row(api, fake_s3):
    """Validation happens before the insert, not during it."""
    from storage.models import ObjectMetadata

    api.post(OBJECTS, {"bucket": "uploads"}, content_type="application/json")
    api.post(OBJECTS, {"bucket": "uploads", "filename": "a.bin", "size": "abc"},
             content_type="application/json")
    assert ObjectMetadata.objects.count() == 0


# --------------------------------------------------------------------------- #
# Path parameters
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
def test_hostile_object_id(api, fake_s3, hostile):
    from urllib.parse import quote

    resp = api.get(f"{OBJECTS}{quote(hostile, safe='')}/")
    assert_no_server_error(resp, f"GET object {hostile[:40]!r}")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "candidate",
    [
        "00000000-0000-0000-0000-000000000000",
        str(uuid.uuid1()),
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
    ],
    ids=["nil", "uuid1", "max"],
)
def test_valid_but_unknown_uuids_are_404(api, fake_s3, candidate):
    assert api.get(f"{OBJECTS}{candidate}/").status_code == 404


# --------------------------------------------------------------------------- #
# Version allocation under contention
# --------------------------------------------------------------------------- #

def test_a_lost_update_on_version_allocation_is_recovered(api, fake_s3, object_factory,
                                                          monkeypatch):
    """A stale version read must not surface as a 500.

    A real thread race is not reproducible enough to gate CI on, so the version
    query is made to return a stale answer *once* — exactly what the loser of a
    race sees, having read the maximum before the winner committed. The first
    attempt then collides with the unique constraint, and the retry has to read
    the true maximum and take the next number.

    Staleness is injected once rather than permanently on purpose: a permanent
    stale read would defeat any retry strategy, so a test that did that would
    fail no matter how the code was written, and prove nothing about the fix.
    """
    from django.db.models.query import QuerySet

    object_factory(filename="fw.bin", version=1)

    original = QuerySet.aggregate
    calls = {"n": 0}

    def sometimes_stale(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if "version__max" in result:
            calls["n"] += 1
            if calls["n"] == 1:
                result["version__max"] = None  # as if no version existed yet
        return result

    monkeypatch.setattr(QuerySet, "aggregate", sometimes_stale)

    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": "fw.bin"}, content_type="application/json"
    )

    assert_no_server_error(resp, "a concurrent upload of the same filename")
    assert resp.status_code == 200, (
        f"the losing side of a version race returned {resp.status_code}"
    )
    assert calls["n"] >= 2, "the stale read did not trigger a retry"

    from storage.models import ObjectMetadata

    created = ObjectMetadata.objects.get(id=resp.json()["object_id"])
    assert created.version == 2, (
        f"the retry allocated version {created.version}, expected 2"
    )


def test_repeated_uploads_of_one_filename_allocate_distinct_versions(api, fake_s3):
    """The property the retry exists to preserve, checked sequentially.

    A threaded version of this test was tried and removed. The suite runs on
    in-memory SQLite, which serialises writers with a table lock and raises
    "database table is locked" rather than exhibiting the interleaving that
    Postgres would — so the test failed for a reason that had nothing to do
    with the code under test. Genuine concurrent behaviour needs the live
    layer and a real Postgres; the deterministic retry test above is the
    evidence that the recovery path works.
    """
    from storage.models import ObjectMetadata

    for _ in range(5):
        resp = api.post(
            OBJECTS,
            {"bucket": "uploads", "filename": "repeated.bin"},
            content_type="application/json",
        )
        assert resp.status_code == 200

    versions = sorted(
        ObjectMetadata.objects.filter(original_filename="repeated.bin").values_list(
            "version", flat=True
        )
    )
    assert versions == [1, 2, 3, 4, 5], f"versions allocated: {versions}"


# --------------------------------------------------------------------------- #
# Size
# --------------------------------------------------------------------------- #

def test_a_very_long_filename_is_handled(api, fake_s3):
    """original_filename is CharField(max_length=255). SQLite does not enforce
    it; Postgres does. Asserting no 500 keeps this honest on both."""
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": "f" * 10_000}, content_type="application/json"
    )
    assert_no_server_error(resp, "POST with a 10000-character filename")


def test_many_extra_fields_are_ignored(api, fake_s3):
    body = {"bucket": "uploads", "filename": "a.bin"}
    body.update({f"junk_{i}": i for i in range(2000)})
    resp = api.post(OBJECTS, body, content_type="application/json")
    assert_no_server_error(resp, "POST with 2000 extra fields")


def test_unknown_fields_cannot_set_is_uploaded(api, fake_s3):
    """A caller must not be able to declare their own upload complete."""
    from storage.models import ObjectMetadata

    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "a.bin", "is_uploaded": True, "version": 99},
        content_type="application/json",
    )
    if resp.status_code == 200:
        obj = ObjectMetadata.objects.get(id=resp.json()["object_id"])
        assert obj.is_uploaded is False, "the body marked the upload complete"
        assert obj.version == 1, "the body chose its own version number"
