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

### Kata-backed Zun containers

The supplied inventories enable Kata Containers 3.31.0 for Zun. Kata runs
each application container in a lightweight VM, while Kolla control-plane
containers continue to use Docker's default `runc` runtime.

Before deployment, every node in `deployment_nodes` must provide `/dev/kvm`.
Enable CPU virtualization in firmware on bare metal. For virtual deployment
nodes, enable nested virtualization on the outer hypervisor; the included
Terraform and Vagrant libvirt definitions use host CPU passthrough. Kata is
not supported when a deployment node is itself an ordinary LXC or Docker
container.

For a custom inventory, set `openstack_zun_kata_enabled: true` to enable Zun
and copy the pinned `kata_version`, `kata_architectures`, and `kata_checksums`
values from one of the supplied inventories. The deployment then performs
these steps in order:

1. `ansible/docker.yml` installs Docker without exposing its remote API and
   removes any legacy unauthenticated listener override.
2. `ansible/devices.yml` installs and validates Kata, creates the private
   Docker PKI, and enables a mutually authenticated Docker listener on each
   node's OpenStack management address at port 2376.
3. Kolla image preparation patches Zun's interactive-exec proxy to use those
   TLS credentials.
4. `scripts/kolla-ansible/configure.sh` selects Kata for Zun, mounts the
   client credentials, and prevents API callers (including administrators)
   from overriding the runtime.

The CA and signing key remain on the deployment controller under
`/etc/yggdrasil/zun-docker-pki`. Server certificates are unique per node;
leaf certificates are renewed when fewer than 30 days remain. The Docker API
is no longer available on unauthenticated port 2375.

After host preparation, useful checks on each deployment node are:

```bash
/opt/kata/bin/kata-runtime check
docker info --format '{{json .Runtimes}}'
docker run --rm --runtime kata busybox true
ss -ltn '( sport = :2375 or sport = :2376 )'
```

