from pathlib import Path

import pytest

from shane_common.runtime.storage.volume import StorageVolume


def test_storage_volume_normalizes_fields() -> None:
    volume = StorageVolume(
        volume_id=" windows:volume:abc ",
        mount_point=Path("/"),
        filesystem_type=" NTFS ",
    )

    assert volume.volume_id == "windows:volume:abc"
    assert volume.mount_point == Path("/")
    assert volume.filesystem_type == "NTFS"


def test_storage_volume_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="volume_id"):
        StorageVolume(volume_id="  ", mount_point=Path("/"))
