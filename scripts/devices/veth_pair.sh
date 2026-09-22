#!/bin/bash

set -xe

cat > /opt/veth_device.sh <<'EOT'
#!/bin/bash
set -euo pipefail

if ! ip link show br0 >/dev/null 2>&1; then
    echo "br0 is not available" >&2
    exit 1
fi

veth0_exists=false
veth1_exists=false
ip link show veth0 >/dev/null 2>&1 && veth0_exists=true
ip link show veth1 >/dev/null 2>&1 && veth1_exists=true

if [[ "$veth0_exists" != true || "$veth1_exists" != true ]]; then
    ip link delete veth0 >/dev/null 2>&1 || true
    ip link delete veth1 >/dev/null 2>&1 || true
    ip link add veth0 type veth peer name veth1
fi

ip link set veth0 up
ip link set veth1 up

veth1_master=$(readlink -f /sys/class/net/veth1/master 2>/dev/null || true)
if [[ -z "$veth1_master" || "$(basename "$veth1_master")" != br0 ]]; then
    ip link set dev veth1 nomaster >/dev/null 2>&1 || true
    ip link set dev veth1 master br0
fi
EOT

cat > /etc/systemd/system/network_veth_device.service <<EOF
[Unit]
Description=create veth device for openstack external gateway
Wants=network-online.target
BindsTo=sys-subsystem-net-devices-br0.device
After=systemd-networkd.service network-online.target sys-subsystem-net-devices-br0.device
PartOf=systemd-networkd.service

[Service]
ExecStart=/bin/bash /opt/veth_device.sh
Type=oneshot
RemainAfterExit=yes
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart network_veth_device.service
systemctl reenable network_veth_device.service
