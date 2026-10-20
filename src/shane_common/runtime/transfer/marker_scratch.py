"""Bounded Dropbox marker-semantics scratch test (PREPARED; execution needs a separate owner authorization).

Question it answers: does Dropbox refuse a second ``WriteMode.add`` to an existing path atomically (exactly one winner, one server revision), and are the signals the commit
protocol relies on (independent metadata hash, ``files_list_revisions``, listing visibility, Range reads) trustworthy?

This module only *runs the checks against a ``DropboxDestination`` it is given* and returns a report; it never builds a client, reads credentials, writes files, or deletes anything.
All objects live under one throw-away folder ``<root>/<folder>`` and are tiny. The caller (an owner-authorized wrapper) enforces authorization, expiry and evidence storage.
The in-process thread races complement - not replace - the two-process race (run two wrappers at the same ``start_at_utc`` with different ``process_tag``) and the manual
kill-mid-request step, both listed in the report as ``manual``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata

from .dropbox_objects import MARKER_LIVE_TEST, MarkerOutcome, MarkerUnknown, RangeNotHonoured, content_hash_of, download_range, store_bytes, store_marker

PROTOCOL_VERSION = "mdpkg-marker-protocol/1"
HARD_MAX_OBJECTS = 200
HARD_MAX_TOTAL_BYTES = 1024 * 1024
HARD_MAX_THREADS = 32
HARD_MAX_ROUNDS = 50
FOLDER_PREFIX = "mdpkg-scratch-"
PAYLOAD_BYTES = 256


# ---- Phase B (v2) additions: cross-process visibility (step 8) and a five-position chain race (step 9). Everything below is additive; the v1 steps and verdict are unchanged.
ALLOWED_TAGS = ("p1", "pa", "pb")                 # a closed set: with add-only uploads and fixed path names it bounds the objects ANY number of processes can ever create in a folder
VISIBILITY_TAGS = ("pa", "pb")
VIS_OBJECTS = 3                                   # objects each process creates (and the peer watches)
CHAIN_POSITIONS = 5
CRITERIA_V1, CRITERIA_V2 = "marker_scratch/1", "marker_scratch/2"


class ScratchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Timing:
    """Schedule of the Phase B steps, relative to ``start_at_utc``. The live launcher always uses ``LIVE_TIMING`` (validated); tests pass tiny values directly to ``run_scratch_test``."""
    vis_offset_seconds: float = 900.0            # step 8 starts this long after start_at_utc, so that steps 1-7 are finished in BOTH processes (Phase A took 169 s for them alone)
    slot_seconds: float = 5.0                    # spacing of the creation slots of step 8
    watch_seconds: float = 60.0                  # how long each peer object is watched (and how long step 9 waits for the listing)
    poll_seconds: float = 1.0
    chain_offset_seconds: float = 120.0          # step 9 starts this long after step 8

    def validate_live(self) -> "Timing":
        if not (300 <= self.vis_offset_seconds <= 1800 and 2 <= self.slot_seconds <= 30 and 30 <= self.watch_seconds <= 60 and 0.5 <= self.poll_seconds <= 5
                and 90 <= self.chain_offset_seconds <= 600):
            raise ScratchConfigError("live timing outside the approved bounds (watch window 30..60 s, polls >= 0.5 s)")
        if self.chain_offset_seconds < (VIS_OBJECTS - 1) * self.slot_seconds + self.watch_seconds + 30:
            raise ScratchConfigError("step 9 must start after step 8's last watch window has ended (with 30 s of margin)")
        return self


LIVE_TIMING = Timing()


@dataclass(frozen=True)
class ScratchConfig:
    folder: str                       # e.g. mdpkg-scratch-20261101 (relative to the destination's remote root)
    rounds: int = 20
    threads: int = 16
    process_tag: str = "p1"
    start_at_utc: Optional[str] = None  # align two processes: both wait until this UTC instant (YYYY-MM-DDTHH:MM:SSZ)
    peer_tag: Optional[str] = None      # Phase B: the other process's tag (steps 8 and 9)
    visibility_probe: bool = False      # Phase B: run steps 8 and 9

    def validate(self) -> "ScratchConfig":
        f = self.folder
        if not f.startswith(FOLDER_PREFIX) or "/" in f or "\\" in f or ".." in f or len(f) > 64:
            raise ScratchConfigError(f"folder must be a single path segment starting with {FOLDER_PREFIX!r}")
        if not 1 <= self.rounds <= HARD_MAX_ROUNDS or not 2 <= self.threads <= HARD_MAX_THREADS:
            raise ScratchConfigError("rounds/threads outside the hard limits")
        if self.process_tag not in ALLOWED_TAGS:
            raise ScratchConfigError(f"process_tag must be one of {ALLOWED_TAGS}")
        if self.visibility_probe:
            if self.process_tag not in VISIBILITY_TAGS or self.peer_tag not in VISIBILITY_TAGS or self.peer_tag == self.process_tag:
                raise ScratchConfigError(f"the visibility probe needs the two distinct tags {VISIBILITY_TAGS}: process_tag and peer_tag")
            if not self.start_at_utc:
                raise ScratchConfigError("the visibility probe needs start_at_utc (both processes align on it)")
        elif self.peer_tag is not None:
            raise ScratchConfigError("peer_tag is only valid with the visibility probe")
        if planned_objects(self) > HARD_MAX_OBJECTS or planned_bytes(self) > HARD_MAX_TOTAL_BYTES:
            raise ScratchConfigError("planned objects/bytes exceed the hard caps")
        return self


def planned_objects(c: ScratchConfig) -> int:
    """Folder-wide worst case assuming BOTH processes run (the figure checked against the authorization caps): distinct + identical race paths, re-add, listing probes, range object
    (upper bound 2*rounds + 7); Phase B adds the 2 x 3 visibility objects and the 5 chain positions."""
    return 2 * c.rounds + 3 + 4 + ((2 * VIS_OBJECTS + CHAIN_POSITIONS) if c.visibility_probe else 0)


def aggregate_max_objects(c: ScratchConfig) -> int:
    """The most objects that ANY number of processes can leave in the folder: every process writes only the fixed paths of ``scratch_paths`` (tags are a closed set, uploads are add-only),
    so concurrent processes cannot each consume the full allowance. 2*rounds + 1 + 3 + 1 (+ 2*VIS + CHAIN)."""
    return 2 * c.rounds + 1 + 3 + 1 + ((2 * VIS_OBJECTS + CHAIN_POSITIONS) if c.visibility_probe else 0)


def aggregate_max_stored_bytes(c: ScratchConfig) -> int:
    return (aggregate_max_objects(c) - 1) * PAYLOAD_BYTES + 64 * 1024       # every object <= 256 bytes except the 64 KiB range blob


def planned_bytes(c: ScratchConfig) -> int:
    return planned_objects(c) * PAYLOAD_BYTES + 64 * 1024              # + the 64 KiB range-probe object


@dataclass(frozen=True)
class ScratchPaths:
    upload_files: frozenset                                            # exact relative paths this process may CREATE (lower-cased)
    read_files: frozenset                                              # exact relative paths it may read (metadata, download, revisions)
    list_dirs: frozenset                                               # directories it may list (recursive)


def scratch_paths(cfg: ScratchConfig) -> ScratchPaths:
    """The complete, fixed set of paths the harness touches for ``cfg`` (also the whitelist the launcher's scoped client enforces)."""
    b = cfg.folder
    up = {f"{b}/distinct/r{r:03d}.json" for r in range(cfg.rounds)} | {f"{b}/identical/r{r:03d}.json" for r in range(cfg.rounds)}
    up |= {f"{b}/readd/one.json", f"{b}/range/blob.bin"} | {f"{b}/listing/m{i}.json" for i in range(3)}
    read, dirs = set(up), {f"{b}/listing"}
    if cfg.visibility_probe:
        up |= {f"{b}/xproc/{cfg.process_tag}_{k}.json" for k in range(1, VIS_OBJECTS + 1)} | {f"{b}/chain/C{n:06d}.json" for n in range(1, CHAIN_POSITIONS + 1)}
        read = up | {f"{b}/xproc/{t}_{k}.json" for t in VISIBILITY_TAGS for k in range(1, VIS_OBJECTS + 1)}
        dirs |= {f"{b}/xproc", f"{b}/chain"}
    return ScratchPaths(frozenset(x.lower() for x in up), frozenset(x.lower() for x in read), frozenset(x.lower() for x in dirs))


def _payload(tag: str, i: int, round_: int) -> bytes:
    return (json.dumps({"p": tag, "t": i, "r": round_}) + " " * PAYLOAD_BYTES)[:PAYLOAD_BYTES].encode()


def _revisions(dest, rel: str) -> Optional[int]:
    fn = getattr(dest._c, "files_list_revisions", None)
    if fn is None:
        return None
    try:
        return len(fn(dest.remote_path(rel), limit=10).entries)
    except Exception:                                                   # noqa: BLE001 - reported as unavailable, never guessed
        return None


def _race(dest, rel: str, payloads: list) -> list:
    barrier, out, lock = threading.Barrier(len(payloads)), [], threading.Lock()

    def work(data: bytes) -> None:
        barrier.wait()
        try:
            r = store_marker(dest, data, rel).outcome.value
        except MarkerUnknown as e:
            r = f"UNKNOWN:{e}"[:80]
        with lock:
            out.append(r)

    ts = [threading.Thread(target=work, args=(p,)) for p in payloads]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def _wait_until(start_at_utc: Optional[str], sleep: Callable[[float], None]) -> None:
    if not start_at_utc:
        return
    _sleep_until(datetime.strptime(start_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), sleep, lambda: datetime.now(timezone.utc))


def _sleep_until(target: datetime, sleep: Callable[[float], None], now: Callable[[], datetime]) -> None:
    while True:
        left = (target - now()).total_seconds()
        if left <= 0:
            return
        sleep(min(left, 1.0))


def _stored_hash(dest, rel: str) -> Optional[str]:
    try:
        f = dest.get_remote(rel)
    except Exception:                                                   # noqa: BLE001 - recorded as unavailable, never guessed
        return None
    return None if f is None else f.content_hash


def run_scratch_test(dest, cfg: ScratchConfig, *, sleep: Callable[[float], None] = time.sleep, timing: Optional[Timing] = None,
                     now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    cfg = cfg.validate()
    timing = timing or LIVE_TIMING
    _wait_until(cfg.start_at_utc, sleep)
    rep: dict = {"protocol_version": PROTOCOL_VERSION, "folder": cfg.folder, "process_tag": cfg.process_tag, "threads": cfg.threads, "rounds": cfg.rounds,
                 "planned_objects": planned_objects(cfg), "steps": {}, "manual": ["step 4b: second process racing on the same paths (same start_at_utc, different process_tag)",
                                                                                   "step 5: kill a client mid-request, then read back the marker state"],
                 "procedure": MARKER_LIVE_TEST}
    base = cfg.folder
    # step 1: distinct payload races -> exactly one WON, the rest EXISTS_DIFFERENT, one revision
    s1 = {"rounds": [], "failures": []}
    for r in range(cfg.rounds):
        rel = f"{base}/distinct/r{r:03d}.json"
        res = Counter(_race(dest, rel, [_payload(cfg.process_tag, i, r) for i in range(cfg.threads)]))
        rev = _revisions(dest, rel)
        ok = res.get("WON", 0) <= 1 and res.get("WON", 0) + res.get("EXISTS_DIFFERENT", 0) == cfg.threads and (rev in (1, None))
        if cfg.process_tag == "p1":
            ok = ok and res.get("WON", 0) == 1
        s1["rounds"].append({"path": rel, "outcomes": dict(res), "revisions": rev})
        if not ok:
            s1["failures"].append(rel)
    s1["pass"] = not s1["failures"] and all(x["revisions"] is not None for x in s1["rounds"])
    s1["revisions_available"] = all(x["revisions"] is not None for x in s1["rounds"])
    rep["steps"]["1_distinct_race"] = s1
    # step 2: identical payload races -> one WON, the rest EXISTS_SAME
    s2 = {"rounds": [], "failures": []}
    for r in range(cfg.rounds):
        rel = f"{base}/identical/r{r:03d}.json"
        res = Counter(_race(dest, rel, [_payload("same", 0, r)] * cfg.threads))
        rev = _revisions(dest, rel)
        ok = res.get("WON", 0) <= 1 and res.get("WON", 0) + res.get("EXISTS_SAME", 0) == cfg.threads and rev in (1, None)
        s2["rounds"].append({"path": rel, "outcomes": dict(res), "revisions": rev})
        if not ok:
            s2["failures"].append(rel)
    s2["pass"] = not s2["failures"]
    rep["steps"]["2_identical_race"] = s2
    # step 3: re-add the same path from one client; revisions must stay at one and the original content must remain
    rel = f"{base}/readd/one.json"
    first = store_marker(dest, _payload(cfg.process_tag, 0, 0), rel).outcome.value
    h1 = _stored_hash(dest, rel)
    again = store_marker(dest, _payload(cfg.process_tag, 0, 0), rel).outcome.value
    h2 = _stored_hash(dest, rel)
    other = store_marker(dest, _payload(cfg.process_tag, 1, 1), rel).outcome.value
    h3 = _stored_hash(dest, rel)
    rev = _revisions(dest, rel)
    rep["steps"]["3_readd"] = {"first": first, "same_again": again, "different": other, "revisions": rev,
                               "hashes": {"after_first": h1, "after_again": h2, "after_different": h3, "own_payload_hash": content_hash_of(_payload(cfg.process_tag, 0, 0))},
                               "pass": first in ("WON", "EXISTS_SAME", "EXISTS_DIFFERENT") and again in ("EXISTS_SAME", "EXISTS_DIFFERENT") and other == "EXISTS_DIFFERENT" and rev in (1, None)}
    # step 6: is a freshly created marker visible to a recursive listing immediately?
    visible = []
    for i in range(3):
        rel = f"{base}/listing/m{i}.json"
        res = store_marker(dest, _payload(cfg.process_tag, i, 99), rel)
        polls = 0
        seen = False
        while polls < 5 and not seen:
            polls += 1
            lf = dest._c.files_list_folder(dest.remote_path(f"{base}/listing"), recursive=True, limit=2000)
            seen = any(getattr(e, "path_lower", "").endswith(f"m{i}.json") for e in lf.entries)
            if not seen:
                sleep(0.5)
        visible.append({"path": rel, "created": res.outcome.value, "visible_after_polls": polls if seen else None})
    rep["steps"]["6_listing_visibility"] = {"objects": visible, "pass": all(v["visible_after_polls"] == 1 for v in visible),
                                            "note": "the chain reader needs listings to show committed markers; a pass requires visibility on the first poll"}
    # step 7: Range read of a small scratch object
    rel = f"{base}/range/blob.bin"
    blob = bytes((i * 7) % 256 for i in range(64 * 1024))
    stored = store_bytes(dest, blob, rel).state
    try:
        got = download_range(dest, rel, 1000, 500)
        rng = {"stored": stored, "honoured": True, "exact": got == blob[1000:1500]}
    except RangeNotHonoured as e:
        rng = {"stored": stored, "honoured": False, "error": str(e)[:120], "exact": False}
    rng["pass"] = rng["honoured"] and rng["exact"]
    rep["steps"]["7_range"] = rng
    rep["manual_steps_status"] = {"4b_two_process_race": "NOT_RUN", "5_kill_mid_request": "NOT_RUN"}      # this harness can never mark them completed
    if cfg.visibility_probe:
        v2 = {}
        v2["8_cross_process_visibility"] = _step8(dest, cfg, timing, sleep, now)
        v2["9_chain_race"] = _step9(dest, cfg, timing, sleep, now)
        rep["steps_v2"] = v2
        rep["manual_steps_status"]["4b_two_process_race"] = "RUN_BY_TWO_LAUNCHER_PROCESSES_SEE_COMBINED_REPORT"
    rep["marker_semantics_verified"] = False                                                              # only the owner may record this, after ALL steps (incl. manual) pass
    rep["pass_automated"] = all(v.get("pass") for v in rep["steps"].values())                              # v1: computed over the v1 steps ONLY, exactly as in Phase A
    rep["criteria_version"] = {"headline": CRITERIA_V1, "also_computed": CRITERIA_V2}
    rep["v2"] = evaluate_v2(rep)
    rep["verdict"] = ("AUTOMATED CHECKS PASSED - still requires the manual steps and the two-process race before the owner may record marker_semantics_verified"
                      if rep["pass_automated"] else "FAILED - do NOT record marker_semantics_verified; keep the single-writer attestation as the only guarantee")
    canon = json.dumps({k: v for k, v in rep.items() if k != "procedure"}, sort_keys=True, separators=(",", ":")).encode()
    rep["report_sha256"] = hashlib.sha256(canon).hexdigest()
    return rep


# ------------------------------------------------------------------------------------------------------ Phase B: steps 8 and 9, v2 criteria, combination of two reports
def _u(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pu(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _vis_payload(tag: str, k: int) -> bytes:
    return (json.dumps({"p": tag, "k": k, "s": "xproc"}) + " " * PAYLOAD_BYTES)[:PAYLOAD_BYTES].encode()


def _is_not_found(e: Exception) -> bool:
    err = getattr(e, "error", None)
    try:
        return bool(err is not None and err.is_path() and err.get_path().is_not_found())
    except Exception:                                                   # noqa: BLE001
        return False


def _download(dest, rel: str):
    """Exact-path download -> (bytes, metadata), or None when the path definitely does not exist. Any other failure raises (never read as 'absent')."""
    path = dest.remote_path(rel)
    try:
        (md, res), _ = dest._call(lambda: dest._c.files_download(path))
    except ApiError as e:
        if _is_not_found(e):
            return None
        raise
    return bytes(res.content), md


def _list_names(dest, folder_rel: str) -> set:
    """Lower-case FILE names currently shown by a recursive listing of ``folder_rel`` (empty when the folder does not exist yet).
    History: until 2026-10-16 every entry, folder entries included, was counted; the live Phase B run therefore recorded the folder name ``chain`` as a "listed marker"
    (``ahead_of_exact: ["chain"]``). Fixed to files only; the historical evidence is untouched and re-read through ``combine_reports(reinterpret_listing_entries=True)``."""
    try:
        lf = dest._c.files_list_folder(dest.remote_path(folder_rel), recursive=True, limit=2000)
    except ApiError as e:
        if _is_not_found(e) or "not_found" in str(getattr(e, "error", "")):
            return set()
        raise
    return {getattr(x, "path_lower", "").rsplit("/", 1)[-1] for x in lf.entries if isinstance(x, FileMetadata)}      # FILES only (as ``list_remote_tree`` does): a folder entry is not a listed marker


def _step8(dest, cfg: ScratchConfig, timing: Timing, sleep, now) -> dict:
    from datetime import timedelta
    base, tag, peer = cfg.folder, cfg.process_tag, cfg.peer_tag
    start = datetime.strptime(cfg.start_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    vis_start = start + timedelta(seconds=timing.vis_offset_seconds)
    out = {"vis_start_utc": _u(vis_start), "late_start": now() > vis_start + timedelta(seconds=timing.slot_seconds), "watch_seconds": timing.watch_seconds, "own_objects": [], "peer_objects": []}
    lock = threading.Lock()

    def creator() -> None:
        for k in range(1, VIS_OBJECTS + 1):
            slot = vis_start + timedelta(seconds=(k - 1) * timing.slot_seconds)
            _sleep_until(slot, sleep, now)
            rel, data = f"{base}/xproc/{tag}_{k}.json", _vis_payload(tag, k)
            issued = now()
            try:
                r = store_marker(dest, data, rel)
                outcome, revs = r.outcome.value, r.revisions
            except MarkerUnknown as e:
                outcome, revs = f"UNKNOWN:{e}"[:80], None
            returned = now()
            with lock:
                out["own_objects"].append({"path": rel, "slot_utc": _u(slot), "issued_utc": _u(issued), "returned_utc": _u(returned), "outcome": outcome, "revisions": revs,
                                           "payload_dropbox_hash": content_hash_of(data)})

    def watcher(k: int) -> None:
        rel, expected = f"{base}/xproc/{peer}_{k}.json", _vis_payload(peer, k)
        exp_hash = content_hash_of(expected)
        slot = vis_start + timedelta(seconds=(k - 1) * timing.slot_seconds)
        deadline = slot + timedelta(seconds=timing.watch_seconds)
        row = {"path": rel, "slot_utc": _u(slot), "first_metadata_utc": None, "first_download_utc": None, "download_bytes_equal": None, "download_hash_equal": None,
               "first_listing_utc": None, "polls": {"metadata": 0, "download": 0, "listing": 0}, "errors": 0, "stopped_utc": None}
        _sleep_until(slot, sleep, now)
        while now() < deadline:
            if row["first_metadata_utc"] is None:
                row["polls"]["metadata"] += 1
                try:
                    if dest.get_remote(rel) is not None:
                        row["first_metadata_utc"] = _u(now())
                except Exception:                                       # noqa: BLE001 - counted, never read as absence
                    row["errors"] += 1
            if row["first_download_utc"] is None:
                row["polls"]["download"] += 1
                try:
                    got = _download(dest, rel)
                    if got is not None:
                        row["first_download_utc"] = _u(now())
                        row["download_bytes_equal"] = got[0] == expected
                        row["download_hash_equal"] = getattr(got[1], "content_hash", None) == exp_hash
                except Exception:                                       # noqa: BLE001
                    row["errors"] += 1
            if row["first_listing_utc"] is None:
                row["polls"]["listing"] += 1
                try:
                    if f"{peer}_{k}.json" in _list_names(dest, f"{base}/xproc"):
                        row["first_listing_utc"] = _u(now())
                except Exception:                                       # noqa: BLE001
                    row["errors"] += 1
            if row["first_metadata_utc"] and row["first_download_utc"] and row["first_listing_utc"]:
                break
            sleep(timing.poll_seconds)
        row["stopped_utc"] = _u(now())
        with lock:
            out["peer_objects"].append(row)

    ts = [threading.Thread(target=creator)] + [threading.Thread(target=watcher, args=(k,)) for k in range(1, VIS_OBJECTS + 1)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    out["own_objects"].sort(key=lambda x: x["path"])
    out["peer_objects"].sort(key=lambda x: x["path"])
    return out


def _step9(dest, cfg: ScratchConfig, timing: Timing, sleep, now) -> dict:
    from datetime import timedelta
    base, tag = cfg.folder, cfg.process_tag
    start = datetime.strptime(cfg.start_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    chain_start = start + timedelta(seconds=timing.vis_offset_seconds + timing.chain_offset_seconds)
    _sleep_until(chain_start, sleep, now)
    late_start = now() > chain_start + timedelta(seconds=max(timing.slot_seconds, 1.0))        # began well after the scheduled instant: the race was not (or not fully) contended
    own, claimed, adopted, anomalies = {}, [], [], []
    prev: Optional[bytes] = None
    for n in range(1, CHAIN_POSITIONS + 1):
        rel = f"{base}/chain/C{n:06d}.json"
        payload = json.dumps({"n": n, "parent": hashlib.sha256(prev).hexdigest() if prev is not None else None, "writer": tag}, sort_keys=True, separators=(",", ":")).encode()
        own[n] = payload
        try:
            outcome = store_marker(dest, payload, rel).outcome.value
        except MarkerUnknown as e:
            anomalies.append({"n": n, "kind": "unknown_outcome", "detail": str(e)[:100]})
            break
        if outcome == "WON":
            claimed.append(n)
            prev = payload
        elif outcome == "EXISTS_DIFFERENT":                             # lost: adopt the WINNER'S stored bytes (exact path) as the parent of the next position
            got, deadline = None, now() + timedelta(seconds=timing.watch_seconds)
            while got is None and now() < deadline:
                try:
                    got = _download(dest, rel)
                except Exception as e:                                  # noqa: BLE001
                    anomalies.append({"n": n, "kind": "winner_read_error", "detail": type(e).__name__})
                if got is None:
                    sleep(timing.poll_seconds)
            if got is None:
                anomalies.append({"n": n, "kind": "winner_unreadable"})
                break
            prev = got[0]
            adopted.append(n)
        else:
            anomalies.append({"n": n, "kind": outcome})                 # EXISTS_SAME, RACE_SUSPECTED, HASH_MISMATCH: none is expected between two writers with different payloads
            break
    t_end = now()
    deadline, seen, complete_after = t_end + timedelta(seconds=timing.watch_seconds), set(), None
    while True:
        try:
            seen = _list_names(dest, f"{base}/chain")
        except Exception as e:                                          # noqa: BLE001
            anomalies.append({"kind": "listing_error", "detail": type(e).__name__})
            seen = set()
        if len(seen) >= CHAIN_POSITIONS:
            complete_after = round((now() - t_end).total_seconds(), 3)
            break
        if now() >= deadline:
            break
        sleep(timing.poll_seconds)
    stored, prev_sha = [], None
    for n in range(1, CHAIN_POSITIONS + 1):
        rel = f"{base}/chain/C{n:06d}.json"
        try:
            got = _download(dest, rel)
        except Exception as e:                                          # noqa: BLE001
            anomalies.append({"n": n, "kind": "final_read_error", "detail": type(e).__name__})
            got = None
        revs = _revisions(dest, rel)
        if got is None:
            stored.append({"n": n, "present": False, "parent_ok": False})
            prev_sha = None
            continue
        data, md = got
        try:
            doc = json.loads(data)
        except ValueError:
            doc = {}
        sha = hashlib.sha256(data).hexdigest()
        parent_ok = doc.get("n") == n and doc.get("parent") == prev_sha                      # position 1 must have no parent; every later one the SHA-256 of the previous stored bytes
        stored.append({"n": n, "present": True, "sha256": sha, "writer": doc.get("writer"), "parent": doc.get("parent"), "parent_ok": parent_ok, "revisions": revs, "own": data == own.get(n),
                       "dropbox_hash_ok": getattr(md, "content_hash", None) == content_hash_of(data)})
        prev_sha = sha
    names = {f"c{x['n']:06d}.json" for x in stored if x["present"]}
    return {"chain_start_utc": _u(chain_start), "late_start": late_start, "claimed": claimed, "adopted": adopted, "anomalies": anomalies, "stored": stored,
            "parent_links_ok": len(stored) == CHAIN_POSITIONS and all(x["parent_ok"] for x in stored),
            "false_commit_claims": [n for n in claimed if not next((x for x in stored if x["n"] == n), {}).get("own")],
            "listing": {"complete_after_seconds": complete_after, "names_final": sorted(seen), "ahead_of_exact": sorted(seen - names)}}


def _ok_outcomes(o: dict, allowed: set, threads: int) -> bool:
    return set(o) <= allowed and sum(o.values()) == threads


def evaluate_v2(rep: dict) -> dict:
    """Criteria marker_scratch/2, computed NEXT TO the unchanged v1 verdict (see M5-5_G6_Marker_Scratch_Criteria_v2_Proposal.md). Local to one process; the cross-process rules
    (exactly one WON per contested path, disjoint chain claims) are evaluated by ``combine_reports``."""
    st, t, out = rep["steps"], rep["threads"], {}
    s1 = st["1_distinct_race"]["rounds"]
    out["1_distinct_race"] = {"pass": all(r["outcomes"].get("WON", 0) <= 1 and r["outcomes"].get("WON", 0) + r["outcomes"].get("EXISTS_DIFFERENT", 0) == t and r["revisions"] == 1 for r in s1)}
    s2 = st["2_identical_race"]["rounds"]
    out["2_identical_race"] = {"pass": all(_ok_outcomes(r["outcomes"], {"WON", "EXISTS_SAME"}, t) and r["revisions"] == 1 for r in s2)}
    s3 = st["3_readd"]
    h = s3.get("hashes") or {}
    same_hashes = bool(h) and h.get("after_first") is not None and h.get("after_first") == h.get("after_again") == h.get("after_different")
    if s3["first"] in ("WON", "EXISTS_SAME"):
        ok3 = s3["same_again"] in ("WON", "EXISTS_SAME") and same_hashes and h.get("after_first") == h.get("own_payload_hash")
    else:
        ok3 = s3["first"] == "EXISTS_DIFFERENT" and s3["same_again"] == "EXISTS_DIFFERENT" and same_hashes
    out["3_readd"] = {"pass": bool(ok3 and s3["different"] == "EXISTS_DIFFERENT" and s3["revisions"] == 1)}
    s6 = st["6_listing_visibility"]["objects"]
    out["6a_exact_path_metadata_immediate"] = {"pass": all(o["created"] in ("WON", "EXISTS_SAME", "EXISTS_DIFFERENT") for o in s6)}
    out["6b_listing_visibility_informational"] = {"pass": None, "visible_after_polls": [o["visible_after_polls"] for o in s6]}
    out["7_range"] = {"pass": bool(st["7_range"]["pass"])}
    v2 = rep.get("steps_v2")
    if v2:
        s8, s9 = v2["8_cross_process_visibility"], v2["9_chain_race"]
        peers_ok = len(s8["peer_objects"]) == VIS_OBJECTS and all(p["first_metadata_utc"] and p["first_download_utc"] and p["download_bytes_equal"] is True and p["download_hash_equal"] is True
                                                                 for p in s8["peer_objects"])
        own_ok = len(s8["own_objects"]) == VIS_OBJECTS and all(o["outcome"] == "WON" and o["revisions"] == 1 for o in s8["own_objects"])
        out["8_cross_process_visibility"] = {"pass": bool(peers_ok and own_ok and not s8["late_start"]),
                                             "listing_first_seen_informational": [p["first_listing_utc"] for p in s8["peer_objects"]]}
        stored_ok = len(s9["stored"]) == CHAIN_POSITIONS and all(x["present"] and x["revisions"] == 1 and x["dropbox_hash_ok"] for x in s9["stored"])
        out["9_chain_race"] = {"pass": bool(not s9["late_start"] and not s9["anomalies"] and not s9["false_commit_claims"] and stored_ok and s9["parent_links_ok"]
                                            and sorted(s9["claimed"] + s9["adopted"]) == list(range(1, CHAIN_POSITIONS + 1)))}
        out["9b_chain_listing_informational"] = {"pass": None, "complete_after_seconds": s9["listing"]["complete_after_seconds"], "ahead_of_exact": s9["listing"]["ahead_of_exact"],
                                                 "names_final": s9["listing"]["names_final"]}
    sections = {"marker_integrity": ["1_distinct_race", "2_identical_race", "3_readd", "9_chain_race"], "exact_path_visibility": ["6a_exact_path_metadata_immediate", "7_range", "8_cross_process_visibility"],
                "listing_consistency": ["6b_listing_visibility_informational", "9b_chain_listing_informational"]}
    return {"criteria": CRITERIA_V2, "steps": out, "pass_local": all(v["pass"] for v in out.values() if v["pass"] is not None), "sections": sections,
            "note": "local to one process; cross-process rules and the overall readiness are in the combined report; v1 verdict is unchanged and remains the headline"}


LISTING_POLL_SECONDS = 1.0       # a listing observation later than one poll interval counts as lag


def _derived_objects(*reps: dict) -> set:
    """Distinct scratch paths that provably exist after the run, derived from the two reports (no extra Dropbox call)."""
    paths = set()
    for r in reps:
        for key in ("1_distinct_race", "2_identical_race"):
            for rd in r["steps"][key]["rounds"]:
                if rd["outcomes"].get("WON", 0) or rd["outcomes"].get("EXISTS_SAME", 0) or rd["outcomes"].get("EXISTS_DIFFERENT", 0):
                    paths.add(rd["path"])
        s3 = r["steps"]["3_readd"]
        if (s3.get("hashes") or {}).get("after_first"):
            paths.add(f"{r['folder']}/readd/one.json")
        for o in r["steps"]["6_listing_visibility"]["objects"]:
            if o["created"] in ("WON", "EXISTS_SAME", "EXISTS_DIFFERENT"):
                paths.add(o["path"])
        if r["steps"]["7_range"].get("stored") in ("stored", "present_same"):
            paths.add(f"{r['folder']}/range/blob.bin")
        v2 = r.get("steps_v2") or {}
        for o in (v2.get("8_cross_process_visibility") or {}).get("own_objects", []):
            if o["outcome"] == "WON":
                paths.add(o["path"])
        for x in (v2.get("9_chain_race") or {}).get("stored", []):
            if x["present"]:
                paths.add(f"{r['folder']}/chain/C{x['n']:06d}.json")
    return paths


MARKER_NAME = re.compile(r"^c\d{6}\.json$")


def combine_reports(ra: dict, rb: dict, *, reinterpret_listing_entries: bool = False) -> dict:
    """Offline, read-only: the cross-process verdict from the two evidence files of a Phase B run. Never contacts Dropbox.
    Four SEPARATE sections: marker integrity, exact-path (metadata + download) visibility, listing consistency and overall readiness. A delayed or invisible listing is never folded into a
    PASS: it yields ``READY_WITH_LISTING_LAG`` (named, and ``combined_pass_v2`` stays false); only a run with no listing lag at all is plain ``READY``."""
    checks, detail = {}, {}
    tags = {ra["process_tag"], rb["process_tag"]}
    checks["same_run_parameters"] = bool(ra["folder"] == rb["folder"] and ra["rounds"] == rb["rounds"] and ra["threads"] == rb["threads"] and tags == set(VISIBILITY_TAGS))
    ba = {r["path"]: r for r in ra["steps"]["1_distinct_race"]["rounds"]}
    bb = {r["path"]: r for r in rb["steps"]["1_distinct_race"]["rounds"]}
    bad1 = [p for p in ba if p not in bb or ba[p]["outcomes"].get("WON", 0) + bb[p]["outcomes"].get("WON", 0) != 1 or ba[p]["revisions"] != 1 or bb[p]["revisions"] != 1
            or any(k.startswith("UNKNOWN") for k in list(ba[p]["outcomes"]) + list(bb[p]["outcomes"]))]
    checks["1_distinct_exactly_one_winner_per_path_across_processes"] = bool(ba) and set(ba) == set(bb) and not bad1
    detail["1_failures"] = bad1
    checks["2_identical_idempotent_both_processes"] = bool(ra["v2"]["steps"]["2_identical_race"]["pass"] and rb["v2"]["steps"]["2_identical_race"]["pass"])
    a3, b3 = ra["steps"]["3_readd"], rb["steps"]["3_readd"]
    ha, hb = a3.get("hashes") or {}, b3.get("hashes") or {}
    final = {ha.get("after_different"), hb.get("after_different")}
    checks["3_readd_one_content_survives_and_different_bytes_refused"] = bool(
        len(final) == 1 and None not in final and final <= {ha.get("own_payload_hash"), hb.get("own_payload_hash")}
        and {a3["first"], b3["first"]} <= {"WON", "EXISTS_DIFFERENT", "EXISTS_SAME"} and [a3["first"], b3["first"]].count("WON") <= 1
        and a3["different"] == "EXISTS_DIFFERENT" and b3["different"] == "EXISTS_DIFFERENT" and a3["revisions"] == 1 and b3["revisions"] == 1)
    ca, cb = ra["steps_v2"]["9_chain_race"], rb["steps_v2"]["9_chain_race"]
    union = sorted(ca["claimed"] + cb["claimed"])
    checks["9_claims_disjoint_and_complete"] = bool(set(ca["claimed"]).isdisjoint(cb["claimed"]) and union == list(range(1, CHAIN_POSITIONS + 1)))
    checks["9_stored_writer_matches_claimant"] = bool(
        all((x["writer"] == ra["process_tag"]) == (x["n"] in ca["claimed"]) for x in ca["stored"] if x["present"])
        and all((x["writer"] == rb["process_tag"]) == (x["n"] in cb["claimed"]) for x in cb["stored"] if x["present"])
        and [x.get("writer") for x in ca["stored"]] == [x.get("writer") for x in cb["stored"]])
    checks["9_both_exact_views_identical"] = bool([x.get("sha256") for x in ca["stored"]] == [x.get("sha256") for x in cb["stored"]])
    checks["9_all_five_parent_links_valid_in_both_views"] = bool(ca["parent_links_ok"] and cb["parent_links_ok"] and all(x["parent_ok"] for x in ca["stored"] + cb["stored"]))
    checks["9_every_position_exactly_one_revision_and_matching_dropbox_hash"] = bool(len(ca["stored"]) == CHAIN_POSITIONS == len(cb["stored"])
                                                                                      and all(x["present"] and x["revisions"] == 1 and x["dropbox_hash_ok"] for x in ca["stored"] + cb["stored"]))
    checks["9_no_false_commits_no_anomalies_not_late"] = bool(not ca["false_commit_claims"] and not cb["false_commit_claims"] and not ca["anomalies"] and not cb["anomalies"]
                                                              and not ca["late_start"] and not cb["late_start"])
    detail["9_positions"] = [{"n": x["n"], "writer_identity": x.get("writer"), "claimed_by": ra["process_tag"] if x["n"] in ca["claimed"] else (rb["process_tag"] if x["n"] in cb["claimed"] else None),
                              "parent_ok": x["parent_ok"], "revisions": x.get("revisions"), "dropbox_hash_ok": x.get("dropbox_hash_ok")} for x in ca["stored"]]
    integrity_keys = [k for k in checks if k.startswith(("1_", "2_", "3_", "9_"))] + ["same_run_parameters"]
    local = {t: r["v2"]["steps"] for t, r in ((ra["process_tag"], ra), (rb["process_tag"], rb))}
    checks["local_marker_integrity_steps_pass_both"] = all(local[t][k]["pass"] for t in local for k in ("1_distinct_race", "2_identical_race", "3_readd"))
    integrity_keys.append("local_marker_integrity_steps_pass_both")
    # exact-path visibility: metadata AND download, correct bytes AND Dropbox hash, within the 60 s window, on schedule
    lat, exact_rows = [], []
    for creator, watcher in ((ra, rb), (rb, ra)):
        mine = {o["path"]: o for o in creator["steps_v2"]["8_cross_process_visibility"]["own_objects"]}
        for p in watcher["steps_v2"]["8_cross_process_visibility"]["peer_objects"]:
            o = mine.get(p["path"])
            row = {"path": p["path"], "creator": creator["process_tag"], "watcher": watcher["process_tag"], "created_returned_utc": o and o["returned_utc"]}
            for kind in ("metadata", "download", "listing"):
                seen = p[f"first_{kind}_utc"]
                row[f"{kind}_latency_s"] = None if not (seen and o) else round((_pu(seen) - _pu(o["returned_utc"])).total_seconds(), 3)
            lat.append(row)
            exact_rows.append({"path": p["path"], "metadata_visible": bool(p["first_metadata_utc"]), "download_visible": bool(p["first_download_utc"]), "bytes_equal": p["download_bytes_equal"],
                               "dropbox_hash_equal": p["download_hash_equal"]})
    checks["8_every_peer_object_visible_by_metadata_and_download"] = bool(lat) and all(r["metadata_latency_s"] is not None and r["download_latency_s"] is not None for r in lat)
    checks["8_every_downloaded_peer_object_has_correct_bytes_and_dropbox_hash"] = bool(exact_rows) and all(r["bytes_equal"] is True and r["dropbox_hash_equal"] is True for r in exact_rows)
    checks["8_own_objects_created_once_and_step_on_schedule"] = bool(all(o["outcome"] == "WON" and o["revisions"] == 1 for r in (ra, rb) for o in r["steps_v2"]["8_cross_process_visibility"]["own_objects"])
                                                                    and not ra["steps_v2"]["8_cross_process_visibility"]["late_start"] and not rb["steps_v2"]["8_cross_process_visibility"]["late_start"])
    checks["6a_exact_path_metadata_immediate_both"] = bool(ra["v2"]["steps"]["6a_exact_path_metadata_immediate"]["pass"] and rb["v2"]["steps"]["6a_exact_path_metadata_immediate"]["pass"])
    exact_keys = ["8_every_peer_object_visible_by_metadata_and_download", "8_every_downloaded_peer_object_has_correct_bytes_and_dropbox_hash", "8_own_objects_created_once_and_step_on_schedule",
                  "6a_exact_path_metadata_immediate_both"]
    detail["8_latencies"], detail["8_exact_path_rows"] = lat, exact_rows
    # listing consistency: measured and reported on its own
    vis6 = [o["visible_after_polls"] for r in (ra, rb) for o in r["steps"]["6_listing_visibility"]["objects"]]
    ahead = sorted(set(ca["listing"]["ahead_of_exact"]) | set(cb["listing"]["ahead_of_exact"]))
    non_marker = []
    if reinterpret_listing_entries:        # RE-INTERPRETATION of recorded evidence (see ``_list_names``): names that are not marker names are not "listed markers"; they are kept and reported
        non_marker = [n for n in ahead if not MARKER_NAME.match(n)]
        ahead = [n for n in ahead if MARKER_NAME.match(n)]
    lat_l = [r["listing_latency_s"] for r in lat]
    s9c = [ca["listing"]["complete_after_seconds"], cb["listing"]["complete_after_seconds"]]
    if ahead:
        lstatus = "INCONSISTENT"
    elif any(x is None for x in lat_l) or any(x is None for x in s9c):
        lstatus = "NOT_VISIBLE_WITHIN_60S"
    elif any(x > LISTING_POLL_SECONDS for x in lat_l) or any(x > LISTING_POLL_SECONDS for x in s9c) or any(v is None or v > 1 for v in vis6):
        lstatus = "LAG_OBSERVED"
    else:
        lstatus = "NO_LAG_OBSERVED"
    listing = {"status": lstatus, "step8_listing_latency_s": lat_l, "step6_visible_after_polls": vis6, "step9_listing_complete_after_seconds": s9c, "ahead_of_exact": ahead,
               "max_step8_latency_s": max([x for x in lat_l if x is not None], default=None), "interpretation": "informational for safety (the commit listing is completed by exact-path probes) but never hidden in the verdict"}
    # traffic (attempts and retries are reported separately from stored objects) and the aggregate bound
    tr = [ra.get("traffic") or {}, rb.get("traffic") or {}]
    checks["traffic_clean"] = bool(all(tr) and all(not t.get("unexpected_errors") and not t.get("refused_calls") and not t.get("paths_outside_scope") for t in tr))
    totals = {"upload_attempts": sum(t.get("upload_attempts", 0) for t in tr), "upload_bytes_attempted": sum(t.get("upload_bytes_attempted", 0) for t in tr),
              "api_calls_total": sum(t.get("api_calls_total", 0) for t in tr), "destination_retries": {k: sum((t.get("destination_retries") or {}).get(k, 0) for t in tr) for k in ("retries", "rate_limits")},
              "transient_errors": {k: sum((t.get("transient_errors") or {}).get(k, 0) for t in tr) for k in sorted({k for t in tr for k in (t.get("transient_errors") or {})})},
              "expected_conflict_errors": sum(v for t in tr for k, v in (t.get("errors") or {}).items() if k.endswith("path_conflict"))}
    objs = _derived_objects(ra, rb)
    cfg = ScratchConfig(ra["folder"], ra["rounds"], ra["threads"], ra["process_tag"], "2026-01-01T00:00:00Z", rb["process_tag"], True)
    stored_bytes_ub = (len(objs) - 1) * PAYLOAD_BYTES + 64 * 1024
    checks["stored_objects_within_the_aggregate_bound"] = bool(len(objs) <= aggregate_max_objects(cfg) and stored_bytes_ub <= aggregate_max_stored_bytes(cfg))
    detail["stored"] = {"objects_derived_from_reports": len(objs), "aggregate_max_objects": aggregate_max_objects(cfg), "stored_bytes_upper_bound": stored_bytes_ub,
                        "aggregate_max_stored_bytes": aggregate_max_stored_bytes(cfg), "network_traffic_reported_separately": totals}
    integrity_pass, exact_pass = all(checks[k] for k in integrity_keys), all(checks[k] for k in exact_keys)
    traffic_pass = checks["traffic_clean"] and checks["stored_objects_within_the_aggregate_bound"]
    if not (integrity_pass and exact_pass and traffic_pass) or lstatus == "INCONSISTENT":
        readiness = "NOT_READY"
    else:
        readiness = "READY" if lstatus == "NO_LAG_OBSERVED" else "READY_WITH_LISTING_LAG"
    if reinterpret_listing_entries:
        listing["non_marker_entries_ignored"] = non_marker
    sections = {"marker_integrity": {"status": "PASS" if integrity_pass else "FAIL", "checks": {k: checks[k] for k in integrity_keys}},
                "exact_path_visibility": {"status": "PASS" if exact_pass else "FAIL", "checks": {k: checks[k] for k in exact_keys}},
                "listing_consistency": listing,
                "traffic_and_objects": {"status": "PASS" if traffic_pass else "FAIL"},
                "overall_readiness": readiness}
    reinterpretation = ({"id": "marker_scratch/2.1-listing-entry-filter", "reason": "the harness counted folder entries as listed markers (defect fixed 2026-10-16); entries whose name is not c<6 digits>.json are not counted as markers",
                         "non_marker_entries_ignored": non_marker, "historical_reports_unchanged": True} if reinterpret_listing_entries else None)
    return {"criteria": CRITERIA_V2, "reinterpretation": reinterpretation, "sections": sections, "overall_readiness": readiness, "combined_pass_v2": readiness == "READY", "integrity_and_exact_pass": bool(integrity_pass and exact_pass and traffic_pass),
            "checks": checks, "detail": detail,
            "v1_headline_unchanged": {r["process_tag"]: {"pass_automated": r["pass_automated"], "verdict": r["verdict"], "report_sha256": r["report_sha256"]} for r in (ra, rb)},
            "kill_mid_request": "NOT_RUN", "marker_semantics_verified": False,
            "verdict": (f"V2: marker integrity {sections['marker_integrity']['status']}; exact-path visibility {sections['exact_path_visibility']['status']}; listing {lstatus}; "
                        f"overall {readiness}. The owner must review the evidence; marker_semantics_verified stays false and the kill test is NOT RUN")}
