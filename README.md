# Yggdrasil Home

Yggdrasil Home is an infrastructure automation repository for standing up a full OpenStack environment with:

- host preparation and hardening,
- Docker and networking setup,
- Ceph deployment via `cephadm`,
- OpenStack control plane deployment via `kolla-ansible`, and
- Day-2 post-deploy bootstrap tasks (client, images, service initialization, alerting/LMA).

The repo is built around `make` targets that orchestrate Ansible playbooks and helper scripts.

---

## What this repository is for

Use this repository when you want to:

- Deploy an **all-in-one (AIO)** or multi-node OpenStack lab.
- Deploy a reproducible dev environment with Vagrant.
- Run specific lifecycle steps independently (prepare, deploy, reconfigure, upgrade, destroy).
- Bootstrap OpenStack test resources and service integrations (Octavia, RGW, Magnum, Manila, Trove).
- Enable LMA alerts and optional PagerDuty integration.

---

## Prerequisites

Minimum tooling:

```bash
apt install -y git make ansible bash-completion
ansible-galaxy collection install ansible.netcommon:2.5.1
```

Clone:

```bash
git clone git@github.com:yggdrasil-cloud-dk/open-yggdrasil-stack.git
cd open-yggdrasil-stack
```

---

## Inventory and variables

The Makefile uses these variables:

- `ENV` (default: `hetzner-vagrant-dev01`): selects inventory directory under `ansible/inventory/<ENV>`.
- `ARGS`: extra flags passed through to some `ansible-playbook` commands.
- `TAGS`: tags used by `kollaansible-*tag*` targets.

Available inventories in-tree:

- `ansible/inventory/aio`
- `ansible/inventory/vagrant-dual-vm`

Examples:

```bash
# Show active ENV value
make print-ENV

# Use AIO inventory for a full deployment
make all-up ENV=aio

# Pass extra ansible options
make harden ENV=aio ARGS='--limit control01 -v'
```

---

## Quick start flows

### AIO inventory ("all-up")

If you want the full end-to-end deployment for the AIO inventory:

```bash
make all-up ENV=aio
```

This runs, in order:

1. `infra-up` (host/network/storage prerequisites)
2. `kollaansible-up` (images, configure, bootstrap, deploy, LMA)
3. `postdeploy-up` (post-deploy + OpenStack resources/services)

### Dev environment with Vagrant

```bash
make dev-up
```

This brings up Vagrant nodes and then executes `all-up` using the default `ENV`.

### Custom inventory

```bash
cp -r ansible/inventory/aio ansible/inventory/<env_name>
$EDITOR ansible/inventory/<env_name>/*
make all-up ENV=<env_name>
```

### Build your own inventory and deploy your own private cloud (step-by-step)

Use this flow if you want to deploy on your own hardware or VMs instead of the bundled examples.

1. **Prepare machines and access**
   - Provision hosts running a supported Linux distribution (Ubuntu is assumed by this repo).
   - Ensure passwordless SSH from your deployment controller to all target hosts.
   - Ensure hostnames resolve from the deployment controller (`/etc/hosts` or DNS).
   - Confirm each target host has the network interfaces you plan to use for:
     - primary connectivity,
     - OpenStack management VLAN,
     - Ceph public/cluster VLANs,
     - provider/external network.

2. **Create a new inventory**

   ```bash
   export ENV=my-private-cloud
   cp -r ansible/inventory/vagrant-dual-vm ansible/inventory/${ENV}
   ```

3. **Define hosts and groups in `hosts.yml`**
   - Edit `ansible/inventory/${ENV}/hosts.yml`.
   - In `all.hosts`, add your deployment controller and all cloud nodes.
   - Set per-host fields:
     - `ansible_host` (reachable IP or DNS name),
     - `inventory_index` (unique integer per deployment node),
     - `ansible_user` if your remote user is not default.
   - Keep group membership accurate:
     - `deployment_controller` should contain your control host.
     - `deployment_nodes` should contain all cloud nodes (compute/storage/control nodes used by playbooks).

