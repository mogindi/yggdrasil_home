
route_table_device_dependencies=
if printf '%s\n' "$routes" | tr ';' '\n' | grep -Eq '(^|[[:space:]])dev[[:space:]]+openstack_mgmt([[:space:]]|$)'; then
route_table_device_dependencies="Requires=sys-subsystem-net-devices-openstack_mgmt.device
After=sys-subsystem-net-devices-openstack_mgmt.device"
fi

cat > /etc/systemd/system/network_create_route_table_$table.service <<EOF
[Unit]
Description=Network route table $table for $network_cidr
Wants=network-online.target
Requires=network_veth_device.service
After=systemd-networkd.service network-online.target network_veth_device.service
PartOf=systemd-networkd.service network_veth_device.service
$route_table_device_dependencies

[Service]
ExecStart=/bin/bash -c "grep -q \"1 $table\" /etc/iproute2/rt_tables || ( echo \"1 $table\" | tee -a /etc/iproute2/rt_tables )"
ExecStart=/bin/bash -c "ip rule add from $network_cidr lookup $table"
ExecStart=/bin/bash -c "ip rule add to $network_cidr lookup $table"
EOF

_routes=$(echo $routes | tr ";" "\n")

while IFS= read -r line; do
cat >> /etc/systemd/system/network_create_route_table_$table.service <<EOF
ExecStart=/bin/bash -c "ip route add $line table $table"
EOF
done <<< "$_routes"

cat >> /etc/systemd/system/network_create_route_table_$table.service <<EOF
Type=oneshot
RemainAfterExit=yes
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart network_create_route_table_$table.service
systemctl reenable network_create_route_table_$table.service
