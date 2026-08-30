SHELL:=/bin/bash

ENV = hetzner-vagrant-dev01
ARGS = 
TAGS = 
# Each environment has its own local Ansible vault password file.
override VAULT_PASSWORD_FILE := $(HOME)/.ansible_vault_$(ENV)
VAULT_ARGS := --vault-password-file "$(VAULT_PASSWORD_FILE)"

ANSIBLE_TARGETS = \
	harden \
	docker \
	vpn \
	provider-gateway-vip \
	devices-configure \
	checks \
	cephadm-deploy \
	kollaansible-images \
	kollaansible-prepare-full \
	kollaansible-prepare \
	kollaansible-create-certs \
	kollaansible-bootstrap \
	kollaansible-prechecks \
	kollaansible-deploy \
	kollaansible-upgrade \
	kollaansible-postdeploy \
	kollaansible-lma \
	prometheus-alerts \
	alertmanager-pagerduty \
	openstack-client-install \
	openstack-resources-init \
	openstack-octavia \
	openstack-rgw \
	openstack-magnum \
	openstack-manila \
	openstack-trove \
	openstack-trove-mongodb-test \
	print-ansible-vars \
	kollaansible-tags-deploy \
	kollaansible-tags-upgrade \
	kollaansible-fromtag-deploy \
	kollaansible-fromtag-upgrade \
	kollaansible-tags-reconfigure \
	kollaansible-reconfigure \
	kollaansible-destroy \
	cephadm-destroy \
	devices-destroy

.PHONY: check-vault-password-file

$(ANSIBLE_TARGETS): check-vault-password-file

check-vault-password-file:
	@if [[ ! -f "$(VAULT_PASSWORD_FILE)" ]]; then \
		echo "WARNING: Ansible vault password file not found: $(VAULT_PASSWORD_FILE)" >&2; \
		echo "Create the file before continuing, or press Ctrl-C to abort." >&2; \
		read -r -p "Create it, then press Enter to continue (Ctrl-C to abort): " _ || true; \
	fi
	@test -f "$(VAULT_PASSWORD_FILE)" || { \
		echo "Ansible vault password file is still missing: $(VAULT_PASSWORD_FILE)" >&2; \
		exit 1; \
	}

#########
# Setup #
#########

# TODO: run in ansible so it runs on all nodes
prepare-ansible:
	rm -rf /etc/ansible/
	mkdir -p /etc/ansible
	ln -sfr ansible/inventory/$(ENV) /etc/ansible/hosts
	ln -sfr ansible/ansible.cfg /etc/ansible/ansible.cfg

harden:
	ansible-playbook ansible/harden.yml $(VAULT_ARGS) $(ARGS)

docker:
	ansible-playbook ansible/docker.yml $(VAULT_ARGS) $(ARGS)

vpn: 
	ansible-playbook ansible/vpn.yml $(VAULT_ARGS) $(ARGS)

provider-gateway-vip: 
	ansible-playbook ansible/provider_gateway_vip.yml $(VAULT_ARGS) $(ARGS)

devices-configure:
	ansible-playbook ansible/devices.yml $(VAULT_ARGS) $(ARGS)

checks:
	ansible-playbook ansible/checks.yml $(VAULT_ARGS) $(ARGS)

cephadm-deploy:
	ansible-playbook ansible/cephadm.yml $(VAULT_ARGS) $(ARGS)

# kolla-ansible #

kollaansible-images:
	ansible-playbook ansible/prepare_images.yml $(VAULT_ARGS) $(ARGS)

kollaansible-prepare-full:
	ansible-playbook ansible/kolla_ansible.yml $(VAULT_ARGS) $(ARGS)

kollaansible-prepare:
	ansible-playbook ansible/kolla_ansible.yml -t configure $(VAULT_ARGS) $(ARGS)

kollaansible-create-certs:
	scripts/kolla-ansible/kolla-ansible.sh octavia-certificates $(VAULT_ARGS)

kollaansible-bootstrap:
	scripts/kolla-ansible/kolla-ansible.sh bootstrap-servers $(VAULT_ARGS)

kollaansible-prechecks:
	scripts/kolla-ansible/kolla-ansible.sh prechecks $(VAULT_ARGS)

