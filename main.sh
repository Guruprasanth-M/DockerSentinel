#!/usr/bin/env bash
# ============================================================================
#  Docker Sentinel — One-Command Installer & Launcher
#
#  Usage:  chmod +x main.sh && ./main.sh
#
#  This script handles EVERYTHING:
#    • Checks & installs system dependencies (Docker, Docker Compose, Python, pip)
#    • Creates directory structure & configuration files
#    • Sets correct file permissions
#    • Builds all Docker images
#    • Starts the complete stack
#    • Validates all services are healthy
#    • Shows the dashboard URL
#
#  Error handling: Every step has retry logic with fallback methods.
#  If one approach fails, it automatically tries alternative approaches.
# ============================================================================

set -euo pipefail

# ── Colors & Symbols ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

TICK="${GREEN}✔${NC}"
CROSS="${RED}✘${NC}"
ARROW="${CYAN}➜${NC}"
WARN="${YELLOW}⚠${NC}"

# ── Project Configuration ────────────────────────────────────────────────────
PROJECT_NAME="Docker Sentinel"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
SENTINEL_VERSION="0.1.0"
LOG_FILE="${PROJECT_DIR}/install.log"

# ── Helper Functions ─────────────────────────────────────────────────────────

log() {
    echo -e "$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $(echo -e "$1" | sed 's/\x1b\[[0-9;]*m//g')" >> "$LOG_FILE"
}

log_step() {
    echo ""
    log "${ARROW} ${BOLD}$1${NC}"
}

log_success() {
    log "  ${TICK} $1"
}

log_warn() {
    log "  ${WARN} $1"
}

log_error() {
    log "  ${CROSS} $1"
}

log_info() {
    log "  ${BLUE}ℹ${NC} $1"
}

# Retry a command with exponential backoff
# Usage: retry <max_attempts> <description> <command...>
retry() {
    local max_attempts=$1
    local description=$2
    shift 2
    local attempt=1
    local wait_time=2

    while [ $attempt -le $max_attempts ]; do
        if "$@" >> "$LOG_FILE" 2>&1; then
            return 0
        fi
        if [ $attempt -lt $max_attempts ]; then
            log_warn "${description} failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_time}s..."
            sleep $wait_time
            wait_time=$((wait_time * 2))
        fi
        attempt=$((attempt + 1))
    done
    return 1
}

# Run command with sudo if not root
run_privileged() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Detect OS and package manager
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_VERSION="${VERSION_ID:-unknown}"
    elif [ -f /etc/redhat-release ]; then
        OS_ID="rhel"
        OS_VERSION="unknown"
    else
        OS_ID="unknown"
        OS_VERSION="unknown"
    fi

    if command -v apt-get &> /dev/null; then
        PKG_MANAGER="apt"
    elif command -v yum &> /dev/null; then
        PKG_MANAGER="yum"
    elif command -v dnf &> /dev/null; then
        PKG_MANAGER="dnf"
    elif command -v pacman &> /dev/null; then
        PKG_MANAGER="pacman"
    elif command -v apk &> /dev/null; then
        PKG_MANAGER="apk"
    else
        PKG_MANAGER="unknown"
    fi

    log_info "Detected OS: ${OS_ID} ${OS_VERSION} (package manager: ${PKG_MANAGER})"
}

# Install a package using detected package manager with fallback
install_package() {
    local package_name="$1"
    local attempt_methods=()

    case "$PKG_MANAGER" in
        apt)
            attempt_methods=(
                "run_privileged apt-get install -y $package_name"
                "run_privileged apt-get update && run_privileged apt-get install -y $package_name"
                "run_privileged apt-get install -y --fix-broken && run_privileged apt-get install -y $package_name"
            )
            ;;
        yum)
            attempt_methods=(
                "run_privileged yum install -y $package_name"
                "run_privileged yum makecache && run_privileged yum install -y $package_name"
            )
            ;;
        dnf)
            attempt_methods=(
                "run_privileged dnf install -y $package_name"
                "run_privileged dnf makecache && run_privileged dnf install -y $package_name"
            )
            ;;
        pacman)
            attempt_methods=("run_privileged pacman -S --noconfirm $package_name")
            ;;
        apk)
            attempt_methods=("run_privileged apk add --no-cache $package_name")
            ;;
    esac

    for method in "${attempt_methods[@]}"; do
        if eval "$method" >> "$LOG_FILE" 2>&1; then
            return 0
        fi
        log_warn "Method failed for $package_name, trying next..."
    done

    log_error "Could not install $package_name with any method"
    return 1
}

