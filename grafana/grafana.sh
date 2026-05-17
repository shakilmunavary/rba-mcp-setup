#!/bin/bash
# =============================================================================
# Minikube Monitoring Stack — NON-DOCKERIZED v1.2
# Grafana + Loki + Promtail + Alert Processor as native Linux systemd services
#
# FIXES in v1.2:
#   - Flask installed with --ignore-installed blinker (Ubuntu 24 conflict)
#   - Routing policy uses X-Disable-Provenance: true (Grafana 13)
#   - Dual-layer dedup: in-memory lock + SNOW check (no duplicate tickets)
#   - HOME_DIR variable — all files kept in /home/rba/grafana
#   - AGENT_SPACE_URL variable for email link
# =============================================================================

# =============================================================================
# SECTION A — DIRECTORIES
# =============================================================================
HOME_DIR="/home/rba/grafana"
INSTALL_DIR="${HOME_DIR}/monitoring"

# =============================================================================
# SECTION B — APP PROFILES
# =============================================================================
APP_1_ALERT_NAME="Java User App - Minikube"
APP_1_NAMESPACE="default"
APP_1_PODS_MONITORED="minikube-app-*"
APP_1_JENKINS_JOB="https://sop-testing-alb-2059918749.us-west-2.elb.amazonaws.com/job/minikube-app/"
APP_1_JENKINS_API="https://sop-testing-alb-2059918749.us-west-2.elb.amazonaws.com/jenkins/job/minikube-app/api/json"
APP_1_JENKINS_PAT="11b841b612a88f1f42ccd62dacb9c37394"
APP_1_GITHUB_REPO="https://github.com/shakilmunavary/minikube-app"
APP_1_GITHUB_TOKEN="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"
APP_1_SRE_EMAIL="shakil.munavary@cognizant.com"

APP_2_ALERT_NAME=""; APP_2_NAMESPACE=""; APP_2_PODS_MONITORED=""
APP_2_JENKINS_JOB=""; APP_2_JENKINS_API=""; APP_2_JENKINS_PAT=""
APP_2_GITHUB_REPO=""; APP_2_GITHUB_TOKEN=""; APP_2_SRE_EMAIL=""

APP_3_ALERT_NAME=""; APP_3_NAMESPACE=""; APP_3_PODS_MONITORED=""
APP_3_JENKINS_JOB=""; APP_3_JENKINS_API=""; APP_3_JENKINS_PAT=""
APP_3_GITHUB_REPO=""; APP_3_GITHUB_TOKEN=""; APP_3_SRE_EMAIL=""

# =============================================================================
# SECTION C — AWS DEVOPS AGENT
# =============================================================================
DEVOPS_AGENT_WEBHOOK_URL="https://event-ai.us-west-2.api.aws/webhook/generic/37e6e8a9-2900-46b8-a550-f20d0412421b"
DEVOPS_AGENT_HMAC_SECRET="tuE8xr0Z2pXC5hbaKrYtliZcLpBVqrcCkiCVA09Mx9k="
DEVOPS_AGENT_SPACE_ID="37e6e8a9-2900-46b8-a550-f20d0412421b"
DEVOPS_AGENT_SPACE_URL="https://b782c8c3-cbd5-4149-afbb-12e910b18d33.aidevops.global.app.aws/dashboard"

# =============================================================================
# SECTION D — EMAIL
# =============================================================================
SRE_EMAIL="shakil.munavary@cognizant.com"
SES_SENDER_EMAIL="shakil.munavary@gmail.com"

# =============================================================================
# SECTION E — SERVICENOW
# =============================================================================
SNOW_INSTANCE="dev375632"
SNOW_USER="admin"
SNOW_PASS='kNy2EbSz+@S3'

# =============================================================================
# SECTION F — STACK CONFIG
# =============================================================================
AWS_REGION="us-west-2"
AWS_ACCOUNT_ID="727646490021"
GRAFANA_PORT=30750
PROCESSOR_PORT=30751
LOKI_PORT=3100
ALERT_FOR_SECONDS=120
LOKI_VERSION="3.4.2"
PROMTAIL_VERSION="3.4.2"

# =============================================================================

LOG_FILE="${HOME_DIR}/setup-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$HOME_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

set -e
trap 'echo ""; echo "[FAILED] at line $LINENO — see $LOG_FILE"; exit 1' ERR

log()   { echo "[$(date +%H:%M:%S)] $1"; }
ok()    { echo "  ✅ $1"; }
warn()  { echo "  ⚠️  $1"; }
wait_() { echo "  ⏳ $1"; }
step()  {
    echo ""
    echo "══════════════════════════════════════════════════════"
    echo "  $1"
    echo "══════════════════════════════════════════════════════"
    echo ""
}

ACTIVE_APPS=()
for i in 1 2 3; do
    ns_var="APP_${i}_NAMESPACE"
    [[ -n "${!ns_var}" ]] && ACTIVE_APPS+=($i)
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Monitoring Stack — Non-Dockerized v1.2"
echo "══════════════════════════════════════════════════════"
echo "  Home Dir     : $HOME_DIR"
echo "  Install Dir  : $INSTALL_DIR"
echo "  Active Apps  : ${#ACTIVE_APPS[@]}"
echo "  Grafana      : port $GRAFANA_PORT"
echo "  Loki         : port $LOKI_PORT"
echo "  Processor    : port $PROCESSOR_PORT"
echo "  Agent URL    : $DEVOPS_AGENT_SPACE_URL"
echo "  Log          : $LOG_FILE"
echo ""
echo "Starting in 5 seconds — Ctrl+C to cancel..."
sleep 5

# =============================================================================
# PHASE 1 — PREREQUISITES
# =============================================================================
step "PHASE 1 — Prerequisites"

command -v curl  &>/dev/null || apt-get install -y curl
command -v jq    &>/dev/null || apt-get install -y jq
command -v unzip &>/dev/null || apt-get install -y unzip

log "Installing Python packages (flask + boto3)..."
pip3 install flask boto3 \
    --break-system-packages \
    --ignore-installed blinker \
    --quiet 2>/dev/null || \
pip install flask boto3 \
    --break-system-packages \
    --ignore-installed blinker \
    --quiet 2>/dev/null || true

python3 -c "import flask, boto3" \
    && ok "Python packages: flask + boto3" \
    || { echo "[ERROR] flask/boto3 not importable — cannot continue"; exit 1; }

mkdir -p $INSTALL_DIR/{loki,promtail,processor,data}
ok "Directories created under $HOME_DIR"

# =============================================================================
# PHASE 2 — CLEANUP
# =============================================================================
step "PHASE 2 — Stop + Remove Previous Installation"

for svc in grafana-server loki promtail devops-alert-processor; do
    systemctl stop    $svc 2>/dev/null || true
    systemctl disable $svc 2>/dev/null || true
done
sleep 3
ok "Previous services stopped"

# =============================================================================
# PHASE 3 — INSTALL GRAFANA
# =============================================================================
step "PHASE 3 — Install Grafana"

if command -v grafana-server &>/dev/null; then
    ok "Grafana already installed: $(grafana-server -v 2>/dev/null | head -1)"
