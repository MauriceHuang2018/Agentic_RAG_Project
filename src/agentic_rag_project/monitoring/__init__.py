"""Monitoring — Drift Alarm + Prometheus exporter (DESIGN 2.2 #14).

Celery beat task that compares current 7-day accuracy against baseline;
writes `drift_alerts` and alerts Prometheus Alertmanager when drop > 5%.
Implemented in T4.3 / T4.4.
"""