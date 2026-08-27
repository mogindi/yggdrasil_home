#!/usr/bin/env bash

set -euo pipefail

if ! command -v openstack >/dev/null 2>&1; then
  echo "openstack client is required" >&2
  exit 1
fi

service_id=$(openstack service list --service messaging -f value -c ID | head -n 1)
if [[ -z "${service_id}" ]]; then
  echo "The Keystone messaging service is not registered" >&2
  exit 1
fi

endpoint=$(openstack endpoint list --service "${service_id}" --interface public -f value -c URL | head -n 1)
if [[ -z "${endpoint}" ]]; then
  echo "The public Zaqar endpoint is not registered" >&2
  exit 1
fi

endpoint="${endpoint%/}"
if [[ "${endpoint}" != */v2 ]]; then
  endpoint="${endpoint}/v2"
fi
endpoint="${endpoint}/"

token=$(openstack token issue -f value -c id)
project_id="${OS_PROJECT_ID:-}"
if [[ -z "${project_id}" ]]; then
  project_id=$(openstack token issue -f value -c project_id)
fi
client_id=$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)
queue="yggdrasil-zaqar-test-${RANDOM}"

cleanup() {
  curl --fail --silent --show-error \
    -X DELETE \
    -H "X-Auth-Token: ${token}" \
    -H "Client-ID: ${client_id}" \
    -H "X-PROJECT-ID: ${project_id}" \
    "${endpoint}queues/${queue}" >/dev/null || true
}
trap cleanup EXIT

curl --fail --silent --show-error \
  -X PUT \
  -H "X-Auth-Token: ${token}" \
  -H "Client-ID: ${client_id}" \
  -H "X-PROJECT-ID: ${project_id}" \
  -H 'Content-Type: application/json' \
  --data '{"source":"yggdrasil-test"}' \
  "${endpoint}queues/${queue}" >/dev/null

curl --fail --silent --show-error \
  -H "X-Auth-Token: ${token}" \
  -H "Client-ID: ${client_id}" \
  -H "X-PROJECT-ID: ${project_id}" \
  "${endpoint}queues/${queue}" | grep -F 'source' >/dev/null

echo "Zaqar API and Keystone registration are working (${endpoint})"
