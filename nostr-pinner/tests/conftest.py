"""Shared pytest fixtures for the nostr-pinner test suite."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Allow `import instant_pin_cache` to resolve to nostr-pinner/instant_pin_cache.py
SIDECAR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SIDECAR_ROOT))


@pytest.fixture
def db(tmp_path):
    """Fresh sqlite DB per test (in-process; no /data/ipfs persistence)."""
    conn = sqlite3.connect(str(tmp_path / "test.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def blob_dir(tmp_path):
    p = tmp_path / "blobs"
    p.mkdir()
    return str(p)
