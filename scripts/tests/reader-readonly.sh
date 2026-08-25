#!/usr/bin/env bash

# Verify that a project-scoped Keystone reader cannot create resources.
#
# The script deliberately uses an admin credential only for test setup and
# cleanup. All create attempts are made with a newly-created user whose only
# project role is "reader".

set -u
set -o pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
OPENSTACK_WORKSPACE_DIR=${OPENSTACK_WORKSPACE_DIR:-$REPO_ROOT/workspace}
OPENSTACK_CONFIG_DIR=${OPENSTACK_CONFIG_DIR:-$OPENSTACK_WORKSPACE_DIR/etc/kolla}
OPENSTACK_ADMIN_RC=${OPENSTACK_ADMIN_RC:-$OPENSTACK_CONFIG_DIR/admin-openrc.sh}
OPENSTACK_BIN=${OPENSTACK_BIN:-$OPENSTACK_WORKSPACE_DIR/kolla-venv/bin/openstack}
OPENSTACK_COMMAND_TIMEOUT=${OPENSTACK_COMMAND_TIMEOUT:-120}

if [[ ! -x "$OPENSTACK_BIN" ]]; then
  OPENSTACK_BIN=$(command -v openstack 2>/dev/null || true)
fi

if [[ -z "$OPENSTACK_BIN" || ! -x "$OPENSTACK_BIN" ]]; then
  echo "ERROR: openstack CLI was not found" >&2
  exit 1
fi

if [[ ! -r "$OPENSTACK_ADMIN_RC" ]]; then
  echo "ERROR: admin openrc was not found: $OPENSTACK_ADMIN_RC" >&2
  exit 1
fi

RESULTS=()
UNEXPECTED_KINDS=()
UNEXPECTED_IDS=()
UNEXPECTED_COUNT=0
ERROR_COUNT=0
DENIED_COUNT=0
SKIPPED_COUNT=0

TEST_PROJECT_NAME="reader-ro-test-$(date -u +%Y%m%d%H%M%S)-$$"
TEST_USER_NAME="$TEST_PROJECT_NAME-user"
TEST_GROUP_NAME="$TEST_PROJECT_NAME-group"
TEST_ROLE_NAME="$TEST_PROJECT_NAME-role"
TEST_PROJECT_ID=""
TEST_USER_ID=""

FIXTURE_NETWORK_ID=""
FIXTURE_SUBNET_ID=""
FIXTURE_VOLUME_ID=""
FIXTURE_CONTAINER=""
FIXTURE_ZONE=""
FIXTURE_ZONE_ID=""
FIXTURE_RECORDSET_NAME=""
FIXTURE_IMAGE_ID=""
FIXTURE_FLAVOR_ID=""
EXTERNAL_NETWORK_ID=""
DEFAULT_SECURITY_GROUP_ID=""
SHARE_TYPE=""
DATASTORE_NAME=""
DATASTORE_VERSION=""

TEMP_FILES=()

run_os() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$OPENSTACK_COMMAND_TIMEOUT" "$OPENSTACK_BIN" "$@"
  else
    "$OPENSTACK_BIN" "$@"
  fi
}

delete_os() {
  run_os "$@" >/dev/null 2>&1 || true
}

wait_for_volume_absent() {
  local volume_id=$1
  local _
  for _ in $(seq 1 30); do
    if ! run_os volume show "$volume_id" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
}

service_available() {
  grep -Fxq -- "$1" <<<"$CATALOG_TYPES"
}

any_service_available() {
  local service
  for service in "$@"; do
    if service_available "$service"; then
      return 0
    fi
  done
  return 1
}

record_result() {
  RESULTS+=("$1|$2")
}

