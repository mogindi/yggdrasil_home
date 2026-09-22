#!/bin/bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT

template="$repo_root/ansible/templates/security_hardening.service.j2"

ANSIBLE_STDOUT_CALLBACK=default ansible localhost -c local \
  -m ansible.builtin.template \
  -a "src=$template dest=$test_dir/early.service" \
  -e '{"item":{"script":"auditd.sh","early":true}}' >/dev/null

ANSIBLE_STDOUT_CALLBACK=default ansible localhost -c local \
  -m ansible.builtin.template \
  -a "src=$template dest=$test_dir/late.service" \
  -e '{"item":{"script":"auditd.sh","after":"apt-daily.service apt-daily-upgrade.service"}}' >/dev/null

grep -Fqx 'Wants=network-pre.target' "$test_dir/early.service"
grep -Fqx 'Before=network-pre.target shutdown.target' "$test_dir/early.service"
grep -Fqx 'After=local-fs.target' "$test_dir/early.service"
grep -Fqx 'DefaultDependencies=no' "$test_dir/early.service"
grep -Fqx 'WantedBy=sysinit.target' "$test_dir/early.service"
! grep -Fq 'network-online.target' "$test_dir/early.service"

grep -Fqx 'Wants=network-online.target' "$test_dir/late.service"
grep -Fqx 'After=network-online.target apt-daily.service apt-daily-upgrade.service' "$test_dir/late.service"
grep -Fqx 'WantedBy=multi-user.target' "$test_dir/late.service"
! grep -Fq 'DefaultDependencies=no' "$test_dir/late.service"

echo "security hardening service ordering test passed"
