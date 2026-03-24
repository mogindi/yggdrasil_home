#!/bin/bash

set -xe

cat > workspace/etc/kolla/config/prometheus/prometheus.yml.d/custom.yml <<EOF
scrape_configs:
  - job_name: custom
    static_configs:
      - targets:
        - '$OPENSTACK_MGMT_IP:8079'
EOF