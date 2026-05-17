#!/bin/bash
# =============================================================================
# DevOps Monitoring Stack — Start/Restart Script
# Run this after reboot or if any service is down
# =============================================================================

GRAFANA_PORT=30750
PROCESSOR_PORT=30751
LOKI_PORT=3100

ok()    { echo "  ✅ $1"; }
warn()  { echo "  ⚠️  $1"; }
wait_() { echo "  ⏳ $1"; }
log()   { echo "[$(date +%H:%M:%S)] $1"; }

echo ""
echo "══════════════════════════════════════════════════════"
echo "  DevOps Monitoring Stack — Start Script"
echo "══════════════════════════════════════════════════════"
echo ""

# =============================================================================
# STEP 1 — Fix Flask if missing (survives OS updates)
# =============================================================================
log "Checking Python dependencies..."
if ! python3 -c "import flask, boto3" 2>/dev/null; then
    warn "Flask/boto3 not found — reinstalling..."
    pip3 install flask boto3 \
        --break-system-packages \
        --ignore-installed blinker \
        --quiet 2>/dev/null || \
    pip install flask boto3 \
        --break-system-packages \
        --ignore-installed blinker \
        --quiet 2>/dev/null || true
    python3 -c "import flask, boto3" \
        && ok "Flask + boto3 installed" \
        || { warn "Failed to install flask — processor may not start"; }
else
    ok "Flask + boto3 already available"
fi

# =============================================================================
# STEP 2 — Start services in order
# =============================================================================
log "Starting services..."

# Loki first (Promtail and Processor depend on it)
if systemctl is-active loki &>/dev/null; then
    ok "Loki already running"
else
    log "Starting Loki..."
    systemctl start loki
    for i in $(seq 1 12); do
        H=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
            "http://localhost:${LOKI_PORT}/ready" 2>/dev/null || echo "000")
        [ "$H" = "200" ] && ok "Loki started" && break
        wait_ "Loki HTTP $H ($i/12)..." && sleep 5
    done
    [ "$H" != "200" ] && warn "Loki may not be ready — check: journalctl -u loki -n 20"
fi

# Promtail
if systemctl is-active promtail &>/dev/null; then
    ok "Promtail already running"
else
    log "Starting Promtail..."
    systemctl start promtail
    sleep 3
    systemctl is-active promtail &>/dev/null \
        && ok "Promtail started" \
        || warn "Promtail failed — check: journalctl -u promtail -n 20"
fi

# Grafana
if systemctl is-active grafana-server &>/dev/null; then
    ok "Grafana already running"
else
    log "Starting Grafana..."
    systemctl start grafana-server
    for i in $(seq 1 18); do
        H=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
            "http://localhost:${GRAFANA_PORT}/api/health" 2>/dev/null || echo "000")
        [ "$H" = "200" ] && ok "Grafana started" && break
        wait_ "Grafana HTTP $H ($i/18)..." && sleep 5
    done
    [ "$H" != "200" ] && warn "Grafana may not be ready — check: journalctl -u grafana-server -n 20"
fi

# Alert Processor
if systemctl is-active devops-alert-processor &>/dev/null; then
    ok "Alert Processor already running"
else
    log "Starting Alert Processor..."
    systemctl start devops-alert-processor
    for i in $(seq 1 18); do
        H=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
            "http://localhost:${PROCESSOR_PORT}/health" 2>/dev/null || echo "000")
        [ "$H" = "200" ] && ok "Alert Processor started" && break
        wait_ "Processor HTTP $H ($i/18)..." && sleep 5
    done
    [ "$H" != "200" ] && {
        warn "Processor not ready — showing last 10 log lines:"
        journalctl -u devops-alert-processor -n 10 --no-pager
    }
fi

# =============================================================================
# STEP 3 — Final status check
# =============================================================================
echo ""
echo "══════════════════════════════════════════════════════"
echo "  Service Status"
echo "══════════════════════════════════════════════════════"
for svc in grafana-server loki promtail devops-alert-processor; do
    STATUS=$(systemctl is-active $svc 2>/dev/null)
    [ "$STATUS" = "active" ] \
        && ok "$svc : active" \
        || warn "$svc : $STATUS"
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Endpoint Health"
echo "══════════════════════════════════════════════════════"

GF=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
    "http://localhost:${GRAFANA_PORT}/api/health" 2>/dev/null || echo "000")
LK=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
    "http://localhost:${LOKI_PORT}/ready" 2>/dev/null || echo "000")
PR=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
    "http://localhost:${PROCESSOR_PORT}/health" 2>/dev/null || echo "000")

[ "$GF" = "200" ] && ok "Grafana   : http://localhost:${GRAFANA_PORT}  (HTTP $GF)" || warn "Grafana   : HTTP $GF"
[ "$LK" = "200" ] && ok "Loki      : http://localhost:${LOKI_PORT}   (HTTP $LK)" || warn "Loki      : HTTP $LK"
[ "$PR" = "200" ] && ok "Processor : http://localhost:${PROCESSOR_PORT}  (HTTP $PR)" || warn "Processor : HTTP $PR"

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Quick Tests"
echo "══════════════════════════════════════════════════════"

if [ "$PR" = "200" ]; then
    APPS=$(curl -s "http://localhost:${PROCESSOR_PORT}/health" \
        | jq -r '.monitored_apps[].name' 2>/dev/null | tr '\n' ', ' | sed 's/,$//')
    ok "Monitored apps : $APPS"

    AGENT=$(curl -s "http://localhost:${PROCESSOR_PORT}/health" \
        | jq -r '.agent_webhook' 2>/dev/null)
    BOTO3=$(curl -s "http://localhost:${PROCESSOR_PORT}/health" \
        | jq -r '.boto3' 2>/dev/null)
    [ "$AGENT" = "true" ] && ok "DevOps Agent   : webhook configured" || warn "DevOps Agent   : not configured"
    [ "$BOTO3" = "true"  ] && ok "boto3 / SES    : available"          || warn "boto3 / SES    : not available"

    LOKI_LOGS=$(curl -s \
        "http://localhost:${PROCESSOR_PORT}/test-loki?ns=default&mins=60" \
        | jq -r '.error_logs_found' 2>/dev/null || echo "?")
    ok "Loki logs (60m): $LOKI_LOGS error(s) found in default ns"
else
    warn "Skipping quick tests — processor not ready"
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Useful Commands"
echo "══════════════════════════════════════════════════════"
echo ""
echo "  # Force restart all services"
echo "  systemctl restart loki promtail grafana-server devops-alert-processor"
echo ""
echo "  # View live logs"
echo "  journalctl -u devops-alert-processor -f"
echo "  tail -f /var/log/devops-processor.log"
echo ""
echo "  # Run tests manually"
echo "  curl -s http://localhost:${PROCESSOR_PORT}/test-snow  | jq ."
echo "  curl -s http://localhost:${PROCESSOR_PORT}/test-agent | jq ."
echo "  curl -s http://localhost:${PROCESSOR_PORT}/test-email | jq ."
echo ""
echo "  # Simulate critical error"
echo "  kubectl exec -n default deploy/minikube-app -- \\"
echo "    /bin/sh -c 'echo \"CRITICAL: NullPointerException\" >&2'"
echo ""
echo "══════════════════════════════════════════════════════"
