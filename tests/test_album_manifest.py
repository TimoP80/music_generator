"""tests.test_album_manifest — EP-level manifest + palette provenance.

Run:  python tests/test_album_manifest.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="album_manifest_")
FAILED = []

_LIB = None
_DB = None


def _library():
    global _LIB, _DB
    if _DB is None:
        from timbor.samples.index import index_library
        ws = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _LIB = os.path.join(TMP, "lib")
        shutil.copytree(os.path.join(ws, "data", "demo_library"), _LIB)
        _DB = os.path.join(TMP, "samples.db")
        index_library(_LIB, db_path=_DB, verbose=False)
    return _LIB, _DB


def _album():
    from timbor.album import AlbumConfig, generate_album
    lib, db = _library()
    cfg = AlbumConfig(name="Manifest EP", seed=424242, tracks=2,
                      genres=["gabber", "jungle"], bpm_center=170,
                      bpm_range=10, key="D", mode="minor", mood="dark",
                      duration_range=[28, 40], sample_dir=lib)
    doc = generate_album(cfg, os.path.join(TMP, "ep"), render=True, db_path=db)
    return doc


def test_manifest_structure_and_provenance():
    from timbor.album import build_manifest
    doc = _album()
    m = build_manifest(doc, doc["root"])
    assert m["format"] == "timbor-album-manifest" and m["version"] == 1
    assert m["name"] == "Manifest EP"
    # DNA block (spec §5 provenance)
    for k in ("root_key", "mode", "bpm_center", "harmonic_family",
              "motif_family", "rhythm_fingerprint", "signature_instruments",
              "dna_hash"):
        assert k in m["dna"], f"dna missing {k}"
    # per-track records (spec §8)
    t = m["tracks"][0]
    for k in ("number", "title", "genre", "seed", "bpm", "lineage"):
        assert k in t, f"track record missing {k}"
    assert t["lineage"]["identity_score"] == 1.0
    # palette provenance (spec §11)
    entries = m["palette"]["entries"]
    assert isinstance(entries, list)
    for e in entries:
        for k in ("id", "path", "filename", "category", "bpm", "key",
                  "selection_score", "tracks_using", "placements", "roles"):
            assert k in e, f"palette entry missing {k}"
    # every rendered track that used a palette sample is recorded
    break_e = next((e for e in entries if "main_break" in e.get("roles", [])),
                   None)
    if break_e and break_e["placements"]:
        assert break_e["tracks_using"], "placements imply tracks_using"
    print("  manifest structure + DNA/track/palette provenance: OK")


def test_manifest_deterministic_bytes():
    from timbor.album import build_manifest
    doc = _album()
    m1 = json.dumps(build_manifest(doc, doc["root"]), indent=1, sort_keys=True)
    m2 = json.dumps(build_manifest(doc, doc["root"]), indent=1, sort_keys=True)
    assert hashlib.md5(m1.encode()).hexdigest() == \
        hashlib.md5(m2.encode()).hexdigest(), "manifest bytes not deterministic"
    assert "generated_at" not in m1, "manifest must not embed wall-clock time"
    print("  manifest bytes deterministic, timestamp-free: OK")


def test_export_manifest_file():
    from timbor.album import export_manifest, load_album
    doc = _album()
    p1 = export_manifest(doc, doc["root"])
    assert os.path.isfile(p1) and p1.endswith("_manifest.json")
    h1 = hashlib.md5(open(p1, "rb").read()).hexdigest()
    # reload from disk (as the CLI does) and export again
    album = load_album(os.path.join(doc["root"], "album.json"))
    p2 = export_manifest(album, doc["root"])
    assert os.path.abspath(p1) == os.path.abspath(p2)
    h2 = hashlib.md5(open(p2, "rb").read()).hexdigest()
    # both exports come from the same on-disk album.json content
    assert h1 == h2, f"manifest differs between exports ({h1} vs {h2})"
    print("  export_manifest writes stable file (reload-equal): OK")


def main() -> int:
    print("test_album_manifest")
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
    print("All album manifest tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
