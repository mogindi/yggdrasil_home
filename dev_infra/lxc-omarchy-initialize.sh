#!/usr/bin/env bash

set -Eeuo pipefail

# Omarchy image builds need a managed LXD bridge and a storage pool, but the
# general Dev Infra initializer assumes a 120 GiB ZFS pool.  This variant is
# safe for hosts with an existing root filesystem and avoids reusing the
# host's 10.227.41.0/24 network.

LXD_STORAGE_POOL="${LXD_STORAGE_POOL:-default}"
LXD_STORAGE_DRIVER="${LXD_STORAGE_DRIVER:-dir}"
LXD_NETWORK="${LXD_NETWORK:-lxdbr0}"
LXD_NETWORK_IPV4="${LXD_NETWORK_IPV4:-10.227.42.1/24}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v lxc >/dev/null 2>&1 || die "lxc is not installed"
command -v lxd >/dev/null 2>&1 || die "lxd is not installed"

if [[ ! -e /dev/kvm ]]; then
  die "/dev/kvm is required for an efficient nested VM build"
fi

if ! lxc info 2>/dev/null | grep -q 'virtual-machine'; then
  die "this LXD server does not advertise virtual-machine support"
fi

if ! lxc storage show "$LXD_STORAGE_POOL" >/dev/null 2>&1; then
  echo "==> Creating LXD $LXD_STORAGE_DRIVER storage pool: $LXD_STORAGE_POOL"
  lxc storage create "$LXD_STORAGE_POOL" "$LXD_STORAGE_DRIVER"
else
  storage_driver="$(lxc storage get "$LXD_STORAGE_POOL" driver 2>/dev/null || true)"
  [[ -n "$storage_driver" ]] || die "Unable to determine driver for LXD storage pool $LXD_STORAGE_POOL"
  echo "==> Reusing LXD storage pool $LXD_STORAGE_POOL ($storage_driver)"
fi

if ! lxc network show "$LXD_NETWORK" >/dev/null 2>&1; then
  echo "==> Creating LXD bridge $LXD_NETWORK ($LXD_NETWORK_IPV4)"
  lxc network create "$LXD_NETWORK" \
    "ipv4.address=$LXD_NETWORK_IPV4" \
    ipv4.nat=true \
    ipv6.address=none
else
  echo "==> Reusing LXD network $LXD_NETWORK"
fi

lxc profile show default >/dev/null 2>&1 || die "LXD default profile is missing"

if ! lxc profile device get default root pool >/dev/null 2>&1; then
  echo "==> Adding the default root disk to profile default"
  lxc profile device add default root disk path=/ pool="$LXD_STORAGE_POOL"
else
  profile_pool="$(lxc profile device get default root pool)"
  [[ "$profile_pool" == "$LXD_STORAGE_POOL" ]] ||
    die "profile default root disk uses pool $profile_pool, expected $LXD_STORAGE_POOL"
fi

if ! lxc profile device get default eth0 network >/dev/null 2>&1; then
  echo "==> Adding $LXD_NETWORK to profile default"
  lxc profile device add default eth0 nic network="$LXD_NETWORK" name=eth0
fi

echo "==> LXD Omarchy build environment is ready"
lxc storage list
lxc network list
lxc profile show default
