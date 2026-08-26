#!/usr/bin/env bash

set -Eeuo pipefail

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/workspace"
config_dir="$workspace_dir/etc/kolla"
poll_seconds=5
test_timeout_seconds="${CINDER_BACKUP_TEST_TIMEOUT:-600}"
delete_timeout_seconds="${CINDER_BACKUP_DELETE_TIMEOUT:-120}"
run_id="$(date -u +%Y%m%d%H%M%S)-$$"

source "$workspace_dir/kolla-venv/bin/activate"
source "$config_dir/admin-openrc.sh"

source_volume_name="cinder-backup-test-source-$run_id"
restore_volume_name="cinder-backup-test-restore-$run_id"
incremental_restore_volume_name="cinder-backup-test-incremental-restore-$run_id"
clone_volume_name="cinder-backup-test-clone-$run_id"
full_backup_name="cinder-backup-test-full-$run_id"
incremental_backup_name="cinder-backup-test-incremental-$run_id"

source_volume_id=""
restore_volume_id=""
incremental_restore_volume_id=""
clone_volume_id=""
full_backup_id=""
incremental_backup_id=""

wait_for_volume() {
  local volume_id="$1"
  local elapsed=0
  local status=""

  while (( elapsed <= test_timeout_seconds )); do
    status="$(openstack volume show "$volume_id" -f value -c status 2>/dev/null || true)"
    case "$status" in
      available)
        return 0
        ;;
      error|error_restoring|error_deleting|error_extending|error_uploading)
        printf 'Volume %s entered failure status: %s\n' "$volume_id" "$status" >&2
        return 1
        ;;
    esac
    sleep "$poll_seconds"
    elapsed=$((elapsed + poll_seconds))
  done

  printf 'Timed out waiting for volume %s; last status: %s\n' "$volume_id" "$status" >&2
  return 1
}

wait_for_backup() {
  local backup_id="$1"
  local elapsed=0
  local status=""

  while (( elapsed <= test_timeout_seconds )); do
    status="$(openstack volume backup show "$backup_id" -f value -c status 2>/dev/null || true)"
    case "$status" in
      available)
        return 0
        ;;
      error|error_deleting|error_restoring|error_creating)
        printf 'Backup %s entered failure status: %s\n' "$backup_id" "$status" >&2
        return 1
        ;;
    esac
    sleep "$poll_seconds"
    elapsed=$((elapsed + poll_seconds))
  done

  printf 'Timed out waiting for backup %s; last status: %s\n' "$backup_id" "$status" >&2
  return 1
}

delete_backup() {
  local backup_id="$1"
  local elapsed=0

  [[ -z "$backup_id" ]] && return 0
  openstack volume backup delete "$backup_id" >/dev/null 2>&1 || true
  while (( elapsed <= delete_timeout_seconds )); do
    if ! openstack volume backup show "$backup_id" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$poll_seconds"
    elapsed=$((elapsed + poll_seconds))
  done
  printf 'Timed out deleting backup %s\n' "$backup_id" >&2
}

delete_volume() {
  local volume_id="$1"
  local elapsed=0

  [[ -z "$volume_id" ]] && return 0
  openstack volume delete "$volume_id" >/dev/null 2>&1 || true
  while (( elapsed <= delete_timeout_seconds )); do
    if ! openstack volume show "$volume_id" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$poll_seconds"
    elapsed=$((elapsed + poll_seconds))
  done
  printf 'Timed out deleting volume %s\n' "$volume_id" >&2
}

cleanup() {
  set +e
  delete_backup "$incremental_backup_id"
  delete_backup "$full_backup_id"
  delete_volume "$clone_volume_id"
  delete_volume "$incremental_restore_volume_id"
  delete_volume "$restore_volume_id"
  delete_volume "$source_volume_id"
}

trap cleanup EXIT

backup_service_state="$(openstack volume service list --service cinder-backup -f value -c State)"
if [[ "$backup_service_state" != *up* ]]; then
  printf 'Cinder backup service is not up: %s\n' "$backup_service_state" >&2
  exit 1
fi

backup_driver="$(docker exec cinder_backup awk -F' *= *' '$1 == "backup_driver" { print $2; exit }' /etc/cinder/cinder.conf)"
if [[ "$backup_driver" != cinder.backup.drivers.s3.S3BackupDriver ]]; then
  printf 'Cinder backup driver is %s, expected RadosGW S3\n' "$backup_driver" >&2
  exit 1
fi

source_volume_id="$(openstack volume create --size 1 "$source_volume_name" -f value -c id)"
wait_for_volume "$source_volume_id"

full_backup_id="$(openstack volume backup create --name "$full_backup_name" "$source_volume_id" -f value -c id)"
wait_for_backup "$full_backup_id"

incremental_backup_id="$(openstack volume backup create --incremental --name "$incremental_backup_name" "$source_volume_id" -f value -c id)"
wait_for_backup "$incremental_backup_id"

restore_volume_id="$(openstack volume create --size 1 "$restore_volume_name" -f value -c id)"
wait_for_volume "$restore_volume_id"
openstack volume backup restore --force "$full_backup_id" "$restore_volume_id" >/dev/null
wait_for_volume "$restore_volume_id"

incremental_restore_volume_id="$(openstack volume create --size 1 "$incremental_restore_volume_name" -f value -c id)"
wait_for_volume "$incremental_restore_volume_id"
openstack volume backup restore --force "$incremental_backup_id" "$incremental_restore_volume_id" >/dev/null
wait_for_volume "$incremental_restore_volume_id"

clone_volume_id="$(openstack volume create --backup "$full_backup_id" "$clone_volume_name" -f value -c id)"
wait_for_volume "$clone_volume_id"

printf 'Cinder RadosGW backup test passed: full, incremental, restore, incremental restore, and create-from-backup.\n'
