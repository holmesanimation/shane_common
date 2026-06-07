from typing import TypedDict


class PingRequest(TypedDict):
    op: str     # "ping"
    nonce: str


class PongPayload(TypedDict):
    op: str     # "pong"
    nonce: str
    ts: float
