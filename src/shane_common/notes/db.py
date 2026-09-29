# shane_common/notes/db.py
"""SQLite connection handling, schema and migrations for the notes database.

* One connection per thread, created lazily (``NotesDb`` never touches the disk
  until it is first used, so constructing one at import time is free).
* ``journal_mode=WAL``, ``synchronous=FULL``, ``foreign_keys=ON``.
* Explicit transactions (``BEGIN IMMEDIATE``) through :meth:`NotesDb.transaction`,
  which nests: an inner ``with`` joins the outer transaction.
* ``meta.schema_version`` plus an ordered list of migrations run at open.
  ``meta.write_seq`` is bumped in every write transaction; the backup uses it to
  skip snapshots when nothing changed.

The live database, its ``-wal`` and its ``-shm`` file must never be copied by
anything: use :func:`sqlite3.Connection.backup` (see the backup snapshot step).

No Qt dependencies.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

#: Newest schema this code understands.  Equals ``len(_MIGRATIONS)``.
SCHEMA_VERSION = 1


class NotesDbError(Exception):
    """The notes database cannot be opened or is not usable."""


_SCHEMA_V1 = """
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE owners (
  key     TEXT PRIMARY KEY,
  label   TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE subjects (
  id        TEXT PRIMARY KEY,
  label     TEXT NOT NULL,
  owner_key TEXT REFERENCES owners(key),
  UNIQUE (owner_key, label COLLATE NOCASE)
);

CREATE TABLE notes (
  note_id    TEXT PRIMARY KEY,
  owner_key  TEXT NOT NULL REFERENCES owners(key),
  created_ts REAL NOT NULL,
  head_rev   INTEGER NOT NULL,
  deleted    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE revisions (
  note_id      TEXT NOT NULL REFERENCES notes(note_id),
  revision_num INTEGER NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('create','edit','delete')),
  ts           REAL NOT NULL,
  wall_ts      REAL NOT NULL,
  note_type    TEXT NOT NULL,
  title        TEXT,
  subject_id   TEXT REFERENCES subjects(id),
  text         TEXT NOT NULL,
  instrument   TEXT,
  strategy_id  TEXT,
  context      TEXT NOT NULL DEFAULT '{}',
  table_id TEXT, table_label TEXT, row_id TEXT, row_ts REAL, row_snapshot TEXT,
  owner_module TEXT, owner_class TEXT,
  PRIMARY KEY (note_id, revision_num)
);

CREATE TABLE correlations (
  correlation_id TEXT PRIMARY KEY,
  time_window    TEXT,
  relation_type  TEXT,
  hypothesis     TEXT,
  tags           TEXT,
  row_ids        TEXT NOT NULL DEFAULT '[]',
  created_ts     REAL NOT NULL
);

CREATE TABLE correlation_notes (
  correlation_id TEXT NOT NULL REFERENCES correlations(correlation_id),
  note_id        TEXT NOT NULL REFERENCES notes(note_id),
  row_id         TEXT,
  PRIMARY KEY (correlation_id, note_id)
);

CREATE INDEX idx_notes_owner_created ON notes(owner_key, created_ts DESC);
CREATE INDEX idx_revisions_subject   ON revisions(subject_id);
CREATE INDEX idx_revisions_row       ON revisions(row_id);

INSERT INTO meta(key, value) VALUES ('write_seq', '0');
"""

#: Ordered migrations; index ``i`` upgrades schema version ``i`` to ``i + 1``.
_MIGRATIONS: list[str] = [_SCHEMA_V1]
assert len(_MIGRATIONS) == SCHEMA_VERSION


def _configure(conn: sqlite3.Connection, *, readonly: bool) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return ``meta.schema_version`` (0 for a database with no schema yet)."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def _migrate(conn: sqlite3.Connection) -> None:
    current = read_schema_version(conn)
    if current > SCHEMA_VERSION:
        raise NotesDbError(
            f"The notes database uses schema version {current}, but this app only "
            f"understands up to version {SCHEMA_VERSION}. Update the app."
        )
    for version in range(current, SCHEMA_VERSION):
        script = _MIGRATIONS[version]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_script(script):
                conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(version + 1),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _split_script(script: str) -> list[str]:
    """Split a migration script into statements (no semicolons inside literals)."""
    return [s.strip() for s in script.split(";") if s.strip()]


class NotesDb:
    """Lazy, thread-aware access to one notes database file.

    Parameters
    ----------
    path:
        Database file.  Its parent directory is created on first use.
    readonly:
        Open an existing database read-only: no directory creation, no
        migrations, no pragmas that write.  Used to inspect restored snapshots.
    on_open:
        Called once with a read/write connection after migrations, inside no
        transaction (used to register the built-in owners).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        on_open: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self._path = Path(str(path))
        self._readonly = readonly
        self._on_open = on_open
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[sqlite3.Connection] = []
        self._opened = False
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def readonly(self) -> bool:
        return self._readonly

    @property
    def is_open(self) -> bool:
        """True once the file has been opened and migrated by some connection."""
        return self._opened

    # -- connections ---------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """Return this thread's connection, opening (and migrating) on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self._closed:
            raise NotesDbError("The notes database has been closed.")
        conn = self._connect()
        self._local.conn = conn
        self._local.depth = 0
        with self._lock:
            self._all.append(conn)
        return conn

    def _connect(self) -> sqlite3.Connection:
        try:
            if self._readonly:
                if not self._path.exists():
                    raise NotesDbError(f"No notes database at {self._path}")
                uri = self._path.resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
            else:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self._path), isolation_level=None, check_same_thread=False, timeout=5.0
                )
        except sqlite3.Error as exc:
            raise NotesDbError(f"Cannot open the notes database {self._path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            _configure(conn, readonly=self._readonly)
            with self._lock:
                first = not self._opened
                if first:
                    if self._readonly:
                        if read_schema_version(conn) > SCHEMA_VERSION:
                            raise NotesDbError(
                                "The notes database was written by a newer version of the app."
                            )
                    else:
                        _migrate(conn)
                        if self._on_open is not None:
                            self._on_open(conn)
                    self._opened = True
        except sqlite3.Error as exc:
            conn.close()
            raise NotesDbError(f"Cannot open the notes database {self._path}: {exc}") from exc
        except BaseException:
            conn.close()
            raise
        return conn

    def close(self) -> None:
        """Close every connection this object opened (any thread)."""
        with self._lock:
            self._closed = True
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    # -- transactions ---------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction; commit on success, roll back on any exception.

        Nested use joins the outermost transaction.  ``meta.write_seq`` is bumped
        once, when the outermost transaction commits.
        """
        conn = self.connection()
        if self._readonly:
            raise NotesDbError("The notes database is open read-only.")
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth -= 1
            return

        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
            conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'write_seq'"
            )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            self._local.depth = 0

    def write_seq(self) -> int:
        """Return ``meta.write_seq`` (number of committed write transactions)."""
        return read_write_seq(self.connection())


def read_write_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'write_seq'").fetchone()
    return int(row[0]) if row else 0
