#!/usr/bin/env bash

set -Eeuo pipefail

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/workspace"
config_dir="${workspace_dir}/etc/kolla"

if [[ -f "${workspace_dir}/kolla-venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${workspace_dir}/kolla-venv/bin/activate"
fi
if [[ -f "${config_dir}/admin-openrc.sh" ]]; then
  # shellcheck disable=SC1091
  source "${config_dir}/admin-openrc.sh"
fi

service_id="$(openstack service list --format value --column ID --column Type \
  | awk '$2 == "backup" { print $1; exit }')"
if [[ -z "${service_id}" ]]; then
  echo "Freezer backup service is not registered in Keystone." >&2
  exit 1
fi

endpoint_count="$(openstack endpoint list --service "${service_id}" \
  --format value --column URL | wc -l)"
if [[ "${endpoint_count}" -lt 1 ]]; then
  echo "Freezer backup service has no Keystone endpoint." >&2
  exit 1
fi

command -v freezer >/dev/null
freezer --help >/dev/null
OS_BACKUP_API_VERSION=2 freezer job-list >/dev/null

echo "Freezer service registration, endpoint discovery, CLI, and v2 API smoke test passed."
