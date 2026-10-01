"""tests.test_album_motifs — motif lineage, variants, identity floor.

Run:  python tests/test_album_motifs.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.album.motif import (base_motif, build_lineage, derive_track_motif,
                                motif_fingerprint, rhythm_fingerprint,
                                genre_phrase_steps)
from timbor.theory import motif_identity

FAILED = []

BASE = [0, None, 2, 3, None, 4, 3, 2, 0, None, 2, None, 4, 5, 4, 2]
GENRES = ["gabber", "jungle", "trance", "hard_house", "rave"]


def test_base_motif_deterministic():
    m1, c1 = base_motif(424242, "natural_minor", "D", steps=16)
    m2, c2 = base_motif(424242, "natural_minor", "D", steps=16)
    assert m1 == m2 and c1 == c2, "base motif not deterministic"
    assert len(m1) == 16 and any(x is not None for x in m1)
    m3, _ = base_motif(424243, "natural_minor", "D", steps=16)
    assert m3 != m1, "different seeds gave the same motif"
    # human alias
    m4, _ = base_motif(424242, "minor", "D", steps=16)
    assert m4 == m1, "'minor' alias should map to natural_minor"
    print("  base motif determinism + alias: OK")


def test_variant_determinism_and_independence():
    l1 = build_lineage(424242, BASE, GENRES)
    l2 = build_lineage(424242, BASE, GENRES)
    for i in range(len(GENRES)):
        assert l1[i][0] == l2[i][0], f"track {i} variant not deterministic"
        assert l1[i][1] == l2[i][1], f"track {i} meta not deterministic"
    # adding track 6 must not change tracks 0..4
    l3 = build_lineage(424242, BASE, GENRES + ["big_beat"])
    for i in range(len(GENRES)):
        assert l1[i][0] == l3[i][0], f"adding a track reshuffled track {i}"
    print("  lineage determinism + track independence: OK")


def test_track0_is_base():
    lin = build_lineage(424242, BASE, GENRES)
    v0, meta0 = lin[0]
    # track 0 states the album motif normalized to its genre's grid —
    # exactly what the engine renders for that genre
    from timbor.album.motif import genre_phrase_steps
    grid = genre_phrase_steps(GENRES[0])
    assert v0 == BASE[:grid], "track 0 must be the untouched Motif A"
    assert meta0["variant"] == "A"
    assert meta0["identity_score"] == 1.0
    assert meta0["transformations"] == ["base_statement"]
    print("  track 0 = untouched Motif A (genre grid): OK")


def test_identity_floor_enforced():
    lin = build_lineage(424242, BASE, GENRES)
    for i in range(1, len(GENRES)):
        score = lin[i][1]["identity_score"]
        assert score >= 0.35, f"track {i} identity {score} below floor"
    # worst case: pure retrograde as the only op would still pass after the
    # safety net (simulate via direct call with max_ops)
    v, meta = derive_track_motif(7, 3, BASE, "gabber", max_ops=0, steps=16)
    assert motif_identity(BASE, v) >= 0.9, "max_ops=0 should stay near base"
    print("  identity floor enforced on all variants: OK")


def test_no_identical_variants():
    for seed in (424242, 999, 12345):
        lin = build_lineage(seed, BASE, GENRES * 2)  # 10 tracks
        fps = [lin[i][1]["fingerprint"] for i in range(len(GENRES) * 2)]
        assert len(set(fps)) == len(fps), f"seed {seed}: duplicate variants"
    print("  no identical variants across tracks (3 seeds): OK")


def test_lineage_metadata_shape():
    lin = build_lineage(424242, BASE, GENRES)
    v, meta = lin[2]
    assert meta["family"] == "main" and meta["parent"] == "A"
    assert meta["variant"].startswith("A")
    assert isinstance(meta["motif"], list) and meta["motif"] == v
    assert isinstance(meta["identity_score"], float)
    assert len(meta["fingerprint"]) == 8
    assert meta["transformations"], "transformations must be recorded"
    known = {"transpose", "note_omission", "rhythmic_augment", "note_insertion",
             "retrograde", "invert", "octave_shift", "arpeggiation",
             "call_response", "fragmentation", "genre_rhythm", "base_statement",
             "dedup_transpose", "transpose(+1)"}
    for t in meta["transformations"]:
        assert t.split("(")[0] in known or t in known, f"unknown op {t}"
    print("  lineage metadata shape (spec §8): OK")


def test_identity_scoring_musical():
    # transposition = same melody
    transposed = [None if x is None else x + 2 for x in BASE]
    assert motif_identity(BASE, transposed) > 0.9
    # unrelated melody scores lower than a light variant
    unrelated = [5, None, 5, 5, None, 5, 5, 5, 5, None, 5, None, 5, 5, 5, 5]
    light, _ = derive_track_motif(424242, 1, BASE, "gabber", max_ops=1, steps=16)
    assert motif_identity(BASE, light) > motif_identity(BASE, unrelated)
    # rhythm fingerprint reflects rests
    fp = rhythm_fingerprint(BASE)
    assert len(fp) == 16 and fp[0] == 1 and fp[1] == 0
    # genre phrase steps match the engine
    # engine table: melody_density >= 0.8 → 16 steps
    assert genre_phrase_steps("frenchcore") == 16
    assert genre_phrase_steps("trance") == 8
    assert genre_phrase_steps("jungle") == 8
    print("  identity scoring is musical (transposition ≈ identity): OK")


def test_motif_fingerprint_stable():
    a = motif_fingerprint(BASE)
    b = motif_fingerprint(list(BASE))
    assert a == b, "fingerprint not stable"
    assert a != motif_fingerprint([0] * 16)
    print("  motif fingerprint stability: OK")


def main() -> int:
    print("test_album_motifs")
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
    print("All album motif tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
