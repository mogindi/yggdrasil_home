#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUEST_SCRIPT="$SCRIPT_DIR/omarchy-image-guest.sh"

LXC_IMAGE_SOURCE="${OMARCHY_LXC_IMAGE_SOURCE:-${OMARCHY_LXC_IMAGE_ALIAS:-ubuntu:24.04}}"
LXC_NAME="${OMARCHY_LXC_NAME:-omarchy-image-builder-$$}"
LXC_CPU="${OMARCHY_LXC_CPU:-4}"
LXC_MEMORY="${OMARCHY_LXC_MEMORY:-8GiB}"
LXC_ROOT_SIZE="${OMARCHY_LXC_ROOT_SIZE:-80GiB}"
LXC_NETWORK="${OMARCHY_LXC_NETWORK:-lxdbr0}"

OMARCHY_DISK_SIZE="${OMARCHY_DISK_SIZE:-50G}"
OMARCHY_GUEST_CPUS="${OMARCHY_GUEST_CPUS:-4}"
OMARCHY_GUEST_MEMORY="${OMARCHY_GUEST_MEMORY:-8G}"
OMARCHY_OUTPUT_FORMAT="${OMARCHY_OUTPUT_FORMAT:-raw}"
OMARCHY_IMAGE_NAME="${OMARCHY_IMAGE_NAME:-omarchy-latest}"
OMARCHY_IMAGE_VISIBILITY="${OMARCHY_IMAGE_VISIBILITY:-public}"
OMARCHY_INSTALL_TIMEOUT="${OMARCHY_INSTALL_TIMEOUT:-30m}"
OMARCHY_GUEST_BOOT_TIMEOUT="${OMARCHY_GUEST_BOOT_TIMEOUT:-20m}"
OMARCHY_ALLOW_TCG="${OMARCHY_ALLOW_TCG:-0}"
OMARCHY_ISO_URL="${OMARCHY_ISO_URL:-}"
OMARCHY_ISO_SHA256_URL="${OMARCHY_ISO_SHA256_URL:-}"
OMARCHY_RELEASE_PAGE="${OMARCHY_RELEASE_PAGE:-https://omarchy.org}"
OPENSTACK_RC_FILE="${OPENSTACK_RC_FILE:-}"
OPENSTACK_BIN="${OMARCHY_OPENSTACK_BIN:-}"

NO_UPLOAD=false
DRY_RUN=false
KEEP_BUILDER=false
KEEP_ARTIFACTS="${OMARCHY_KEEP_ARTIFACTS:-0}"
LXC_CREATED=false
RUN_DIR=""
OPENSTACK_READY=false

usage() {
  cat <<'EOF'
Usage: omarchy-image-builder.sh [options]

Build an Omarchy OpenStack image inside an LXC VM and upload it to Glance.

Options:
  --no-upload       Build the image but do not upload it to OpenStack.
  --dry-run         Resolve and print the build configuration without launching LXC.
  --keep-builder    Keep the temporary LXC VM after the build for inspection.
  --help            Show this help text.

Important environment variables:
  OMARCHY_ISO_URL             Override the ISO URL (otherwise omarchy.org is queried).
  OMARCHY_ISO_SHA256_URL      Override the checksum URL (defaults to ISO URL + .sha256).
  OMARCHY_IMAGE_NAME          Glance image name (default: omarchy-latest).
  OMARCHY_IMAGE_VISIBILITY    public or private (default: public).
  OMARCHY_DISK_SIZE           Nested guest disk size, minimum 32G (default: 50G).
  OMARCHY_OUTPUT_FORMAT       raw or qcow2 (default: raw).
  OMARCHY_GUEST_CPUS          Nested guest vCPU count (default: 4).
  OMARCHY_GUEST_MEMORY        Nested guest memory (default: 8G).
  OMARCHY_LXC_IMAGE_SOURCE    LXC VM image/remote alias (default: ubuntu:24.04).
  OMARCHY_LXC_NETWORK         Managed LXD network (default: lxdbr0).
  OMARCHY_ALLOW_TCG           Set to 1 to allow software emulation if /dev/kvm is absent.
  OPENSTACK_RC_FILE           Optional OpenStack RC file to source before upload.
  OMARCHY_OPENSTACK_BIN       OpenStack CLI path (auto-detected from a Kolla venv when unset).
  OMARCHY_KEEP_ARTIFACTS      Set to 1 to preserve the local build directory.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

command_required() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

valid_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

valid_image_name() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]
}

