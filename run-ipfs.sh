#!/bin/bash
#
# IPFS Storage Docker Runner
#
# Starts the IPFS Kubo node in Docker with optional SSL/TLS
# via the ssl-manager base image and HAProxy auto-registration.
#
# Usage:
#   ./run-ipfs.sh                                      # No SSL (self-signed, direct ports)
#   ./run-ipfs.sh --domain ipfs.example.com             # SSL with HAProxy
#   ./run-ipfs.sh --domain ipfs.example.com --no-haproxy  # SSL with direct ports
#   ./run-ipfs.sh --domain ipfs.example.com --ssl-email admin@example.com
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── App identity (set BEFORE sourcing run-lib.sh) ────────────────────────────
CONTAINER_NAME="${CONTAINER_NAME:-ipfs-kubo}"
IMAGE_NAME="${IPFS_IMAGE:-ipfs-storage:latest}"
APP_TITLE="IPFS Storage Service"

# ─── App networking ───────────────────────────────────────────────────────────
APP_NET="${APP_NET:-ipfs-net}"
# run-lib.sh mounts DATA_VOLUME at /data; we use a scratch volume for that
# and mount the real IPFS data at /data/ipfs via app_docker_args()
IPFS_DATA_VOLUME="${IPFS_DATA_VOLUME:-ipfs-data}"
DATA_VOLUME="${DATA_VOLUME:-ipfs-scratch}"
HEALTH_PORT=5001                       # IPFS API — first port to come up
SSL_CHECK_PORT=443                     # HTTPS gateway — verify after SSL setup
SSL_HTTPS_PORT="${SSL_HTTPS_PORT:-443}"
APP_HTTP_PORT="${APP_HTTP_PORT:-9080}"  # HTTP gateway exposed via ssl-manager proxy

# ─── App defaults ─────────────────────────────────────────────────────────────
DOMAIN="${DOMAIN:-localhost}"
PORT_SWARM="${PORT_SWARM:-4001}"
PORT_WSS="${PORT_WSS:-4003}"
PORT_HTTPS="${PORT_HTTPS:-443}"
PORT_HTTP="${PORT_HTTP:-9080}"

# Nostr pinner configuration
NOSTR_RELAYS="${NOSTR_RELAYS:-wss://nostr-relay.testnet.unicity.network,ws://unicity-nostr-relay-20250927-alb-1919039002.me-central-1.elb.amazonaws.com:8080}"
NOSTR_PRIVATE_KEY="${NOSTR_PRIVATE_KEY:-}"

# Sidecar configuration
STALE_THRESHOLD_SECONDS="${STALE_THRESHOLD_SECONDS:-15}"
CHAIN_VALIDATION_MODE="${CHAIN_VALIDATION_MODE:-strict}"

# Pin list (optional, only needed on first run)
PIN_LIST="${PIN_LIST:-}"

# ─── Source the ssl-manager run library ───────────────────────────────────────
if [ ! -f "${SCRIPT_DIR}/run-lib.sh" ]; then
    echo "ERROR: run-lib.sh not found in ${SCRIPT_DIR}" >&2
    echo "Download it from https://github.com/unicitynetwork/ssl-manager" >&2
    exit 1
fi
source "${SCRIPT_DIR}/run-lib.sh"

# ═══════════════════════════════════════════════════════════════════════════════
# App-specific hooks
# ═══════════════════════════════════════════════════════════════════════════════

app_parse_args() {
    case "$1" in
        --port-swarm)           require_arg "$1" "${2:-}"; PORT_SWARM="$2";  return 2 ;;
        --port-wss)             require_arg "$1" "${2:-}"; PORT_WSS="$2";    return 2 ;;
        --port-http)            require_arg "$1" "${2:-}"; PORT_HTTP="$2";   return 2 ;;
        --nostr-relays)         require_arg "$1" "${2:-}"; NOSTR_RELAYS="$2"; return 2 ;;
        --nostr-private-key)    require_arg "$1" "${2:-}"; NOSTR_PRIVATE_KEY="$2"; return 2 ;;
        --pin-list)             require_arg "$1" "${2:-}"; PIN_LIST="$2";    return 2 ;;
        --stale-threshold)      require_arg "$1" "${2:-}"; STALE_THRESHOLD_SECONDS="$2"; return 2 ;;
        --chain-validation)     require_arg "$1" "${2:-}"; CHAIN_VALIDATION_MODE="$2"; return 2 ;;
        *)                      return 0 ;;
    esac
}

app_validate() {
    validate_port "PORT_SWARM" "$PORT_SWARM"
    validate_port "PORT_WSS" "$PORT_WSS"
    validate_port "PORT_HTTP" "$PORT_HTTP"
}

app_env_args() {
    echo "-e DOMAIN=${SSL_DOMAIN:-$DOMAIN}"
    echo "-e IPFS_PROFILE=server"
    echo "-e NOSTR_RELAYS=$NOSTR_RELAYS"
    echo "-e STALE_THRESHOLD_SECONDS=$STALE_THRESHOLD_SECONDS"
    echo "-e CHAIN_VALIDATION_MODE=$CHAIN_VALIDATION_MODE"
    if [ -n "$NOSTR_PRIVATE_KEY" ]; then echo "-e NOSTR_PRIVATE_KEY=$NOSTR_PRIVATE_KEY"; fi
}

