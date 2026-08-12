#!/bin/bash

cat > /etc/systemd/system/network_routes.service <<EOF
[Unit]
Description=Network routes for openstack deployment
Wants=network-online.target
After=systemd-networkd.service network-online.target
PartOf=systemd-networkd.service

[Service]
ExecStart=/bin/bash -c "ip route replace $OPENSTACK_MGMT_NET via $NETWORK_PRIMARY_GATEWAY_IP"
ExecStart=/bin/bash -c "ip route replace $CEPH_PUBLIC_NET via $NETWORK_PRIMARY_GATEWAY_IP"
ExecStart=/bin/bash -c "ip route replace $NETWORK_PROVIDER_NET via $NETWORK_PROVIDER_GATEWAY_IP"
ExecStart=-/bin/bash -c "iptables-save | grep -q '\-A FORWARD -o virbr0 -j ACCEPT' || iptables -I FORWARD 1 -o virbr0 -j ACCEPT"  # this allows routing between provider and primary networks
ExecStart=-/bin/bash -c "iptables-save | grep -q '\-A FORWARD -o virbr1 -j ACCEPT' || iptables -I FORWARD 1 -o virbr1 -j ACCEPT"  # this allows routing between provider and primary networks
Type=oneshot
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart network_routes.service
systemctl reenable network_routes.service
