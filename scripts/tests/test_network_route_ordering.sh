#!/bin/bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
devices_playbook="$repo_root/ansible/devices.yml"
veth_script="$repo_root/scripts/devices/veth_pair.sh"
route_table_script="$repo_root/scripts/devices/create-route-table.sh"

grep -Fq 'Requires=network_veth_device.service' "$devices_playbook"
grep -Fq 'After=systemd-networkd.service network-online.target network_veth_device.service' "$devices_playbook"
grep -Fq 'PartOf=systemd-networkd.service network_veth_device.service' "$devices_playbook"
grep -Fq 'Restart=on-failure' "$devices_playbook"
grep -Fq 'RestartSec=5s' "$devices_playbook"

grep -Fqx 'Requires=sys-subsystem-net-devices-br0.device' "$veth_script"
grep -Fqx 'After=systemd-networkd.service network-online.target sys-subsystem-net-devices-br0.device' "$veth_script"

grep -Fq 'Requires=network_veth_device.service' "$route_table_script"
grep -Fq 'After=systemd-networkd.service network-online.target network_veth_device.service' "$route_table_script"
grep -Fq 'PartOf=systemd-networkd.service network_veth_device.service' "$route_table_script"
grep -Fq 'Restart=on-failure' "$route_table_script"
grep -Fq 'RestartSec=5s' "$route_table_script"

echo "network route ordering test passed"
