import threading

from shane_common.runtime.backup_contract import JobRecord, JobStore


def test_get_survives_concurrent_atomic_replaces(tmp_path):
    store = JobStore(tmp_path)
    rec = JobRecord(job_id="1-abc", kind="sync_cloud", source="s", state="running")
    store.save(rec)
    stop, misses = threading.Event(), []

    def writer():
        while not stop.is_set():
            rec.files_done += 1
            store.save(rec)
    t = threading.Thread(target=writer)
    t.start()
    for _ in range(300):
        if store.get("1-abc") is None:
            misses.append(1)
    stop.set()
    t.join()
    assert not misses
