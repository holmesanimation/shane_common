"""JSON-safe coercion for datetimes, enums, bytes, sets/tuples, NaN/Inf, dataclasses."""

import dataclasses
import datetime
import math
from enum import Enum


def sanitize_json(value):
    """
    Recursively coerce *value* into JSON-safe primitives.

    Handles:
    - None, bool, int, str  → pass through unchanged
    - float NaN / Inf       → None
    - datetime.datetime     → ISO 8601 string (naive treated as UTC)
    - datetime.date         → ISO 8601 date string
    - datetime.time         → ISO 8601 time string
    - Enum                  → .value (recursed)
    - bytes                 → latin-1 decoded string (lossless round-trip)
    - dataclass instance    → dict (via dataclasses.asdict, recursed)
    - dict                  → dict with str keys (recursed values)
    - list / tuple / set / frozenset → list (recursed)
    - anything else         → str()
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat()
    if isinstance(value, Enum):
        return sanitize_json(value.value)
    if isinstance(value, bytes):
        return value.decode("latin-1")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return sanitize_json(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_json(item) for item in value]
    return str(value)