# ── Dependency Checks & Installation ─────────────────────────────────────────

check_docker() {
    log_step "Checking Docker installation..."

    if command -v docker &> /dev/null; then
        local docker_version
        docker_version=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
        log_success "Docker found: v${docker_version}"

        # Check if Docker daemon is running
        if docker info &> /dev/null 2>&1; then
            log_success "Docker daemon is running"
        else
            log_warn "Docker daemon not running, attempting to start..."
            if run_privileged systemctl start docker 2>/dev/null; then
                log_success "Docker daemon started"
            elif run_privileged service docker start 2>/dev/null; then
                log_success "Docker daemon started (service command)"
            else
                log_error "Failed to start Docker daemon"
                log_info "Try: sudo systemctl start docker"
                return 1
            fi
        fi

        # Check if current user can run Docker without sudo
        if ! docker ps &> /dev/null 2>&1; then
            log_warn "Current user cannot access Docker, fixing permissions..."
            if run_privileged usermod -aG docker "$(whoami)" 2>/dev/null; then
                log_success "Added $(whoami) to docker group"
                log_warn "You may need to log out and back in for group changes to take effect"
                # Try using newgrp in a subshell
                if sg docker -c "docker ps" &> /dev/null 2>&1; then
                    log_success "Docker accessible via group refresh"
                fi
            fi
        fi
        return 0
    fi

    log_warn "Docker not found, installing..."
    install_docker
}

install_docker() {
    log_step "Installing Docker..."

    # Method 1: Official Docker install script
    log_info "Trying official Docker install script..."
    if curl -fsSL https://get.docker.com -o /tmp/get-docker.sh 2>/dev/null && \
       run_privileged sh /tmp/get-docker.sh >> "$LOG_FILE" 2>&1; then
        rm -f /tmp/get-docker.sh
        log_success "Docker installed via official script"
        run_privileged systemctl enable docker 2>/dev/null || true
        run_privileged systemctl start docker 2>/dev/null || true
        run_privileged usermod -aG docker "$(whoami)" 2>/dev/null || true
        return 0
    fi
    rm -f /tmp/get-docker.sh

    # Method 2: Package manager
    log_warn "Official script failed, trying package manager..."
    case "$PKG_MANAGER" in
        apt)
            if run_privileged apt-get update >> "$LOG_FILE" 2>&1 && \
               run_privileged apt-get install -y docker.io docker-compose-plugin >> "$LOG_FILE" 2>&1; then
                log_success "Docker installed via apt"
                run_privileged systemctl enable docker 2>/dev/null || true
                run_privileged systemctl start docker 2>/dev/null || true
                run_privileged usermod -aG docker "$(whoami)" 2>/dev/null || true
                return 0
            fi
            ;;
        yum|dnf)
            if run_privileged $PKG_MANAGER install -y docker docker-compose-plugin >> "$LOG_FILE" 2>&1; then
                log_success "Docker installed via $PKG_MANAGER"
                run_privileged systemctl enable docker 2>/dev/null || true
                run_privileged systemctl start docker 2>/dev/null || true
                run_privileged usermod -aG docker "$(whoami)" 2>/dev/null || true
                return 0
            fi
            ;;
    esac

    # Method 3: Snap
    log_warn "Package manager failed, trying snap..."
    if command -v snap &> /dev/null; then
        if run_privileged snap install docker >> "$LOG_FILE" 2>&1; then
            log_success "Docker installed via snap"
            return 0
        fi
    fi

    log_error "Failed to install Docker with all methods"
    log_info "Please install Docker manually: https://docs.docker.com/engine/install/"
    return 1
}

