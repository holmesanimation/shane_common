"""Owner / subject registry: validation, safe keys, scoping, persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from shane_common.notes.notes_writer import NotesWriter
from shane_common.notes.registry import (
    NotesRegistry,
    RegistryError,
    normalize_owner_label,
    owner_key_for_label,
)
from shane_common.notes.store import NotesStore

BUILTINS = (("journal", "journal"), ("Prayer", "Prayer"))


def _reg(tmp_path: Path) -> NotesRegistry:
    return NotesRegistry(NotesStore(tmp_path / "notes.db"), builtin_owners=BUILTINS)


# -- owners -----------------------------------------------------------------

def test_builtin_owners_always_present_and_first(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.add_owner("Zebra")
    reg.add_owner("Alpha")
    assert [o.key for o in reg.owners()] == ["journal", "Prayer", "alpha", "zebra"]
    assert all(o.builtin for o in reg.owners()[:2])


def test_owner_with_spaces_gets_stable_key_and_keeps_label(tmp_path: Path) -> None:
    entry = _reg(tmp_path).add_owner("  Sleep   Log ")
    assert (entry.label, entry.key) == ("Sleep Log", "sleep_log")
    assert _reg(tmp_path).owner_label("sleep_log") == "Sleep Log"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "a/b", "a\\b", "..", "x.y", "con", "NUL", "com1",
     "name:with:colons", "-leading", "日本語", "a" * 41, "a\x00b", "___"],
)
def test_bad_owner_names_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(RegistryError):
        _reg(tmp_path).add_owner(bad)


def test_owner_key_is_always_a_lowercase_slug(tmp_path: Path) -> None:
    for label in ("A-b_c 1", "MiXeD Case", "x  y"):
        key = owner_key_for_label(normalize_owner_label(label))
        assert key and all(c.isalnum() or c == "_" for c in key) and key == key.lower()


def test_duplicate_owner_rejected_case_insensitively_incl_builtins(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    for dup in ("journal", "JOURNAL", "prayer", "Prayer"):
        with pytest.raises(RegistryError):
            reg.add_owner(dup)
    reg.add_owner("Sleep Log")
    with pytest.raises(RegistryError):
        reg.add_owner("sleep-log")  # same key


def test_owner_clash_with_reserved_key(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        _reg(tmp_path).add_owner("Legacy", reserved_keys=["legacy"])


def test_owner_clash_with_owner_created_by_a_write(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    w = NotesWriter(reg.store, "adhoc")
    w.commit(w.build_note(text="x"))  # registers "adhoc" on the fly
    with pytest.raises(RegistryError):
        reg.add_owner("Adhoc")


# -- subjects ---------------------------------------------------------------

def test_subject_scoping_owner_vs_all(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    mine = reg.add_subject("Anxiety", "journal")
    shared = reg.add_subject("Gratitude", None)
    other = reg.add_subject("Intercession", "Prayer")

    assert [s.id for s in reg.subjects_for_owner("journal")] == [mine.id, shared.id]
    assert [s.id for s in reg.subjects_for_owner("Prayer")] == [shared.id, other.id]
    assert mine.id.startswith("subj_") and shared.is_global
    assert reg.subject_available_for(mine.id, "journal")
    assert not reg.subject_available_for(mine.id, "Prayer")
    assert reg.subject_available_for(shared.id, "Prayer")
    assert reg.subject_available_for("UNSET", "Prayer")


def test_subject_label_lookup(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    s = reg.add_subject("Work & Life: 100%", "journal")  # any printable text is fine
    assert reg.subject_label(s.id) == "Work & Life: 100%"
    assert reg.subject_label(None) == "UNSET"
    assert reg.subject_label("subj_missing") == "subj_missing"


@pytest.mark.parametrize("bad", ["", "  ", "ALL", "all", "Unset", "UNSET", "a" * 61, "bad\x07"])
def test_bad_subject_names_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(RegistryError):
        _reg(tmp_path).add_subject(bad, "journal")


def test_subject_duplicates_only_within_visible_scope(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.add_subject("Ideas", "journal")
    reg.add_subject("Ideas", "Prayer")  # different owner: fine
    with pytest.raises(RegistryError):
        reg.add_subject("ideas", "journal")
    with pytest.raises(RegistryError):
        reg.add_subject("Ideas", None)  # an all-owner one would clash with both
    reg.add_subject("Shared", None)
    with pytest.raises(RegistryError):
        reg.add_subject("shared", "journal")  # owner subject would shadow the shared one


def test_all_owner_subject_duplicates_are_rejected_in_application_code(tmp_path: Path) -> None:
    """SQLite's UNIQUE treats NULL owners as distinct, so this rule lives in the registry."""
    reg = _reg(tmp_path)
    reg.add_subject("Everywhere", None)
    with pytest.raises(RegistryError):
        reg.add_subject("EVERYWHERE", None)
    assert len(reg.subjects()) == 1


def test_subject_for_unknown_owner_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        _reg(tmp_path).add_subject("Dreams", "no_such_owner")


# -- persistence ------------------------------------------------------------

def test_persists_across_instances_and_builtins_are_flagged(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.add_owner("Sleep Log")
    sub = reg.add_subject("Dreams", "sleep_log")

    again = _reg(tmp_path)
    assert [o.key for o in again.owners()][-1] == "sleep_log"
    assert again.subject(sub.id).owner == "sleep_log"
    assert [o.key for o in again.owners() if o.builtin] == ["journal", "Prayer"]


def test_new_database_is_empty_registry_with_builtins(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    assert [o.key for o in reg.owners()] == ["journal", "Prayer"]
    assert reg.subjects() == []
