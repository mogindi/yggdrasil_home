#!/bin/bash

output_file="/tmp/custom_metrics/network_routes_status.json"
main_routes_service="/etc/systemd/system/network_create_routes.service"
route_table_services_dir="/etc/systemd/system"
route_table_service_prefix="network_create_route_table_"
route_table_service_suffix=".service"

metric_lines=()

sanitize_metric_name() {
  local value="$1"
  value=$(echo "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//; s/_+/_/g')
  echo "$value"
}

add_metric() {
  metric_lines+=("\"$1\" : $2")
}

extract_expected_routes() {
  local service_file="$1"
  local table="$2"

  awk -v table="$table" '
    {
      add = index($0, "ip route add ")
      suffix = " table " table
      if (add == 0) next

      route = substr($0, add + length("ip route add "))
      suffix_start = index(route, suffix)
      if (suffix_start == 0) next

      print substr(route, 1, suffix_start - 1)
    }
  ' "$service_file"
}

check_routes_for_table() {
  local table="$1"
  local service_file="$2"
  local metric_table
  metric_table=$(sanitize_metric_name "$table")

  if [[ ! -f "$service_file" ]]; then
    add_metric "network_routes_${metric_table}_configured" 0
    add_metric "network_routes_${metric_table}_healthy" -1
    add_metric "network_routes_${metric_table}_expected_routes" 0
    add_metric "network_routes_${metric_table}_present_routes" 0
    return
  fi

  local expected_routes
  expected_routes=$(extract_expected_routes "$service_file" "$table")
  local expected_count=0
  local present_count=0

  while IFS= read -r route; do
    [[ -z "$route" ]] && continue
    expected_count=$((expected_count + 1))

    if ip route show table "$table" | grep -Fq "$route"; then
      present_count=$((present_count + 1))
      add_metric "network_route_${metric_table}_$(sanitize_metric_name "$route")_present" 1
    else
      add_metric "network_route_${metric_table}_$(sanitize_metric_name "$route")_present" 0
    fi
  done <<< "$expected_routes"

  add_metric "network_routes_${metric_table}_configured" 1
  add_metric "network_routes_${metric_table}_expected_routes" "$expected_count"
  add_metric "network_routes_${metric_table}_present_routes" "$present_count"

  if [[ $expected_count -eq 0 ]]; then
    add_metric "network_routes_${metric_table}_healthy" -1
  elif [[ $expected_count -eq $present_count ]]; then
    add_metric "network_routes_${metric_table}_healthy" 1
  else
    add_metric "network_routes_${metric_table}_healthy" 0
  fi
}

check_routes_for_table "main" "$main_routes_service"

shopt -s nullglob
route_table_services=("${route_table_services_dir}/${route_table_service_prefix}"*"${route_table_service_suffix}")
shopt -u nullglob

for service_file in "${route_table_services[@]}"; do
  service_name=$(basename "$service_file")
  table="${service_name#${route_table_service_prefix}}"
  table="${table%${route_table_service_suffix}}"
  check_routes_for_table "$table" "$service_file"
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
