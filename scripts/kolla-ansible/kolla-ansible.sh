#!/bin/bash

# Based on https://docs.openstack.org/kolla-ansible/latest/user/quickstart.html

set -xe

# source venv
cd workspace
source kolla-venv/bin/activate

CONFIG_DIR=$(pwd)/etc/kolla
INVENTORY=$(pwd)/inventory

cd kolla-ansible/ansible/

kolla-ansible $@ -i $INVENTORY --configdir $CONFIG_DIR 
