#!/bin/bash
set -euo pipefail

# Health check for IPFS Storage Service
# Checks both IPFS daemon and nginx

# Check IPFS API (POST required in Kubo v0.39+)
if ! curl -sf -X POST http://127.0.0.1:5001/api/v0/id >/dev/null 2>&1; then
    echo "IPFS API is not responding"
    exit 1
fi

# Check nginx (via gateway)
if ! curl -sf http://127.0.0.1:8080/api/v0/version >/dev/null 2>&1; then
    # Gateway might not respond to this, try a simpler check
    if ! pgrep -x nginx >/dev/null 2>&1; then
        echo "nginx is not running"
        exit 1
    fi
fi

echo "healthy"
exit 0
