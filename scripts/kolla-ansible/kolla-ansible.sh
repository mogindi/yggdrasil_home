#!/bin/bash

# Based on https://docs.openstack.org/kolla-ansible/latest/user/quickstart.html

set -xe

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# source venv
cd workspace
source kolla-venv/bin/activate

CONFIG_DIR=$(pwd)/etc/kolla
INVENTORY=$(pwd)/inventory

cd kolla-ansible/ansible/

kolla-ansible "$@" -i "$INVENTORY" --configdir "$CONFIG_DIR"

if [[ "${1:-}" == "post-deploy" ]]; then
    bash "$SCRIPT_DIR/ensure-admin-openrc.sh" "$CONFIG_DIR/admin-openrc.sh"
fi

case "${1:-}" in
    deploy|post-deploy)
        if [[ -f "$CONFIG_DIR/admin-openrc.sh" ]]; then
            source "$CONFIG_DIR/admin-openrc.sh"
            python "$SCRIPT_DIR/enable-cloudkitty-gnocchi-collector.py"
        else
            echo "Skipping CloudKitty Gnocchi collector reconciliation until admin-openrc.sh is available; run post-deploy after the first deployment."
        fi
        ;;
esac