cleanup_registered_resources() {
  local i kind resource_id zone_name recordset_name

  for ((i=${#UNEXPECTED_IDS[@]} - 1; i>=0; i--)); do
    kind=${UNEXPECTED_KINDS[$i]}
    resource_id=${UNEXPECTED_IDS[$i]}
    case "$kind" in
      project) delete_os project delete "$resource_id" ;;
      user) delete_os user delete "$resource_id" ;;
      group) delete_os group delete "$resource_id" ;;
      role) delete_os role delete "$resource_id" ;;
      network) delete_os network delete "$resource_id" ;;
      subnet) delete_os subnet delete "$resource_id" ;;
      port) delete_os port delete "$resource_id" ;;
      router) delete_os router delete "$resource_id" ;;
      security_group) delete_os security group delete "$resource_id" ;;
      security_group_rule) delete_os security group rule delete "$resource_id" ;;
      address_scope) delete_os address scope delete "$resource_id" ;;
      subnet_pool) delete_os subnet pool delete "$resource_id" ;;
      floating_ip) delete_os floating ip delete "$resource_id" ;;
      keypair) delete_os keypair delete "$resource_id" ;;
      server) delete_os server delete "$resource_id" ;;
      server_group) delete_os server group delete "$resource_id" ;;
      flavor) delete_os flavor delete "$resource_id" ;;
      volume)
        delete_os volume delete "$resource_id"
        wait_for_volume_absent "$resource_id"
        ;;
      volume_type) delete_os volume type delete "$resource_id" ;;
      volume_snapshot) delete_os volume snapshot delete "$resource_id" ;;
      image) delete_os image delete "$resource_id" ;;
      container) delete_os container delete --recursive "$resource_id" ;;
      object) delete_os object delete "$FIXTURE_CONTAINER" "$resource_id" ;;
      stack) delete_os stack delete --yes "$resource_id" ;;
      zone) delete_os zone delete "$resource_id" ;;
      recordset)
        if [[ "$resource_id" == *'|'* ]]; then
          zone_name=${resource_id%%|*}
          recordset_name=${resource_id#*|}
          delete_os recordset delete "$zone_name" "$recordset_name"
        else
          delete_os recordset delete "$resource_id"
        fi
        ;;
      loadbalancer) delete_os loadbalancer delete --cascade "$resource_id" ;;
      share) delete_os share delete "$resource_id" ;;
      database_instance) delete_os database instance delete "$resource_id" ;;
    esac
  done
}

cleanup() {
  local exit_status=$?
  trap - EXIT

  # Restore admin credentials before attempting cleanup. This is intentionally
  # best-effort so a failed API call cannot prevent later cleanup steps.
  if [[ -r "$OPENSTACK_ADMIN_RC" ]]; then
    # shellcheck disable=SC1090
    source "$OPENSTACK_ADMIN_RC" >/dev/null 2>&1 || true
    cleanup_registered_resources

    if [[ -n "$FIXTURE_CONTAINER" ]]; then
      delete_os container delete --recursive "$FIXTURE_CONTAINER"
    fi
    if [[ -n "$FIXTURE_ZONE_ID" ]]; then
      delete_os zone delete "$FIXTURE_ZONE_ID"
    fi
    if [[ -n "$FIXTURE_VOLUME_ID" ]]; then
      delete_os volume delete "$FIXTURE_VOLUME_ID"
      wait_for_volume_absent "$FIXTURE_VOLUME_ID"
    fi
    if [[ -n "$FIXTURE_SUBNET_ID" ]]; then
      delete_os subnet delete "$FIXTURE_SUBNET_ID"
    fi
    if [[ -n "$FIXTURE_NETWORK_ID" ]]; then
      delete_os network delete "$FIXTURE_NETWORK_ID"
    fi

    if [[ -n "$TEST_USER_ID" ]]; then
      delete_os user delete "$TEST_USER_ID"
    fi
    if [[ -n "$TEST_PROJECT_ID" ]]; then
      delete_os project delete "$TEST_PROJECT_ID"
    fi
  fi

  local temp_file
  for temp_file in "${TEMP_FILES[@]}"; do
    rm -f -- "$temp_file"
  done

  exit "$exit_status"
}

