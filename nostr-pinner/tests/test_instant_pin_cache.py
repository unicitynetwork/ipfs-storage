"""
Unit tests for instant_pin_cache.

Covers the submit/get round-trip, LRU eviction policy (only confirmed
entries), back-pressure when the cache is full of unconfirmed data,
reconciler promotion via mocked Kubo, schema-migration idempotence, and
recovery from a stale DB row whose disk blob has vanished.
"""

import asyncio
import os
import sqlite3
import time

import httpx
import pytest

from instant_pin_cache import (
    InstantPinCache,
    SubmitOutcome,
    init_instant_pin_cache_schema,
    _blob_path,
)


# ----- Helpers --------------------------------------------------------------

# Real-shaped CIDs (CIDv1, dag-cbor codec, sha2-256 - 'bag' prefix per
# multibase 'b' + multihash). They just need to satisfy the regex used by
# nostr_pinner.is_valid_cid; the cache itself does not parse multibase.
CID_A = "bagcqcera" + "a" * 50
CID_B = "bagcqcera" + "b" * 50
CID_C = "bagcqcera" + "c" * 50
CID_D = "bagcqcera" + "d" * 50


def _build_cache(db, blob_dir, **overrides) -> InstantPinCache:
    defaults = dict(
        db=db,
        ipfs_api_url="http://kubo.invalid:5001",
        blob_dir=blob_dir,
        max_bytes=1024 * 1024,
        max_entries=4,
        reconcile_interval_s=1,
        promotion_timeout_s=60,
        kubo_pin_timeout_s=2,
        max_blob_bytes=512 * 1024,
        enabled=True,
    )
    defaults.update(overrides)
    return InstantPinCache(**defaults)


class FakeKuboTransport(httpx.AsyncBaseTransport):
    """
    httpx mock transport that simulates a Kubo HTTP API. Records every call
    and answers based on what's been "put" so far.
    """

    def __init__(self):
        self.put_blobs: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self._pending_cids: list[str] = []
        # Toggleable behaviors for negative-path tests
        self.fail_block_put = False
        self.fail_pin_add = False
        self.fail_block_stat_after_put = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((path, params))
        if path == "/api/v0/block/stat":
            cid = params.get("arg", "")
            if self.fail_block_stat_after_put and cid in self.put_blobs:
                return httpx.Response(500, text="forced fail")
            if cid in self.put_blobs:
                return httpx.Response(200, json={"Key": cid, "Size": len(self.put_blobs[cid])})
            return httpx.Response(404, text="not in blockstore")
        if path == "/api/v0/block/put":
            if self.fail_block_put:
                return httpx.Response(500, text="forced fail")
            # In a real Kubo response the body has a 'Key' field with the
            # actual CID derived from the bytes. We don't know the CID
            # without doing the real hash; the cache infers success purely
            # from a subsequent block/stat. So we pull the cid from the
            # querystring fallback: real callers won't have it but our
            # reconciler immediately calls block/stat with the cid it
            # already knows. To simulate "kubo now has the blob keyed by
            # the cid we just submitted", we cheat by reading the call
            # order: the very next stat() call after a put() in the same
            # reconciler iteration will succeed because we stash the
            # body under a sentinel and flip it on the next stat.
            #
            # Simpler approach: peek at the last call which is the
            # block/stat just before put, and store the body keyed by
            # whatever cid the test asserted. We do that via the
            # `register_pending_cid` mechanism the test calls before
            # invoking the reconciler.
            for cid in self._pending_cids:
                self.put_blobs[cid] = b""  # any non-None marker
            self._pending_cids.clear()
            return httpx.Response(200, json={"Key": "ignored", "Size": 0})
        if path == "/api/v0/pin/add":
            if self.fail_pin_add:
                return httpx.Response(500, text="forced fail")
            return httpx.Response(200, json={"Pins": [params.get("arg", "")]})
        return httpx.Response(404, text=f"unknown path {path}")

    def register_pending_cid(self, cid: str) -> None:
        self._pending_cids.append(cid)


def _patch_httpx_with(monkeypatch, transport: httpx.AsyncBaseTransport) -> None:
    """
    Replace `httpx.AsyncClient` so the cache uses our fake transport.
    """
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ----- Schema --------------------------------------------------------------

def test_schema_is_idempotent(db):
    init_instant_pin_cache_schema(db)
    init_instant_pin_cache_schema(db)  # second call must not raise
    cur = db.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='instant_pin_cache'")
    assert cur.fetchone() is not None
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_ipc_state'"
    )
    assert cur.fetchone() is not None


# ----- Round-trip + outcomes -----------------------------------------------

