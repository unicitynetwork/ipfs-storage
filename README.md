# IPFS Storage Service

IPFS storage backend for the agentic Internet. Used to pin and store objects for Unicity agents.

A Docker-based IPFS pinning and hosting service for the Unicity network. Provides TCP/IP and WSS endpoints for browser clients and peers, with integrated Nostr-based automatic pinning.

## Features

- **Single Container** - All services managed by supervisord
- IPFS Kubo node with WebSocket transport
- nginx TLS termination for WSS (port 4003) and HTTPS gateway (port 443)
- Integrated Nostr pinner service for automatic CID pinning via relay subscriptions
- Persistent storage via Docker volumes
- Development mode with self-signed certificates

## Quick Start

### Build the Image

```bash
make build
```

### Development Mode (Self-Signed Certificate)

For local development without SSL certificates:

```bash
make dev
```

This starts the container with a self-signed certificate on `localhost`. Access:
- HTTP Gateway: http://localhost:9080/ipfs/<CID>
- WSS: wss://localhost:4003 (browser will warn about self-signed cert)

### Production Mode (With SSL Certificates)

For production with Let's Encrypt or other SSL certificates:

```bash
make run \
  SSL_CERT=/etc/letsencrypt/live/yourdomain.com/fullchain.pem \
  SSL_KEY=/etc/letsencrypt/live/yourdomain.com/privkey.pem \
  DOMAIN=yourdomain.com
```

Or using docker-compose:

```bash
make compose-up \
  SSL_CERT=/etc/letsencrypt/live/yourdomain.com/fullchain.pem \
  SSL_KEY=/etc/letsencrypt/live/yourdomain.com/privkey.pem \
  DOMAIN=yourdomain.com
```

### HAProxy Mode (Behind Reverse Proxy)

To run behind HAProxy without exposing ports 80/443 publicly:

```bash
make haproxy-up \
  SSL_CERT=/etc/letsencrypt/live/yourdomain.com/fullchain.pem \
  SSL_KEY=/etc/letsencrypt/live/yourdomain.com/privkey.pem \
  DOMAIN=yourdomain.com
```

This mode:
- Joins the `haproxy-net` Docker network
- Does NOT expose ports 80/443 to the host (HAProxy handles external HTTPS)
- Container handles its own TLS (HAProxy uses SSL passthrough)
- Container name `ipfs-kubo` must match the haproxy.cfg backend configuration

**Prerequisites:**
1. HAProxy must be running on `haproxy-net` network
2. Create the network if it doesn't exist: `docker network create haproxy-net`
3. HAProxy must be configured to route traffic to container `ipfs-kubo`

**Verification:**
```bash
# Check container is on haproxy-net
docker inspect ipfs-kubo | grep -A 5 haproxy-net

# Test connectivity from HAProxy
docker exec haproxy ping ipfs-kubo

# Check HAProxy backend status
docker logs haproxy 2>&1 | grep ipfs-kubo
```

The container includes:
- **IPFS Kubo** - IPFS node with WebSocket transport
- **nginx** - TLS termination proxy for WSS and HTTPS
- **Nostr pinner** - Python service subscribing to Nostr relays for pin requests
- **Pin worker** - Processes initial pin list at startup

### Initial Pin List (Optional)

You can provide a text file with CIDs to pin at startup. Since pins are persisted in the Docker volume, this only needs to be done once:

```bash
# Create a pin list file
cat > cids.txt << 'EOF'
# Initial CIDs to pin
# Comments and empty lines are ignored

bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi
bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku
QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG
EOF

# Run with pin list
make run \
  SSL_CERT=/etc/letsencrypt/live/yourdomain.com/fullchain.pem \
  SSL_KEY=/etc/letsencrypt/live/yourdomain.com/privkey.pem \
  DOMAIN=yourdomain.com \
  PIN_LIST=./cids.txt
```

**Note:** Pins persist in the Docker volume. Subsequent restarts without `PIN_LIST` will retain all previously pinned content.

## Port Reference

| Port | Protocol | Purpose | HAProxy Mode |
|------|----------|---------|--------------|
| 4001 | TCP/UDP | IPFS Swarm (peer connections) | Exposed |
| 4003 | TCP | WSS (TLS-terminated WebSocket for browsers) | Exposed |
| 443 | TCP | HTTPS Gateway | Not exposed (HAProxy) |
| 9080 | TCP | HTTP Gateway | Exposed |
| 5001 | - | IPFS API (internal only, not exposed) | Internal |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DOMAIN` | localhost | Domain for SSL and WSS announcements |
| `SSL_CERT` | - | Path to SSL certificate (fullchain.pem) |
| `SSL_KEY` | - | Path to SSL private key (privkey.pem) |
| `PIN_LIST` | - | Path to initial CID pin list file (optional) |
| `DEV_MODE` | false | Use self-signed certificate |

### Port Configuration

Override default ports to avoid conflicts with other services:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT_SWARM` | 4001 | IPFS Swarm port (TCP/UDP) |
| `PORT_WSS` | 4003 | WebSocket Secure port |
| `PORT_HTTPS` | 443 | HTTPS Gateway port (set empty to disable with `make run`) |
| `PORT_HTTP` | 9080 | HTTP Gateway port |

**Examples:**

