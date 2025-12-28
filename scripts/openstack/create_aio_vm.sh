
set -xe

# Networks & Subnets

create_network_and_subnet () {
	subnet_cidr=$2
	subnet_range=$3
	openstack network show aio_net || openstack network create aio_net
	openstack subnet show aio_subnet || openstack subnet create aio_subnet --dns-nameserver 8.8.8.8  --allocation-pool ${subnet_range} --network $network --subnet-range ${subnet_cidr}
}

network=aio_net
subnet_cidr=172.16.0.0/24
subnet_range="start=172.16.0.101,end=172.16.0.254"
create_network_and_subnet $network $subnet_cidr $subnet_range

# Router

router=aio_router
openstack router show $router || openstack router create $router
openstack router set --external-gateway public1 $router
attached_subnet_ids=$(openstack router show $router -f value -c interfaces_info | sed "s/'/\"/g" | jq -r .[].subnet_id)

subnet_name=aio_subnet
echo $attached_subnet_ids | grep -q $(openstack subnet show $subnet_name -f value -c id) || openstack router add subnet $router $subnet_name


# Security Groups

nsg=aio_nsg
openstack security group show $nsg || openstack security group create $nsg
openstack security group rule create $nsg --protocol tcp --dst-port 22 || true
openstack security group rule create $nsg --protocol icmp || true  # TODO: remove

# Flavor

total_cpus=$(lscpu | grep "^CPU(s)" | awk '{print $2}')
needed_cpus=16

vcpus=$total_cpus
if [ $needed_cpus -lt $total_cpus ]; then vcpus=$needed_cpus; fi

openstack flavor show aio || openstack flavor create aio --private --project admin --ram 65536 --disk 300 --vcpus $vcpus

# Volume

openstack volume show aio-ceph || openstack volume create aio-ceph --size 300 

# User-data

cat > /tmp/user_data.sh <<EOF
#!/bin/bash

set -x

# enable password ssh
sed 's/.*PasswordAuthentication no/PasswordAuthentication yes/' -i /etc/ssh/sshd_config
rm -rf /etc/ssh/sshd_config.d/*
systemctl restart sshd

# change password
echo "ubuntu:ubuntu" | chpasswd

EOF

# VM and Floating IP

vm_name=aio
openstack server show $vm_name || ( \
openstack server create \
    --image jammy-server-cloudimg-amd64 \
    --flavor aio \
    --network $network \
    --user-data /tmp/user_data.sh \
    $vm_name -f value -c id && \
fip=$(openstack floating ip create public1 -f value -c floating_ip_address) && \
sleep 30 && \
openstack server add floating ip $vm_name $fip && \
openstack server add volume $vm_name aio-ceph
)


