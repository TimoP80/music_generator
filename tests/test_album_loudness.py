"""tests.test_album_loudness — measurement + loudness policy (§9–§14).

Run:  python tests/test_album_loudness.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.loudness import (analyze_file, format_loudness_report,
                                   measure_track)
from timbor.album.mastering import (apply_gain, compute_gains, limit,
                                    peak_report, policy_from)

FAILED = []
SR = 44100


def _sig(seconds=8.0, amp=0.5, freq=1000.0):
    t = np.arange(int(seconds * SR)) / SR
    x = amp * np.sin(2 * np.pi * freq * t)
    return x, x.copy()


def test_measurements_deterministic_and_sane():
    l, r = _sig()
    m1 = measure_track(l, r, SR)
    m2 = measure_track(l.copy(), r.copy(), SR)
    assert m1 == m2, "measurement not deterministic"
    assert m1["peak_dbfs"] == -6.02
    # dual-mono sine: book LUFS -6.71 + ~+0.45 dB K-curve at 1 kHz
    assert -7.7 < m1["integrated_lufs_approx"] < -5.7
    assert m1["loudness_range_lu"] == 0.0
    assert m1["duration_seconds"] == 8.0
    assert m1["silence_lead_seconds"] == 0.0
    print("  deterministic, peak/LUFS/LRA on known signal: OK")


def test_rms_never_labeled_lufs():
    l, r = _sig()
    m = measure_track(l, r, SR)
    # RMS is reported as dBFS; only loudness numbers carry the LUFS label
    assert "rms_dbfs" in m
    assert not any("rms" in k and "lufs" in k for k in m), sorted(m)
    assert m["method"].startswith("itu_bs1770_style")
    # quiet noise floor: integrated gate should reject, peak still measured
    l2, r2 = _sig(amp=0.0005)
    m2 = measure_track(l2, r2, SR)
    assert m2["integrated_lufs_approx"] is None or \
        m2["integrated_lufs_approx"] < -60, m2["integrated_lufs_approx"]
    print("  gate behavior + RMS/LUFS separation: OK")


def test_report_table_deterministic():
    l, r = _sig()
    m = measure_track(l, r, SR)
    m.update({"position": 1, "title": "T", "genre": "g", "bpm": 170})
    doc = {"method": m["method"], "method_note": "x", "tracks": [m]}
    t1 = format_loudness_report(doc)
    t2 = format_loudness_report(doc)
    assert t1 == t2 and "Track" in t1 and "01 T" in t1
    print("  report table deterministic: OK")


def _album_fixture(tmp_root):
    """Minimal in-memory album doc pointing at synthetic WAV masters."""
    import json
    import wave
    tracks = []
    for i, amp in enumerate((0.5, 0.25, 0.35, 0.4)):
        d = os.path.join(tmp_root, f"{i+1:02d}", "audio")
        os.makedirs(d, exist_ok=True)
        l, r = _sig(seconds=6.0, amp=amp)
        data = np.empty((len(l), 2))
        data[:, 0], data[:, 1] = l, r
        pcm = (data * 32767).astype("<i2")
        p = os.path.join(d, "master.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        tracks.append({"number": f"{i+1:02d}", "title": f"T{i+1}",
                       "directory": f"{i+1:02d}", "project": f"{i+1:02d}/project.json",
                       "genre": "gabber", "bpm": 170.0, "seed": i})
    return {"name": "L", "tracks": tracks,
            "dna": {"root_key": "D", "mode": "minor"}}


def test_modes_preserve_match_target(tmpdir=None):
    import tempfile
    tmp = tempfile.mkdtemp(prefix="loud_pol_")
    album = _album_fixture(tmp)
    root = tmp
    # preserve: zero gains everywhere (the §36 default)
    pol = policy_from(album)
    assert pol["mode"] == "preserve" and pol["limiter_enabled"] is False
    g = compute_gains(album, root, pol)
    assert all(t["gain_db"] == 0.0 for t in g["tracks"])
    # target: every track moves to -14 LUFS within clamp rules
    album_t = dict(album)
    album_t["loudness"] = {"mode": "target", "target_lufs": -14.0}
    gt = compute_gains(album_t, root, policy_from(album_t))
    for t in gt["tracks"]:
        assert t["gain_db"] != 0.0
        # measured LUFS + gain ≈ target (gain = target - measured by design)
        assert abs((t["integrated_lufs_approx"] + t["gain_db"]) - (-14.0)) < 0.01
    # boost clamp: an absurdly quiet library cannot be boosted past MAX_BOOST
    album_q = dict(album)
    album_q["loudness"] = {"mode": "target", "target_lufs": 0.0}
    gq = compute_gains(album_q, root, policy_from(album_q))
    assert all(t["gain_db"] <= 3.0 + 1e-9 for t in gq["tracks"])
    # match: anchors on the loudest track → the anchor stays at 0 dB,
    # tracks within match_window (3 dB) also stay untouched, quieter ones
    # are lifted toward the anchor (boosts capped at MAX_BOOST_DB)
    album_m = dict(album)
    album_m["loudness"] = {"mode": "match"}
    gm = compute_gains(album_m, root, policy_from(album_m))
    gains = [t["gain_db"] for t in gm["tracks"]]
    anchor_gain = gains[int(np.argmax([t["integrated_lufs_approx"]
                                       for t in gm["tracks"]]))]
    assert anchor_gain == 0.0, "anchor (loudest) track must stay at 0 dB"
    assert all(g <= 3.0 + 1e-9 for g in gains)
    print("  preserve/match/target policies + clamps: OK")


def test_gain_and_limiter_deterministic():
    l, r = _sig(amp=1.2)          # above full scale → limiter must engage
    g1, r1 = limit(l.copy(), r.copy(), SR, ceiling_db=-1.0)
    g2, r2 = limit(l.copy(), r.copy(), SR, ceiling_db=-1.0)
    assert np.array_equal(g1, g2), "limiter not deterministic"
    pr = peak_report(g1, r1)
    assert pr["peak_dbfs"] <= -0.95, pr
    assert pr["clipped_samples"] == 0
    # untouched signal passes through apply_gain exactly
    a, b = apply_gain(l, r, 0.0)
    assert np.array_equal(a, l) and np.array_equal(b, r)
    a, b = apply_gain(l, r, -6.0)
    assert np.allclose(a, l * 10 ** (-6.0 / 20.0))
    print("  limiter holds ceiling deterministically; gains exact: OK")


def test_clipping_detection():
    hot = np.ones(SR) * 1.5       # hard-clipped material
    pr = peak_report(hot, hot * 0.5)
    assert pr["clipped_samples"] == SR, "clipped samples must be counted"
    print("  clipping detection: OK")


def test_analyze_file_roundtrip(tmpdir=None):
    import tempfile
    import wave
    tmp = tempfile.mkdtemp(prefix="loud_file_")
    l, r = _sig(seconds=5.0, amp=0.4)
    data = np.empty((len(l), 2))
    data[:, 0], data[:, 1] = l, r
    p = os.path.join(tmp, "x.wav")
    with wave.open(p, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(((np.stack([l, r], 1)) * 32767).astype("<i2").tobytes())
    m = analyze_file(p)
    assert m["duration_seconds"] == 5.0 and m["peak_dbfs"] < -7.0
    print("  analyze_file on int16 WAV: OK")


def main() -> int:
    print("test_album_loudness")
    for name, fn in sorted((k, v) for k, v in globals().items()
                           if k.startswith("test_") and callable(v)):
        try:
            fn()
        except AssertionError as e:
            FAILED.append(name)
            print(f"  {name}: FAILED — {e}")
        except Exception as e:  # noqa: BLE001
            FAILED.append(name)
            print(f"  {name}: ERROR — {type(e).__name__}: {e}")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("All album loudness tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