```bash
# Use different HTTPS port to avoid conflict
make run SSL_CERT=... SSL_KEY=... DOMAIN=... PORT_HTTPS=8443

# Disable HTTPS entirely (only works with make run, not docker-compose)
make run SSL_CERT=... SSL_KEY=... DOMAIN=... PORT_HTTPS=

# Use custom ports for everything
make compose-up SSL_CERT=... SSL_KEY=... DOMAIN=... \
  PORT_SWARM=14001 PORT_WSS=14003 PORT_HTTPS=8443 PORT_HTTP=19080
```

**Note:** To completely disable a port with docker-compose, edit `docker-compose.yml` directly or use `make run` instead.

### Nostr Pinner Configuration

The Nostr pinner is integrated into the container and listens for pin requests on Nostr relays.

| Variable | Default | Description |
|----------|---------|-------------|
| `NOSTR_RELAYS` | Unicity testnet relays | Comma-separated relay URLs (uses Unicity relays by default) |
| `PIN_KIND` | 30078 | Nostr event kind for pin requests |
| `LOG_LEVEL` | INFO | Pinner log level (DEBUG, INFO, WARNING, ERROR) |
| `RECONNECT_DELAY` | 10 | Seconds to wait before reconnecting to relay |
| `PIN_TIMEOUT` | 300 | Pin operation timeout in seconds |

## Make Commands

```bash
make help           # Show all available commands

# Container Management
make build          # Build Docker image
make run            # Run with SSL certificates
make dev            # Run in development mode
make stop           # Stop and remove container
make restart        # Restart container
make logs           # View container logs
make shell          # Open shell in container

# IPFS Operations
make info           # Show peer info and endpoints
make health         # Check service health
make pins           # List all pinned CIDs
make pin CID=<cid>  # Pin a specific CID
make unpin CID=<cid># Unpin a CID
make peers          # List connected peers
make gc             # Run garbage collection

# Docker Compose
make compose-up     # Start container via docker-compose
make compose-down   # Stop container via docker-compose
make compose-logs   # View logs

# HAProxy Mode (behind reverse proxy)
make haproxy-up     # Start behind HAProxy (no public 80/443)
make haproxy-down   # Stop HAProxy mode stack
make haproxy-logs   # View HAProxy mode logs

# Cleanup
make clean          # Remove container and volume (DESTRUCTIVE)
```

## Examples

### Pin Content Manually

```bash
# Pin a CID
make pin CID=bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi

# List all pins
make pins

# Remove a pin
make unpin CID=bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi
```

### Access Content via Gateway

```bash
# HTTPS (production)
curl https://yourdomain.com/ipfs/<CID>

# HTTP (development)
curl http://localhost:9080/ipfs/<CID>
```

### Connect from Browser (Helia)

```javascript
import { createHelia } from 'helia';
import { webSockets } from '@libp2p/websockets';

const helia = await createHelia({
  libp2p: {
    transports: [webSockets()],
    // Connect to your node
    bootstrap: ['/dns4/yourdomain.com/tcp/4003/wss/p2p/<PEER_ID>']
  }
});
```

Get your peer ID with `make info`.

## Nostr Pin Protocol

The pinner listens for NIP-78 app-specific events (kind 30078) with the following format:

```json
{
  "kind": 30078,
  "tags": [
    ["d", "ipfs-pin"],
    ["cid", "bafybeig..."]
  ],
  "content": ""
}
```

Any Nostr client can publish these events to request pinning. The Sphere app publishes them automatically after storing content to IPFS.

## Architecture

```
                    Internet
                       |
         +-------------+-------------+
         |             |             |
         v             v             v
     :4001         :4003          :443
    (Swarm)        (WSS)        (HTTPS)
         |             |             |
         |        +----+----+        |
         |        |  nginx  |--------+
         |        |  (TLS)  |
         |        +----+----+
         |             |
         v             v
    +--------------------------------------+
    |              IPFS Kubo               |
    |       :4001 :4002/ws :5001/api       |
    +------------------+-------------------+
                       |
                       v
    +--------------------------------------+
    |           Nostr Pinner               |
    |   (subscribes to kind 30078 events)  |---- Nostr Relays
    +--------------------------------------+
                       |
                       v
                 +-----------+
                 | ipfs-data |
                 |  (volume) |
                 +-----------+

    All services managed by supervisord in a single container
```

### Supervisord Process Hierarchy

| Priority | Process | Type | Description |
|----------|---------|------|-------------|
| 100 | ipfs | continuous | IPFS Kubo daemon |
| 200 | nginx | continuous | TLS termination proxy |
| 300 | pin-worker | oneshot | Initial pin list processor |
| 400 | nostr-pinner | continuous | Nostr relay listener |

## Troubleshooting

### Container won't start

Check SSL certificate paths:
```bash
ls -la /etc/letsencrypt/live/yourdomain.com/
```

### WSS connection fails

Verify nginx is running:
```bash
make shell
supervisorctl status
```

### Nostr pinner not connecting

Check logs and verify relay URLs:
```bash
make logs
# Look for "Connected to" or error messages from nostr-pinner
```

### Peers not connecting

Check swarm addresses:
```bash
make info
```

### View detailed logs

```bash
make logs
```

### Check individual service status

```bash
make shell
supervisorctl status
```

## License

MIT
