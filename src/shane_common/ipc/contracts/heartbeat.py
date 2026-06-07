from typing import Literal, TypedDict


class HeartbeatMsg(TypedDict):
    op: Literal["heartbeat"]
    ts: float
