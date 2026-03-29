#!/bin/bash

output_file="/tmp/custom_metrics/network_interfaces_status.json"
interfaces=(vpn0 ceph_public openstack_mgmt ceph_cluster)
metric_lines=()

sanitize_metric_name() {
  local value="$1"
  value=$(echo "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//; s/_+/_/g')
  echo "$value"
}

add_metric() {
  metric_lines+=("\"$1\" : $2")
}

for interface in "${interfaces[@]}"; do
  metric_name=$(sanitize_metric_name "$interface")

  if ip -o link show dev "$interface" >/dev/null 2>&1; then
    add_metric "network_interface_${metric_name}_present" 1

    state=$(cat "/sys/class/net/${interface}/operstate" 2>/dev/null || echo "unknown")
    if [[ "$state" == "up" ]]; then
      add_metric "network_interface_${metric_name}_up" 1
    else
      add_metric "network_interface_${metric_name}_up" 0
    fi
  else
    add_metric "network_interface_${metric_name}_present" 0
    add_metric "network_interface_${metric_name}_up" 0
  fi
done

mkdir -p "$(dirname "$output_file")"

{
  echo "{"
  last_index=$(( ${#metric_lines[@]} - 1 ))
  for idx in "${!metric_lines[@]}"; do
    if [[ $idx -eq $last_index ]]; then
      echo "${metric_lines[$idx]}"
    else
      echo "${metric_lines[$idx]},"
    fi
  done
  echo "}"
} > "$output_file"
