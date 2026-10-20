"""M5-1: verified Dropbox destination, auth and DPAPI store. Fixtures + in-memory Dropbox double only: no network, no credentials, no real drive."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from dropbox.exceptions import ApiError, AuthError, BadInputError, InternalServerError, RateLimitError

from shane_common.runtime.transfer import dropbox_auth as da
from shane_common.runtime.transfer import dropbox_destination as dd
from shane_common.runtime.transfer.dropbox_destination import DropboxAuthInvalid, DropboxDestination, UploadOutcome as O
from shane_common.runtime.transfer.dropbox_testing import FakeDropbox, _hash
from shane_common.security.secret_store import InMemorySecretStore

MIB = 1024 * 1024
RP = "/marketdata_backup/"


@pytest.fixture()
def fake():
    return FakeDropbox()


def dest(fake, **kw):
    kw.setdefault("sleep", lambda s: None)
    return DropboxDestination(fake, remote_root="/marketdata_backup", **kw)


def mk(tmp_path, name="f.bin", data=b"hello dropbox"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------------------------------ hashing
def test_dropbox_hash_matches_definition_and_both_hashes_single_pass(tmp_path):
    data = os.urandom(9 * MIB + 17)                                              # 3 blocks
    p = mk(tmp_path, data=data)
    blocks = b"".join(hashlib.sha256(data[i:i + 4 * MIB]).digest() for i in range(0, len(data), 4 * MIB))
    assert dd.dropbox_content_hash(p) == hashlib.sha256(blocks).hexdigest() == _hash(data)
    sha, dbx, size = dd.hash_file_both(p)
    assert (sha, dbx, size) == (hashlib.sha256(data).hexdigest(), dd.dropbox_content_hash(p), len(data))
    e = mk(tmp_path, "empty", b"")
    assert dd.dropbox_content_hash(e) == hashlib.sha256(b"").hexdigest()


# ------------------------------------------------------------------------------------------ verified upload
def test_small_upload_is_verified_by_an_independent_metadata_call(fake, tmp_path):
    p = mk(tmp_path)
    r = dest(fake).store_file(p, "IBApp/AAA/1m.csv")
    assert r.outcome == O.VERIFIED_UPLOADED and r.verified and r.remote_hash == r.dropbox_hash == dd.dropbox_content_hash(p)
    assert fake.calls.count("files_get_metadata") == 2                       # pre-check + separate post-upload verification
    assert fake.files[RP.lower() + "ibapp/aaa/1m.csv"][0] == p.read_bytes()
    assert (r.source_size, r.source_mtime_ns) == (p.stat().st_size, p.stat().st_mtime_ns) and r.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()


def test_upload_success_with_mismatched_remote_hash_is_not_verified(fake, tmp_path):
    fake.corrupt_next_upload = True
    r = dest(fake).store_file(mk(tmp_path), "a/b.bin")
    assert r.outcome == O.HASH_MISMATCH and not r.verified and r.remote_hash != r.dropbox_hash


def test_already_present_identical_sends_nothing(fake, tmp_path):
    p = mk(tmp_path)
    d = dest(fake)
    d.store_file(p, "a/b.bin")
    fake.calls.clear()
    r = d.store_file(p, "a/b.bin")
    assert r.outcome == O.ALREADY_PRESENT_VERIFIED and r.verified and r.bytes_sent == 0 and "files_upload" not in fake.calls


def test_conflict_is_not_overwritten(fake, tmp_path):
    p = mk(tmp_path)
    d = dest(fake)
    first = d.store_file(p, "a/b.bin")
    fake.files[RP + "a/b.bin"] = (b"someone else wrote this", "deadbeef0")           # remote changed behind our back
    p.write_bytes(b"new local content")
    r = d.store_file(p, "a/b.bin", recorded_remote_hash=first.remote_hash)
    assert r.outcome == O.CONFLICT and not r.verified
    assert fake.files[RP + "a/b.bin"][0] == b"someone else wrote this"          # untouched


def test_refresh_only_when_remote_equals_recorded_verified_hash(fake, tmp_path):
    p = mk(tmp_path)
    d = dest(fake)
    first = d.store_file(p, "a/b.bin")
    p.write_bytes(b"local changed after backup")
    r = d.store_file(p, "a/b.bin", recorded_remote_hash=first.remote_hash)
    assert r.outcome == O.REFRESHED_VERIFIED and r.verified and fake.files[RP + "a/b.bin"][0] == b"local changed after backup"
    p.write_bytes(b"changed again")
    assert d.store_file(p, "a/b.bin", recorded_remote_hash=first.remote_hash).outcome == O.CONFLICT   # stale evidence: remote no longer equals what was recorded
    assert d.store_file(p, "a/b.bin", recorded_remote_hash=r.remote_hash, allow_refresh=False).outcome == O.CONFLICT


def test_folder_at_file_path_is_a_conflict(fake, tmp_path):
    fake.folders.add(RP + "a/b.bin")
    assert dest(fake).store_file(mk(tmp_path), "a/b.bin").outcome == O.CONFLICT


def test_source_changing_during_upload_is_not_verified(fake, tmp_path):
    p = mk(tmp_path)

    def mutate(path):
        p.write_bytes(b"mutated while sending!!")
        os.utime(p, ns=(1, 1))
    fake.on_upload = mutate
    r = dest(fake).store_file(p, "a/b.bin")
    assert r.outcome == O.SOURCE_CHANGED and not r.verified


def test_source_changing_while_hashing_is_detected(fake, tmp_path, monkeypatch):
    p = mk(tmp_path)
    real = dd.hash_file_both

    def racing(path):
        out = real(path)
        Path(path).write_bytes(b"changed after hash")
        os.utime(path, ns=(5, 5))
        return out
    monkeypatch.setattr(dd, "hash_file_both", racing)
    assert dest(fake).store_file(p, "a/b.bin").outcome == O.SOURCE_CHANGED
    assert not fake.files


def test_missing_source_is_unreadable(fake, tmp_path):
    assert dest(fake).store_file(tmp_path / "gone", "a/b.bin").outcome == O.SOURCE_UNREADABLE


@pytest.mark.parametrize("rel", ["", "/abs", "a/../b", "a\\b", "c:/x", "a//b", "./a"])
def test_unsafe_remote_paths_are_refused(fake, rel):
    with pytest.raises(ValueError):
        dest(fake).remote_path(rel)


# ------------------------------------------------------------------------------------------ large / resumable
def big(tmp_path, n=20 * MIB):
    return mk(tmp_path, "big.bin", os.urandom(n))


def test_large_upload_uses_a_session_and_verifies(fake, tmp_path):
    p = big(tmp_path)
    r = dest(fake, single_shot_limit=4 * MIB, chunk_size=4 * MIB).store_file(p, "big/big.bin")
    assert r.outcome == O.VERIFIED_UPLOADED and r.bytes_sent == p.stat().st_size and not r.resumed
    assert {"files_upload_session_start", "files_upload_session_append_v2", "files_upload_session_finish"} <= set(fake.calls)
    assert fake.files[RP + "big/big.bin"][0] == p.read_bytes() and not fake.sessions


def test_interrupted_session_resumes_from_persisted_offset(fake, tmp_path):
    p = big(tmp_path)
    prog = dd.InMemoryProgress()
    d = dest(fake, single_shot_limit=4 * MIB, chunk_size=4 * MIB, progress=prog)
    fake.fail["files_upload_session_append_v2"] = [ConnectionError("link down")] * 5          # exhausts retries mid-upload
    r = d.store_file(p, "big/big.bin")
    key = RP + "big/big.bin"
    assert r.outcome == O.FAILED and r.failure.retriable and prog.get(key)["offset"] >= 4 * MIB
    saved = prog.get(key)["offset"]
    fake.calls.clear()
    r2 = d.store_file(p, "big/big.bin")                                                            # restart: new call, same progress store
    assert r2.outcome == O.VERIFIED_UPLOADED and r2.resumed and r2.bytes_sent == p.stat().st_size - saved
    assert "files_upload_session_start" not in fake.calls and fake.files[key][0] == p.read_bytes()
    assert prog.get(key) is None


def test_resume_is_discarded_when_the_source_changed(fake, tmp_path):
    p = big(tmp_path)
    prog = dd.InMemoryProgress()
    d = dest(fake, single_shot_limit=4 * MIB, chunk_size=4 * MIB, progress=prog)
    fake.fail["files_upload_session_append_v2"] = [ConnectionError("x")] * 5
    d.store_file(p, "big/big.bin")
    p.write_bytes(os.urandom(20 * MIB))
    fake.calls.clear()
    r = d.store_file(p, "big/big.bin")
    assert r.outcome == O.VERIFIED_UPLOADED and not r.resumed and "files_upload_session_start" in fake.calls and fake.files[RP + "big/big.bin"][0] == p.read_bytes()


def test_expired_session_restarts_once(fake, tmp_path):
    p = big(tmp_path)
    prog = dd.InMemoryProgress()
    d = dest(fake, single_shot_limit=4 * MIB, chunk_size=4 * MIB, progress=prog)
    fake.fail["files_upload_session_append_v2"] = [ConnectionError("x")] * 5
    d.store_file(p, "big/big.bin")
    fake.forget_sessions()                                                                          # Dropbox discarded the session
    r = d.store_file(p, "big/big.bin")
    assert r.outcome == O.VERIFIED_UPLOADED and fake.files[RP + "big/big.bin"][0] == p.read_bytes()


def test_ledger_progress_survives_a_new_instance(tmp_path):
    a = dd.LedgerProgress(tmp_path / "p.json")
    a.put("k", {"session_id": "s", "offset": 4})
    assert dd.LedgerProgress(tmp_path / "p.json").get("k")["offset"] == 4
    a.delete("k")
    assert dd.LedgerProgress(tmp_path / "p.json").get("k") is None


# ------------------------------------------------------------------------------------------ retries / failures / auth
def test_retries_rate_limit_and_server_errors_then_succeeds(fake, tmp_path):
    waits = []
    fake.fail["files_upload"] = [RateLimitError("r", backoff=2), InternalServerError("r", 500, "boom")]
    r = DropboxDestination(fake, remote_root="/x", sleep=waits.append).store_file(mk(tmp_path), "a.bin")
    assert r.outcome == O.VERIFIED_UPLOADED and r.attempts >= 3 and waits[0] == 2


def test_non_retriable_api_error_is_a_structured_failure(fake, tmp_path):
    fake.fail["files_upload"] = [ApiError("r", "insufficient_space", "full", "en")]
    r = dest(fake).store_file(mk(tmp_path), "a.bin")
    assert r.outcome == O.FAILED and r.failure.kind == "insufficient_space" and not r.failure.retriable and not r.verified


def test_auth_error_raises_and_is_never_retried(fake, tmp_path):
    fake.fail["files_get_metadata"] = [AuthError("r", "expired_access_token")]
    with pytest.raises(DropboxAuthInvalid):
        dest(fake).store_file(mk(tmp_path), "a.bin")
    assert fake.calls.count("files_get_metadata") == 1
    fake.fail["files_get_metadata"] = [BadInputError("r", "invalid_grant")]
    with pytest.raises(DropboxAuthInvalid):
        dest(fake).get_remote("a.bin")


# ------------------------------------------------------------------------------------------ remote re-verification / download
def test_verify_remote_states(fake, tmp_path):
    d = dest(fake)
    r = d.store_file(mk(tmp_path), "a.bin")
    assert d.verify_remote("a.bin", r.dropbox_hash).state == "OK"
    assert d.verify_remote("a.bin", "0" * 64).state == "MISMATCH"
    fake.files_delete_v2(RP + "a.bin")
    assert d.verify_remote("a.bin", r.dropbox_hash).state == "MISSING"
    fake.fail["files_get_metadata"] = [ConnectionError("down")] * 5
    assert d.verify_remote("a.bin", r.dropbox_hash).state == "ERROR"


def test_download_to_reports_independent_hashes_without_claiming_a_restore(fake, tmp_path):
    d = dest(fake)
    p = mk(tmp_path)
    d.store_file(p, "a.bin")
    got = d.download_to("a.bin", tmp_path / "dl" / "a.bin")
    assert got.remote_hash_ok and got.sha256 == hashlib.sha256(p.read_bytes()).hexdigest() and got.size == p.stat().st_size


# ------------------------------------------------------------------------------------------ auth / secrets
class FakeFlow:
    class _R:
        refresh_token = "RT-SECRET"
        account_id = "dbid:1"

    def start(self):
        return "https://dropbox.example/authorize"

    def finish(self, code):
        assert code == "THE-CODE"
        return self._R


def test_authorization_persists_only_the_refresh_token_and_never_returns_it():
    store = InMemorySecretStore()
    creds = da.DropboxCredentialStore(store, "trading_system.dropbox")
    res = da.complete_authorization("APPKEY", " THE-CODE\n", creds, flow=da.DropboxAuthFlow("APPKEY", flow=FakeFlow()))
    assert creds.get_refresh_token() == "RT-SECRET" and res.refresh_token == "" and res.account_id == "dbid:1"
    assert "RT-SECRET" not in repr(res)
    creds.clear()
    assert creds.get_refresh_token() is None


def test_client_is_built_from_refresh_token_with_app_key_and_no_access_token():
    seen = {}

    def factory(**kw):
        seen.update(kw)
        return "client"
    creds = da.DropboxCredentialStore(InMemorySecretStore(), "ns")
    with pytest.raises(LookupError):
        da.build_client("APPKEY", creds, client_factory=factory)
    creds.set_refresh_token("RT")
    assert da.build_client("APPKEY", creds, client_factory=factory) == "client"
    assert seen["oauth2_refresh_token"] == "RT" and seen["app_key"] == "APPKEY" and "oauth2_access_token" not in seen      # the SDK refreshes access tokens itself
    with pytest.raises(ValueError):
        da.DropboxCredentialStore(InMemorySecretStore(), "")


def test_real_sdk_client_accepts_refresh_token_arguments_offline():
    import dropbox
    s = InMemorySecretStore()
    s.set_secret("ns", "refresh_token", "RT")
    c = da.build_client("APPKEY", da.DropboxCredentialStore(s, "ns"))
    assert isinstance(c, dropbox.Dropbox)                                                  # constructed offline: no network call, no token exchange yet


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_store_roundtrip_never_writes_plaintext(tmp_path):
    from shane_common.security.windows_dpapi_store import WindowsDpapiSecretStore
    p = tmp_path / "secrets.dat"
    s = WindowsDpapiSecretStore(path=p)
    s.set_secret("ns", "refresh_token", "PLAINTEXT-SECRET-123")
    assert s.get_secret("ns", "refresh_token") == "PLAINTEXT-SECRET-123" and "PLAINTEXT-SECRET-123" not in p.read_text()
    s.delete_secret("ns", "refresh_token")
    assert s.get_secret("ns", "refresh_token") is None
    with pytest.raises(ValueError):
        WindowsDpapiSecretStore()


def test_independence_no_application_imports():
    import ast
    import importlib
    for name in ("dropbox_destination", "dropbox_auth", "dropbox_testing"):
        mod = importlib.import_module(f"shane_common.runtime.transfer.{name}")
        names = set()
        for n in ast.walk(ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        assert not ({"purity_app", "trading_platform", "trading_system", "services"} & names), (name, names)
