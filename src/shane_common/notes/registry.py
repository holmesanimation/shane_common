# shane_common/notes/registry.py
"""Owner and subject registry for notes.

Owners
    An *owner* is a bucket of notes.  The user-facing **label** may contain
    spaces (``"Sleep Log"``); the **key** is a stable slug (``"sleep_log"``)
    derived from it once at creation and never changed.  Hardcoded (built-in)
    owners are supplied by the application and registered in the database when
    it is first opened.

Subjects
    A *subject* groups notes within an owner (or across all owners).  Each has
    an opaque id (``subj_1a2b3c4d``) which is what notes store, a display
    label, and an owner scope: an owner key, or ``None`` for "All" owners.
    Notes without a subject carry ``UNSET_SUBJECT`` (``"UNSET"``).

Owners and subjects live in the ``owners`` / ``subjects`` tables of the notes
database (see ``NotesStore``); this class is a validating facade over them.
Every call reads the database, so two windows stay consistent without cache
invalidation.

No Qt dependencies.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from shane_common.notes.model import UNSET_SUBJECT
from shane_common.notes.store import NotesStore, NotesStoreError

#: Subject-list entries with special meaning; user subjects may not use them.
ALL_SUBJECTS_LABEL = "ALL"
_RESERVED_SUBJECT_LABELS = {ALL_SUBJECTS_LABEL.casefold(), UNSET_SUBJECT.casefold()}

MAX_OWNER_LABEL_LEN = 40
MAX_SUBJECT_LABEL_LEN = 60

_OWNER_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
_SLUG_SEP_RE = re.compile(r"[^a-z0-9]+")
# Owner keys are no longer file names, but the strict slug rules are kept so keys
# stay stable and predictable.  These names are reserved because Windows refuses
# them as file stems (owners may be exported to files later).
_RESERVED_OWNER_KEYS = {"con", "prn", "aux", "nul"} | {
    f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)
}


class RegistryError(ValueError):
    """Raised for an invalid or conflicting owner/subject name.

    The message is written for end users and is safe to show in a dialog.
    """


@dataclass(frozen=True)
class OwnerEntry:
    key: str          # stable slug; the value notes store as their owner
    label: str        # display name (may contain spaces)
    builtin: bool = False


@dataclass(frozen=True)
class SubjectEntry:
    id: str           # opaque; the value notes store as their subject
    label: str
    owner: str | None  # owner key, or ``None`` = available to all owners

    @property
    def is_global(self) -> bool:
        return self.owner is None


# ---------------------------------------------------------------------------
# Validation / normalisation (pure functions — unit-testable, also used live by
# the "new owner" / "new subject" prompts to give feedback while typing)
# ---------------------------------------------------------------------------

def _normalize_label(raw: str) -> str:
    """NFC-normalise, drop control characters and collapse whitespace."""
    text = unicodedata.normalize("NFC", str(raw or ""))
    if any(unicodedata.category(ch).startswith("C") and not ch.isspace() for ch in text):
        raise RegistryError("Names cannot contain control characters.")
    return " ".join(text.split())


def normalize_owner_label(raw: str) -> str:
    """Return the cleaned owner label, or raise ``RegistryError``."""
    label = _normalize_label(raw)
    if not label:
        raise RegistryError("Enter a name.")
    if len(label) > MAX_OWNER_LABEL_LEN:
        raise RegistryError(f"Keep the name to {MAX_OWNER_LABEL_LEN} characters or fewer.")
    if not _OWNER_LABEL_RE.match(label):
        raise RegistryError(
            "Use only letters, numbers, spaces, hyphens and underscores, "
            "starting with a letter or number."
        )
    return label


def owner_key_for_label(label: str) -> str:
    """Derive the stable owner key (a lowercase slug) from an already-normalised label."""
    key = _SLUG_SEP_RE.sub("_", label.casefold()).strip("_")
    if not key or key in _RESERVED_OWNER_KEYS:
        raise RegistryError(f"“{label}” is a reserved name; choose another.")
    return key


def normalize_subject_label(raw: str) -> str:
    """Return the cleaned subject label, or raise ``RegistryError``."""
    label = _normalize_label(raw)
    if not label:
        raise RegistryError("Enter a name.")
    if len(label) > MAX_SUBJECT_LABEL_LEN:
        raise RegistryError(f"Keep the name to {MAX_SUBJECT_LABEL_LEN} characters or fewer.")
    if label.casefold() in _RESERVED_SUBJECT_LABELS:
        raise RegistryError(f"“{label}” is reserved; choose another name.")
    return label


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class NotesRegistry:
    """Owners and subjects for the notes system.

    Parameters
    ----------
    store:
        The notes database holding the ``owners`` and ``subjects`` tables.
    builtin_owners:
        ``(key, label)`` pairs for hardcoded owners.  They are registered in the
        database (flagged ``builtin``) when it is first opened and always list
        first, in the declared order.
    """

    def __init__(
        self,
        store: NotesStore,
        builtin_owners: Iterable[tuple[str, str]] = (),
    ) -> None:
        self._store = store
        builtin = tuple((str(k), str(lbl)) for k, lbl in builtin_owners)
        if builtin:
            store.declare_builtin_owners(builtin)

    @property
    def store(self) -> NotesStore:
        return self._store

    # -- reading -----------------------------------------------------------

    def owners(self) -> list[OwnerEntry]:
        """Built-in owners (in declared order) followed by the others, A–Z."""
        return [
            OwnerEntry(key=o.key, label=o.label or o.key, builtin=o.builtin)
            for o in self._store.owners()
        ]

    def owner_label(self, key: str) -> str:
        for o in self.owners():
            if o.key == key:
                return o.label
        return key

    def subjects(self) -> list[SubjectEntry]:
        return [
            SubjectEntry(id=s.id, label=s.label, owner=s.owner_key)
            for s in self._store.subjects()
        ]

    def subjects_for_owner(self, owner_key: str) -> list[SubjectEntry]:
        """Subjects that owner *owner_key* can use: its own plus all-owner subjects."""
        subs = [s for s in self.subjects() if s.owner is None or s.owner == owner_key]
        return sorted(subs, key=lambda s: s.label.casefold())

    def subject(self, subject_id: str) -> SubjectEntry | None:
        for s in self.subjects():
            if s.id == subject_id:
                return s
        return None

    def subject_label(self, subject_id: str | None) -> str:
        """Display label for *subject_id*; ``UNSET`` for none, the raw id if unknown."""
        if not subject_id or subject_id == UNSET_SUBJECT:
            return UNSET_SUBJECT
        entry = self.subject(subject_id)
        return entry.label if entry is not None else subject_id

    def subject_available_for(self, subject_id: str | None, owner_key: str) -> bool:
        """True if *subject_id* may be used on a note owned by *owner_key*."""
        if not subject_id or subject_id == UNSET_SUBJECT:
            return True
        entry = self.subject(subject_id)
        return entry is not None and (entry.owner is None or entry.owner == owner_key)

    # -- writing -----------------------------------------------------------

    def add_owner(self, label: str, *, reserved_keys: Iterable[str] = ()) -> OwnerEntry:
        """Create an owner from a user-typed *label*.

        *reserved_keys* are extra keys the caller knows are taken.
        """
        label = normalize_owner_label(label)
        key = owner_key_for_label(label)
        with self._store.transaction():
            taken = {o.key.casefold(): o for o in self.owners()}
            for k in reserved_keys:
                taken.setdefault(str(k).casefold(), OwnerEntry(key=str(k), label=str(k)))
            clash = taken.get(key.casefold())
            if clash is not None:
                raise RegistryError(f"An owner named “{clash.label}” already exists.")
            try:
                self._store.add_owner(key, label)
            except NotesStoreError as exc:
                raise RegistryError(str(exc)) from exc
        return OwnerEntry(key=key, label=label)

    def add_subject(self, label: str, owner: str | None) -> SubjectEntry:
        """Create a subject scoped to *owner* (an owner key) or to all owners (``None``)."""
        label = normalize_subject_label(label)
        owner = str(owner) if owner else None
        with self._store.transaction():
            existing = self.subjects()
            for s in existing:
                if s.label.casefold() != label.casefold():
                    continue
                # Same label clashes if the two would ever be visible to the same owner.
                if owner is None or s.owner is None or s.owner == owner:
                    where = "all owners" if s.owner is None else self.owner_label(s.owner)
                    raise RegistryError(
                        f"A subject named “{s.label}” already exists for {where}."
                    )
            used = {s.id for s in existing}
            sid = f"subj_{secrets.token_hex(4)}"
            while sid in used:
                sid = f"subj_{secrets.token_hex(4)}"
            try:
                self._store.add_subject(sid, label, owner)
            except NotesStoreError as exc:
                raise RegistryError(str(exc)) from exc
        return SubjectEntry(id=sid, label=label, owner=owner)


# ---------------------------------------------------------------------------
# Application-wide default
#
# ``NoteDialog`` is constructed from many call sites; rather than thread a
# registry through each, the application installs one at startup and widgets
# fall back to it when none is passed explicitly.
# ---------------------------------------------------------------------------

_default_registry: NotesRegistry | None = None


def set_default_registry(registry: NotesRegistry | None) -> None:
    global _default_registry
    _default_registry = registry


def get_default_registry() -> NotesRegistry | None:
    return _default_registry
