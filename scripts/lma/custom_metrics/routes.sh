#!/bin/bash

output_file="/tmp/custom_metrics/network_routes_status.json"
main_routes_service="/etc/systemd/system/network_create_routes.service"
vswitch_routes_service="/etc/systemd/system/network_create_route_table_vswitch.service"

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
  local table=$2
  cat $service_file | grep -o "ip route add.* table $table" | sed "s/ip route add //; s/ table $table//"
}

check_routes_for_table() {
  local table="$1"
  local service_file="$2"

  if [[ ! -f "$service_file" ]]; then
    add_metric "network_routes_${table}_configured" 0
    add_metric "network_routes_${table}_healthy" -1
    add_metric "network_routes_${table}_expected_routes" 0
    add_metric "network_routes_${table}_present_routes" 0
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
      add_metric "network_route_${table}_$(sanitize_metric_name "$route")_present" 1
    else
      add_metric "network_route_${table}_$(sanitize_metric_name "$route")_present" 0
    fi
  done <<< "$expected_routes"

  add_metric "network_routes_${table}_configured" 1
  add_metric "network_routes_${table}_expected_routes" "$expected_count"
  add_metric "network_routes_${table}_present_routes" "$present_count"

  if [[ $expected_count -eq 0 ]]; then
    add_metric "network_routes_${table}_healthy" -1
  elif [[ $expected_count -eq $present_count ]]; then
    add_metric "network_routes_${table}_healthy" 1
  else
    add_metric "network_routes_${table}_healthy" 0
  fi
}

check_routes_for_table "main" "$main_routes_service"
check_routes_for_table "vswitch" "$vswitch_routes_service"

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
