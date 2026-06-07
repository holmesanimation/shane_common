from typing import Any, Dict, Literal, TypedDict


class TrafficRecord(TypedDict):
    direction: Literal["incoming", "outgoing"]
    ts: float
    topic: str
    kind: str
    seq: int
    payload_preview: Dict[str, Any]