valid_size() {
  [[ "$1" =~ ^[1-9][0-9]*(G|GiB|GB|M|MiB|MB)$ ]]
}

size_in_mib() {
  local size="$1"
  local value="${size//[!0-9]/}"
  local unit="${size//[0-9]/}"
  case "$unit" in
    G|GiB|GB)
      printf '%s\n' "$((value * 1024))"
      ;;
    M|MiB|MB)
      printf '%s\n' "$value"
      ;;
    *)
      return 1
      ;;
  esac
}

prepare_openstack() {
  [[ "$OPENSTACK_READY" == true ]] && return 0

  if [[ -n "$OPENSTACK_RC_FILE" ]]; then
    [[ -f "$OPENSTACK_RC_FILE" ]] || die "OpenStack RC file not found: $OPENSTACK_RC_FILE"
    # shellcheck disable=SC1090
    source "$OPENSTACK_RC_FILE"
  fi

  if [[ -n "$OPENSTACK_BIN" ]]; then
    command_required "$OPENSTACK_BIN"
    OPENSTACK_READY=true
    return 0
  fi

  if command -v openstack >/dev/null 2>&1; then
    OPENSTACK_BIN="$(command -v openstack)"
    OPENSTACK_READY=true
    return 0
  fi

  local candidate
  local -a workspace_candidates=(
    "${OMARCHY_OPENSTACK_WORKSPACE:-}"
    workspace
    /root/yggdrasil_home/workspace
    /workspace4/yggdrasil_home/workspace
  )
  for candidate in "${workspace_candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    if [[ -x "$candidate/kolla-venv/bin/openstack" ]]; then
      OPENSTACK_BIN="$candidate/kolla-venv/bin/openstack"
      OPENSTACK_READY=true
      return 0
    fi
  done

  die "OpenStack CLI not found; source the Kolla venv or set OMARCHY_OPENSTACK_BIN"
}

openstack_cmd() {
  "$OPENSTACK_BIN" "$@"
}