trap cleanup EXIT

umask 077

if command -v openssl >/dev/null 2>&1; then
  TEST_PASSWORD=$(openssl rand -hex 24)
else
  TEST_PASSWORD=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
fi

# shellcheck disable=SC1090
source "$OPENSTACK_ADMIN_RC"

echo "Creating isolated project and reader user: $TEST_PROJECT_NAME"
if ! TEST_PROJECT_ID=$(run_os project create "$TEST_PROJECT_NAME" \
    --domain Default -f value -c id 2>/dev/null); then
  echo "ERROR: unable to create test project" >&2
  exit 1
fi

if ! TEST_USER_ID=$(run_os user create "$TEST_USER_NAME" \
    --domain Default --password "$TEST_PASSWORD" -f value -c id 2>/dev/null); then
  echo "ERROR: unable to create test user" >&2
  exit 1
fi

if ! run_os role add reader --project "$TEST_PROJECT_ID" --user "$TEST_USER_ID" \
    >/dev/null 2>&1; then
  echo "ERROR: unable to assign reader role" >&2
  exit 1
fi

ASSIGNED_ROLES=$(run_os role assignment list --user "$TEST_USER_ID" \
  --project "$TEST_PROJECT_ID" --names -f value -c Role 2>/dev/null || true)
if ! grep -Fxq reader <<<"$ASSIGNED_ROLES" || \
    grep -v -Fx reader <<<"$ASSIGNED_ROLES" | grep -q '[^[:space:]]'; then
  echo "ERROR: reader role assignment could not be verified" >&2
  exit 1
fi

CATALOG_TYPES=$(run_os catalog list -f value -c Type 2>/dev/null | sort -u)

# Create a project-owned network fixture so subnet, port, server, and
# load-balancer create attempts do not depend on pre-existing tenant data.
if service_available network; then
  FIXTURE_NETWORK_NAME="$TEST_PROJECT_NAME-network"
  FIXTURE_SUBNET_NAME="$TEST_PROJECT_NAME-subnet"
  FIXTURE_SUBNET_CIDR=${READER_TEST_SUBNET_CIDR:-198.18.250.0/28}

  FIXTURE_NETWORK_ID=$(run_os network create "$FIXTURE_NETWORK_NAME" \
    --project "$TEST_PROJECT_ID" -f value -c id 2>/dev/null || true)
  if [[ -n "$FIXTURE_NETWORK_ID" ]]; then
    FIXTURE_SUBNET_ID=$(run_os subnet create "$FIXTURE_SUBNET_NAME" \
      --network "$FIXTURE_NETWORK_ID" --subnet-range "$FIXTURE_SUBNET_CIDR" \
      --project "$TEST_PROJECT_ID" -f value -c id 2>/dev/null || true)
  fi
fi

if service_available object-store; then
  FIXTURE_CONTAINER="$TEST_PROJECT_NAME-container"
  if ! run_os container create "$FIXTURE_CONTAINER" >/dev/null 2>&1; then
    FIXTURE_CONTAINER=""
  fi
fi

if service_available dns; then
  FIXTURE_ZONE="$TEST_PROJECT_NAME.example."
  FIXTURE_RECORDSET_NAME="reader-recordset.$TEST_PROJECT_NAME.example."
  FIXTURE_ZONE_ID=$(run_os zone create "$FIXTURE_ZONE" \
    --email "reader-test@example.invalid" -f value -c id 2>/dev/null || true)
fi

