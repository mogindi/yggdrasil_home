#!/usr/bin/env bash

set -euo pipefail

drop_commit=b3f19f8146b3a08c8b5383dbf3fd90a36975ae87
git cat-file -e "${drop_commit}^" 2>/dev/null || {
  echo "Kolla-Ansible Senlin parent commit is unavailable: ${drop_commit}" >&2
  exit 1
}

# The current branch reorganized group_vars and common templates after Senlin
# was removed, so restore the role and add the small current-layout shims here.
git archive "${drop_commit}^" \
  ansible/roles/senlin \
  ansible/roles/common/templates/cron-logrotate-senlin.conf.j2 | tar -x

cat > ansible/group_vars/all/senlin.yml <<'EOF'
enable_senlin: "no"
senlin_internal_fqdn: "{{ kolla_internal_fqdn }}"
senlin_external_fqdn: "{{ kolla_external_fqdn }}"
senlin_api_port: "8778"
senlin_api_listen_port: "{{ senlin_api_port }}"
senlin_api_public_port: "{{ haproxy_single_external_frontend_public_port if haproxy_single_external_frontend | bool else senlin_api_port }}"
EOF

sed -i '/^[[:space:]]*- enable_skyline_{{ enable_skyline | bool }}/i\        - enable_senlin_{{ enable_senlin | bool }}' ansible/site.yml

loadbalancer_block=$(mktemp)
cat >"$loadbalancer_block" <<'EOF'
        - include_role:
            name: senlin
            tasks_from: loadbalancer
          tags: senlin
          when: enable_senlin | bool
EOF
awk -v block_file="$loadbalancer_block" '
  BEGIN { while ((getline line < block_file) > 0) block = block line ORS }
  /^[[:space:]]+name: skyline$/ { printf "%s", block }
  { print }
' ansible/site.yml > ansible/site.yml.tmp
mv ansible/site.yml.tmp ansible/site.yml
rm -f "$loadbalancer_block"

role_block=$(mktemp)
cat >"$role_block" <<'EOF'
- name: Apply role senlin
  gather_facts: false
  hosts:
    - senlin-api
    - senlin-conductor
    - senlin-engine
    - senlin-health-manager
    - '&enable_senlin_True'
  serial: '{{ kolla_serial|default("0") }}'
  max_fail_percentage: >-
    {{ senlin_max_fail_percentage |
       default(kolla_max_fail_percentage) |
       default(100) }}
  roles:
    - { role: senlin,
        tags: senlin }

EOF
awk -v block_file="$role_block" '
  BEGIN { while ((getline line < block_file) > 0) block = block line ORS }
  /^- name: Apply role tacker$/ { printf "%s", block }
  { print }
' ansible/site.yml > ansible/site.yml.tmp
mv ansible/site.yml.tmp ansible/site.yml
rm -f "$role_block"

for inventory in ansible/inventory/all-in-one ansible/inventory/multinode; do
  cat >>"$inventory" <<'EOF'

# Senlin
[senlin:children]
control

[senlin-api:children]
senlin

[senlin-conductor:children]
senlin

[senlin-engine:children]
senlin

[senlin-health-manager:children]
senlin
EOF
done
