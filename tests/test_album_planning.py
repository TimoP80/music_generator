"""tests.test_album_planning — BPM neighborhood, durations, per-track Plans.

Run:  python tests/test_album_planning.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.dna import AlbumConfig
from timbor.album.generator import build_dna
from timbor.album.planning import (assign_bpm, bars_for_duration,
                                   duration_target, make_plan, track_identity,
                                   default_bpm_center)

FAILED = []


def _cfg(**kw):
    base = dict(seed=424242, tracks=4, genres=["gabber", "jungle", "trance",
                                               "hard_house"],
                bpm_center=170, bpm_range=12, key="D", mode="minor",
                mood="dark", duration_range=[90, 180])
    base.update(kw)
    return AlbumConfig(**base)


def _dna(cfg=None):
    return build_dna(cfg or _cfg())


def test_bpm_deterministic_within_neighborhood():
    cfg, dna = _cfg(), _dna()
    for i in range(4):
        b1 = assign_bpm(cfg, dna, cfg.genres[i], i)
        b2 = assign_bpm(cfg, dna, cfg.genres[i], i)
        assert b1 == b2, f"track {i} bpm not deterministic"
    # at least two distinct BPMs across 6 tracks of one genre (no lockstep)
    bpms = {assign_bpm(cfg, dna, "gabber", i) for i in range(6)}
    assert len(bpms) >= 2, f"expected BPM variety, got {bpms}"
    print("  BPM deterministic with per-track variety: OK")


def test_bpm_respects_genre_over_neighborhood():
    cfg, dna = _cfg(), _dna()
    # trance's authentic range (130-145) sits below a 170 center: genre wins
    for i in (2, 5, 7):
        b = assign_bpm(cfg, dna, "trance", i)
        assert 130 <= b <= 145, f"trance bpm {b} outside genre range"
    lo, hi = dna.bpm_window()
    b = assign_bpm(cfg, dna, "gabber", 0)
    assert lo <= b <= hi, "gabber sits inside the genre range → inside window"
    print("  genre ranges override the geometric window: OK")


def test_duration_and_bars():
    cfg = _cfg(duration_range=[90, 180], divergence="high")
    d1 = duration_target(cfg, 0)
    d2 = duration_target(cfg, 1)
    assert d1 == duration_target(cfg, 0), "duration not deterministic"
    assert d1 != d2, "high divergence should spread durations"
    subtle = _cfg(duration_range=[90, 180], divergence="subtle")
    assert duration_target(subtle, 0) == duration_target(subtle, 3) == 135.0
    # bars: 4-bar phrases, sane bounds, tempo-dependent
    bars = bars_for_duration("gabber", 170.0, 90.0)
    assert bars % 4 == 0 and 16 <= bars <= 128
    assert bars_for_duration("trance", 140.0, 90.0) != bars, \
        "same seconds must give different bars at different tempos"
    print("  duration targets + bar conversion: OK")


def test_track_identity():
    cfg = _cfg(titles=["Intro", None, "Third Track"])
    id0 = track_identity(cfg, 0)
    id1 = track_identity(cfg, 1)
    id2 = track_identity(cfg, 2)
    assert id0["number"] == "01" and id0["title"] == "Intro"
    assert id1["title"] == "Track 02", "missing titles fall back"
    assert id2["slug"] == "third-track"
    assert id0["slug"] == "intro" and id0["slug"] != id1["slug"]
    print("  track identity (numbers, titles, slugs): OK")


def test_make_plan_constrains_engine():
    cfg, dna = _cfg(), _dna()
    plan = make_plan(cfg, dna, 1, sample_dir=None)
    from timbor.album.dna import derive_seed
    assert plan.seed == derive_seed(cfg.seed, "track-02"), "seed not derived"
    assert plan.genre == "jungle"
    assert plan.key == "D" and plan.scale == "natural_minor"
    assert plan.mood == "dark"
    assert dna.bpm_window()[0] - 1 <= plan.bpm or plan.genre in ("trance",)
    assert plan.bars_limit and plan.bars_limit % 4 == 0
    assert plan.sample_mode == "balanced"
    assert plan.prog_family_override == [dna.harmonic_family]
    assert plan.motif_override is None, "motif injected later, not by make_plan"
    print("  make_plan constrains seed/genre/key/mood/bars/prog family: OK")


def test_make_plan_standalone_equivalence():
    """Album plans must not leak album state into normal generation."""
    cfg, dna = _cfg(), _dna()
    p0 = make_plan(cfg, dna, 0, None)
    p1 = make_plan(cfg, dna, 1, None)
    assert p0.seed != p1.seed and p0.genre != p1.genre
    # same inputs → same plan values
    p0b = make_plan(cfg, dna, 0, None)
    assert p0b.bpm == p0.bpm and p0b.bars_limit == p0.bars_limit
    print("  per-track plan independence: OK")


def main() -> int:
    print("test_album_planning")
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
    print("All album planning tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
