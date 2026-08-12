"""Fixtures for the bucket suite.

storage/views.py builds one module-level boto3 client and every view closes over
it. Swapping that single name is therefore enough to take the whole blueprint
offline, which is what `fake_s3` does.

The fake records the exact Params it was asked to sign. That matters more here
than the URL it returns: the presigned URL *is* the authorisation in this
service, so the Bucket and Key that go into it are the security-relevant output.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that require a running bucket service",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs a running bucket service; pass --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


class FakeS3Client:
    """Stands in for the module-level boto3 client in storage/views.py."""

    def __init__(self):
        self.signed = []
        self.fail = False

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None, **kwargs):
        if self.fail:
            raise RuntimeError("garage is unreachable")
        self.signed.append(
            {"operation": operation, "params": dict(Params or {}), "expires_in": ExpiresIn}
        )
        bucket = (Params or {}).get("Bucket", "unknown")
        key = (Params or {}).get("Key", "unknown")
        return f"https://garage.invalid/{bucket}/{key}?op={operation}&signed=1"

    def last(self, operation=None):
        entries = [s for s in self.signed if operation is None or s["operation"] == operation]
        return entries[-1] if entries else None


@pytest.fixture
def fake_s3(monkeypatch):
    from storage import views

    fake = FakeS3Client()
    monkeypatch.setattr(views, "s3", fake)
    return fake


@pytest.fixture
def api(client):
    """Django's test client. Named so tests read as API calls, not page loads."""
    return client


@pytest.fixture
def object_factory(db):
    from storage.models import ObjectMetadata

    def make(filename="firmware.bin", bucket="uploads", version=1, uploaded=True, **extra):
        return ObjectMetadata.objects.create(
            bucket=bucket,
            object_key=extra.pop("object_key", f"objects/{bucket}-{filename}-v{version}"),
            original_filename=filename,
            version=version,
            is_uploaded=uploaded,
            content_type=extra.pop("content_type", "application/octet-stream"),
            size=extra.pop("size", 1024),
            **extra,
        )

    return make


# --------------------------------------------------------------------------- #
# Live fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def live_base_url():
    import os

    return os.getenv("BUCKET_BASE_URL", "http://localhost:8000").rstrip("/")
