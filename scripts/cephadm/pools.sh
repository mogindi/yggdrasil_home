#!/bin/bash

# Based on https://docs.ceph.com/en/octopus/install/ceph-deploy/quick-ceph-deploy/

set -xe

ensure_pool() {
	if ceph osd pool ls --format plain | grep -Fxq "$1"; then
		return
	fi
	ceph osd pool create "$1"
}

# create ceph pools
ensure_pool volumes
ensure_pool images
ensure_pool backups
ensure_pool vms
ensure_pool gnocchi

# initialize pools
rbd pool init volumes
rbd pool init images
rbd pool init backups
rbd pool init vms
