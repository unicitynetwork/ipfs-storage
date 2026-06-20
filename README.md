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
| `ANNOUNCE_INTERVAL` | 0 | Re-announcement interval in seconds (0 = use probability, e.g., 30 for testing) |
| `ANNOUNCE_PROBABILITY` | 0.000277778 | Probability per second (~1/3600 = once per hour avg) |
| `NOSTR_PRIVATE_KEY` | (generated) | Hex private key for signing announcements (persists identity) |

### Instant-Pin Cache (Write-Through)

The sidecar holds CAR/block bytes durably between submit and Kubo
blockstore registration. This closes the "publish-then-immediately-read"
window where Kubo has accepted a CAR but hasn't yet made it retrievable
by CID — the dominant source of cross-device IPFS races for the
sphere-sdk integration tests (see issue
[#6](https://github.com/unicitynetwork/ipfs-storage/issues/6)).

#### How it works

1. A client POSTs raw bytes to `POST /sidecar/submit?cid=<cid>`. The
   sidecar writes them atomically to `SIDECAR_CACHE_DIR/<cid[:2]>/<cid>`
   and records a `pending` row in the `instant_pin_cache` SQLite table.
   The 200 response is returned **immediately** — no Kubo round-trip on
   the write path.
2. Reads via `/api/v0/block/get?arg=<cid>` and `/ipfs/<cid>` are routed
   to the sidecar first by nginx (with a short timeout). On cache hit
   the bytes are served from disk; on miss / non-raw-bytes Accept,
   nginx falls back to the Kubo gateway transparently.
3. A background reconciler (every `SIDECAR_CACHE_RECONCILE_INTERVAL`
   seconds) tries to push each `pending` blob to Kubo via
   `/api/v0/block/put` + `/api/v0/pin/add`. Once Kubo confirms via
   `/api/v0/block/stat`, the row flips to `in-kubo` and the disk blob
   is deleted (SQLite metadata kept for audit).
4. The cache is bounded by total bytes AND entry count. LRU eviction
   targets only `in-kubo` rows; **pending rows are never silently
   evicted** because the publisher believes they're durable. When full
   of unconfirmed entries, new submits get HTTP `503 Service
   Unavailable` so the writer back-pressures.

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIDECAR_CACHE_ENABLED` | `true` | Kill switch — set to `false` to skip schema migration and disable all `/sidecar/*` endpoints |
| `SIDECAR_CACHE_DIR` | `/data/ipfs/sidecar-cache` | Directory for blob bytes (auto-created) |
| `SIDECAR_CACHE_MAX_BYTES` | `1073741824` (1 GiB) | Total cache budget across pending + in-kubo rows |
| `SIDECAR_CACHE_MAX_ENTRIES` | `100000` | Entry cap (whichever cap hits first) |
| `SIDECAR_CACHE_MAX_BLOB_BYTES` | `33554432` (32 MiB) | Per-submission size limit; oversize returns HTTP 413 |
| `SIDECAR_CACHE_RECONCILE_INTERVAL` | `5` | Seconds between reconciler iterations |
| `SIDECAR_CACHE_PROMOTION_TIMEOUT` | `86400` (24 h) | Pending rows older than this trigger operator WARN logs (never silently aged out) |
| `SIDECAR_CACHE_KUBO_TIMEOUT` | `30` | Per-call timeout for the reconciler's Kubo requests |

#### Scope and limitations

- The cache works at the **block** granularity: the bytes you POST under
  a CID are the bytes the cache serves back for that CID. For payloads
  uploaded via `/api/v0/add?cid-version=1` that fit in a single Kubo
  block (default chunk size 256 KiB, which covers all current
  sphere-sdk UXF fragments), the gateway-fetched bytes
  (`/ipfs/<cid>` with `Accept: application/octet-stream`) and the
  sidecar-cached bytes match exactly. For multi-block UnixFS files the
  sidecar cannot reconstruct the dag traversal — clients that need
  reads on multi-block files must fall through to the Kubo gateway.
- The cache is a **window-closer**, not a replacement for Kubo: the
  publisher's primary write path (e.g., `/api/v0/add`) should continue
  to be the source of truth. The sidecar `/sidecar/submit` call is an
  additive instant-availability hint.

#### Operator endpoints

- `GET /sidecar/cache-stats` — JSON snapshot of pending/confirmed
  counts, byte usage, and lifetime promotion counters.
- `GET /metrics` — existing endpoint now includes an `instant_pin_cache`
  object with the same data.

#### Running the tests

```bash
cd nostr-pinner
pip install -r requirements-dev.txt
pytest tests/
```

(Run from a Python 3.12 environment; in CI we use the same Docker image
that builds the sidecar.)

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
