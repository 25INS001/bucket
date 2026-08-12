"""Hostile input against the bucket API.

ObjectView.post indexes the parsed body directly:

    bucket_name = request.data["bucket"]
    filename    = request.data["filename"]

DRF does not turn a KeyError into a 400 — it propagates, and the request 500s.
So "a required field is missing" and "the body is the wrong shape" are the same
bug class here, and both are recorded below.
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

@pytest.mark.defect
@pytest.mark.xfail(
    strict=True,
    reason=(
        "PHAS-BUCKET-KEYERR: ObjectView.post reads request.data['bucket'] and "
        "['filename'] with no validation. A body missing either raises KeyError, "
        "which DRF does not translate, so the caller gets a 500 instead of a "
        "400. Fix: a serializer, or .get() with an explicit error response."
    ),
)
@pytest.mark.parametrize(
    "body",
    [{}, {"bucket": "uploads"}, {"filename": "a.bin"}],
    ids=["empty", "no-filename", "no-bucket"],
)
def test_missing_required_field_returns_500_not_400(api, fake_s3, body):
    resp = api.post(OBJECTS, body, content_type="application/json")
    assert_no_server_error(resp, f"POST with body {body}")


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


@pytest.mark.defect
@pytest.mark.xfail(
    strict=True,
    reason=(
        "PHAS-BUCKET-KEYERR: an empty body parses to {} rather than failing, so "
        "it reaches request.data['bucket'] and raises KeyError like any other "
        "missing field."
    ),
)
def test_an_empty_body_is_a_missing_field_not_a_parse_error(api, fake_s3):
    resp = api.post(OBJECTS, b"", content_type="application/json")
    assert_no_server_error(resp, "POST with an empty body")


@pytest.mark.defect
@pytest.mark.xfail(
    strict=True,
    reason="PHAS-BUCKET-KEYERR: a non-object body indexes into a list or string.",
)
@pytest.mark.parametrize("raw", [b"[]", b'"str"', b"123"], ids=["array", "string", "number"])
def test_non_object_bodies_crash(api, fake_s3, raw):
    resp = api.post(OBJECTS, raw, content_type="application/json")
    assert_no_server_error(resp, "POST with a non-object body")


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
def test_container_typed_filename_survives_by_accident(api, fake_s3, value):
    """generate_object_key does `"." in filename`, which is a valid membership
    test on a list or dict — so these produce an extensionless key instead of
    raising. Pinned as the accident it is, not as intended behaviour."""
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": value}, content_type="application/json"
    )
    assert_no_server_error(resp, f"POST with filename={value!r}")


@pytest.mark.defect
@pytest.mark.xfail(
    strict=True,
    reason=(
        "PHAS-BUCKET-NOVALIDATION: generate_object_key runs `\".\" in filename` "
        "and then filename.split('.'). A scalar non-string — int, bool, None — "
        "is not iterable, so the request 500s. Fix: validate filename is a "
        "non-empty string before building the key."
    ),
)
@pytest.mark.parametrize("value", [12345, True, None], ids=["int", "bool", "null"])
def test_scalar_non_string_filename_crashes_key_generation(api, fake_s3, value):
    resp = api.post(
        OBJECTS, {"bucket": "uploads", "filename": value}, content_type="application/json"
    )
    assert_no_server_error(resp, f"POST with filename={value!r}")


def test_a_negative_size_is_accepted(api, fake_s3):
    """size is a BigIntegerField, so a negative value stores without complaint.

    Recorded rather than asserted as correct: nothing reads size for a decision
    today, so a wrong value is cosmetic — but it is unvalidated.
    """
    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "a.bin", "size": -1},
        content_type="application/json",
    )
    assert_no_server_error(resp, "POST with size=-1")


@pytest.mark.defect
@pytest.mark.xfail(
    strict=True,
    reason=(
        "PHAS-BUCKET-NOVALIDATION: size goes straight into a BigIntegerField. "
        "A string, list or dict raises inside the field's get_prep_value during "
        "the INSERT, so the request 500s after the row is half-built."
    ),
)
@pytest.mark.parametrize("value", ["abc", [], {}], ids=["str", "list", "dict"])
def test_wrong_typed_size_crashes_the_insert(api, fake_s3, value):
    resp = api.post(
        OBJECTS,
        {"bucket": "uploads", "filename": "a.bin", "size": value},
        content_type="application/json",
    )
    assert_no_server_error(resp, f"POST with size={value!r}")


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

@pytest.mark.defect
@pytest.mark.xfail(
    strict=True,
    reason=(
        "PHAS-BUCKET-RACE: ObjectView.post wraps only the SELECT MAX(version) "
        "in transaction.atomic(); the create() that uses the result runs "
        "outside it. Two concurrent uploads of the same file therefore compute "
        "the same next version and the second violates the "
        "(bucket, original_filename, version) unique constraint, which "
        "surfaces as a 500. Fix: put the read and the write in one transaction, "
        "or use select_for_update."
    ),
)
def test_a_lost_update_on_version_allocation_is_a_500(api, fake_s3, object_factory, monkeypatch):
    """Simulates the interleaving deterministically.

    A real thread race is not reproducible enough to gate CI on, so instead the
    version query is forced to return a stale answer — exactly what the second
    request sees when both read before either writes.
    """
    from django.db.models.query import QuerySet

    object_factory(filename="fw.bin", version=1)

    original = QuerySet.aggregate

    def stale_aggregate(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if "version__max" in result:
            result["version__max"] = None  # as if no version existed yet
        return result

    monkeypatch.setattr(QuerySet, "aggregate", stale_aggregate)

    resp = api.post(OBJECTS, {"bucket": "uploads", "filename": "fw.bin"}, content_type="application/json")
    assert_no_server_error(resp, "a concurrent upload of the same filename")


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
