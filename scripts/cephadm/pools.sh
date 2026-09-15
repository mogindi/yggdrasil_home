#!/bin/bash

# Based on https://docs.ceph.com/en/octopus/install/ceph-deploy/quick-ceph-deploy/

set -xe

if [ -z "${CEPHADM_IMAGE:-}" ]; then
	printf '%s\n' 'CEPHADM_IMAGE must be set' >&2
	exit 1
fi

cephadm_ceph() {
	timeout 120s cephadm --image "$CEPHADM_IMAGE" shell -- "$@"
}

ensure_pool() {
	if cephadm_ceph ceph osd pool get "$1" pg_num >/dev/null 2>&1; then
		return
	fi
	cephadm_ceph ceph osd pool create "$1"
}

ensure_pool_setting() {
	local pool="$1"
	local setting="$2"
	local expected="$3"
	local current

	current=$(cephadm_ceph ceph osd pool get "$pool" "$setting" |
		awk -v setting="$setting" '$1 == setting ":" { print $2; exit }')
	if [ "$current" != "$expected" ]; then
		cephadm_ceph ceph osd pool set "$pool" "$setting" "$expected"
	fi
}

gnocchi_pool="${CEPH_GNOCCHI_POOL_NAME:-gnocchi}"

# create ceph pools
ensure_pool volumes
ensure_pool images
ensure_pool backups
ensure_pool vms
ensure_pool "$gnocchi_pool"

# The values are normally inherited from Ceph's default pool settings. Set
# them explicitly for Gnocchi so a later pool creation or a multi-node
# inventory cannot silently leave the metrics pool with the AIO replica count.
if [ -n "${CEPH_GNOCCHI_POOL_SIZE:-}" ]; then
	ensure_pool_setting "$gnocchi_pool" size "$CEPH_GNOCCHI_POOL_SIZE"
fi
if [ -n "${CEPH_GNOCCHI_POOL_MIN_SIZE:-}" ]; then
	ensure_pool_setting "$gnocchi_pool" min_size "$CEPH_GNOCCHI_POOL_MIN_SIZE"
fi

# initialize pools
cephadm_ceph rbd pool init volumes
cephadm_ceph rbd pool init images
cephadm_ceph rbd pool init backups
cephadm_ceph rbd pool init vms

touch "${CEPHADM_POOLS_MARKER:-/root/cephadm_pools_v2.done}"
