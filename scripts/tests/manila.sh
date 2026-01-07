#!/bin/bash

set -xe

# source venv
cd workspace
source kolla-venv/bin/activate

CONFIG_DIR=$(pwd)/etc/kolla

# source admin rc
. $CONFIG_DIR/admin-openrc.sh

manila type-show default_share_type || manila type-create default_share_type True

openstack image show manila-service-image || (
  (ls manila-service-image-master.qcow2 || wget --no-verbose http://tarballs.openstack.org/manila-image-elements/images/manila-service-image-master.qcow2) && \
  openstack image create "manila-service-image" \
    --file manila-service-image-master.qcow2 \
    --disk-format qcow2 --container-format bare && \
  rm manila-service-image-master.qcow2
)

manila share-network-show demo-share-network1 || (
  manila share-network-create --name demo-share-network1 \
	  --neutron-net-id $(openstack network show demo-net -f value -c id) \
	  --neutron-subnet-id $(openstack subnet show demo-subnet -f value -c id)
)

openstack flavor show manila-service-flavor || openstack flavor create --id 100 --ram 2048 --disk 30 --vcpus 2

suffix=$RANDOM
manila create CIFS 1 --name demo-share-$suffix --share-network demo-share-network1 --share-type default_share_type

set +x

timeout_seconds=300
sleep_time=5
time=0
exit_status=('available' 'error' 'inactive')
while true; do
  status=$(openstack share show demo-share-$suffix -f value -c status)
  if [[ $time -gt $timeout_seconds ]]; then
    echo Timeout reached - exiting
    exit 1
  elif echo available | grep -q $status; then
    echo Share now available
    break
  elif echo ${exit_status[@]} | grep -q $status; then
    echo Share unexpected status
    exit 1
  fi
  time=$(( $time + $sleep_time ))
  sleep $sleep_time
done