if any_service_available block-storage volumev3; then
  FIXTURE_VOLUME_NAME="$TEST_PROJECT_NAME-volume"
  FIXTURE_VOLUME_ID=$(run_os volume create --size 1 --project "$TEST_PROJECT_ID" \
    "$FIXTURE_VOLUME_NAME" -f value -c id 2>/dev/null || true)
  if [[ -n "$FIXTURE_VOLUME_ID" ]]; then
    volume_ready=false
    for _ in $(seq 1 30); do
      volume_status=$(run_os volume show "$FIXTURE_VOLUME_ID" -f value -c status 2>/dev/null || true)
      if [[ "$volume_status" == available ]]; then
        volume_ready=true
        break
      fi
      if [[ "$volume_status" == error* || "$volume_status" == error ]]; then
        break
      fi
      sleep 2
    done
    if [[ "$volume_ready" != true ]]; then
      delete_os volume delete "$FIXTURE_VOLUME_ID"
      FIXTURE_VOLUME_ID=""
    fi
  fi
fi

if service_available compute; then
  FIXTURE_IMAGE_ID=$(run_os image list --status active -f value -c ID 2>/dev/null \
    | awk 'NF {print $1; exit}')
  FIXTURE_FLAVOR_ID=$(run_os flavor list -f value -c ID 2>/dev/null \
    | awk 'NF {print $1; exit}')
fi

if service_available network; then
  EXTERNAL_NETWORK_ID=$(run_os network list --external -f value -c ID 2>/dev/null \
    | awk 'NF {print $1; exit}')
  DEFAULT_SECURITY_GROUP_ID=$(run_os security group show default \
    --project "$TEST_PROJECT_ID" -f value -c id 2>/dev/null || true)
fi

if any_service_available share sharev2; then
  SHARE_TYPE=$(run_os share type list -f value -c Name 2>/dev/null \
    | awk 'NF {print $1; exit}')
fi

if service_available database; then
  DATASTORE_NAME=$(run_os datastore list -f value -c Name 2>/dev/null \
    | awk 'NF {print $1; exit}')
  if [[ -n "$DATASTORE_NAME" ]]; then
    DATASTORE_VERSION=$(run_os datastore version list --datastore "$DATASTORE_NAME" \
      -f value -c Name 2>/dev/null | awk 'NF {print $1; exit}')
  fi
fi

# Switch from the admin token to a project-scoped token carrying only reader.
# Keep this in a mode-600 temporary file because it contains the test password.
READER_RC=$(mktemp)
TEMP_FILES+=("$READER_RC")
{
  printf '%s\n' 'for key in $(set | awk '\''{FS="="} /^OS_/ {print $1}'\''); do unset "$key"; done'
  printf 'export OS_PROJECT_DOMAIN_NAME=%q\n' "${OS_PROJECT_DOMAIN_NAME:-Default}"
  printf 'export OS_USER_DOMAIN_NAME=%q\n' "${OS_USER_DOMAIN_NAME:-Default}"
  printf 'export OS_PROJECT_NAME=%q\n' "$TEST_PROJECT_NAME"
  printf 'export OS_TENANT_NAME=%q\n' "$TEST_PROJECT_NAME"
  printf 'export OS_PROJECT_ID=%q\n' "$TEST_PROJECT_ID"
  printf 'export OS_USERNAME=%q\n' "$TEST_USER_NAME"
  printf 'export OS_PASSWORD=%q\n' "$TEST_PASSWORD"
  printf 'export OS_AUTH_URL=%q\n' "${OS_AUTH_URL:-}"
  printf 'export OS_INTERFACE=%q\n' "${OS_INTERFACE:-internal}"
  printf 'export OS_ENDPOINT_TYPE=%q\n' "${OS_ENDPOINT_TYPE:-internalURL}"
  printf 'export OS_REGION_NAME=%q\n' "${OS_REGION_NAME:-RegionOne}"
  printf 'export OS_IDENTITY_API_VERSION=%q\n' "${OS_IDENTITY_API_VERSION:-3}"
  printf 'export OS_AUTH_PLUGIN=%q\n' "${OS_AUTH_PLUGIN:-password}"
  if [[ -n "${OS_CACERT:-}" ]]; then
    printf 'export OS_CACERT=%q\n' "$OS_CACERT"
  fi
} >"$READER_RC"

