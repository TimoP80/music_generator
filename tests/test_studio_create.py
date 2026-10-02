"""Tests for Studio's safe, standalone music-creation job submission."""
from __future__ import annotations

import collections
import os
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import studio.server as studio
from studio.creation_store import CreationStore

_TEST_DATA = tempfile.TemporaryDirectory()


class ThreadProbe:
    """Capture submitted workers so tests never run a generator process."""
    calls = []

    def __init__(self, target, args, daemon, name):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.name = name
        self.calls.append(self)

    def start(self):
        pass


def runner_without_worker():
    studio.creation_store = CreationStore(os.path.join(_TEST_DATA.name, f"creation-{len(ThreadProbe.calls)}.db"))
    runner = object.__new__(studio.JobRunner)
    runner._lock = threading.Lock()
    runner._queue = collections.deque()
    runner._current = None
    runner._counter = 0
    runner.create_jobs = {}
    runner.logs = collections.deque(maxlen=100)
    runner._log_seq = 0
    return runner


def test_procedural_submission_builds_safe_cli():
    old_thread = studio.threading.Thread
    ThreadProbe.calls.clear()
    studio.threading.Thread = ThreadProbe
    try:
        runner = runner_without_worker()
        job = runner.submit_creation({
            "prompt": "dark warehouse rave", "genre": "gabber", "bpm": "180",
            "key": "F minor", "seed": "42", "bars": 16,
            "sample_mode": "balanced", "engine": "procedural",
        })
    finally:
        studio.threading.Thread = old_thread

    assert job["kind"] == "create_track" and job["status"] == "queued"
    assert "output" not in job and job["output_rel"].startswith("projects/created/")
    command = ThreadProbe.calls[0].args[1]
    assert command[0] == sys.executable
    assert "--stems" in command and "--stable-audio" not in command
    assert command[command.index("--genre") + 1] == "gabber"
    assert command[command.index("--bpm") + 1] == "180.0"
    assert command[command.index("--key") + 1] == "F minor"
    assert command[command.index("--seed") + 1] == "42"
    assert command[command.index("--bars") + 1] == "16"


def test_batch_submission_persists_unique_takes_and_varies_seeds():
    old_thread = studio.threading.Thread
    ThreadProbe.calls.clear()
    studio.threading.Thread = ThreadProbe
    try:
        runner = runner_without_worker()
        result = runner.submit_creation({"prompt": "warehouse rave", "takes": 4,
                                         "seed": 10482, "seed_mode": "vary"})
        persisted = studio.creation_store.get_creation(result["creation_id"])
    finally:
        studio.threading.Thread = old_thread
    assert result["take_count"] == 4
    assert len({take["id"] for take in result["takes"]}) == 4
    assert [take["seed"] for take in result["takes"]] == [10482, 10483, 10484, 10485]
    assert len(persisted["takes"]) == 4
    assert ThreadProbe.calls[0].target == runner._run_creation_batch
    assert len(ThreadProbe.calls) == 1


def test_same_seed_controlled_repeat_and_batch_limits():
    old_thread = studio.threading.Thread
    ThreadProbe.calls.clear()
    studio.threading.Thread = ThreadProbe
    try:
        runner = runner_without_worker()
        result = runner.submit_creation({"prompt": "warehouse rave", "takes": 2,
                                         "seed": 42, "seed_mode": "same"})
        assert [take["seed"] for take in result["takes"]] == [42, 42]
        try:
            runner.submit_creation({"prompt": "warehouse rave", "takes": 3})
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported batch size accepted")
    finally:
        studio.threading.Thread = old_thread


def test_remote_engine_submissions_build_isolated_cli_and_enforce_single_take():
    old_thread = studio.threading.Thread
    ThreadProbe.calls.clear()
    studio.threading.Thread = ThreadProbe
    try:
        cases = [
            ("yue2", {"YUE2_ENABLED": "true", "YUE2_MODAL_URL": "https://yue.modal.run",
                      "YUE2_API_KEY": "local-test", "YUE2_DEFAULT_DURATION": "180"}, 180.0),
            ("acestep", {"ACESTEP_ENABLED": "true", "ACESTEP_MODAL_URL": "https://ace.modal.run",
                          "ACESTEP_API_KEY": "local-test", "ACESTEP_DEFAULT_DURATION": "45"}, 45.0),
        ]
        for engine, provider_env, expected_duration in cases:
            ThreadProbe.calls.clear()
            with patch.dict(os.environ, provider_env):
                runner = runner_without_worker()
                result = runner.submit_creation({
                    "prompt": "dark warehouse rave", "engine": engine,
                    "genre": "gabber", "bpm": 180, "key": "F minor",
                    "era": "1994 Rotterdam", "lyrics": "[Chorus] Rave!",
                })
                command = ThreadProbe.calls[0].args[1]
                assert result["engine"] == engine
                assert command[command.index("--engine") + 1] == engine
                assert float(command[command.index("--duration") + 1]) == expected_duration
                assert command[command.index("--lyrics") + 1] == "[Chorus] Rave!"
                assert command[command.index("--era") + 1] == "1994 Rotterdam"
                assert "--stems" not in command and "--sample-dir" not in command
                try:
                    runner.submit_creation({"prompt": "warehouse rave", "engine": engine,
                                            "takes": 2})
                except ValueError as exc:
                    assert "one take" in str(exc)
                else:
                    raise AssertionError(f"{engine} accepted a multi-take request")
    finally:
        studio.threading.Thread = old_thread


def test_invalid_creation_requests_are_rejected_before_worker_launch():
    runner = runner_without_worker()
    for request in (
        {"prompt": "hi"},
        {"prompt": "warehouse rave", "bpm": "nan"},
        {"prompt": "warehouse rave", "seed": "4294967296"},
        {"prompt": "warehouse rave", "bars": 129},
        {"prompt": "warehouse rave", "genre": "made-up"},
    ):
        try:
            runner.submit_creation(request)
        except ValueError:
            continue
        raise AssertionError(f"invalid creation request was accepted: {request}")


def main() -> int:
    tests = [test_procedural_submission_builds_safe_cli,
             test_remote_engine_submissions_build_isolated_cli_and_enforce_single_take,
             test_batch_submission_persists_unique_takes_and_varies_seeds,
             test_same_seed_controlled_repeat_and_batch_limits,
             test_invalid_creation_requests_are_rejected_before_worker_launch]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
