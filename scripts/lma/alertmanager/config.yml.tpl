global:
  resolve_timeout: 5m

route:
  receiver: default
  group_by: ['alertname', 'cluster', 'service', 'severity']
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
        # PagerDuty accepts critical, error, warning, and info. The OpenStack
        # rules currently use P3/P4/P5 in the severity label, so translate
        # those values at the notification boundary without changing rules.
        severity: '{{ if or (eq .CommonLabels.severity "critical") (eq .CommonLabels.severity "P1") (eq .CommonLabels.severity "P2") }}critical{{ else if or (eq .CommonLabels.severity "error") (eq .CommonLabels.severity "P3") }}error{{ else if or (eq .CommonLabels.severity "warning") (eq .CommonLabels.severity "P4") }}warning{{ else }}info{{ end }}'
        send_resolved: true
