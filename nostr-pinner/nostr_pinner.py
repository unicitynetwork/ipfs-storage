#!/usr/bin/env python3
"""
Nostr IPFS Pin Service

Subscribes to Nostr relays and pins announced CIDs to local IPFS node.
Designed for open access - any Nostr pubkey can request pins.

Environment Variables:
    NOSTR_RELAYS: Comma-separated relay URLs (default: wss://relay.damus.io,wss://nos.lol)
    IPFS_API_URL: IPFS API endpoint (default: http://ipfs:5001)
    PIN_KIND: Nostr event kind for pin requests (default: 30078)
    LOG_LEVEL: Logging level (default: INFO)
    RECONNECT_DELAY: Seconds to wait before reconnecting (default: 10)
    PIN_TIMEOUT: Pin operation timeout in seconds (default: 300)
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass
from typing import Optional

import httpx
import websockets
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

# CID validation regex (CIDv0 and CIDv1)
# CIDv1 prefixes: bafy (dag-pb), baga (dag-json), bafk (raw), etc.
CID_REGEX = re.compile(r'^(Qm[1-9A-HJ-NP-Za-km-z]{44,}|baf[a-z][a-z2-7]{50,}|bag[a-z][a-z2-7]{50,})$')

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
    name: Optional[str] = None
    relay: str = ""


# ==========================================
# IPFS Pinning
# ==========================================

async def pin_cid(cid: str, timeout: int = PIN_TIMEOUT) -> bool:
    """
    Pin a CID to the local IPFS node.

    Args:
        cid: The IPFS CID to pin
        timeout: Operation timeout in seconds

    Returns:
        True if pinning succeeded, False otherwise
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{IPFS_API_URL}/api/v0/pin/add",
                params={"arg": cid, "progress": "false"}
            )

            if response.status_code == 200:
                logger.info(f"Successfully pinned: {cid}")
                return True
            else:
                logger.error(f"Failed to pin {cid}: HTTP {response.status_code} - {response.text}")
                return False

    except httpx.TimeoutException:
        logger.error(f"Timeout pinning {cid} (>{timeout}s)")
        return False
    except httpx.RequestError as e:
        logger.error(f"Request error pinning {cid}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error pinning {cid}: {e}")
        return False


def is_valid_cid(cid: str) -> bool:
    """Validate CID format (CIDv0 or CIDv1)."""
    return bool(CID_REGEX.match(cid))


# ==========================================
# Nostr Event Parsing
# ==========================================

def parse_pin_request(event: dict, relay: str) -> Optional[PinRequest]:
    """
    Parse a Nostr event into a PinRequest if valid.

    Expects events with:
    - kind: PIN_KIND (30078)
    - tags: [["d", "ipfs-pin"], ["cid", "<CID>"], ["name", "<optional>"]]

    Args:
        event: Nostr event dictionary
        relay: Source relay URL

    Returns:
        PinRequest if valid, None otherwise
    """
    try:
        kind = event.get("kind")
        if kind != PIN_KIND:
            return None

        pubkey = event.get("pubkey", "")
        event_id = event.get("id", "")
        tags = event.get("tags", [])

        # Check for ipfs-pin d-tag
        has_pin_tag = False
        cid = None
        name = None

        for tag in tags:
            if len(tag) >= 2:
                if tag[0] == "d" and tag[1] == "ipfs-pin":
                    has_pin_tag = True
                elif tag[0] == "cid":
                    cid = tag[1].strip()
                elif tag[0] == "name":
                    name = tag[1]

        if not has_pin_tag:
            logger.debug(f"Event {event_id[:8]}... missing ipfs-pin d-tag")
            return None

        if not cid:
            logger.debug(f"Event {event_id[:8]}... missing cid tag")
            return None

        if not is_valid_cid(cid):
            logger.warning(f"Invalid CID format: {cid[:20]}...")
            return None

        return PinRequest(
            cid=cid,
            pubkey=pubkey,
            event_id=event_id,
            name=name,
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
    pin_queue: asyncio.Queue,
    shutdown_event: asyncio.Event
):
    """
    Connect to a Nostr relay and subscribe to pin events.

    Handles reconnection on disconnect.

    Args:
        relay_url: WebSocket URL of the relay
        pin_queue: Queue to put valid pin requests
        shutdown_event: Event to signal shutdown
    """
    subscription_id = "ipfs-pin-sub"

    # Filter for PIN_KIND events with ipfs-pin d-tag
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

                # Send subscription request
                req = ["REQ", subscription_id] + filters
                await ws.send(json.dumps(req))
                logger.debug(f"Subscribed to {relay_url} for kind {PIN_KIND}")

                # Listen for events
                while not shutdown_event.is_set():
                    try:
                        # Use wait_for to allow checking shutdown_event
                        message = await asyncio.wait_for(
                            ws.recv(),
                            timeout=30.0
                        )

                        try:
                            data = json.loads(message)

                            if data[0] == "EVENT" and len(data) >= 3:
                                event = data[2]
                                pin_req = parse_pin_request(event, relay_url)

                                if pin_req:
                                    logger.info(
                                        f"Pin request from {pin_req.pubkey[:8]}... "
                                        f"CID: {pin_req.cid[:16]}..."
                                    )
                                    await pin_queue.put(pin_req)

                            elif data[0] == "EOSE":
                                logger.debug(f"End of stored events from {relay_url}")

                            elif data[0] == "NOTICE":
                                logger.warning(f"Relay notice from {relay_url}: {data[1]}")

                        except (json.JSONDecodeError, IndexError, TypeError) as e:
                            logger.debug(f"Failed to parse message from {relay_url}: {e}")

                    except asyncio.TimeoutError:
                        # Send ping to keep connection alive
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
# Pin Worker
# ==========================================

