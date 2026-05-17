#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║         MCP Gateway Platform — Automated Setup Script           ║
# ║                                                                  ║
# ║  Deploys:                                                        ║
# ║    • Jenkins MCP Server    (port 6500)                          ║
# ║    • GitHub MCP Server     (port 6501)                          ║
# ║    • GitLab MCP Server     (port 6502)                          ║
# ║    • ServiceNow MCP Server (port 6503)                          ║
# ║    • SonarQube MCP Server  (port 6504)                          ║
# ║    • agentgateway v1.1.0   (port 6000 MCP / 15000 Admin UI)    ║
# ╚══════════════════════════════════════════════════════════════════╝

set -e

BASE_DIR="/home/rba/mcpgateway"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}   MCP Gateway Platform — Setup                     ${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════${NC}"
echo ""

info "Checking prerequisites..."
command -v docker  &>/dev/null || error "Docker not installed."
command -v python3 &>/dev/null || error "Python3 not installed."
docker compose version &>/dev/null || error "Docker Compose plugin not found."
success "Docker       $(docker --version | grep -oP '[\d.]+' | head -1)"
success "Compose      $(docker compose version | grep -oP '[\d.]+' | head -1)"
success "Python3      $(python3 --version | awk '{print $2}')"

echo ""
info "Creating directory structure at $BASE_DIR ..."
mkdir -p "$BASE_DIR"/{jenkins,github,gitlab,snow,sonar}
cd "$BASE_DIR"
success "Directories ready"

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}   Enter Credentials                                ${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════${NC}"
echo ""

if [ -f "$BASE_DIR/.env" ]; then
    warn ".env already exists — skipping credential collection."
    warn "Delete .env and re-run to change credentials."
    source "$BASE_DIR/.env"
else
    echo -e "${YELLOW}Press ENTER to use default value shown in [brackets]${NC}"
    echo ""

    echo -e "${CYAN}── MCP Gateway ──────────────────────────────────────${NC}"
    read -rp  "MCP Secret Token (Bearer token for clients): " MCP_SECRET_TOKEN
    MCP_SECRET_TOKEN=${MCP_SECRET_TOKEN:-"changeme_strong_secret_here"}

    echo ""
    echo -e "${CYAN}── Jenkins ──────────────────────────────────────────${NC}"
    read -rp  "Jenkins URL [https://your-jenkins.example.com/]: " JENKINS_URL
    JENKINS_URL=${JENKINS_URL:-"https://your-jenkins.example.com/"}
    read -rp  "Jenkins Username [admin]: " JENKINS_USERNAME
    JENKINS_USERNAME=${JENKINS_USERNAME:-"admin"}
    read -rsp "Jenkins API Token: " JENKINS_TOKEN; echo ""
    read -rp  "Verify Jenkins SSL [false]: " JENKINS_VERIFY_SSL
    JENKINS_VERIFY_SSL=${JENKINS_VERIFY_SSL:-"false"}

    echo ""
    echo -e "${CYAN}── GitHub ───────────────────────────────────────────${NC}"
    read -rp  "GitHub API URL [https://api.github.com]: " GITHUB_URL
    GITHUB_URL=${GITHUB_URL:-"https://api.github.com"}
    read -rsp "GitHub Personal Access Token (PAT): " GITHUB_PAT; echo ""
    read -rp  "GitHub Organization name: " GITHUB_ORG
    read -rp  "Verify GitHub SSL [true]: " GITHUB_VERIFY_SSL
    GITHUB_VERIFY_SSL=${GITHUB_VERIFY_SSL:-"true"}

    echo ""
    echo -e "${CYAN}── GitLab ───────────────────────────────────────────${NC}"
    read -rp  "GitLab URL [https://gitlab.com]: " GITLAB_URL
    GITLAB_URL=${GITLAB_URL:-"https://gitlab.com"}
    read -rsp "GitLab Access Token: " GITLAB_TOKEN; echo ""
    read -rp  "Verify GitLab SSL [true]: " GITLAB_VERIFY_SSL
    GITLAB_VERIFY_SSL=${GITLAB_VERIFY_SSL:-"true"}

    echo ""
    echo -e "${CYAN}── ServiceNow ───────────────────────────────────────${NC}"
    read -rp  "ServiceNow URL [https://your-instance.service-now.com]: " SNOW_URL
    SNOW_URL=${SNOW_URL:-"https://your-instance.service-now.com"}
    read -rp  "ServiceNow Username [admin]: " SNOW_USERNAME
    SNOW_USERNAME=${SNOW_USERNAME:-"admin"}
    read -rsp "ServiceNow Password: " SNOW_PASSWORD; echo ""
    read -rp  "Verify ServiceNow SSL [true]: " SNOW_VERIFY_SSL
    SNOW_VERIFY_SSL=${SNOW_VERIFY_SSL:-"true"}

    echo ""
    echo -e "${CYAN}── SonarQube ────────────────────────────────────────${NC}"
    read -rp  "SonarQube URL [https://your-sonarqube.example.com]: " SONARQUBE_URL
    SONARQUBE_URL=${SONARQUBE_URL:-"https://your-sonarqube.example.com"}
    read -rsp "SonarQube Token: " SONAR_TOKEN; echo ""
    read -rp  "Verify SonarQube SSL [false]: " SONAR_VERIFY_SSL
    SONAR_VERIFY_SSL=${SONAR_VERIFY_SSL:-"false"}

    cat > "$BASE_DIR/.env" <<EOF
# ── Gateway ──────────────────────────────────────────────────────
MCP_SECRET_TOKEN=${MCP_SECRET_TOKEN}
TOOL_CACHE_TTL=300

# ── Jenkins ──────────────────────────────────────────────────────
JENKINS_URL=${JENKINS_URL}
JENKINS_USERNAME=${JENKINS_USERNAME}
JENKINS_TOKEN=${JENKINS_TOKEN}
JENKINS_VERIFY_SSL=${JENKINS_VERIFY_SSL}

# ── GitHub ───────────────────────────────────────────────────────
GITHUB_URL=${GITHUB_URL}
GITHUB_PAT=${GITHUB_PAT}
GITHUB_ORG=${GITHUB_ORG}
GITHUB_VERIFY_SSL=${GITHUB_VERIFY_SSL}

# ── GitLab ───────────────────────────────────────────────────────
GITLAB_URL=${GITLAB_URL}
GITLAB_TOKEN=${GITLAB_TOKEN}
GITLAB_VERIFY_SSL=${GITLAB_VERIFY_SSL}

# ── ServiceNow ───────────────────────────────────────────────────
SNOW_URL=${SNOW_URL}
SNOW_USERNAME=${SNOW_USERNAME}
SNOW_PASSWORD=${SNOW_PASSWORD}
SNOW_VERIFY_SSL=${SNOW_VERIFY_SSL}

# ── SonarQube ────────────────────────────────────────────────────
SONARQUBE_URL=${SONARQUBE_URL}
SONAR_TOKEN=${SONAR_TOKEN}
SONAR_VERIFY_SSL=${SONAR_VERIFY_SSL}
EOF
    chmod 600 "$BASE_DIR/.env"
    success ".env created (chmod 600)"
fi

source "$BASE_DIR/.env"

echo ""
info "Writing docker-compose.yaml ..."

cat > "$BASE_DIR/docker-compose.yaml" << 'COMPOSE'
version: "3.9"

networks:
  mcp-net:
    driver: bridge

x-common: &common
  restart: unless-stopped
  networks:
    - mcp-net
  logging:
    driver: "json-file"
    options:
      max-size: "50m"
      max-file: "5"

services:

  jenkins-mcp:
    <<: *common
    build:
      context: ./jenkins
    container_name: jenkins-mcp
    ports:
      - "6500:6500"
    environment:
      JENKINS_URL:        ${JENKINS_URL}
      JENKINS_USERNAME:   ${JENKINS_USERNAME:-admin}
      JENKINS_TOKEN:      ${JENKINS_TOKEN}
      JENKINS_VERIFY_SSL: ${JENKINS_VERIFY_SSL:-false}
      MCP_SECRET_TOKEN:   ""
      PORT:               "6500"
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6500/health',timeout=5)"]
      interval: 30s
      timeout:  10s
      retries:  3
      start_period: 20s

  github-mcp:
    <<: *common
    build:
      context: ./github
    container_name: github-mcp
    ports:
      - "6501:6501"
    environment:
      GITHUB_URL:        ${GITHUB_URL:-https://api.github.com}
      GITHUB_PAT:        ${GITHUB_PAT}
      GITHUB_ORG:        ${GITHUB_ORG:-}
      GITHUB_VERIFY_SSL: ${GITHUB_VERIFY_SSL:-true}
      MCP_SECRET_TOKEN:  ""
      PORT:              "6501"
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6501/health',timeout=5)"]
      interval: 30s
      timeout:  10s
      retries:  3
      start_period: 20s

  gitlab-mcp:
    <<: *common
    build:
      context: ./gitlab
    container_name: gitlab-mcp
    ports:
      - "6502:6502"
    environment:
      GITLAB_URL:        ${GITLAB_URL:-https://gitlab.com}
      GITLAB_TOKEN:      ${GITLAB_TOKEN}
      GITLAB_VERIFY_SSL: ${GITLAB_VERIFY_SSL:-true}
      MCP_SECRET_TOKEN:  ""
      PORT:              "6502"
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6502/health',timeout=5)"]
      interval: 30s
      timeout:  10s
      retries:  3
      start_period: 20s

  snow-mcp:
    <<: *common
    build:
      context: ./snow
    container_name: snow-mcp
    ports:
      - "6503:6503"
    environment:
      SNOW_URL:         ${SNOW_URL}
      SNOW_USERNAME:    ${SNOW_USERNAME:-admin}
      SNOW_PASSWORD:    ${SNOW_PASSWORD}
      SNOW_VERIFY_SSL:  ${SNOW_VERIFY_SSL:-true}
      MCP_SECRET_TOKEN: ""
      PORT:             "6503"
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6503/health',timeout=5)"]
      interval: 30s
      timeout:  10s
      retries:  3
      start_period: 20s

  sonar-mcp:
    <<: *common
    build:
      context: ./sonar
    container_name: sonar-mcp
    ports:
      - "6504:6504"
    environment:
      SONARQUBE_URL:    ${SONARQUBE_URL}
      SONAR_TOKEN:      ${SONAR_TOKEN}
      SONAR_VERIFY_SSL: ${SONAR_VERIFY_SSL:-false}
      MCP_SECRET_TOKEN: ""
      PORT:             "6504"
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6504/health',timeout=5)"]
      interval: 30s
      timeout:  10s
      retries:  3
      start_period: 20s

  mcp-gateway:
    <<: *common
    image: cr.agentgateway.dev/agentgateway:v1.1.0
    container_name: mcp-gateway
    ports:
      - "6000:6000"
      - "15000:15000"
    volumes:
      - ./config.yaml:/config.yaml
    environment:
      ADMIN_ADDR:       "0.0.0.0:15000"
      MCP_SECRET_TOKEN: ${MCP_SECRET_TOKEN}
    command: ["-f", "/config.yaml"]
    depends_on:
      jenkins-mcp:
        condition: service_healthy
      github-mcp:
        condition: service_healthy
      gitlab-mcp:
        condition: service_healthy
      snow-mcp:
        condition: service_healthy
      sonar-mcp:
        condition: service_healthy
COMPOSE

success "docker-compose.yaml created"

echo ""
info "Writing agentgateway config.yaml ..."

python3 - << PYEOF
import re
env = open("${BASE_DIR}/.env").read()
m = re.search(r'MCP_SECRET_TOKEN=(.+)', env)
secret = m.group(1).strip() if m else "changeme"
config = """# yaml-language-server: \$schema=https://agentgateway.dev/schema/config
binds:
- port: 6000
  listeners:
  - routes:
    - policies:
        cors:
          allowOrigins:
          - "*"
          allowHeaders:
          - "*"
          exposeHeaders:
          - "Mcp-Session-Id"
        apiKey:
          mode: strict
          keys:
          - key: "{secret}"
      backends:
      - mcp:
          targets:
          - name: jenkins
            mcp:
              host: "http://jenkins-mcp:6500/mcp"
          - name: github
            mcp:
              host: "http://github-mcp:6501/mcp"
          - name: gitlab
            mcp:
              host: "http://gitlab-mcp:6502/mcp"
          - name: snow
            mcp:
              host: "http://snow-mcp:6503/mcp"
          - name: sonar
            mcp:
              host: "http://sonar-mcp:6504/mcp"
""".format(secret=secret)
with open("${BASE_DIR}/config.yaml", "w") as f:
    f.write(config)
print("  config.yaml written with apiKey auth (mode: strict)")
PYEOF

success "config.yaml created with API key auth"

echo ""
info "Removing duplicate tool prefixes from backend server files ..."

for entry in "jenkins:jenkins/jenkins_mcp_server.py" "github:github/github_mcp_server.py" "gitlab:gitlab/gitlab_mcp_server.py" "snow:snow/snow_mcp_server.py" "sonar:sonar/sonarqube_mcp_server.py"; do
    prefix="${entry%%:*}"
    file="$BASE_DIR/${entry##*:}"
    if [ -f "$file" ]; then
        if grep -q "\"${prefix}_" "$file" 2>/dev/null; then
            sed -i "s/\"${prefix}_\"/\"/" "$file"
            success "${entry##*:} — '${prefix}_' prefix removed"
        else
            info "${entry##*:} — already clean"
        fi
    else
        warn "${entry##*:} — not found, skipping"
    fi
done

echo ""
info "Pulling agentgateway v1.1.0 ..."
docker pull cr.agentgateway.dev/agentgateway:v1.1.0
success "agentgateway image ready"

echo ""
info "Building all backend MCP server images (parallel) ..."
cd "$BASE_DIR"
docker compose build --parallel
success "All images built"

echo ""
info "Starting all containers ..."
docker compose up -d
success "Containers started"

echo ""
info "Waiting for backends to become healthy (up to 90s) ..."
BACKENDS=("jenkins-mcp" "github-mcp" "gitlab-mcp" "snow-mcp" "sonar-mcp")
TIMEOUT=90; ELAPSED=0; ALL_HEALTHY=false
while [ $ELAPSED -lt $TIMEOUT ]; do
    ALL_HEALTHY=true
    for svc in "${BACKENDS[@]}"; do
        STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "missing")
        [ "$STATUS" != "healthy" ] && ALL_HEALTHY=false && break
    done
    $ALL_HEALTHY && break
    sleep 5; ELAPSED=$((ELAPSED + 5))
    echo -ne "\r  Elapsed: ${ELAPSED}s / ${TIMEOUT}s ..."
done
echo ""
$ALL_HEALTHY && success "All 5 backends healthy" || warn "Some backends not healthy — check: docker compose ps"

echo ""
info "Verifying MCP gateway ..."
sleep 5

SESSION=$(curl -si -X POST http://localhost:6000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer ${MCP_SECRET_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"setup","version":"1.0"}}}' \
  2>/dev/null | grep -i 'mcp-session-id' | awk '{print $2}' | tr -d '\r\n')

if [ -n "$SESSION" ]; then
    TOOL_COUNT=$(curl -s -X POST http://localhost:6000/mcp \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      -H "Authorization: Bearer ${MCP_SECRET_TOKEN}" \
      -H "Mcp-Session-Id: $SESSION" \
      -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
      2>/dev/null | grep '^data:' | sed 's/^data: //' \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['result']['tools']))" 2>/dev/null || echo "?")
    success "Gateway OK — ${TOOL_COUNT} tools loaded"
else
    warn "Gateway not responding — check: docker compose logs mcp-gateway"
fi

EC2_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}   Setup Complete!                                   ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${CYAN}Container Status:${NC}"
docker compose ps
echo ""
echo -e "${CYAN}Endpoints:${NC}"
echo "  MCP  (local) : http://${EC2_IP}:6000/mcp"
echo "  UI   (local) : http://${EC2_IP}:15000/ui/"
echo "  MCP  (ALB)   : https://mcp-gateway-lb-154346661.us-west-2.elb.amazonaws.com/mcp"
echo "  UI   (ALB)   : https://mcp-gateway-lb-154346661.us-west-2.elb.amazonaws.com/ui/"
echo ""
echo -e "${CYAN}Auth Token:${NC}"
echo "  Authorization: Bearer ${MCP_SECRET_TOKEN}"
echo ""
echo -e "${CYAN}Useful Commands:${NC}"
echo "  Start   : docker compose up -d"
echo "  Stop    : docker compose down"
echo "  Restart : docker compose restart"
echo "  Rebuild : docker compose build --parallel && docker compose up -d --force-recreate"
echo "  Logs    : docker compose logs -f"
echo "  Status  : docker compose ps"
echo ""