else
    log "Installing Grafana..."
    apt-get install -y apt-transport-https software-properties-common wget 2>/dev/null || true
    wget -q -O - https://apt.grafana.com/gpg.key \
        | gpg --dearmor > /usr/share/keyrings/grafana.key 2>/dev/null || true
    echo "deb [signed-by=/usr/share/keyrings/grafana.key] https://apt.grafana.com stable main" \
        > /etc/apt/sources.list.d/grafana.list
    apt-get update -qq 2>/dev/null || true
    apt-get install -y grafana 2>/dev/null || true
fi

cat > /etc/grafana/grafana.ini << GRAFANA_INI
[server]
http_port = ${GRAFANA_PORT}
root_url = https://devopsatkmcptools.com/grafana/
serve_from_sub_path = true

[security]
admin_user = admin
admin_password = admin123

[unified_alerting]
enabled = true

[alerting]
enabled = false

[log]
level = warn
GRAFANA_INI

systemctl daemon-reload
systemctl enable grafana-server
systemctl restart grafana-server
sleep 5

GRAFANA_HTTP="000"
for i in $(seq 1 18); do
    GRAFANA_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
        "http://localhost:${GRAFANA_PORT}/api/health" 2>/dev/null || echo "000")
    [ "$GRAFANA_HTTP" = "200" ] && ok "Grafana running on port $GRAFANA_PORT" && break
    wait_ "Grafana HTTP $GRAFANA_HTTP ($i/18)..." && sleep 10
done
[ "$GRAFANA_HTTP" != "200" ] && warn "Grafana not responding — check: journalctl -u grafana-server -n 50"

# =============================================================================
# PHASE 4 — INSTALL LOKI
# =============================================================================
step "PHASE 4 — Install Loki"

LOKI_BIN="$INSTALL_DIR/loki/loki"
if [ ! -f "$LOKI_BIN" ]; then
    log "Downloading Loki $LOKI_VERSION..."
    ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
    curl -sL "https://github.com/grafana/loki/releases/download/v${LOKI_VERSION}/loki-linux-${ARCH}.zip" \
        -o /tmp/loki.zip
    unzip -o /tmp/loki.zip -d $INSTALL_DIR/loki/
    mv $INSTALL_DIR/loki/loki-linux-${ARCH} $LOKI_BIN 2>/dev/null || true
    chmod +x $LOKI_BIN
    rm -f /tmp/loki.zip
fi
ok "Loki binary: $LOKI_BIN"

mkdir -p $INSTALL_DIR/data/loki/{chunks,rules}

cat > $INSTALL_DIR/loki/loki-config.yaml << LOKI_CONFIG
auth_enabled: false

server:
  http_listen_port: ${LOKI_PORT}
  grpc_listen_port: 9096
  log_level: warn

common:
  instance_addr: 127.0.0.1
  path_prefix: ${INSTALL_DIR}/data/loki
  storage:
    filesystem:
      chunks_directory: ${INSTALL_DIR}/data/loki/chunks
      rules_directory:  ${INSTALL_DIR}/data/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2020-10-24
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  allow_structured_metadata: false

ruler:
  alertmanager_url: http://localhost:9093
LOKI_CONFIG

cat > /etc/systemd/system/loki.service << LOKI_SVC
[Unit]
Description=Loki Log Aggregation
After=network.target

[Service]
Type=simple
User=root
ExecStart=${LOKI_BIN} -config.file=${INSTALL_DIR}/loki/loki-config.yaml
Restart=always
RestartSec=10
StandardOutput=append:${HOME_DIR}/logs/loki.log
StandardError=append:${HOME_DIR}/logs/loki.log

[Install]
WantedBy=multi-user.target
LOKI_SVC

mkdir -p ${HOME_DIR}/logs
systemctl daemon-reload
systemctl enable loki
systemctl restart loki
sleep 5

LOKI_HTTP="000"
for i in $(seq 1 12); do
    LOKI_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
        "http://localhost:${LOKI_PORT}/ready" 2>/dev/null || echo "000")
    [ "$LOKI_HTTP" = "200" ] && ok "Loki running on port $LOKI_PORT" && break
    wait_ "Loki HTTP $LOKI_HTTP ($i/12)..." && sleep 10
done
[ "$LOKI_HTTP" != "200" ] && warn "Loki not ready — check: journalctl -u loki -n 50"

# =============================================================================
# PHASE 5 — INSTALL PROMTAIL
# =============================================================================
step "PHASE 5 — Install Promtail"

PROMTAIL_BIN="$INSTALL_DIR/promtail/promtail"
if [ ! -f "$PROMTAIL_BIN" ]; then
    log "Downloading Promtail $PROMTAIL_VERSION..."
    ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
    curl -sL "https://github.com/grafana/loki/releases/download/v${PROMTAIL_VERSION}/promtail-linux-${ARCH}.zip" \
        -o /tmp/promtail.zip
    unzip -o /tmp/promtail.zip -d $INSTALL_DIR/promtail/
    mv $INSTALL_DIR/promtail/promtail-linux-${ARCH} $PROMTAIL_BIN 2>/dev/null || true
    chmod +x $PROMTAIL_BIN
    rm -f /tmp/promtail.zip
fi
ok "Promtail binary: $PROMTAIL_BIN"

