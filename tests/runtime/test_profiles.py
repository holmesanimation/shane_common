"""Tests for strict runtime-profile loading."""

from pathlib import Path

import pytest

from shane_common.runtime.profiles import (
    RuntimeConfigurationError,
    load_runtime_profile,
)


def test_load_valid_runtime_profile(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(
        """
schema_version: 1
runtime_profile_id: dev.paper.local
environment:
  stage: development
  execution_mode: paper
storage_profile_set: local_hybrid_v1
service_topology: dev_paper_v1
observability_profile: local_v1
network_profile: localhost_v1
""".strip(),
        encoding="utf-8",
    )

    profile = load_runtime_profile(path)

    assert profile.schema_version == 1
    assert profile.runtime_profile_id == "dev.paper.local"
    assert profile.environment["stage"] == "development"
    assert profile.environment["execution_mode"] == "paper"
    assert profile.storage_profile_set == "local_hybrid_v1"


def test_missing_runtime_profile_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError, match="does not exist"):
        load_runtime_profile(tmp_path / "missing.yaml")


def test_malformed_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text("runtime: [bad", encoding="utf-8")

    with pytest.raises(RuntimeConfigurationError, match="Malformed YAML"):
        load_runtime_profile(path)


@pytest.mark.parametrize("content", ["[]", "'text'", "null"])
def test_root_must_be_mapping(tmp_path: Path, content: str) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeConfigurationError, match="mapping"):
        load_runtime_profile(path)


def test_missing_schema_version_fails(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text("runtime_profile_id: dev.paper.local\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigurationError, match="schema_version"):
        load_runtime_profile(path)


def test_unsupported_schema_version_fails(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(
        "schema_version: 999\nruntime_profile_id: dev.paper.local\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigurationError, match="Unsupported"):
        load_runtime_profile(path)


def test_missing_runtime_profile_id_fails(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigurationError, match="runtime_profile_id"):
        load_runtime_profile(path)


def test_environment_must_be_mapping(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(
        "schema_version: 1\nruntime_profile_id: x\nenvironment: nope\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigurationError, match="environment"):
        load_runtime_profile(path)
