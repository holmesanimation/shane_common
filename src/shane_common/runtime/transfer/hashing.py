"""Generic content hashing helpers for verified file transfer."""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Return the hex-encoded SHA-256 digest of a file's contents."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