resolve_iso() {
  if [[ -z "$OMARCHY_ISO_URL" ]]; then
    log "Resolving the latest Omarchy ISO from $OMARCHY_RELEASE_PAGE"
    local release_page
    release_page="$(curl --fail --silent --show-error --location --retry 3 "$OMARCHY_RELEASE_PAGE")" ||
      die "Unable to fetch the Omarchy release page"
    OMARCHY_ISO_URL="$(printf '%s' "$release_page" | grep -Eo 'https://iso\.omarchy\.org/omarchy-[0-9]+(\.[0-9]+)*\.iso' | sort -V | tail -n 1 || true)"
    [[ -n "$OMARCHY_ISO_URL" ]] ||
      die "Unable to find an Omarchy ISO URL on $OMARCHY_RELEASE_PAGE; set OMARCHY_ISO_URL explicitly"
  fi

  [[ "$OMARCHY_ISO_URL" =~ ^https://[^[:space:]]+\.iso$ ]] ||
    die "OMARCHY_ISO_URL must be an HTTPS URL ending in .iso"

  local iso_filename
  iso_filename="${OMARCHY_ISO_URL##*/}"
  OMARCHY_ISO_VERSION="${iso_filename#omarchy-}"
  OMARCHY_ISO_VERSION="${OMARCHY_ISO_VERSION%.iso}"
  [[ "$OMARCHY_ISO_VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]] ||
    die "Unable to determine an Omarchy version from $OMARCHY_ISO_URL"

  if [[ -z "$OMARCHY_ISO_SHA256_URL" ]]; then
    OMARCHY_ISO_SHA256_URL="${OMARCHY_ISO_URL}.sha256"
  fi
  [[ "$OMARCHY_ISO_SHA256_URL" =~ ^https://[^[:space:]]+$ ]] ||
    die "OMARCHY_ISO_SHA256_URL must be an HTTPS URL"
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
      --no-upload)
        NO_UPLOAD=true
        ;;
      --dry-run)
        DRY_RUN=true
        ;;
      --keep-builder)
        KEEP_BUILDER=true
        KEEP_ARTIFACTS=1
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
    shift
  done
}

validate_configuration() {
  valid_positive_integer "$LXC_CPU" || die "OMARCHY_LXC_CPU must be a positive integer"
  valid_positive_integer "$OMARCHY_GUEST_CPUS" || die "OMARCHY_GUEST_CPUS must be a positive integer"
  valid_size "$OMARCHY_DISK_SIZE" || die "OMARCHY_DISK_SIZE must be a size such as 50G or 50GiB"
  local disk_size_mib
  disk_size_mib="$(size_in_mib "$OMARCHY_DISK_SIZE")"
  ((disk_size_mib >= 32768)) || die "OMARCHY_DISK_SIZE must be at least 32G for Omarchy"
  valid_image_name "$OMARCHY_IMAGE_NAME" || die "OMARCHY_IMAGE_NAME contains unsupported characters"
  [[ "$LXC_NETWORK" =~ ^[A-Za-z0-9_.-]+$ ]] || die "OMARCHY_LXC_NETWORK contains unsupported characters"
  case "$OMARCHY_OUTPUT_FORMAT" in
    raw|qcow2) ;;
    *) die "OMARCHY_OUTPUT_FORMAT must be raw or qcow2" ;;
  esac
  case "$OMARCHY_IMAGE_VISIBILITY" in
    public|private) ;;
    *) die "OMARCHY_IMAGE_VISIBILITY must be public or private" ;;
  esac
}

print_configuration() {
  cat <<EOF
OMARCHY_ISO_URL=$OMARCHY_ISO_URL
OMARCHY_ISO_VERSION=$OMARCHY_ISO_VERSION
OMARCHY_ISO_SHA256_URL=$OMARCHY_ISO_SHA256_URL
OMARCHY_IMAGE_NAME=$OMARCHY_IMAGE_NAME
OMARCHY_IMAGE_VISIBILITY=$OMARCHY_IMAGE_VISIBILITY
OMARCHY_DISK_SIZE=$OMARCHY_DISK_SIZE
OMARCHY_OUTPUT_FORMAT=$OMARCHY_OUTPUT_FORMAT
OMARCHY_GUEST_CPUS=$OMARCHY_GUEST_CPUS
OMARCHY_GUEST_MEMORY=$OMARCHY_GUEST_MEMORY
OMARCHY_LXC_IMAGE_SOURCE=$LXC_IMAGE_SOURCE
OMARCHY_LXC_NETWORK=$LXC_NETWORK
OMARCHY_LXC_NAME=$LXC_NAME
EOF
}

cleanup() {
  local exit_code=$?
  if [[ "$LXC_CREATED" == true && "$KEEP_BUILDER" == false ]]; then
    log "Removing temporary LXC VM $LXC_NAME"
    lxc delete --force "$LXC_NAME" >/dev/null 2>&1 || true
  elif [[ "$LXC_CREATED" == true ]]; then
    echo "LXC builder retained: $LXC_NAME"
  fi

  if [[ -n "$RUN_DIR" && "$KEEP_ARTIFACTS" != 1 ]]; then
    rm -rf -- "$RUN_DIR"
  elif [[ -n "$RUN_DIR" ]]; then
    echo "Build artifacts retained in $RUN_DIR"
  fi
  exit "$exit_code"
}

