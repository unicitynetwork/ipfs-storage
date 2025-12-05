#!/bin/bash
# Pin List Worker
# Runs after IPFS starts to process initial pin list
# Called by supervisord as a oneshot process

set -euo pipefail

PIN_LIST_PATH="/data/pin-list.txt"
IPFS_API="/ip4/127.0.0.1/tcp/5001"
IPFS_CMD="/usr/local/bin/ipfs"

# Exit if no pin list provided
if [[ ! -f "${PIN_LIST_PATH}" ]]; then
    echo "[pin-worker] No pin list found at ${PIN_LIST_PATH}, skipping"
    exit 0
fi

echo "[pin-worker] Processing initial pin list..."

# Wait for IPFS API to be ready (up to 2 minutes)
echo "[pin-worker] Waiting for IPFS API..."
for i in {1..60}; do
    if $IPFS_CMD --api=${IPFS_API} id >/dev/null 2>&1; then
        echo "[pin-worker] IPFS API ready"
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "[pin-worker] ERROR: IPFS API not ready after 2 minutes"
        exit 1
    fi
    sleep 2
done

# Count CIDs to process
total=$(grep -cvE '^\s*(#|$)' "${PIN_LIST_PATH}" 2>/dev/null || echo "0")
echo "[pin-worker] Found ${total} CIDs to process"

# Process each CID
count=0
failed=0
while IFS= read -r line || [[ -n "$line" ]]; do
    # Trim whitespace
    cid=$(echo "$line" | xargs)

    # Skip empty lines and comments
    [[ -z "$cid" || "$cid" == \#* ]] && continue

    count=$((count + 1))
    echo "[pin-worker] (${count}/${total}) Pinning: ${cid}"

    # Pin the CID (without --progress for cleaner logs)
    if $IPFS_CMD --api=${IPFS_API} pin add "${cid}" 2>&1; then
        echo "[pin-worker] Pinned: ${cid}"
    else
        echo "[pin-worker] WARN: Failed to pin: ${cid}"
        failed=$((failed + 1))
    fi
done < "${PIN_LIST_PATH}"

echo "[pin-worker] Complete: ${count} processed, ${failed} failed"
