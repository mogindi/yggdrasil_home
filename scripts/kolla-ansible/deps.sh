#!/bin/bash

# Based on documentation from https://docs.openstack.org/kolla-ansible/latest/user/quickstart.html

set -xe

mkdir -p workspace
cd workspace

# update repos
apt update

# install python and deps
apt install -y python3-dev libffi-dev gcc libssl-dev python3-venv python3-pip libdbus-glib-1-dev #python3-openssl python3-docker

# ensure pyopenssl and docker installed and updated
#pip install -U pyopenssl docker  --break-system-packages

# refresh venv
rm -rf kolla-venv

# create venv
# NOTE: adding `--system-site-packages` because it needs python-apt module`
python3 -m venv --system-site-packages kolla-venv

# source path
source kolla-venv/bin/activate

# upgrade pip
pip install -U pip

# install ansible
ANSIBLE_SKIP_CONFLICT_CHECK=1 pip install -U --ignore-installed 'ansible-core>=2.18,<2.19' 

# get python path in venv
#PYTHON_PATH=$(realpath -s kolla-venv/bin/python)

# configure ansible
#ln -sf /etc/ansible/ansible.cfg ./kolla-ansible/ansible