kollaansible-deploy:
	scripts/kolla-ansible/kolla-ansible.sh deploy $(VAULT_ARGS) $(ARGS) || scripts/kolla-ansible/kolla-ansible.sh deploy $(VAULT_ARGS) $(ARGS)

kollaansible-upgrade:
	scripts/kolla-ansible/kolla-ansible.sh upgrade $(VAULT_ARGS) $(ARGS)

kollaansible-postdeploy:
	scripts/kolla-ansible/kolla-ansible.sh post-deploy $(VAULT_ARGS)

kollaansible-lma:
	ansible-playbook ansible/lma.yml -v $(VAULT_ARGS) $(ARGS)
	scripts/kolla-ansible/kolla-ansible.sh reconfigure -t prometheus $(VAULT_ARGS)

prometheus-alerts:
	ansible-playbook ansible/lma.yml -v -t copy_rules $(VAULT_ARGS) $(ARGS)
	scripts/kolla-ansible/kolla-ansible.sh reconfigure -t prometheus $(VAULT_ARGS)

alertmanager-pagerduty:
	ansible-playbook ansible/lma.yml -v -t pagerduty $(VAULT_ARGS) $(ARGS)
	scripts/kolla-ansible/kolla-ansible.sh reconfigure -t prometheus $(VAULT_ARGS)

# openstack #

openstack-client-install:
	ansible-playbook ansible/client.yml $(VAULT_ARGS) $(ARGS)

openstack-project-resources:
	@test -n "$(PROJECT)" || (echo "PROJECT is required, for example: make openstack-project-resources PROJECT=admin" >&2; exit 2)
	@if [[ -f workspace/kolla-venv/bin/activate ]]; then source workspace/kolla-venv/bin/activate; fi; scripts/openstack/list-project-resources.py --project "$(PROJECT)" $(ARGS)

openstack-resources-init:
	ansible-playbook ansible/init_resources.yml $(VAULT_ARGS) $(ARGS)
	#scripts/openstack/init-resources.sh

openstack-images-upload:
ifeq ($(ENV),aio)
	@echo "AIO: uploading Ubuntu Noble; Magnum uploads Fedora CoreOS 38 separately."
	scripts/openstack/upload-images.sh --noble-only
else
	scripts/openstack/upload-images.sh
endif

openstack-omarchy-image:
	dev_infra/omarchy-image-builder.sh $(OMARCHY_ARGS)