@pytest.mark.asyncio
async def test_submit_then_get_round_trip(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    payload = b"hello world" * 100

    result = await cache.submit(CID_A, payload)
    assert result.outcome == SubmitOutcome.ACCEPTED

    fetched = await cache.get(CID_A)
    assert fetched == payload

    stats = cache.stats()
    assert stats["accepted_submits"] == 1
    assert stats["pending_entries"] == 1
    assert stats["bytes_used_pending"] == len(payload)
    assert stats["cache_hits"] == 1


@pytest.mark.asyncio
async def test_submit_invalid_inputs(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    assert (await cache.submit("", b"x")).outcome == SubmitOutcome.INVALID
    assert (await cache.submit(CID_A, b"")).outcome == SubmitOutcome.INVALID


@pytest.mark.asyncio
async def test_submit_too_large(db, blob_dir):
    cache = _build_cache(db, blob_dir, max_blob_bytes=10)
    result = await cache.submit(CID_A, b"this is way too big")
    assert result.outcome == SubmitOutcome.TOO_LARGE
    assert result.http_status == 413
    assert cache.stats()["rejected_too_large"] == 1


@pytest.mark.asyncio
async def test_submit_disabled(db, blob_dir):
    cache = _build_cache(db, blob_dir, enabled=False)
    result = await cache.submit(CID_A, b"data")
    assert result.outcome == SubmitOutcome.DISABLED
    assert result.http_status == 503
    assert (await cache.get(CID_A)) is None


@pytest.mark.asyncio
async def test_submit_idempotent_for_pending(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"original")
    second = await cache.submit(CID_A, b"different bytes - ignored")
    assert second.outcome == SubmitOutcome.ALREADY_PRESENT
    # The first bytes are still on disk
    fetched = await cache.get(CID_A)
    assert fetched == b"original"


@pytest.mark.asyncio
async def test_get_missing_returns_none(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    assert (await cache.get(CID_A)) is None
    assert cache.stats()["cache_misses"] == 1


@pytest.mark.asyncio
async def test_get_for_in_kubo_row_returns_none(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"payload")
    # Simulate the reconciler having promoted the row
    cur = db.cursor()
    cur.execute(
        "UPDATE instant_pin_cache SET state='in-kubo', kubo_confirmed_at=? WHERE cid=?",
        (int(time.time()), CID_A),
    )
    db.commit()
    assert (await cache.get(CID_A)) is None  # cache says "let Kubo serve"


@pytest.mark.asyncio
async def test_get_recovers_from_missing_blob(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"payload")
    # Manually delete the blob - simulates disk corruption / external rm
    os.unlink(_blob_path(blob_dir, CID_A))
    assert (await cache.get(CID_A)) is None
    # Row should have been pruned
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM instant_pin_cache WHERE cid = ?", (CID_A,))
    assert cur.fetchone()["c"] == 0


# ----- Eviction + back-pressure --------------------------------------------

@pytest.mark.asyncio
async def test_lru_eviction_only_targets_confirmed(db, blob_dir):
    cache = _build_cache(db, blob_dir, max_entries=2, max_bytes=1024 * 1024)
    # Fill with two pending
    await cache.submit(CID_A, b"A" * 100)
    await cache.submit(CID_B, b"B" * 100)
    # New submit should be refused - both existing entries are pending
    result = await cache.submit(CID_C, b"C" * 100)
    assert result.outcome == SubmitOutcome.CACHE_FULL
    assert result.http_status == 503
    assert cache.stats()["rejected_full"] == 1

    # Pending entries are still intact
    assert (await cache.get(CID_A)) is not None
    assert (await cache.get(CID_B)) is not None


@pytest.mark.asyncio
async def test_lru_eviction_frees_room_via_confirmed(db, blob_dir):
    cache = _build_cache(db, blob_dir, max_entries=2)
    await cache.submit(CID_A, b"A" * 50)
    await cache.submit(CID_B, b"B" * 50)

    # Mark A as confirmed (in-kubo) with an older last_accessed timestamp
    cur = db.cursor()
    cur.execute(
        "UPDATE instant_pin_cache SET state='in-kubo', kubo_confirmed_at=?, last_accessed_at=? "
        "WHERE cid=?",
        (int(time.time()), int(time.time()) - 1000, CID_A),
    )
    db.commit()

    # New submit should now succeed - A gets evicted to make room
    result = await cache.submit(CID_C, b"C" * 50)
    assert result.outcome == SubmitOutcome.ACCEPTED

    # A is marked evicted in the DB
    cur.execute("SELECT state FROM instant_pin_cache WHERE cid=?", (CID_A,))
    assert cur.fetchone()["state"] == "evicted"


@pytest.mark.asyncio
async def test_bytes_cap_back_pressures_when_full_pending(db, blob_dir):
    cache = _build_cache(db, blob_dir, max_entries=100, max_bytes=200)
    assert (await cache.submit(CID_A, b"A" * 150)).outcome == SubmitOutcome.ACCEPTED
    # 100 more bytes pushes us past 200 cap, and only entry is pending
    result = await cache.submit(CID_B, b"B" * 100)
    assert result.outcome == SubmitOutcome.CACHE_FULL


@pytest.mark.asyncio
async def test_already_confirmed_submit_is_idempotent(db, blob_dir):
    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"data")
    # Promote
    cur = db.cursor()
    cur.execute(
        "UPDATE instant_pin_cache SET state='in-kubo', kubo_confirmed_at=? WHERE cid=?",
        (int(time.time()), CID_A),
    )
    db.commit()
    # Re-submit returns ALREADY_CONFIRMED (no need to re-cache)
    second = await cache.submit(CID_A, b"data")
    assert second.outcome == SubmitOutcome.ALREADY_CONFIRMED
    assert second.http_status == 200


# ----- Reconciler ---------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciler_promotes_pending_to_in_kubo(db, blob_dir, monkeypatch):
    fake = FakeKuboTransport()
    _patch_httpx_with(monkeypatch, fake)

    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"some payload")
    # Tell our fake transport that this CID will be considered present in
    # Kubo after block/put has been called.
    fake.register_pending_cid(CID_A)

    await cache._reconcile_once()

    cur = db.cursor()
    cur.execute("SELECT state FROM instant_pin_cache WHERE cid=?", (CID_A,))
    assert cur.fetchone()["state"] == "in-kubo"

    # Blob file is removed from disk to free space
    assert not os.path.exists(_blob_path(blob_dir, CID_A))

    stats = cache.stats()
    assert stats["promotions_succeeded"] == 1
    assert stats["pending_entries"] == 0
    assert stats["confirmed_entries"] == 1


@pytest.mark.asyncio
async def test_reconciler_skipped_when_already_in_kubo(db, blob_dir, monkeypatch):
    """If block/stat returns 200 on first try, no block/put is issued."""
    fake = FakeKuboTransport()
    fake.put_blobs[CID_A] = b"existing"
    _patch_httpx_with(monkeypatch, fake)

    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"payload")
    await cache._reconcile_once()

    # No block/put call should have been issued
    put_calls = [c for c in fake.calls if c[0] == "/api/v0/block/put"]
    assert put_calls == []

    cur = db.cursor()
    cur.execute("SELECT state FROM instant_pin_cache WHERE cid=?", (CID_A,))
    assert cur.fetchone()["state"] == "in-kubo"


