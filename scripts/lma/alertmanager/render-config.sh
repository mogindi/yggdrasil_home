#!/bin/bash

set -euo pipefail

CONFIG_DIR="workspace/etc/kolla/config/alertmanager"
CONFIG_FILE="$CONFIG_DIR/config.yml"
TEMPLATE_FILE="$(dirname "$0")/config.yml.tpl"

mkdir -p "$CONFIG_DIR"

if [[ ! -f "$TEMPLATE_FILE" ]]; then
  echo "Template file not found: $TEMPLATE_FILE" >&2
  exit 1
fi

PAGERDUTY_ROUTING_KEY="${PAGERDUTY_ROUTING_KEY:-}"
PAGERDUTY_SEVERITY_MAP="${PAGERDUTY_SEVERITY_MAP:-critical}"

export PAGERDUTY_ROUTING_KEY PAGERDUTY_SEVERITY_MAP
envsubst < "$TEMPLATE_FILE" > "$CONFIG_FILE"

echo "Generated $CONFIG_FILE"

if [[ -z "$PAGERDUTY_ROUTING_KEY" ]]; then
  echo "PAGERDUTY_ROUTING_KEY is empty. PagerDuty receiver is disabled in generated config."
fi
