"""UTC timestamp helpers, day buckets, and epoch normalization."""

import datetime

# Timestamps larger than this value are assumed to be milliseconds, not seconds.
_MS_THRESHOLD = 1e10


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with timezone."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def local_now_iso() -> str:
    """Return the current local wall-clock time as ISO 8601 with UTC offset."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


def local_iso_from_epoch(ts: float) -> str:
    """Convert epoch seconds to local ISO 8601 with UTC offset."""
    dt = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).astimezone()
    return dt.isoformat()


def utc_iso_from_epoch(ts: float) -> str:
    """Convert epoch seconds to UTC ISO 8601."""
    dt = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
    return dt.isoformat()


def normalize_epoch_seconds(value) -> float:
    """
    Normalize an epoch timestamp to float seconds.

    If the value is larger than 1e10 it is assumed to be milliseconds and
    is divided by 1000.
    """
    v = float(value)
    if v > _MS_THRESHOLD:
        return v / 1000.0
    return v


def day_bucket_from_ts(ts) -> str:
    """
    Return 'YYYY-MM-DD' for a UTC timestamp.

    Accepts:
    - int or float seconds since epoch (milliseconds auto-detected)
    - ISO 8601 string with or without timezone (naive treated as UTC)
    - datetime.datetime (naive treated as UTC)
    - datetime.date
    """
    if isinstance(ts, datetime.datetime):
        dt = ts if ts.tzinfo is not None else ts.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).date().isoformat()
    if isinstance(ts, datetime.date):
        return ts.isoformat()
    if isinstance(ts, str):
        try:
            dt = datetime.datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc).date().isoformat()
        except ValueError:
            pass
    epoch_s = normalize_epoch_seconds(ts)
    dt = datetime.datetime.fromtimestamp(epoch_s, tz=datetime.timezone.utc)
    return dt.date().isoformat()