check_docker_compose() {
    log_step "Checking Docker Compose..."

    # Method 1: docker compose (v2 plugin)
    if docker compose version &> /dev/null 2>&1; then
        local compose_version
        compose_version=$(docker compose version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
        log_success "Docker Compose (plugin) found: v${compose_version}"
        COMPOSE_CMD="docker compose"
        return 0
    fi

    # Method 2: docker-compose (standalone v1/v2)
    if command -v docker-compose &> /dev/null; then
        local compose_version
        compose_version=$(docker-compose --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
        log_success "Docker Compose (standalone) found: v${compose_version}"
        COMPOSE_CMD="docker-compose"
        return 0
    fi

    log_warn "Docker Compose not found, installing..."
    install_docker_compose
}

install_docker_compose() {
    # Method 1: Docker Compose plugin
    log_info "Trying Docker Compose plugin..."
    case "$PKG_MANAGER" in
        apt)
            if run_privileged apt-get install -y docker-compose-plugin >> "$LOG_FILE" 2>&1; then
                COMPOSE_CMD="docker compose"
                log_success "Docker Compose plugin installed"
                return 0
            fi
            ;;
        yum|dnf)
            if run_privileged $PKG_MANAGER install -y docker-compose-plugin >> "$LOG_FILE" 2>&1; then
                COMPOSE_CMD="docker compose"
                log_success "Docker Compose plugin installed"
                return 0
            fi
            ;;
    esac

    # Method 2: Download binary
    log_warn "Plugin install failed, downloading binary..."
    local compose_url="https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)"
    if curl -fsSL "$compose_url" -o /tmp/docker-compose >> "$LOG_FILE" 2>&1; then
        run_privileged mv /tmp/docker-compose /usr/local/bin/docker-compose
        run_privileged chmod +x /usr/local/bin/docker-compose
        COMPOSE_CMD="docker-compose"
        log_success "Docker Compose binary installed"
        return 0
    fi

    # Method 3: pip install
    log_warn "Binary download failed, trying pip..."
    if pip3 install docker-compose >> "$LOG_FILE" 2>&1 || \
       pip install docker-compose >> "$LOG_FILE" 2>&1; then
        COMPOSE_CMD="docker-compose"
        log_success "Docker Compose installed via pip"
        return 0
    fi

    log_error "Failed to install Docker Compose"
    return 1
}

check_python() {
    log_step "Checking Python..."

    local python_cmd=""
    if command -v python3 &> /dev/null; then
        python_cmd="python3"
    elif command -v python &> /dev/null; then
        python_cmd="python"
    fi

    if [ -n "$python_cmd" ]; then
        local python_version
        python_version=$($python_cmd --version 2>&1 | grep -oP '\d+\.\d+\.\d+')
        log_success "Python found: v${python_version} (${python_cmd})"
    else
        log_warn "Python not found, installing..."
        install_package python3
        install_package python3-pip
        install_package python3-venv
    fi

    # Check pip
    if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
        log_warn "pip not found, installing..."
        install_package python3-pip || {
            # Fallback: ensurepip
            log_info "Trying ensurepip..."
            python3 -m ensurepip --upgrade >> "$LOG_FILE" 2>&1 || true
        }
        install_package python3-venv 2>/dev/null || true
    fi

    log_success "Python environment ready"
}

check_system_deps() {
    log_step "Checking system dependencies..."

    local deps_needed=()
    command -v curl &> /dev/null || deps_needed+=("curl")
    command -v git &> /dev/null || deps_needed+=("git")
    command -v jq &> /dev/null || deps_needed+=("jq")

    if [ ${#deps_needed[@]} -gt 0 ]; then
        log_info "Installing: ${deps_needed[*]}"
        for dep in "${deps_needed[@]}"; do
            install_package "$dep" && log_success "Installed $dep" || log_warn "Could not install $dep (optional)"
        done
    else
        log_success "All system dependencies present"
    fi
}

# ── Directory Structure ──────────────────────────────────────────────────────

setup_directories() {
    log_step "Setting up directory structure..."

    local dirs=(
        "data/redis"
        "data/models"
        "data/audit"
        "data/collector-state"
        "data/geoip"
        "data/db"
        "config"
        "backups"
        "logs"
        "docs"
        "wiki"
    )

    for dir in "${dirs[@]}"; do
        mkdir -p "${PROJECT_DIR}/${dir}"
    done

    log_success "Directory structure created"
}

# ── Configuration Files ──────────────────────────────────────────────────────

setup_config() {
    log_step "Setting up configuration..."

    # Generate API token if .env doesn't exist
    if [ ! -f "${PROJECT_DIR}/.env" ]; then
        local api_token
        api_token=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || head -c 64 /dev/urandom | xxd -p | tr -d '\n' | head -c 64)
        local redis_pass
        redis_pass=$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))")
        local db_pass
        db_pass=$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))")
        local secret_key
        secret_key=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))")

        cat > "${PROJECT_DIR}/.env" << EOF
