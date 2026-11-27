#!/usr/bin/env bash

# Source the Kolla admin RC and switch its Keystone scope to system-wide.
# Usage: source scripts/openstack/admin-openrc-system-scope.sh

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Source this helper so its credentials and scope are set in your shell." >&2
  exit 2
fi

_YGGDRASIL_OPENSTACK_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
_YGGDRASIL_OPENSTACK_REPO_DIR="$(cd "${_YGGDRASIL_OPENSTACK_SCRIPT_DIR}/../.." && pwd)" || return 1
OPENSTACK_KOLLA_WORKSPACE="${OPENSTACK_KOLLA_WORKSPACE:-${_YGGDRASIL_OPENSTACK_REPO_DIR}/workspace}"
OPENSTACK_RC="${OPENSTACK_RC:-${OPENSTACK_KOLLA_WORKSPACE}/etc/kolla/admin-openrc.sh}"

if [[ ! -r "${OPENSTACK_RC}" ]]; then
  echo "OpenStack admin credentials file not found or unreadable: ${OPENSTACK_RC}" >&2
  unset _YGGDRASIL_OPENSTACK_SCRIPT_DIR _YGGDRASIL_OPENSTACK_REPO_DIR
  return 1
fi

# shellcheck disable=SC1090
source "${OPENSTACK_RC}" || {
  unset _YGGDRASIL_OPENSTACK_SCRIPT_DIR _YGGDRASIL_OPENSTACK_REPO_DIR
  return 1
}

# Scope settings in an RC file override system scope during authentication.
unset OS_PROJECT_NAME OS_PROJECT_ID OS_TENANT_NAME OS_TENANT_ID
unset OS_PROJECT_DOMAIN_NAME OS_PROJECT_DOMAIN_ID OS_DOMAIN_NAME OS_DOMAIN_ID
unset OS_DEFAULT_DOMAIN
export OS_SYSTEM_SCOPE=all

OPENSTACK_BIN="${OPENSTACK_BIN:-${OPENSTACK_KOLLA_WORKSPACE}/kolla-venv/bin/openstack}"
if [[ ! -x "${OPENSTACK_BIN}" ]] && command -v openstack >/dev/null 2>&1; then
  OPENSTACK_BIN="$(command -v openstack)"
fi
export OPENSTACK_BIN

OPENSTACK_PYTHON="${OPENSTACK_PYTHON:-${OPENSTACK_KOLLA_WORKSPACE}/kolla-venv/bin/python3}"
if [[ ! -x "${OPENSTACK_PYTHON}" ]]; then
  OPENSTACK_PYTHON="$(command -v python3)"
fi
export OPENSTACK_PYTHON

unset _YGGDRASIL_OPENSTACK_SCRIPT_DIR _YGGDRASIL_OPENSTACK_REPO_DIR
