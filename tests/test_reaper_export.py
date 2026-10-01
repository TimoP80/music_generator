"""tests.test_reaper_export — REAPER .rpp export: structure, track names,
markers, media refs, sample-event documentation, determinism.

Run:  python tests/test_reaper_export.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.timeline import Timeline, TimelineEvent, SectionSpan, SampleUse
from timbor.timeline.serialization import load_project, PROJECT_FORMAT, PROJECT_VERSION
from timbor.export.reaper import export_reaper

TMP = None
FAILED = []


def _project(path: str) -> dict:
    t = Timeline(150.0)
    t.add_section(SectionSpan("intro", 0, 0.0, 16.0, 0.4, 4))
    t.add_section(SectionSpan("drop", 1, 16.0, 32.0, 1.0, 4))
    t.add(TimelineEvent(start_beat=0.0, duration_beats=0.25, bus="drums",
                        type="kick", role="kick", section="intro", velocity=0.9))
    t.add(TimelineEvent(start_beat=16.0, duration_beats=4.0, bus="samples",
                        type="breakbeat", role="main_break", section="drop",
                        sample_id="s1", source_path="lib/amen.wav",
                        pitch_semitones=-3.0, stretch_ratio=1.05,
                        reverse=True))
    t.add(TimelineEvent(start_beat=16.0, duration_beats=8.0, bus="fx",
                        type="riser", role="riser", section="drop",
                        instrument="riser"))
    t.register_sample(SampleUse(id="s1", path="lib/amen.wav", filename="amen.wav",
                                file_size=5, mtime=1.0, bpm=160.0))
    import json, os
    project = {
        "format": PROJECT_FORMAT, "version": PROJECT_VERSION,
        "generator": {"name": "TIMBOR", "version": "0.3.0",
                      "python": "3.x", "os": "test", "sample_rate": 44100,
                      "seed": 424242, "generated_at": "2026-01-01T00:00:00",
                      "genre": "jungle", "mood": "dark", "era": "classic",
                      "sample_mode": "balanced", "sample_library": None,
                      "kit_seed": 7, "kick_type": "sf",
                      "drums_kind": "breaks"},
        "song": {"genre": "jungle", "bpm": 150.0, "key": "A", "scale": "minor",
                 "progression": "i-VI", "motif": [0, 3],
                 "bass_instrument": "reese", "bass_style": "rolling",
                 "lead_instrument": "stab", "duration_seconds": 12.8,
                 "total_bars": 8},
        "plan": {"prompt": "test", "authenticity": "hybrid", "era": "classic",
                 "secondary_genres": [], "variants": {}},
        "sections": [s.to_json() for s in t.sections],
        "timeline": t.to_json()["events"], "samples": t.to_json()["samples"],
        "stems": [], "audio_dir": "audio",
        "qc": {"notes": ["stem qc: all stems ok"]},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=1)
    return load_project(path)


def _rpp(project: dict, root: str) -> str:
    return open(export_reaper(project, root), encoding="utf-8").read()


def test_structure_header():
    root = os.path.join(TMP, "proj_r")
    _project(os.path.join(root, "project.json"))
    txt = _rpp(_project(os.path.join(root, "project.json")), root)
    assert txt.startswith('<REAPER_PROJECT 0.1 "TIMBOR export" 1'), "RPP header"
    assert "SAMPLERATE 44100 0 0" in txt
    assert "TEMPO 150.0 4 4" in txt
    # 32 beats @150bpm = 12.8 s
    assert "LENGTH 12.800000" in txt
    print("  structure: OK (header, samplerate, tempo, length)")


def test_tracks_and_markers():
    root = os.path.join(TMP, "proj_t")
    project = _project(os.path.join(root, "project.json"))
    txt = _rpp(project, root)
    for name in ("Master", "Drums", "Bass", "Melody", "FX", "Samples",
                 "TIMBOR Sample Events"):
        assert f"NAME {name}" in txt, f"missing track {name}"
    assert "MARKER 1 0.000000 INTRO 0 R 0" in txt, "intro marker at 0"
    # drop marker: beat 16 @150bpm = 6.4 s, second marker id
    assert "MARKER 2 6.400000 DROP 0 R 0" in txt, "drop marker at 6.4s, id 2"
    import re as _re
    ids = [int(x) for x in _re.findall(r"MARKER (\d+) ", txt)]
    assert ids == list(range(1, len(ids) + 1)), f"marker ids increment: {ids}"
    assert txt.count("<TRACK") == 7, "7 tracks"
    print("  tracks/markers: OK (7 tracks, section markers at exact seconds)")


def test_media_refs_resolve():
    root = os.path.join(TMP, "proj_m")
    project = _project(os.path.join(root, "project.json"))
    os.makedirs(os.path.join(root, "audio"), exist_ok=True)
    for bus in ("drums", "bass", "mel", "fx", "samples", "master"):
        open(os.path.join(root, "audio", f"{bus}.wav"), "wb").write(b"RIFF")
    project["stems"] = [f"audio/{b}.wav" for b in
                        ("drums", "bass", "mel", "fx", "samples", "master")]
    txt = _rpp(project, root)
    assert 'SOURCE FILE "../audio/master.wav"' in txt, \
        "master media item (rpp is one level below the project root)"
    # every referenced file must exist relative to the rpp location
    import re
    rpp_path = export_reaper(project, root)
    base = os.path.dirname(rpp_path)
    refs = re.findall(r'SOURCE FILE "([^"]+)"', txt)
    assert refs, "no media refs"
    stem_refs = [r for r in refs if r.startswith("..")]
    assert len(stem_refs) == 6, f"6 stem items expected, got {len(stem_refs)}"
    # TIMBOR guarantees stem items resolve; sample-source refs point at the
    # user's library and may legitimately live elsewhere
    for ref in stem_refs:
        p = os.path.normpath(os.path.join(base, ref.replace("/", os.sep)))
        assert os.path.exists(p), f"stem ref does not resolve: {ref}"
    print(f"  media refs: OK ({len(stem_refs)} stem items resolve on disk, "
          f"{len(refs) - len(stem_refs)} library refs)")


def test_sample_events_documented():
    root = os.path.join(TMP, "proj_s")
    project = _project(os.path.join(root, "project.json"))
    txt = _rpp(project, root)
    assert "lib/amen.wav" in txt, "sample source path present"
    assert "PLAYRATE 1.05" in txt, "stretch as playrate"
    assert "PITCH" in txt and "-0.25" in txt, "semitones as semitones/octave"
    assert "reverse" in txt, "reverse documented in notes"
    print("  sample events: OK (path, playrate, pitch, reverse notes)")


def test_deterministic():
    root = os.path.join(TMP, "proj_d")
    project = _project(os.path.join(root, "project.json"))
    a = export_reaper(project, root)
    b = export_reaper(project, root)
    assert open(a, "rb").read() == open(b, "rb").read(), "rpp bytes differ"
    print(f"  determinism: OK ({os.path.getsize(a)} bytes, byte-identical)")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="timbor_rpp_")
    try:
        for t in (test_structure_header, test_tracks_and_markers,
                  test_media_refs_resolve, test_sample_events_documented,
                  test_deterministic):
            name = t.__name__
            try:
                t()
            except AssertionError as e:
                FAILED.append(name)
                print(f"  {name}: FAIL — {e}")
            except Exception as e:
                FAILED.append(name)
                import traceback
                print(f"  {name}: ERROR — {type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILED:
        print(f"\nFAILED: {', '.join(FAILED)}")
        return 1
    print("\nAll Reaper export tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
