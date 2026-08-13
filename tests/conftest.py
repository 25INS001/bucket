"""Fixtures for the bucket-service live suite.

Everything here talks to a running service over HTTP. There is no in-process
layer any more: the service is a C++ binary, so the Django test client and the
fake boto3 client that used to stand in for the object store both went with the
port.

The presigner keeps its own hermetic tests in tests/unit, built and run by
CMake — that is where the signing logic is checked without a stack.
"""

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that require a running bucket-service",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs a running bucket-service; pass --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_base_url():
    return os.getenv("BUCKET_BASE_URL", "http://localhost:8000").rstrip("/")
