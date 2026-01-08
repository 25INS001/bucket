#!/bin/sh
set -e

echo "Waiting for Garage..."
sleep 5

NODE_ID=$(/garage status | awk '/HEALTHY/{getline; print $1}')

/garage layout assign "$NODE_ID" -z local --capacity 10G || true
/garage layout apply --version 1 || true

/garage bucket create blobs || true

/garage key create app-key || true

/garage bucket allow blobs --key app-key --read --write || true

echo "Garage initialized"