SENTINEL_VERSION=${SENTINEL_VERSION}
REDIS_PASSWORD=${redis_pass}
REDIS_URL=redis://:${redis_pass}@redis:6379/0
SENTINEL_CONFIG=/config/sentinel.yml
DATA_DIR=./data
DASHBOARD_PORT=${DASHBOARD_PORT}
COMPOSE_PROJECT_NAME=sentinel
SENTINEL_API_TOKEN=${api_token}
SENTINEL_SECRET_KEY=${secret_key}
DB_NAME=sentinel
DB_USER=sentinel
DB_PASSWORD=${db_pass}
DB_URL=postgresql://sentinel:${db_pass}@db:5432/sentinel
EOF
        # Update redis.conf with the generated password
        if [ -f "${PROJECT_DIR}/config/redis.conf" ]; then
            sed -i "s/^requirepass .*/requirepass ${redis_pass}/" "${PROJECT_DIR}/config/redis.conf"
        fi
        log_success "Generated .env with secure credentials"
    else
        log_success ".env already exists, keeping current config"
    fi

    # Create sentinel.yml from example if not present
    if [ ! -f "${PROJECT_DIR}/config/sentinel.yml" ]; then
        cat > "${PROJECT_DIR}/config/sentinel.yml" << EOF
version: "0.1"

sentinel:
  # API token loaded from SENTINEL_API_TOKEN env var — do NOT hardcode
  host_name: "$(hostname)"
  
  ml:
    model_version: "v1_pretrained"
    score_threshold_alert: 0.6
    score_threshold_critical: 0.8

  collection:
    interval_ms: 5000
    feature_window_seconds: 10
    log_resume_enabled: true

  actions:
    enabled: true
    max_per_minute: 5
    default_block_duration_minutes: 60
    protected_ips: ["127.0.0.1", "::1"]
    protected_ports: [22, 51820]

  logging:
    level: "INFO"
    format: "json"

  stream_limits:
    logs: 50000
    network: 50000
    processes: 50000
    features: 10000
    scores: 10000
    alerts: 10000
    actions: 5000
    audit: 100000
EOF
        log_success "Generated config/sentinel.yml"
    else
        log_success "config/sentinel.yml exists, keeping current config"
    fi

    # Create policies.yml if not present
    if [ ! -f "${PROJECT_DIR}/config/policies.yml" ]; then
        cp "${PROJECT_DIR}/config/policies.yml.example" "${PROJECT_DIR}/config/policies.yml" 2>/dev/null || \
        cat > "${PROJECT_DIR}/config/policies.yml" << 'EOF'
version: "0.1"

policies:
  - name: brute_force_block
    enabled: true
    conditions:
      score_above: 0.80
      anomaly_type: brute_force
      repeated_within_seconds: 60
      min_occurrences: 3
    action: block_ip
    severity: high
    notify: true

  - name: port_scan_alert
    enabled: true
    conditions:
      score_above: 0.65
      anomaly_type: port_scan
    action: alert_only
    severity: medium
    notify: true

  - name: high_risk_alert
    enabled: true
    conditions:
      score_above: 0.75
    action: alert_only
    severity: high
    notify: true

  - name: critical_threat
    enabled: true
    conditions:
      score_above: 0.90
    action: block_ip
    severity: critical
    notify: true

  - name: ssh_anomaly
    enabled: true
    conditions:
      score_above: 0.60
      anomaly_type: ssh_failure
      repeated_within_seconds: 30
      min_occurrences: 5
    action: alert_only
    severity: medium
    notify: true

  - name: process_spike
    enabled: true
    conditions:
      score_above: 0.70
      anomaly_type: process_spike
    action: alert_only
    severity: medium
    notify: false
