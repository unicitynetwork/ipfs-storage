#!/usr/bin/env python3
"""
Nostr IPFS Pin Service with Propagation Support

Subscribes to Nostr relays and pins announced CIDs to local IPFS node.
Features:
- Rate-limited pin queue (100 pins/sec max)
- SQLite persistence for pinned CIDs and IPNS records
- IPNS record interception and republishing
- Random re-announcement scheduler for propagation
- HTTP server for IPNS record capture

Environment Variables:
    NOSTR_RELAYS: Comma-separated relay URLs
    IPFS_API_URL: IPFS API endpoint (default: http://127.0.0.1:5001)
    PIN_KIND: Nostr event kind for pin requests (default: 30078)
    LOG_LEVEL: Logging level (default: INFO)
    RECONNECT_DELAY: Seconds to wait before reconnecting (default: 10)
    PIN_TIMEOUT: Pin operation timeout in seconds (default: 300)
    MAX_PINS_PER_SECOND: Rate limit for pinning (default: 100)
    HTTP_PORT: Port for IPNS interception server (default: 8080)
    DB_PATH: SQLite database path (default: /data/ipfs/propagation.db)
    NODE_NAME: Node identity for announcements (default: ipfs-node)
    NOSTR_PRIVATE_KEY: Hex private key for publishing (optional)
    ANNOUNCE_INTERVAL: Re-announcement interval in seconds (default: 0 = use probability)
    ANNOUNCE_PROBABILITY: Probability per second of re-announcement (default: 0.000277778 = 1/3600)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import secrets
import signal
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
import secp256k1
import websockets
from aiohttp import web
from websockets.exceptions import ConnectionClosed

# ==========================================
# Configuration
# ==========================================

NOSTR_RELAYS = os.getenv(
    "NOSTR_RELAYS",
    "wss://nostr-relay.testnet.unicity.network,ws://unicity-nostr-relay-20250927-alb-1919039002.me-central-1.elb.amazonaws.com:8080"
).split(",")

IPFS_API_URL = os.getenv("IPFS_API_URL", "http://127.0.0.1:5001")
PIN_KIND = int(os.getenv("PIN_KIND", "30078"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "10"))
PIN_TIMEOUT = int(os.getenv("PIN_TIMEOUT", "300"))
MAX_PINS_PER_SECOND = int(os.getenv("MAX_PINS_PER_SECOND", "100"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/data/ipfs/propagation.db")
NODE_NAME = os.getenv("NODE_NAME", "ipfs-node")
NOSTR_PRIVATE_KEY = os.getenv("NOSTR_PRIVATE_KEY", "")
# Re-announcement interval: ANNOUNCE_INTERVAL (seconds) takes priority over ANNOUNCE_PROBABILITY
ANNOUNCE_INTERVAL = int(os.getenv("ANNOUNCE_INTERVAL", "0"))  # seconds, 0 = use probability
if ANNOUNCE_INTERVAL > 0:
    ANNOUNCE_PROBABILITY = 1.0 / ANNOUNCE_INTERVAL
else:
    ANNOUNCE_PROBABILITY = float(os.getenv("ANNOUNCE_PROBABILITY", "0.000277778"))  # ~1/3600

# CID validation regex (CIDv0 and CIDv1)
CID_REGEX = re.compile(r'^(Qm[1-9A-HJ-NP-Za-km-z]{44}|baf[a-z][a-z2-7]{50,}|bag[a-z][a-z2-7]{50,})$')

# IPNS name validation regex (libp2p peer ID format)
IPNS_REGEX = re.compile(r'^(12D3KooW[a-zA-Z0-9]{44}|k[a-z2-7]{50,})$')

# ==========================================
# Logging Setup
# ==========================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ==========================================
# Data Types
# ==========================================

@dataclass
class PinRequest:
    """Represents a request to pin a CID from Nostr."""
    cid: str
    pubkey: str
    event_id: str
    ipns_name: Optional[str] = None
    relay: str = ""


@dataclass
class Metrics:
    """Track service metrics."""
    cids_queued: int = 0
    cids_pinned: int = 0
    cids_rejected_format: int = 0
    cids_failed: int = 0
    ipns_records_stored: int = 0
    reannouncements: int = 0
    nostr_events_received: int = 0
    start_time: float = field(default_factory=time.time)


# ==========================================
# SQLite Database
# ==========================================

def init_database(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with required tables."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()

    # Create tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pinned_cids (
            cid TEXT PRIMARY KEY,
            source TEXT DEFAULT 'nostr',
            pinned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_announced TIMESTAMP,
            announce_count INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ipns_records (
            ipns_name TEXT PRIMARY KEY,
            marshalled_record BLOB NOT NULL,
            cid TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_announced TIMESTAMP,
            announce_count INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            key TEXT PRIMARY KEY,
            value INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    logger.info(f"Database initialized at {db_path}")
    return conn


# ==========================================
# CID Validation
# ==========================================

def is_valid_cid(cid: str) -> bool:
    """Validate CID format (CIDv0 or CIDv1)."""
    if not cid:
        return False
    return bool(CID_REGEX.match(cid))


def is_valid_ipns_name(name: str) -> bool:
    """Validate IPNS name format."""
    if not name:
        return False
    return bool(IPNS_REGEX.match(name))


# ==========================================
# Rate-Limited Pin Queue
# ==========================================

class RateLimitedPinQueue:
    """
    Queue that processes pins at a controlled rate.
    Maximum MAX_PINS_PER_SECOND pins per second.
    """

    def __init__(self, db: sqlite3.Connection, metrics: Metrics):
        self.queue: deque[PinRequest] = deque()
        self.in_queue: set[str] = set()  # CIDs currently in queue
        self.db = db
        self.metrics = metrics
        self._load_existing_pins()

    def _load_existing_pins(self):
        """Load already-pinned CIDs from database."""
        cursor = self.db.cursor()
        cursor.execute("SELECT cid FROM pinned_cids")
        self.pinned = {row['cid'] for row in cursor.fetchall()}
        logger.info(f"Loaded {len(self.pinned)} existing pinned CIDs from database")

    def enqueue(self, pin_req: PinRequest) -> tuple[bool, str]:
        """
        Add CID to queue if valid and not already pinned/queued.

        Returns:
            (success, reason) tuple
        """
        cid = pin_req.cid

        if not is_valid_cid(cid):
            self.metrics.cids_rejected_format += 1
            return False, "invalid_format"

        if cid in self.pinned:
            return False, "already_pinned"

        if cid in self.in_queue:
            return False, "already_queued"

        self.queue.append(pin_req)
        self.in_queue.add(cid)
        self.metrics.cids_queued += 1
        return True, "queued"

    async def process_loop(self, shutdown_event: asyncio.Event):
        """Process queue at max MAX_PINS_PER_SECOND pins/second."""
        logger.info(f"Pin queue processor started (max {MAX_PINS_PER_SECOND} pins/sec)")

        while not shutdown_event.is_set():
            pins_this_second = 0
            start = time.time()

            while self.queue and pins_this_second < MAX_PINS_PER_SECOND:
                pin_req = self.queue.popleft()
                self.in_queue.discard(pin_req.cid)

                try:
                    success = await self._pin_cid(pin_req.cid)
                    if success:
                        self.pinned.add(pin_req.cid)
                        self._store_pin(pin_req)
                        self.metrics.cids_pinned += 1
                        pins_this_second += 1
                    else:
                        # Re-queue failed pins at the end
                        self.queue.append(pin_req)
                        self.in_queue.add(pin_req.cid)
                        self.metrics.cids_failed += 1
                except Exception as e:
                    logger.error(f"Pin error for {pin_req.cid[:16]}...: {e}")
                    self.metrics.cids_failed += 1

            # Sleep remainder of second
            elapsed = time.time() - start
            if elapsed < 1.0:
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=1.0 - elapsed
                    )
                except asyncio.TimeoutError:
                    pass

        logger.info("Pin queue processor stopped")

    async def _pin_cid(self, cid: str) -> bool:
        """Pin a CID to the local IPFS node."""
        try:
            async with httpx.AsyncClient(timeout=PIN_TIMEOUT) as client:
                response = await client.post(
                    f"{IPFS_API_URL}/api/v0/pin/add",
                    params={"arg": cid, "progress": "false"}
                )

                if response.status_code == 200:
                    logger.info(f"Pinned: {cid[:16]}...")
                    return True
                else:
                    logger.warning(f"Pin failed for {cid[:16]}...: HTTP {response.status_code}")
                    return False
        except httpx.TimeoutException:
            logger.warning(f"Timeout pinning {cid[:16]}...")
            return False
        except Exception as e:
            logger.error(f"Error pinning {cid[:16]}...: {e}")
            return False

    def _store_pin(self, pin_req: PinRequest):
        """Store pinned CID in database."""
        try:
            cursor = self.db.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO pinned_cids (cid, source) VALUES (?, ?)",
                (pin_req.cid, "nostr")
            )
            self.db.commit()
        except Exception as e:
            logger.error(f"Database error storing pin: {e}")

    @property
    def queue_depth(self) -> int:
        """Current number of CIDs waiting in queue."""
        return len(self.queue)


# ==========================================
# IPNS Record Storage
# ==========================================

class IpnsRecordStore:
    """Stores and republishes IPNS records."""

    def __init__(self, db: sqlite3.Connection, metrics: Metrics):
        self.db = db
        self.metrics = metrics

    def store_record(self, ipns_name: str, record_bytes: bytes, cid: Optional[str] = None):
        """Store an IPNS record for later republishing."""
        try:
            cursor = self.db.cursor()
            cursor.execute(
                """INSERT OR REPLACE INTO ipns_records
                   (ipns_name, marshalled_record, cid, last_updated)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                (ipns_name, record_bytes, cid)
            )
            self.db.commit()
            self.metrics.ipns_records_stored += 1
            logger.info(f"Stored IPNS record: {ipns_name[:16]}...")
        except Exception as e:
            logger.error(f"Error storing IPNS record: {e}")

    def get_all_records(self) -> list[tuple[str, bytes]]:
        """Get all stored IPNS records."""
        cursor = self.db.cursor()
        cursor.execute("SELECT ipns_name, marshalled_record FROM ipns_records")
        return [(row['ipns_name'], row['marshalled_record']) for row in cursor.fetchall()]

    async def republish_record(self, ipns_name: str, record_bytes: bytes) -> bool:
        """Republish an IPNS record to kubo."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Kubo routing/put expects multipart form data with 'value-file' field
                files = {'value-file': ('record', record_bytes, 'application/octet-stream')}
                response = await client.post(
                    f"{IPFS_API_URL}/api/v0/routing/put",
                    params={"arg": f"/ipns/{ipns_name}", "allow-offline": "true"},
                    files=files
                )

                if response.status_code == 200:
                    logger.info(f"Republished IPNS: {ipns_name[:16]}...")
                    return True
                else:
                    logger.warning(f"Failed to republish IPNS {ipns_name[:16]}...: {response.status_code} - {response.text[:100]}")
                    return False
        except Exception as e:
            logger.error(f"Error republishing IPNS: {e}")
            return False

    def mark_announced(self, ipns_name: str):
        """Update announcement timestamp."""
        try:
            cursor = self.db.cursor()
            cursor.execute(
                """UPDATE ipns_records
                   SET last_announced = CURRENT_TIMESTAMP,
                       announce_count = announce_count + 1
                   WHERE ipns_name = ?""",
                (ipns_name,)
            )
            self.db.commit()
        except Exception as e:
            logger.error(f"Error marking IPNS announced: {e}")


# ==========================================
# Nostr Publishing (NIP-01 compliant)
# ==========================================

class NostrPublisher:
    """Publishes signed Nostr events for re-announcement (NIP-01 compliant)."""

    def __init__(self, relays: list[str], private_key_hex: str = ""):
        self.relays = relays
        # Use provided key or generate a new one
        if private_key_hex:
            self.private_key_hex = private_key_hex
        else:
            self.private_key_hex = secrets.token_hex(32)
            logger.info("Generated new Nostr private key (set NOSTR_PRIVATE_KEY to persist)")

        # Initialize secp256k1 keypair
        self.privkey = secp256k1.PrivateKey(bytes.fromhex(self.private_key_hex))
        pubkey_bytes = self.privkey.pubkey.serialize(compressed=True)
        # Remove the prefix byte (02 or 03) for x-only pubkey (BIP-340/Schnorr)
        self.pubkey_hex = pubkey_bytes[1:].hex()
        logger.info(f"Nostr publisher initialized with pubkey: {self.pubkey_hex[:16]}...")

    def _compute_event_id(self, event: dict) -> str:
        """Compute NIP-01 event ID (SHA256 of serialized event)."""
        serialized = json.dumps([
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"]
        ], separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _sign_event(self, event: dict) -> str:
        """Sign event with Schnorr signature (NIP-01)."""
        event_id_bytes = bytes.fromhex(event["id"])
        # Sign with Schnorr (BIP-340)
        sig = self.privkey.schnorr_sign(event_id_bytes, None, raw=True)
        return sig.hex()

    def _create_signed_event(self, kind: int, tags: list, content: str) -> dict:
        """Create a fully signed NIP-01 event."""
        event = {
            "pubkey": self.pubkey_hex,
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags,
            "content": content
        }
        event["id"] = self._compute_event_id(event)
        event["sig"] = self._sign_event(event)
        return event

    async def publish_reannouncement(self, cids: list[str], ipns_names: list[str]):
        """Publish bulk re-announcement event to relays."""
        if not cids and not ipns_names:
            return

        tags = [
            ["d", "ipfs-pin"],
            ["type", "bulk-reannounce"]
        ]

        for cid in cids[:50]:  # Limit to 50 CIDs per event
            tags.append(["cid", cid])

        for name in ipns_names[:50]:  # Limit to 50 IPNS names
            tags.append(["ipns", name])

        content = json.dumps({
            "source": NODE_NAME,
            "timestamp": int(time.time())
        })

        # Create signed event
        event = self._create_signed_event(PIN_KIND, tags, content)
        logger.info(f"Publishing re-announcement: {len(cids)} CIDs, {len(ipns_names)} IPNS names")

        # Publish to all relays concurrently
        tasks = []
        for relay in self.relays:
            relay_url = relay.strip()
            if relay_url:
                tasks.append(self._publish_to_relay(relay_url, event))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success = sum(1 for r in results if r is True)
            logger.info(f"Published to {success}/{len(tasks)} relays")

    async def _publish_to_relay(self, relay_url: str, event: dict) -> bool:
        """Publish signed event to a single relay via WebSocket."""
        try:
            async with websockets.connect(relay_url, close_timeout=5) as ws:
                message = json.dumps(["EVENT", event])
                await ws.send(message)
                # Wait for OK response (with timeout)
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    resp_data = json.loads(response)
                    if resp_data[0] == "OK" and resp_data[2] is True:
                        logger.debug(f"Published to {relay_url}: OK")
                        return True
                    else:
                        logger.debug(f"Relay {relay_url} rejected: {resp_data}")
                        return False
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout waiting for response from {relay_url}")
                    return True  # Assume success if no response
        except Exception as e:
            logger.debug(f"Failed to publish to {relay_url}: {e}")
            return False


# ==========================================
# Re-announcement Scheduler
# ==========================================

class ReannounceScheduler:
    """Random scheduler for periodic re-announcements."""

    def __init__(
        self,
        db: sqlite3.Connection,
        ipns_store: IpnsRecordStore,
        publisher: NostrPublisher,
        metrics: Metrics
    ):
        self.db = db
        self.ipns_store = ipns_store
        self.publisher = publisher
        self.metrics = metrics

    async def run(self, shutdown_event: asyncio.Event):
        """Run scheduler loop with random probability trigger."""
        logger.info(f"Re-announcement scheduler started (probability: {ANNOUNCE_PROBABILITY:.6f}/sec)")

        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                pass

            if shutdown_event.is_set():
                break

            # Random trigger check
            if random.random() < ANNOUNCE_PROBABILITY:
                await self._do_reannouncement()

        logger.info("Re-announcement scheduler stopped")

    async def _do_reannouncement(self):
        """Perform re-announcement of all pins and IPNS records."""
        logger.info("Triggering re-announcement...")

        # Get all pinned CIDs
        cursor = self.db.cursor()
        cursor.execute("SELECT cid FROM pinned_cids LIMIT 100")
        cids = [row['cid'] for row in cursor.fetchall()]

        # Get all IPNS records
        ipns_records = self.ipns_store.get_all_records()
        ipns_names = [name for name, _ in ipns_records]

        # Republish IPNS records to kubo
        for ipns_name, record_bytes in ipns_records:
            await self.ipns_store.republish_record(ipns_name, record_bytes)
            self.ipns_store.mark_announced(ipns_name)

        # Publish Nostr announcement
        await self.publisher.publish_reannouncement(cids, ipns_names)

        self.metrics.reannouncements += 1
        logger.info(f"Re-announced {len(cids)} CIDs and {len(ipns_names)} IPNS records")


# ==========================================
# HTTP Server for IPNS Interception
# ==========================================

class IpnsInterceptServer:
    """HTTP server to intercept IPNS publish requests."""

    def __init__(self, ipns_store: IpnsRecordStore, db: sqlite3.Connection, metrics: Metrics, port: int = HTTP_PORT):
        self.ipns_store = ipns_store
        self.db = db
        self.metrics = metrics
        self.port = port
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        """Set up HTTP routes."""
        self.app.router.add_post('/ipns-intercept', self._handle_ipns_intercept)
        self.app.router.add_get('/health', self._handle_health)
        self.app.router.add_get('/metrics', self._handle_metrics)

    async def _handle_ipns_intercept(self, request: web.Request) -> web.Response:
        """Handle mirrored IPNS publish requests."""
        try:
            # Extract IPNS name from query string
            query = request.query_string
            parsed = parse_qs(query)
            arg = parsed.get('arg', [''])[0]

            # Parse /ipns/{name} format
            if arg.startswith('/ipns/'):
                ipns_name = arg[6:]  # Remove /ipns/ prefix
            else:
                ipns_name = arg

            if not is_valid_ipns_name(ipns_name):
                return web.Response(status=400, text="Invalid IPNS name")

            # Extract IPNS record from request body
            # Handle multipart/form-data (sent by browsers/fetch) vs raw bytes
            content_type = request.content_type
            record_bytes = None

            if content_type and 'multipart/form-data' in content_type:
                # Parse multipart form data to extract the actual record
                try:
                    reader = await request.multipart()
                    async for part in reader:
                        if part.name == 'file':
                            record_bytes = await part.read()
                            break
                except Exception as e:
                    logger.warning(f"Multipart parse failed, trying raw body: {e}")
                    record_bytes = await request.read()
            else:
                # Raw binary body
                record_bytes = await request.read()

            if record_bytes:
                # Validate it's not multipart boundary data (sanity check)
                if record_bytes.startswith(b'------'):
                    logger.error(f"Received multipart boundary as record data, skipping")
                    return web.Response(status=400, text="Invalid record format")

                logger.info(f"Storing IPNS record for {ipns_name[:16]}... ({len(record_bytes)} bytes)")
                self.ipns_store.store_record(ipns_name, record_bytes)
                return web.Response(status=200, text="OK")
            else:
                return web.Response(status=400, text="Empty body")

        except Exception as e:
            logger.error(f"IPNS intercept error: {e}")
            return web.Response(status=500, text=str(e))

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.Response(status=200, text="OK")

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Metrics endpoint returning actual service statistics."""
        try:
            cursor = self.db.cursor()

            # Count pinned CIDs
            cursor.execute("SELECT COUNT(*) FROM pinned_cids")
            total_pinned = cursor.fetchone()[0]

            # Count IPNS records
            cursor.execute("SELECT COUNT(*) FROM ipns_records")
            total_ipns = cursor.fetchone()[0]

            # Get announce counts
            cursor.execute("SELECT SUM(announce_count) FROM pinned_cids")
            cid_announces = cursor.fetchone()[0] or 0

            cursor.execute("SELECT SUM(announce_count) FROM ipns_records")
            ipns_announces = cursor.fetchone()[0] or 0

            uptime = time.time() - self.metrics.start_time

            metrics_data = {
                "status": "ok",
                "uptime_seconds": int(uptime),
                "node_name": NODE_NAME,
                "database": {
                    "total_pinned_cids": total_pinned,
                    "total_ipns_records": total_ipns,
                    "cid_announcements": cid_announces,
                    "ipns_announcements": ipns_announces
                },
                "session": {
                    "cids_queued": self.metrics.cids_queued,
                    "cids_pinned": self.metrics.cids_pinned,
                    "cids_rejected": self.metrics.cids_rejected_format,
                    "cids_failed": self.metrics.cids_failed,
                    "ipns_records_stored": self.metrics.ipns_records_stored,
                    "nostr_events_received": self.metrics.nostr_events_received,
                    "reannouncements": self.metrics.reannouncements
                },
                "relays": len(NOSTR_RELAYS)
            }

            return web.Response(
                status=200,
                content_type='application/json',
                text=json.dumps(metrics_data)
            )
        except Exception as e:
            logger.error(f"Metrics error: {e}")
            return web.Response(
                status=500,
                content_type='application/json',
                text=json.dumps({"status": "error", "message": str(e)})
            )

    async def start(self):
        """Start the HTTP server."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        logger.info(f"HTTP server started on port {self.port}")
        return runner


# ==========================================
# Nostr Event Parsing
# ==========================================

def parse_pin_request(event: dict, relay: str) -> Optional[PinRequest]:
    """Parse a Nostr event into a PinRequest if valid."""
    try:
        kind = event.get("kind")
        if kind != PIN_KIND:
            return None

        pubkey = event.get("pubkey", "")
        event_id = event.get("id", "")
        tags = event.get("tags", [])

        has_pin_tag = False
        cid = None
        ipns_name = None

        for tag in tags:
            if len(tag) >= 2:
                if tag[0] == "d" and tag[1] == "ipfs-pin":
                    has_pin_tag = True
                elif tag[0] == "cid":
                    cid = tag[1].strip()
                elif tag[0] == "ipns":
                    ipns_name = tag[1].strip()

        if not has_pin_tag:
            return None

        if not cid:
            return None

        return PinRequest(
            cid=cid,
            pubkey=pubkey,
            event_id=event_id,
            ipns_name=ipns_name,
            relay=relay
        )

    except Exception as e:
        logger.debug(f"Failed to parse event: {e}")
        return None


# ==========================================
# Nostr Relay Subscription
# ==========================================

async def subscribe_to_relay(
    relay_url: str,
    pin_queue: RateLimitedPinQueue,
    metrics: Metrics,
    shutdown_event: asyncio.Event
):
    """Connect to a Nostr relay and subscribe to pin events."""
    subscription_id = "ipfs-pin-sub"

    filters = [
        {
            "kinds": [PIN_KIND],
            "#d": ["ipfs-pin"]
        }
    ]

    while not shutdown_event.is_set():
        try:
            logger.info(f"Connecting to {relay_url}...")

            async with websockets.connect(
                relay_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                logger.info(f"Connected to {relay_url}")

                req = ["REQ", subscription_id] + filters
                await ws.send(json.dumps(req))

                while not shutdown_event.is_set():
                    try:
                        message = await asyncio.wait_for(
                            ws.recv(),
                            timeout=30.0
                        )

                        try:
                            data = json.loads(message)

                            if data[0] == "EVENT" and len(data) >= 3:
                                event = data[2]
                                metrics.nostr_events_received += 1
                                pin_req = parse_pin_request(event, relay_url)

                                if pin_req:
                                    success, reason = pin_queue.enqueue(pin_req)
                                    if success:
                                        logger.info(
                                            f"Queued CID: {pin_req.cid[:16]}... "
                                            f"(queue: {pin_queue.queue_depth})"
                                        )
                                    else:
                                        logger.debug(f"Skip {pin_req.cid[:16]}...: {reason}")

                            elif data[0] == "EOSE":
                                logger.debug(f"EOSE from {relay_url}")

                        except (json.JSONDecodeError, IndexError, TypeError) as e:
                            logger.debug(f"Parse error from {relay_url}: {e}")

                    except asyncio.TimeoutError:
                        continue

        except ConnectionClosed as e:
            logger.warning(f"Connection to {relay_url} closed: {e}")
        except Exception as e:
            logger.error(f"Error with {relay_url}: {e}")

        if not shutdown_event.is_set():
            logger.info(f"Reconnecting to {relay_url} in {RECONNECT_DELAY}s...")
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=RECONNECT_DELAY
                )
            except asyncio.TimeoutError:
                pass


# ==========================================
# Health Check
# ==========================================

async def check_ipfs_connection() -> bool:
    """Check if IPFS API is reachable."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{IPFS_API_URL}/api/v0/id")
            if response.status_code == 200:
                data = response.json()
                peer_id = data.get("ID", "unknown")
                logger.info(f"Connected to IPFS node: {peer_id}")
                return True
            else:
                logger.error(f"IPFS API returned status {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"Failed to connect to IPFS API: {e}")
        return False


async def wait_for_ipfs(max_attempts: int = 30, delay: int = 5) -> bool:
    """Wait for IPFS API to become available."""
    for attempt in range(max_attempts):
        if await check_ipfs_connection():
            return True
        logger.info(f"Waiting for IPFS API... (attempt {attempt + 1}/{max_attempts})")
        await asyncio.sleep(delay)

    logger.error("IPFS API not available after maximum attempts")
    return False


# ==========================================
# Main
# ==========================================

async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("  Nostr IPFS Pin Service with Propagation Support")
    logger.info("=" * 60)
    logger.info(f"  IPFS API:       {IPFS_API_URL}")
    logger.info(f"  Relays:         {len(NOSTR_RELAYS)}")
    logger.info(f"  Event kind:     {PIN_KIND}")
    logger.info(f"  Max pins/sec:   {MAX_PINS_PER_SECOND}")
    logger.info(f"  HTTP port:      {HTTP_PORT}")
    logger.info(f"  Database:       {DB_PATH}")
    logger.info(f"  Node name:      {NODE_NAME}")
    logger.info("=" * 60)

    # Wait for IPFS to be available
    if not await wait_for_ipfs():
        logger.error("Exiting: IPFS API not available")
        sys.exit(1)

    # Initialize database
    db = init_database(DB_PATH)

    # Create shared resources
    metrics = Metrics()
    pin_queue = RateLimitedPinQueue(db, metrics)
    ipns_store = IpnsRecordStore(db, metrics)
    publisher = NostrPublisher(NOSTR_RELAYS, NOSTR_PRIVATE_KEY)
    scheduler = ReannounceScheduler(db, ipns_store, publisher, metrics)
    http_server = IpnsInterceptServer(ipns_store, db, metrics, HTTP_PORT)

    shutdown_event = asyncio.Event()

    # Handle shutdown signals
    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Start HTTP server
    http_runner = await http_server.start()

    # Start relay subscriptions
    relay_tasks = [
        asyncio.create_task(
            subscribe_to_relay(relay.strip(), pin_queue, metrics, shutdown_event),
            name=f"relay-{relay}"
        )
        for relay in NOSTR_RELAYS
        if relay.strip()
    ]

    # Start pin queue processor
    queue_task = asyncio.create_task(
        pin_queue.process_loop(shutdown_event),
        name="pin-queue"
    )

    # Start re-announcement scheduler
    scheduler_task = asyncio.create_task(
        scheduler.run(shutdown_event),
        name="scheduler"
    )

    logger.info(f"Service started: {len(relay_tasks)} relay(s), rate-limited queue, scheduler")

    # Wait for shutdown
    await shutdown_event.wait()

    # Cleanup
    logger.info("Shutting down...")

    # Cancel all tasks
    for task in relay_tasks + [queue_task, scheduler_task]:
        task.cancel()

    await asyncio.gather(*relay_tasks, queue_task, scheduler_task, return_exceptions=True)

    # Cleanup HTTP server
    await http_runner.cleanup()

    # Close database
    db.close()

    # Log final metrics
    logger.info("=" * 40)
    logger.info("  Final Metrics")
    logger.info("=" * 40)
    logger.info(f"  CIDs queued:      {metrics.cids_queued}")
    logger.info(f"  CIDs pinned:      {metrics.cids_pinned}")
    logger.info(f"  CIDs rejected:    {metrics.cids_rejected_format}")
    logger.info(f"  CIDs failed:      {metrics.cids_failed}")
    logger.info(f"  IPNS stored:      {metrics.ipns_records_stored}")
    logger.info(f"  Re-announcements: {metrics.reannouncements}")
    logger.info(f"  Nostr events:     {metrics.nostr_events_received}")
    logger.info("=" * 40)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
