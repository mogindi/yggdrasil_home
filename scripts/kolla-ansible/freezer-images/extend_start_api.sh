#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# Bootstrap and exit if KOLLA_BOOTSTRAP is set. This catches all cases of the
# variable being present, including an explicitly empty value.
if [[ "${!KOLLA_BOOTSTRAP[@]}" ]]; then
    if ! freezer-manage db update; then
        echo "Freezer database bootstrap failed" >&2
        exit 1
    fi
    exit 0
fi

. /usr/local/bin/kolla_httpd_setup
