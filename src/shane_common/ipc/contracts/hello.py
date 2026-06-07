from typing import TypedDict


class HelloRequest(TypedDict):
    op: str             # "hello"
    client_name: str
    client_version: str
    protocol_version: int   # always 1 for this plan


class HelloAckPayload(TypedDict):
    op: str             # "hello.ack"
    session_id: str
    server_name: str
    platform_version: str
    protocol_version: int
    ts: float