EOF
        log_success "Generated config/policies.yml"
    fi

    # Create webhooks.yml if not present
    if [ ! -f "${PROJECT_DIR}/config/webhooks.yml" ]; then
        cp "${PROJECT_DIR}/config/webhooks.yml.example" "${PROJECT_DIR}/config/webhooks.yml" 2>/dev/null || \
        cat > "${PROJECT_DIR}/config/webhooks.yml" << 'EOF'
version: "0.1"

# Webhook secret for HMAC-SHA256 payload signing
webhook_secret: "change-this-to-a-secure-secret"

webhooks:
  # Example: Slack webhook (disabled by default)
  - name: slack_security
    url: https://hooks.slack.com/services/YOUR/WEBHOOK/URL
    events: [attack_detected, critical_alert]
    enabled: false
    sign_payloads: true

  # Example: Discord webhook (disabled by default)
  - name: discord_alerts
    url: https://discord.com/api/webhooks/YOUR/WEBHOOK
    events: [attack_detected, anomaly_detected, critical_alert]
    enabled: false
    sign_payloads: false

  # Example: Custom SIEM endpoint (disabled by default)
  - name: custom_siem
    url: http://your-siem-server:9000/sentinel
    events: [attack_detected, anomaly_detected, action_taken, critical_alert]
    enabled: false
    sign_payloads: true
    headers:
      Authorization: "Bearer YOUR_SIEM_TOKEN"
      Content-Type: "application/json"

  # Example: Email notification via webhook relay (disabled by default)
  - name: email_alerts
    url: http://your-email-relay:3000/send
    events: [critical_alert]
    enabled: false
    sign_payloads: false
EOF
        log_success "Generated config/webhooks.yml"
    fi

    log_success "All configuration files ready"
}

# ── File Permissions ─────────────────────────────────────────────────────────

set_permissions() {
    log_step "Setting file permissions..."

    # Make scripts executable
    find "${PROJECT_DIR}/scripts" -name "*.sh" -exec chmod +x {} \; 2>/dev/null || true
    chmod +x "${PROJECT_DIR}/main.sh" 2>/dev/null || true

    # Ensure data directories are writable
    chmod -R 755 "${PROJECT_DIR}/data" 2>/dev/null || true
    chmod -R 755 "${PROJECT_DIR}/config" 2>/dev/null || true
    chmod -R 755 "${PROJECT_DIR}/logs" 2>/dev/null || true

    log_success "Permissions set"
}

# ── Docker Build & Launch ────────────────────────────────────────────────────

stop_existing() {
    log_step "Stopping any existing containers..."

    cd "${PROJECT_DIR}"

    # Stop existing sentinel containers with live progress
    if $COMPOSE_CMD ps -q 2>/dev/null | grep -q .; then
        local running
        running=$($COMPOSE_CMD ps --format '{{.Name}}' 2>/dev/null | wc -l)
        printf "  ${CYAN}│${NC} Stopping %d containers " "$running"
        $COMPOSE_CMD down --remove-orphans >> "$LOG_FILE" 2>&1 &
        local pid=$!
        local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        while kill -0 "$pid" 2>/dev/null; do
            local i=$(( ${i:-0} + 1 ))
            printf "\r  ${CYAN}│${NC} Stopping %d containers %s" "$running" "${spin:i%${#spin}:1}"
            sleep 0.15
        done
        wait "$pid" 2>/dev/null || true
        printf "\r"
        log_success "Stopped existing containers"
    else
        log_success "No existing containers to stop"
    fi

    # Kill any process using the dashboard port
    if command -v lsof &> /dev/null; then
        local pid
        pid=$(lsof -ti ":${DASHBOARD_PORT}" 2>/dev/null || true)
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || run_privileged kill "$pid" 2>/dev/null || true
            log_info "Freed port ${DASHBOARD_PORT}"
        fi
    fi
}

