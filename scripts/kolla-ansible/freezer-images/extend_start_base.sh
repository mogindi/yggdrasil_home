#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

LOG_DIR=/var/log/kolla/freezer
mkdir -p "${LOG_DIR}"
if [[ "$(id -u)" -eq 0 ]]; then
    chown freezer:freezer "${LOG_DIR}"
fi
chmod 0755 "${LOG_DIR}"

if [[ -f /usr/local/bin/kolla_freezer_extend_start ]]; then
    . /usr/local/bin/kolla_freezer_extend_start
fi
