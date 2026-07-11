"""load_json_file, save_json_file_atomic – thin helpers over atomic writes."""

import json
import traceback
from pathlib import Path

from .atomic import write_json_atomic


def load_json_file(path, default=None):
    """
    Load and parse a JSON file.

    Returns *default* if the file does not exist or cannot be parsed.
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        traceback.print_exc()
        return default


def save_json_file_atomic(path, data, **kwargs) -> None:
    """Save *data* as JSON to *path* atomically. Keyword args forwarded to write_json_atomic."""
    write_json_atomic(path, data, **kwargs)
