#!/bin/bash

set -xe

cd custom_exporter

./docker_build.sh

docker ps | grep -q custom_exporter || ./docker_run.sh


service=custom-metrics-scripts
runner=/usr/local/bin/custom-metrics-runner.sh

cat > "$runner" << 'EOF'
#!/bin/bash
set -euo pipefail

while true; do
  pids=()

  shopt -s nullglob
  for script in /opt/custom_metrics/*; do
    [[ -f "$script" ]] || continue
    bash "$script" &
    pids+=("$!")
  done
  shopt -u nullglob

  for pid in "${pids[@]}"; do
    wait "$pid"
  done

  sleep 300
done
EOF

chmod +x "$runner"

cat > /etc/systemd/system/$service.service << EOF
[Unit]
After=docker.service

[Service]
ExecStartPre=mkdir -p /tmp/custom_metrics
ExecStart=$runner

[Install]
WantedBy=default.target
EOF

systemctl daemon-reload
systemctl restart $service
systemctl enable $service


exit 0
