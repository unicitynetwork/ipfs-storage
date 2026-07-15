# Kubo Node Requirements for UXF Profile Module

**Date:** 2026-04-09
**Related PR:** sphere-sdk#105 (feature/uxf-packaging-format)
**Status:** One nginx change required, everything else already configured

---

## Current Status: Almost Ready

After reviewing the existing infrastructure (`configure-ipfs.sh`, `nginx.conf.template`), the Kubo node is **already configured** for most Profile requirements:

| Requirement | Status | Notes |
|---|---|---|
| WebSocket transport (:4002) | **Ready** | Configured in `configure-ipfs.sh` line 24 |
| WSS via nginx (:4003) | **Ready** | TLS termination in `nginx.conf.template` stream block |
| Circuit Relay v2 | **Ready** | Enabled in `configure-ipfs.sh` lines 46-57 with resource caps |
| `/api/v0/add` (legacy upload) | **Ready** | Proxied in nginx with 50M limit |
| `/ipfs/{CID}` (gateway fetch) | **Ready** | Proxied with 7-day cache |
| CORS headers | **Ready** | Configured in both Kubo API and nginx |
| Bootstrap peers (TCP + WSS) | **Ready** | Both formats in `configure-ipfs.sh` lines 68-71 |
| **`/api/v0/dag/put` (new DAG upload)** | **MISSING** | **Not proxied by nginx — returns 404** |

---

## Required Change: Add `/api/v0/dag/put` to nginx

The Profile module's `pinToIpfs()` uploads encrypted UXF CAR files via:
```
POST https://unicity-ipfs1.dyndns.org/api/v0/dag/put
Content-Type: multipart/form-data
Body: <encrypted CAR bytes>
```

This endpoint is currently NOT proxied from nginx to Kubo's API (:5001). It needs a location block in `nginx.conf.template`, matching the pattern used for `/api/v0/add`:

```nginx
# DAG put endpoint for UXF Profile CAR uploads
location /api/v0/dag/put {
    client_max_body_size 50M;
    proxy_pass http://127.0.0.1:5001;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    proxy_pass_header Access-Control-Allow-Origin;
    proxy_pass_header Access-Control-Allow-Methods;
    proxy_pass_header Access-Control-Allow-Headers;
    proxy_pass_header Access-Control-Expose-Headers;
}
```

Place it after the existing `/api/v0/add` block (around line 214).

---

## No Other Changes Required

### Already configured (no action needed):

**WebSocket + WSS:**
- IPFS listens on `/ip4/0.0.0.0/tcp/4002/ws` (configure-ipfs.sh line 24)
- nginx terminates TLS on :4003 and proxies to :4002 (nginx.conf.template stream block)
- WSS announce address configured: `/dns4/${DOMAIN}/tcp/4003/wss` (configure-ipfs.sh line 31)
- Browser OrbitDB/Helia clients can connect via `wss://unicity-ipfs1.dyndns.org:4003`

**Circuit Relay v2:**
- Relay service enabled with caps (configure-ipfs.sh lines 46-57)
- 64 max reservations, 16 max circuits, 2min connection duration
- Browser clients behind NAT can use the Kubo node as relay

**Bootstrap peers:**
- Both TCP and WSS multiaddrs registered (configure-ipfs.sh lines 68-71)
- `sphere-sdk/constants.ts` `DEFAULT_IPFS_BOOTSTRAP_PEERS` lists TCP peers
- WSS peers should be added to the SDK constants when Profile goes live

**QUIC + WebTransport:**
- Already configured: `/ip4/0.0.0.0/udp/4001/quic-v1/webtransport` (line 23)
- Future-proof for newer browser transports

---

## Storage Capacity Monitoring

Encrypted UXF CARs accumulate (old bundles never unpinned by design). Monitor:

```bash
# Inside the Docker container:
docker exec ipfs-storage ipfs repo stat
docker exec ipfs-storage ipfs pin ls --type=recursive | wc -l
```

Growth estimate: ~50-200 MB/user/year. For 100 users: ~5-20 GB/year.

Set an alert at 80% disk capacity. GC strategy is deferred — the SDK only removes bundle references from the Profile, never calls unpin.

---

## Action Items

- [ ] Add `/api/v0/dag/put` location block to `config/nginx.conf.template`
- [ ] Rebuild and redeploy: `make build && make run DOMAIN=unicity-ipfs1.dyndns.org`
- [ ] Test: `curl -X POST "https://unicity-ipfs1.dyndns.org/api/v0/dag/put" -F "file=@test.cbor" -v`
- [ ] Add WSS bootstrap multiaddrs to `sphere-sdk/constants.ts` `DEFAULT_IPFS_BOOTSTRAP_PEERS`
- [ ] Set up disk monitoring alert
