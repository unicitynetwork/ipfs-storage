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

# Enable relay for browser clients
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.RelayClient.Enabled true"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.RelayService.Enabled true"

# Set connection limits for server profile
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ConnMgr.LowWater 100"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config --json Swarm.ConnMgr.HighWater 400"

# Add Unicity bootstrap peers
echo "[IPFS] Adding Unicity bootstrap peers..."
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs2.dyndns.org/tcp/4001/p2p/12D3KooWLNi5NDPPHbrfJakAQqwBqymYTTwMQXQKEWuCrJNDdmfh" || true
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs2.dyndns.org/tcp/4003/wss/p2p/12D3KooWLNi5NDPPHbrfJakAQqwBqymYTTwMQXQKEWuCrJNDdmfh" || true
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs3.dyndns.org/tcp/4001/p2p/12D3KooWQ4aujVE4ShLjdusNZBdffq3TbzrwT2DuWZY9H1Gxhwn6" || true
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD bootstrap add /dns4/unicity-ipfs3.dyndns.org/tcp/4003/wss/p2p/12D3KooWQ4aujVE4ShLjdusNZBdffq3TbzrwT2DuWZY9H1Gxhwn6" || true

echo "[IPFS] Configuration complete"

# Show current Swarm addresses
echo "[IPFS] Swarm addresses:"
su -s /bin/sh ipfs -c "IPFS_PATH=/data/ipfs $IPFS_CMD config Addresses.Swarm" | head -10
