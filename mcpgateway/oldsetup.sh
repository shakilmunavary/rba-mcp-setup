
#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  MCP Gateway — Setup Script
#  Tailored for: /home/mcpserver/mcpgateway/
#
#  Usage:
#    cd /home/mcpserver/mcpgateway
#    chmod +x setup.sh
#    sudo ./setup.sh
# ══════════════════════════════════════════════════════════════════

set -euo pipefail

BASE_DIR="/home/mcpserver/mcpgateway"
COMPOSE_CMD=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header()  { echo -e "\n${BLUE}══════════════════════════════════════════${NC}";
            echo -e "${BLUE}  $*${NC}";
            echo -e "${BLUE}══════════════════════════════════════════${NC}"; }

header "Step 1: Checking Docker"

if ! command -v docker &>/dev/null; then
  info "Docker not found — installing..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker
  systemctl start docker
  usermod -aG docker "${SUDO_USER:-$(whoami)}" 2>/dev/null || true
  success "Docker installed"
else
  success "Docker: $(docker --version)"
fi

if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
  success "Docker Compose v2: $(docker compose version)"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
  warn "Using legacy docker-compose"
else
  info "Installing Docker Compose v2..."
  ARCH=$(uname -m)
  COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest \
    | grep '"tag_name"' | cut -d'"' -f4)
  curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
    -o /usr/local/bin/docker-compose
  chmod +x /usr/local/bin/docker-compose
  COMPOSE_CMD="docker-compose"
  success "Docker Compose installed: ${COMPOSE_VERSION}"
fi

header "Step 2: Verifying directory structure"

[[ -d "$BASE_DIR" ]] || error "Base directory not found: $BASE_DIR"
cd "$BASE_DIR"

MISSING=0
for entry in \
  "jenkins/jenkins_mcp_server.py" \
  "github/github_mcp_server.py" \
  "gitlab/gitlab_mcp_server.py" \
  "snow/snow_mcp_server.py" \
  "sonar/sonarqube_mcp_server.py" \
  "gateway/gateway.py" \
  "gateway/Dockerfile" \
  "jenkins/Dockerfile" \
  "github/Dockerfile" \
  "gitlab/Dockerfile" \
  "snow/Dockerfile" \
  "sonar/Dockerfile"; do
  if [[ -f "$BASE_DIR/$entry" ]]; then
    success "Found: $entry"
  else
    warn "MISSING: $entry"
    MISSING=$((MISSING+1))
  fi
done

[[ $MISSING -eq 0 ]] || error "${MISSING} required file(s) missing. Fix above and re-run."

header "Step 3: Checking .env"

[[ -f "$BASE_DIR/.env" ]] || error ".env not found at $BASE_DIR/.env"

for key in MCP_SECRET_TOKEN JENKINS_TOKEN GITHUB_PAT GITLAB_TOKEN SNOW_PASSWORD SONAR_TOKEN; do
  if grep -q "^${key}=" "$BASE_DIR/.env" 2>/dev/null; then
    success ".env has: $key"
  else
    warn ".env missing key: $key"
  fi
done
success ".env is present and configured"

header "Step 4: Writing docker-compose.yaml"

cat > "${BASE_DIR}/docker-compose.yaml" << 'COMPOSE_EOF'
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
      MCP_SECRET_TOKEN:   ${MCP_SECRET_TOKEN}
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
      MCP_SECRET_TOKEN:  ${MCP_SECRET_TOKEN}
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
      MCP_SECRET_TOKEN:  ${MCP_SECRET_TOKEN}
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
      MCP_SECRET_TOKEN: ${MCP_SECRET_TOKEN}
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
      MCP_SECRET_TOKEN: ${MCP_SECRET_TOKEN}
      PORT:             "6504"
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6504/health',timeout=5)"]
      interval: 30s
      timeout:  10s
      retries:  3
      start_period: 20s

  mcp-gateway:
    <<: *common
    build:
      context: ./gateway
    container_name: mcp-gateway
    ports:
      - "6000:6000"
    environment:
      PORT:              "6000"
      MCP_SECRET_TOKEN:  ${MCP_SECRET_TOKEN}
      TOOL_CACHE_TTL:    ${TOOL_CACHE_TTL:-300}
      JENKINS_MCP_URL:   http://jenkins-mcp:6500
      JENKINS_MCP_TOKEN: ${MCP_SECRET_TOKEN}
      GITHUB_MCP_URL:    http://github-mcp:6501
      GITHUB_MCP_TOKEN:  ${MCP_SECRET_TOKEN}
      GITLAB_MCP_URL:    http://gitlab-mcp:6502
      GITLAB_MCP_TOKEN:  ${MCP_SECRET_TOKEN}
      SNOW_MCP_URL:      http://snow-mcp:6503
      SNOW_MCP_TOKEN:    ${MCP_SECRET_TOKEN}
      SONAR_MCP_URL:     http://sonar-mcp:6504
      SONAR_MCP_TOKEN:   ${MCP_SECRET_TOKEN}
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
    healthcheck:
      test: ["CMD","python3","-c","import urllib.request; urllib.request.urlopen('http://localhost:6000/health',timeout=8)"]
      interval: 30s
      timeout:  10s
      retries:  5
      start_period: 45s
