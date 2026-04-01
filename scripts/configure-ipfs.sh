#!/bin/bash
set -euo pipefail

# Configure IPFS for WebSocket transport and WSS announcement
# Run as ipfs user

export IPFS_PATH=/data/ipfs
IPFS_CMD=/usr/local/bin/ipfs

echo "[IPFS] Configuring WebSocket transport..."

# Enable WebSocket transport
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.Transports.Network.Websocket true"

# Configure Swarm addresses
# - TCP/4001: Standard libp2p
# - UDP/4001: QUIC transport
# - TCP/4002: WebSocket for browser clients
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Addresses.Swarm '[
  \"/ip4/0.0.0.0/tcp/4001\",
  \"/ip6/::/tcp/4001\",
  \"/ip4/0.0.0.0/udp/4001/quic-v1\",
  \"/ip4/0.0.0.0/udp/4001/quic-v1/webtransport\",
  \"/ip4/0.0.0.0/tcp/4002/ws\"
]'"

# Announce WSS address (via nginx TLS proxy)
if [[ "${DOMAIN}" != "localhost" ]]; then
    echo "[IPFS] Setting WSS announce address for ${DOMAIN}..."
    su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Addresses.AppendAnnounce '[
      \"/dns4/${DOMAIN}/tcp/4003/wss\"
    ]'"
fi

# Configure API to allow connections from Docker network
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json API.HTTPHeaders.Access-Control-Allow-Origin '[\"*\"]'"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json API.HTTPHeaders.Access-Control-Allow-Methods '[\"PUT\", \"POST\", \"GET\"]'"

# Bind API to all interfaces (within container)
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config Addresses.API /ip4/0.0.0.0/tcp/5001"

# Bind Gateway to all interfaces (within container)
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config Addresses.Gateway /ip4/0.0.0.0/tcp/8080"

# Enable relay client and service with resource caps
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.RelayClient.Enabled true"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.RelayService '{
  \"Enabled\": true,
  \"ConnectionDurationLimit\": \"2m0s\",
  \"ConnectionDataLimit\": 131072,
  \"ReservationTTL\": \"60m\",
  \"MaxReservations\": 64,
  \"MaxCircuits\": 16,
  \"BufferSize\": 4096,
  \"MaxReservationsPerIP\": 8,
  \"MaxReservationsPerASN\": 32
}'"

# Connection manager: cap connections with graceful trimming
# LowWater/HighWater ratio kept gentle (75%) to avoid connection storm churn
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ConnMgr.Type '\"basic\"'"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ConnMgr.LowWater 96"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ConnMgr.HighWater 128"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ConnMgr.GracePeriod '\"2m\"'"

# Add Unicity bootstrap peers
echo "[IPFS] Adding Unicity bootstrap peers..."
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs2.dyndns.org/tcp/4001/p2p/12D3KooWLNi5NDPPHbrfJakAQqwBqymYTTwMQXQKEWuCrJNDdmfh" || true
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs2.dyndns.org/tcp/4003/wss/p2p/12D3KooWLNi5NDPPHbrfJakAQqwBqymYTTwMQXQKEWuCrJNDdmfh" || true
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs3.dyndns.org/tcp/4001/p2p/12D3KooWQ4aujVE4ShLjdusNZBdffq3TbzrwT2DuWZY9H1Gxhwn6" || true
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs3.dyndns.org/tcp/4003/wss/p2p/12D3KooWQ4aujVE4ShLjdusNZBdffq3TbzrwT2DuWZY9H1Gxhwn6" || true

# === RESOURCE MANAGER: cap system-wide resources ===
# Kubo 0.19+ removed Swarm.ResourceMgr.Limits — use libp2p-resource-limit-overrides.json instead
echo "[IPFS] Configuring resource manager limits..."
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ResourceMgr.Enabled true"

cat > /data/ipfs/libp2p-resource-limit-overrides.json << 'RESLIMITS'
{
  "System": {
    "Conns": 512,
    "ConnsInbound": 256,
    "ConnsOutbound": 256,
    "Streams": 2048,
    "StreamsInbound": 1024,
    "StreamsOutbound": 1024,
    "FD": 4096,
    "Memory": 1610612736
  }
}
RESLIMITS
chown ipfs:ipfs /data/ipfs/libp2p-resource-limit-overrides.json

# === REPROVIDER: use "roots" strategy ===
# "roots" announces root blocks of all DAGs (not every sub-block), keeping child blocks
# discoverable via DAG traversal while reducing DHT announcement volume vs "all"
# Using Provide.* (Reprovider.* deprecated since Kubo 0.38)
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Provide.Strategy '\"roots\"'"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Provide.DHT.Interval '\"22h\"'"

# === PERFORMANCE OPTIMIZATIONS FOR FAST HTTP RESPONSES ===
echo "[IPFS] Applying performance optimizations..."

# Disable AcceleratedDHTClient — it aggressively crawls the full routing table,
# generating thousands of DNS lookups and connections. Standard DHT server mode
# is sufficient for IPNS/CID propagation and serving the network.
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Routing.AcceleratedDHTClient false"

# IPNS cache: 1024 balances cold-start coverage vs DHT refresh load (was 4096)
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Ipns.ResolveCacheSize 1024"

# Enable gateway routing API exposure
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Gateway.ExposeRoutingAPI true"

# Optimize datastore bloom filter for faster reads
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Datastore.BloomFilterSize 1048576"

echo "[IPFS] Performance optimizations applied"

echo "[IPFS] Configuration complete"

# Show current Swarm addresses
echo "[IPFS] Swarm addresses:"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config Addresses.Swarm" | head -10
