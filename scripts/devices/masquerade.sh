#!/bin/bash

set -xe

sudo apt install -y net-tools

cat > /etc/systemd/system/network_masquerade.service <<EOF
[Unit]
Description=Masquerade vlans when exiting primary interface
Wants=network-online.target
After=systemd-networkd.service network-online.target
PartOf=systemd-networkd.service

[Service]
Environment=DEFAULT_GW_INTERFACE=$(ip r | grep ^default | head -n 1 | grep -o "dev .*" | cut -d ' ' -f 2)
ExecStart=/bin/bash -c "iptables-save | grep -q \"\-A POSTROUTING -o \${DEFAULT_GW_INTERFACE} -j MASQUERADE\" || iptables -t nat -A POSTROUTING -o \${DEFAULT_GW_INTERFACE} -j MASQUERADE"
Type=oneshot
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart network_masquerade.service
systemctl reenable network_masquerade.service
