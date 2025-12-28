
set -xe

if [[ -z $1 ]]; then echo please give the agrument for user; exit 1; fi

USER=$1
PROJECT=${USER}_dev

# Networks & Subnets

create_network_and_subnet () {
	network=$1
	subnet_cidr=$2
	subnet_range=$3
	openstack network show $network || openstack network create $network --project $PROJECT
	openstack subnet show $network || openstack subnet create $network --project $PROJECT --dns-nameserver 10.38.1.130  --allocation-pool ${subnet_range} --network $network --subnet-range ${subnet_cidr}
}


# app 
network=$PROJECT.app
subnet_cidr=192.168.1.0/24
subnet_range="start=192.168.1.101,end=192.168.1.254"
create_network_and_subnet $network $subnet_cidr $subnet_range

# db
network=$PROJECT.db
subnet_cidr=192.168.2.0/24
subnet_range="start=192.168.2.101,end=192.168.2.254"
create_network_and_subnet $network $subnet_cidr $subnet_range

# auth
network=$PROJECT.auth
subnet_cidr=192.168.3.0/24
subnet_range="start=192.168.3.101,end=192.168.3.254"
create_network_and_subnet $network $subnet_cidr $subnet_range

# msg
network=$PROJECT.msg
subnet_cidr=192.168.4.0/24
subnet_range="start=192.168.4.101,end=192.168.4.254"
create_network_and_subnet $network $subnet_cidr $subnet_range

# mgmt
network=$PROJECT.mgmt
subnet_cidr=192.168.5.0/24
subnet_range="start=192.168.5.101,end=192.168.5.254"
create_network_and_subnet $network $subnet_cidr $subnet_range


# Router

router=$PROJECT.internal
openstack router show $router || openstack router create $router --project $PROJECT
openstack router set --external-gateway public1 $router
attached_subnet_ids=$(openstack router show $router -f value -c interfaces_info | sed "s/'/\"/g" | jq -r .[].subnet_id)

for subnet_name in $PROJECT.app $PROJECT.db $PROJECT.auth $PROJECT.msg $PROJECT.mgmt; do
	echo $attached_subnet_ids | grep -q $(openstack subnet show $subnet_name -f value -c id) || openstack router add subnet $router $subnet_name
done


# Security Groups

nsg=$PROJECT.app
openstack security group show $nsg || openstack security group create $nsg --project $PROJECT
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 22 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 3389 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 80 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 443 || true
openstack security group rule create $nsg --project $PROJECT --protocol icmp || true  # TODO: remove
openstack security group rule create $nsg --project $PROJECT --protocol tcp || true  # TODO: remove
openstack security group rule create $nsg --project $PROJECT --protocol udp || true  # TODO: remove

nsg=$PROJECT.db
openstack security group show $nsg || openstack security group create $nsg --project $PROJECT
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 22 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 3389 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 5432 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 3306 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 1433 || true

nsg=$PROJECT.auth
openstack security group show $nsg || openstack security group create $nsg --project $PROJECT
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 22 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 3389 || true

nsg=$PROJECT.msg
openstack security group show $nsg || openstack security group create $nsg --project $PROJECT
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 22 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 3389 || true

nsg=$PROJECT.mgmt
openstack security group show $nsg || openstack security group create $nsg --project $PROJECT
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 22 || true
openstack security group rule create $nsg --project $PROJECT --protocol tcp --dst-port 3389 || true
openstack security group rule create $nsg --project $PROJECT --protocol icmp || true  # TODO: remove
openstack security group rule create $nsg --project $PROJECT --protocol tcp || true  # TODO: remove
openstack security group rule create $nsg --project $PROJECT --protocol udp || true  # TODO: remove


router_interfaces_info=$(openstack router show $router -f value -c interfaces_info | sed "s/'/\"/g")
for subnet_name in $PROJECT.app $PROJECT.db $PROJECT.auth $PROJECT.msg $PROJECT.mgmt; do
	subnet_id=$(openstack subnet show $subnet_name -f value -c id)
	port_id=$(echo $router_interfaces_info | jq -r ".[] | select(.subnet_id==\"$subnet_id\") | .port_id")
	openstack port set --enable-port-security --security-group $subnet_name $port_id || true
done


# Jumpbox


cat > /tmp/user_data.sh <<EOF
#!/bin/bash

set -x

sed 's/.*PasswordAuthentication no/PasswordAuthentication yes/' -i /etc/ssh/sshd_config
rm -rf /etc/ssh/sshd_config.d/*

systemctl restart sshd

echo "ubuntu:ubuntu" | chpasswd
EOF

vm_name=$PROJECT.jump
openstack server list --project $PROJECT | grep -q $vm_name || (
  user=${PROJECT}.bot && \
  password=$(echo $RANDOM | sha1sum | head -c 16) && \
  openstack user create --password $password $user && \
  openstack role add --user $user --project $PROJECT member && \
  (openstack --os-username $user --os-password $password --os-project-name $PROJECT security group rule create default \
  --protocol tcp || true) && \
  (openstack --os-username $user --os-password $password --os-project-name $PROJECT security group rule create default \
  --protocol udp || true) && \
  vm_id=$(openstack --os-username $user --os-password $password --os-project-name $PROJECT server create \
    --image jammy-server-cloudimg-amd64 \
    --flavor m1.small \
    --network $PROJECT.mgmt \
    $vm_name -f value -c id --user-data /tmp/user_data.sh) && \
  openstack user delete $user && \
  fip=$(openstack floating ip create public1 --project $PROJECT -f value -c floating_ip_address) && \
  openstack server add floating ip $vm_id $fip
)



