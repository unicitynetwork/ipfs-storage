#!/bin/bash
set -e

# IPFS Storage Service Entrypoint
# Integrates with ssl-manager for automatic SSL certificate management

# ---------------------------------------------------------------------------
# Signal handler for graceful shutdown
# ---------------------------------------------------------------------------
SHUTDOWN_REQUESTED=0

handle_signal() {
    local signal=$1
    echo "[entrypoint] Received $signal signal, initiating graceful shutdown..."
    SHUTDOWN_REQUESTED=1

    # Deregister from HAProxy if we were registered
    if [ -n "${SSL_DOMAIN:-}" ] && [ -n "${HAPROXY_HOST:-}" ]; then
        echo "[entrypoint] Deregistering from HAProxy..."
        haproxy-register unregister 2>/dev/null || true
    fi

    # Stop the HTTP reverse proxy (ssl-manager)
    if [ -f /tmp/.ssl-http-proxy.pid ]; then
        local proxy_pid
        proxy_pid=$(cat /tmp/.ssl-http-proxy.pid 2>/dev/null || true)
        if [ -n "$proxy_pid" ] && kill -0 "$proxy_pid" 2>/dev/null; then
            kill "$proxy_pid" 2>/dev/null || true
        fi
    fi

    # Stop the renewal loop
    if [ -f /tmp/.ssl-renew.pid ]; then
        local renew_pid
        renew_pid=$(cat /tmp/.ssl-renew.pid 2>/dev/null || true)
        if [ -n "$renew_pid" ] && kill -0 "$renew_pid" 2>/dev/null; then
            kill "$renew_pid" 2>/dev/null || true
        fi
    fi

    # Stop the TLS alias proxy
    if [ -n "$ALIAS_PROXY_PID" ] && kill -0 "$ALIAS_PROXY_PID" 2>/dev/null; then
        kill "$ALIAS_PROXY_PID" 2>/dev/null || true
    fi

    # Forward signal to supervisord (it will stop all managed processes)
    if [ -n "$SUPERVISOR_PID" ] && kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
        echo "[entrypoint] Forwarding $signal to supervisord (PID $SUPERVISOR_PID)..."
        kill -"$signal" "$SUPERVISOR_PID" 2>/dev/null || true

        local wait_count=0
        while kill -0 "$SUPERVISOR_PID" 2>/dev/null && [ $wait_count -lt 30 ]; do
            sleep 1
            wait_count=$((wait_count + 1))
        done

        if kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
            echo "[entrypoint] Supervisord did not exit gracefully, sending SIGKILL..."
            kill -9 "$SUPERVISOR_PID" 2>/dev/null || true
        fi
    fi

    echo "[entrypoint] Graceful shutdown complete"
    exit 0
}

trap 'handle_signal TERM' SIGTERM
trap 'handle_signal INT' SIGINT

# ---------------------------------------------------------------------------
# IPFS Initialization
# ---------------------------------------------------------------------------
init_ipfs() {
    export IPFS_PATH=/data/ipfs

    # Ensure data directory exists with correct ownership
    mkdir -p "${IPFS_PATH}"
    chown ipfs:ipfs "${IPFS_PATH}"

    # Fix ownership of critical IPFS repo files (may be corrupted by root operations)
    # Only fix specific files — chown -R on 690GB blocks/ would take minutes
    if [ -f "${IPFS_PATH}/config" ]; then
        chown ipfs:ipfs "${IPFS_PATH}/config" 2>/dev/null || true
        chown ipfs:ipfs "${IPFS_PATH}/datastore_spec" 2>/dev/null || true
        chown -R ipfs:ipfs "${IPFS_PATH}/datastore" 2>/dev/null || true
        chown -R ipfs:ipfs "${IPFS_PATH}/keystore" 2>/dev/null || true
    fi

    # Remove stale locks from previous unclean shutdown (e.g., SIGKILL)
    rm -f "${IPFS_PATH}/repo.lock" "${IPFS_PATH}/datastore/LOCK"

    if [ ! -f "${IPFS_PATH}/config" ]; then
        # Safety guard: refuse to initialize if volume contains existing IPFS data
        # at a different path (e.g., volume mounted at /data instead of /data/ipfs)
        if [ -f /data/config ] && [ -d /data/blocks ]; then
            echo "[entrypoint] FATAL: IPFS repo found at /data/ but not at /data/ipfs/"
            echo "[entrypoint] Volume appears to be mounted at /data instead of /data/ipfs"
            echo "[entrypoint] Refusing to initialize a new repo to protect existing data"
            echo "[entrypoint] Fix: mount the volume at /data/ipfs (e.g., -v ipfs-data:/data/ipfs)"
            exit 1
        fi

        echo "[entrypoint] Initializing IPFS repository..."
        su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs /usr/local/bin/ipfs init --profile=${IPFS_PROFILE:-server}"
        echo "[entrypoint] IPFS repository initialized"
    else
        echo "[entrypoint] IPFS repository already exists"
    fi

    echo "[entrypoint] Configuring IPFS..."
    /usr/local/bin/configure-ipfs.sh
}

