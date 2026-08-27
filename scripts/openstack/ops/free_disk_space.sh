#!/usr/bin/env bash

# Recover space from an AIO/OpenStack host without deleting live Ceph storage.
# Run as root, for example:
#   sudo ./free_disk_space.sh
#
# The script is intentionally explicit about the dangerous operations:
#   --aggressive                  remove all unused images/volumes and truncate
#                                 active Docker JSON logs as well
#   --discard-fluentd-retry       discard queued OpenSearch retry records
#   --remove-freebsd-artifacts    remove known disposable FreeBSD build images
#   --dry-run                     print planned destructive actions only
#   --yes                         skip the confirmation prompt

set -Eeuo pipefail

readonly OPENSEARCH_URL_DEFAULT="http://10.0.10.100:9200"
readonly OPENSEARCH_SOFT_RETENTION_DAYS_DEFAULT=3
readonly OPENSEARCH_HARD_RETENTION_DAYS_DEFAULT=7
readonly MARIADB_BINLOG_RETENTION_DAYS_DEFAULT=7

OPENSEARCH_URL="${OPENSEARCH_URL:-$OPENSEARCH_URL_DEFAULT}"
OPENSEARCH_SOFT_RETENTION_DAYS="${OPENSEARCH_SOFT_RETENTION_DAYS:-$OPENSEARCH_SOFT_RETENTION_DAYS_DEFAULT}"
OPENSEARCH_HARD_RETENTION_DAYS="${OPENSEARCH_HARD_RETENTION_DAYS:-$OPENSEARCH_HARD_RETENTION_DAYS_DEFAULT}"
MARIADB_BINLOG_RETENTION_DAYS="${MARIADB_BINLOG_RETENTION_DAYS:-$MARIADB_BINLOG_RETENTION_DAYS_DEFAULT}"

ASSUME_YES=false
DRY_RUN=false
TRUNCATE_HOST_LOGS=true
TRUNCATE_ACTIVE_DOCKER_LOGS=false
PRUNE_ALL_IMAGES=false
PRUNE_UNUSED_VOLUMES=false
DISCARD_FLUENTD_RETRY=false
REMOVE_FREEBSD_ARTIFACTS=false

