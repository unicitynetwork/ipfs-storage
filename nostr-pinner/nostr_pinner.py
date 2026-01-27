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
- Version chain validation for IPNS updates

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
    CHAIN_VALIDATION_ENABLED: Enable version chain validation (default: true)
    CHAIN_VALIDATION_MODE: strict | queue (default: strict)
    CID_FETCH_TIMEOUT: Timeout for CID content fetches (default: 10)
    CID_CACHE_SIZE: LRU cache size for CID content (default: 1000)
    CID_CACHE_TTL: Cache TTL in seconds (default: 60)
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
from collections import deque, OrderedDict
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

# Staleness threshold for IPNS records - records older than this trigger async DHT sync
STALE_THRESHOLD_SECONDS = int(os.getenv("STALE_THRESHOLD_SECONDS", "60"))

# Chain validation configuration
CHAIN_VALIDATION_ENABLED = os.getenv("CHAIN_VALIDATION_ENABLED", "true").lower() == "true"
CHAIN_VALIDATION_MODE = os.getenv("CHAIN_VALIDATION_MODE", "strict")  # strict | queue
CID_FETCH_TIMEOUT = int(os.getenv("CID_FETCH_TIMEOUT", "10"))
CID_CACHE_SIZE = int(os.getenv("CID_CACHE_SIZE", "1000"))
CID_CACHE_TTL = int(os.getenv("CID_CACHE_TTL", "60"))

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
    chain_validations_passed: int = 0
    chain_validations_failed_break: int = 0
    chain_validations_failed_fetch: int = 0
    chain_validations_skipped: int = 0
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
            sequence INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_announced TIMESTAMP,
            announce_count INTEGER DEFAULT 0
        )
    """)

    # Migration: Add sequence column if it doesn't exist (for existing databases)
    try:
        cursor.execute("ALTER TABLE ipns_records ADD COLUMN sequence INTEGER DEFAULT 0")
        logger.info("Added sequence column to ipns_records table")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add last_cid column for chain validation
    try:
        cursor.execute("ALTER TABLE ipns_records ADD COLUMN last_cid TEXT")
        logger.info("Added last_cid column to ipns_records table")
    except sqlite3.OperationalError:
        pass

    # Add version column from _meta
    try:
        cursor.execute("ALTER TABLE ipns_records ADD COLUMN version INTEGER DEFAULT 0")
        logger.info("Added version column to ipns_records table")
    except sqlite3.OperationalError:
        pass

    # Add lock_version column for optimistic locking (separate from content version)
    try:
        cursor.execute("ALTER TABLE ipns_records ADD COLUMN lock_version INTEGER DEFAULT 0")
        logger.info("Added lock_version column to ipns_records table")
    except sqlite3.OperationalError:
        pass

    # Performance indexes for IPNS lookups (critical for <50ms response time)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ipns_name
        ON ipns_records(ipns_name)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ipns_last_updated
        ON ipns_records(last_updated DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ipns_sequence
        ON ipns_records(sequence DESC)
    """)

    # Create forensic log table for chain breaks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chain_validation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ipns_name TEXT NOT NULL,
            violation_type TEXT NOT NULL,
            current_cid TEXT,
            rejected_cid TEXT,
            rejected_sequence INTEGER,
            expected_lastcid TEXT,
            actual_lastcid TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            details TEXT
        )
    """)

    # Create indexes for forensic log queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chain_log_ipns
        ON chain_validation_log(ipns_name)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chain_log_timestamp
        ON chain_validation_log(timestamp DESC)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            key TEXT PRIMARY KEY,
            value INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Forensic events table for comprehensive logging (async, non-blocking)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forensic_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            event_type TEXT NOT NULL,
            ipns_name TEXT NOT NULL,
            details TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_forensic_timestamp
        ON forensic_events(timestamp DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_forensic_ipns
        ON forensic_events(ipns_name)
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


def parse_ipns_record(record_bytes: bytes) -> tuple[int, str | None]:
    """
    Parse IPNS record (protobuf) to extract sequence number and CID.

    IPNS record protobuf fields (from ipns.proto):
    - field 1 (bytes): value (path like /ipfs/<cid>)
    - field 3 (varint): validity_type (EOL=0)
    - field 5 (varint): sequence number  <- IMPORTANT: field 5, not 3!
    - field 6 (varint): ttl

    Returns: (sequence_number, cid_or_none)
    """
    sequence = 0
    value = None

    try:
        pos = 0
        while pos < len(record_bytes):
            # Read field key (varint)
            key = 0
            shift = 0
            while pos < len(record_bytes):
                b = record_bytes[pos]
                pos += 1
                key |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7

            field_number = key >> 3
            wire_type = key & 0x07

            if wire_type == 0:  # Varint
                val = 0
                shift = 0
                while pos < len(record_bytes):
                    b = record_bytes[pos]
                    pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7

                if field_number == 5:  # sequence (field 5, not 3!)
                    sequence = val

            elif wire_type == 2:  # Length-delimited
                length = 0
                shift = 0
                while pos < len(record_bytes):
                    b = record_bytes[pos]
                    pos += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7

                data = record_bytes[pos:pos + length]
                pos += length

                if field_number == 1:  # value
                    value = data.decode('utf-8', errors='ignore')
            else:
                # Skip unknown wire types
                break
    except Exception as e:
        logger.warning(f"Error parsing IPNS record: {e}")

    # Extract CID from value (e.g., "/ipfs/bafyrei...")
    cid = None
    if value and value.startswith('/ipfs/'):
        cid = value[6:]  # Remove /ipfs/ prefix

    return (sequence, cid)


# ==========================================
# CID Content Cache
# ==========================================

class CidContentCache:
    """LRU cache for CID content to avoid repeated fetches."""

    def __init__(self, max_size: int = 1000, ttl: int = 60):
        self.cache: OrderedDict[str, tuple[dict, float]] = OrderedDict()  # cid -> (content, expiry)
        self.max_size = max_size
        self.ttl = ttl
        self.lock = asyncio.Lock()

    async def get(self, cid: str) -> Optional[dict]:
        """Get cached content if not expired. Moves to end for LRU tracking."""
        try:
            async with asyncio.timeout(5):  # 5-second timeout to prevent deadlock
                async with self.lock:
                    if cid in self.cache:
                        content, expiry = self.cache[cid]
                        if time.time() < expiry:
                            self.cache.move_to_end(cid)  # Mark as recently used
                            return content
                        del self.cache[cid]
        except asyncio.TimeoutError:
            logger.error(f"Cache lock timeout for get({cid[:16]}...)")
        return None

    async def set(self, cid: str, content: dict):
        """Cache content with TTL using LRU eviction."""
        try:
            async with asyncio.timeout(5):  # 5-second timeout to prevent deadlock
                async with self.lock:
                    # Remove oldest (first item) if at capacity
                    if len(self.cache) >= self.max_size:
                        self.cache.popitem(last=False)  # Remove oldest (FIFO order)

                    self.cache[cid] = (content, time.time() + self.ttl)
                    self.cache.move_to_end(cid)  # Ensure it's at the end
        except asyncio.TimeoutError:
            logger.error(f"Cache lock timeout for set({cid[:16]}...)")

    def invalidate(self, cid: str):
        """Remove entry from cache."""
        if cid in self.cache:
            del self.cache[cid]


# Global cache instance
_cid_cache: Optional[CidContentCache] = None

def get_cid_cache() -> CidContentCache:
    global _cid_cache
    if _cid_cache is None:
        _cid_cache = CidContentCache(CID_CACHE_SIZE, CID_CACHE_TTL)
    return _cid_cache


async def fetch_cid_content(cid: str, timeout: int = CID_FETCH_TIMEOUT) -> Optional[dict]:
    """
    Fetch CID content from local IPFS node with content validation.
    Returns parsed JSON content or None on failure.

    Validates:
    - Content is a dict (not list or primitive)
    - Content has tokens OR _meta field (valid wallet content)
    - Rejects empty content with only Data/Links fields
    """
    cache = get_cid_cache()

    # Check cache first
    cached = await cache.get(cid)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Use IPFS cat API to fetch content
            response = await client.post(
                f"{IPFS_API_URL}/api/v0/cat",
                params={"arg": cid}
            )

            if response.status_code != 200:
                logger.warning(f"CID fetch failed for {cid[:16]}...: status={response.status_code}")
                return None

            # Parse JSON content
            content = response.json()

            # Validate content has expected structure
            if not isinstance(content, dict):
                logger.warning(f"CID {cid[:16]}... contains non-dict content (type={type(content).__name__})")
                return None

            # Check for token data OR _meta field (valid wallet content)
            # IPFS empty nodes typically have only Data/Links fields
            ipfs_internal_keys = {'Data', 'Links'}
            actual_keys = set(content.keys())
            non_ipfs_keys = actual_keys - ipfs_internal_keys

            has_tokens = any(k not in ('_meta', 'Data', 'Links') for k in content.keys())
            has_meta = '_meta' in content and isinstance(content.get('_meta'), dict)

            if not has_tokens and not has_meta:
                # Content appears empty or has only IPFS internal fields
                if non_ipfs_keys:
                    # Has some custom keys but no tokens or meta - might be valid, log warning
                    logger.debug(f"CID {cid[:16]}... has custom keys but no tokens/_meta: {non_ipfs_keys}")
                else:
                    # Only Data/Links - definitely empty
                    logger.warning(f"CID {cid[:16]}... appears empty (only Data/Links), not caching")
                    return None

            await cache.set(cid, content)
            return content

    except httpx.TimeoutException:
        logger.warning(f"CID fetch timeout for {cid[:16]}...")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"CID content not valid JSON for {cid[:16]}...: {e}")
        return None
    except Exception as e:
        logger.error(f"CID fetch error for {cid[:16]}...: {e}")
        return None


@dataclass
class ChainValidationResult:
    """Result of chain validation."""
    valid: bool
    reason: str
    last_cid: Optional[str] = None
    version: Optional[int] = None


async def validate_version_chain(
    ipns_name: str,
    new_cid: str,
    current_cid: Optional[str],
    new_sequence: int,
    current_sequence: int,
    current_version: int,
    db: sqlite3.Connection,
    metrics: Metrics
) -> ChainValidationResult:
    """
    Validate that new CID maintains version chain integrity.

    Rules:
    1. First record (no current_cid): Accept, lastCid should be null, version should be 1
    2. Same CID (republish): Accept
    3. New CID: Must have _meta.lastCid == current_cid AND _meta.version == current_version + 1

    Escape hatches:
    - Sequence jump >5: Bypasses chain validation (indicates multi-device conflict)
    - log_only mode: Accepts records but logs violations for forensics
    """

    if not CHAIN_VALIDATION_ENABLED:
        metrics.chain_validations_skipped += 1
        return ChainValidationResult(valid=True, reason="validation_disabled")

    # Escape hatch: Large sequence jumps bypass chain validation
    # (Indicates multi-device conflict or recovery, not corruption)
    sequence_delta = new_sequence - current_sequence
    if sequence_delta > 5:
        logger.warning(
            f"Sequence jump detected for {ipns_name[:16]}...: "
            f"{current_sequence} -> {new_sequence} (delta={sequence_delta}), bypassing chain validation"
        )
        metrics.chain_validations_skipped += 1
        return ChainValidationResult(valid=True, reason="sequence_jump_bypass")

    # Case 1: First record for this IPNS name
    if current_cid is None:
        # Fetch new CID to verify it's a valid bootstrap (no lastCid)
        content = await fetch_cid_content(new_cid)
        if content is None:
            if CHAIN_VALIDATION_MODE == "strict":
                metrics.chain_validations_failed_fetch += 1
                return ChainValidationResult(valid=False, reason="fetch_failed_bootstrap")
            else:
                metrics.chain_validations_skipped += 1
                return ChainValidationResult(valid=True, reason="fetch_failed_optimistic")

        meta = content.get('_meta', {})
        last_cid = meta.get('lastCid')
        version = meta.get('version', 1)

        # Bootstrap record should NOT have lastCid (or it should be empty)
        if last_cid:
            logger.warning(
                f"CHAIN BREAK: Bootstrap record for {ipns_name[:16]}... has unexpected lastCid"
            )
            _log_chain_violation(
                db, ipns_name, "invalid_bootstrap",
                None, new_cid, new_sequence, None, last_cid
            )
            metrics.chain_validations_failed_break += 1

            # In log_only mode: accept the record but log the violation
            if CHAIN_VALIDATION_MODE == "log_only":
                logger.warning(
                    f"Invalid bootstrap accepted in log_only mode for {ipns_name[:16]}..."
                )
                return ChainValidationResult(valid=True, reason="invalid_bootstrap_logged", last_cid=last_cid, version=version)

            return ChainValidationResult(valid=False, reason="invalid_bootstrap_lastcid")

        metrics.chain_validations_passed += 1
        # Bootstrap has no lastCid (it's the first version)
        return ChainValidationResult(valid=True, reason="valid_bootstrap", last_cid=None, version=version)

    # Case 2: Same CID (republish with higher sequence)
    if new_cid == current_cid:
        metrics.chain_validations_passed += 1
        # Republish doesn't change the chain - preserve existing lastCid
        return ChainValidationResult(valid=True, reason="republish", last_cid=current_cid, version=0)

    # Case 3: New CID - validate chain continuity
    content = await fetch_cid_content(new_cid)
    if content is None:
        if CHAIN_VALIDATION_MODE == "strict":
            logger.warning(
                f"CHAIN VALIDATION BLOCKED: Cannot fetch CID {new_cid[:16]}... for {ipns_name[:16]}..."
            )
            metrics.chain_validations_failed_fetch += 1
            return ChainValidationResult(valid=False, reason="fetch_failed_strict")
        else:
            logger.warning(
                f"CHAIN VALIDATION SKIPPED: Fetch failed for {new_cid[:16]}..., accepting in queue mode"
            )
            metrics.chain_validations_skipped += 1
            return ChainValidationResult(valid=True, reason="fetch_failed_queue")

    meta = content.get('_meta', {})
    last_cid = meta.get('lastCid')
    version = meta.get('version', 0)

    # Validate chain: lastCid must equal current_cid
    if last_cid != current_cid:
        logger.error(
            f"CHAIN BREAK DETECTED for {ipns_name[:16]}...\n"
            f"  Current CID:      {current_cid}\n"
            f"  New CID:          {new_cid}\n"
            f"  New _meta.lastCid: {last_cid}\n"
            f"  Expected lastCid to equal current CID!"
        )
        _log_chain_violation(
            db, ipns_name, "chain_break",
            current_cid, new_cid, new_sequence, current_cid, last_cid
        )
        metrics.chain_validations_failed_break += 1

        # In log_only mode: accept the record but log the violation
        if CHAIN_VALIDATION_MODE == "log_only":
            logger.warning(
                f"Chain break accepted in log_only mode for {ipns_name[:16]}..."
            )
            return ChainValidationResult(valid=True, reason="chain_break_logged", last_cid=last_cid, version=version)

        # In strict mode: reject the record
        return ChainValidationResult(valid=False, reason="chain_break", last_cid=last_cid)

    # Validate version number: new version must be exactly current_version + 1
    expected_version = current_version + 1
    if version != expected_version:
        logger.error(
            f"VERSION MISMATCH for {ipns_name[:16]}...\n"
            f"  Current version:  {current_version}\n"
            f"  Expected version: {expected_version}\n"
            f"  Actual version:   {version}\n"
            f"  Version must increment by exactly 1!"
        )
        _log_chain_violation(
            db, ipns_name, "version_mismatch",
            current_cid, new_cid, new_sequence, str(expected_version), str(version)
        )
        metrics.chain_validations_failed_break += 1

        # In log_only mode: accept the record but log the violation
        if CHAIN_VALIDATION_MODE == "log_only":
            logger.warning(
                f"Version mismatch accepted in log_only mode for {ipns_name[:16]}..."
            )
            return ChainValidationResult(valid=True, reason="version_mismatch_logged", last_cid=last_cid, version=version)

        # In strict mode: reject the record
        return ChainValidationResult(valid=False, reason="version_mismatch", last_cid=last_cid, version=version)

    logger.info(
        f"CHAIN VALID: {ipns_name[:16]}... seq={new_sequence} v={version}"
    )
    metrics.chain_validations_passed += 1
    return ChainValidationResult(valid=True, reason="valid_chain", last_cid=last_cid, version=version)


def _log_chain_violation(
    db: sqlite3.Connection,
    ipns_name: str,
    violation_type: str,
    current_cid: Optional[str],
    rejected_cid: str,
    rejected_sequence: int,
    expected_lastcid: Optional[str],
    actual_lastcid: Optional[str]
):
    """Log chain validation violation for forensic analysis."""
    try:
        cursor = db.cursor()
        cursor.execute(
            """INSERT INTO chain_validation_log
               (ipns_name, violation_type, current_cid, rejected_cid,
                rejected_sequence, expected_lastcid, actual_lastcid)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ipns_name, violation_type, current_cid, rejected_cid,
             rejected_sequence, expected_lastcid, actual_lastcid)
        )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log chain violation: {e}")


