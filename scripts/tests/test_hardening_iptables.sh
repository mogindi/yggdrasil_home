#!/bin/bash

set -euo pipefail

test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT

cat > "$test_dir/iptables-save" <<'EOF'
#!/bin/bash
exit 1
EOF

cat > "$test_dir/iptables" <<'EOF'
#!/bin/bash
printf '[%s]\n' "$@" >> "$IPTABLES_TEST_LOG"
EOF

chmod +x "$test_dir/iptables-save" "$test_dir/iptables"

cat > "$test_dir/additional.rules" <<'EOF'
# The comment and spaces exercise line-preserving rule loading.
-A INPUT -p tcp --dport 9443 -m comment --comment 'allow example service' -j ACCEPT
EOF

export PATH="$test_dir:$PATH"
export IPTABLES_TEST_LOG="$test_dir/iptables.log"
export OPENSTACK_MGMT_NET=10.0.10.0/24
export CEPH_PUBLIC_NET=10.0.50.0/24
export CEPH_CLUSTER_NET=10.0.60.0/24
export NETWORK_PROVIDER_NETS=192.168.150.0/24
export DEPLOYMENT_NODE_IPS=10.0.10.11
export VPN_NET=10.84.0.0/24
export ADDITIONAL_IPTABLE_RULES_FILE="$test_dir/additional.rules"

"$(dirname "$0")/../hardening/iptables.sh" >/dev/null 2>&1

grep -F -q -- "[-A]" "$IPTABLES_TEST_LOG"
grep -F -q -- "[allow example service]" "$IPTABLES_TEST_LOG"
grep -F -q -- "[DROP]" "$IPTABLES_TEST_LOG"

echo "iptables additional rules test passed"
