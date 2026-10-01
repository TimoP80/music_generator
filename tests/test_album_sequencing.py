"""tests.test_album_sequencing — sequence construction + ordering (§3–§5).

Run:  python tests/test_album_sequencing.py

Uses the shipped Machine Rave EP as the integration fixture (§35).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.sequencing import (SequenceConfig, build_sequence,
                                     ordered_tracks, validate_sequence,
                                     load_sequence, wav_info)

FAILED = []

_WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EP = os.path.join(_WS, "albums", "timbor-machine-rave-ep")


def _album():
    with open(os.path.join(_EP, "album.json"), encoding="utf-8") as f:
        return json.load(f)


def _require_ep():
    if not os.path.isfile(os.path.join(_EP, "album.json")):
        raise RuntimeError("Machine Rave EP not found — generate it first")


def test_explicit_order_never_filesystem():
    _require_ep()
    album = _album()
    tracks = ordered_tracks(album)
    assert [t["number"] for t in tracks] == ["01", "02", "03", "04"]
    # explicit override reorders without touching album.json on disk
    album2 = json.loads(json.dumps(album))
    album2["sequencing"] = {"order": ["03", "01", "04", "02"]}
    tracks2 = ordered_tracks(album2)
    assert [t["number"] for t in tracks2] == ["03", "01", "04", "02"]
    # bad orders rejected
    album3 = json.loads(json.dumps(album))
    album3["sequencing"] = {"order": ["01", "02", "03"]}
    try:
        ordered_tracks(album3)
        raise AssertionError("incomplete order accepted")
    except ValueError as e:
        assert "misses" in str(e)
    print("  explicit ordering + override + validation: OK")


def test_integer_timing_gap_mode():
    _require_ep()
    album, root = _album(), _EP
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=2.0)
    seq = build_sequence(album, root, cfg)
    sr = seq["sample_rate"]
    assert seq["total_samples"] == seq["entries"][-1]["end_sample"]
    # exact integer arithmetic: start(n) = end(n-1) + gap_samples
    for a, b in zip(seq["entries"], seq["entries"][1:]):
        assert b["start_sample"] == a["end_sample"] + int(round(2.0 * sr)), \
            "gap position not exact integer arithmetic"
        assert b["gap_before"] == 2.0
    # no fp accumulation drift: total == sum(durations) + gaps in samples
    expect = sum(e["source_samples"] for e in seq["entries"]) + \
        3 * int(round(2.0 * sr))
    assert seq["total_samples"] == expect
    assert validate_sequence(seq, cfg) == []
    print("  gap mode: exact integer positions, no drift: OK")


def test_crossfade_and_continuous_layout():
    _require_ep()
    album, root = _album(), _EP
    xf = build_sequence(album, root, SequenceConfig(
        transition_mode="crossfade", crossfade_seconds=4.0, quantize="bar"))
    for a, b in zip(xf["entries"], xf["entries"][1:]):
        assert b["start_sample"] == a["end_sample"] - b["overlap_samples"]
        assert b["overlap_samples"] > 0
        # bar quantization: the 4.0 s request snaps UP to whole bars of the
        # OUTGOING track's bpm (1 bar = 4 beats)
        bar_seconds = 4 * 60.0 / a["bpm"]
        units = math.ceil(4.0 / bar_seconds - 1e-9)
        bar_samples = round(units * bar_seconds * xf["sample_rate"])
        assert b["overlap_samples"] == bar_samples, \
            (b["overlap_samples"], bar_samples)
    assert validate_sequence(xf, SequenceConfig(
        transition_mode="crossfade", crossfade_seconds=4.0)) == []
    cont = build_sequence(album, root, SequenceConfig(
        transition_mode="continuous"))
    for a, b in zip(cont["entries"], cont["entries"][1:]):
        assert b["start_sample"] == a["end_sample"], "continuous must butt"
        assert b["gap_before"] == 0.0 and b["overlap_samples"] == 0
    # crossfade shortens the album; continuous is longer than crossfade but
    # shorter than gapped
    gap_total = build_sequence(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=2.0))["total_samples"]
    assert xf["total_samples"] < cont["total_samples"] < gap_total
    print("  crossfade (bar-quantized) + continuous layouts: OK")


def test_sequence_validation_catches_problems():
    # fixture: track 02 starts negative AND its window (900) is shorter
    # than its source (905) = truncation; total_samples is inconsistent
    seq = {"entries": [
        {"position": 1, "track_id": "01", "start_sample": 0,
         "end_sample": 1000, "source_samples": 1000},
        {"position": 2, "track_id": "02", "start_sample": -5,
         "end_sample": 895, "source_samples": 905},
    ], "total_samples": 950, "transitions": []}
    problems = validate_sequence(seq)
    assert any("negative" in p for p in problems), problems
    assert any("truncation" in p or "window" in p for p in problems), problems
    assert any("total_samples" in p for p in problems), problems
    print("  validation detects negative/truncated/mismatched sequences: OK")


def test_sequence_json_deterministic():
    _require_ep()
    album, root = _album(), _EP
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=1.5)
    s1 = build_sequence(album, root, cfg)
    s2 = build_sequence(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=1.5))
    assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True), \
        "sequence not deterministic"
    tmp = tempfile.mkdtemp(prefix="seq_json_")
    p = os.path.join(tmp, "sequence.json")
    from timbor.album.sequencing import save_sequence
    save_sequence(p, s1)
    h1 = hashlib.md5(open(p, "rb").read()).hexdigest()
    save_sequence(p, s2)
    h2 = hashlib.md5(open(p, "rb").read()).hexdigest()
    assert h1 == h2
    loaded = load_sequence(p)
    assert loaded["total_samples"] == s1["total_samples"]
    print("  sequence.json byte-deterministic + round trip: OK")


def test_order_change_only_alters_sequence():
    """§30 core: reordering must not touch track identity metadata."""
    _require_ep()
    album, root = _album(), _EP
    s_orig = build_sequence(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=2.0))
    album2 = json.loads(json.dumps(album))
    album2["sequencing"] = {"order": ["04", "03", "02", "01"]}
    s_rev = build_sequence(album2, root, SequenceConfig(
        transition_mode="gap", gap_seconds=2.0))
    # same set of tracks with identical per-track metadata...
    meta_orig = {e["track_id"]: (e["bpm"], e["source_samples"]) for e in s_orig["entries"]}
    meta_rev = {e["track_id"]: (e["bpm"], e["source_samples"]) for e in s_rev["entries"]}
    assert meta_orig == meta_rev, "track identity changed with order"
    # ...but different positions in time
    assert [e["track_id"] for e in s_rev["entries"]] == ["04", "03", "02", "01"]
    assert s_rev["entries"][0]["start_sample"] == 0
    print("  reorder changes only the sequence, not track identity: OK")


def test_wav_info_probe():
    _require_ep()
    frames, sr, ch, bits = wav_info(os.path.join(
        _EP, "01", "audio", "master.wav"))
    assert sr == 44100 and ch == 2 and bits == 32
    assert abs(frames / sr - 63.118) < 0.01
    print("  WAV header probe (frames/sr/ch/bits): OK")


def main() -> int:
    print("test_album_sequencing")
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
    print("All album sequencing tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
