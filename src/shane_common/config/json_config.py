"""Small JSON config repository with normalize/default callbacks."""

from pathlib import Path
from typing import Any, Callable, Optional

from ..io.json_files import load_json_file, save_json_file_atomic


class JsonConfigStore:
    """
    Small JSON config repository pattern with normalize/default callbacks.

    Parameters
    ----------
    path:            File path for the persisted JSON config.
    default_factory: Zero-argument callable that returns the default config
                     when the file is missing or unreadable.
    normalize:       Optional callable(raw_value) → config; applied to the
                     parsed JSON on load and to the value passed to save.
                     Defaults to identity if not provided.
    to_json_ready:   Optional callable(config) → json-safe dict/list; called
                     on the normalized value just before writing to disk.
                     Defaults to identity if not provided.
    """

    def __init__(
        self,
        path,
        default_factory: Callable[[], Any],
        normalize: Optional[Callable[[Any], Any]] = None,
        to_json_ready: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._path = Path(path)
        self._default_factory = default_factory
        self._normalize = normalize if normalize is not None else (lambda x: x)
        self._to_json_ready = to_json_ready if to_json_ready is not None else (lambda x: x)

    def load(self) -> Any:
        """Load config from disk, applying *normalize* on the way in."""
        raw = load_json_file(self._path)
        if raw is None:
            return self._default_factory()
        return self._normalize(raw)

    def save(self, config: Any) -> Any:
        """
        Normalize *config*, serialize via *to_json_ready*, and atomically
        write to disk.  Returns the normalized (not raw JSON-ready) config.
        """
        cleaned = self._normalize(config)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        save_json_file_atomic(self._path, self._to_json_ready(cleaned))
        return cleaned