Also create a Zun smoke container, confirm its Docker
`HostConfig.Runtime` is `kata`, test Kuryr networking, and open an interactive
exec session. Existing Zun containers retain their original runtime: pause
new container creation during rollout and recreate existing workloads after
the hosts and Zun services have been reconfigured.

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
   - the RadosGW Cinder backup settings (`openstack_cinder_backup_s3_*`);
     leave the secret unset to have the RGW initialization playbook generate
     it,
   - any environment-specific quotas and worker counts.
   - Nova utilization-aware scheduling is enabled by default with
     `cpu.virt_driver` and `cpu.percent=-1.0`. Override these with
     `openstack_nova_compute_monitors` and
     `openstack_nova_metrics_weight_setting` when needed.
   - `openstack_zun_host_shared_with_nova` defaults to `true`, so Zun and
     Nova use the same Placement provider on shared compute hosts. In this
     mode, Zun preserves the Nova/Placement allocation ratios; the
     `openstack_zun_*_allocation_ratio` values apply when Zun uses its own
     provider.

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

   To inventory the resources visible to one project, run the repository CLI
   after sourcing the same credentials and client virtual environment:

   ```bash
   source workspace/kolla-venv/bin/activate
   scripts/openstack/list-project-resources.py --project admin
   scripts/openstack/list-project-resources.py --project admin --format json
   # equivalent Make target:
   make openstack-project-resources PROJECT=admin
   ```

   The CLI first reads the authenticated Keystone catalog through
   `openstacksdk` (the Python equivalent of `openstack endpoint list -f json`).
   It uses the core `openstack endpoint list -f json` command only as a
   fallback if the SDK cannot read the catalog. It then resolves each selected
   endpoint to an installed `openstacksdk` service proxy or legacy Python
   client and enumerates its project-scoped resources. It exits nonzero if an
   endpoint has no matching list-capable client or if a client/list request
   fails, so the output is never presented as a complete inventory when it is
   partial. New services integrated into `openstacksdk` or registered as
   OpenStack client plugins are discovered without editing this script.

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
export PAGERDUTY_INTEGRATION_KEY=<pagerduty-integration-key>
export PAGERDUTY_SEVERITY_MAP='critical|warning'
make alertmanager-pagerduty
```

Notes:

- `PAGERDUTY_INTEGRATION_KEY` enables PagerDuty notifications.
- `PAGERDUTY_SEVERITY_MAP` is used for Alertmanager route matching.

---

## Make target inventory

Below is a complete catalog of Make targets in this repo.

### Setup / infrastructure targets

- `prepare-ansible` — links `/etc/ansible` to selected in-repo inventory/config.
- `harden` — runs the host hardening playbook, idempotently installing dependencies and enabling the `security_*` systemd services.
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
- `openstack-project-resources` — lists all resources visible to `PROJECT`,
  using the Python SDKs discovered from the endpoint catalog. Pass
  `ARGS="--format json"` for machine-readable output; missing clients and list
  failures are hard errors.
- `openstack-resources-init` — initializes OpenStack resources.
- `openstack-images-upload` — uploads the default cloud image set. When run with
  `ENV=aio`, this target uploads only the Ubuntu Noble image; the Magnum
  initialization also uploads the Fedora CoreOS 38 image required by its
  cluster template.
- `openstack-cinder-backup-test` — creates temporary volumes and backups to
  verify the RadosGW-backed Cinder full, incremental, restore, and
  create-from-backup workflows, then removes them.
- `openstack-freezer-test` — verifies the Keystone `backup` service and
  endpoint, the `freezer` CLI, and the Freezer v2 API.
- `scripts/openstack/install-freezer-agent.sh` — installs the release-pinned
  Freezer agent and scheduler into a self-managed virtual environment.
- `symlink-etc-kolla` — symlinks `workspace/etc/kolla/*` into `/etc/kolla/`.
- `openstack-octavia` — initializes Octavia resources.
- `openstack-rgw` — initializes RGW resources and configures Cinder's S3
  backup driver to use the root RadosGW endpoint at the internal VIP on port
  `6780`. It creates a dedicated non-admin RGW S3 user and stores its generated
  secret only in the ignored Kolla `globals.d` configuration.
- `openstack-magnum` — initializes Magnum resources.
- `openstack-manila` — initializes Manila resources.
- `openstack-trove` — initializes Trove resources and registers MongoDB when its
  adapted guest image is available. Run `make openstack-trove-mongodb-image` first
  to create that image; set `TROVE_MONGODB_REBUILD=1` when refreshing the existing
  image after changing the injected guest adapter. This uploads a replacement
  image and removes the previous image because Glance cannot replace active image
  data in place. Set
  `TROVE_MONGODB_VERSION` to override the default MongoDB version (`8.0`). The
  injected adapter is experimental and supports standalone
  lifecycle/configuration operations plus unauthenticated MongoDB replica-set
  clusters with two or more members. Pass `topology=sharded` in
  `--extended-properties` to create a sharded cluster with config servers, one
  shard replica set, and one or more `mongos` routers; use `num_configsvr` and
  `num_mongos` to size those roles. Sharded clusters can add complete shards or
  routers with `cluster grow`, and remove complete shards or routers with
  `cluster shrink`; config-server membership remains fixed for the life of the
  cluster. Cluster configuration changes are applied with a rolling restart
  when Trove marks them restart-required. Database/user administration,
  backups, and restore snapshots are not included, and authentication is
  disabled, so use it only in an isolated test environment.
- `openstack-trove-mongodb-test` — exercises the supported MongoDB matrix:
  standalone lifecycle and port reachability, configuration attach/detach,
  restart-required configuration changes, replica-set rolling updates and
  multi-member grow/shrink, and sharded config-server, shard, and query-router
  creation, rolling updates, scaling, reachability, router removal, complete
  shard removal, and invalid partial-removal guards. It uses `m1.small` by
  default; override the flavor with `TROVE_MONGODB_TEST_FLAVOR` when the
  deployment has different capacity. Use `TROVE_MONGODB_TEST_SCOPE=replica`
  or `sharding` for focused runs; the default scope is `all`.
- `openstack-remove-test-resources` — removes test resources.

### File-level backups with Freezer

Freezer is enabled by `scripts/kolla-ansible/configure.sh` and deployed as a
custom Kolla extension. The extension keeps the OpenStack 2025.2 release,
builds local `freezer-api` and `freezer-scheduler` images, and installs
Freezer 17.0.0. `kolla-ansible-freezer.patch` is applied automatically when
the Kolla-Ansible checkout is installed; it adds the API, scheduler,
inventory, load-balancer, logging, database, and password integration.

Agents are intentionally self-managed. On every host whose files need to be
protected, install the agent and provide a project-scoped credential:

```bash
sudo OPENSTACK_RELEASE=2025.2 \
  scripts/openstack/install-freezer-agent.sh

export OS_AUTH_URL=https://keystone.example/v3
export OS_PROJECT_ID=<project-id>
export OS_APPLICATION_CREDENTIAL_ID=<application-credential-id>
export OS_APPLICATION_CREDENTIAL_SECRET=<application-credential-secret>
export OS_ENDPOINT_TYPE=internal
export OS_BACKUP_API_VERSION=2

/opt/freezer-agent/bin/freezer-scheduler \
  --client-id "${OS_PROJECT_ID}_$(hostname -s)" \
  --interval 60 start
```

Create the application credential with only the roles required by the
deployment's Freezer policy, keep its secret on the agent host, and register
the resulting client ID in the Skyline **Storage → File Backups** page. The
page supports filesystem, MySQL, MongoDB, and SQL Server sources; Swift, S3,
SSH/SFTP, FTP, FTPS, and local targets; tar/rsync engines; scheduled jobs;
and a 30-day retention action by default. Database configuration files and
all source paths are read on the agent host.

For non-Swift targets, keep credentials on the agent host rather than in the
Freezer API. The wrapper accepts `FREEZER_ACCESS_KEY`, `FREEZER_SECRET_KEY`,
and `FREEZER_ENDPOINT` for S3; `FREEZER_SSH_HOST`, `FREEZER_SSH_USERNAME`,
`FREEZER_SSH_KEY` or `FREEZER_SSH_PASSWORD`, and `FREEZER_SSH_PORT` for SSH;
and corresponding `FREEZER_FTP_*` values for FTP/FTPS. These variables are
intended for a root-owned service environment file and are never sent to the
console or stored in a Freezer job.

The Kolla scheduler is client-scoped and advertises the deployment allowlist
(`local,swift,ssh,s3,ftp,ftps`, filesystem/database modes, and safe engines).
Self-managed agents may use the same backends when their local credentials
and policy permit them. Stored-byte quota and billing are measured by the
cloud's normal object-storage accounting; Freezer itself does not provide a
native quota meter.

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
