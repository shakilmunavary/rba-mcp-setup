#!/bin/bash
# DevOps Monitoring Stack — Start/Status Script
# Run after reboot or if any service is down

GRAFANA_PORT=30750
PROCESSOR_PORT=30751
LOKI_PORT=3100
HOME_DIR="/home/rba/grafana"

ok()    { echo "  ✅ $1"; }
warn()  { echo "  ⚠️  $1"; }
wait_() { echo "  ⏳ $1"; }
log()   { echo "[$(date +%H:%M:%S)] $1"; }

echo ""
echo "══════════════════════════════════════════════════════"
echo "  DevOps Monitoring Stack — Start"
echo "══════════════════════════════════════════════════════"
echo ""

log "Checking Python dependencies..."
if ! python3 -c "import flask, boto3" 2>/dev/null; then
    warn "Flask/boto3 missing — reinstalling..."
    pip3 install flask boto3 --break-system-packages --ignore-installed blinker --quiet 2>/dev/null || true
    python3 -c "import flask, boto3" && ok "Flask + boto3 installed" || warn "Install failed"
else
    ok "Flask + boto3 available"
fi

for svc in loki promtail grafana-server devops-alert-processor; do
    if systemctl is-active $svc &>/dev/null; then
        ok "$svc already running"
    else
        log "Starting $svc..."
        systemctl start $svc
        sleep 3
        systemctl is-active $svc &>/dev/null && ok "$svc started" || warn "$svc failed to start"
    fi
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Status"
echo "══════════════════════════════════════════════════════"
for svc in grafana-server loki promtail devops-alert-processor; do
    STATUS=$(systemctl is-active $svc 2>/dev/null)
    [ "$STATUS" = "active" ] && ok "$svc" || warn "$svc: $STATUS"
done
echo ""
curl -s -o /dev/null -w "  Grafana   : HTTP %{http_code}\n" http://localhost:${GRAFANA_PORT}/api/health
curl -s -o /dev/null -w "  Loki      : HTTP %{http_code}\n" http://localhost:${LOKI_PORT}/ready
curl -s -o /dev/null -w "  Processor : HTTP %{http_code}\n" http://localhost:${PROCESSOR_PORT}/health
echo ""
echo "  Logs : ${HOME_DIR}/logs/"
echo "  curl -s http://localhost:${PROCESSOR_PORT}/health | jq ."
echo "══════════════════════════════════════════════════════"
