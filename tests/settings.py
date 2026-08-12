"""Test settings for the bucket suite.

bucket/settings.py fails closed: it raises unless BUCKET_SECRET_KEY and the
POSTGRES_* variables are present. storage/views.py goes further and reads
os.environ["GARAGE_*"] at import time, so a missing one is a KeyError before a
single test runs.

Rather than weaken either, this module supplies obviously-fake values first and
then imports the real settings on top, changing only the database. Every value
here is a placeholder — nothing in this file is or should be a credential.
"""

import os

_PLACEHOLDERS = {
    "BUCKET_SECRET_KEY": "test-only-not-a-real-secret-key",
    "BUCKET_DEBUG": "False",
    "BUCKET_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
    "POSTGRES_DB": "test-db",
    "POSTGRES_USER": "test-user",
    "POSTGRES_PASSWORD": "test-password",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    # storage/views.py indexes these directly at import.
    "GARAGE_ENDPOINT": "http://garage.invalid:3900",
    "GARAGE_ACCESS_KEY": "test-access-key",
    "GARAGE_SECRET_KEY": "test-secret-key",
    "GARAGE_REGION": "garage",
}

for _name, _value in _PLACEHOLDERS.items():
    os.environ.setdefault(_name, _value)

from bucket.settings import *  # noqa: F401,F403,E402

# SQLite in memory: the suite must not need a PostgreSQL server, and must never
# be able to reach a real one by accident.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Keep test output readable and hashing fast.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
LOGGING_CONFIG = None
