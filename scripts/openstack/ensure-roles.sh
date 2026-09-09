#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
KOLLA_WORKSPACE="${OPENSTACK_KOLLA_WORKSPACE:-$REPO_DIR/workspace}"
OPENSTACK_RC="${OPENSTACK_RC:-$KOLLA_WORKSPACE/etc/kolla/admin-openrc.sh}"
OPENSTACK_BIN="${OPENSTACK_BIN:-$KOLLA_WORKSPACE/kolla-venv/bin/openstack}"

if [[ ! -r "$OPENSTACK_RC" ]]; then
  echo "OpenStack admin credentials file not found: $OPENSTACK_RC" >&2
  exit 1
fi

if [[ -x "$OPENSTACK_BIN" ]]; then
  OPENSTACK=("$OPENSTACK_BIN")
elif command -v openstack >/dev/null 2>&1; then
  OPENSTACK=("$(command -v openstack)")
else
  echo "OpenStack client not found: $OPENSTACK_BIN" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$OPENSTACK_RC"

if (($#)); then
  roles=("$@")
else
  read -r -a roles <<< "${OPENSTACK_ROLES:-data_reader data_editor data_admin project_admin}"
fi

if ((${#roles[@]} == 0)); then
  echo "No OpenStack roles were provided." >&2
  exit 2
fi

for role in "${roles[@]}"; do
  if "${OPENSTACK[@]}" role show "$role" >/dev/null 2>&1; then
    echo "OpenStack role already exists: $role"
    continue
  fi

  "${OPENSTACK[@]}" role create "$role" >/dev/null
  echo "Created OpenStack role: $role"
done
