#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if (($# == 0)) || [[ "${1:-}" == "-h" ]] || [[ "${1:-}" == "--help" ]]; then
  exec python3 "${SCRIPT_DIR}/recover-heat-stack.py" "$@"
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/admin-openrc-system-scope.sh"
exec python3 "${SCRIPT_DIR}/recover-heat-stack.py" "$@"
