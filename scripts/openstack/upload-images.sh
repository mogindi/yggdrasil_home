#!/bin/bash

set -xe

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# source venv
cd workspace
source kolla-venv/bin/activate

CONFIG_DIR=$(pwd)/etc/kolla

# source admin rc
. $CONFIG_DIR/admin-openrc.sh

source ../scripts/openstack/image-utils.sh

date=$(date +%Y%m%d)

commands="apt update && DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Options::=--force-confdef -o DPkg::Options::=--force-confold install -y qemu-guest-agent"

upload_ubuntu_noble_image() {
  create_openstack_linux_image https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img  \
    ubuntu-noble-24.04.$date.x86_64 \
    "$commands" \
    "--public --property os_distro=ubuntu --property os_type=linux --property os_version=24.04 --property os_admin_user=root --property hw_qemu_guest_agent=yes"
}

upload_trove_mongodb_image() {
  local image_name="${TROVE_MONGODB_GUEST_IMAGE_NAME:-trove-master-guest-ubuntu-noble-mongodb}"
  local image_url="${TROVE_MONGODB_GUEST_IMAGE_URL:-https://tarballs.opendev.org/openstack/trove/images/trove-master-guest-ubuntu-noble.qcow2}"
  local mongodb_image="${TROVE_MONGODB_CONTAINER_IMAGE:-mongo}"
  local mongodb_version="${TROVE_MONGODB_VERSION:-8.0}"
  local mongodb_image_ref="$mongodb_image"
  local payload_dir="$SCRIPT_DIR/trove-mongodb"
  local commands

  if [[ ! "$image_name" =~ ^[A-Za-z0-9._-]+$ ]] || \
     [[ ! "$mongodb_image" =~ ^[A-Za-z0-9._/@:-]+$ ]] || \
     [[ ! "$mongodb_version" =~ ^[0-9][A-Za-z0-9._-]*$ ]]; then
    echo "Invalid MongoDB guest image name, container image, or version" >&2
    return 2
  fi

  if [[ "$mongodb_image" != *@* && \
        "${mongodb_image##*/}" != *:* ]]; then
    mongodb_image_ref="$mongodb_image:$mongodb_version"
  fi

  commands="mkdir -p /etc/trove && printf '%s\\n' '$mongodb_image_ref' > /etc/trove/mongodb-image && chmod 0644 /etc/trove/mongodb-image"

  create_openstack_linux_image "$image_url" \
    "$image_name" \
    "$commands" \
    "--private --property hw_rng_model=virtio --property trove_mongodb_injected=true --tag trove --tag mongodb" \
    "" \
    "" \
    "$payload_dir" \
    "/opt/guest-agent-venv/lib/python3.12/site-packages/trove/guestagent/datastore/experimental/mongodb"
}

case "${1:-}" in
  "") ;;
  --noble-only)
    upload_ubuntu_noble_image
    exit 0
    ;;
  --trove-mongodb)
    upload_trove_mongodb_image
    exit 0
    ;;
  *)
    echo "Usage: $0 [--noble-only|--trove-mongodb]" >&2
    exit 2
    ;;
esac

create_openstack_linux_image https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img \
  ubuntu-jammy-22.04.$date.x86_64 \
  "$commands" \
  "--public --property os_distro=ubuntu --property os_type=linux --property os_version=22.04 --property os_admin_user=root --property hw_qemu_guest_agent=yes"

upload_ubuntu_noble_image

# not running commands - no sh?
create_openstack_linux_image https://cloud.centos.org/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2 \
  centos-stream-10.$date.x86_64 \
  "" \
  "--public --property os_distro=centos --property os_type=linux --property os_version=10 --property os_admin_user=root --property hw_qemu_guest_agent=no"  # TODO add qemu agent

create_openstack_linux_image https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2 \
  fedora-cloud-43.$date.x86_64 \
  "" \
  "--public --property os_distro=fedora --property os_type=linux --property os_version=43 --property os_admin_user=root --property hw_qemu_guest_agent=no"  # TODO add qemu agent

create_openstack_linux_image https://fastly.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2 \
  arch-linux-v20260101.20260101.x86_64 \
  "" \
  "--public --property os_distro=arch --property os_type=linux --property os_version=v20260101 --property os_admin_user=root --property hw_qemu_guest_agent=no"  # TODO add qemu agent

create_openstack_linux_image https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2 \
  debian-trixie-13.$date.x86_64 \
  "" \
  "--public --property os_distro=debian --property os_type=linux --property os_version=13 --property os_admin_user=root --property hw_qemu_guest_agent=no" # TODO add qemu agent

# can't even mount or run commands in image
create_openstack_linux_image https://download.freebsd.org/releases/VM-IMAGES/15.0-RELEASE/amd64/Latest/FreeBSD-15.0-RELEASE-amd64-ufs.qcow2.xz \
  freebsd-RELEASE-15.$date.x86_64 \
  "" \
  "--public --property os_distro=freebsd --property os_type=linux --property os_version=15 --property os_admin_user=root --property hw_qemu_guest_agent=no" \
  "xz_decompress"


pass=$(cat ~/hetzner-storagebox.pass)
if ! mount | grep -q /mnt/winshare; then
  mkdir -p /mnt/winshare
  mount.cifs -o user=u429780,pass=$pass //u429780.your-storagebox.de/backup /mnt/winshare/
fi

file=/mnt/winshare/Win2022_20251209.raw
image_name=windows-server-2022.20251209.x86_64
openstack image list -f value | grep $(echo $image_name | sed 's/\..*//g') || openstack image create --public \
  --property os_distro=windows --property os_type=windows --property os_version=s2022 \
  --property os_admin_user=Administrator --property hw_qemu_guest_agent=yes \
  --file $file \
  --progress \
  $image_name