async def _log_forensic_event_async(
    db: sqlite3.Connection,
    event_type: str,
    ipns_name: str,
    details: dict
):
    """
    Async forensic logging (non-blocking).
    Logs to forensic_events table for debugging and analysis.
    """
    # Schedule the actual write as a background task to avoid blocking
    asyncio.create_task(_write_forensic_log(db, event_type, ipns_name, details))


async def _write_forensic_log(
    db: sqlite3.Connection,
    event_type: str,
    ipns_name: str,
    details: dict
):
    """Background log writer for forensic events."""
    try:
        cursor = db.cursor()
        cursor.execute(
            """INSERT INTO forensic_events (event_type, ipns_name, details)
               VALUES (?, ?, ?)""",
            (event_type, ipns_name, json.dumps(details))
        )
        db.commit()
    except Exception as e:
        logger.error(f"Forensic log failed: {e}")


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

    async def store_record(self, ipns_name: str, record_bytes: bytes, cid: Optional[str] = None) -> bool:
        """
        Store an IPNS record for later republishing.
        Uses optimistic locking to prevent race conditions.

        Validates:
        1. Sequence number >= existing sequence
        2. Version chain integrity (new CID's _meta.lastCid == current CID)

        Returns True if stored, False if rejected.
        """
        max_retries = 3

        for attempt in range(max_retries):
            try:
                # Parse the incoming record to get sequence and CID
                new_sequence, parsed_cid = parse_ipns_record(record_bytes)
                if cid is None and parsed_cid:
                    cid = parsed_cid

                cursor = self.db.cursor()

                # Get existing record state (including lock_version for optimistic locking)
                cursor.execute(
                    'SELECT cid, sequence, last_cid, version, lock_version FROM ipns_records WHERE ipns_name = ?',
                    (ipns_name,)
                )
                row = cursor.fetchone()

                current_cid = row['cid'] if row else None
                existing_sequence = row['sequence'] if row else 0
                current_version = row['version'] if row and row['version'] else 0
                db_lock_version = row['lock_version'] if row and row['lock_version'] else 0

                # Sequence validation: ALWAYS reject lower or equal sequences
                # This is critical for preventing token loss from stale/replayed records
                if new_sequence <= existing_sequence:
                    sequence_delta = existing_sequence - new_sequence

                    # Log anomaly if delta is suspiciously large (for forensics only)
                    if sequence_delta > 100:
                        logger.error(
                            f"SEQUENCE ANOMALY DETECTED for {ipns_name[:16]}...: "
                            f"cached seq={existing_sequence}, incoming seq={new_sequence}, delta={sequence_delta}"
                        )
                        await _log_forensic_event_async(
                            self.db, "sequence_anomaly", ipns_name,
                            {
                                "cached_sequence": existing_sequence,
                                "incoming_sequence": new_sequence,
                                "delta": sequence_delta,
                                "action": "rejected"
                            }
                        )

                    # ALWAYS reject - no exceptions for lower/equal sequences
                    logger.warning(
                        f"Rejecting IPNS record for {ipns_name[:16]}...: "
                        f"new seq={new_sequence} <= existing seq={existing_sequence}"
                    )
                    return False

                # Chain and version validation (if we have a CID to validate)
                if cid:
                    validation = await validate_version_chain(
                        ipns_name, cid, current_cid,
                        new_sequence, existing_sequence,
                        current_version,
                        self.db, self.metrics
                    )

                    if not validation.valid:
                        logger.warning(
                            f"Rejecting IPNS record for {ipns_name[:16]}...: "
                            f"chain validation failed: {validation.reason}"
                        )
                        return False

                    # Extract version and lastCid from validation result
                    version = validation.version if validation.version else 0
                    # Store the _meta.lastCid from the new content (chain link)
                    last_cid_to_store = validation.last_cid
                else:
                    version = 0
                    last_cid_to_store = None

                # Store the record with optimistic locking (using lock_version, not content version)
                if row is None:
                    # INSERT for new records (lock_version starts at 1)
                    cursor.execute(
                        """INSERT INTO ipns_records
                           (ipns_name, marshalled_record, cid, sequence, last_cid, version, lock_version, last_updated)
                           VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)""",
                        (ipns_name, record_bytes, cid, new_sequence, last_cid_to_store, version)
                    )
                else:
                    # UPDATE with lock_version check (optimistic lock)
                    # Atomically increment lock_version to prevent race conditions
                    cursor.execute(
                        """UPDATE ipns_records
                           SET marshalled_record=?, cid=?, sequence=?, last_cid=?, version=?,
                               lock_version = lock_version + 1, last_updated=CURRENT_TIMESTAMP
                           WHERE ipns_name = ? AND lock_version = ?""",
                        (record_bytes, cid, new_sequence, last_cid_to_store, version, ipns_name, db_lock_version)
                    )

                    if cursor.rowcount == 0:
                        # Another update happened concurrently, retry
                        logger.warning(
                            f"Concurrent update detected for {ipns_name[:16]}..., "
                            f"retrying (attempt {attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(0.1 * (attempt + 1))  # Exponential backoff
                        continue

                self.db.commit()
                self.metrics.ipns_records_stored += 1
                logger.info(f"Stored IPNS record: {ipns_name[:16]}... seq={new_sequence} v={version}")
                return True

            except Exception as e:
                logger.error(f"Error storing IPNS record (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return False
                await asyncio.sleep(0.1 * (attempt + 1))

        return False

    def get_all_records(self) -> list[tuple[str, bytes]]:
        """Get all stored IPNS records."""
        cursor = self.db.cursor()
        cursor.execute("SELECT ipns_name, marshalled_record FROM ipns_records")
        return [(row['ipns_name'], row['marshalled_record']) for row in cursor.fetchall()]

    async def republish_record(self, ipns_name: str, record_bytes: bytes) -> bool:
        """Republish an IPNS record to kubo DHT."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Kubo routing/put expects multipart form data with 'value-file' field
                # NOTE: Removed allow-offline=true which was preventing DHT propagation!
                files = {'value-file': ('record', record_bytes, 'application/octet-stream')}
                response = await client.post(
                    f"{IPFS_API_URL}/api/v0/routing/put",
                    params={"arg": f"/ipns/{ipns_name}"},
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
# DHT Sync Worker (Background)
# ==========================================

class DhtSyncWorker:
    """
    Background worker that syncs SQLite with Kubo DHT.
    Triggered when cached records become stale.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        ipns_store: IpnsRecordStore,
        subscription_manager: 'IpnsSubscriptionManager'
    ):
        self.db = db
        self.ipns_store = ipns_store
        self.subscription_manager = subscription_manager
        self.sync_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self.in_flight: set[str] = set()  # Deduplicate concurrent syncs

    async def run(self, shutdown_event: asyncio.Event):
        """Main sync loop - processes queue of stale IPNS names."""
        logger.info("DHT sync worker started")

        while not shutdown_event.is_set():
            try:
                # Wait for next IPNS name to sync (with timeout)
                try:
                    ipns_name = await asyncio.wait_for(
                        self.sync_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if ipns_name in self.in_flight:
                    continue  # Already syncing

                self.in_flight.add(ipns_name)
                try:
                    await self._sync_single_record(ipns_name)
                finally:
                    self.in_flight.discard(ipns_name)

            except Exception as e:
                logger.error(f"DHT sync worker error: {e}")
                await asyncio.sleep(1)

        logger.info("DHT sync worker stopped")

    async def sync_record(self, ipns_name: str):
        """Queue an IPNS name for background sync."""
        if ipns_name not in self.in_flight:
            try:
                self.sync_queue.put_nowait(ipns_name)
            except asyncio.QueueFull:
                logger.warning(f"Sync queue full, dropping {ipns_name[:16]}...")

    async def _sync_single_record(self, ipns_name: str):
        """Fetch latest record from DHT and update SQLite if newer."""
        try:
            # Get current sequence from SQLite
            cursor = self.db.cursor()
            cursor.execute(
                'SELECT sequence FROM ipns_records WHERE ipns_name = ?',
                (ipns_name,)
            )
            row = cursor.fetchone()
            db_sequence = row['sequence'] if row else 0

            # Query Kubo DHT
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{IPFS_API_URL}/api/v0/routing/get",
                    params={"arg": f"/ipns/{ipns_name}"}
                )

                if response.status_code != 200:
                    return

                kubo_data = response.json()
                if not kubo_data.get('Extra'):
                    return

                record_bytes = base64.b64decode(kubo_data['Extra'])
                kubo_sequence, cid = parse_ipns_record(record_bytes)

                # Only update if DHT has newer record
                if kubo_sequence > db_sequence:
                    logger.info(
                        f"DHT sync: updating {ipns_name[:16]}... "
                        f"seq {db_sequence} -> {kubo_sequence}"
                    )
                    await self.ipns_store.store_record(ipns_name, record_bytes)

                    # Notify WebSocket subscribers of update
                    await self.subscription_manager.notify(
                        ipns_name, kubo_sequence, cid
                    )
                else:
                    # Just update last_updated timestamp
                    cursor.execute(
                        'UPDATE ipns_records SET last_updated = ? WHERE ipns_name = ?',
                        (datetime.utcnow().isoformat(), ipns_name)
                    )
                    self.db.commit()

        except Exception as e:
            logger.debug(f"DHT sync failed for {ipns_name[:16]}...: {e}")


# ==========================================
# WebSocket Subscription Manager
# ==========================================

class IpnsSubscriptionManager:
    """
    WebSocket subscription manager for IPNS updates.
    Clients subscribe to specific IPNS names and receive push notifications.
    """

    def __init__(self):
        # Map: ipns_name -> set of WebSocket connections
        self.subscriptions: dict[str, set[web.WebSocketResponse]] = {}
        self.lock = asyncio.Lock()

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connection for /ws/ipns."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        logger.info(f"New WebSocket connection from {request.remote}")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_message(ws, msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    break
        finally:
            await self._remove_all_subscriptions(ws)

        return ws

    async def _handle_message(self, ws: web.WebSocketResponse, data: str):
        """Handle incoming WebSocket message."""
        try:
            message = json.loads(data)
            action = message.get('action')

            if action == 'subscribe':
                ipns_names = message.get('names', [])
                for name in ipns_names:
                    if is_valid_ipns_name(name):
                        await self._add_subscription(name, ws)
                await ws.send_json({
                    'type': 'subscribed',
                    'names': ipns_names
                })

            elif action == 'unsubscribe':
                ipns_names = message.get('names', [])
                for name in ipns_names:
                    await self._remove_subscription(name, ws)
                await ws.send_json({
                    'type': 'unsubscribed',
                    'names': ipns_names
                })

            elif action == 'ping':
                await ws.send_json({'type': 'pong'})

        except json.JSONDecodeError:
            await ws.send_json({'type': 'error', 'message': 'Invalid JSON'})
        except Exception as e:
            logger.error(f"WebSocket message error: {e}")

    async def _add_subscription(self, ipns_name: str, ws: web.WebSocketResponse):
        async with self.lock:
            if ipns_name not in self.subscriptions:
                self.subscriptions[ipns_name] = set()
            self.subscriptions[ipns_name].add(ws)

    async def _remove_subscription(self, ipns_name: str, ws: web.WebSocketResponse):
        async with self.lock:
            if ipns_name in self.subscriptions:
                self.subscriptions[ipns_name].discard(ws)
                if not self.subscriptions[ipns_name]:
                    del self.subscriptions[ipns_name]

    async def _remove_all_subscriptions(self, ws: web.WebSocketResponse):
        async with self.lock:
            for name in list(self.subscriptions.keys()):
                self.subscriptions[name].discard(ws)
                if not self.subscriptions[name]:
                    del self.subscriptions[name]

    async def notify(self, ipns_name: str, sequence: int, cid: str | None):
        """Notify all subscribers of an IPNS update."""
        async with self.lock:
            subscribers = self.subscriptions.get(ipns_name, set()).copy()

        if not subscribers:
            return

        message = json.dumps({
            'type': 'update',
            'name': ipns_name,
            'sequence': sequence,
            'cid': cid,
            'timestamp': datetime.utcnow().isoformat()
        })

        for ws in subscribers:
            try:
                if not ws.closed:
                    await ws.send_str(message)
            except Exception as e:
                logger.debug(f"Failed to notify subscriber: {e}")


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

    def __init__(self, ipns_store: IpnsRecordStore, db: sqlite3.Connection, metrics: Metrics, port: int = HTTP_PORT, scheduler: 'ReannounceScheduler' = None):
        self.ipns_store = ipns_store
        self.db = db
        self.metrics = metrics
        self.port = port
        self.scheduler = scheduler
        self.app = web.Application()

        # Initialize subscription manager for WebSocket IPNS updates
        self.subscription_manager = IpnsSubscriptionManager()

        # Initialize DHT sync worker for background synchronization
        self.dht_sync_worker = DhtSyncWorker(
            db, ipns_store, self.subscription_manager
        )

        self._setup_routes()

    def _setup_routes(self):
        """Set up HTTP routes."""
        self.app.router.add_post('/ipns-intercept', self._handle_ipns_intercept)
        self.app.router.add_get('/health', self._handle_health)
        self.app.router.add_get('/metrics', self._handle_metrics)
        self.app.router.add_post('/reannounce', self._handle_reannounce)
        # Fast-path serving endpoints
        self.app.router.add_get('/routing-get', self._handle_routing_get)
        self.app.router.add_post('/routing-get', self._handle_routing_get)  # Support POST like kubo
        self.app.router.add_get('/pin-status', self._handle_pin_status)
        # WebSocket endpoint for IPNS subscriptions
        self.app.router.add_get('/ws/ipns', self.subscription_manager.handle_websocket)

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
                await self.ipns_store.store_record(ipns_name, record_bytes)
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
                "chain_validation": {
                    "passed": self.metrics.chain_validations_passed,
                    "failed_break": self.metrics.chain_validations_failed_break,
                    "failed_fetch": self.metrics.chain_validations_failed_fetch,
                    "skipped": self.metrics.chain_validations_skipped,
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

    async def _handle_reannounce(self, request: web.Request) -> web.Response:
        """Manually trigger re-announcement of all IPNS records and CIDs."""
        try:
            if not self.scheduler:
                return web.Response(
                    status=503,
                    content_type='application/json',
                    text=json.dumps({"status": "error", "message": "Scheduler not available"})
                )

            logger.info("Manual re-announcement triggered via HTTP")
            await self.scheduler._do_reannouncement()

            return web.Response(
                status=200,
                content_type='application/json',
                text=json.dumps({
                    "status": "ok",
                    "message": "Re-announcement triggered",
                    "reannouncements": self.metrics.reannouncements
                })
            )
        except Exception as e:
            logger.error(f"Manual reannounce error: {e}")
            return web.Response(
                status=500,
                content_type='application/json',
                text=json.dumps({"status": "error", "message": str(e)})
            )

    def _is_stale(self, last_updated: str | None) -> bool:
        """Check if record is older than STALE_THRESHOLD_SECONDS."""
        if not last_updated:
            return True
        try:
            # Handle both ISO format with and without timezone
            if last_updated.endswith('Z'):
                updated_time = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
            elif '+' in last_updated or last_updated.endswith('+00:00'):
                updated_time = datetime.fromisoformat(last_updated)
            else:
                # Assume UTC if no timezone
                updated_time = datetime.fromisoformat(last_updated)
                updated_time = updated_time.replace(tzinfo=None)
                age = (datetime.utcnow() - updated_time).total_seconds()
                return age > STALE_THRESHOLD_SECONDS

            age = (datetime.now(updated_time.tzinfo) - updated_time).total_seconds()
            return age > STALE_THRESHOLD_SECONDS
        except Exception:
            return True

    async def _fetch_from_dht_and_store(self, ipns_name: str) -> web.Response:
        """Blocking DHT fetch for records not in cache."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{IPFS_API_URL}/api/v0/routing/get",
                    params={"arg": f"/ipns/{ipns_name}"}
                )
                if response.status_code == 200:
                    kubo_data = response.json()
                    if kubo_data.get('Extra'):
                        record_bytes = base64.b64decode(kubo_data['Extra'])
                        sequence, cid = parse_ipns_record(record_bytes)

                        # Store in SQLite for future fast lookups
                        await self.ipns_store.store_record(ipns_name, record_bytes)

                        # Notify WebSocket subscribers
                        await self.subscription_manager.notify(ipns_name, sequence, cid)

                        return web.Response(
                            status=200,
                            content_type='application/json',
                            text=json.dumps(kubo_data),
                            headers={
                                'X-IPNS-Source': 'kubo',
                                'X-IPNS-Sequence': str(sequence)
                            }
                        )
            return web.Response(status=404, text='Not found')
        except Exception as e:
            logger.error(f"DHT fetch error for {ipns_name[:16]}...: {e}")
            return web.Response(status=500, text=str(e))

    async def _refresh_and_push(self, ipns_name: str):
        """
        Background refresh with WebSocket notification (non-blocking).
        Called when serving stale data - refreshes from DHT and pushes update.
        """
        try:
            # Fetch from DHT (up to 10s timeout)
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{IPFS_API_URL}/api/v0/routing/get",
                    params={"arg": f"/ipns/{ipns_name}"}
                )

                if response.status_code != 200:
                    logger.debug(f"Background refresh: DHT returned {response.status_code} for {ipns_name[:16]}...")
                    return

                kubo_data = response.json()
                if not kubo_data.get('Extra'):
                    return

                record_bytes = base64.b64decode(kubo_data['Extra'])
                sequence, cid = parse_ipns_record(record_bytes)

                # Store in cache (this validates sequence and chain)
                stored = await self.ipns_store.store_record(ipns_name, record_bytes)

                if stored:
                    # Push to all subscribed clients via WebSocket
                    await self.subscription_manager.notify(ipns_name, sequence, cid)
                    logger.info(f"Background refresh complete: {ipns_name[:16]}... seq={sequence}")

        except asyncio.TimeoutError:
            logger.warning(f"Background refresh timeout for {ipns_name[:16]}...")
        except Exception as e:
            logger.error(f"Background refresh failed for {ipns_name[:16]}...: {e}")

    async def _handle_routing_get(self, request: web.Request) -> web.Response:
        """
        Fast-path IPNS resolution: SQLite-first, async DHT sync.
        Target latency: 5-20ms for cached records.

        Flow:
        1. Query SQLite immediately (5-20ms)
        2. If record exists, return it immediately
        3. If record is stale, trigger async DHT sync (non-blocking)
        4. If no record exists, blocking DHT fetch (first request only)
        """
        try:
            # Extract IPNS name from query param: ?arg=/ipns/{name}
            arg = request.query.get('arg', '')
            match = re.match(r'^/ipns/(.+)$', arg)
            if not match:
                logger.debug(f"routing-get: Invalid arg format: {arg}")
                return web.Response(status=400, text='Invalid arg format')

            ipns_name = match.group(1)

            if not is_valid_ipns_name(ipns_name):
                logger.debug(f"routing-get: Invalid IPNS name: {ipns_name}")
                return web.Response(status=400, text='Invalid IPNS name')

            # 1. Query SQLite immediately (5-20ms)
            cursor = self.db.cursor()
            cursor.execute(
                'SELECT marshalled_record, cid, sequence, last_updated FROM ipns_records WHERE ipns_name = ?',
                (ipns_name,)
            )
            row = cursor.fetchone()

            if row:
                db_record = row['marshalled_record']
                db_sequence = row['sequence'] or 0
                last_updated = row['last_updated']

                # Calculate age in seconds for staleness headers
                age_seconds = 0
                is_stale = False
                try:
                    if last_updated:
                        if last_updated.endswith('Z'):
                            updated_time = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                            age_seconds = (datetime.now(updated_time.tzinfo) - updated_time).total_seconds()
                        elif '+' in last_updated:
                            updated_time = datetime.fromisoformat(last_updated)
                            age_seconds = (datetime.now(updated_time.tzinfo) - updated_time).total_seconds()
                        else:
                            updated_time = datetime.fromisoformat(last_updated)
                            age_seconds = (datetime.utcnow() - updated_time).total_seconds()
                        is_stale = age_seconds > STALE_THRESHOLD_SECONDS
                    else:
                        is_stale = True
                        age_seconds = STALE_THRESHOLD_SECONDS + 1
                except Exception:
                    is_stale = True
                    age_seconds = STALE_THRESHOLD_SECONDS + 1

                # ALWAYS return cached data immediately (maintains <50ms target)
                # If stale, trigger non-blocking background refresh
                if is_stale:
                    # Non-blocking: queue DHT sync task with WebSocket push
                    asyncio.create_task(self._refresh_and_push(ipns_name))
                    logger.debug(f"routing-get: Queued async DHT sync for stale record {ipns_name[:16]}... (age={int(age_seconds)}s)")

                # Return immediately from SQLite with staleness headers
                response_data = {
                    "Extra": base64.b64encode(db_record).decode('ascii'),
                    "Type": 5
                }
                return web.Response(
                    status=200,
                    content_type='application/json',
                    text=json.dumps(response_data),
                    headers={
                        'X-IPNS-Source': 'sidecar-cache',
                        'X-IPNS-Sequence': str(db_sequence),
                        'X-IPNS-Last-Updated': last_updated or '',
                        'X-IPNS-Age': str(int(age_seconds)),
                        'X-IPNS-Stale': 'true' if is_stale else 'false',
                        'Cache-Control': f'max-age={STALE_THRESHOLD_SECONDS}, stale-while-revalidate=30'
                    }
                )
            else:
                # No record in SQLite - must query DHT (blocking for first fetch)
                logger.info(f"routing-get: No cache for {ipns_name[:16]}..., fetching from DHT")
                return await self._fetch_from_dht_and_store(ipns_name)

        except Exception as e:
            logger.error(f"routing-get error: {e}")
            return web.Response(status=500, text=str(e))

    async def _handle_pin_status(self, request: web.Request) -> web.Response:
        """
        Check if a CID is pinned locally.
        Used for instant verification without IPFS API call.
        """
        try:
            cid = request.query.get('cid', '')

            if not cid:
                return web.Response(
                    status=400,
                    content_type='application/json',
                    text=json.dumps({"error": "Missing cid parameter"})
                )

            if not is_valid_cid(cid):
                return web.Response(
                    status=400,
                    content_type='application/json',
                    text=json.dumps({"error": "Invalid CID format"})
                )

            # Query database
            cursor = self.db.cursor()
            cursor.execute(
                'SELECT pinned_at, source FROM pinned_cids WHERE cid = ?',
                (cid,)
            )
            row = cursor.fetchone()

            if row:
                return web.Response(
                    status=200,
                    content_type='application/json',
                    text=json.dumps({
                        "pinned": True,
                        "cid": cid,
                        "pinned_at": row['pinned_at'],
                        "source": row['source']
                    })
                )
            else:
                return web.Response(
                    status=200,
                    content_type='application/json',
                    text=json.dumps({
                        "pinned": False,
                        "cid": cid
                    })
                )

        except Exception as e:
            logger.error(f"pin-status error: {e}")
            return web.Response(
                status=500,
                content_type='application/json',
                text=json.dumps({"error": str(e)})
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
    logger.info(f"  Stale threshold: {STALE_THRESHOLD_SECONDS}s")
    logger.info(f"  Chain validation: {CHAIN_VALIDATION_ENABLED} (mode: {CHAIN_VALIDATION_MODE})")
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
    http_server = IpnsInterceptServer(ipns_store, db, metrics, HTTP_PORT, scheduler=scheduler)

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

    # Start DHT sync worker for background IPNS record synchronization
    dht_sync_task = asyncio.create_task(
        http_server.dht_sync_worker.run(shutdown_event),
        name="dht-sync"
    )

    logger.info(f"Service started: {len(relay_tasks)} relay(s), rate-limited queue, scheduler, DHT sync worker")

    # Wait for shutdown
    await shutdown_event.wait()

    # Cleanup
    logger.info("Shutting down...")

    # Cancel all tasks
    for task in relay_tasks + [queue_task, scheduler_task, dht_sync_task]:
        task.cancel()

    await asyncio.gather(*relay_tasks, queue_task, scheduler_task, dht_sync_task, return_exceptions=True)

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
    logger.info(f"  Chain validation:")
    logger.info(f"    Passed:         {metrics.chain_validations_passed}")
    logger.info(f"    Failed (break): {metrics.chain_validations_failed_break}")
    logger.info(f"    Failed (fetch): {metrics.chain_validations_failed_fetch}")
    logger.info(f"    Skipped:        {metrics.chain_validations_skipped}")
    logger.info("=" * 40)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
