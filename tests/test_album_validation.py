"""tests.test_album_validation — continuity validation + serialization gate.

Run:  python tests/test_album_validation.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.dna import AlbumConfig
from timbor.album.generator import build_dna
from timbor.album.validation import validate_album, revalidate_album

FAILED = []


def _dna(**kw):
    cfg = AlbumConfig(seed=424242, tracks=2, key="D", mode="minor",
                      bpm_center=170, bpm_range=10, mood="dark", **kw)
    return cfg, build_dna(cfg)


def _track(i, genre="gabber", bpm=170.0, seed=None, variant=None,
           score=0.8, motif=None, duration=120.0):
    if seed is None:
        seed = 1000 + i
    lin = {"family": "main", "parent": "A", "variant": variant or ("A" if i == 0 else "A'_1"),
           "identity_score": score, "fingerprint": f"fp{i:04d}",
           "motif": motif if motif is not None else [0, 2, 3, None, 4, 3, 2, 0,
                                                     2, None, 4, 5, 4, 2, 0, 2]}
    return {"number": f"{i+1:02d}", "title": f"T{i}", "genre": genre,
            "seed": seed, "bpm": bpm, "key": "D minor", "bars": 32,
            "lineage": lin, "duration_seconds": duration, "directory": None,
            "project": None, "exports": None}


def test_clean_album_passes():
    cfg, dna = _dna()
    tracks = [_track(0, bpm=168.0, variant="A", score=1.0),
              _track(1, genre="jungle", bpm=165.0, score=0.6)]
    issues = validate_album(cfg, dna, tracks)
    assert not [i for i in issues if i["severity"] == "error"], issues
    print("  coherent album → no errors: OK")


def test_duplicate_seeds_error():
    cfg, dna = _dna()
    tracks = [_track(0, seed=111), _track(1, seed=111, genre="jungle")]
    issues = validate_album(cfg, dna, tracks)
    assert any(i["code"] == "duplicate_seeds" and i["severity"] == "error"
               for i in issues), issues
    print("  duplicate seeds detected: OK")


def test_key_drift_error():
    cfg, dna = _dna()
    t = _track(1, genre="jungle")
    t["key"] = "G minor"
    issues = validate_album(cfg, dna, [_track(0), t])
    assert any(i["code"] == "key_drift" and i["severity"] == "error"
               for i in issues), issues
    print("  key drift detected: OK")


def test_motif_identity_floor():
    cfg, dna = _dna()
    t = _track(1, genre="jungle", variant="A'_1", score=0.10)
    issues = validate_album(cfg, dna, [_track(0), t])
    assert any(i["code"] == "motif_identity_below_floor" and
               i["severity"] == "error" for i in issues), issues
    # duplicate variant fingerprints across tracks
    t2 = _track(1, genre="jungle", variant="A'_1", score=0.8)
    t2["lineage"]["fingerprint"] = _track(0)["lineage"]["fingerprint"]
    issues = validate_album(cfg, dna, [_track(0), t2])
    assert any(i["code"] == "duplicate_motif_variant" for i in issues), issues
    print("  identity floor + duplicate variant detection: OK")


def test_bpm_warnings_vs_errors():
    cfg, dna = _dna()
    far = _track(1, genre="trance", bpm=142.0)   # genre-snapped: warning only
    issues = validate_album(cfg, dna, [_track(0), far])
    codes = [(i["code"], i["severity"]) for i in issues]
    assert ("bpm_outside_neighborhood", "warning") in codes, codes
    assert not any(s == "error" for _, s in codes), codes
    print("  bpm outside window (genre-snapped) → warning, not error: OK")


def test_project_level_checks():
    cfg, dna = _dna()
    t0, t1 = _track(0), _track(1, genre="jungle", bpm=165.0)
    good = {"song": {"bpm": 165.0, "motif": t1["lineage"]["motif"],
                     "key": "D", "scale": "natural_minor"}}
    bad = {"song": {"bpm": 150.0, "motif": [1, 2, 3], "key": "E",
                    "scale": "phrygian"}}
    issues = validate_album(cfg, dna, [t0, t1], project_dicts=[None, good])
    assert not [i for i in issues if i["severity"] == "error"], issues
    issues = validate_album(cfg, dna, [t0, t1], project_dicts=[None, bad])
    codes = {i["code"] for i in issues}
    assert {"project_bpm_mismatch", "project_motif_mismatch",
            "project_key_mismatch"} <= codes, codes
    print("  project bpm/motif/key coherence checks: OK")


def test_palette_majority_rule():
    cfg, dna = _dna()
    t0, t1 = _track(0), _track(1, genre="jungle", bpm=165.0)
    pal = {"roles": {"main_break": {"path": "lib/amen.wav", "filename": "amen.wav",
                                    "roles": ["main_break"]}}}
    used = {"song": {"bpm": 165.0, "motif": t1["lineage"]["motif"],
                     "key": "D", "scale": "natural_minor"},
            "samples": [{"id": "s1", "path": "lib\\amen.wav"}],
            "timeline": [{"sample_id": "s1", "start_beat": 0.0}]}
    issues = validate_album(cfg, dna, [t0, t1], project_dicts=[None, used],
                            palette=pal)
    assert not [i for i in issues if i["code"].startswith("palette")], issues
    # unused palette sample is an error; weak propagation a warning
    t2 = _track(2, genre="trance", bpm=142.0, variant="A'_2")
    pal2 = {"roles": {"main_break": {"path": "lib/amen.wav",
                                     "filename": "amen.wav",
                                     "roles": ["main_break"]},
                      "impact": {"path": "lib/impact.wav",
                                 "filename": "impact.wav", "roles": ["impact"]}}}
    issues = validate_album(cfg, dna, [t0, t1], project_dicts=[used, used],
                            palette=pal2)
    codes = {i["code"] for i in issues}
    assert "palette_unused" in codes, (codes, issues)
    print("  palette majority propagation (strict-unused = error): OK")


def test_serialization_gate():
    from timbor.album.serialization import (save_album, load_album,
                                            ALBUM_FORMAT, ALBUM_VERSION)
    tmp = tempfile.mkdtemp(prefix="album_ser_")
    doc = {"format": ALBUM_FORMAT, "version": ALBUM_VERSION, "name": "X",
           "config": {}, "dna": {}, "tracks": [], "palette": {},
           "validation": []}
    p = os.path.join(tmp, "album.json")
    save_album(p, doc)
    loaded = load_album(p)
    assert loaded["name"] == "X"
    doc["format"] = "wrong"
    save_album(p, doc)
    try:
        load_album(p)
        raise AssertionError("bad format accepted")
    except ValueError as e:
        assert "not a TIMBOR album" in str(e)
    doc.update(format=ALBUM_FORMAT, version=99)
    save_album(p, doc)
    try:
        load_album(p)
        raise AssertionError("bad version accepted")
    except ValueError:
        pass
    json.dump({"hello": 1}, open(p, "w"))
    try:
        load_album(p)
        raise AssertionError("non-album accepted")
    except ValueError:
        pass
    print("  album.json format/version gate: OK")


def main() -> int:
    print("test_album_validation")
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
    print("All album validation tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
