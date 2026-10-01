"""tests.test_album_release — release packaging + §29/§30/§31 invariants.

Run:  python tests/test_album_release.py

Uses the shipped Machine Rave EP (§35): gap / crossfade / continuous
versions from the SAME four child projects.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.release import (_md5, assemble_release, package_release,
                                  verify_master_immutability)
from timbor.album.sequencing import SequenceConfig, build_sequence

FAILED = []

_WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EP = os.path.join(_WS, "albums", "timbor-machine-rave-ep")


def _album():
    with open(os.path.join(_EP, "album.json"), encoding="utf-8") as f:
        return json.load(f)


def _require_ep():
    if not os.path.isfile(os.path.join(_EP, "album.json")):
        raise RuntimeError("Machine Rave EP not found — generate it first")


def test_release_package_structure():
    _require_ep()
    album, root = _album(), _EP
    assemble_release(album, root, SequenceConfig(transition_mode="gap",
                                                 gap_seconds=2.0))
    res = package_release(album, root, include_track_masters=True)
    files = res["files"]
    for need in ("album.wav", "album_int16.wav", "tracklist.json",
                 "album.cue", "manifest.json", "README.md"):
        assert need in files, (need, files)
    assert "tracks" in files and len(res["track_masters"]) == 4
    # packaged album master == assembled album master
    assert res["album_master_hash"] == _md5(os.path.join(
        res["release_dir"], "album.wav"))
    # no sample libraries leak into the package (§22)
    assert not any("samples" in f.lower() or f.endswith(".db")
                   for f in files)
    print("  release package structure (§22) + no library leakage: OK")


def test_release_readme_deterministic():
    _require_ep()
    album, root = _album(), _EP
    out = assemble_release(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=2.0))
    txt1 = open(out["outputs"]["readme"], encoding="utf-8").read()
    out2 = assemble_release(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=2.0))
    txt2 = open(out2["outputs"]["readme"], encoding="utf-8").read()
    assert txt1 == txt2, "README not deterministic"
    for need in ("TIMBOR Album Release", "Tracklist:", "Sample rate:",
                 "Channels:", "Master format:", "Seed:"):
        assert need in txt1, need
    assert "generated" not in txt1.lower(), "README must have no timestamp"
    print("  release README deterministic + timestamp-free (§23): OK")


def test_manifest_content():
    _require_ep()
    album, root = _album(), _EP
    out = assemble_release(album, root, SequenceConfig(
        transition_mode="crossfade", crossfade_seconds=4.0))
    man = json.load(open(out["outputs"]["manifest"], encoding="utf-8"))
    # §20: album / tracks / sequence / audio / provenance blocks
    for block in ("album", "tracks", "sequence", "audio", "provenance"):
        assert block in man, block
    t = man["tracks"][0]
    for k in ("order", "title", "genre", "bpm", "key", "duration_seconds",
              "source_project", "source_master_hash", "album_gain_db"):
        assert k in t, k
    assert man["audio"]["sample_rate"] == 44100
    assert man["audio"]["album_master_hash"] == out["album_master_hash"]
    assert man["sequence"]["mode"] == "crossfade"
    assert man["sequence"]["transitions"], "transitions must be recorded"
    # determinism of bytes (§31)
    import re
    raw1 = open(out["outputs"]["manifest"], "rb").read()
    out2 = assemble_release(album, root, SequenceConfig(
        transition_mode="crossfade", crossfade_seconds=4.0))
    raw2 = open(out2["outputs"]["manifest"], "rb").read()
    assert raw1 == raw2, "manifest bytes differ between runs"
    print("  release manifest blocks + byte-determinism (§31): OK")


def test_full_config_reproducibility():
    """§31: same inputs ⇒ byte-identical sequence/loudness/tracklist/wav."""
    _require_ep()
    album, root = _album(), _EP
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=1.0)
    o1 = assemble_release(album, root, cfg)
    o2 = assemble_release(album, root, SequenceConfig(
        transition_mode="gap", gap_seconds=1.0))
    for key in ("sequence", "loudness", "tracklist"):
        h1 = hashlib.md5(open(o1["outputs"][key], "rb").read()).hexdigest()
        h2 = hashlib.md5(open(o2["outputs"][key], "rb").read()).hexdigest()
        assert h1 == h2, f"{key} not byte-identical"
    assert o1["album_master_hash"] == o2["album_master_hash"], \
        "album.wav not byte-identical"
    print("  §31 full reproducibility (sequence/loudness/tracklist/wav): OK")


def test_three_modes_same_children():
    """§35: gap/crossfade/continuous from the same four projects."""
    _require_ep()
    album, root = _album(), _EP
    before = {t["number"]: _md5(os.path.join(
        root, t["directory"], "audio", "master.wav")) for t in album["tracks"]}
    totals = {}
    seqs = {}
    import gc
    for mode, cfg in (
            ("gap", SequenceConfig(transition_mode="gap", gap_seconds=2.0)),
            ("crossfade", SequenceConfig(transition_mode="crossfade",
                                         crossfade_seconds=4.0)),
            ("continuous", SequenceConfig(transition_mode="continuous"))):
        out = assemble_release(album, root, cfg)
        totals[mode] = out["result"]["duration_seconds"]
        seqs[mode] = out["result"]["sequence"]
        assert out["qc_ok"], f"{mode} QC failed"
        imm = verify_master_immutability(album, root, before)
        assert imm["unchanged"], f"{mode} modified masters!"
        # three full assemblies in one process: release buffers between
        # modes (the studio runs these as separate jobs)
        del out, imm
        gc.collect()
    # sequence metadata describes each version distinctly
    assert totals["crossfade"] < totals["continuous"] < totals["gap"]
    assert seqs["gap"]["mode"] == "gap"
    assert seqs["continuous"]["mode"] == "continuous"
    assert all(e["overlap_samples"] > 0
               for e in seqs["crossfade"]["entries"][1:])
    assert all(e["overlap_samples"] == 0
               for e in seqs["continuous"]["entries"][1:])
    print(f"  3 modes from same masters: gap {totals['gap']:.1f}s > "
          f"continuous {totals['continuous']:.1f}s > crossfade "
          f"{totals['crossfade']:.1f}s, masters intact: OK")


def test_order_independence():
    """§30: reordering changes only sequence metadata/audio, not children."""
    _require_ep()
    album, root = _album(), _EP
    before = {t["number"]: _md5(os.path.join(
        root, t["directory"], "audio", "master.wav")) for t in album["tracks"]}
    cfg = SequenceConfig(transition_mode="gap", gap_seconds=2.0)

    a1 = json.loads(json.dumps(album))
    s1 = build_sequence(a1, root, cfg)
    a2 = json.loads(json.dumps(album))
    a2["sequencing"] = {"order": ["04", "02", "01", "03"]}
    s2 = build_sequence(a2, root, cfg)

    # child identity (bpm/samples per track) identical across both orders
    ident1 = {e["track_id"]: (e["bpm"], e["source_samples"])
              for e in s1["entries"]}
    ident2 = {e["track_id"]: (e["bpm"], e["source_samples"])
              for e in s2["entries"]}
    assert ident1 == ident2
    # ordering metadata differs
    assert [e["track_id"] for e in s2["entries"]] == ["04", "02", "01", "03"]
    # and the actual children on disk are untouched by either build
    imm = verify_master_immutability(album, root, before)
    assert imm["unchanged"]
    # seed-level identity lives in album.json and was never edited
    seeds1 = {t["number"]: t["seed"] for t in a1["tracks"]}
    seeds2 = {t["number"]: t["seed"] for t in a2["tracks"]}
    assert seeds1 == seeds2
    print("  §30 order-independence (children/seed/motif untouched): OK")


def test_track_master_immutability_full_cycle():
    """§29 mandatory: hashes before vs after sequencing+loudness+render+pack."""
    _require_ep()
    album, root = _album(), _EP
    before = {t["number"]: _md5(os.path.join(
        root, t["directory"], "audio", "master.wav")) for t in album["tracks"]}
    album_m = json.loads(json.dumps(album))
    album_m["loudness"] = {"mode": "match"}
    assemble_release(album_m, root, SequenceConfig(
        transition_mode="crossfade", crossfade_seconds=3.0))
    package_release(album_m, root, include_track_masters=True)
    imm = verify_master_immutability(album, root, before)
    assert imm["unchanged"], f"masters changed: {imm['changed']}"
    print("  §29 immutability across sequencing+matching+render+package: OK")


def main() -> int:
    print("test_album_release")
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
    print("All album release tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