# shellcheck disable=SC1090
source "$READER_RC"

echo "Testing project-scoped reader create permissions"

# Keystone resources: these are deliberately attempted even though the token
# is project-scoped. A reader must not gain identity-management privileges.
attempt_create() {
  local label=$1
  local kind=$2
  local cleanup_id=$3
  local output rc actual_id
  shift 3

  output=$(mktemp)
  TEMP_FILES+=("$output")
  run_os "$@" >"$output" 2>&1
  rc=$?

  if [[ $rc -eq 0 ]]; then
    if [[ "$cleanup_id" == auto ]]; then
      actual_id=$(awk 'NF {print $1; exit}' "$output")
      cleanup_id=${actual_id:-$label}
    fi
    UNEXPECTED_KINDS+=("$kind")
    UNEXPECTED_IDS+=("$cleanup_id")
    UNEXPECTED_COUNT=$((UNEXPECTED_COUNT + 1))
    record_result "$label" UNEXPECTED_SUCCESS
    echo "FAIL  $label: reader created a resource"
  elif grep -Eiq '(forbidden|403|unauthorized|not authorized|policy.*allow|access denied|operation not permitted)' "$output"; then
    DENIED_COUNT=$((DENIED_COUNT + 1))
    record_result "$label" EXPECTED_DENIED
    echo "PASS  $label: denied"
  elif grep -Eiq '(no such command|unknown command|command .* not found|service .* not available|no endpoint|endpoint.*not found|failed to contact the endpoint|fallback to using that endpoint|^Unknown$)' "$output"; then
    SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
    record_result "$label" NOT_APPLICABLE
    echo "SKIP  $label: command/service unavailable"
  else
    local error_detail
    error_detail=$(tail -n 1 "$output" | tr -s ' ')
    ERROR_COUNT=$((ERROR_COUNT + 1))
    record_result "$label" INCONCLUSIVE
    echo "ERROR $label: inconclusive response: $error_detail"
    rm -f -- "$output"
    return
  fi

  rm -f -- "$output"
}

attempt_create "identity project" project "$TEST_PROJECT_NAME-project" \
  project create "$TEST_PROJECT_NAME-project" --domain Default -f value -c id
attempt_create "identity user" user "$TEST_USER_NAME-user" \
  user create "$TEST_USER_NAME-user" --domain Default --password "$TEST_PASSWORD" -f value -c id
attempt_create "identity group" group "$TEST_GROUP_NAME" \
  group create "$TEST_GROUP_NAME" --domain Default -f value -c id
attempt_create "identity role" role "$TEST_ROLE_NAME" \
  role create "$TEST_ROLE_NAME" -f value -c id

if service_available network; then
  attempt_create "neutron network" network "$TEST_PROJECT_NAME-created-network" \
    network create "$TEST_PROJECT_NAME-created-network" -f value -c id

  if [[ -n "$FIXTURE_NETWORK_ID" ]]; then
    attempt_create "neutron subnet" subnet "$TEST_PROJECT_NAME-created-subnet" \
      subnet create "$TEST_PROJECT_NAME-created-subnet" --network "$FIXTURE_NETWORK_ID" \
      --subnet-range 198.18.240.0/28 -f value -c id
    attempt_create "neutron port" port "$TEST_PROJECT_NAME-created-port" \
      port create "$TEST_PROJECT_NAME-created-port" --network "$FIXTURE_NETWORK_ID" \
      -f value -c id
  fi

  attempt_create "neutron router" router "$TEST_PROJECT_NAME-router" \
    router create "$TEST_PROJECT_NAME-router" -f value -c id
  attempt_create "neutron security group" security_group "$TEST_PROJECT_NAME-security-group" \
    security group create "$TEST_PROJECT_NAME-security-group" -f value -c id

  if [[ -n "$DEFAULT_SECURITY_GROUP_ID" ]]; then
    attempt_create "neutron security-group rule" security_group_rule auto \
      security group rule create "$DEFAULT_SECURITY_GROUP_ID" --ingress \
      --ethertype IPv4 --protocol tcp --dst-port 65534 \
      --remote-ip 198.51.100.1/32 -f value -c id
  fi

  attempt_create "neutron address scope" address_scope "$TEST_PROJECT_NAME-address-scope" \
    address scope create "$TEST_PROJECT_NAME-address-scope" --ip-version 4 -f value -c id
  attempt_create "neutron subnet pool" subnet_pool "$TEST_PROJECT_NAME-subnet-pool" \
    subnet pool create "$TEST_PROJECT_NAME-subnet-pool" \
    --pool-prefix 198.18.224.0/20 --default-prefix-length 28 -f value -c id

  if [[ -n "$EXTERNAL_NETWORK_ID" ]]; then
    attempt_create "neutron floating IP" floating_ip "$TEST_PROJECT_NAME-floating-ip" \
      floating ip create "$EXTERNAL_NETWORK_ID" -f value -c id
  fi