@pytest.mark.asyncio
async def test_reconciler_records_failure_when_kubo_rejects(db, blob_dir, monkeypatch):
    fake = FakeKuboTransport()
    fake.fail_block_put = True
    _patch_httpx_with(monkeypatch, fake)

    cache = _build_cache(db, blob_dir)
    await cache.submit(CID_A, b"payload")
    await cache._reconcile_once()

    cur = db.cursor()
    cur.execute(
        "SELECT state, last_error, promotion_attempts FROM instant_pin_cache WHERE cid=?",
        (CID_A,),
    )
    row = cur.fetchone()
    assert row["state"] == "pending"
    assert row["last_error"] is not None
    assert row["promotion_attempts"] >= 1
    assert cache.stats()["promotion_failures"] == 1


@pytest.mark.asyncio
async def test_reconciler_handles_no_pending_rows(db, blob_dir, monkeypatch):
    fake = FakeKuboTransport()
    _patch_httpx_with(monkeypatch, fake)
    cache = _build_cache(db, blob_dir)
    await cache._reconcile_once()  # no rows; no calls
    assert fake.calls == []


@pytest.mark.asyncio
async def test_reconciler_emits_alert_on_stuck_row(db, blob_dir, monkeypatch, caplog):
    """A row pending longer than promotion_timeout_s logs a WARN."""
    fake = FakeKuboTransport()
    fake.fail_block_put = True  # ensures it stays pending
    _patch_httpx_with(monkeypatch, fake)

    cache = _build_cache(db, blob_dir, promotion_timeout_s=1)
    await cache.submit(CID_A, b"payload")
    # Backdate submitted_at to make it look ancient
    cur = db.cursor()
    cur.execute(
        "UPDATE instant_pin_cache SET submitted_at = ? WHERE cid = ?",
        (int(time.time()) - 3600, CID_A),
    )
    db.commit()

    import logging
    with caplog.at_level(logging.WARNING, logger="instant_pin_cache"):
        await cache._reconcile_once()

    assert any(
        "has been pending" in record.message for record in caplog.records
    ), "expected a stuck-row warning"
    assert cache.stats()["promotion_timeout_alerts"] >= 1


# ----- Reconciler loop lifecycle ------------------------------------------

@pytest.mark.asyncio
async def test_reconciler_loop_exits_on_shutdown(db, blob_dir):
    cache = _build_cache(db, blob_dir, reconcile_interval_s=10)
    shutdown = asyncio.Event()
    task = asyncio.create_task(cache.run_reconciler(shutdown))
    await asyncio.sleep(0.01)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_disabled_reconciler_is_noop(db, blob_dir):
    cache = _build_cache(db, blob_dir, enabled=False)
    shutdown = asyncio.Event()
    # Should return immediately without ever sleeping
    await asyncio.wait_for(cache.run_reconciler(shutdown), timeout=1)
