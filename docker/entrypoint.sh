#!/bin/sh
# Renders src/config.json from the environment, then execs the service.
#
# The rendered file holds the database password and the S3 secret key, so it is
# written at runtime and never baked into an image layer.
set -eu

: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required}"

POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_SSL_MODE="${POSTGRES_SSL_MODE:-prefer}"
AWS_REGION="${AWS_REGION:-us-east-1}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-http://s3:8333}"
AUTH_SERVICE_URL="${AUTH_SERVICE_URL:-http://auth-service:8080}"
DROGON_LOG_LEVEL="${DROGON_LOG_LEVEL:-INFO}"

# envsubst substitutes literally and knows nothing about JSON, so a secret
# containing a quote or a backslash would render an unparseable config and
# Drogon would fail with a message about the file rather than the value.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

POSTGRES_PASSWORD="$(json_escape "$POSTGRES_PASSWORD")"
POSTGRES_USER="$(json_escape "$POSTGRES_USER")"
POSTGRES_DB="$(json_escape "$POSTGRES_DB")"
AWS_ACCESS_KEY_ID="$(json_escape "$AWS_ACCESS_KEY_ID")"
AWS_SECRET_ACCESS_KEY="$(json_escape "$AWS_SECRET_ACCESS_KEY")"

export POSTGRES_HOST POSTGRES_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
       POSTGRES_SSL_MODE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION \
       S3_ENDPOINT_URL AUTH_SERVICE_URL DROGON_LOG_LEVEL

envsubst < /app/src/config.json.template > /app/src/config.json
chmod 600 /app/src/config.json

exec /app/bucket-service "$@"
