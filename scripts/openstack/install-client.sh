#!/bin/bash

# Based on https://docs.openstack.org/kolla-ansible/latest/user/quickstart.html

set -xe


OPENSTACK_RELEASE="${OPENSTACK_RELEASE:-2023.2}"

# source venv
cd workspace
source kolla-venv/bin/activate

# install openstack clients
pip install -U -c https://releases.openstack.org/constraints/upper/$OPENSTACK_RELEASE \
  openstacksdk \
  python-openstackclient \
  python-mistralclient \
  python-heatclient \
  python-troveclient \
  python-magnumclient \
  python-cloudkittyclient \
  gnocchiclient \
  aodhclient \
  python-swiftclient \
  python-neutronclient \
  python-designateclient \
  python-muranoclient \
  python-manilaclient \
  python-solumclient \
  python-zunclient \
  python-octaviaclient \
  python-barbicanclient \
  python-freezerclient==6.1.0 \
  python-senlinclient \
  python-masakariclient

# python-senlinclient still supplies legacy formatter callables while the
# 2025.2 osc-lib release only accepts FormattableColumn classes.
pip install --no-deps 'osc-lib==3.2.0'
