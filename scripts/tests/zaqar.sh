#!/usr/bin/env bash

set -Eeuo pipefail

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/workspace"
config_dir="${OPENSTACK_CONFIG_DIR:-${workspace_dir}/etc/kolla}"
kolla_venv="${OPENSTACK_KOLLA_VENV:-${workspace_dir}/kolla-venv}"

if [[ -f "${kolla_venv}/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${kolla_venv}/bin/activate"
fi
if [[ -f "${config_dir}/admin-openrc.sh" ]]; then
  # shellcheck disable=SC1091
  source "${config_dir}/admin-openrc.sh"
fi

if ! command -v openstack >/dev/null 2>&1; then
  echo "openstack client is required" >&2
  exit 1
fi

service_id=$(openstack service list -f value -c ID -c Type |
  awk '$2 == "messaging" { print $1; exit }')
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
message_marker="zaqar-roundtrip-${RANDOM}-$$"

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

curl --fail --silent --show-error \
  -X POST \
  -H "X-Auth-Token: ${token}" \
  -H "Client-ID: ${client_id}" \
  -H "X-PROJECT-ID: ${project_id}" \
  -H 'Content-Type: application/json' \
  --data "{\"messages\":[{\"body\":{\"message\":\"${message_marker}\"},\"ttl\":3600}]}" \
  "${endpoint}queues/${queue}/messages" >/dev/null

read_response=$(curl --fail --silent --show-error \
  -H "X-Auth-Token: ${token}" \
  -H "Client-ID: ${client_id}" \
  -H "X-PROJECT-ID: ${project_id}" \
  "${endpoint}queues/${queue}/messages?limit=10&echo=true&include_claimed=true&include_delayed=true&marker=__zaqar_start__")

MESSAGE_MARKER="${message_marker}" python3 -c '
import json
import os
import sys

marker = os.environ["MESSAGE_MARKER"]
payload = json.load(sys.stdin)
messages = payload.get("messages", [])
if not any(message.get("body", {}).get("message") == marker
           for message in messages):
    raise SystemExit("written Zaqar message was not returned by the queue")
' <<<"${read_response}"

echo "Zaqar queue write/read round trip passed (${endpoint}queues/${queue})"
