# IPFS Storage Service with WSS Support
# Based on ssl-manager for automatic SSL certificate management and HAProxy integration

FROM ghcr.io/unicitynetwork/ssl-manager:latest

# ssl-manager is based on debian:trixie-slim and already includes:
#   certbot, openssl, curl, jq, netcat-openbsd, python3, procps, ca-certificates
#
# IPFS-specific dependencies:
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    nginx \
    libnginx-mod-stream \
    supervisor \
    gettext-base \
    python3-pip \
    python3-dev \
    # Build dependencies for secp256k1 Python package
    build-essential \
    autoconf \
    automake \
    libtool \
    libsecp256k1-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install IPFS Kubo
ARG IPFS_VERSION=v0.39.0
RUN cd /tmp && \
    ARCH=$(dpkg --print-architecture) && \
    curl -fsSL "https://dist.ipfs.tech/kubo/${IPFS_VERSION}/kubo_${IPFS_VERSION}_linux-${ARCH}.tar.gz" -o kubo.tar.gz && \
    tar -xzf kubo.tar.gz && \
    mv kubo/ipfs /usr/local/bin/ && \
    rm -rf /tmp/* && \
    ipfs --version

# Create ipfs user and directories
RUN useradd -m -d /data/ipfs -u 1000 -s /bin/bash ipfs && \
    mkdir -p /data/ipfs && \
    mkdir -p /data/ipfs/.config/ipfs/denylists && \
    chown -R ipfs:ipfs /data/ipfs

# Create nginx directories
RUN mkdir -p /run/nginx /var/log/nginx /var/cache/nginx/ipfs && \
    chown -R www-data:www-data /run/nginx /var/log/nginx /var/cache/nginx

# Install nostr-pinner Python dependencies
COPY nostr-pinner/requirements.txt /tmp/nostr-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/nostr-requirements.txt && \
    rm /tmp/nostr-requirements.txt

# Copy nostr-pinner script
COPY nostr-pinner/nostr_pinner.py /usr/local/bin/nostr_pinner.py
RUN chmod 644 /usr/local/bin/nostr_pinner.py

# Copy configuration files
COPY config/supervisord.conf /etc/supervisord.conf
COPY config/nginx.conf.template /etc/nginx/nginx.conf.template
COPY scripts/ /usr/local/bin/
RUN chmod 755 /usr/local/bin/*.sh

# Volumes — declared for documentation; explicit -v mounts are always used
# Do NOT use VOLUME ["/data/ipfs"] — it creates anonymous volumes that silently
# mask mount failures, risking data loss of 690GB+ of pinned IPFS content.
VOLUME ["/etc/letsencrypt"]

# Ports
# 4001 - IPFS Swarm (TCP/UDP)
# 4002 - WebSocket (internal, proxied by nginx)
# 4003 - WSS (TLS-terminated by nginx)
# 5001 - IPFS API (internal)
# 8080 - IPFS Gateway (internal)
# 443  - HTTPS Gateway (via nginx)
# 9080 - HTTP Gateway (exposed)
# Port 80 is already EXPOSE'd by ssl-manager base image
EXPOSE 4001/tcp 4001/udp 4003 443 9080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD /usr/local/bin/healthcheck.sh

# Environment variables
ENV DOMAIN=localhost \
    IPFS_PROFILE=server \
    IPFS_LOGGING=info \
    # Nostr pinner configuration (Unicity relays)
    NOSTR_RELAYS="wss://nostr-relay.testnet.unicity.network,ws://unicity-nostr-relay-20250927-alb-1919039002.me-central-1.elb.amazonaws.com:8080" \
    IPFS_API_URL="http://127.0.0.1:5001" \
    PIN_KIND="30078" \
    LOG_LEVEL="INFO" \
    RECONNECT_DELAY="10" \
    PIN_TIMEOUT="300" \
    # Propagation sidecar configuration
    MAX_PINS_PER_SECOND="100" \
    HTTP_PORT="9081" \
    DB_PATH="/data/ipfs/propagation.db" \
    NODE_NAME="ipfs-node" \
    NOSTR_PRIVATE_KEY="" \
    ANNOUNCE_INTERVAL="0" \
    ANNOUNCE_PROBABILITY="0.000277778"

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
