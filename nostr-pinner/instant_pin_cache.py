"""
Instant-pin write-through cache for the IPFS sidecar.

Closes the publish-then-read window: after the sidecar acknowledges a CAR
submit, the bytes are durably retrievable by CID from {sidecar blobstore,
Kubo blockstore} within milliseconds.

Submit path
-----------
1. Caller POSTs raw bytes to /sidecar/submit?cid=<cid>.
2. Bytes are written atomically to <SIDECAR_CACHE_DIR>/<cid[:2]>/<cid> and a
   row is upserted in `instant_pin_cache` (state='pending').
3. Caller receives 200 immediately.
4. A background reconciler tries to push the bytes to Kubo via
   `/api/v0/block/put` and (once stored) `/api/v0/pin/add`; when Kubo
   confirms availability the row flips to 'in-kubo' and the blob is removed
   from disk. SQLite row is retained for audit.

Read path
---------
GET /sidecar/blob?cid=<cid> returns the bytes if present, else 404. nginx
falls back to Kubo on 404 for the public /ipfs/<cid> path.

Capacity / back-pressure
------------------------
The cache is bounded by total bytes (SIDECAR_CACHE_MAX_BYTES, default 1 GiB)
AND entry count (SIDECAR_CACHE_MAX_ENTRIES, default 100_000). When full:

* If there is room to evict already-confirmed (`in-kubo`) entries, evict LRU.
* If only `pending` entries remain, refuse the new submit with HTTP 503 so
  the writer back-pressures. We NEVER evict pending blobs - that would
  silently lose data the writer thinks is durable.

Promotion timeout
-----------------
If a row sits at `pending` longer than SIDECAR_CACHE_PROMOTION_TIMEOUT
(default 24 h), the reconciler emits a WARN log + metric. It does NOT
silently age out - operator visibility matters more than auto-cleanup here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ----- Configuration defaults -----------------------------------------------

DEFAULT_CACHE_DIR = "/data/ipfs/sidecar-cache"
DEFAULT_MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_RECONCILE_INTERVAL = 5  # seconds
DEFAULT_PROMOTION_TIMEOUT = 24 * 60 * 60  # 24h alert threshold
DEFAULT_KUBO_PIN_TIMEOUT = 30  # seconds; per-promotion call cap
DEFAULT_MAX_BLOB_BYTES = 32 * 1024 * 1024  # 32 MiB per submission


class SubmitOutcome(Enum):
    ACCEPTED = "accepted"  # New blob stored
    ALREADY_PRESENT = "already_present"  # CID already cached locally
    ALREADY_CONFIRMED = "already_confirmed"  # CID already in-kubo (no need to cache)
    CACHE_FULL = "cache_full"  # Back-pressure: cap reached with no evictable entries
    TOO_LARGE = "too_large"  # Body exceeded SIDECAR_CACHE_MAX_BLOB_BYTES
    INVALID = "invalid"  # Bad CID or empty body
    DISABLED = "disabled"  # Cache disabled via env


@dataclass
class SubmitResult:
    outcome: SubmitOutcome
    detail: str = ""

    @property
    def http_status(self) -> int:
        if self.outcome in (SubmitOutcome.ACCEPTED, SubmitOutcome.ALREADY_PRESENT, SubmitOutcome.ALREADY_CONFIRMED):
            return 200
        if self.outcome == SubmitOutcome.CACHE_FULL:
            return 503
        if self.outcome == SubmitOutcome.TOO_LARGE:
            return 413
        if self.outcome == SubmitOutcome.DISABLED:
            return 503
        return 400


@dataclass
class CacheStats:
    enabled: bool = True
    pending_entries: int = 0
    confirmed_entries: int = 0
    bytes_used_pending: int = 0
    bytes_used_total: int = 0
    accepted_submits: int = 0
    rejected_full: int = 0
    rejected_too_large: int = 0
    promotions_succeeded: int = 0
    promotion_attempts: int = 0
    promotion_failures: int = 0
    promotion_timeout_alerts: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


# ----- Schema migration -----------------------------------------------------

def init_instant_pin_cache_schema(db: sqlite3.Connection) -> None:
    """Idempotent schema migration. Safe to call on every startup."""
    cursor = db.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS instant_pin_cache (
            cid TEXT PRIMARY KEY,
            submitted_at INTEGER NOT NULL,
            byte_size INTEGER NOT NULL,
            state TEXT NOT NULL,
            kubo_confirmed_at INTEGER,
            last_error TEXT,
            last_promotion_attempt INTEGER,
            promotion_attempts INTEGER DEFAULT 0,
            last_accessed_at INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ipc_state ON instant_pin_cache(state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ipc_last_accessed ON instant_pin_cache(last_accessed_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ipc_submitted ON instant_pin_cache(submitted_at)")
    db.commit()
    logger.info("instant_pin_cache schema initialized")


# ----- Filesystem blob layout -----------------------------------------------

def _blob_path(blob_dir: str, cid: str) -> str:
    # Two-char fanout keeps directory entries shallow without growing too many
    # subdirs. CIDs differ enough in their prefix bytes that this works for
    # both CIDv0 (Qm...) and CIDv1 (baf..., bag...).
    return os.path.join(blob_dir, cid[:2], cid)


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{time.time_ns()}"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"instant_pin_cache: unlink failed for {path}: {e}")


# ----- The cache itself -----------------------------------------------------

class InstantPinCache:
    """
    Write-through cache that holds CAR/block bytes until Kubo confirms them.

    Thread-safety: all public coroutines acquire `self._lock` (asyncio.Lock)
    before touching the DB or filesystem. The DB connection is the same one
    used by the rest of the nostr_pinner process; SQLite serializes writes
    internally via the connection lock, but we still serialize at the cache
    level so the LRU/byte-cap bookkeeping is consistent with the DB state.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        ipfs_api_url: str,
        blob_dir: str = DEFAULT_CACHE_DIR,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        reconcile_interval_s: int = DEFAULT_RECONCILE_INTERVAL,
        promotion_timeout_s: int = DEFAULT_PROMOTION_TIMEOUT,
        kubo_pin_timeout_s: int = DEFAULT_KUBO_PIN_TIMEOUT,
        max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
        enabled: bool = True,
    ):
        self.db = db
        self.ipfs_api_url = ipfs_api_url.rstrip("/")
        self.blob_dir = blob_dir
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.reconcile_interval_s = reconcile_interval_s
        self.promotion_timeout_s = promotion_timeout_s
        self.kubo_pin_timeout_s = kubo_pin_timeout_s
        self.max_blob_bytes = max_blob_bytes
        self.enabled = enabled
        self._lock = asyncio.Lock()
        self._stats = CacheStats(enabled=enabled)

        if self.enabled:
            os.makedirs(self.blob_dir, exist_ok=True)
            init_instant_pin_cache_schema(self.db)
            self._reload_stats()
            logger.info(
                f"InstantPinCache: dir={blob_dir} max_bytes={max_bytes} "
                f"max_entries={max_entries} reconcile_every={reconcile_interval_s}s "
                f"promotion_timeout={promotion_timeout_s}s"
            )
        else:
            logger.info("InstantPinCache: DISABLED via SIDECAR_CACHE_ENABLED=false")

    # ----- Stats / introspection --------------------------------------------

    def _reload_stats(self) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT state, COUNT(*) AS n, COALESCE(SUM(byte_size), 0) AS bytes "
            "FROM instant_pin_cache GROUP BY state"
        )
        pending_n = 0
        confirmed_n = 0
        pending_bytes = 0
        total_bytes = 0
        for row in cursor.fetchall():
            state = row["state"]
            n = row["n"]
            b = row["bytes"]
            total_bytes += b
            if state == "pending":
                pending_n = n
                pending_bytes = b
            elif state == "in-kubo":
                confirmed_n = n
        self._stats.pending_entries = pending_n
        self._stats.confirmed_entries = confirmed_n
        self._stats.bytes_used_pending = pending_bytes
        self._stats.bytes_used_total = total_bytes

    def stats(self) -> dict:
        """Snapshot of current cache stats. Cheap and safe to call from /metrics."""
        s = self._stats
        return {
            "enabled": s.enabled,
            "pending_entries": s.pending_entries,
            "confirmed_entries": s.confirmed_entries,
            "bytes_used_pending": s.bytes_used_pending,
            "bytes_used_total": s.bytes_used_total,
            "max_bytes": self.max_bytes,
            "max_entries": self.max_entries,
            "accepted_submits": s.accepted_submits,
            "rejected_full": s.rejected_full,
            "rejected_too_large": s.rejected_too_large,
            "promotions_succeeded": s.promotions_succeeded,
            "promotion_attempts": s.promotion_attempts,
            "promotion_failures": s.promotion_failures,
            "promotion_timeout_alerts": s.promotion_timeout_alerts,
            "cache_hits": s.cache_hits,
            "cache_misses": s.cache_misses,
        }

    # ----- Submit path -------------------------------------------------------

    async def submit(self, cid: str, data: bytes) -> SubmitResult:
        if not self.enabled:
            return SubmitResult(SubmitOutcome.DISABLED, "instant_pin_cache disabled")

        if not cid or not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            return SubmitResult(SubmitOutcome.INVALID, "empty cid or body")

        if len(data) > self.max_blob_bytes:
            self._stats.rejected_too_large += 1
            return SubmitResult(
                SubmitOutcome.TOO_LARGE,
                f"body {len(data)} > max {self.max_blob_bytes}",
            )

        async with self._lock:
            cursor = self.db.cursor()
            cursor.execute(
                "SELECT state, byte_size FROM instant_pin_cache WHERE cid = ?",
                (cid,),
            )
            row = cursor.fetchone()
            now = int(time.time())

            if row is not None:
                if row["state"] == "in-kubo":
                    # Already promoted to Kubo - reads will succeed via Kubo path,
                    # no need to re-cache. Just bump last_accessed for LRU tail.
                    cursor.execute(
                        "UPDATE instant_pin_cache SET last_accessed_at = ? WHERE cid = ?",
                        (now, cid),
                    )
                    self.db.commit()
                    return SubmitResult(SubmitOutcome.ALREADY_CONFIRMED, "already in kubo")
                if row["state"] == "pending":
                    # Already cached and unconfirmed. Idempotent submit.
                    cursor.execute(
                        "UPDATE instant_pin_cache SET last_accessed_at = ? WHERE cid = ?",
                        (now, cid),
                    )
                    self.db.commit()
                    return SubmitResult(SubmitOutcome.ALREADY_PRESENT, "already pending")
                # state == 'evicted' - fall through and re-cache

            # Make room if we're at the cap. Evictions touch only `in-kubo` rows.
            has_room = self._make_room_for(len(data))
            if not has_room:
                self._stats.rejected_full += 1
                return SubmitResult(
                    SubmitOutcome.CACHE_FULL,
                    "cache full of unconfirmed entries; retry later",
                )

            # Write blob to disk first, then commit the DB row. If write fails,
            # we leave no DB row.
            path = _blob_path(self.blob_dir, cid)
            try:
                _atomic_write(path, bytes(data))
            except OSError as e:
                logger.error(f"instant_pin_cache: blob write failed for {cid[:16]}...: {e}")
                return SubmitResult(SubmitOutcome.INVALID, f"write failed: {e}")

            cursor.execute(
                "INSERT OR REPLACE INTO instant_pin_cache "
                "(cid, submitted_at, byte_size, state, last_accessed_at, promotion_attempts) "
                "VALUES (?, ?, ?, 'pending', ?, 0)",
                (cid, now, len(data), now),
            )
            self.db.commit()

            self._stats.accepted_submits += 1
            self._stats.pending_entries += 1
            self._stats.bytes_used_pending += len(data)
            self._stats.bytes_used_total += len(data)
            logger.info(
                f"instant_pin_cache: submitted {cid[:16]}... ({len(data)} bytes, "
                f"pending={self._stats.pending_entries} confirmed={self._stats.confirmed_entries})"
            )
            return SubmitResult(SubmitOutcome.ACCEPTED, "stored pending kubo")

    def _make_room_for(self, incoming_bytes: int) -> bool:
        """
        Ensure (entries+1 <= max_entries) and (bytes_used_total + incoming <= max_bytes).

        Eviction targets only state='in-kubo' rows, ordered by last_accessed_at ASC.
        Returns True if there's room (possibly after eviction), False if we'd
        have to evict a pending entry to fit.

        Caller must already hold self._lock.
        """
        cursor = self.db.cursor()

        cursor.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(byte_size), 0) AS b "
            "FROM instant_pin_cache WHERE state IN ('pending','in-kubo')"
        )
        live = cursor.fetchone()
        n_live = live["n"]
        b_live = live["b"]

        if (n_live + 1) <= self.max_entries and (b_live + incoming_bytes) <= self.max_bytes:
            return True

        cursor.execute(
            "SELECT cid, byte_size FROM instant_pin_cache "
            "WHERE state = 'in-kubo' "
            "ORDER BY last_accessed_at ASC"
        )
        candidates = cursor.fetchall()

        for cand in candidates:
            cid = cand["cid"]
            size = cand["byte_size"]
            cursor.execute(
                "UPDATE instant_pin_cache SET state = 'evicted' WHERE cid = ?",
                (cid,),
            )
            self._stats.confirmed_entries = max(0, self._stats.confirmed_entries - 1)
            self._stats.bytes_used_total = max(0, self._stats.bytes_used_total - size)
            n_live -= 1
            b_live -= size
            if (n_live + 1) <= self.max_entries and (b_live + incoming_bytes) <= self.max_bytes:
                self.db.commit()
                return True

        self.db.commit()
        return False

    # ----- Read path ---------------------------------------------------------

    async def get(self, cid: str) -> Optional[bytes]:
        """
        Return cached bytes for `cid` if present and not yet evicted, else None.

        Side effect: bumps last_accessed_at so LRU works.
        """
        if not self.enabled or not cid:
            return None

        async with self._lock:
            cursor = self.db.cursor()
            cursor.execute(
                "SELECT state, byte_size FROM instant_pin_cache WHERE cid = ?",
                (cid,),
            )
            row = cursor.fetchone()
            if row is None:
                self._stats.cache_misses += 1
                return None
            state = row["state"]
            now = int(time.time())
            cursor.execute(
                "UPDATE instant_pin_cache SET last_accessed_at = ? WHERE cid = ?",
                (now, cid),
            )
            self.db.commit()
            if state != "pending":
                # 'in-kubo' and 'evicted' have no disk blob; let Kubo serve.
                self._stats.cache_misses += 1
                return None

            path = _blob_path(self.blob_dir, cid)
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self._stats.cache_hits += 1
                return data
            except FileNotFoundError:
                logger.warning(
                    f"instant_pin_cache: pending row for {cid[:16]}... has no blob on disk; removing row"
                )
                cursor.execute("DELETE FROM instant_pin_cache WHERE cid = ?", (cid,))
                self.db.commit()
                self._stats.pending_entries = max(0, self._stats.pending_entries - 1)
                self._stats.bytes_used_pending = max(0, self._stats.bytes_used_pending - row["byte_size"])
                self._stats.bytes_used_total = max(0, self._stats.bytes_used_total - row["byte_size"])
                self._stats.cache_misses += 1
                return None

    # ----- Reconciler --------------------------------------------------------

    async def run_reconciler(self, shutdown_event: asyncio.Event) -> None:
        """Long-running task: promote pending → in-kubo, alert on stuck rows."""
        if not self.enabled:
            return
        logger.info(f"instant_pin_cache reconciler started (interval={self.reconcile_interval_s}s)")
        while not shutdown_event.is_set():
            try:
                await self._reconcile_once()
            except Exception as e:
                logger.error(f"instant_pin_cache: reconciler iteration error: {e}")
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self.reconcile_interval_s,
                )
                break  # shutdown triggered
            except asyncio.TimeoutError:
                continue
        logger.info("instant_pin_cache reconciler stopped")

    async def _reconcile_once(self) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT cid, byte_size, submitted_at, last_promotion_attempt, promotion_attempts "
            "FROM instant_pin_cache "
            "WHERE state = 'pending' "
            "ORDER BY COALESCE(last_promotion_attempt, 0) ASC, submitted_at ASC "
            "LIMIT 32"
        )
        pending_rows = cursor.fetchall()
        if not pending_rows:
            return

        now = int(time.time())
        for row in pending_rows:
            cid = row["cid"]
            submitted_at = row["submitted_at"]
            attempts = row["promotion_attempts"] or 0

            # Stuck-row alert (operator visibility). We DO NOT delete.
            age = now - submitted_at
            if age > self.promotion_timeout_s:
                last_attempt = row["last_promotion_attempt"] or 0
                if now - last_attempt > (self.promotion_timeout_s // 12):
                    logger.warning(
                        f"instant_pin_cache: CID {cid[:16]}... has been pending for "
                        f"{age}s (> {self.promotion_timeout_s}s threshold, attempts={attempts}). "
                        f"Operator action recommended."
                    )
                    self._stats.promotion_timeout_alerts += 1

            success, err = await self._try_promote(cid)
            self._stats.promotion_attempts += 1

            async with self._lock:
                if success:
                    cursor2 = self.db.cursor()
                    cursor2.execute(
                        "UPDATE instant_pin_cache SET state = 'in-kubo', "
                        "kubo_confirmed_at = ?, last_error = NULL, last_promotion_attempt = ?, "
                        "promotion_attempts = promotion_attempts + 1 "
                        "WHERE cid = ? AND state = 'pending'",
                        (now, now, cid),
                    )
                    changed = cursor2.rowcount
                    self.db.commit()
                    if changed > 0:
                        _safe_unlink(_blob_path(self.blob_dir, cid))
                        self._stats.promotions_succeeded += 1
                        self._stats.pending_entries = max(0, self._stats.pending_entries - 1)
                        self._stats.confirmed_entries += 1
                        self._stats.bytes_used_pending = max(0, self._stats.bytes_used_pending - row["byte_size"])
                        logger.info(
                            f"instant_pin_cache: promoted {cid[:16]}... to kubo "
                            f"(pending={self._stats.pending_entries} confirmed={self._stats.confirmed_entries})"
                        )
                else:
                    cursor2 = self.db.cursor()
                    cursor2.execute(
                        "UPDATE instant_pin_cache SET last_error = ?, "
                        "last_promotion_attempt = ?, promotion_attempts = promotion_attempts + 1 "
                        "WHERE cid = ? AND state = 'pending'",
                        (err[:200] if err else None, now, cid),
                    )
                    self.db.commit()
                    self._stats.promotion_failures += 1

    async def _try_promote(self, cid: str) -> tuple[bool, Optional[str]]:
        """
        Push the locally-cached bytes to Kubo. Returns (success, error_msg).

        1. `ipfs block/stat` - if Kubo already has it, success.
        2. Otherwise, read bytes from disk and POST them to `/api/v0/block/put`
           (codec/format inferred from CID multibase prefix).
        3. Then `/api/v0/pin/add` so Kubo's GC won't reap it.
        4. Re-stat to confirm.
        """
        stat_ok, _ = await self._kubo_block_stat(cid)
        if stat_ok:
            return True, None

        path = _blob_path(self.blob_dir, cid)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return False, "blob missing on disk"
        except OSError as e:
            return False, f"blob read error: {e}"

        put_ok, put_err = await self._kubo_block_put(cid, data)
        if not put_ok:
            return False, put_err

        pin_ok, pin_err = await self._kubo_pin_add(cid)
        if not pin_ok:
            return False, f"block put ok but pin failed: {pin_err}"

        stat_ok, stat_err = await self._kubo_block_stat(cid)
        if not stat_ok:
            return False, f"post-put stat failed: {stat_err}"

        return True, None

    async def _kubo_block_stat(self, cid: str) -> tuple[bool, Optional[str]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"{self.ipfs_api_url}/api/v0/block/stat",
                    params={"arg": cid},
                )
                if resp.status_code == 200:
                    return True, None
                return False, f"http {resp.status_code}"
        except httpx.TimeoutException:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    async def _kubo_block_put(self, cid: str, data: bytes) -> tuple[bool, Optional[str]]:
        params = self._infer_block_put_params(cid)
        try:
            async with httpx.AsyncClient(timeout=self.kubo_pin_timeout_s) as client:
                files = {"data": ("blob", data, "application/octet-stream")}
                resp = await client.post(
                    f"{self.ipfs_api_url}/api/v0/block/put",
                    params=params,
                    files=files,
                )
                if resp.status_code == 200:
                    return True, None
                return False, f"http {resp.status_code}: {resp.text[:200]}"
        except httpx.TimeoutException:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _infer_block_put_params(cid: str) -> dict:
        """
        Choose Kubo /api/v0/block/put hints based on CID multibase prefix.

        - `Qm...` -> CIDv0, dag-pb + sha2-256.
        - `bag...` -> CIDv1, dag-cbor + sha2-256 (UXF CAR blocks typically).
        - everything else (`baf...`) -> raw + sha2-256.

        Kubo verifies the body hashes back to the requested codec; if our
        hint is wrong the returned CID will differ from the caller's, and the
        next block/stat check fails. The promotion then retries with the row
        marked as failed - safe behavior.
        """
        params = {"allow-big-block": "true"}
        if cid.startswith("Qm"):
            params["cid-codec"] = "dag-pb"
            params["mhtype"] = "sha2-256"
        elif cid.startswith("bag"):
            params["cid-codec"] = "dag-cbor"
            params["mhtype"] = "sha2-256"
        else:
            params["cid-codec"] = "raw"
            params["mhtype"] = "sha2-256"
        return params

    async def _kubo_pin_add(self, cid: str) -> tuple[bool, Optional[str]]:
        try:
            async with httpx.AsyncClient(timeout=self.kubo_pin_timeout_s) as client:
                resp = await client.post(
                    f"{self.ipfs_api_url}/api/v0/pin/add",
                    params={"arg": cid, "progress": "false"},
                )
                if resp.status_code == 200:
                    return True, None
                return False, f"http {resp.status_code}"
        except httpx.TimeoutException:
            return False, "timeout"
        except Exception as e:
            return False, str(e)


# ----- Env-driven factory ---------------------------------------------------

def build_from_env(db: sqlite3.Connection, ipfs_api_url: str) -> InstantPinCache:
    enabled = os.getenv("SIDECAR_CACHE_ENABLED", "true").lower() != "false"
    return InstantPinCache(
        db=db,
        ipfs_api_url=ipfs_api_url,
        blob_dir=os.getenv("SIDECAR_CACHE_DIR", DEFAULT_CACHE_DIR),
        max_bytes=int(os.getenv("SIDECAR_CACHE_MAX_BYTES", str(DEFAULT_MAX_BYTES))),
        max_entries=int(os.getenv("SIDECAR_CACHE_MAX_ENTRIES", str(DEFAULT_MAX_ENTRIES))),
        reconcile_interval_s=int(os.getenv("SIDECAR_CACHE_RECONCILE_INTERVAL", str(DEFAULT_RECONCILE_INTERVAL))),
        promotion_timeout_s=int(os.getenv("SIDECAR_CACHE_PROMOTION_TIMEOUT", str(DEFAULT_PROMOTION_TIMEOUT))),
        kubo_pin_timeout_s=int(os.getenv("SIDECAR_CACHE_KUBO_TIMEOUT", str(DEFAULT_KUBO_PIN_TIMEOUT))),
        max_blob_bytes=int(os.getenv("SIDECAR_CACHE_MAX_BLOB_BYTES", str(DEFAULT_MAX_BLOB_BYTES))),
        enabled=enabled,
    )
