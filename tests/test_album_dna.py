"""tests.test_album_dna — album configuration, seed derivation, AlbumDNA.

Run:  python tests/test_album_dna.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.dna import AlbumConfig, AlbumDNA, derive_seed, crc32_json
from timbor.album.generator import build_dna

FAILED = []


def test_config_validation():
    errs = AlbumConfig(tracks=0).validate()
    assert any("tracks" in e for e in errs), errs
    errs = AlbumConfig(tracks=2, palette_mode="bogus").validate()
    assert any("palette_mode" in e for e in errs), errs
    errs = AlbumConfig(tracks=2, divergence="wild").validate()
    assert any("divergence" in e for e in errs), errs
    errs = AlbumConfig(tracks=2, duration_range=[0, 100]).validate()
    assert any("duration_range" in e for e in errs), errs
    # unsorted ranges are accepted leniently (sorted internally)
    assert not any("duration_range" in e for e in
                   AlbumConfig(tracks=2, duration_range=[180, 90]).validate())
    errs = AlbumConfig(tracks=2, track_overrides={9: {"bpm_center": 170}}).validate()
    assert any("track_overrides" in e for e in errs), errs
    assert AlbumConfig().validate() == []
    print("  config validation: OK")


def test_config_roundtrip_and_overrides():
    cfg = AlbumConfig(name="X", seed=7, tracks=3, genres=["gabber"],
                      track_overrides={1: {"mood": "euphoric"}})
    data = cfg.to_json()
    cfg2 = AlbumConfig.from_json(data)
    assert cfg2 == cfg, "round trip changed the config"
    t1 = cfg.track_config(1)
    assert t1.mood == "euphoric" and cfg.mood is None, "override not applied"
    t2 = cfg.track_config(2)
    assert t2.mood is None, "override leaked to other tracks"
    print("  config round trip + per-track overrides: OK")


def test_seed_derivation():
    a = derive_seed(424242, "track-01")
    b = derive_seed(424242, "track-01")
    c = derive_seed(424242, "track-02")
    d = derive_seed(424243, "track-01")
    assert a == b, "same inputs must give the same seed"
    assert a != c, "labels must not collide"
    assert a != d, "album seed must matter"
    assert 0 <= a < 2**31, "seed out of range"
    # stable across processes: no Python hash() involvement
    assert derive_seed(int(a), "x") != derive_seed(int(a) + 1, "x")
    h = crc32_json({"a": 1, "b": [1, 2]})
    assert h == crc32_json({"b": [1, 2], "a": 1}), "crc32_json not canonical"
    print("  seed derivation (SHA-256, no builtin hash): OK")


def test_dna_roundtrip_and_hash():
    cfg = AlbumConfig(seed=424242, tracks=4, key="D", mode="minor",
                      bpm_center=170, bpm_range=12, mood="dark")
    dna = build_dna(cfg)
    data = dna.to_json()
    dna2 = AlbumDNA.from_json(data)
    assert dna2 == dna, "DNA round trip changed fields"
    assert dna.dna_hash() == dna2.dna_hash()
    lo, hi = dna.bpm_window()
    assert lo == 158.0 and hi == 182.0, (lo, hi)
    print("  AlbumDNA round trip + dna_hash + bpm window: OK")


def test_build_dna_defaults():
    cfg = AlbumConfig(seed=424242, tracks=2, genres=["trance", "jungle"],
                      mood="dark")
    dna = build_dna(cfg)
    assert dna.root_key == "A" and dna.mode == "minor", (dna.root_key, dna.mode)
    # center defaults to the anchor genre's authentic midpoint (135-142)
    assert dna.bpm_center == 138.5, "center should default to genre midpoint"
    assert dna.genre_anchor == "trance" and dna.harmonic_family != ""
    # grid follows the anchor genre's melody_density (8 or 16)
    assert len(dna.motif_family) in (8, 16) \
        and any(x is not None for x in dna.motif_family)
    assert dna.rhythm_fingerprint and set(dna.rhythm_fingerprint) <= {0, 1}
    assert dna.mood == "dark"
    # explicit key/mode honored
    cfg2 = AlbumConfig(seed=1, tracks=1, key="F#", mode="phrygian", mood="dark")
    dna2 = build_dna(cfg2)
    assert dna2.root_key == "F#" and dna2.mode == "phrygian"
    print("  build_dna defaults + explicit key/mode: OK")


def main() -> int:
    print("test_album_dna")
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
    print("All album DNA tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
