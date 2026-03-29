#!/bin/bash

set -xe

cd custom_exporter

./docker_build.sh

docker ps | grep -q custom_exporter || ./docker_run.sh


service=custom-metrics-scripts
cat > /etc/systemd/system/$service.service << EOF
[Unit]
After=docker.service

[Service]
ExecStartPre=mkdir -p /tmp/custom_metrics
ExecStart=/bin/bash -c "while true; do ls -d /opt/custom_metrics/* | xargs -I% bash % & sleep 300; done"

[Install]
WantedBy=default.target
EOF

systemctl daemon-reload
systemctl restart $service
systemctl enable $service


exit 0
