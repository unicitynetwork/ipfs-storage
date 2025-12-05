#!/bin/bash
set -euo pipefail

# IPFS Storage Service Entrypoint
# Validates SSL, initializes IPFS, configures nginx, starts supervisord

echo "========================================"
echo "  IPFS Storage Service"
echo "  Domain: ${DOMAIN}"
echo "========================================"

# ==========================================
# SSL Certificate Validation
# ==========================================

validate_ssl() {
    if [[ "${DEV_MODE}" == "true" ]]; then
        echo "[INFO] DEV_MODE=true - Skipping SSL validation"

        # Generate self-signed cert for development
        if [[ ! -f "${SSL_CERT_PATH}" ]]; then
            echo "[INFO] Generating self-signed certificate for development..."
            mkdir -p "$(dirname "${SSL_CERT_PATH}")" "$(dirname "${SSL_KEY_PATH}")"
            openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
                -keyout "${SSL_KEY_PATH}" \
                -out "${SSL_CERT_PATH}" \
                -subj "/CN=${DOMAIN}" \
                2>/dev/null
            echo "[INFO] Self-signed certificate generated"
        fi
        return 0
    fi

    echo "[INFO] Validating SSL certificates..."

    if [[ ! -f "${SSL_CERT_PATH}" ]]; then
        echo "[ERROR] SSL certificate not found: ${SSL_CERT_PATH}"
        echo "[ERROR] Please mount Let's Encrypt certificates or set DEV_MODE=true"
        exit 1
    fi

    if [[ ! -f "${SSL_KEY_PATH}" ]]; then
        echo "[ERROR] SSL private key not found: ${SSL_KEY_PATH}"
        echo "[ERROR] Please mount Let's Encrypt certificates or set DEV_MODE=true"
        exit 1
    fi

    # Validate certificate matches key
    CERT_MODULUS=$(openssl x509 -noout -modulus -in "${SSL_CERT_PATH}" 2>/dev/null | openssl md5)
    KEY_MODULUS=$(openssl rsa -noout -modulus -in "${SSL_KEY_PATH}" 2>/dev/null | openssl md5)

    if [[ "${CERT_MODULUS}" != "${KEY_MODULUS}" ]]; then
        echo "[ERROR] SSL certificate and key do not match"
        exit 1
    fi

    echo "[INFO] SSL certificates validated successfully"
}

# ==========================================
# IPFS Initialization
# ==========================================

init_ipfs() {
    export IPFS_PATH=/data/ipfs

    if [[ ! -f "${IPFS_PATH}/config" ]]; then
        echo "[INFO] Initializing IPFS repository..."

        # Initialize with server profile
        su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs /usr/local/bin/ipfs init --profile=${IPFS_PROFILE}"

        echo "[INFO] IPFS repository initialized"
    else
        echo "[INFO] IPFS repository already exists"
    fi

    # Configure IPFS for WebSocket support
    echo "[INFO] Configuring IPFS for WebSocket transport..."
    /usr/local/bin/configure-ipfs.sh
}

# ==========================================
# nginx Configuration
# ==========================================

configure_nginx() {
    echo "[INFO] Rendering nginx configuration..."

    # Use envsubst to render template
    envsubst '${DOMAIN} ${SSL_CERT_PATH} ${SSL_KEY_PATH}' \
        < /etc/nginx/nginx.conf.template \
        > /etc/nginx/nginx.conf

    # Validate nginx configuration
    if ! nginx -t 2>/dev/null; then
        echo "[ERROR] nginx configuration is invalid"
        nginx -t
        exit 1
    fi

    echo "[INFO] nginx configuration ready"
}

# ==========================================
# Main
# ==========================================

main() {
    # Step 1: Validate SSL
    validate_ssl

    # Step 2: Initialize IPFS
    init_ipfs

    # Step 3: Configure nginx
    configure_nginx

    # Step 4: Display connection info
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

    # Step 5: Start supervisord
    exec /usr/bin/supervisord -c /etc/supervisord.conf
}

main "$@"
