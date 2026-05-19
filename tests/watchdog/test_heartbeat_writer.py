from pathlib import Path

from shane_common.watchdog.heartbeat_writer import HeartbeatWriter


def test_start_clears_stale_exit_marker(tmp_path: Path) -> None:
    writer = HeartbeatWriter("purity_app", tmp_path)
    exit_marker = tmp_path / "purity_app.exit_marker.json"
    exit_marker.parent.mkdir(parents=True, exist_ok=True)
    exit_marker.write_text("{}", encoding="utf-8")

    writer.start()
    writer.stop()

    assert exit_marker.exists()

    writer2 = HeartbeatWriter("purity_app", tmp_path)
    writer2.start()
    try:
        assert exit_marker.exists() is False
    finally:
        writer2.stop()