SCRAPE_CONFIGS=""
for i in "${ACTIVE_APPS[@]}"; do
    app_ns="$(eval echo \$APP_${i}_NAMESPACE)"
    app_name="$(eval echo \$APP_${i}_ALERT_NAME)"
    SCRAPE_CONFIGS="${SCRAPE_CONFIGS}
  - job_name: ${app_ns}-pods
    static_configs:
      - targets:
          - localhost
        labels:
          job:       kubernetes-pods
          namespace: ${app_ns}
          app:       ${app_name}
          __path__:  /var/log/pods/${app_ns}_*/*/*.log
    pipeline_stages:
      - cri: {}
      - json:
          expressions:
            log:    log
            stream: stream
      - labels:
          stream:
      - output:
          source: log
"
done

SCRAPE_CONFIGS="${SCRAPE_CONFIGS}
  - job_name: all-pods-fallback
    static_configs:
      - targets:
          - localhost
        labels:
          job:      kubernetes-pods-all
          __path__: /var/log/pods/*/*/*.log
    pipeline_stages:
      - cri: {}
      - regex:
          expression: '/var/log/pods/(?P<namespace>[^_]+)_(?P<pod>[^_]+)_[^/]+/[^/]+/[0-9]+\.log'
          source: filename
      - labels:
          namespace:
          pod:
"

cat > $INSTALL_DIR/promtail/promtail-config.yaml << PROMTAIL_CONFIG
server:
  http_listen_port: 9080
  grpc_listen_port: 0
  log_level: warn

positions:
  filename: ${INSTALL_DIR}/data/promtail-positions.yaml

clients:
  - url: http://localhost:${LOKI_PORT}/loki/api/v1/push

scrape_configs:
${SCRAPE_CONFIGS}
PROMTAIL_CONFIG

cat > /etc/systemd/system/promtail.service << PROMTAIL_SVC
[Unit]
Description=Promtail Log Shipper
After=network.target loki.service

[Service]
Type=simple
User=root
ExecStart=${PROMTAIL_BIN} -config.file=${INSTALL_DIR}/promtail/promtail-config.yaml
Restart=always
RestartSec=10
StandardOutput=append:${HOME_DIR}/logs/promtail.log
StandardError=append:${HOME_DIR}/logs/promtail.log

[Install]
WantedBy=multi-user.target
PROMTAIL_SVC

systemctl daemon-reload
systemctl enable promtail
systemctl restart promtail
sleep 3
ok "Promtail running (scraping /var/log/pods/)"

# =============================================================================
# PHASE 6 — ALERT PROCESSOR
# =============================================================================
step "PHASE 6 — Alert Processor (Flask + dedup fix)"

APP_PROFILES_JSON="["
FIRST=true
for i in "${ACTIVE_APPS[@]}"; do
    [ "$FIRST" = false ] && APP_PROFILES_JSON="${APP_PROFILES_JSON},"
    FIRST=false
    APP_PROFILES_JSON="${APP_PROFILES_JSON}
    {
        \"alert_name\":    \"$(eval echo \$APP_${i}_ALERT_NAME)\",
        \"namespace\":     \"$(eval echo \$APP_${i}_NAMESPACE)\",
        \"pods_monitored\":\"$(eval echo \$APP_${i}_PODS_MONITORED)\",
        \"jenkins_job\":   \"$(eval echo \$APP_${i}_JENKINS_JOB)\",
        \"jenkins_api\":   \"$(eval echo \$APP_${i}_JENKINS_API)\",
        \"jenkins_pat\":   \"$(eval echo \$APP_${i}_JENKINS_PAT)\",
        \"github_repo\":   \"$(eval echo \$APP_${i}_GITHUB_REPO)\",
        \"github_token\":  \"$(eval echo \$APP_${i}_GITHUB_TOKEN)\",
        \"sre_email\":     \"$(eval echo \$APP_${i}_SRE_EMAIL)\"
    }"
done
APP_PROFILES_JSON="${APP_PROFILES_JSON}]"
echo "$APP_PROFILES_JSON" > $INSTALL_DIR/processor/profiles.json
ok "App profiles: $INSTALL_DIR/processor/profiles.json"

cat > $INSTALL_DIR/processor/app.py << 'PYEOF'
from flask import Flask, request, jsonify
import json, urllib.request, urllib.parse, ssl, base64, os, re, hashlib, hmac, time, threading
from datetime import datetime, timedelta

app = Flask(__name__)

LOKI_URL             = os.environ.get('LOKI_URL',             'http://localhost:3100')
SNOW_URL             = os.environ.get('SNOW_URL',             '')
SNOW_USER            = os.environ.get('SNOW_USER',            'admin')
SNOW_PASS            = os.environ.get('SNOW_PASS',            '')
SNOW_INSTANCE        = os.environ.get('SNOW_INSTANCE',        'dev375632')
AWS_REGION           = os.environ.get('AWS_REGION',           'us-west-2')
CLUSTER_NAME         = os.environ.get('CLUSTER_NAME',         'minikube')
DEVOPS_AGENT_WEBHOOK = os.environ.get('DEVOPS_AGENT_WEBHOOK_URL', '')
DEVOPS_AGENT_HMAC    = os.environ.get('DEVOPS_AGENT_HMAC_SECRET',  '')
DEVOPS_AGENT_SPACE_URL = os.environ.get('DEVOPS_AGENT_SPACE_URL',  '')
DEFAULT_SRE_EMAIL    = os.environ.get('SRE_EMAIL',            '')
SES_SENDER           = os.environ.get('SES_SENDER_EMAIL',     '')
PROFILES_FILE        = os.environ.get('PROFILES_FILE',        '/home/rba/grafana/monitoring/processor/profiles.json')

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode    = ssl.CERT_NONE

# ── Dual-layer dedup ──────────────────────────────────────────────────────────
# Layer 1: in-memory lock — blocks concurrent duplicate webhooks (race condition fix)
# Layer 2: ServiceNow check — blocks duplicates across restarts / longer gaps
_dedup_lock  = threading.Lock()
_recent_keys = {}        # { "ns:hash": epoch_time }
DEDUP_WINDOW = 300       # 5 minutes

def _is_duplicate_in_memory(key):
    with _dedup_lock:
        last = _recent_keys.get(key)
        if last and (time.time() - last) < DEDUP_WINDOW:
            return True
        _recent_keys[key] = time.time()
        return False

def _do(url, method='GET', data=None, headers=None, auth=None):
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items(): req.add_header(k, v)
    if auth:
        cred = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header('Authorization', 'Basic ' + cred)
    if data:
        req.data = json.dumps(data).encode()
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as r:
            body = r.read().decode(errors='replace')
            return (json.loads(body) if body else {}), r.status
    except urllib.error.HTTPError as e:
        return {'error': str(e), 'body': e.read().decode(errors='replace')}, e.code
    except Exception as e:
        return {'error': str(e)}, 500

def load_profiles():
    try:
        with open(PROFILES_FILE) as f: return json.load(f)
    except: return []

def get_profile(ns):
    for p in load_profiles():
        if p.get('namespace') == ns: return p
    return {}

ERROR_PATS   = [r'\bERROR\b', r'\bFATAL\b', r'\bCRITICAL\b',
                r'HTTP[/ ][45]\d{2}', r'\b(500|501|502|503|504)\b',
                r'[A-Za-z]+Exception', r'[A-Za-z]+Error:',
                r'Traceback \(most recent', r'java\.lang\.',
                r'NullPointerException', r'OutOfMemoryError']
EXCLUDE_PATS = [r'\bDEBUG\b', r'\bWARN\b', r'\bWARNING\b', r'\bINFO\b', r'\bTRACE\b']

def _is_error(line):
    for p in EXCLUDE_PATS:
        if re.search(p, line, re.IGNORECASE): return False
    return any(re.search(p, line) for p in ERROR_PATS)

def query_loki(ns, mins=10):
    try:
        end   = datetime.utcnow()
        start = end - timedelta(minutes=mins)
        q = ('{namespace="' + ns + '"} '
             '|~ "(?i)(ERROR|FATAL|CRITICAL|Exception|Error:|500|501|502|503|504)" '
             '!~ "(?i)(\\\\bDEBUG\\\\b|\\\\bINFO\\\\b|\\\\bWARN\\\\b|\\\\bTRACE\\\\b)"')
        params = {'query': q,
                  'start': str(int(start.timestamp() * 1e9)),
                  'end':   str(int(end.timestamp()   * 1e9)),
                  'limit': '100'}
        url = LOKI_URL + '/loki/api/v1/query_range?' + urllib.parse.urlencode(params)
        r, s = _do(url, headers={'Accept': 'application/json'})
        logs = []; pods = set()
        if s == 200 and r.get('data', {}).get('result'):
            for stream in r['data']['result']:
                if 'pod' in stream.get('stream', {}): pods.add(stream['stream']['pod'])
                for v in stream.get('values', []):
                    ts = datetime.fromtimestamp(int(v[0]) / 1e9).strftime('%Y-%m-%d %H:%M:%S')
                    if _is_error(v[1]): logs.append('[' + ts + '] ' + v[1])
        return logs[:80], list(pods)
    except Exception as e:
        return ['Loki query error: ' + str(e)], []

def _sig(logs):
    parts = []
    for l in logs:
        m = re.search(r'([A-Za-z]+Exception|[A-Za-z]+Error):', l)
        if m: parts.append(m.group(1))
        if re.search(r'\b(500|502|503|504)\b', l): parts.append('HTTP_5XX')
        if re.search(r'\b(401|403)\b', l):          parts.append('HTTP_AUTH')
        if 'Connection refused' in l:                parts.append('CONN_REFUSED')
        if re.search(r'[Tt]imeout', l):             parts.append('TIMEOUT')
        if 'OutOfMemory' in l:                       parts.append('OOM')
        if 'NullPointerException' in l:             parts.append('NPE')
    return '_'.join(sorted(set(parts))[:4]) if parts else 'GENERIC_ERROR'

def _hash(ns, sig):
    return hashlib.md5(f"{ns}:{sig}".encode()).hexdigest()[:8].upper()

def _etype(sig):
    if 'HTTP_5XX'     in sig: return 'HTTP 5xx Server Error'
    if 'HTTP_AUTH'    in sig: return 'HTTP Auth Error'
    if 'CONN_REFUSED' in sig: return 'Connection Refused'
    if 'TIMEOUT'      in sig: return 'Request Timeout'
    if 'OOM'          in sig: return 'Out Of Memory'
    if 'NPE'          in sig: return 'NullPointerException'
    return 'Application Error'

def get_jenkins(profile):
    japi = profile.get('jenkins_api', '')
    jpat = profile.get('jenkins_pat', '')
    det  = {'job_url': profile.get('jenkins_job',''), 'build_number': 'N/A',
            'build_status': 'unknown', 'git_branch': 'N/A', 'git_commit': 'N/A',
            'console_url': profile.get('jenkins_job','')}
    if not japi: return det
    try:
        hdrs = {'Accept': 'application/json'}
        if jpat: hdrs['Authorization'] = 'Basic ' + base64.b64encode(('admin:'+jpat).encode()).decode()
        req = urllib.request.Request(japi)
        for k, v in hdrs.items(): req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as r:
            data = json.loads(r.read().decode())
        last = data.get('lastBuild', {})
        det['build_number'] = str(last.get('number', 'N/A'))
        burl = last.get('url', '')
        if burl:
            req2 = urllib.request.Request(burl + 'api/json')
            for k, v in hdrs.items(): req2.add_header(k, v)
            with urllib.request.urlopen(req2, timeout=30, context=ssl_ctx) as r2:
                b = json.loads(r2.read().decode())
            det['build_status'] = b.get('result', 'IN_PROGRESS')
            det['console_url']  = burl + 'console'
            for action in b.get('actions', []):
                if 'lastBuiltRevision' in action:
                    det['git_commit'] = action['lastBuiltRevision'].get('SHA1','')[:10]
                    br = action['lastBuiltRevision'].get('branch', [{}])
                    if br: det['git_branch'] = br[0].get('name', 'N/A')
    except Exception as e:
        det['error'] = str(e)
    return det

def _snow_h(): return {'Accept':'application/json','Content-Type':'application/json'}

def _check_existing(ns, sig):
    h   = _hash(ns, sig)
    q   = 'short_descriptionLIKE' + h + '^stateIN1,2,3^ORDERBYDESCsys_created_on'
    url = SNOW_URL + '?sysparm_query=' + urllib.parse.quote(q) + '&sysparm_limit=1'
    r, s = _do(url, headers=_snow_h(), auth=(SNOW_USER, SNOW_PASS))
    if s == 200 and r.get('result'):
        return r['result'][0].get('number'), r['result'][0].get('sys_id')
    return None, None

def _create_ticket(title, desc, notes):
    data = {'short_description': title[:160], 'description': desc, 'work_notes': notes,
            'caller_id': 'admin', 'urgency': '2', 'impact': '2',
            'category': 'software', 'subcategory': 'application', 'state': '1'}
    r, s = _do(SNOW_URL, method='POST', data=data,
               headers=_snow_h(), auth=(SNOW_USER, SNOW_PASS))
    if s in (200, 201):
        return r.get('result', {}).get('number'), r.get('result', {}).get('sys_id')
    print(f'[SNOW] Create failed: HTTP {s}')
    return None, None

def _update_ticket(sys_id, notes):
    _do(SNOW_URL + '/' + sys_id, method='PATCH',
        data={'work_notes': notes}, headers=_snow_h(), auth=(SNOW_USER, SNOW_PASS))

def trigger_agent(inc_number, inc_sys_id, title, logs, pods, ns, etype, jdet, profile):
    if not DEVOPS_AGENT_WEBHOOK: return False, 'not configured'
    timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
    payload   = {
        'eventType': 'incident', 'incidentId': inc_number, 'action': 'created',
        'priority': 'HIGH', 'title': title, 'timestamp': timestamp,
        'service': CLUSTER_NAME + '/' + ns,
        'description': (f'App: {profile.get("alert_name","N/A")}\nError: {etype}\n'
                        f'Cluster: {CLUSTER_NAME}\nNamespace: {ns}\nPods: {", ".join(pods)}\n'
                        f'Jenkins: #{jdet.get("build_number","N/A")} ({jdet.get("build_status","N/A")})\n'
                        f'SNOW: https://{SNOW_INSTANCE}.service-now.com/incident.do?sysparm_query=number={inc_number}'),
        'data': {
            'incidentSysId': inc_sys_id, 'alertName': profile.get('alert_name',''),
            'clusterName': CLUSTER_NAME, 'namespace': ns, 'awsRegion': AWS_REGION,
            'affectedPods': pods, 'errorSampleCount': len(logs), 'errorSample': logs[:20],
            'jenkinsJob': profile.get('jenkins_job',''),
            'jenkinsBuildNum': jdet.get('build_number',''),
            'jenkinsBuildStatus': jdet.get('build_status',''),
            'jenkinsConsoleUrl': jdet.get('console_url',''),
            'githubRepo': profile.get('github_repo',''),
            'serviceNowUrl': f'https://{SNOW_INSTANCE}.service-now.com/incident.do?sysparm_query=number={inc_number}'
        }
    }
    payload_str = json.dumps(payload)
    message     = (timestamp + ':' + payload_str).encode('utf-8')
    signature   = base64.b64encode(
        hmac.new(DEVOPS_AGENT_HMAC.encode('utf-8'), message, 'sha256').digest()
    ).decode('utf-8')
    req = urllib.request.Request(DEVOPS_AGENT_WEBHOOK, method='POST')
    req.add_header('Content-Type',            'application/json')
    req.add_header('x-amzn-event-timestamp',  timestamp)
    req.add_header('x-amzn-event-signature',  signature)
    req.data = payload_str.encode('utf-8')
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as r:
            body = r.read().decode()
            result = json.loads(body) if body else {}
            return True, result.get('investigationId', inc_number)
    except urllib.error.HTTPError as e:
        return False, 'HTTP ' + str(e.code) + ': ' + e.read().decode()
    except Exception as e:
        return False, str(e)

def get_rca_from_snow(inc_number, wait_secs=90):
    print(f'[RCA] Waiting {wait_secs}s for Agent RCA on {inc_number}...')
    time.sleep(wait_secs)
    try:
        r, s = _do(SNOW_URL + '?sysparm_query=number=' + inc_number +
                   '&sysparm_limit=1&sysparm_fields=sys_id',
                   headers=_snow_h(), auth=(SNOW_USER, SNOW_PASS))
        if s == 200 and r.get('result'):
            sys_id = r['result'][0].get('sys_id', '')
            base   = SNOW_URL.rsplit('/incident', 1)[0]
            nr, ns2 = _do(base + '/sys_journal_field?sysparm_query=element_id=' + sys_id +
                          '^ORDERBYDESCsys_created_on&sysparm_limit=5&sysparm_fields=value',
                          headers=_snow_h(), auth=(SNOW_USER, SNOW_PASS))
            notes = [n.get('value','') for n in nr.get('result',[])
                     if len(n.get('value','')) > 100] if ns2 == 200 else []
            result = '\n\n'.join(notes[:2])
            print(f'[RCA] Retrieved {len(result)} chars')
            return result
    except Exception as e:
        print(f'[RCA] Error: {e}')
    return ''

def send_email(inc_number, title, ns, pods, logs, inv_id, jdet, profile, rca_content=''):
    sre = profile.get('sre_email','') or DEFAULT_SRE_EMAIL
    if not sre or not SES_SENDER:
        print(f'[EMAIL] Skipped — sre={sre}')
        return False, 'not configured'
    try:
        import boto3
        ses = boto3.client('ses', region_name=AWS_REGION)
    except Exception as e:
        return False, f'boto3: {e}'

    snow_link  = f'https://{SNOW_INSTANCE}.service-now.com/incident.do?sysparm_query=number={inc_number}'
    agent_link = DEVOPS_AGENT_SPACE_URL
    subject    = f'[ACTION REQUIRED] RCA Ready: {profile.get("alert_name", ns)} | {inc_number}'
    jcolor     = 'red' if jdet.get('build_status') in ('FAILURE','UNSTABLE') else 'green'

    rca_html = (
        f'<div style="background:#f0f7ff;border-left:4px solid #1565c0;padding:16px;'
        f'margin:16px 0;border-radius:4px"><h3 style="color:#1565c0;margin-top:0">'
        f'🤖 AWS DevOps Agent — Root Cause Analysis</h3>'
        f'<div style="font-size:13px;line-height:1.8">'
        f'{rca_content.replace(chr(10),"<br>")}</div></div>'
        if rca_content else
        '<div style="background:#fff8e1;border-left:4px solid #f9a825;padding:16px;'
        'margin:16px 0;border-radius:4px"><p style="margin:0;color:#666">⏳ Agent '
        'investigation in progress. Click <b>View Agent Investigation</b> for live RCA.</p></div>'
    )

    body_html = f'''<html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;padding:20px;background:#f5f5f5">
<div style="background:white;border-radius:8px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,0.1)">

<div style="background:#c0392b;color:white;padding:16px;border-radius:6px;margin-bottom:20px">
<h2 style="margin:0;font-size:20px">🚨 Production Incident — {profile.get("alert_name",ns)}</h2>
<p style="margin:4px 0 0;opacity:0.9;font-size:13px">Cluster: {CLUSTER_NAME} | Namespace: {ns} | Ticket: {inc_number}</p>
</div>

{rca_html}

<h3 style="color:#333;border-bottom:2px solid #eee;padding-bottom:8px">📋 Incident Details</h3>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px">
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;width:140px;border:1px solid #ddd">Incident</td>
    <td style="padding:8px;border:1px solid #ddd"><a href="{snow_link}" style="color:#1565c0;font-weight:bold">{inc_number}</a></td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Application</td>
    <td style="padding:8px;border:1px solid #ddd">{profile.get("alert_name",ns)}</td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Namespace</td>
    <td style="padding:8px;border:1px solid #ddd">{ns}</td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Pods</td>
    <td style="padding:8px;border:1px solid #ddd">{", ".join(pods) or "Unknown"}</td></tr>
</table>

<h3 style="color:#333;border-bottom:2px solid #eee;padding-bottom:8px">🔧 Jenkins CI/CD</h3>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px">
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;width:140px;border:1px solid #ddd">Job</td>
    <td style="padding:8px;border:1px solid #ddd"><a href="{jdet.get("job_url","#")}" style="color:#1565c0">{jdet.get("job_url","N/A")}</a></td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Build</td>
    <td style="padding:8px;border:1px solid #ddd">#{jdet.get("build_number","N/A")}</td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Status</td>
    <td style="padding:8px;border:1px solid #ddd;color:{jcolor};font-weight:bold">{jdet.get("build_status","N/A")}</td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Branch</td>
    <td style="padding:8px;border:1px solid #ddd">{jdet.get("git_branch","N/A")}</td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Commit</td>
    <td style="padding:8px;border:1px solid #ddd;font-family:monospace">{jdet.get("git_commit","N/A")}</td></tr>
<tr><td style="padding:8px;background:#f9f9f9;font-weight:bold;border:1px solid #ddd">Console</td>
    <td style="padding:8px;border:1px solid #ddd"><a href="{jdet.get("console_url","#")}" style="color:#1565c0">View Console</a></td></tr>
</table>

<h3 style="color:#333;border-bottom:2px solid #eee;padding-bottom:8px">🔴 Error Logs</h3>
<pre style="background:#1e1e1e;color:#f8f8f2;padding:16px;border-radius:6px;font-size:11px;max-height:200px;overflow:auto">{chr(10).join(logs[:20])}</pre>

<div style="text-align:center;margin:24px 0">
<a href="{snow_link}" style="background:#1565c0;color:white;padding:14px 22px;text-decoration:none;border-radius:6px;font-weight:bold;margin:6px;display:inline-block">📋 View ServiceNow</a>
<a href="{agent_link}" style="background:#2e7d32;color:white;padding:14px 22px;text-decoration:none;border-radius:6px;font-weight:bold;margin:6px;display:inline-block">🤖 View Agent Investigation</a>
<a href="{jdet.get("console_url","#")}" style="background:#e65100;color:white;padding:14px 22px;text-decoration:none;border-radius:6px;font-weight:bold;margin:6px;display:inline-block">🔧 Jenkins Console</a>
<a href="{profile.get("github_repo","#")}" style="background:#424242;color:white;padding:14px 22px;text-decoration:none;border-radius:6px;font-weight:bold;margin:6px;display:inline-block">📦 GitHub</a>
</div>

<hr style="border:1px solid #eee"/>
<p style="color:#999;font-size:11px;text-align:center">DevOps Monitoring v1.2 — {CLUSTER_NAME} — {AWS_REGION}</p>
</div></body></html>'''

    try:
        resp = ses.send_email(
            Source=SES_SENDER,
            Destination={'ToAddresses': [sre]},
            Message={'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                     'Body': {'Html': {'Data': body_html, 'Charset': 'UTF-8'}}}
        )
        print(f'[EMAIL] Sent: {resp.get("MessageId","")}')
        return True, resp.get('MessageId','')
    except Exception as e:
        print(f'[EMAIL] Error: {e}')
        return False, str(e)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        body   = request.json or {}
        alerts = body.get('alerts', [body])
        results = []
        for alert in alerts:
            if alert.get('status') != 'firing':
                results.append({'action':'skipped','reason':'not firing'}); continue
            labels  = alert.get('labels', {})
            ns      = labels.get('namespace', '')
            if not ns:
                results.append({'action':'skipped','reason':'no namespace'}); continue
            profile = get_profile(ns)
            print(f'[WEBHOOK] ns={ns} app={profile.get("alert_name","?")}')

            logs, pods = query_loki(ns)
            if not logs or (len(logs)==1 and 'Loki' in logs[0] and 'error' in logs[0].lower()):
                results.append({'action':'skipped','reason':'no error logs'}); continue

            sig       = _sig(logs)
            h         = _hash(ns, sig)
            etype     = _etype(sig)
            ts        = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
            dedup_key = f"{ns}:{h}"

            # Layer 1: in-memory dedup (race condition fix)
            if _is_duplicate_in_memory(dedup_key):
                print(f'[DEDUP] Blocked by memory lock: {dedup_key}')
                results.append({'action':'skipped','reason':'duplicate (in-memory)','hash':h})
                continue

            # Layer 2: ServiceNow open ticket check
            jdet = get_jenkins(profile)
            existing, sys_id = _check_existing(ns, sig)
            if existing:
                _update_ticket(sys_id, f'RECURRING — {ts}\n' + '\n'.join(logs[:20]))
                print(f'[SNOW] Updated existing: {existing}')
                results.append({'action':'updated','incident':existing,'hash':h})
                continue

            # Create new ticket
            title = f'[CRITICAL] {etype} | {profile.get("alert_name",ns)} | NS:{ns} | [{h}]'
            desc  = (f'APP: {profile.get("alert_name",ns)}\nNS: {ns}\nCLUSTER: {CLUSTER_NAME}\n'
                     f'ERROR: {etype}\nHASH: {h}\nCOUNT: {len(logs)}\nPODS: {", ".join(pods)}\n'
                     f'JENKINS: #{jdet.get("build_number","N/A")} ({jdet.get("build_status","N/A")})')
            notes = 'PODS:\n' + '\n'.join(f'  {p}' for p in pods) + '\n\nLOGS:\n' + '\n'.join(logs[:40])

            inc_num, inc_sid = _create_ticket(title, desc, notes)
            if not inc_num:
                with _dedup_lock: _recent_keys.pop(dedup_key, None)
                results.append({'action':'failed','reason':'SNOW create failed'}); continue

            agent_ok, inv_id = trigger_agent(inc_num, inc_sid, title, logs,
                                              list(pods), ns, etype, jdet, profile)

            def _async(in_, ttl, ns_, pods_, logs_, inv, jd, prof, aok):
                rca = get_rca_from_snow(in_, 90) if aok else ''
                send_email(in_, ttl, ns_, pods_, logs_, inv, jd, prof, rca)
            threading.Thread(
                target=_async,
                args=(inc_num,title,ns,list(pods),logs,
                      inv_id if agent_ok else '',jdet,profile,agent_ok),
                daemon=True).start()

            print(f'[SNOW] Created: {inc_num} | Agent: {agent_ok} | hash: {h}')
            results.append({'action':'created','incident':inc_num,
                            'hash':h,'agent_triggered':agent_ok})
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    profiles = load_profiles()
    try: import boto3; b3 = True
    except: b3 = False
    return jsonify({'status':'healthy','version':'non-docker-1.2','cluster':CLUSTER_NAME,
                    'monitored_apps':[{'name':p.get('alert_name'),'namespace':p.get('namespace')} for p in profiles],
                    'agent_webhook':bool(DEVOPS_AGENT_WEBHOOK),
                    'boto3':b3,
                    'dedup_window_secs':DEDUP_WINDOW,
                    'active_dedup_keys':len(_recent_keys)})

@app.route('/test-loki', methods=['GET'])
def test_loki():
    ns = request.args.get('ns','default'); mins = int(request.args.get('mins',60))
    logs, pods = query_loki(ns, mins)
    return jsonify({'namespace':ns,'error_logs_found':len(logs),'pods':pods,'sample':logs[:10]})

@app.route('/test-snow', methods=['GET'])
def test_snow():
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    num, sid = _create_ticket(f'[TEST] Non-Docker Monitor v1.2 — {ts}',
                               'Automated test. Safe to close.', 'test-snow endpoint')
    return jsonify({'ticket_created':bool(num),'incident_number':num,
                    'snow_url':f'https://{SNOW_INSTANCE}.service-now.com/incident.do?sysparm_query=number={num}' if num else None})

@app.route('/test-agent', methods=['GET'])
def test_agent():
    prof = (load_profiles() or [{}])[0]
    ok, res = trigger_agent('INC-TEST','sys-test','[TEST] Agent connectivity',
                             ['[TEST] ERROR: test'],['test-pod'],
                             prof.get('namespace','default'),'Application Error',
                             get_jenkins(prof), prof)
    return jsonify({'agent_triggered':ok,'result':res})

@app.route('/test-email', methods=['GET'])
def test_email():
    prof = (load_profiles() or [{}])[0]
    ok, res = send_email('INC-TEST','[TEST] Email Pipeline v1.2',
                          prof.get('namespace','default'),['test-pod'],
                          ['[TEST] ERROR: email test'],'test-inv',
                          get_jenkins(prof), prof,
                          '## Test RCA\n✅ Non-docker email working\n✅ Dedup lock active\n✅ Agent Space URL configured')
    return jsonify({'email_sent':ok,'result':res,
                    'recipient':prof.get('sre_email','') or DEFAULT_SRE_EMAIL,
                    'sender':SES_SENDER})

if __name__ == '__main__':
    profiles = load_profiles()
    print(f'Alert Processor non-docker v1.2')
    print(f'SNOW: {SNOW_INSTANCE} | Agent: {bool(DEVOPS_AGENT_WEBHOOK)} | Dedup: {DEDUP_WINDOW}s')
    print(f'Apps: {[p.get("alert_name") for p in profiles]}')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','5000')))
PYEOF

ok "Alert processor written: $INSTALL_DIR/processor/app.py"

cat > /etc/systemd/system/devops-alert-processor.service << PROC_SVC
[Unit]
Description=DevOps Alert Processor (Flask)
After=network.target loki.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}/processor
Environment="LOKI_URL=http://localhost:${LOKI_PORT}"
Environment="SNOW_URL=https://${SNOW_INSTANCE}.service-now.com/api/now/table/incident"
Environment="SNOW_USER=${SNOW_USER}"
Environment="SNOW_PASS=${SNOW_PASS}"
Environment="SNOW_INSTANCE=${SNOW_INSTANCE}"
Environment="AWS_REGION=${AWS_REGION}"
Environment="AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID}"
Environment="CLUSTER_NAME=minikube"
Environment="DEVOPS_AGENT_WEBHOOK_URL=${DEVOPS_AGENT_WEBHOOK_URL}"
Environment="DEVOPS_AGENT_HMAC_SECRET=${DEVOPS_AGENT_HMAC_SECRET}"
Environment="DEVOPS_AGENT_SPACE_URL=${DEVOPS_AGENT_SPACE_URL}"
Environment="SRE_EMAIL=${SRE_EMAIL}"
Environment="SES_SENDER_EMAIL=${SES_SENDER_EMAIL}"
Environment="PROFILES_FILE=${INSTALL_DIR}/processor/profiles.json"
Environment="PORT=${PROCESSOR_PORT}"
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/processor/app.py
Restart=always
RestartSec=5
StandardOutput=append:${HOME_DIR}/logs/processor.log
StandardError=append:${HOME_DIR}/logs/processor.log

[Install]
WantedBy=multi-user.target
PROC_SVC

systemctl daemon-reload
systemctl enable devops-alert-processor
systemctl restart devops-alert-processor

log "Waiting for alert processor (up to 90s)..."
PROC_HTTP="000"
for i in $(seq 1 18); do
    PROC_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
        "http://localhost:${PROCESSOR_PORT}/health" 2>/dev/null || echo "000")
    [ "$PROC_HTTP" = "200" ] && ok "Alert processor running on port $PROCESSOR_PORT" && break
    wait_ "Processor HTTP $PROC_HTTP ($i/18)..." && sleep 5
done
[ "$PROC_HTTP" != "200" ] && {
    warn "Processor not ready — showing logs:"
    journalctl -u devops-alert-processor -n 20 --no-pager
}

# =============================================================================
# PHASE 7 — CONFIGURE GRAFANA
# =============================================================================
step "PHASE 7 — Configure Grafana (datasource + alert rules + routing)"

GF_API="http://localhost:${GRAFANA_PORT}"
GF_AUTH="admin:admin123"

log "Registering Loki datasource..."
DS=$(curl -s -X POST "$GF_API/api/datasources" \
    -H "Content-Type: application/json" -u "$GF_AUTH" \
    -d "{\"name\":\"Loki\",\"type\":\"loki\",\"url\":\"http://localhost:${LOKI_PORT}\",\"access\":\"proxy\",\"isDefault\":true}" 2>/dev/null)
LOKI_UID=$(echo "$DS" | jq -r '.datasource.uid // .uid // empty' 2>/dev/null)
if [ -z "$LOKI_UID" ] || [ "$LOKI_UID" = "null" ]; then
    LOKI_UID=$(curl -s "$GF_API/api/datasources" -u "$GF_AUTH" 2>/dev/null \
        | jq -r '.[] | select(.type=="loki") | .uid' | head -1)
fi
[ -z "$LOKI_UID" ] && LOKI_UID="loki"
ok "Loki datasource UID: $LOKI_UID"

log "Creating alert folder..."
FR=$(curl -s -X POST "$GF_API/api/folders" \
    -H "Content-Type: application/json" -u "$GF_AUTH" \
    -d '{"title":"App Alerts"}' 2>/dev/null)
FOLDER_UID=$(echo "$FR" | jq -r '.uid // empty' 2>/dev/null)
if [ -z "$FOLDER_UID" ] || [ "$FOLDER_UID" = "null" ]; then
    FOLDER_UID=$(curl -s "$GF_API/api/folders" -u "$GF_AUTH" 2>/dev/null \
        | jq -r '.[] | select(.title=="App Alerts") | .uid' | head -1)
fi
[ -n "$FOLDER_UID" ] && ok "Folder UID: $FOLDER_UID" || FOLDER_UID="general"

for i in "${ACTIVE_APPS[@]}"; do
    app_name="$(eval echo \$APP_${i}_ALERT_NAME)"
    app_ns="$(eval echo \$APP_${i}_NAMESPACE)"
    log "Creating alert rule: $app_name (ns=$app_ns)..."
    RR=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$GF_API/api/v1/provisioning/alert-rules" \
        -H "Content-Type: application/json" -u "$GF_AUTH" \
        -d "{
            \"title\":\"${app_name} — Critical Error Alert\",
            \"ruleGroup\":\"app-error-alerts\",
            \"folderUID\":\"${FOLDER_UID}\",
            \"condition\":\"C\",
            \"for\":\"${ALERT_FOR_SECONDS}s\",
            \"noDataState\":\"OK\",
            \"execErrState\":\"Error\",
            \"annotations\":{\"summary\":\"Critical errors in ${app_name}\"},
            \"labels\":{\"severity\":\"critical\",\"namespace\":\"${app_ns}\",\"app\":\"${app_name}\"},
            \"data\":[
                {\"refId\":\"A\",\"datasourceUid\":\"${LOKI_UID}\",\"relativeTimeRange\":{\"from\":600,\"to\":0},
                 \"model\":{\"expr\":\"count_over_time({namespace=\\\"${app_ns}\\\"} |~ \\\"(?i)(ERROR|FATAL|CRITICAL|Exception|Error:|500|501|502|503|504)\\\" !~ \\\"(?i)(\\\\\\\\bDEBUG\\\\\\\\b|\\\\\\\\bINFO\\\\\\\\b|\\\\\\\\bWARN\\\\\\\\b)\\\" [5m])\",\"refId\":\"A\"}},
                {\"refId\":\"B\",\"datasourceUid\":\"__expr__\",\"relativeTimeRange\":{\"from\":0,\"to\":0},
                 \"model\":{\"type\":\"reduce\",\"expression\":\"A\",\"reducer\":\"last\",\"refId\":\"B\"}},
                {\"refId\":\"C\",\"datasourceUid\":\"__expr__\",\"relativeTimeRange\":{\"from\":0,\"to\":0},
                 \"model\":{\"type\":\"threshold\",\"expression\":\"B\",\"conditions\":[{\"evaluator\":{\"type\":\"gt\",\"params\":[0]}}],\"refId\":\"C\"}}
            ]
        }")
    [ "$RR" = "201" ] && ok "  Rule created: $app_name" || warn "  Rule HTTP: $RR"
done

log "Creating contact point → alert processor..."
CP=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$GF_API/api/v1/provisioning/contact-points" \
    -H "Content-Type: application/json" -u "$GF_AUTH" \
    -d "{\"name\":\"DevOps-Alert-Processor\",\"type\":\"webhook\",\"settings\":{\"url\":\"http://localhost:${PROCESSOR_PORT}/webhook\",\"httpMethod\":\"POST\"}}")
[ "$CP" = "202" ] && ok "Contact point created" || warn "Contact point HTTP: $CP"

# FIX: Grafana 13 requires X-Disable-Provenance + fetch real default receiver
log "Setting routing policy (Grafana 13 fix)..."
DEFAULT_RECEIVER=$(curl -s "$GF_API/api/v1/provisioning/policies" \
    -u "$GF_AUTH" 2>/dev/null | jq -r '.receiver // "grafana-default-email"')
log "  Default receiver: $DEFAULT_RECEIVER"
RP=$(curl -s -o /dev/null -w "%{http_code}" \
    -X PUT "$GF_API/api/v1/provisioning/policies" \
    -H "Content-Type: application/json" \
    -H "X-Disable-Provenance: true" \
    -u "$GF_AUTH" \
    -d "{
        \"receiver\": \"${DEFAULT_RECEIVER}\",
        \"routes\": [{
            \"receiver\": \"DevOps-Alert-Processor\",
            \"object_matchers\": [[\"severity\", \"=\", \"critical\"]]
        }]
    }")
[ "$RP" = "202" ] && ok "Routing policy set ✅" || warn "Routing HTTP: $RP"

# =============================================================================
# PHASE 8 — VERIFY
# =============================================================================
step "PHASE 8 — Verification"

echo ""
log "Service status:"
for svc in grafana-server loki promtail devops-alert-processor; do
    STATUS=$(systemctl is-active $svc 2>/dev/null)
    [ "$STATUS" = "active" ] && ok "$svc" || warn "$svc: $STATUS"
done
echo ""

GF_H=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
    "http://localhost:${GRAFANA_PORT}/api/health" 2>/dev/null || echo "000")
LK_H=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
    "http://localhost:${LOKI_PORT}/ready" 2>/dev/null || echo "000")
PR_H=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
    "http://localhost:${PROCESSOR_PORT}/health" 2>/dev/null || echo "000")

[ "$GF_H" = "200" ] && ok "Grafana   : http://localhost:${GRAFANA_PORT}" || warn "Grafana: $GF_H"
[ "$LK_H" = "200" ] && ok "Loki      : http://localhost:${LOKI_PORT}"   || warn "Loki: $LK_H"
[ "$PR_H" = "200" ] && ok "Processor : http://localhost:${PROCESSOR_PORT}" || warn "Processor: $PR_H"

log "Testing ServiceNow..."
SR=$(curl -s --max-time 20 "http://localhost:${PROCESSOR_PORT}/test-snow" 2>/dev/null || echo '{}')
SOK=$(echo "$SR"  | jq -r '.ticket_created  // false' 2>/dev/null || echo "false")
SINC=$(echo "$SR" | jq -r '.incident_number // "N/A"' 2>/dev/null || echo "N/A")
[ "$SOK" = "true" ] && ok "ServiceNow : test ticket $SINC ✅" || warn "ServiceNow : $SR"

log "Testing DevOps Agent..."
AT=$(curl -s --max-time 20 "http://localhost:${PROCESSOR_PORT}/test-agent" 2>/dev/null || echo '{}')
AOK=$(echo "$AT" | jq -r '.agent_triggered // false' 2>/dev/null || echo "false")
[ "$AOK" = "true" ] && ok "DevOps Agent : webhook OK ✅" || warn "DevOps Agent : $AT"

log "Sending test email..."
ET=$(curl -s --max-time 30 "http://localhost:${PROCESSOR_PORT}/test-email" 2>/dev/null || echo '{}')
EOK=$(echo "$ET" | jq -r '.email_sent // false' 2>/dev/null || echo "false")
[ "$EOK" = "true" ] && ok "Email        : sent to $SRE_EMAIL ✅" || warn "Email        : $ET"

EC2_IP=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "YOUR-EC2-IP")

# Save start script for reboots
cat > ${HOME_DIR}/start-monitoring.sh << STARTSCRIPT
#!/bin/bash
# DevOps Monitoring Stack — Start/Status Script
# Run after reboot or if any service is down

GRAFANA_PORT=${GRAFANA_PORT}
PROCESSOR_PORT=${PROCESSOR_PORT}
LOKI_PORT=${LOKI_PORT}
HOME_DIR="${HOME_DIR}"

ok()    { echo "  ✅ \$1"; }
warn()  { echo "  ⚠️  \$1"; }
wait_() { echo "  ⏳ \$1"; }
log()   { echo "[\$(date +%H:%M:%S)] \$1"; }

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
    if systemctl is-active \$svc &>/dev/null; then
        ok "\$svc already running"
    else
        log "Starting \$svc..."
        systemctl start \$svc
        sleep 3
        systemctl is-active \$svc &>/dev/null && ok "\$svc started" || warn "\$svc failed to start"
    fi
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Status"
echo "══════════════════════════════════════════════════════"
for svc in grafana-server loki promtail devops-alert-processor; do
    STATUS=\$(systemctl is-active \$svc 2>/dev/null)
    [ "\$STATUS" = "active" ] && ok "\$svc" || warn "\$svc: \$STATUS"
done
echo ""
curl -s -o /dev/null -w "  Grafana   : HTTP %{http_code}\n" http://localhost:\${GRAFANA_PORT}/api/health
curl -s -o /dev/null -w "  Loki      : HTTP %{http_code}\n" http://localhost:\${LOKI_PORT}/ready
curl -s -o /dev/null -w "  Processor : HTTP %{http_code}\n" http://localhost:\${PROCESSOR_PORT}/health
echo ""
echo "  Logs : \${HOME_DIR}/logs/"
echo "  curl -s http://localhost:\${PROCESSOR_PORT}/health | jq ."
echo "══════════════════════════════════════════════════════"
STARTSCRIPT

chmod +x ${HOME_DIR}/start-monitoring.sh
ok "Start script saved: ${HOME_DIR}/start-monitoring.sh"

# =============================================================================
echo ""
echo "══════════════════════════════════════════════════════"
echo "  ✅  MONITORING STACK v1.2 READY"
echo "══════════════════════════════════════════════════════"
echo ""
echo "  📁 HOME DIR      : $HOME_DIR"
echo "     setup log     : $LOG_FILE"
echo "     start script  : ${HOME_DIR}/start-monitoring.sh"
echo "     service logs  : ${HOME_DIR}/logs/"
echo ""
echo "  📊 GRAFANA"
echo "     Local    : http://localhost:${GRAFANA_PORT}"
echo "     EC2      : http://${EC2_IP}:${GRAFANA_PORT}"
echo "     Domain   : https://devopsatkmcptools.com/grafana"
echo "     Login    : admin / admin123"
echo "     Alerts   : http://localhost:${GRAFANA_PORT}/alerting/list"
echo ""
echo "  📱 MONITORED APPS"
for i in "${ACTIVE_APPS[@]}"; do
    echo "     App $i : $(eval echo \$APP_${i}_ALERT_NAME) (ns: $(eval echo \$APP_${i}_NAMESPACE))"
done
echo ""
echo "  🤖 AWS DEVOPS AGENT  : $AOK"
echo "     Space URL : $DEVOPS_AGENT_SPACE_URL"
echo "  📧 EMAIL             : $EOK"
echo "  🎫 SNOW TEST TICKET  : $SINC"
echo ""
echo "  🔧 ALB TARGET GROUP"
echo "     Protocol : HTTP   Port: $GRAFANA_PORT   Health: /api/health"
echo ""
echo "  🔍 ALERT FLOW"
echo "     /var/log/pods/ → Promtail → Loki"
echo "       └─▶ Grafana fires after ${ALERT_FOR_SECONDS}s"
echo "             └─▶ Processor (dedup: memory + SNOW)"
echo "                   ├─▶ SNOW ticket (no duplicates)"
echo "                   ├─▶ AWS DevOps Agent"
echo "                   └─▶ RCA email after 90s"
echo ""
echo "  🛠️  COMMANDS"
echo "     ${HOME_DIR}/start-monitoring.sh     # start if down"
echo "     systemctl restart devops-alert-processor"
echo "     tail -f ${HOME_DIR}/logs/processor.log"
echo ""
echo "  🧪 TEST"
echo "     curl -s http://localhost:${PROCESSOR_PORT}/health     | jq ."
echo "     curl -s http://localhost:${PROCESSOR_PORT}/test-snow  | jq ."
echo "     curl -s http://localhost:${PROCESSOR_PORT}/test-agent | jq ."
echo "     curl -s http://localhost:${PROCESSOR_PORT}/test-email | jq ."
echo ""
echo "  Log: $LOG_FILE"
echo "══════════════════════════════════════════════════════"