COMPOSE_EOF

success "docker-compose.yaml written"

header "Step 5: Stopping any existing containers"
cd "$BASE_DIR"
${COMPOSE_CMD} down --remove-orphans 2>/dev/null && success "Old containers removed" \
  || info "No old containers found"

header "Step 6: Building Docker images (2-3 minutes)"
${COMPOSE_CMD} build --parallel
success "All images built"

header "Step 7: Starting the MCP stack"
${COMPOSE_CMD} up -d
success "Stack started"

header "Step 8: Waiting for services (~60s)"

wait_healthy() {
  local container="$1"
  local max_wait=120
  local elapsed=0
  printf "  %-20s " "${container}..."
  while [[ $elapsed -lt $max_wait ]]; do
    status=$(docker inspect --format='{{.State.Health.Status}}' "${container}" 2>/dev/null || echo "unknown")
    if [[ "$status" == "healthy" ]]; then
      echo -e " ${GREEN}healthy ✓${NC}"; return 0
    fi
    sleep 3; elapsed=$((elapsed+3)); printf "."
  done
  echo -e " ${RED}TIMEOUT (status: $status)${NC}"; return 1
}

ALL_HEALTHY=true
for c in jenkins-mcp github-mcp gitlab-mcp snow-mcp sonar-mcp mcp-gateway; do
  wait_healthy "$c" || ALL_HEALTHY=false
done

header "Setup Complete!"

echo ""
${COMPOSE_CMD} ps
echo ""

MCP_TOKEN=$(grep "^MCP_SECRET_TOKEN=" "${BASE_DIR}/.env" | cut -d= -f2 | tr -d '\r')
HOST_IP=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null \
          || hostname -I | awk '{print $1}')

echo -e "${CYAN}Health check:${NC}"
echo "  curl -s http://localhost:6000/health | python3 -m json.tool"
echo ""
echo -e "${CYAN}Count total tools loaded:${NC}"
echo "  curl -s -X POST http://localhost:6000/mcp \\"
echo "    -H 'Authorization: Bearer ${MCP_TOKEN}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}' \\"
echo "    | python3 -c \"import sys,json; d=json.load(sys.stdin); print('Total tools:', len(d['result']['tools']))\""
echo ""
echo -e "${CYAN}ALB Configuration:${NC}"
echo "  Target     : ${HOST_IP}:6000"
echo "  Health path: /health"
echo "  Auth header: Authorization: Bearer ${MCP_TOKEN}"
echo ""
echo -e "${CYAN}Useful commands:${NC}"
echo "  cd ${BASE_DIR}"
echo "  ${COMPOSE_CMD} ps"
echo "  ${COMPOSE_CMD} logs -f mcp-gateway"
echo "  ${COMPOSE_CMD} logs -f jenkins-mcp"
echo "  ${COMPOSE_CMD} restart mcp-gateway"
echo "  ${COMPOSE_CMD} down && ${COMPOSE_CMD} up -d"
echo ""

if [[ "$ALL_HEALTHY" == "true" ]]; then
  echo -e "${GREEN}✅  All 6 containers healthy. MCP Gateway is live on port 6000!${NC}"
else
  echo -e "${YELLOW}⚠️  Some containers not yet healthy. Run: ${COMPOSE_CMD} logs --tail=50${NC}"
fi
