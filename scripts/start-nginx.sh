#!/bin/bash
set -euo pipefail

# Start nginx after IPFS is ready
# Waits for IPFS API to be available before starting

echo "[nginx] Waiting for IPFS to be ready..."

# Wait for IPFS API to be available
for i in {1..60}; do
    if curl -s http://127.0.0.1:5001/api/v0/id >/dev/null 2>&1; then
        echo "[nginx] IPFS API is ready"
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "[nginx] WARNING: IPFS API not ready after 60 seconds, starting anyway"
    fi
    sleep 2
done

# Get and display peer ID
PEER_ID=$(curl -s http://127.0.0.1:5001/api/v0/id 2>/dev/null | grep -o '"ID":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
echo "[nginx] IPFS Peer ID: ${PEER_ID}"
echo "[nginx] WSS Multiaddr: /dns4/${DOMAIN}/tcp/4003/wss/p2p/${PEER_ID}"

echo "[nginx] Starting nginx..."

# Start nginx in foreground
exec nginx -g "daemon off;"
