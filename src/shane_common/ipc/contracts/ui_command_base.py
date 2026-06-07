from typing import Any, Dict, Literal, TypedDict


class UiCommandBase(TypedDict):
    op: Literal["command"]
    command_id: str     # uuid
    command: str
    params: Dict[str, Any]
