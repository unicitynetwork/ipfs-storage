#!/bin/bash
set -euo pipefail

# Start IPFS daemon with migration support
# Runs as ipfs user (via supervisord)

export IPFS_PATH=/data/ipfs
export IPFS_LOGGING="${IPFS_LOGGING:-info}"

echo "[IPFS] Starting IPFS daemon..."

# Wait for config to be ready
for i in {1..30}; do
    if [[ -f "${IPFS_PATH}/config" ]]; then
        break
    fi
    echo "[IPFS] Waiting for config file..."
    sleep 1
done

if [[ ! -f "${IPFS_PATH}/config" ]]; then
    echo "[IPFS] ERROR: Config file not found after 30 seconds"
    exit 1
fi

# Start the daemon
exec /usr/local/bin/ipfs daemon --migrate=true --enable-gc
