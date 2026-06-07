from typing import Any, TypedDict


class ApiEnvelope(TypedDict):
    kind: str
    ts: float
    seq: int
    lane: str           # "lossless" | "lossy"
    schema_version: int
    payload: Any