4. **Set cloud networking variables**
   In `all.vars`, adapt these values to your network plan:
   - `network_primary_interface` (e.g. `bond0` or `eth0`),
   - `network_provider_gateway_interface`,
   - `network_netplan_overrides_file` VLAN IDs and subnet CIDRs,
   - `openstack_network_interface`,
   - `openstack_neutron_external_interface`,
   - `openstack_kolla_internal_vip_address`,
   - `openstack_public_network` (`cidr`, `gateway_ip`, allocation range),
   - Octavia/amphora network values (`openstack_amphora_*`).

5. **Set storage/Ceph variables**
   In `all.vars`, adapt:
   - `storage_create_loop_devices` (set to `false` on real disks),
   - `storage_ceph_osd_devices` (real block devices for OSDs),
   - `storage_ceph_default_pool_size` and `storage_ceph_default_min_pool_size` (for production, use replica values matching your failure domains).

6. **Set OpenStack release and service values**
   In `all.vars`, confirm:
   - `openstack_release`,
   - DNS forwarders (`openstack_designate_forwarders_*`),
   - `openstack_ceph_rgw_hosts` values,
   - any environment-specific quotas and worker counts.

7. **Validate inventory before deployment**

   ```bash
   make print-ENV ENV=${ENV}
   make init ENV=${ENV}
   make ping-nodes ENV=${ENV}
   make print-ansible-vars ENV=${ENV}
   ```

8. **Deploy infra and private cloud in phases**
   Run phased deployment first (recommended for first run):

   ```bash
   make infra-up ENV=${ENV}
   make kollaansible-up ENV=${ENV}
   make postdeploy-up ENV=${ENV}
   ```

   Or run full orchestration in one command:

   ```bash
   make all-up ENV=${ENV}
   ```

9. **Verify cluster and API availability**
   - Source OpenStack credentials generated by Kolla post-deploy (`/etc/kolla/admin-openrc.sh` on the controller).
   - Verify API:

   ```bash
   source /etc/kolla/admin-openrc.sh
   openstack endpoint list
   openstack compute service list
   openstack network agent list
   openstack volume service list
   ```

10. **Operate and maintain**
    - Reconfigure services after variable updates:
      - `make kollaansible-reconfigure ENV=${ENV}`
    - Upgrade:
      - `make all-upgrade ENV=${ENV}`
    - Destroy/decommission when needed (destructive):
      - `make clean ENV=${ENV}`

Production notes:
- The sample inventories are lab-oriented defaults. Review all IP ranges, interface names, storage device paths, and replica settings before using in production.
- Always run from a host with stable SSH connectivity to all nodes.

---

## LMA alerts and PagerDuty integration

Deploy full LMA bundle:

```bash
make kollaansible-lma
```

Deploy only Prometheus alert rules:

```bash
make prometheus-alerts
```

Render/apply Alertmanager PagerDuty config:

```bash
export PAGERDUTY_ROUTING_KEY=<pagerduty-integration-key>
export PAGERDUTY_SEVERITY_MAP='critical|warning'
make alertmanager-pagerduty
```

Notes:

- `PAGERDUTY_ROUTING_KEY` enables PagerDuty notifications.
- `PAGERDUTY_SEVERITY_MAP` is used for Alertmanager route matching.

---

## Make target inventory

Below is a complete catalog of Make targets in this repo.

### Setup / infrastructure targets

- `prepare-ansible` — links `/etc/ansible` to selected in-repo inventory/config.
- `harden` — runs host hardening playbook.
- `docker` — configures Docker.
- `vpn` — configures VPN.
- `provider-gateway-vip` — configures provider gateway VIP.
- `devices-configure` — configures host devices.
- `checks` — runs validation checks.
- `cephadm-deploy` — deploys Ceph via cephadm playbook.

### Kolla-Ansible deployment targets

- `kollaansible-images` — prepare/pull Kolla images.
- `kollaansible-prepare-full` — full Kolla prepare/configure playbook.
- `kollaansible-prepare` — Kolla configure-only stage (`-t configure`).
- `kollaansible-create-certs` — creates Octavia certificates.
- `kollaansible-bootstrap` — runs Kolla bootstrap servers.
- `kollaansible-prechecks` — runs Kolla prechecks.
- `kollaansible-deploy` — deploys OpenStack with retry-once behavior.
- `kollaansible-upgrade` — performs Kolla upgrade.
- `kollaansible-postdeploy` — runs Kolla post-deploy tasks.
- `kollaansible-lma` — deploys LMA playbook + reconfigures prometheus/alertmanager.
- `prometheus-alerts` — copies Prometheus rules + reconfigures Prometheus.
- `alertmanager-pagerduty` — renders Alertmanager config + reconfigures Alertmanager.