# ---------------------------------------------------------------------------
# nginx Configuration
# ---------------------------------------------------------------------------
configure_nginx() {
    echo "[entrypoint] Rendering nginx configuration..."

    mkdir -p /var/cache/nginx/ipfs
    chown -R www-data:www-data /var/cache/nginx

    envsubst '${DOMAIN} ${SSL_CERT_PATH} ${SSL_KEY_PATH}' \
        < /etc/nginx/nginx.conf.template \
        > /etc/nginx/nginx.conf

    if ! nginx -t 2>/dev/null; then
        echo "[entrypoint] ERROR: nginx configuration is invalid"
        nginx -t
        exit 1
    fi

    echo "[entrypoint] nginx configuration ready"
}

# ===========================================================================
# Main execution
# ===========================================================================

echo "========================================"
echo "  IPFS Storage Service"
echo "  Domain: ${DOMAIN:-localhost}"
echo "========================================"

# Step 1: Run ssl-setup from ssl-manager base image
echo "[entrypoint] Running ssl-setup..."
ssl_setup_exit=0
/usr/local/bin/ssl-setup || ssl_setup_exit=$?

if [ $ssl_setup_exit -ne 0 ]; then
    if [ "${SSL_REQUIRED:-true}" = "true" ]; then
        echo "[entrypoint] ERROR: ssl-setup failed (exit code $ssl_setup_exit) and SSL_REQUIRED=true"
        echo "[entrypoint] Set SSL_REQUIRED=false to allow fallback to self-signed cert."
        exit $ssl_setup_exit
    else
        echo "[entrypoint] WARNING: ssl-setup failed (exit code $ssl_setup_exit) but SSL_REQUIRED=false"
        echo "[entrypoint] Continuing with fallback SSL configuration"
    fi
fi

# Step 2: Source SSL environment and set cert paths for nginx
if [ -f /tmp/.ssl-env ]; then
    . /tmp/.ssl-env
    echo "[entrypoint] SSL configured: cert=${SSL_CERT_FILE}, key=${SSL_KEY_FILE}"

    # Export paths for nginx template substitution
    export SSL_CERT_PATH="${SSL_CERT_FILE}"
    export SSL_KEY_PATH="${SSL_KEY_FILE}"

    if [ -f "${SSL_CERT_FILE}" ]; then
        EXPIRY=$(openssl x509 -enddate -noout -in "${SSL_CERT_FILE}" | cut -d= -f2)
        echo "[entrypoint] SSL certificate expires: ${EXPIRY}"
    fi

    if [ "${SSL_TEST_MODE:-}" = "true" ]; then
        echo "[entrypoint] WARNING: SSL_TEST_MODE is active -- using self-signed certificate"
    fi
else
    echo "[entrypoint] No SSL certificates from ssl-manager -- generating self-signed cert"
    SSL_CERT_PATH="/etc/ssl/certs/fullchain.pem"
    SSL_KEY_PATH="/etc/ssl/private/privkey.pem"
    export SSL_CERT_PATH SSL_KEY_PATH

    if [ ! -f "${SSL_CERT_PATH}" ]; then
        mkdir -p "$(dirname "${SSL_CERT_PATH}")" "$(dirname "${SSL_KEY_PATH}")"
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "${SSL_KEY_PATH}" \
            -out "${SSL_CERT_PATH}" \
            -subj "/CN=${DOMAIN:-localhost}" \
            2>/dev/null
        echo "[entrypoint] Self-signed certificate generated"
    fi
fi

