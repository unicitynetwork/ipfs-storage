# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IPFS storage backend for the Unicity agentic network. Provides pinning and hosting services for Sphere app with:
- IPFS Kubo node with WebSocket (WS) and secure WebSocket (WSS) support
- nginx TLS termination for browser clients
- Nostr-based CID announcement system for decentralized pinning

## Build Commands

```bash
# Build Docker image
make build

# Run in production (requires SSL certs at /etc/letsencrypt/live/DOMAIN/)
make run DOMAIN=unicity-ipfs1.dyndns.org

# Run in development mode (self-signed cert)
make dev

# View logs
make logs

# Show peer info and endpoints
make info

# Pin a CID manually
make pin CID=Qm...

# List all pins
make pins

# Docker Compose (includes nostr-pinner)
make compose-up
make compose-down
make compose-logs
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│              ipfs-storage Container                  │
│  ┌─────────┐    ┌─────────┐                         │
│  │  nginx  │───▶│  IPFS   │◀─── nostr-pinner        │
│  │ :4003   │    │  Kubo   │     (sidecar)           │
│  └─────────┘    └─────────┘                         │
└─────────────────────────────────────────────────────┘
```

**Ports:**
- 4001: IPFS Swarm (TCP/UDP)
- 4003: WSS (TLS-terminated by nginx)
- 443: HTTPS Gateway
- 9080: HTTP Gateway
- 5001: IPFS API (internal only)

## Key Files

- `Dockerfile` - Single container with IPFS + nginx + supervisord
- `docker-compose.yml` - Multi-container setup with nostr-pinner
- `config/nginx.conf.template` - TLS termination config
- `config/supervisord.conf` - Process management
- `scripts/entrypoint.sh` - Container initialization
- `scripts/configure-ipfs.sh` - WebSocket transport setup
- `nostr-pinner/nostr_pinner.py` - Python service for Nostr pin requests

## Nostr Pin Protocol

CID announcements use kind 30078 (NIP-78 app-specific data):

```json
{
  "kind": 30078,
  "tags": [
    ["d", "ipfs-pin"],
    ["cid", "bafybeig..."],
    ["name", "optional-filename"]
  ],
  "content": ""
}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| DOMAIN | localhost | Domain for SSL and WSS announcements |
| DEV_MODE | false | Use self-signed certificate |
| NOSTR_RELAYS | wss://relay.damus.io,... | Comma-separated relay URLs |
| PIN_KIND | 30078 | Nostr event kind for pin requests |
