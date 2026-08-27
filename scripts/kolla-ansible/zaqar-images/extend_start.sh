#!/bin/bash

set -o errexit

LOG_DIR=/var/log/kolla/zaqar

if [[ ! -d "${LOG_DIR}" ]]; then
    mkdir -p "${LOG_DIR}"
fi

chown zaqar:kolla "${LOG_DIR}"
chown -R zaqar:zaqar /var/lib/zaqar
