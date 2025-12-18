#!/bin/bash

# Based on https://docs.openstack.org/kolla-ansible/latest/user/quickstart.html

set -xe

# source venv
cd workspace
source kolla-venv/bin/activate

# install kolla-ansible
rm -rf kolla-ansible

BRANCH=stable/$OPENSTACK_RELEASE
if [[ $OPENSTACK_RELEASE == '2024.1' ]]; then
	BRANCH=unmaintained/2024.1
fi
git clone --branch $BRANCH https://github.com/openstack/kolla-ansible.git

# apply patch and setup
cd kolla-ansible
if [[ -s ../../kolla-ansible.patch ]]; then
  git apply --reject --whitespace=fix ../../kolla-ansible.patch
fi
python3 setup.py develop
cd ..

# install ansible galaxy deps
kolla-ansible install-deps

# create config dir for kolla
mkdir -p etc/kolla
mkdir -p etc/kolla/config/prometheus/prometheus.yml.d
mkdir -p /etc/kolla/haproxy/services.d

# copy config files to config dir
cp -r kolla-ansible/etc/kolla/globals.yml etc/kolla/globals.yml

# add section for additional configs
cat >> etc/kolla/globals.yml <<-EOF
	
	######################
	# Additional Configs #
	######################
	EOF

date_suffix=$(date +"%Y%m%dT%H%M")

PW_FILE=etc/kolla/passwords.yml
PW_FILE_BK=$PW_FILE.bk_$date_suffix

if [ -f "$PW_FILE" ]; then
	mv $PW_FILE $PW_FILE_BK
fi
cp kolla-ansible/etc/kolla/passwords.yml $PW_FILE
kolla-genpwd -p $PW_FILE
if [ -f "$PW_FILE_BK" ]; then
	kolla-mergepwd --old $PW_FILE_BK --new $PW_FILE --final $PW_FILE
        if [[ -z $(diff $PW_FILE $PW_FILE_BK) ]] || [[ ! -s $PW_FILE_BK ]]; then 
		rm $PW_FILE_BK
	fi
fi


# create inventory directory in workspace
mkdir -p inventory

cat kolla-ansible/ansible/inventory/all-in-one | sed -n '/common/,$p' > inventory/99-openstack_groups