fi

if service_available compute; then
  PUBLIC_KEY_FILE=$(mktemp)
  TEMP_FILES+=("$PUBLIC_KEY_FILE")
  if command -v ssh-keygen >/dev/null 2>&1; then
    rm -f -- "$PUBLIC_KEY_FILE"
    ssh-keygen -q -t rsa -b 2048 -N '' -f "$PUBLIC_KEY_FILE" >/dev/null 2>&1
    TEMP_FILES+=("$PUBLIC_KEY_FILE.pub")
    PUBLIC_KEY_FILE="$PUBLIC_KEY_FILE.pub"
  else
    printf '%s\n' 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7readerreadonlytest reader-test' \
      >"$PUBLIC_KEY_FILE"
  fi

  attempt_create "nova keypair" keypair "$TEST_PROJECT_NAME-keypair" \
    keypair create --public-key "$PUBLIC_KEY_FILE" "$TEST_PROJECT_NAME-keypair" \
    -f value -c name
  attempt_create "nova server group" server_group "$TEST_PROJECT_NAME-server-group" \
    server group create "$TEST_PROJECT_NAME-server-group" --policy affinity -f value -c id

  if [[ -n "$FIXTURE_NETWORK_ID" && -n "$FIXTURE_IMAGE_ID" && -n "$FIXTURE_FLAVOR_ID" ]]; then
    attempt_create "nova server" server "$TEST_PROJECT_NAME-server" \
      server create "$TEST_PROJECT_NAME-server" --image "$FIXTURE_IMAGE_ID" \
      --flavor "$FIXTURE_FLAVOR_ID" --network "$FIXTURE_NETWORK_ID" -f value -c id
  fi

  attempt_create "nova flavor" flavor "$TEST_PROJECT_NAME-flavor" \
    flavor create "$TEST_PROJECT_NAME-flavor" --ram 64 --disk 1 --vcpus 1 -f value -c id
fi

if any_service_available block-storage volumev3; then
  attempt_create "cinder volume" volume auto \
    volume create --size 1 "$TEST_PROJECT_NAME-created-volume" -f value -c id
  attempt_create "cinder volume type" volume_type "$TEST_PROJECT_NAME-volume-type" \
    volume type create "$TEST_PROJECT_NAME-volume-type" -f value -c id

  if [[ -n "$FIXTURE_VOLUME_ID" ]]; then
    attempt_create "cinder volume snapshot" volume_snapshot "$TEST_PROJECT_NAME-snapshot" \
      volume snapshot create --force --volume "$FIXTURE_VOLUME_ID" \
      "$TEST_PROJECT_NAME-snapshot" -f value -c id
  fi
fi

if service_available image; then
  attempt_create "glance image" image "$TEST_PROJECT_NAME-image" \
    image create "$TEST_PROJECT_NAME-image" --disk-format raw --container-format bare \
    -f value -c id
