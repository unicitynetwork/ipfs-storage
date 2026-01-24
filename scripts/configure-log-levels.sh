#!/bin/bash
# Configure IPFS log levels to reduce noise
# Runs after IPFS daemon starts

set -euo pipefail

export IPFS_PATH=/data/ipfs

echo "[LOG-CONFIG] Waiting for IPFS API to be ready..."

# Wait for IPFS API to be available (longer wait since IPFS takes time to start)
max_wait=120
waited=0
while [ $waited -lt $max_wait ]; do
    if /usr/local/bin/ipfs id >/dev/null 2>&1; then
        echo "[LOG-CONFIG] IPFS API ready after ${waited}s"
        break
    fi
    sleep 2
    waited=$((waited + 2))
done

if ! /usr/local/bin/ipfs id >/dev/null 2>&1; then
    echo "[LOG-CONFIG] ERROR: IPFS API not available after ${max_wait} seconds"
    exit 1
fi

echo "[LOG-CONFIG] Configuring log levels..."

# Suppress noisy bitswap logs (protocol negotiation failures, send failures)
/usr/local/bin/ipfs log level bitswap error
/usr/local/bin/ipfs log level bitswap/client/msgq error
/usr/local/bin/ipfs log level bitswap/bsnet error

# Suppress peer identification failures
/usr/local/bin/ipfs log level net/identify error

# Suppress relay service noise
/usr/local/bin/ipfs log level relay error

# Suppress WebRTC transport noise (connection failures)
/usr/local/bin/ipfs log level webrtc-transport-pion error

# Suppress swarm dial failures (DNS resolution failures)
/usr/local/bin/ipfs log level swarm2 error

echo "[LOG-CONFIG] Log levels configured successfully"
