"""Append-only JSONL event writer with optional JSON sanitization."""

import json
from pathlib import Path
from typing import Callable, Union

from ..json_safety import sanitize_json


class JsonlEventWriter:
    """
    Append-only JSONL writer.

    *path_factory* is a zero-argument callable returning a Path or str.
    It is called on every :meth:`append`, which allows rotating paths
    (e.g. monthly files) without re-creating the writer.

    Parameters
    ----------
    path_factory:  Callable returning the current write path.
    sanitize:      When True (default), records are passed through
                   :func:`~shane_common.json_safety.sanitize_json` before
                   serialization.
    ensure_ascii:  Forwarded to json.dumps; defaults to False so non-ASCII
                   characters are written as-is.
    """

    def __init__(
        self,
        path_factory: Callable[[], Union[Path, str]],
        *,
        sanitize: bool = True,
        ensure_ascii: bool = False,
    ) -> None:
        self._path_factory = path_factory
        self._sanitize = sanitize
        self._ensure_ascii = ensure_ascii

    def append(self, record: dict) -> None:
        """Serialize *record* and append it as one line to the current path."""
        path = Path(self._path_factory())
        path.parent.mkdir(parents=True, exist_ok=True)
        data = sanitize_json(record) if self._sanitize else record
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=self._ensure_ascii) + "\n")
