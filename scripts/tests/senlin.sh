#!/usr/bin/env bash

set -euo pipefail

: "${SENLIN_TEST_IMAGE:=cirros-0.6.3-x86_64}"
: "${SENLIN_TEST_FLAVOR:=m1.tiny}"
: "${SENLIN_TEST_NETWORK:=demo-net}"
: "${SENLIN_TEST_KEYPAIR:=yggdrasil}"
: "${SENLIN_TEST_NAME:=yggdrasil-senlin-test-$$}"

tmp_spec=$(mktemp)
profile_id=''
cluster_id=''
trap 'rm -f "$tmp_spec"; [ -z "$cluster_id" ] || openstack cluster delete --force --force-delete --wait "$cluster_id" >/dev/null 2>&1 || true; [ -z "$profile_id" ] || openstack cluster profile delete --force "$profile_id" >/dev/null 2>&1 || true' EXIT

cat >"$tmp_spec" <<EOF
type: os.nova.server
version: 1.0
properties:
  name: ${SENLIN_TEST_NAME}
  flavor: ${SENLIN_TEST_FLAVOR}
  image: ${SENLIN_TEST_IMAGE}
  key_name: ${SENLIN_TEST_KEYPAIR}
  networks:
    - network: ${SENLIN_TEST_NETWORK}
EOF

profile_id=$(openstack cluster profile create -f value -c id --spec-file "$tmp_spec" "$SENLIN_TEST_NAME-profile")
cluster_id=$(openstack cluster create -f value -c id --profile "$profile_id" --min-size 1 --desired-capacity 1 --max-size 2 "$SENLIN_TEST_NAME")

for _ in $(seq 1 30); do
  status=$(openstack cluster show "$cluster_id" -f value -c status)
  [ "$status" = ACTIVE ] && break
  case "$status" in
    ERROR|CRITICAL|DELETED) echo "Senlin cluster entered unexpected status: $status" >&2; exit 1 ;;
  esac
  sleep 5
done
[ "$(openstack cluster show "$cluster_id" -f value -c status)" = ACTIVE ]
openstack cluster expand --count 1 --wait "$cluster_id" >/dev/null
openstack cluster show "$cluster_id" >/dev/null
echo "Senlin cluster lifecycle smoke test passed: $cluster_id"