app_port_args() {
    echo "-p ${PORT_SWARM}:4001/tcp"
    echo "-p ${PORT_SWARM}:4001/udp"
    echo "-p ${PORT_WSS}:4003"
    echo "-p ${PORT_HTTPS}:443"
    echo "-p ${PORT_HTTP}:9080"
}

app_docker_args() {
    # Each flag on its own line — _read_docker_args splits on first space per line
    echo "-v ${IPFS_DATA_VOLUME}:/data/ipfs"

    # Mount pin list if provided
    if [ -n "$PIN_LIST" ] && [ -f "$PIN_LIST" ]; then
        echo "-v $(realpath "$PIN_LIST"):/data/pin-list.txt:ro"
    fi

    # Ulimits for IPFS connection handling
    echo "--ulimit nofile=65536:65536"

    # Auto-populate HAProxy extra ports for IPFS protocols
    if [ "$USE_HAPROXY" = true ] && [ -n "$HAPROXY_HOST" ] && [ -z "$EXTRA_PORTS" ]; then
        EXTRA_PORTS='[{"listen":4001,"target":4001,"mode":"tcp"},{"listen":4003,"target":4003,"mode":"tcp"},{"listen":9080,"target":9080,"mode":"http"}]'
        echo "-e EXTRA_PORTS=$EXTRA_PORTS"
    fi
}

app_print_config() {
    echo "  Nostr:      ${NOSTR_RELAYS%%,*}..."
    if [ -n "$PIN_LIST" ]; then echo "  Pin list:   $PIN_LIST"; fi
}

app_help() {
    cat <<'APPHELP'
IPFS Configuration:
  --port-swarm <port>        Swarm TCP/UDP port (default: 4001)
  --port-wss <port>          WSS port (default: 4003)
  --port-http <port>         HTTP gateway port (default: 9080)
  --pin-list <file>          Initial pin list file (CIDs, one per line)

Nostr Pinner:
  --nostr-relays <urls>      Comma-separated Nostr relay URLs
  --nostr-private-key <key>  Nostr private key for announcements

Sidecar:
  --stale-threshold <secs>   IPNS staleness threshold (default: 15)
  --chain-validation <mode>  Chain validation mode: strict|log_only (default: strict)
APPHELP
}

app_health_check() {
    local container="$1"

    # 1. IPFS daemon responding
    local peer_id
    peer_id=$(docker exec "$container" curl -sf -X POST http://127.0.0.1:5001/api/v0/id 2>/dev/null | jq -r '.ID' 2>/dev/null)
    if [ -n "$peer_id" ] && [ "$peer_id" != "null" ]; then
        echo "pass:IPFS daemon: ${peer_id:0:16}..."
    else
        echo "fail:IPFS API not responding"
    fi

    # 2. nginx running
    if docker exec "$container" pgrep -x nginx >/dev/null 2>&1; then
        echo "pass:nginx running"
    else
        echo "fail:nginx not running"
    fi

    # 3. Swarm peers
    local peers
    peers=$(docker exec "$container" curl -sf -X POST http://127.0.0.1:5001/api/v0/swarm/peers 2>/dev/null | jq '.Peers | length' 2>/dev/null)
    if [ -n "$peers" ] && [ "$peers" != "null" ] && [ "$peers" -gt 0 ] 2>/dev/null; then
        echo "pass:Swarm peers: $peers connected"
    else
        echo "warn:No swarm peers yet (may still be connecting)"
    fi

    # 4. Nostr pinner running
    if docker exec "$container" pgrep -f nostr_pinner >/dev/null 2>&1; then
        echo "pass:Nostr pinner running"
    else
        echo "warn:Nostr pinner not running"
    fi
}

app_summary() {
    echo ""
    echo "Endpoints:"
    if [ "$USE_HAPROXY" = true ] && [ -n "$HAPROXY_HOST" ] && [ -n "$SSL_DOMAIN" ]; then
        echo "  Gateway:   https://$SSL_DOMAIN/ipfs/<CID>"
        echo "  WSS:       /dns4/$SSL_DOMAIN/tcp/4003/wss"
        echo "  Swarm:     $SSL_DOMAIN:4001 (via HAProxy)"
        echo "  HTTP:      $SSL_DOMAIN:9080 (via HAProxy)"
    else
        echo "  Gateway:   https://localhost:$PORT_HTTPS/ipfs/<CID>"
        echo "  WSS:       /dns4/${SSL_DOMAIN:-localhost}/tcp/$PORT_WSS/wss"
        echo "  Swarm:     localhost:$PORT_SWARM"
        echo "  HTTP:      localhost:$PORT_HTTP"
    fi
    echo ""
    echo "  Pins:  docker exec \"$CONTAINER_NAME\" ipfs pin ls --type=recursive"
    echo "  Peers: docker exec \"$CONTAINER_NAME\" ipfs swarm peers"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Run
# ═══════════════════════════════════════════════════════════════════════════════
ssl_manager_run "$@"