async def pin_worker(
    pin_queue: asyncio.Queue,
    shutdown_event: asyncio.Event
):
    """
    Worker that processes pin requests from the queue.

    Handles deduplication within a session.

    Args:
        pin_queue: Queue of PinRequest objects
        shutdown_event: Event to signal shutdown
    """
    pinned_cids: set[str] = set()
    pending_count = 0

    logger.info("Pin worker started")

    while not shutdown_event.is_set():
        try:
            # Wait for pin request with timeout to check shutdown
            try:
                pin_req = await asyncio.wait_for(
                    pin_queue.get(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            # Check for duplicates
            if pin_req.cid in pinned_cids:
                logger.debug(f"Already pinned this session: {pin_req.cid[:16]}...")
                pin_queue.task_done()
                continue

            # Pin the CID
            pending_count += 1
            logger.info(
                f"Pinning CID: {pin_req.cid} "
                f"(from {pin_req.pubkey[:8]}..., queue: {pin_queue.qsize()})"
            )

            success = await pin_cid(pin_req.cid)

            if success:
                pinned_cids.add(pin_req.cid)
                logger.info(
                    f"Pin complete: {pin_req.cid[:16]}... "
                    f"(total pinned: {len(pinned_cids)})"
                )
            else:
                logger.warning(f"Pin failed: {pin_req.cid[:16]}...")

            pending_count -= 1
            pin_queue.task_done()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Pin worker error: {e}")

    logger.info(f"Pin worker stopped. Pinned {len(pinned_cids)} CIDs this session.")


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
    logger.info("=" * 50)
    logger.info("  Nostr IPFS Pin Service")
    logger.info("=" * 50)
    logger.info(f"  IPFS API:    {IPFS_API_URL}")
    logger.info(f"  Relays:      {len(NOSTR_RELAYS)}")
    logger.info(f"  Event kind:  {PIN_KIND}")
    logger.info("=" * 50)

    # Wait for IPFS to be available
    if not await wait_for_ipfs():
        logger.error("Exiting: IPFS API not available")
        sys.exit(1)

    # Create shared resources
    pin_queue: asyncio.Queue[PinRequest] = asyncio.Queue()
    shutdown_event = asyncio.Event()

    # Handle shutdown signals
    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Start relay subscriptions
    relay_tasks = [
        asyncio.create_task(
            subscribe_to_relay(relay.strip(), pin_queue, shutdown_event),
            name=f"relay-{relay}"
        )
        for relay in NOSTR_RELAYS
        if relay.strip()
    ]

    # Start pin worker
    worker_task = asyncio.create_task(
        pin_worker(pin_queue, shutdown_event),
        name="pin-worker"
    )

    logger.info(f"Monitoring {len(relay_tasks)} relay(s) for pin requests...")

    # Wait for shutdown
    await shutdown_event.wait()

    # Cleanup
    logger.info("Shutting down...")

    # Cancel all tasks
    for task in relay_tasks + [worker_task]:
        task.cancel()

    # Wait for tasks to complete
    await asyncio.gather(*relay_tasks, worker_task, return_exceptions=True)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