fi

if service_available object-store; then
  attempt_create "swift container" container "$TEST_PROJECT_NAME-created-container" \
    container create "$TEST_PROJECT_NAME-created-container"
  if [[ -n "$FIXTURE_CONTAINER" ]]; then
    OBJECT_FILE=$(mktemp)
    TEMP_FILES+=("$OBJECT_FILE")
    : >"$OBJECT_FILE"
    attempt_create "swift object" object "$TEST_PROJECT_NAME-object" \
      object create "$FIXTURE_CONTAINER" "$OBJECT_FILE" -f value -c name
  fi
fi

if service_available orchestration; then
  TEMPLATE_FILE=$(mktemp)
  TEMP_FILES+=("$TEMPLATE_FILE")
  cat >"$TEMPLATE_FILE" <<'EOF'
heat_template_version: 2018-08-31
resources: {}
EOF
  attempt_create "heat stack" stack "$TEST_PROJECT_NAME-stack" \
    stack create -t "$TEMPLATE_FILE" "$TEST_PROJECT_NAME-stack" -f value -c id
fi

if service_available dns; then
  attempt_create "designate zone" zone "$TEST_PROJECT_NAME-created.example." \
    zone create "$TEST_PROJECT_NAME-created.example." \
    --email "reader-test@example.invalid" -f value -c id
  if [[ -n "$FIXTURE_ZONE_ID" ]]; then
    attempt_create "designate recordset" recordset "$FIXTURE_ZONE|$FIXTURE_RECORDSET_NAME" \
      recordset create "$FIXTURE_ZONE" "$FIXTURE_RECORDSET_NAME" --type A \
      --record 198.51.100.10 -f value -c id
  fi
fi

if service_available load-balancer && [[ -n "$FIXTURE_SUBNET_ID" ]]; then
  attempt_create "octavia load balancer" loadbalancer "$TEST_PROJECT_NAME-loadbalancer" \
    loadbalancer create --name "$TEST_PROJECT_NAME-loadbalancer" \
    --vip-subnet-id "$FIXTURE_SUBNET_ID" -f value -c id
fi

if any_service_available share sharev2 && [[ -n "$SHARE_TYPE" ]]; then
  attempt_create "manila share" share "$TEST_PROJECT_NAME-share" \
    share create NFS 1 --name "$TEST_PROJECT_NAME-share" \
    --share-type "$SHARE_TYPE" -f value -c id
fi

if service_available database && [[ -n "$FIXTURE_NETWORK_ID" && -n "$FIXTURE_FLAVOR_ID" \
    && -n "$DATASTORE_NAME" && -n "$DATASTORE_VERSION" ]]; then
  attempt_create "trove database instance" database_instance "$TEST_PROJECT_NAME-database" \
    database instance create "$TEST_PROJECT_NAME-database" \
    --flavor "$FIXTURE_FLAVOR_ID" --size 1 --nic net-id="$FIXTURE_NETWORK_ID" \
    --datastore "$DATASTORE_NAME" --datastore-version "$DATASTORE_VERSION" \
    --databases reader_test --users reader_test:ReaderTestPassword123 \
    -f value -c id
fi

echo
echo "Reader create-permission results"
printf '%-34s %s\n' resource result
printf '%-34s %s\n' '----------------------------------' '------------------'
for result in "${RESULTS[@]}"; do
  IFS='|' read -r label status <<<"$result"
  printf '%-34s %s\n' "$label" "$status"
done
echo
echo "Denied: $DENIED_COUNT; skipped: $SKIPPED_COUNT; unexpected successes: $UNEXPECTED_COUNT; inconclusive: $ERROR_COUNT"

if [[ $UNEXPECTED_COUNT -gt 0 || $ERROR_COUNT -gt 0 ]]; then
  echo "Reader is not conclusively read-only for every tested resource type."
  exit 1
fi

echo "All applicable tested create operations were denied for the reader user."