symlink-etc-kolla:
	ln -sfr workspace/etc/kolla/* /etc/kolla/

openstack-octavia:
	ansible-playbook ansible/openstack_initialize/octavia.yml $(VAULT_ARGS) $(ARGS)

openstack-rgw:
	ansible-playbook ansible/openstack_initialize/rgw.yml $(VAULT_ARGS) $(ARGS)

openstack-cinder-backup-test:
	scripts/tests/cinder-backup.sh

openstack-freezer-test:
	scripts/tests/freezer.sh

openstack-zaqar-test:
	scripts/tests/zaqar.sh

openstack-senlin-test:
	scripts/tests/senlin.sh

openstack-magnum:
#	scripts/tests/magnum.sh
	ansible-playbook ansible/openstack_initialize/magnum.yml $(VAULT_ARGS) $(ARGS)

openstack-manila:
#	scripts/tests/manila.sh
	ansible-playbook ansible/openstack_initialize/manila.yml $(VAULT_ARGS) $(ARGS)

openstack-trove:
#	scripts/tests/trove_postgres.sh
	ansible-playbook ansible/openstack_initialize/trove.yml $(VAULT_ARGS) $(ARGS)

openstack-trove-mongodb-image:
	scripts/openstack/upload-images.sh --trove-mongodb

openstack-trove-mongodb-test:
	ansible-playbook ansible/openstack_initialize/trove_mongodb.yml $(VAULT_ARGS) $(ARGS)

openstack-remove-test-resources:
	scripts/tests/remove-all.sh


###########
# Bundles #
###########

init: prepare-ansible

infra-up: harden docker vpn devices-configure provider-gateway-vip checks cephadm-deploy 

kollaansible-up: kollaansible-images kollaansible-prepare-full kollaansible-create-certs kollaansible-bootstrap kollaansible-prechecks kollaansible-deploy kollaansible-lma

postdeploy-up: kollaansible-postdeploy openstack-client-install openstack-resources-init symlink-etc-kolla openstack-services

all-up: infra-up kollaansible-up postdeploy-up

all-upgrade: kollaansible-upgrade

dev-up: vagrant-up all-up

dev-down: vagrant-destroy

openstack-services:
	ls ~/.ssh/id_rsa.pub || ssh-keygen -b 2048 -t rsa -f ~/.ssh/id_rsa -q -N ""
	$(MAKE) -j 10 -Oline openstack-images-upload openstack-octavia openstack-rgw openstack-magnum  openstack-manila openstack-trove
	source scripts/image-utils.sh && image_cleanup
#	$(MAKE) openstack-remove-test-resources


########
# Util #
########

vagrant-install:
	cd vagrant && ./setup.sh

vagrant-up:
	cd vagrant && vagrant up

vagrant-destroy:
	cd vagrant && vagrant destroy -f

# print make vars. Use like this "make print-ENV" to print ENV 
print-%  : ; @echo $* = $($*)

# ping nodes
ping-nodes:
	scripts/ping-nodes.sh

# print ansible inventory vars
print-ansible-vars:
	ansible all $(VAULT_ARGS) -m debug -a "var=hostvars"
	
# missing tags that are used with "import_playbook" list nova
print-tags:
	@grep "^        tags:" workspace/kolla-ansible/ansible/site.yml | sed 's/        tags: //g; s/ }//g; s/,.*//g; s/\[//g' | xargs | sed 's/ /,/g' | sed 's/,ovn,ovn/,ovn,nova/' |  tee /tmp/print-tags

kollaansible-tags-deploy: kollaansible-prepare
	scripts/kolla-ansible/kolla-ansible.sh deploy -t $(TAGS) $(VAULT_ARGS)

kollaansible-tags-upgrade: kollaansible-prepare
	scripts/kolla-ansible/kolla-ansible.sh upgrade -t $(TAGS) $(VAULT_ARGS)

# Set single tag
kollaansible-fromtag-deploy: kollaansible-prepare print-tags
	all_tags=$$(cat /tmp/print-tags) && \
	remaining_tags=$$(echo $$all_tags | grep -o $(TAGS).*) && \
	scripts/kolla-ansible/kolla-ansible.sh deploy -t $$remaining_tags $(VAULT_ARGS)

kollaansible-fromtag-upgrade: kollaansible-prepare print-tags
	all_tags=$$(cat /tmp/print-tags) && \
	remaining_tags=$$(echo $$all_tags | grep -o $(TAGS).*) && \
	scripts/kolla-ansible/kolla-ansible.sh upgrade -t $$remaining_tags $(VAULT_ARGS)

kollaansible-up-upgrade: kollaansible-images kollaansible-prepare kollaansible-prechecks kollaansible-upgrade kollaansible-lma

kollaansible-tags-reconfigure: kollaansible-check-tags kollaansible-prepare
	scripts/kolla-ansible/kolla-ansible.sh reconfigure -t $(TAGS) -v $(VAULT_ARGS)

kollaansible-check-tags:
	@test -n "$(strip $(TAGS))" || { echo "TAGS is required; use for example TAGS='nova,neutron'" >&2; exit 2; }

kollaansible-reconfigure: kollaansible-prepare
	scripts/kolla-ansible/kolla-ansible.sh reconfigure -v $(VAULT_ARGS)

kollaansible-destroy:
	scripts/kolla-ansible/kolla-ansible.sh destroy --yes-i-really-really-mean-it $(VAULT_ARGS)
	@echo -e "-----\nPLEASE REBOOT NODES\n-----"; sleep 5

kollaansible-purge: kollaansible-destroy
	@rm -rf workspace

cephadm-destroy:
	ansible-playbook ansible/cephadm.yml -t destroy $(VAULT_ARGS)

devices-destroy:
	ansible-playbook ansible/devices.yml -t destroy $(VAULT_ARGS)

openstack-resources-destroy:
	scripts/openstack/destroy-resources.sh

clean: kollaansible-purge cephadm-destroy devices-destroy
