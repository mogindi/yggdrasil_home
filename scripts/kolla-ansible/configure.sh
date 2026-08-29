#!/bin/bash

# Based on https://docs.openstack.org/kolla-ansible/latest/user/quickstart.html

CONFIG_DIR=etc/kolla
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${OPENSTACK_NOVA_COMPUTE_MONITORS:=cpu.virt_driver}"
: "${OPENSTACK_NOVA_METRICS_WEIGHT_SETTING:=cpu.percent=-1.0}"
: "${OPENSTACK_ZUN_KATA_ENABLED:=no}"
: "${OPENSTACK_ZUN_CPU_ALLOCATION_RATIO:=8.0}"
: "${OPENSTACK_ZUN_RAM_ALLOCATION_RATIO:=1.5}"
: "${OPENSTACK_ZUN_HOST_SHARED_WITH_NOVA:=true}"
: "${OPENSTACK_FUNCTION_CLOUDKITTY_ENABLED:=no}"
: "${OPENSTACK_NETWORK_GUARD_ENABLED:=no}"

is_enabled() {
	case "${1,,}" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

# just sets `<key>: <value>` in globals.yml
function set_global_config () {
	config_key=$1
	config_value=$(echo "$2" | sed 's/\//\\\//g')  # escape any slash in value (e.g for a cidr)

	# handles when comments only in beginning of line
	if grep -q "#\? *$config_key:" $CONFIG_DIR/globals.yml; then
		sed -i "s/#\? *\($config_key:\).*/\1 $config_value/g" $CONFIG_DIR/globals.yml
	else
		sed -i "$ a $config_key: $config_value" $CONFIG_DIR/globals.yml
	fi
}

set -xe

# source venv
cd workspace
source kolla-venv/bin/activate

# generate passwords
PW_FILE=$CONFIG_DIR/passwords.yml
if [ ! -f "$PW_FILE" ]; then
	echo "$PW_FILE not found"
	exit 1
fi

kolla-genpwd -p $PW_FILE


# set global configs
set_global_config kolla_base_distro ubuntu
set_global_config kolla_install_type source

set_global_config network_interface $OPENSTACK_NETWORK_INTERFACE
set_global_config neutron_external_interface $OPENSTACK_NEUTRON_EXTERNAL_INTERFACE
set_global_config kolla_internal_vip_address $OPENSTACK_KOLLA_INTERNAL_VIP_ADDRESS

set_global_config glance_backend_ceph yes
set_global_config glance_backend_file no
set_global_config ceph_glance_keyring client.admin.keyring
set_global_config ceph_glance_user admin

set_global_config nova_backend_ceph yes
set_global_config ceph_nova_keyring client.admin.keyring
set_global_config ceph_nova_user admin

set_global_config enable_cinder yes
set_global_config cinder_backend_ceph yes
set_global_config ceph_cinder_keyring client.admin.keyring
set_global_config ceph_cinder_user admin

set_global_config enable_cinder_backup yes
set_global_config ceph_cinder_backup_keyring client.admin.keyring
set_global_config ceph_cinder_backup_user admin

set_global_config neutron_plugin_agent ovn
set_global_config neutron_ovn_dhcp_agent yes
set_global_config neutron_dns_domain "xyz.local."

# Network guard is deliberately opt-in. Kolla's packet-logging plugin is the
# low-overhead data-plane signal used by the network-guard detector. Normalize
# the deployment flag so a later reconfigure with the default value reliably
# turns packet logging back off.
network_guard_packet_logging=no
if is_enabled "$OPENSTACK_NETWORK_GUARD_ENABLED"; then
	network_guard_packet_logging=yes
fi
set_global_config enable_neutron_packet_logging "$network_guard_packet_logging"

set_global_config designate_dnssec_validation no
set_global_config designate_recursion yes
set_global_config designate_forwarders_addresses "\"$OPENSTACK_DESIGNATE_FORWARDERS_FIRST_ADDRESS; $OPENSTACK_DESIGNATE_FORWARDERS_SECOND_ADDRESS\""

set_global_config enable_aodh yes
set_global_config enable_barbican yes
set_global_config enable_ceilometer yes
set_global_config enable_central_logging yes  # takes lots of resources
set_global_config enable_cloudkitty yes  # deployment broken - something about no cloudkitty database found
set_global_config enable_designate yes
# Freezer is enabled through the release-pinned Kolla extension applied by
# scripts/kolla-ansible/install.sh. Agents remain self-managed on tenant hosts.
set_global_config enable_freezer yes
# Zaqar is provided by the local Kolla extension and uses Kolla's Valkey
# Sentinel cluster for both message and management storage.
set_global_config enable_zaqar yes
set_global_config enable_gnocchi yes
set_global_config enable_grafana yes
set_global_config enable_kuryr yes
set_global_config enable_magnum yes
set_global_config enable_manila yes
set_global_config enable_manila_backend_generic yes
#set_global_config enable_masakari yes  # hacluster must be enabled - but this is an aio
set_global_config enable_mistral yes

# Mistral creates a Keystone trust when a cron trigger is provisioned.  Its
# legacy security client is explicitly the Keystone v3 client and therefore
# needs the versioned identity URL, unlike the generic middleware settings
# emitted by Kolla.
mkdir -p "$CONFIG_DIR/config"
cat > "$CONFIG_DIR/config/mistral.conf" <<EOF
[keystone_authtoken]
www_authenticate_uri = http://$OPENSTACK_KOLLA_INTERNAL_VIP_ADDRESS:5000/v3
auth_url = http://$OPENSTACK_KOLLA_INTERNAL_VIP_ADDRESS:5000/v3
EOF

#set_global_config enable_murano yes  # broken in 2023.2 for some reason
#set_global_config enable_neutron_vpnaas yes  # broken in 2025.1
set_global_config enable_octavia yes
set_global_config enable_prometheus yes
# Keep Nova API collection on the stable microversion supported by the
# deployment, and omit Nova's obsolete security-group metric. Security groups
# are managed by Neutron ports; the Neutron security-group metric remains
# enabled by the exporter defaults.
set_global_config prometheus_openstack_exporter_compute_api_version '"2.1"'
set_global_config prometheus_openstack_exporter_cmdline_extras '"--disable-metric=nova-security-groups"'
#set_global_config enable_redis yes
set_global_config enable_valkey yes
set_global_config enable_sahara yes
if [[ "${OPENSTACK_RELEASE:-}" == '2025.2' ]]; then
	set_global_config enable_senlin yes
else
	set_global_config enable_senlin no
fi
set_global_config enable_skyline yes
set_global_config enable_solum yes
set_global_config enable_tacker yes
set_global_config enable_trove yes
set_global_config enable_venus yes
#set_global_config enable_vitrage yes
set_global_config enable_watcher yes
set_global_config enable_zun "$OPENSTACK_ZUN_KATA_ENABLED"

if is_enabled "$OPENSTACK_ZUN_KATA_ENABLED"; then
	set_global_config docker_custom_config '{ "live-restore": true, "ip-forward-no-drop": true, "runtimes": { "kata": { "runtimeType": "io.containerd.kata.v2" } } }'
	set_global_config zun_compute_extra_volumes '["/etc/zun/docker-pki:/etc/zun/docker-pki:ro"]'
	set_global_config zun_wsproxy_extra_volumes '["/etc/zun/docker-pki:/etc/zun/docker-pki:ro"]'
else
	set_global_config docker_custom_config '{ "live-restore": true, "ip-forward-no-drop": true }'
	set_global_config zun_compute_extra_volumes '[]'
	set_global_config zun_wsproxy_extra_volumes '[]'
fi

set_global_config octavia_provider_drivers '"amphora:Amphora provider, ovn:OVN provider"'
set_global_config octavia_amp_network_cidr $OPENSTACK_AMPHORA_SUBNET_CIDR

set_global_config cloudkitty_storage_backend opensearch

set_global_config enable_ceph_rgw yes
set_global_config ceph_rgw_hosts "$OPENSTACK_CEPH_RGW_HOSTS"
set_global_config ceph_rgw_swift_account_in_url yes  # used to namespace per project with "AUTH_%(project_id)s"
set_global_config ceph_rgw_swift_compatibility no  # this is used to add "/swift/" in url, to distinguish from s3

set_global_config nova_console novnc

set_global_config openstack_service_workers "$OPENSTACK_WORKER_COUNT"
set_global_config heat_api_workers 4
set_global_config openstack_service_rpc_workers "$OPENSTACK_WORKER_COUNT"

set_global_config disable_firewall no

for service in glance nova cinder/cinder-volume cinder/cinder-backup; do
	mkdir -p etc/kolla/config/$service/
	cp /etc/ceph/ceph.client.admin.keyring etc/kolla/config/$service/
	cat /etc/ceph/ceph.conf | sed 's/^\t//g' > etc/kolla/config/$service/ceph.conf
done

# magnum
cat > etc/kolla/config/magnum.conf <<EOF
[drivers]
disabled_drivers = k8s_cluster_api_ubuntu,k8s_cluster_api_ubuntu_focal,k8s_cluster_api_debian,k8s_cluster_api_flatcar,k8s_cluster_api_rockylinux
EOF
mkdir -p etc/kolla/config/magnum/
cat > etc/kolla/config/magnum/magnum-conductor.conf <<EOF
[trust]
cluster_user_trust = True
EOF

# trove
cat > etc/kolla/config/trove.conf <<EOF
[DEFAULT]
max_accepted_volume_size = 100

[mongodb]
# Use the injected MongoDB topology controller.  Replica-set clusters remain
# the default; extended property topology=sharded enables config servers,
# shard replica sets, and mongos routers.
api_strategy = trove_yggdrasil_mongodb.api.MongoDbAPIStrategy
taskmanager_strategy = trove_yggdrasil_mongodb.taskmanager.MongoDbTaskManagerStrategy
guestagent_strategy = trove_yggdrasil_mongodb.guestagent.MongoDbGuestAgentStrategy
cluster_support = true
cluster_secure = false

#[oslo_messaging_rabbit]
#rabbit_quorum_queue = false
#amqp_durable_queues = true
#rabbit_ha_queues = true

EOF
mkdir -p etc/kolla/config/trove/
cat > etc/kolla/config/trove/trove-taskmanager.conf <<EOF
[DEFAULT]
nova_keypair = testkey
# Inject the guest-agent configuration through Nova's config drive.  This is
# reliable for clustered guests even when the Neutron metadata service is
# temporarily unavailable during a parallel Nova boot.
use_nova_server_config_drive = true

#[oslo_messaging_rabbit]
#rabbit_quorum_queue = true
#rabbit_ha_queues = false
EOF
cat > etc/kolla/config/trove-guestagent.conf <<EOF
[postgresql]
database_service_uid = 1001
database_service_gid = 1001

[mysql]
database_service_uid = 1001
database_service_gid = 1001

[mariadb]
database_service_uid = 1001
database_service_gid = 1001
EOF

# designate
mkdir -p etc/kolla/config/designate/
cat > etc/kolla/config/designate/named.conf <<EOF
#jinja2: trim_blocks: False
include "/etc/rndc.key";
options {
        listen-on port {{ designate_bind_port }} { {{ 'api' | kolla_address }}; };
        {% if api_interface != dns_interface %}
        listen-on port {{ designate_bind_port }} { {{ 'dns' | kolla_address }}; };
        {% endif %}
        directory       "/var/lib/named";
        allow-new-zones yes;
        dnssec-validation {{ designate_dnssec_validation }};
        auth-nxdomain no;
        request-ixfr no;
        recursion {{ designate_recursion }};
        allow-query-cache { any; };

        {% if designate_forwarders_addresses %}
        forwarders { {{ designate_forwarders_addresses }}; };
        {% endif %}
        minimal-responses yes;
        allow-notify { {% for host in groups['designate-worker'] %}{{ 'api' | kolla_address(host) }};{% endfor %} };
};

controls {
        inet {{ 'api' | kolla_address }} port {{ designate_rndc_port }} allow { {% for host in groups['designate-worker'] %}{{ 'api' | kolla_address(host) }}; {% endfor %} } keys { "rndc-key"; };
};
EOF

# neutron
config_dir=etc/kolla/config/neutron
mkdir -p $config_dir
cat > $config_dir/dhcp_agent.ini <<EOF
[DEFAULT]
#dnsmasq_dns_servers = {{ 'api' | kolla_address }}
dnsmasq_local_resolv = True
EOF
# TODO: NOT SURE OF THESE VALUES
cat >  etc/kolla/config/neutron.conf <<EOF
[DEFAULT]
global_physnet_mtu = $OPENSTACK_PHYSNET_MTU
dns_servers = 1.1.1.1,8.8.8.8
EOF
cat > $config_dir/ml2_conf.ini <<EOF
path_mtu = $OPENSTACK_PATH_MTU
EOF


# glance
config_dir=etc/kolla/config/glance
mkdir -p $config_dir
cat > $config_dir/glance-api.conf <<EOF
[DEFAULT]
show_image_direct_url = true
EOF

# nova
cat >  etc/kolla/config/nova.conf <<EOF
[DEFAULT]
cpu_allocation_ratio = 16.0
ram_allocation_ratio = 5.0
EOF
config_dir=etc/kolla/config/nova
mkdir -p $config_dir
cat > $config_dir/nova-compute.conf <<EOF
[DEFAULT]
compute_monitors = $OPENSTACK_NOVA_COMPUTE_MONITORS

[glance]
enable_rbd_download = true
rbd_user = {{ ceph_glance_user }}
rbd_pool = {{ ceph_glance_pool_name }}
rbd_ceph_conf = /etc/ceph/ceph.conf
rbd_connect_timeout = 5

[libvirt]
cpu_mode = host-passthrough
EOF
cat > $config_dir/nova-scheduler.conf <<EOF
[metrics]
weight_setting = $OPENSTACK_NOVA_METRICS_WEIGHT_SETTING
EOF

# Keep project-scoped reader users from mutating Nova keypairs. Kolla copies
# this policy file into the Nova API and metadata service containers.
NOVA_POLICY_SOURCE="${OPENSTACK_NOVA_POLICY_FILE:-$SCRIPT_DIR/policies/nova/policy.yaml}"
cp "$NOVA_POLICY_SOURCE" "$config_dir/policy.yaml"

# Keep project-scoped reader users from creating Cinder volumes. OpenStack
# policies are enforced per service, so Cinder needs its own override.
config_dir=etc/kolla/config/cinder
mkdir -p "$config_dir"
CINDER_POLICY_SOURCE="${OPENSTACK_CINDER_POLICY_FILE:-$SCRIPT_DIR/policies/cinder/policy.yaml}"
cp "$CINDER_POLICY_SOURCE" "$config_dir/policy.yaml"

# skyline
mkdir -p etc/kolla/config/skyline/
cat >  etc/kolla/config/skyline/skyline.yaml <<EOF
openstack:
  service_mapping:
    alarming: aodh
    backup: freezer
    messaging: zaqar
    metric: gnocchi
    volumev3: cinder
EOF

# manila
cat > etc/kolla/config/manila.conf <<EOF
[DEFAULT]
delete_share_server_with_last_share = false
EOF

# zun
if is_enabled "$OPENSTACK_ZUN_KATA_ENABLED"; then
	mkdir -p etc/kolla/config/zun
	cat > etc/kolla/config/zun.conf <<'EOF'
[DEFAULT]
container_runtime = kata

[docker]
docker_remote_api_version = 1.44
api_url = https://{{ api_interface_address | put_address_in_context('url') }}:2376
docker_remote_api_url = https://{{ api_interface_address | put_address_in_context('url') }}:2376
docker_remote_api_host = {{ api_interface_address }}
docker_remote_api_port = 2376
api_insecure = false
ca_file = /etc/zun/docker-pki/ca.pem
cert_file = /etc/zun/docker-pki/cert.pem
key_file = /etc/zun/docker-pki/key.pem
EOF
	cat > etc/kolla/config/zun/policy.yaml <<'EOF'
"container:create:runtime": "!"
EOF
else
	rm -f etc/kolla/config/zun/policy.yaml
	cat > etc/kolla/config/zun.conf <<EOF
[docker]
docker_remote_api_version = 1.44
EOF
fi

cat >> etc/kolla/config/zun.conf <<EOF

[compute]
host_shared_with_nova = $OPENSTACK_ZUN_HOST_SHARED_WITH_NOVA
EOF

# A shared Nova/Zun provider has one Placement inventory.  Let Nova/Placement
# own its allocation ratios instead of having Zun overwrite them periodically.
if ! is_enabled "$OPENSTACK_ZUN_HOST_SHARED_WITH_NOVA"; then
	cat >> etc/kolla/config/zun.conf <<EOF
cpu_allocation_ratio = $OPENSTACK_ZUN_CPU_ALLOCATION_RATIO
ram_allocation_ratio = $OPENSTACK_ZUN_RAM_ALLOCATION_RATIO
EOF
fi

# Keep function collection and CloudKitty publication disabled unless the
# single function rating switch is enabled.
mkdir -p etc/kolla/config/cloudkitty
METRICS_SOURCE="${CLOUDKITTY_METRICS_FILE:-$SCRIPT_DIR/cloudkitty-metrics.yml}"
if [[ ! -f "$METRICS_SOURCE" ]]; then
	echo "CloudKitty metrics definition not found: $METRICS_SOURCE" >&2
	exit 1
fi
cp "$METRICS_SOURCE" etc/kolla/config/cloudkitty/metrics.yml
if is_enabled "$OPENSTACK_FUNCTION_CLOUDKITTY_ENABLED"; then
	cat >> etc/kolla/config/cloudkitty/metrics.yml <<'EOF'

  function.cpu.utilization:
    unit: vCPU
    groupby:
      - id
      - project_id
    metadata:
      - function_name
      - runtime
    extra_args:
      aggregation_method: mean
      resource_type: yggdrasil_function
      force_granularity: 600

  function.memory.usage:
    unit: GiB
    groupby:
      - id
      - project_id
    metadata:
      - function_name
      - runtime
    extra_args:
      aggregation_method: mean
      resource_type: yggdrasil_function
      force_granularity: 600
EOF
fi
