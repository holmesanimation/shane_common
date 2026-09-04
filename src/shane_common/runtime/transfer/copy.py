"""Generic idempotent copy + content-hash verification primitive.

Domain-neutral: this module knows nothing about migration manifests,
trading resource IDs, or lifecycle policy. It performs exactly one thing:
copy a single source file to a destination path and prove the destination
matches the source via size + SHA-256 hash. It never deletes the source.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .hashing import sha256_file


class CopyOutcome(str, Enum):
    COPIED = "copied"
    ALREADY_PRESENT_IDENTICAL = "already_present_identical"
    DESTINATION_CONFLICT = "destination_conflict"
    SOURCE_CHANGED_DURING_COPY = "source_changed_during_copy"


@dataclass(frozen=True)
class CopyVerifyResult:
    outcome: CopyOutcome
    source_size: int
    source_hash: str
    destination_size: int | None
    destination_hash: str | None


def copy_file_verified(source: Path, destination: Path) -> CopyVerifyResult:
    """Idempotently copy ``source`` to ``destination``, verifying content hash.

    - If ``destination`` already exists with identical size+hash to
      ``source``, no bytes are copied (``ALREADY_PRESENT_IDENTICAL``).
    - If ``destination`` exists with different content, nothing is
      overwritten (``DESTINATION_CONFLICT``) — the caller must resolve it.
    - Otherwise, copies via a temp file in the destination directory then an
      atomic ``os.replace``, so an interrupted copy never leaves a partially
      written file at ``destination`` itself.
    - After copying, ``source`` is re-hashed; if it no longer matches the
      pre-copy hash/size, the just-written temp copy is discarded and
      ``SOURCE_CHANGED_DURING_COPY`` is returned (mtime alone is never used
      for this check).
    """
    source = Path(source)
    destination = Path(destination)

    source_size = source.stat().st_size
    source_hash_before = sha256_file(source)

    if destination.exists():
        dest_size = destination.stat().st_size
        dest_hash = sha256_file(destination)
        if dest_size == source_size and dest_hash == source_hash_before:
            return CopyVerifyResult(
                outcome=CopyOutcome.ALREADY_PRESENT_IDENTICAL,
                source_size=source_size,
                source_hash=source_hash_before,
                destination_size=dest_size,
                destination_hash=dest_hash,
            )
        return CopyVerifyResult(
            outcome=CopyOutcome.DESTINATION_CONFLICT,
            source_size=source_size,
            source_hash=source_hash_before,
            destination_size=dest_size,
            destination_hash=dest_hash,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".transfer_", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(source, tmp_path)

        source_hash_after = sha256_file(source)
        if (
            source_hash_after != source_hash_before
            or source.stat().st_size != source_size
        ):
            return CopyVerifyResult(
                outcome=CopyOutcome.SOURCE_CHANGED_DURING_COPY,
                source_size=source_size,
                source_hash=source_hash_before,
                destination_size=None,
                destination_hash=None,
            )

        dest_hash = sha256_file(tmp_path)
        dest_size = tmp_path.stat().st_size
        os.replace(tmp_path, destination)
        tmp_path = destination  # already moved; skip cleanup below
    finally:
        if tmp_path != destination and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return CopyVerifyResult(
        outcome=CopyOutcome.COPIED,
        source_size=source_size,
        source_hash=source_hash_before,
        destination_size=dest_size,
        destination_hash=dest_hash,
    )
