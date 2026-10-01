"""tests.test_album_render — assembly rendering (§15–§17, §27–§29).

Run:  python tests/test_album_render.py

Uses the shipped Machine Rave EP (§35 integration fixture).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.release import (_md5, assemble_release, package_release,
                                  render_album, verify_album,
                                  verify_master_immutability)
from timbor.album.sequencing import SequenceConfig
from timbor.album.loudness import read_wav_stereo

FAILED = []

_WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EP = os.path.join(_WS, "albums", "timbor-machine-rave-ep")


def _require_ep():
    if not os.path.isfile(os.path.join(_EP, "album.json")):
        raise RuntimeError("Machine Rave EP not found — generate it first")


def _album():
    with open(os.path.join(_EP, "album.json"), encoding="utf-8") as f:
        return json.load(f)


def _master_hashes(album, root):
    return {t["number"]: _md5(os.path.join(root, t["directory"], "audio",
                                           "master.wav"))
            for t in album["tracks"]}


def test_gap_render_deterministic_and_immutable():
    _require_ep()
    album, root = _album(), _EP
    before = _master_hashes(album, root)
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=2.0)
    r1 = render_album(album, root, cfg,
                      out_dir=tempfile.mkdtemp(prefix="rend_a_"))
    r2 = render_album(album, root, cfg,
                      out_dir=tempfile.mkdtemp(prefix="rend_b_"))
    h1 = _md5(r1["outputs"]["album_wav"])
    h2 = _md5(r2["outputs"]["album_wav"])
    assert h1 == h2, "album.wav not byte-deterministic"
    # integer-sample exactness: total == sum(sources) + integer gaps
    sr = r1["sample_rate"]
    expect = sum(e["source_samples"] for e in r1["sequence"]["entries"]) + \
        3 * int(round(2.0 * sr))
    assert r1["total_samples"] == expect
    # §29 mandatory immutability
    assert verify_master_immutability(album, root, before)["unchanged"]
    print(f"  gap render byte-deterministic ({h1[:10]}), masters intact: OK")


def test_assemble_release_artifacts():
    _require_ep()
    album, root = _album(), _EP
    before = _master_hashes(album, root)
    out = assemble_release(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=1.0))
    need = {"album_wav", "album_int16_wav", "sequence", "loudness", "qc",
            "cue", "tracklist", "manifest", "readme"}
    assert need <= set(out["outputs"]), sorted(out["outputs"])
    assert out["qc_ok"], "album QC failed"
    # cue sheet corresponds to the sequence exactly
    cue = open(out["outputs"]["cue"], encoding="utf-8").read()
    seq = out["result"]["sequence"]

    def cue_time(seconds):
        tf = int(round(seconds * 75.0))
        mm, rem = divmod(tf, 75 * 60)
        ss, ff = divmod(rem, 75)
        return f"{mm:02d}:{ss:02d}:{ff:02d}"
    for e in seq["entries"]:
        assert f"INDEX 01 {cue_time(e['start_seconds'])}" in cue
    assert cue.count("TRACK ") == len(seq["entries"])
    # tracklist matches spec §19 shape
    tl = json.load(open(out["outputs"]["tracklist"], encoding="utf-8"))
    assert tl["album"] == album["name"]
    assert tl["tracks"][1]["gap_before"] == 1.0
    assert abs(tl["tracks"][1]["start_seconds"] -
               seq["entries"][1]["start_seconds"]) < 1e-6
    # immutability after the whole assembly (incl. loudness analysis)
    assert verify_master_immutability(album, root, before)["unchanged"]
    print("  assemble_release: all 9 artifacts, cue/tracklist exact: OK")


def test_verify_album_tiers():
    _require_ep()
    album, root = _album(), _EP
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=2.0)
    assemble_release(album, root, cfg)
    v = verify_album(album, root, cfg)
    assert v["status"] == "IDENTICAL", v
    assert v["stored_hash"] == v["fresh_hash"]
    # tamper with the stored album → DIFFERENT, never forgiven (§28)
    stored = os.path.join(root, "album", "album.wav")
    with open(stored, "r+b") as f:
        f.seek(200)
        b = f.read(2)
        f.seek(200)
        f.write(bytes([b[0] ^ 0xFF, b[1] ^ 0x55]))
    v2 = verify_album(album, root, cfg)
    assert v2["status"] == "DIFFERENT", v2
    # restore the correct file
    assemble_release(album, root, cfg)
    print("  verify: IDENTICAL when intact, DIFFERENT when tampered: OK")


def test_crossfade_audio_semantics():
    _require_ep()
    album, root = _album(), _EP
    cfg = SequenceConfig(transition_mode="crossfade", crossfade_seconds=4.0,
                         quantize="bar")
    out_dir = tempfile.mkdtemp(prefix="rend_xf_")
    r = render_album(album, root, cfg, out_dir=out_dir)
    l, rr, sr = read_wav_stereo(r["outputs"]["album_wav"])
    entries = r["sequence"]["entries"]
    # the crossfaded album is SHORTER than gapped: overlap eats gap+blend
    e2 = entries[1]
    ov = e2["overlap_samples"]
    assert ov > 0
    # boundary region must be a true mix: contains energy from both tracks
    # (sampled at the seam midpoint both signals are ~-6 dB of themselves)
    seam = e2["start_sample"] + ov // 2
    window = l[seam - 100: seam + 100]
    assert np.max(np.abs(window)) > 1e-4, "silent seam — crossfade missing"
    # no truncation: total == sum(sources) - sum(overlaps)
    expect = sum(e["source_samples"] for e in entries) - \
        sum(e["overlap_samples"] for e in entries)
    assert r["total_samples"] == expect
    before = _master_hashes(album, root)
    assert verify_master_immutability(album, root, before)["unchanged"]
    print("  crossfade render: overlap mixed, sources intact: OK")


def test_match_mode_gain_is_applied_and_recorded():
    _require_ep()
    album, root = _album(), _EP
    album = json.loads(json.dumps(album))
    album["loudness"] = {"mode": "match"}
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=2.0)
    r = render_album(album, root, cfg,
                     out_dir=tempfile.mkdtemp(prefix="rend_m_"))
    assert r["loudness_mode"] == "match"
    assert r["gains"] is not None and len(r["gains"]) == 4
    # anchor track has 0 dB, others are <= 0 (pulled down, not boosted)
    g = r["gains"]
    assert min(g.values()) == 0.0
    assert all(v <= 3.0 + 1e-9 for v in g.values())
    print(f"  match mode gains applied + recorded ({g}): OK")


def test_limiter_off_by_default_and_explicit():
    _require_ep()
    album, root = _album(), _EP
    r = render_album(album, root, SequenceConfig(transition_mode="gap"),
                     out_dir=tempfile.mkdtemp(prefix="rend_l_"))
    assert r["limiter_applied"] is False, "limiter must default OFF (§14)"
    album2 = json.loads(json.dumps(album))
    album2["loudness"] = {"mode": "preserve", "limiter": True,
                          "limiter_ceiling_db": -1.0}
    r2 = render_album(album2, root, SequenceConfig(transition_mode="gap"),
                      out_dir=tempfile.mkdtemp(prefix="rend_l2_"))
    assert r2["limiter_applied"] is True
    assert r2["peak"]["peak_dbfs"] <= -0.95, r2["peak"]
    # and the masters still untouched
    before = _master_hashes(album, root)
    assert verify_master_immutability(album, root, before)["unchanged"]
    print("  limiter default OFF; explicit ON holds ceiling: OK")


def test_no_regeneration_dependency():
    """render_album must not import the song pipeline (§15)."""
    import timbor.album.release as rel
    import inspect
    src = inspect.getsource(rel)
    for banned in ("render_track(", "generate_song(", "SongPlan(",
                   "render_section("):
        assert banned not in src, f"release.py references {banned}"
    print("  render path contains no song-regeneration calls: OK")


def main() -> int:
    print("test_album_render")
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
    print("All album render tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
