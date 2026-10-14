"""tests.test_album_palette — shared palette build, modes, provenance.

Run:  python tests/test_album_palette.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="album_palette_")
FAILED = []

_DB = None


def _index():
    global _DB
    from timbor.samples.cache import SampleIndex
    from timbor.samples.index import index_library
    from tests.test_samples import build_fixture_library
    lib = os.path.join(TMP, "lib")
    _DB = os.path.join(TMP, "samples.db")
    if not os.path.exists(lib):
        build_fixture_library(lib)
    idx = SampleIndex(_DB)
    if idx.analyzed_count() == 0:
        idx.close()
        index_library(lib, db_path=_DB, verbose=False)
        idx = SampleIndex(_DB)
    return idx


def _cfg(mode="balanced", **kw):
    from timbor.album.dna import AlbumConfig
    base = dict(seed=424242, tracks=4, genres=["gabber", "jungle", "trance",
                                               "hard_house"],
                bpm_center=170, bpm_range=12, key="D", mode="minor",
                mood="dark", palette_mode=mode)
    base.update(kw)
    return AlbumConfig(**base)


def test_role_sets_by_mode():
    from timbor.album.palette import palette_roles_for
    assert "main_break" in palette_roles_for("loose")
    assert len(palette_roles_for("loose")) == 1
    assert set(palette_roles_for("balanced")) == {"main_break", "impact",
                                                  "vocal_hit", "melodic_loop"}
    assert len(palette_roles_for("strict")) > 4
    print("  role sets per mode (strict/balanced/loose): OK")


def test_build_palette_deterministic_with_provenance():
    idx = _index()
    try:
        p1 = _index and __import__("timbor.album.palette", fromlist=["build_palette"]) \
            .build_palette(_cfg(), idx, ["gabber", "jungle", "trance", "hard_house"])
        p2 = __import__("timbor.album.palette", fromlist=["build_palette"]) \
            .build_palette(_cfg(), idx, ["gabber", "jungle", "trance", "hard_house"])
        assert p1["roles"] == p2["roles"], "palette not deterministic"
        for role, e in p1["roles"].items():
            for field in ("id", "path", "filename", "category", "selection_score",
                          "roles"):
                assert field in e, f"{role} missing {field}"
            assert e["selection_score"] > 0, "selection score must be recorded"
        assert p1["mode"] == "balanced"
        print("  deterministic build + provenance fields: OK")
    finally:
        idx.close()


def test_loose_strict_role_counts():
    idx = _index()
    try:
        from timbor.album.palette import build_palette
        loose = build_palette(_cfg("loose"), idx, ["gabber", "jungle"])
        balanced = build_palette(_cfg("balanced"), idx, ["gabber", "jungle"])
        strict = build_palette(_cfg("strict"), idx, ["gabber", "jungle"])
        assert len(loose["roles"]) <= 2  # main_break (+merged role)
        assert len(balanced["roles"]) >= 3
        assert len(strict["roles"]) >= len(balanced["roles"])
        print("  palette sizes scale with mode: OK")
    finally:
        idx.close()


def test_same_path():
    from timbor.album.palette import same_path
    assert same_path("a/b/c.wav", "a/b/c.wav")
    assert same_path("data\\lib\\x.wav", "data/lib/x.wav")
    # relative vs absolute: falls back to basename
    import os
    abs_p = os.path.abspath(os.path.join("data", "demo_library", "breaks",
                                         "amen_break_00_174bpm.wav"))
    assert same_path(abs_p, "data/demo_library/breaks/amen_break_00_174bpm.wav")
    assert not same_path("x.wav", None)
    assert not same_path(None, "x.wav")
    assert not same_path("a/one.wav", "b/two.wav")
    print("  project-relative vs library-absolute path matching: OK")


def test_palette_summary():
    idx = _index()
    try:
        from timbor.album.palette import build_palette, palette_summary
        p = build_palette(_cfg("balanced"), idx, ["gabber", "jungle"])
        s = palette_summary(p)
        assert "balanced" in s and "main_break" in s
        print("  palette summary text: OK")
    finally:
        idx.close()


def main() -> int:
    print("test_album_palette")
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
    print("All album palette tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
