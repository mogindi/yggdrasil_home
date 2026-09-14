#!/bin/bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 PATH_TO_ADMIN_OPENRC" >&2
    exit 2
fi

openrc_file="$1"
if [[ ! -f "$openrc_file" ]]; then
    echo "error: admin openrc file was not created: $openrc_file" >&2
    exit 1
fi

if grep -Eq '^[[:space:]]*(export[[:space:]]+)?OS_BACKUP_API_VERSION=' "$openrc_file"; then
    sed -i -E \
        's/^[[:space:]]*(export[[:space:]]+)?OS_BACKUP_API_VERSION=.*/export OS_BACKUP_API_VERSION=2/' \
        "$openrc_file"
else
    printf '\n# Freezer API version enabled by the deployed Freezer service.\nexport OS_BACKUP_API_VERSION=2\n' \
        >> "$openrc_file"
fi

if grep -Eq '^[[:space:]]*(export[[:space:]]+)?OS_INFRA_OPTIM_API_VERSION=' "$openrc_file"; then
    sed -i -E \
        's/^[[:space:]]*(export[[:space:]]+)?OS_INFRA_OPTIM_API_VERSION=.*/export OS_INFRA_OPTIM_API_VERSION=1.0/' \
        "$openrc_file"
else
    printf '\n# Watcher API version required by python-watcherclient.\nexport OS_INFRA_OPTIM_API_VERSION=1.0\n' \
        >> "$openrc_file"
fi