usage() {
  cat <<'EOF'
Usage: free_disk_space.sh [options]

Options:
  --aggressive                  Prune all unused Docker images and volumes and
                                truncate active Docker JSON logs.
  --discard-fluentd-retry       Stop Fluentd, discard its queued OpenSearch
                                retry records, and start it again.
  --remove-freebsd-artifacts    Remove known disposable FreeBSD build images.
  --preserve-host-logs          Do not truncate current /var/log/*.log files.
  --dry-run                     Show planned actions without changing data.
  --yes                         Do not ask for confirmation.
  -h, --help                    Show this help.

Environment overrides:
  OPENSEARCH_URL, OPENSEARCH_SOFT_RETENTION_DAYS,
  OPENSEARCH_HARD_RETENTION_DAYS, MARIADB_BINLOG_RETENTION_DAYS,
  MARIADB_ROOT_PASSWORD
EOF
}

log() {
  printf '[free-disk-space] %s\n' "$*"
}

warn() {
  printf '[free-disk-space] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[free-disk-space] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  if $DRY_RUN; then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

while (($#)); do
  case "$1" in
    --aggressive)
      PRUNE_ALL_IMAGES=true
      PRUNE_UNUSED_VOLUMES=true
      TRUNCATE_ACTIVE_DOCKER_LOGS=true
      ;;
    --discard-fluentd-retry)
      DISCARD_FLUENTD_RETRY=true
      ;;
    --remove-freebsd-artifacts)
      REMOVE_FREEBSD_ARTIFACTS=true
      ;;
    --preserve-host-logs)
      TRUNCATE_HOST_LOGS=false
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    --yes)
      ASSUME_YES=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option: $1"
      ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || die "run this script as root (for example: sudo $0)"

if ! $ASSUME_YES && ! $DRY_RUN; then
  read -r -p "WARNING: this performs destructive cleanup on the AIO host. Continue? (y/N) " reply
  [[ "$reply" =~ ^[Yy]$ ]] || exit 1
fi

trap 'warn "cleanup stopped at line $LINENO"' ERR

log "starting cleanup; filesystem state before changes:"
df -hT /

log "vacuuming systemd journal"
run journalctl --vacuum-size=500M

log "rotating Kolla service logs"
if docker ps --format '{{.Names}}' | grep -qx cron; then
  if $DRY_RUN; then
    log "would run logrotate inside the cron container"
  elif ! docker exec cron logrotate -f /etc/logrotate.conf; then
    warn "Kolla logrotate failed; continuing with the remaining cleanup"
  fi
else
  warn "cron container is not running; skipping Kolla logrotate"
fi

log "removing rotated host logs"
if $DRY_RUN; then
  find /var/log -xdev -type f \( -name '*.log.[0-9]*' -o -name '*.log.gz' \) -print
else
  find /var/log -xdev -type f \( -name '*.log.[0-9]*' -o -name '*.log.gz' \) -delete
fi

if $TRUNCATE_HOST_LOGS; then
  log "truncating current host log files"
  while IFS= read -r -d '' logfile; do
    if $DRY_RUN; then
      printf 'would truncate %s\n' "$logfile"
    else
      truncate -s 0 -- "$logfile"
    fi
  done < <(find /var/log -xdev -type f -name '*.log' -print0)
fi

log "clearing rotated Docker JSON logs"
while IFS= read -r -d '' logfile; do
  if $DRY_RUN; then
    printf 'would truncate %s\n' "$logfile"
  else
    truncate -s 0 -- "$logfile"
  fi
done < <(find /var/lib/docker/containers -xdev -type f -name '*-json.log.[1-9]*' -print0 2>/dev/null)

if $TRUNCATE_ACTIVE_DOCKER_LOGS; then
  log "truncating active Docker JSON logs (--aggressive)"
  while IFS= read -r -d '' logfile; do
    if $DRY_RUN; then
      printf 'would truncate %s\n' "$logfile"
    else
      truncate -s 0 -- "$logfile"
    fi
  done < <(find /var/lib/docker/containers -xdev -type f -name '*-json.log' -print0 2>/dev/null)
fi

log "pruning Docker caches"
if $PRUNE_ALL_IMAGES; then
  run docker image prune --all --force
else
  run docker image prune --force
fi
run docker builder prune --force
if $PRUNE_UNUSED_VOLUMES; then
  run docker volume prune --all --force
else
  run docker volume prune --force
fi

log "cleaning package and known application caches"
run apt-get clean
if command -v npm >/dev/null 2>&1; then
  run npm cache clean --force
fi
if command -v pip >/dev/null 2>&1; then
  run pip cache purge
fi
if [[ -d /root/.cache/Cypress ]]; then
  if $DRY_RUN; then
    log "would remove /root/.cache/Cypress contents"
  else
    find /root/.cache/Cypress -mindepth 1 -delete
  fi
fi
if [[ -d /var/cache ]]; then
  while IFS= read -r -d '' cache_file; do
    if $DRY_RUN; then
      printf 'would remove %s\n' "$cache_file"
    else
      unlink -- "$cache_file"
    fi
  done < <(find /var/cache -maxdepth 1 -type f -name 'kata-static-*.tar.zst' -print0)
fi

if $REMOVE_FREEBSD_ARTIFACTS; then
  log "removing explicitly requested FreeBSD build artifacts"
  for artifact in /root/yggdrasil_home/workspace/freebsd-RELEASE-*.raw \
                  /root/yggdrasil_home/workspace/freebsd-RELEASE-*.qcow2; do
    [[ -e "$artifact" ]] || continue
    if $DRY_RUN; then
      printf 'would remove %s\n' "$artifact"
    else
      unlink -- "$artifact"
    fi
  done
fi

log "preserving Ceph OSD loop-backed images"
for image in /mnt/disk-*.img; do
  [[ -e "$image" ]] || continue
  loop_devices="$(losetup -j "$image" 2>/dev/null || true)"
  if [[ -n "$loop_devices" ]]; then
    log "protected active Ceph image: $image ($loop_devices)"
  else
    warn "found an un-attached /mnt image; no automatic deletion is performed: $image"
  fi
done

log "cleaning MariaDB binary logs older than ${MARIADB_BINLOG_RETENTION_DAYS} days"
mariadb_container="$(docker ps --format '{{.Names}}' | awk '$0 == "mariadb" { print; exit }')"
mariadb_password="${MARIADB_ROOT_PASSWORD:-}"
if [[ -z "$mariadb_password" && -f /etc/kolla/passwords.yml ]]; then
  mariadb_password="$(sed -n 's/^database_password:[[:space:]]*//p' /etc/kolla/passwords.yml | head -n 1)"
fi

mariadb_query() {
  docker exec -e MYSQL_PWD="$mariadb_password" "$mariadb_container" \
    mariadb -uroot --batch --skip-column-names -e "$1"
}

if [[ -n "$mariadb_container" && -n "$mariadb_password" ]]; then
  if wsrep_ready="$(mariadb_query 'SHOW GLOBAL STATUS LIKE "wsrep_ready";' | awk '$1 == "wsrep_ready" { print $2 }')"; then
    wsrep_cluster_size="$(mariadb_query 'SHOW GLOBAL STATUS LIKE "wsrep_cluster_size";' | awk '$1 == "wsrep_cluster_size" { print $2 }')"
    replica_status="$(mariadb_query 'SHOW SLAVE STATUS\G' 2>/dev/null || true)"
    purge_before="$(date -u -d "-${MARIADB_BINLOG_RETENTION_DAYS} days" '+%Y-%m-%d %H:%M:%S')"
    if $DRY_RUN; then
      log "would set MariaDB binlog expiry to ${MARIADB_BINLOG_RETENTION_DAYS} days"
      if [[ -n "$replica_status" ]]; then
        warn "MariaDB reports an asynchronous replica; binlog purge would be skipped"
      elif [[ "$wsrep_ready" == ON && "${wsrep_cluster_size:-0}" -ge 1 ]]; then
        log "would purge MariaDB binary logs before $purge_before"
      else
        warn "MariaDB is not wsrep-ready; binlog purge would be skipped"
      fi
    else
      mariadb_query "SET GLOBAL binlog_expire_logs_seconds = $((MARIADB_BINLOG_RETENTION_DAYS * 86400));"
      if [[ -n "$replica_status" ]]; then
        warn "MariaDB reports an asynchronous replica; binlog purge was skipped"
      elif [[ "$wsrep_ready" == ON && "${wsrep_cluster_size:-0}" -ge 1 ]]; then
        mariadb_query "PURGE BINARY LOGS BEFORE '$purge_before';"
      else
        warn "MariaDB is not wsrep-ready; binlog purge was skipped"
      fi
    fi
  else
    warn "could not query MariaDB; binlog cleanup was skipped"
  fi
else
  warn "MariaDB container or password unavailable; binlog cleanup was skipped"
fi

log "cleaning OpenSearch log indices older than ${OPENSEARCH_HARD_RETENTION_DAYS} days"
if curl -fsS --max-time 10 "$OPENSEARCH_URL/_cluster/health" >/dev/null 2>&1; then
  if $DRY_RUN; then
    log "would clear OpenSearch read-only blocks"
  else
    curl -fsS --max-time 30 -X PUT \
      -H 'Content-Type: application/json' \
      "$OPENSEARCH_URL/*/_settings?expand_wildcards=open,closed" \
      --data '{"index.blocks.read_only_allow_delete":null}' >/dev/null
  fi

  soft_cutoff="$(date -u -d "today - ${OPENSEARCH_SOFT_RETENTION_DAYS} days" +%s)"
  hard_cutoff="$(date -u -d "today - ${OPENSEARCH_HARD_RETENTION_DAYS} days" +%s)"
  while IFS= read -r index; do
    [[ "$index" =~ ^flog-([0-9]{4}\.[0-9]{2}\.[0-9]{2})$ ]] || continue
    index_epoch="$(date -u -d "${BASH_REMATCH[1]//./-} 00:00:00" +%s 2>/dev/null || true)"
    [[ -n "$index_epoch" ]] || continue
    if (( index_epoch < hard_cutoff )); then
      if $DRY_RUN; then
        log "would delete OpenSearch index $index"
      else
        curl -fsS --max-time 30 -X DELETE "$OPENSEARCH_URL/$index" >/dev/null
      fi
    elif (( index_epoch < soft_cutoff )); then
      if $DRY_RUN; then
        log "would close OpenSearch index $index"
      else
        curl -fsS --max-time 30 -X POST "$OPENSEARCH_URL/$index/_close" >/dev/null
      fi
    fi
  done < <(curl -fsS --max-time 30 \
    "$OPENSEARCH_URL/_cat/indices/flog-*?expand_wildcards=open,closed&format=tsv&h=index" || true)

  if $DRY_RUN; then
    log "would flush OpenSearch flog indices"
  else
    curl -fsS --max-time 60 -X POST "$OPENSEARCH_URL/flog-*/_flush" >/dev/null || \
      warn "OpenSearch flush failed"
  fi
else
  warn "OpenSearch is unavailable; index cleanup was skipped"
fi

if $DISCARD_FLUENTD_RETRY; then
  log "discarding Fluentd OpenSearch retry records"
  fluentd_volume="$(docker volume inspect -f '{{.Mountpoint}}' fluentd_data 2>/dev/null || true)"
  fluentd_buffer="${fluentd_volume:+$fluentd_volume/opensearch.buffer}"
  [[ -d "${fluentd_buffer:-}" ]] || die "Fluentd retry buffer was not found"
  fluentd_was_running=false
  if docker ps --format '{{.Names}}' | grep -qx fluentd; then
    fluentd_was_running=true
    run docker stop fluentd
  fi
  if $DRY_RUN; then
    find "$fluentd_buffer" -maxdepth 1 -type f -name '*.log' -print
  else
    find "$fluentd_buffer" -maxdepth 1 -type f -name '*.log' -delete
  fi
  if $fluentd_was_running; then
    run docker start fluentd
  fi
else
  fluentd_volume="$(docker volume inspect -f '{{.Mountpoint}}' fluentd_data 2>/dev/null || true)"
  if [[ -d "${fluentd_volume:-}/opensearch.buffer" ]]; then
    log "preserving Fluentd retry buffer:"
    du -sh "$fluentd_volume/opensearch.buffer"
  fi
fi

log "cleanup complete; filesystem state after changes:"
df -hT /
docker system df