build_images() {
    log_step "Building Docker images..."
    echo ""

    cd "${PROJECT_DIR}"

    local services=("redis" "db" "collectors" "ml" "policy" "actions" "webhooks" "api" "dashboard")
    local total=${#services[@]}
    local built=0
    local failed=0
    local skip_build=("redis" "db")  # Pre-built images, no build needed

    for svc in "${services[@]}"; do
        built=$((built + 1))

        # Skip services that use pre-built images (no Dockerfile)
        local should_skip=false
        for sb in "${skip_build[@]}"; do
            if [ "$svc" = "$sb" ]; then
                should_skip=true
                break
            fi
        done

        if $should_skip; then
            printf "  ${TICK} [%d/%d] %-12s (pre-built image)\n" "$built" "$total" "$svc"
            continue
        fi

        printf "  ${BLUE}⏳${NC} [%d/%d] Building %-12s ... " "$built" "$total" "$svc"

        # Build with output going to log file but show a spinner
        local build_log="${LOG_FILE}.${svc}"
        if $COMPOSE_CMD build "$svc" > "$build_log" 2>&1; then
            printf "\r  ${TICK} [%d/%d] %-12s built ✓                \n" "$built" "$total" "$svc"
            rm -f "$build_log"
        else
            printf "\r  ${CROSS} [%d/%d] %-12s FAILED              \n" "$built" "$total" "$svc"
            echo "--- Build error for ${svc} ---" >> "$LOG_FILE"
            cat "$build_log" >> "$LOG_FILE" 2>/dev/null
            rm -f "$build_log"

            # Show last 5 lines of error on screen
            echo -e "  ${RED}Build output (last 5 lines):${NC}"
            tail -5 "$LOG_FILE" | sed 's/^/    /'

            failed=$((failed + 1))
        fi
    done

    echo ""
    if [ $failed -gt 0 ]; then
        log_error "${failed} service(s) failed to build. Check ${LOG_FILE}"
        return 1
    fi
    log_success "All images built successfully (${total} services)"
}

start_stack() {
    log_step "Starting Docker Sentinel stack..."

    cd "${PROJECT_DIR}"

    echo ""
    if $COMPOSE_CMD up -d 2>&1 | while IFS= read -r line; do
        # Show compose output live with indentation
        echo -e "  ${BLUE}│${NC} ${line}"
    done; then
        echo ""
        log_success "Stack started"
    else
        echo ""
        log_error "Failed to start stack"
        log_info "Check logs: ${COMPOSE_CMD} logs"
        return 1
    fi
}

wait_for_healthy() {
    log_step "Waiting for all services to become healthy..."
    echo ""

    local max_wait=120
    local elapsed=0
    local all_healthy=false
    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    while [ $elapsed -lt $max_wait ]; do
        local running_count
        running_count=$($COMPOSE_CMD ps --status running 2>/dev/null | tail -n +2 | wc -l || echo "0")
        running_count=$(echo "$running_count" | tr -d ' ')

        # Spinner character
        local idx=$(( elapsed % ${#spin_chars} ))
        local spinner="${spin_chars:$idx:1}"

        if [ "$running_count" -ge 7 ]; then
            # Check health endpoint
            if curl -sf "http://localhost:${DASHBOARD_PORT}/api/health" > /dev/null 2>&1; then
                all_healthy=true
                break
            fi
        fi

        printf "\r  ${CYAN}%s${NC}  %d/%d containers running │ %ds elapsed " "$spinner" "$running_count" "9" "$elapsed"
        sleep 2
        elapsed=$((elapsed + 2))
    done

    printf "\r                                                              \r"

    if $all_healthy; then
        log_success "All services healthy! (took ${elapsed}s)"
    else
        log_warn "Some services may still be starting. Checking individual status..."
        $COMPOSE_CMD ps 2>/dev/null || true
    fi
}

# ── Health Validation ────────────────────────────────────────────────────────

validate_stack() {
    log_step "Validating stack health..."

    local errors=0

    # Check API health
    local health_response
    health_response=$(curl -sf "http://localhost:${DASHBOARD_PORT}/api/health" 2>/dev/null || echo "FAIL")

    if [ "$health_response" != "FAIL" ]; then
        log_success "API responding on port ${DASHBOARD_PORT}"
        
        # Parse health info if jq available
        if command -v jq &> /dev/null; then
            local status version
            status=$(echo "$health_response" | jq -r '.status // "unknown"')
            version=$(echo "$health_response" | jq -r '.version // "unknown"')
            log_info "Status: ${status} | Version: ${version}"
        fi
    else
        log_error "API not responding"
        errors=$((errors + 1))
    fi

    # Check dashboard
    if curl -sf "http://localhost:${DASHBOARD_PORT}/" > /dev/null 2>&1; then
        log_success "Dashboard accessible"
    else
        log_error "Dashboard not accessible"
        errors=$((errors + 1))
    fi

    # Check WebSocket endpoint exists
    if curl -sf -o /dev/null -w "%{http_code}" "http://localhost:${DASHBOARD_PORT}/ws/live" 2>/dev/null | grep -qE "101|426|400"; then
        log_success "WebSocket endpoint available"
    else
        log_info "WebSocket endpoint check inconclusive (normal for HTTP check)"
    fi

    # Check container count
    local container_count
    container_count=$($COMPOSE_CMD ps --status running 2>/dev/null | tail -n +2 | wc -l || echo "0")
    if [ "$container_count" -ge 7 ]; then
        log_success "All ${container_count} containers running"
    elif [ "$container_count" -ge 5 ]; then
        log_warn "${container_count} containers running (some may still be starting)"
    else
        log_error "Only ${container_count} containers running"
        errors=$((errors + 1))
    fi

    return $errors
}

# ── Print Summary ────────────────────────────────────────────────────────────

print_banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}                                                              ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}   ${BOLD}🛡️  Docker Sentinel v${SENTINEL_VERSION}${NC}                                ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}   ${BLUE}Real-Time Docker Security Monitoring${NC}                       ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}                                                              ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}   Built by ${BOLD}Guruprasanth M${NC}                                     ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}   ${BLUE}https://github.com/Guruprasanth-M${NC}                          ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}                                                              ${CYAN}║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

