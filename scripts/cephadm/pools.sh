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

# create ceph pools
ensure_pool volumes
ensure_pool images
ensure_pool backups
ensure_pool vms

# initialize pools
cephadm_ceph rbd pool init volumes
cephadm_ceph rbd pool init images
cephadm_ceph rbd pool init backups
cephadm_ceph rbd pool init vms

touch /root/cephadm_pools.done
