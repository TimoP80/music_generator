"""tests.test_album_generation — end-to-end EP generation + determinism.

Run:  python tests/test_album_generation.py

Renders small albums (16-bar tracks) against a copy of the demo library.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="album_gen_")
FAILED = []

_LIB = None
_DB = None


def _library():
    global _LIB, _DB
    if _DB is None:
        from timbor.samples.index import index_library
        from tests.test_samples import build_fixture_library
        ws = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _LIB = os.path.join(TMP, "lib")
        if not os.path.exists(_LIB):
            shutil.copytree(os.path.join(ws, "data", "demo_library"), _LIB)
        if not any(f.endswith(".wav") for _, _, files in os.walk(_LIB) for f in files):
            build_fixture_library(_LIB)
        _DB = os.path.join(TMP, "samples.db")
        index_library(_LIB, db_path=_DB, verbose=False)
    return _LIB, _DB


def _cfg(**kw):
    from timbor.album import AlbumConfig
    lib, _ = _library()
    base = dict(name="Test EP", seed=424242, tracks=2,
                genres=["gabber", "jungle"], bpm_center=170, bpm_range=10,
                key="D", mode="minor", mood="dark", duration_range=[28, 40],
                sample_dir=lib)
    base.update(kw)
    return AlbumConfig(**base)


def _tree_hashes(root, strip_timestamps=True):
    hashes = {}
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if strip_timestamps and rel in ("album.json",) or \
                    (strip_timestamps and rel.endswith("/project.json")):
                with open(p, encoding="utf-8") as fh:
                    d = json.load(fh)
                d.get("generator", {}).pop("generated_at", None)
                hashes[rel] = hashlib.md5(
                    json.dumps(d, sort_keys=True).encode()).hexdigest()
            else:
                hashes[rel] = hashlib.md5(
                    open(p, "rb").read()).hexdigest()
    return hashes


def test_album_plan_only():
    from timbor.album import generate_album, load_album
    cfg = _cfg()
    out = os.path.join(TMP, "plan_only")
    doc = generate_album(cfg, out, render=False, db_path=_DB)
    assert os.path.isfile(os.path.join(doc["root"], "album.json"))
    loaded = load_album(os.path.join(doc["root"], "album.json"))
    assert loaded["format"] == "timbor-album"
    assert len(loaded["tracks"]) == 2
    assert all(t["directory"] is None for t in loaded["tracks"]), \
        "plan-only must not create project dirs"
    assert not any(os.path.isdir(os.path.join(doc["root"], d))
                   for d in ("01", "02")), "no audio dirs in plan-only"
    # spec §25: report/manifest ops never regenerate; plan docs are complete
    assert loaded["dna"]["root_key"] == "D"
    print("  plan-only mode (no audio, complete documents): OK")


def test_full_generation_structure():
    from timbor.album import generate_album
    cfg = _cfg(titles=["First Strike", None])
    out = os.path.join(TMP, "full")
    doc = generate_album(cfg, out, render=True, db_path=_DB)
    root = doc["root"]
    assert doc["tracks"][0]["directory"] == "01-first-strike"
    assert doc["tracks"][1]["directory"] == "02", \
        "untitled track uses plain number dir"
    for t in doc["tracks"]:
        td = os.path.join(root, t["directory"])
        assert os.path.isfile(os.path.join(td, "project.json"))
        assert os.path.isfile(os.path.join(td, "audio", "master.wav"))
        for stem in ("drums", "bass", "mel", "fx", "samples"):
            assert os.path.isfile(os.path.join(td, "audio", f"{stem}.wav"))
        assert os.path.isfile(os.path.join(
            td, "midi", f"{t['directory'].split('/')[-1]}.mid"))
        assert t["exports"] and sorted(t["exports"]) == \
            ["ableton", "manifest", "midi", "reaper"]
        # lineage metadata injected into the project
        pj = json.load(open(os.path.join(td, "project.json"), encoding="utf-8"))
        alb = pj["metadata"]["album"]
        assert alb["lineage"]["variant"] in ("A", "A'_1")
        assert alb["position"] in (1, 2) and alb["of"] == 2
    # per-track musical identity
    t0, t1 = doc["tracks"]
    assert t0["genre"] == "gabber" and t1["genre"] == "jungle"
    assert t0["seed"] != t1["seed"], "tracks must have distinct derived seeds"
    assert t0["lineage"]["identity_score"] == 1.0
    assert t1["lineage"]["identity_score"] >= 0.35
    print("  full generation: structure, exports, lineage injection: OK")


def test_album_determinism():
    from timbor.album import generate_album
    cfg = _cfg()
    d1 = generate_album(cfg, os.path.join(TMP, "det_a"), render=True,
                        db_path=_DB)
    d2 = generate_album(cfg, os.path.join(TMP, "det_b"), render=True,
                        db_path=_DB)
    h1 = _tree_hashes(d1["root"])
    h2 = _tree_hashes(d2["root"])
    assert sorted(h1) == sorted(h2), "file sets differ between runs"
    diff = [k for k in h1 if h1[k] != h2[k]]
    assert not diff, f"non-deterministic files: {diff}"
    n_audio = sum(1 for k in h1 if k.endswith(".wav"))
    assert n_audio >= 12, "expected full audio sets"
    print(f"  album determinism: {len(h1)} files byte-identical "
          f"(JSON sans generated_at): OK")


def test_regen_track_single():
    from timbor.album import generate_album, regen_track
    cfg = _cfg(seed=999, tracks=3,
               genres=["gabber", "jungle", "trance"])
    out = os.path.join(TMP, "regen")
    doc = generate_album(cfg, out, render=True, db_path=_DB)
    root = doc["root"]

    def master_md5(tdir):
        return hashlib.md5(open(os.path.join(root, tdir, "audio",
                                             "master.wav"), "rb").read()).hexdigest()

    before = {t["number"]: master_md5(t["directory"]) for t in doc["tracks"]}
    sibling = os.path.join(root, "01", "audio", "drums.wav")
    sibling_md5 = hashlib.md5(open(sibling, "rb").read()).hexdigest()

    res = regen_track(os.path.join(root, "album.json"), position=1,
                      db_path=_DB)
    rebuilt = master_md5("02")
    assert rebuilt == before["02"], "regenerated track differs from original"
    assert hashlib.md5(open(sibling, "rb").read()).hexdigest() == sibling_md5, \
        "sibling track was modified by regen"
    assert sorted(res["exports"]) == ["ableton", "manifest", "midi", "reaper"]
    print("  regen_track: byte-identical rebuild, siblings untouched: OK")


def test_palette_propagates_and_records():
    from timbor.album import generate_album
    cfg = _cfg(palette_mode="balanced")
    doc = generate_album(cfg, os.path.join(TMP, "pal"), render=True,
                         db_path=_DB)
    root = doc["root"]
    assert doc["palette"]["roles"], "expected a non-empty palette"
    # count tracks using the signature break
    pal_break = next(e for e in doc["palette"]["roles"].values()
                     if "main_break" in e.get("roles", []))
    users = 0
    for t in doc["tracks"]:
        pj = json.load(open(os.path.join(root, t["directory"],
                                         "project.json"), encoding="utf-8"))
        paths = {os.path.basename(s["path"]) for s in pj.get("samples", [])}
        if os.path.basename(pal_break["path"]) in paths:
            users += 1
    assert users >= 1, "palette break must reach at least one track"
    assert doc["validation"] is not None
    errors = [i for i in doc["validation"] if i["severity"] == "error"]
    assert not errors, f"unexpected errors: {errors}"
    print(f"  palette propagation (break in {users}/2 tracks, 0 errors): OK")


def test_single_track_album():
    from timbor.album import generate_album
    cfg = _cfg(tracks=1, genres=["gabber"])
    doc = generate_album(cfg, os.path.join(TMP, "single"), render=True,
                         db_path=_DB)
    assert len(doc["tracks"]) == 1
    assert doc["tracks"][0]["lineage"]["variant"] == "A"
    assert not [i for i in doc["validation"] if i["severity"] == "error"]
    print("  single-track album edge case: OK")


def main() -> int:
    print("test_album_generation")
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
    print("All album generation tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
