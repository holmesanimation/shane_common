"""Path helpers for shared app preferences."""

from __future__ import annotations

import os
from pathlib import Path


def app_settings_path(app_id: str, filename: str = "settings.yaml") -> Path:
    if not app_id:
        raise ValueError("app_id must not be empty")
    if not filename:
        raise ValueError("filename must not be empty")

    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        raise RuntimeError(
            "LOCALAPPDATA environment variable is not set. "
            "App settings paths require a Windows environment."
        )

    return Path(localappdata) / app_id / filename