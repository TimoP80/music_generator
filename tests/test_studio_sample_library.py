"""Tests for the Studio adapter over TIMBOR's real sample-index pipeline."""
from __future__ import annotations

import base64
import json
import math
import os
import struct
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio.sample_library import SampleLibraryService


def write_tone(path: str, seconds: float = 0.5, rate: int = 22050,
               frequency: float = 220.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = bytearray()
    for i in range(int(seconds * rate)):
        sample = int(10000 * math.sin(2 * math.pi * frequency * i / rate))
        frames.extend(struct.pack("<h", sample))
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


def wait_scan(service: SampleLibraryService) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        status = service.scan_status()
        if status["state"] not in ("SCANNING", "ANALYZING"):
            return status
        time.sleep(0.05)
    raise AssertionError("scan timed out")


def expect_value_error(fn, message: str) -> None:
    try:
        fn()
    except ValueError:
        return
    raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="timbor_studio_lib_") as tmp:
        root = os.path.join(tmp, "library")
        amen = os.path.join(root, "breaks", "amen_break_174.wav")
        copy = os.path.join(root, "breaks", "amen_copy.wav")
        write_tone(amen, frequency=174)
        # Identical audio at a second path exercises deterministic duplicate detection.
        import shutil
        shutil.copyfile(amen, copy)
        svc = SampleLibraryService(tmp, os.path.join(tmp, "samples.db"),
                                   os.path.join(tmp, "roots.json"))
        added = svc.add_root(root)
        assert svc.add_root(root)["id"] == added["id"]
        assert len(svc.roots()) == 1

        status = svc.start_scan(added["id"])
        assert status["state"] == "SCANNING"
        done = wait_scan(svc)
        assert done["state"] == "COMPLETE", done
        assert done["discovered"] == 2 and done["analyzed"] == 2, done

        result = svc.search({"q": "amen", "page_size": "1", "sort": "name"})
        assert result["total"] == 2 and len(result["results"]) == 1
        first = result["results"][0]
        assert first["filename"] == "amen_break_174.wav"
        assert first["status"] == "ANALYZED"
        detail = svc.detail(first["id"])
        assert detail["duration"] > 0 and detail["sample_rate"] == 22050
        assert svc.resolve_id(first["id"])[0] == os.path.realpath(amen)
        assert svc.search({"q": "amen", "page": "2", "page_size": "1"})["total"] == 2
        assert svc.search({"q": "amen", "page": "2", "page_size": "1"})["results"][0]["filename"] == "amen_copy.wav"
        assert svc.search({"tag": "amen", "category": "BREAKBEAT"})["total"] == 2
        all_samples = svc.search({"q": "amen"})["results"]
        indexed_bpms = [row["bpm"] for row in all_samples if row["bpm"] is not None]
        if indexed_bpms:
            lo, hi = min(indexed_bpms), max(indexed_bpms)
            assert svc.search({"bpm_min": str(lo), "bpm_max": str(hi)})["total"] >= len(indexed_bpms)
            assert svc.search({"bpm_min": str(hi + 100), "bpm_max": str(hi + 200)})["total"] == 0
        else:
            assert svc.search({"bpm_min": "0", "bpm_max": "999"})["total"] == 0
        assert svc.search({"duration_min": "0.4", "duration_max": "0.6"})["total"] == 2
        assert svc.search({"key": "f#"})["total"] == 0
        expect_value_error(lambda: svc.search({"bpm_min": "NaN"}), "NaN BPM accepted")
        expect_value_error(lambda: svc.search({"page_size": "1000"}), "oversize page accepted")
        expect_value_error(lambda: svc.search({"sort": "filename desc"}), "arbitrary sort accepted")

        # Initial indexing stores hashes, so a second untouched scan is fully incremental.
        svc.start_scan(added["id"])
        done = wait_scan(svc)
        assert done["changed"] == 0 and done["unchanged"] == 2, done
        stats = svc.root_stats(root)
        assert stats["duplicate_groups"] == 1, stats
        assert stats["analyzed"] == 2 and stats["files"] == 2

        # Change/add/remove are detected and keyed only to this registered root.
        changed = amen
        write_tone(changed, seconds=0.6, frequency=330)
        os.utime(changed, (time.time() + 2, time.time() + 2))
        write_tone(os.path.join(root, "kicks", "kick.wav"), seconds=0.2)
        svc.start_scan(added["id"])
        done = wait_scan(svc)
        assert done["changed"] == 2 and done["analyzed"] == 2, done
        os.remove(copy)
        svc.start_scan(added["id"])
        done = wait_scan(svc)
        assert done["deleted"] == 1 and done["discovered"] == 2, done
        assert svc.search({"category": "kick", "page_size": "10"})["total"] == 1
        assert svc.search({"library": added["id"]})["total"] == 2

        # Opaque IDs cannot escape the registered root, even if forged.
        traversal = base64.urlsafe_b64encode(
            json.dumps([added["id"], "../outside.wav"]).encode()).decode().rstrip("=")
        expect_value_error(lambda: svc.resolve_id(traversal), "traversal ID accepted")
        expect_value_error(lambda: svc.add_root(os.path.join(tmp, "missing")),
                           "nonexistent library registered")

        # Removing a root unregisters but never deletes files, and persists as empty.
        assert svc.remove_root(added["id"])
        assert os.path.isfile(changed)
        assert svc.search({"q": "amen"})["total"] == 0
        assert svc.roots() == []
        reloaded = SampleLibraryService(tmp, os.path.join(tmp, "samples.db"),
                                        os.path.join(tmp, "roots.json"))
        assert reloaded.roots() == []
    print("Studio sample library service tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
