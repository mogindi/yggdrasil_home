global:
  resolve_timeout: 5m

route:
  receiver: default
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
      - routing_key: '${PAGERDUTY_ROUTING_KEY}'
        severity: '{{ if .CommonLabels.severity }}{{ .CommonLabels.severity }}{{ else }}critical{{ end }}'
        send_resolved: true
        description: '{{ .CommonAnnotations.summary }}'
        details:
          firing: '{{ .Alerts.Firing | len }}'
          resolved: '{{ .Alerts.Resolved | len }}'
          alertname: '{{ .CommonLabels.alertname }}'
          cluster: '{{ .CommonLabels.cluster }}'
          service: '{{ .CommonLabels.service }}'
          instance: '{{ .CommonLabels.instance }}'

inhibit_rules:
  - source_matchers: ['severity="critical"']
    target_matchers: ['severity="warning"']
    equal: ['alertname', 'cluster', 'service', 'instance']
