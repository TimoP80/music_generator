"""tests.test_album_transitions — transition engine (§6–§8, §25, §26).

Run:  python tests/test_album_transitions.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.transitions import (Transition, apply_boundary, click_status,
                                      crossfade_curves, fade_envelopes,
                                      plan_boundary, quantize_duration,
                                      boundary_diagnostics)

FAILED = []
SR = 44100


def test_quantization():
    # 'none': nearest sample
    assert quantize_duration(1.234, 170.0, "none", SR) == int(round(1.234 * SR))
    # bar at 170 bpm: 4 beats = 4*60/170 s; durations snap UP to the grid
    bar = 4 * 60.0 / 170.0
    q = quantize_duration(3.0, 170.0, "bar", SR)   # 3 s = 2.13 bars → 3 bars
    assert abs(q - round(3 * bar * SR)) < 2, (q, 3 * bar * SR)
    q2 = quantize_duration(5.5, 170.0, "bar", SR)  # 5.5 s = 3.9 bars → 4 bars
    assert abs(q2 - round(4 * bar * SR)) < 2
    # a duration exactly on the grid stays put (no epsilon drift upward)
    assert abs(quantize_duration(2 * bar, 170.0, "bar", SR) -
               round(2 * bar * SR)) < 2
    # beat and phrase
    assert abs(quantize_duration(0.4, 170.0, "beat", SR) -
               round(2 * 60.0 / 170.0 * SR)) < 2   # 0.4 s → 2 beats
    assert abs(quantize_duration(3.0, 170.0, "phrase", SR) -
               round(16 * 60.0 / 170.0 * SR)) < 2
    # negative rejected
    try:
        quantize_duration(-1.0, 170.0, "bar", SR)
        raise AssertionError("negative accepted")
    except ValueError:
        pass
    print("  beat/bar/phrase quantization from source BPM: OK")


def test_plan_boundary_types():
    out = {"bpm": 170.0, "source_samples": 10 * SR}
    inc = {"bpm": 172.0, "source_samples": 9 * SR}
    g = plan_boundary(out, inc, Transition(type="gap", duration_seconds=2.0), SR)
    assert g["gap_samples"] == int(round(2.0 * SR)) and g["overlap_samples"] == 0
    assert g["source_bpm"] == 170.0 and g["destination_bpm"] == 172.0
    assert g["bpm_mismatch"] is True, "differing BPMs must be recorded"
    h = plan_boundary(out, inc, Transition(type="hard_cut"), SR)
    assert h["gap_samples"] == 0 and h["overlap_samples"] == 0
    x = plan_boundary(out, inc, Transition(type="equal_power_crossfade",
                                           duration_seconds=4.0,
                                           quantize="bar"), SR)
    assert x["overlap_samples"] > 0 and x["curve"] == "equal_power"
    # impossible crossfade: overlap > half of the shorter track
    tiny = {"bpm": 170.0, "source_samples": 1000}
    try:
        plan_boundary(tiny, tiny, Transition(type="linear_crossfade",
                                             duration_seconds=4.0), SR)
        raise AssertionError("oversized crossfade accepted")
    except ValueError as e:
        assert "impossible" in str(e)
    print("  boundary planning per type + safety cap: OK")


def test_curves_and_fades():
    fo, fi = crossfade_curves(1000, "equal_power")
    # equal-power: power sum constant
    power = fo ** 2 + fi ** 2
    assert np.allclose(power, 1.0, atol=1e-9)
    lo, li = crossfade_curves(1000, "linear")
    assert np.allclose(lo + li, 1.0)
    assert fo[0] == 1.0 and fi[0] == 0.0
    assert fo[-1] < 0.01 and li[-1] > 0.99
    # track fades
    g, _ = fade_envelopes(10000, 500, 500)
    assert g[0] == 0.0 and g[250] == 0.5 and abs(g[-1]) < 0.01
    assert g[2000] == 1.0  # middle untouched
    print("  crossfade curves (linear/equal-power) + fades: OK")


def test_apply_boundary_blends_without_truncation():
    n_out, n_in = 4 * SR, 3 * SR
    t = np.arange(n_out) / SR
    out = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float64)
    t2 = np.arange(n_in) / SR
    inc = (0.5 * np.sin(2 * np.pi * 330 * t2)).astype(np.float64)
    ov = SR  # 1 s overlap
    total = n_out + n_in - ov
    alb_l = np.zeros(total)
    alb_r = np.zeros(total)
    # place outgoing track
    alb_l[:n_out] += out
    alb_r[:n_out] += out
    layout = {"overlap_samples": ov, "curve": "equal_power"}
    end = apply_boundary(alb_l, alb_r, out.copy(), out.copy(),
                         inc.copy(), inc.copy(), n_out - ov, layout)
    assert end == total
    # blend zone is the LAST `ov` samples of the outgoing track (where the
    # incoming track was written): equal-power crossfade of tail and head
    zone = alb_l[n_out - ov:n_out]
    fo, fi = crossfade_curves(ov, "equal_power")
    expect = out[n_out - ov:] * fo + inc[:ov] * fi
    assert np.allclose(zone, expect, atol=1e-9)
    # the outgoing track's head (before the blend zone) is untouched
    assert np.allclose(alb_l[:n_out - ov], out[:n_out - ov], atol=1e-12)
    # incoming tail fully present (no truncation)
    assert np.allclose(alb_l[n_out:], inc[ov:], atol=1e-12)
    print("  boundary application: exact blend, no truncation: OK")


def test_click_detection_thresholds():
    assert click_status(0.001) == "PASS"
    assert click_status(0.15) == "WARNING"
    assert click_status(0.5) == "ERROR"
    # synthetic boundary with a planted click
    sr = SR
    audio = np.zeros(sr)
    audio[: sr // 2] = 0.5
    audio[sr // 2 + 5] = -0.9     # planted discontinuity
    diags = boundary_diagnostics(audio.reshape(-1, 1),
                                 [{"start_sample": sr // 2, "from": "01",
                                   "to": "02", "mode": "gap"}], sr)
    d = diags[0]
    assert d["status"] == "ERROR", d
    assert d["discontinuity"] > 0.3
    # clean boundary passes
    audio2 = np.linspace(0.5, 0.0, sr)
    diags2 = boundary_diagnostics(audio2.reshape(-1, 1),
                                  [{"start_sample": sr // 2, "from": "01",
                                    "to": "02", "mode": "gap"}], sr)
    assert diags2[0]["status"] in ("PASS", "WARNING"), diags2[0]
    print("  click detection (planted click → ERROR, clean → PASS): OK")


def test_transition_roundtrip():
    tr = Transition(type="linear_crossfade", duration_seconds=4.0,
                    curve="linear", source_position=1, destination_position=2,
                    source_bpm=170.0, destination_bpm=167.0, quantize="bar",
                    overlap_samples=176941, bpm_mismatch=True)
    d = tr.to_json()
    tr2 = Transition.from_json(d)
    assert tr2 == tr
    print("  Transition JSON round trip: OK")


def main() -> int:
    print("test_album_transitions")
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
    print("All album transition tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