wait_for_lxc_exec() {
  local attempt
  for attempt in $(seq 1 120); do
    if lxc exec "$LXC_NAME" -- true >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  die "LXC VM $LXC_NAME did not become ready"
}

wait_for_glance_active() {
  local image_id="$1"
  local attempt status
  for attempt in $(seq 1 120); do
    status="$(openstack_cmd image show "$image_id" -f value -c status 2>/dev/null || true)"
    case "$status" in
      active)
        return 0
        ;;
      killed|deleted)
        die "Uploaded image $image_id entered terminal status $status"
        ;;
    esac
    sleep 5
  done
  die "Uploaded image $image_id did not become active"
}

upload_image() {
  local artifact="$1"
  local metadata_file="$2"
  local image_format min_disk image_version existing_id new_image_id
  image_format="$(awk -F= '$1 == "IMAGE_FORMAT" { print $2; exit }' "$metadata_file")"
  image_version="$(awk -F= '$1 == "OMARCHY_VERSION" { print $2; exit }' "$metadata_file")"
  min_disk="$(awk -F= '$1 == "DISK_SIZE_GIB" { print $2; exit }' "$metadata_file")"

  [[ "$image_format" == raw || "$image_format" == qcow2 ]] || die "Invalid image format in build metadata"
  valid_positive_integer "$min_disk" || die "Invalid minimum disk size in build metadata"
  [[ "$image_version" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "Invalid Omarchy version in build metadata"

  prepare_openstack

  local visibility_args=()
  if [[ "$OMARCHY_IMAGE_VISIBILITY" == public ]]; then
    visibility_args+=(--public)
  else
    visibility_args+=(--private)
  fi

  local image_args=(
    image create "$OMARCHY_IMAGE_NAME"
    --file "$artifact"
    --disk-format "$image_format"
    --container-format bare
    --min-disk "$min_disk"
    "${visibility_args[@]}"
    --property "os_distro=omarchy"
    --property "os_type=linux"
    --property "os_version=$image_version"
    --property "os_admin_user=omarchy"
    --property "hw_qemu_guest_agent=yes"
    --property "hw_firmware_type=uefi"
    --property "hw_machine_type=q35"
    --property "hw_disk_bus=virtio"
    --property "cloud_init=enabled"
    --property "omarchy_version=$image_version"
    --tag omarchy
    --tag desktop
    --tag cloud-init
    --tag qemu-guest-agent
    --progress
  )

  existing_id=""
  if openstack_cmd image show "$OMARCHY_IMAGE_NAME" >/dev/null 2>&1; then
    existing_id="$(openstack_cmd image show "$OMARCHY_IMAGE_NAME" -f value -c id)"
  fi

  log "Uploading $OMARCHY_IMAGE_NAME ($image_format, Omarchy $image_version)"
  new_image_id="$(openstack_cmd "${image_args[@]}" -f value -c id)"
  [[ -n "$new_image_id" ]] || die "OpenStack did not return an image ID"
  wait_for_glance_active "$new_image_id"

  if [[ -n "$existing_id" && "$existing_id" != "$new_image_id" ]]; then
    if ! openstack_cmd image delete "$existing_id"; then
      openstack_cmd image delete "$new_image_id" >/dev/null 2>&1 || true
      die "Unable to replace existing image $existing_id"
    fi
  fi
  echo "OpenStack image ready: $OMARCHY_IMAGE_NAME ($new_image_id)"
}

main() {
  parse_args "$@"
  validate_configuration
  command_required curl
  command_required sha256sum
  command_required mktemp
  command_required grep
  command_required awk
  command_required sort
  resolve_iso

  if [[ "$DRY_RUN" == true ]]; then
    print_configuration
    return 0
  fi

  command_required lxc
  command_required seq
  [[ -x "$GUEST_SCRIPT" ]] || die "Guest builder script is not executable: $GUEST_SCRIPT"
  if [[ "$NO_UPLOAD" == false ]]; then
    prepare_openstack
  fi

  lxc info "$LXC_NAME" >/dev/null 2>&1 && die "LXC VM already exists: $LXC_NAME"

  RUN_DIR="$(mktemp -d /var/tmp/yggdrasil-omarchy.XXXXXX)"
  trap cleanup EXIT

  local user_data
  user_data=$(cat <<'EOF'
#cloud-config
package_update: true
packages:
  - ca-certificates
  - curl
  - genisoimage
  - jq
  - openssh-client
  - openssl
  - ovmf
  - qemu-system-x86
  - qemu-utils
  - xz-utils
EOF
  )

  log "Launching LXC VM $LXC_NAME"
  lxc launch --vm \
    --config "limits.cpu=$LXC_CPU" \
    --config "limits.memory=$LXC_MEMORY" \
    --config security.secureboot=false \
    --device "eth0,name=eth0,network=$LXC_NETWORK,type=nic" \
    --device "root,size=$LXC_ROOT_SIZE" \
    --config "cloud-init.user-data=$user_data" \
    "$LXC_IMAGE_SOURCE" "$LXC_NAME"
  LXC_CREATED=true

  wait_for_lxc_exec
  log "Waiting for LXC cloud-init"
  lxc exec "$LXC_NAME" -- cloud-init status --wait

  lxc file push "$GUEST_SCRIPT" "$LXC_NAME/root/omarchy-image-guest.sh"
  lxc exec "$LXC_NAME" -- chmod 0700 /root/omarchy-image-guest.sh
  log "Building Omarchy inside the LXC VM"
  lxc exec "$LXC_NAME" -- env \
    "OMARCHY_ISO_URL=$OMARCHY_ISO_URL" \
    "OMARCHY_ISO_SHA256_URL=$OMARCHY_ISO_SHA256_URL" \
    "OMARCHY_ISO_VERSION=$OMARCHY_ISO_VERSION" \
    "OMARCHY_DISK_SIZE=$OMARCHY_DISK_SIZE" \
    "OMARCHY_OUTPUT_FORMAT=$OMARCHY_OUTPUT_FORMAT" \
    "OMARCHY_GUEST_CPUS=$OMARCHY_GUEST_CPUS" \
    "OMARCHY_GUEST_MEMORY=$OMARCHY_GUEST_MEMORY" \
    "OMARCHY_INSTALL_TIMEOUT=$OMARCHY_INSTALL_TIMEOUT" \
    "OMARCHY_GUEST_BOOT_TIMEOUT=$OMARCHY_GUEST_BOOT_TIMEOUT" \
    "OMARCHY_ALLOW_TCG=$OMARCHY_ALLOW_TCG" \
    /root/omarchy-image-guest.sh

  local artifact_name artifact metadata_name artifact_path metadata_path
  artifact_name="omarchy-image.$OMARCHY_OUTPUT_FORMAT"
  metadata_name="build-metadata.env"
  artifact_path="$RUN_DIR/$artifact_name"
  metadata_path="$RUN_DIR/$metadata_name"
  lxc file pull "$LXC_NAME/root/omarchy-build/$artifact_name" "$artifact_path"
  lxc file pull "$LXC_NAME/root/omarchy-build/$metadata_name" "$metadata_path"
  [[ -s "$artifact_path" ]] || die "Builder returned an empty image artifact"
  [[ -s "$metadata_path" ]] || die "Builder did not return build metadata"

  if [[ "$NO_UPLOAD" == true ]]; then
    KEEP_ARTIFACTS=1
    echo "Image artifact ready: $artifact_path"
    echo "Build metadata: $metadata_path"
  else
    upload_image "$artifact_path" "$metadata_path"
  fi
}

main "$@"
