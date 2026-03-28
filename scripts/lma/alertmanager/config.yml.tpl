global:
  resolve_timeout: 5m

route:
  receiver: pagerduty
  group_by: ['alertname', 'cluster', 'service']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - receiver: pagerduty
      continue: false
      matchers:
        - severity=~"${PAGERDUTY_SEVERITY_MAP}"

receivers:
  - name: default

  - name: pagerduty
    pagerduty_configs:
      - service_key: '${PAGERDUTY_INTEGRATION_KEY}'
        severity: 'critical'
        send_resolved: true

