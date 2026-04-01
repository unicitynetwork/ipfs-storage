#!/usr/bin/env python3
"""
IPFS Garbage Collection: Remove old inventory versions.

For each IPNS name in the sidecar database, keeps the N most recent
inventory versions (following the _meta.lastCid chain) and unpins
everything else.

Uses the IPFS HTTP API directly (no subprocess overhead).

Usage (run inside the ipfs-kubo container):
    # Dry run (default) — show what would be unpinned
    python3 /usr/local/bin/gc-old-versions.py

    # Actually unpin
    python3 /usr/local/bin/gc-old-versions.py --execute

    # Keep 10 versions instead of 5
    python3 /usr/local/bin/gc-old-versions.py --keep 10

    # Process only first 100 IPNS names (for testing)
    python3 /usr/local/bin/gc-old-versions.py --limit 100

Or from the host:
    docker exec ipfs-kubo python3 /usr/local/bin/gc-old-versions.py --execute
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

IPFS_API = "http://127.0.0.1:5001"
DB_PATH = "/data/ipfs/propagation.db"


def ipfs_api(endpoint, timeout=10):
    """Call IPFS HTTP API, return response body as bytes."""
    url = f"{IPFS_API}{endpoint}"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def ipfs_cat(cid, timeout=10):
    """Fetch content of a CID via IPFS API."""
    data = ipfs_api(f"/api/v0/cat?arg={cid}", timeout=timeout)
    if data:
        return data.decode("utf-8", errors="replace")
    return None


def ipfs_pin_rm(cid):
    """Unpin a CID via IPFS API."""
    return ipfs_api(f"/api/v0/pin/rm?arg={cid}", timeout=30) is not None


def ipfs_pin_ls_streaming():
    """Stream all recursive pins from the IPFS API, yielding CIDs one at a time.

    Uses the streaming JSON API to avoid buffering all output at once.
    """
    url = f"{IPFS_API}/api/v0/pin/ls?type=recursive&stream=true"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            buf = b""
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        cid = obj.get("Cid", "")
                        if cid:
                            yield cid
                    except json.JSONDecodeError:
                        pass
            # Handle remaining data
            if buf.strip():
                try:
                    obj = json.loads(buf)
                    cid = obj.get("Cid", "")
                    if cid:
                        yield cid
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        print(f"[GC] ERROR streaming pins: {e}", file=sys.stderr)


def ipfs_pin_ls_recursive():
    """Get all recursively pinned CIDs as a set using streaming API."""
    print("[GC] Loading all recursive pins (streaming)...", flush=True)
    pins = set()
    count = 0
    start = time.time()
    for cid in ipfs_pin_ls_streaming():
        pins.add(cid)
        count += 1
        if count % 100000 == 0:
            elapsed = time.time() - start
            print(f"[GC]   ... loaded {count:,} pins ({elapsed:.0f}s)", flush=True)
    elapsed = time.time() - start
    print(f"[GC]   Loaded {count:,} pins in {elapsed:.0f}s", flush=True)
    return pins


def walk_chain(start_cid, keep_count):
    """Walk the _meta.lastCid chain from start_cid, return list of CIDs to keep."""
    keep = []
    current = start_cid
    for i in range(keep_count):
        if not current:
            break
        keep.append(current)
        content = ipfs_cat(current)
        if not content:
            break
        try:
            data = json.loads(content)
            meta = data.get("_meta", {})
            current = meta.get("lastCid")
        except (json.JSONDecodeError, KeyError):
            break
    return keep


def get_ipns_current_cids(db_path):
    """Get all current IPNS name -> CID mappings from the sidecar DB."""
    db = sqlite3.connect(db_path)
    rows = db.execute(
        "SELECT ipns_name, cid, sequence FROM ipns_records WHERE cid IS NOT NULL"
    ).fetchall()
    db.close()
    return [(name, cid, seq) for name, cid, seq in rows]


def main():
    parser = argparse.ArgumentParser(description="IPFS GC: remove old inventory versions")
    parser.add_argument("--keep", type=int, default=5, help="Number of recent versions to keep (default: 5)")
    parser.add_argument("--execute", action="store_true", help="Actually unpin (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N IPNS names (0=all)")
    parser.add_argument("--skip-chain-walk", action="store_true",
                        help="Skip chain walking — only protect current CID (faster but less safe)")
    parser.add_argument("--skip-gc", action="store_true", help="Skip ipfs repo gc after unpinning")
    parser.add_argument("--db-path", default=DB_PATH, help="Path to propagation.db")
    args = parser.parse_args()

    print(f"[GC] IPFS Inventory Garbage Collection", flush=True)
    print(f"[GC] Keep: {args.keep} most recent versions per IPNS name", flush=True)
    print(f"[GC] Mode: {'EXECUTE' if args.execute else 'DRY RUN'}", flush=True)
    print(flush=True)

    # Step 1: Load all recursive pins
    all_pins = ipfs_pin_ls_recursive()
    print(f"[GC] Total recursive pins: {len(all_pins):,}", flush=True)
    if not all_pins:
        print("[GC] ERROR: No pins found. Is the IPFS daemon running?")
        sys.exit(1)

    # Step 2: Get current IPNS -> CID mappings
    ipns_records = get_ipns_current_cids(args.db_path)
    print(f"[GC] IPNS names with CIDs: {len(ipns_records):,}", flush=True)

    if args.limit > 0:
        ipns_records = ipns_records[:args.limit]
        print(f"[GC] Limited to first {args.limit} names", flush=True)

    # Step 3: Build set of CIDs to KEEP (parallel chain walks)
    keep_cids = set()
    errors = 0
    start_time = time.time()
    total = len(ipns_records)
    completed = 0

    def process_one(record):
        name, current_cid, seq = record
        if args.skip_chain_walk:
            return [current_cid], False
        chain = walk_chain(current_cid, args.keep)
        if chain:
            return chain, False
        return [current_cid], True  # error but still keep current

    workers = 1 if args.skip_chain_walk else 20
    print(f"[GC] Chain walk with {workers} workers...", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_one, rec): rec for rec in ipns_records}
        for future in as_completed(futures):
            completed += 1
            chain, had_error = future.result()
            keep_cids.update(chain)
            if had_error:
                errors += 1
            if completed % 500 == 0:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                remaining = total - completed
                eta = remaining / rate if rate > 0 else 0
                print(f"[GC] Chain walk: {completed:,}/{total:,} "
                      f"({rate:.0f}/s, keep={len(keep_cids):,}, "
                      f"errors={errors}, ETA={eta/60:.0f}m)", flush=True)

    elapsed = time.time() - start_time
    print(flush=True)
    print(f"[GC] Chain walk complete in {elapsed:.0f}s", flush=True)
    print(f"[GC] CIDs to keep: {len(keep_cids):,}", flush=True)
    print(f"[GC] Chain walk errors: {errors:,}", flush=True)

    # Step 4: Calculate CIDs to unpin
    to_unpin = all_pins - keep_cids
    try:
        db = sqlite3.connect(args.db_path)
        nostr_cids = set(
            row[0] for row in db.execute("SELECT cid FROM pinned_cids").fetchall()
        )
        db.close()
        to_unpin -= nostr_cids
        print(f"[GC] Nostr-pinned CIDs protected: {len(nostr_cids):,}", flush=True)
    except Exception:
        pass

    print(f"[GC] CIDs to unpin: {len(to_unpin):,}", flush=True)
    print(f"[GC] CIDs retained: {len(all_pins) - len(to_unpin):,}", flush=True)
    print(flush=True)

    if not to_unpin:
        print("[GC] Nothing to unpin!")
        return

    # Step 5: Unpin
    if not args.execute:
        print("[GC] DRY RUN — no changes made. Run with --execute to unpin.", flush=True)
        print(f"[GC] Would unpin {len(to_unpin):,} objects", flush=True)
        return

    print(f"[GC] Unpinning {len(to_unpin):,} CIDs...", flush=True)
    unpinned = 0
    failed = 0
    start_time = time.time()

    for cid in to_unpin:
        if ipfs_pin_rm(cid):
            unpinned += 1
        else:
            failed += 1

        total = unpinned + failed
        if total % 1000 == 0:
            elapsed = time.time() - start_time
            rate = total / elapsed if elapsed > 0 else 0
            remaining = len(to_unpin) - total
            eta = remaining / rate if rate > 0 else 0
            print(f"[GC] Unpin: {total:,}/{len(to_unpin):,} "
                  f"({rate:.0f}/s, ok={unpinned:,}, fail={failed:,}, "
                  f"ETA={eta/60:.0f}m)", flush=True)

    elapsed = time.time() - start_time
    print(flush=True)
    print(f"[GC] Unpinning complete in {elapsed:.0f}s", flush=True)
    print(f"[GC] Unpinned: {unpinned:,}", flush=True)
    print(f"[GC] Failed: {failed:,}", flush=True)

    # Step 6: Run GC
    if args.skip_gc:
        print("[GC] Skipping ipfs repo gc (--skip-gc)", flush=True)
        print("[GC] Run manually: docker exec ipfs-kubo sh -c 'IPFS_PATH=/data/ipfs ipfs repo gc'")
        return

    print(flush=True)
    print("[GC] Running ipfs repo gc to reclaim disk space...", flush=True)
    print("[GC] This may take a very long time for large repos...", flush=True)
    try:
        url = f"{IPFS_API}/api/v0/repo/gc"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=7200) as resp:
            count = 0
            buf = b""
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    count += 1
                    if count % 100000 == 0:
                        print(f"[GC]   GC removed {count:,} objects so far...", flush=True)
            print(f"[GC] GC removed {count:,} objects", flush=True)
    except Exception as e:
        print(f"[GC] GC error: {e}", flush=True)
        print("[GC] Run manually: docker exec ipfs-kubo sh -c 'IPFS_PATH=/data/ipfs ipfs repo gc'")

    print(flush=True)
    print("[GC] Done!", flush=True)


if __name__ == "__main__":
    main()
