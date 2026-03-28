#!/bin/bash

set -xeuo pipefail


CONFIG_DIR=etc/kolla

set -xe

# source venv
cd workspace
source kolla-venv/bin/activate

PROM_CONFIG_DIR="$CONFIG_DIR/config/prometheus"
PROM_CONFIG_FILE="$PROM_CONFIG_DIR/prometheus-alertmanager.yml"
PROM_TEMPLATE_FILE="$(dirname "$0")/config.yml.tpl"

mkdir -p "$PROM_CONFIG_DIR"

if [[ ! -f "$PROM_TEMPLATE_FILE" ]]; then
  echo "Template file not found: $PROM_TEMPLATE_FILE" >&2
  exit 1
fi

PAGERDUTY_INTEGRATION_KEY="${PAGERDUTY_INTEGRATION_KEY:-}"
PAGERDUTY_SEVERITY_MAP="${PAGERDUTY_SEVERITY_MAP:-critical}"

export PAGERDUTY_INTEGRATION_KEY PAGERDUTY_SEVERITY_MAP
envsubst < "$PROM_TEMPLATE_FILE" > "$PROM_CONFIG_FILE"

echo "Generated $PROM_CONFIG_FILE"

realpath "$PROM_CONFIG_FILE"

if [[ -z "$PAGERDUTY_INTEGRATION_KEY" ]]; then
  echo "PAGERDUTY_INTEGRATION_KEY is empty. PagerDuty receiver is disabled in generated config."
  exit 1
fi