# Step 3: Set DOMAIN from SSL_DOMAIN if available
if [ -n "${SSL_DOMAIN:-}" ]; then
    if [ "${DOMAIN:-localhost}" != "localhost" ] && [ "${DOMAIN}" != "${SSL_DOMAIN}" ]; then
        echo "[entrypoint] WARNING: DOMAIN=$DOMAIN differs from SSL_DOMAIN=$SSL_DOMAIN"
        echo "[entrypoint] Using SSL_DOMAIN for nginx server_name to match HAProxy registration"
    fi
    export DOMAIN="${SSL_DOMAIN}"
fi

# Step 4: Initialize IPFS
init_ipfs

# Step 5: Configure nginx
configure_nginx

# Step 6: Display connection info
echo ""
echo "========================================"
echo "  Starting Services"
echo "========================================"
echo ""
echo "  Swarm:    /ip4/<host>/tcp/4001"
echo "  WSS:      /dns4/${DOMAIN}/tcp/4003/wss"
echo "  Gateway:  https://${DOMAIN}:443/ipfs/<CID>"
echo "  HTTP:     http://<host>:9080/ipfs/<CID>"
echo ""
echo "========================================"

# Step 7: Start supervisord with signal forwarding
SUPERVISOR_PID=""
/usr/bin/supervisord -c /etc/supervisord.conf &
SUPERVISOR_PID=$!
echo "[entrypoint] Supervisord started (PID $SUPERVISOR_PID)"

# Step 7b: Start TLS alias proxy after supervisord (needs nginx on 443 to be ready)
ALIAS_PROXY_PID=""
if [ -f /tmp/.ssl-alias-proxy.pid ]; then
    # ssl-setup started the proxy too early (before nginx). Restart it now.
    old_pid=$(cat /tmp/.ssl-alias-proxy.pid 2>/dev/null)
    kill "$old_pid" 2>/dev/null || true
    sleep 2
    echo "[entrypoint] Restarting TLS alias proxy (waiting for nginx)..."
    # Wait for nginx to be ready on port 443
    for _i in $(seq 1 30); do
        if nc -z localhost 443 2>/dev/null; then break; fi
        sleep 2
    done
    python3 /usr/local/bin/ssl-alias-proxy &
    ALIAS_PROXY_PID=$!
    echo "[entrypoint] TLS alias proxy restarted (PID $ALIAS_PROXY_PID)"
elif [ -n "${SSL_DOMAIN_ALIASES:-}" ] && [ -f /tmp/.ssl-env ]; then
    # Aliases configured but proxy wasn't started by ssl-setup (e.g., SSL_REQUIRED=false fallback)
    for _i in $(seq 1 30); do
        if nc -z localhost 443 2>/dev/null; then break; fi
        sleep 2
    done
    python3 /usr/local/bin/ssl-alias-proxy &
    ALIAS_PROXY_PID=$!
    echo "[entrypoint] TLS alias proxy started (PID $ALIAS_PROXY_PID)"
fi

# Step 8: Watch for SSL renewal restart marker and supervisord exit
# ssl-renew touches /tmp/.ssl-renewal-restart when certs are renewed;
# we must reload nginx to pick up the new cert.
while true; do
    # Check for SSL cert renewal marker
    if [ -f /tmp/.ssl-renewal-restart ]; then
        rm -f /tmp/.ssl-renewal-restart
        echo "[entrypoint] SSL certificate renewed — reloading nginx"

        # Re-source cert paths (in case they changed)
        if [ -f /tmp/.ssl-env ]; then
            . /tmp/.ssl-env
            export SSL_CERT_PATH="${SSL_CERT_FILE}"
            export SSL_KEY_PATH="${SSL_KEY_FILE}"
        fi

        # Re-render nginx config with new cert paths and reload
        configure_nginx
        supervisorctl -c /etc/supervisord.conf signal HUP nginx 2>/dev/null || \
            nginx -s reload 2>/dev/null || true
        echo "[entrypoint] nginx reloaded with renewed certificate"
    fi

    # Check if supervisord is still running
    if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
        wait "$SUPERVISOR_PID" 2>/dev/null || true
        if [ $SHUTDOWN_REQUESTED -eq 0 ]; then
            echo "[entrypoint] Supervisord exited unexpectedly"
            exit 1
        fi
        break
    fi

    sleep 10
done
