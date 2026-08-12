#!/bin/bash

set -x

interface=$1

ip=$(ip -o -f inet addr | grep $interface | awk '{print $4}')
gw=$(ip route | grep "^default" | grep -o 'via .\+' | cut -d ' ' -f 2)
dns=$(resolvectl dns $interface | sed 's/.*: //g')

cat > /opt/revive_internet_access.sh << EOF
#!/bin/bash

while true; do
  if (! ping -c 3 8.8.8.8 > /dev/null 2>&1); then
    if (ip a | grep -q br-ex) && test -z \$(ip -o -f inet addr | grep br-ex | awk '{print \$4}'); then
      ip addr flush dev $interface
      ip addr add $ip dev br-ex
      ip link set br-ex up
      ip route add default via $gw dev br-ex
      resolvectl dns br-ex $dns

      ip r replace 10.0.10.0/24 via $(echo $ip | sed 's/\/.*//g')
      ip r replace 10.0.50.0/24 via $(echo $ip | sed 's/\/.*//g')
      ip r replace 10.0.60.0/24 via $(echo $ip | sed 's/\/.*//g')

    fi
  fi
  sleep 5
done
EOF

cat > /etc/systemd/system/internet-access-bridge.service << EOF
[Unit]
Wants=network-online.target
After=systemd-networkd.service network-online.target
PartOf=systemd-networkd.service

[Service]
ExecStart=-/bin/bash /opt/revive_internet_access.sh

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl reenable internet-access-bridge
systemctl start internet-access-bridge