### OpenStack initialization targets

- `openstack-client-install` — installs OpenStack client tooling.
- `openstack-resources-init` — initializes OpenStack resources.
- `openstack-images-upload` — uploads cloud images.
- `symlink-etc-kolla` — symlinks `workspace/etc/kolla/*` into `/etc/kolla/`.
- `openstack-octavia` — initializes Octavia resources.
- `openstack-rgw` — initializes RGW resources.
- `openstack-magnum` — initializes Magnum resources.
- `openstack-manila` — initializes Manila resources.
- `openstack-trove` — initializes Trove resources.
- `openstack-remove-test-resources` — removes test resources.

### Bundles / orchestrated targets

- `init` — alias for `prepare-ansible`.
- `infra-up` — runs `harden docker vpn devices-configure provider-gateway-vip checks cephadm-deploy`.
- `kollaansible-up` — runs images + prepare + certs + bootstrap + prechecks + deploy + LMA.
- `postdeploy-up` — runs post-deploy, client install, resources init, kolla symlink, and service initialization.
- `all-up` — full pipeline: `infra-up kollaansible-up postdeploy-up`.
- `all-upgrade` — alias for `kollaansible-upgrade`.
- `dev-up` — `vagrant-up` then `all-up`.
- `dev-down` — destroys Vagrant VMs.
- `openstack-services` — parallel initialization of images + OpenStack services, then image cleanup.

### Utility and lifecycle targets

- `vagrant-install` — installs local Vagrant/KVM prerequisites from `vagrant/setup.sh`.
- `vagrant-up` — starts Vagrant environment.
- `vagrant-destroy` — destroys Vagrant VMs.
- `print-%` — prints value of any make variable (e.g. `make print-ENV`).
- `ping-nodes` — pings nodes from inventory.
- `print-ansible-vars` — dumps ansible `hostvars`.
- `print-tags` — extracts Kolla site tags to `/tmp/print-tags`.
- `kollaansible-tags-deploy` — deploys selected `TAGS`.
- `kollaansible-tags-upgrade` — upgrades selected `TAGS`.
- `kollaansible-fromtag-deploy` — deploy from a starting tag onward.
- `kollaansible-fromtag-upgrade` — upgrade from a starting tag onward.
- `kollaansible-up-upgrade` — images + prepare + prechecks + upgrade + LMA.
- `kollaansible-tags-reconfigure` — reconfigure selected `TAGS`.
- `kollaansible-reconfigure` — full Kolla reconfigure.
- `kollaansible-destroy` — destroys Kolla deployment (dangerous).
- `kollaansible-purge` — destroy then remove `workspace`.
- `cephadm-destroy` — destroys Ceph deployment (`-t destroy`).
- `devices-destroy` — destroys device configuration (`-t destroy`).
- `openstack-resources-destroy` — destroys initialized OpenStack resources.
- `clean` — purges Kolla, destroys Ceph and device configuration.

---

## Common command cookbook

```bash
# Full AIO deployment
make all-up ENV=aio

# Prepare only infrastructure
make infra-up ENV=aio

# Run only Kolla deploy pipeline
make kollaansible-up ENV=aio

# Reconfigure specific services
make kollaansible-tags-reconfigure ENV=aio TAGS='nova,neutron'

# Upgrade from a specific tag onward
make kollaansible-fromtag-upgrade ENV=aio TAGS='keystone'

# Tear down OpenStack resources
make openstack-resources-destroy ENV=aio
```

---

## Safety notes

- Many targets require root privileges and direct access to deployment hosts.
- Destructive targets: `kollaansible-destroy`, `kollaansible-purge`, `cephadm-destroy`, `devices-destroy`, `clean`, `openstack-resources-destroy`.
- Review inventory and variables (`make print-ENV`, `make print-ansible-vars`) before running deployment commands.