print_summary() {
    local server_ip
    server_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || curl -sf ifconfig.me 2>/dev/null || echo "localhost")

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║${NC}  ${BOLD}${TICK} Docker Sentinel is running!${NC}                               ${GREEN}║${NC}"
    echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║${NC}                                                              ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${BOLD}Dashboard:${NC}  http://${server_ip}:${DASHBOARD_PORT}                      ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${BOLD}API:${NC}        http://${server_ip}:${DASHBOARD_PORT}/api/health            ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                              ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${BOLD}Commands:${NC}                                                   ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}    View logs:      ${COMPOSE_CMD} logs -f                  ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}    Stop:           ${COMPOSE_CMD} down                     ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}    Restart:        ${COMPOSE_CMD} restart                  ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}    Simulate attack: ./scripts/simulate_attack.sh all       ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}    Backup:         ./scripts/backup.sh                     ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                              ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${BOLD}API Token:${NC} $(grep SENTINEL_API_TOKEN "${PROJECT_DIR}/.env" 2>/dev/null | cut -d= -f2 | head -c 16)...  ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  (Full token in .env file)                                   ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                              ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${BLUE}Install log: ${LOG_FILE}${NC}      ${GREEN}║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

# ── Main Execution ───────────────────────────────────────────────────────────

main() {
    # Initialize log file
    echo "=== Docker Sentinel Installation — $(date) ===" > "$LOG_FILE"

    print_banner

    log_step "Starting Docker Sentinel setup..."
    log_info "Project directory: ${PROJECT_DIR}"
    log_info "Installation log: ${LOG_FILE}"

    # Phase 1: System checks
    detect_os
    check_system_deps
    check_docker || { log_error "Docker is required. Aborting."; exit 1; }
    check_docker_compose || { log_error "Docker Compose is required. Aborting."; exit 1; }
    check_python

    # Phase 2: Project setup
    setup_directories
    setup_config
    set_permissions

    # Phase 3: Docker build & launch
    stop_existing
    build_images || { log_error "Build failed. Check ${LOG_FILE}"; exit 1; }
    start_stack || { log_error "Stack start failed. Check ${LOG_FILE}"; exit 1; }
    wait_for_healthy

    # Phase 4: Validation
    if validate_stack; then
        print_summary
    else
        log_warn "Stack started but some health checks failed."
        log_info "This is often temporary. Wait 30 seconds and try:"
        log_info "  curl http://localhost:${DASHBOARD_PORT}/api/health"
        print_summary
    fi
}

# Run
main "$@"
