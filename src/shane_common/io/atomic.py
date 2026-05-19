"""Atomic JSON and text file writes with Windows os.replace retry."""

import json
import os
import time
from pathlib import Path


def write_json_atomic(
    path,
    data,
    *,
    retries: int = 5,
    delay: float = 0.05,
    sort_keys: bool = True,
) -> None:
    """
    Write *data* as JSON to *path* atomically using a temp file + os.replace.

    On Windows, os.replace can fail transiently with PermissionError when
    antivirus or another process holds a file lock. The call is retried up to
    *retries* times, sleeping *delay* seconds between attempts.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys)
    tmp.write_text(text, encoding="utf-8")
    _replace_with_retry(tmp, path, retries=retries, delay=delay)


def write_text_atomic(
    path,
    text: str,
    *,
    retries: int = 5,
    delay: float = 0.05,
    encoding: str = "utf-8",
) -> None:
    """Write *text* to *path* atomically using a temp file + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding=encoding)
    _replace_with_retry(tmp, path, retries=retries, delay=delay)


def _replace_with_retry(src: Path, dst: Path, *, retries: int, delay: float) -> None:
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
