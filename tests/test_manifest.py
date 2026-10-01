"""tests.test_manifest — DAW-neutral export manifest: schema completeness,
determinism, event/sample fidelity.

Run:  python tests/test_manifest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.timeline import Timeline, TimelineEvent, SectionSpan, SampleUse
from timbor.timeline.serialization import load_project, PROJECT_FORMAT, PROJECT_VERSION
from timbor.export.manifest import build_manifest, export_manifest
from timbor.export.ableton import export_ableton

TMP = None
FAILED = []


def _project(path: str) -> dict:
    t = Timeline(128.0)
    t.add_section(SectionSpan("intro", 0, 0.0, 16.0, 0.4, 4))
    t.add_section(SectionSpan("drop", 1, 16.0, 32.0, 1.0, 4))
    t.add(TimelineEvent(start_beat=0.0, duration_beats=0.25, bus="drums",
                        type="kick", role="kick", section="intro", velocity=0.9))
    t.add(TimelineEvent(start_beat=0.0, duration_beats=1.0, bus="bass",
                        type="bass", role="bass", pitch=31.0, velocity=0.8,
                        section="intro"))
    t.add(TimelineEvent(start_beat=16.0, duration_beats=0.5, bus="mel",
                        type="melody", role="lead", pitch=67.0, velocity=0.7,
                        section="drop"))
    t.add(TimelineEvent(start_beat=16.0, duration_beats=4.0, bus="samples",
                        type="breakbeat", role="main_break", section="drop",
                        sample_id="s9", source_path="lib/loop.wav",
                        pitch_semitones=1.0, stretch_ratio=0.98,
                        reverse=True, chop_ops={"reverse": 0.25}))
    t.add(TimelineEvent(start_beat=16.0, duration_beats=4.0, bus="fx",
                        type="impact", role="impact", section="drop",
                        instrument="impact"))
    t.register_sample(SampleUse(id="s9", path="lib/loop.wav", filename="loop.wav",
                                file_size=999, mtime=123.0, bpm=128.0,
                                key="G"))
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
        "song": {"genre": "jungle", "bpm": 128.0, "key": "G", "scale": "minor",
                 "progression": "i-VI", "motif": [0, 3],
                 "bass_instrument": "reese", "bass_style": "rolling",
                 "lead_instrument": "stab", "duration_seconds": 15.0,
                 "total_bars": 8},
        "plan": {"prompt": "test", "authenticity": "hybrid", "era": "classic",
                 "secondary_genres": [], "variants": {}},
        "sections": [s.to_json() for s in t.sections],
        "timeline": t.to_json()["events"], "samples": t.to_json()["samples"],
        "stems": [], "audio_dir": "audio", "qc": {"notes": []},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=1)
    return load_project(path)


def test_schema_completeness():
    p = _project(os.path.join(TMP, "p1", "project.json"))
    m = build_manifest(p)
    assert m["format"] == "timbor-export-manifest" and m["version"] == 1
    assert m["tempo"] == {"bpm": 128.0, "time_signature": "4/4"}
    assert m["key"] == {"root": "G", "scale": "minor"}
    assert m["source_project"]["generator"]["name"] == "TIMBOR"
    assert m["source_project"]["generator"]["seed"] == 424242
    assert [s["name"] for s in m["sections"]] == ["intro", "drop"]
    assert m["project_length_beats"] == 32.0
    buses = {t["bus"] for t in m["tracks"]}
    assert buses == {"drums", "bass", "mel", "fx", "samples", "master"}
    by_bus = {t["bus"]: t for t in m["tracks"]}
    assert by_bus["samples"]["events"] == 1
    assert by_bus["drums"]["events"] == 1
    # event fidelity
    ev = next(e for e in m["events"] if e["bus"] == "samples")
    assert ev["sample"]["id"] == "s9"
    assert ev["sample"]["path"] == "lib/loop.wav"
    assert ev["pitch_semitones"] == 1.0 and ev["stretch_ratio"] == 0.98
    assert ev["reverse"] is True and ev["chop_ops"] == {"reverse": 0.25}
    mel_ev = next(e for e in m["events"] if e["bus"] == "mel")
    assert mel_ev["pitch"] == 67.0 and mel_ev["velocity"] == 0.7
    # samples registry
    s = m["samples"][0]
    assert s["id"] == "s9" and s["filename"] == "loop.wav" and s["bpm"] == 128.0
    print("  schema: OK (format v1, 6 buses, event+sample fidelity)")


def test_no_timestamps_and_sorted():
    p = _project(os.path.join(TMP, "p2", "project.json"))
    raw = json.dumps(build_manifest(p), sort_keys=True)
    for banned in ("generated_at", "timestamp", "created", "modified"):
        assert banned not in raw, banned
    events = build_manifest(p)["events"]
    keys = [(e["start_beat"], e["id"]) for e in events]
    assert keys == sorted(keys), "events sorted by (start_beat, id)"
    print("  determinism fields: OK (no timestamps, canonical order)")


def test_export_file_deterministic():
    p = _project(os.path.join(TMP, "p3", "project.json"))
    d1 = os.path.join(TMP, "e1")
    d2 = os.path.join(TMP, "e2")
    f1 = export_manifest(p, d1, name="proj")
    f2 = export_manifest(p, d2, name="proj")
    assert os.path.basename(f1) == "proj_export.json"
    assert open(f1, "rb").read() == open(f2, "rb").read(), "manifest bytes differ"
    loaded = json.load(open(f1, encoding="utf-8"))
    assert loaded["tempo"]["bpm"] == 128.0
    print(f"  file export: OK ({os.path.getsize(f1)} bytes, byte-identical)")


def test_ableton_kit():
    # two same-named project roots in different parents -> byte-identical kits
    root1 = os.path.join(TMP, "ka", "proj_kit")
    root2 = os.path.join(TMP, "kb", "proj_kit")
    p = _project(os.path.join(root1, "project.json"))
    kit = export_ableton(p, root1)
    assert os.path.isdir(kit)
    files = set()
    for dp, _dn, fns in os.walk(kit):
        for fn in fns:
            files.add(os.path.relpath(os.path.join(dp, fn), kit).replace("\\", "/"))
    assert {"README.md", "manifest.json", "stems.md", "samples.csv",
            "midi/proj_kit.mid"} <= files, files
    # no fake .als, ever
    assert not any(f.endswith(".als") for f in files), "must not fake .als"
    readme = open(os.path.join(kit, "README.md"), encoding="utf-8").read()
    assert "does not write" in readme and "128.0 BPM" in readme
    # manifest.json identical to export_manifest bytes
    f = export_manifest(p, os.path.join(root1, "export"), name="proj_kit")
    kit_manifest = open(os.path.join(kit, "manifest.json"), "rb").read()
    assert kit_manifest == open(f, "rb").read(), "kit manifest differs"
    # midi identical to build_midi
    from timbor.export.midi import build_midi
    assert open(os.path.join(kit, "midi", "proj_kit.mid"),
                "rb").read() == build_midi(p)
    # cue sheet covers the sample event
    csv_txt = open(os.path.join(kit, "samples.csv"), encoding="utf-8").read()
    assert "loop.wav" in csv_txt and "main_break" in csv_txt
    assert csv_txt.count(",") >= 5, "csv columns present"
    # determinism across two kit builds (same project, same root name)
    kit2 = export_ableton(p, root2)
    for rel in files:
        a = open(os.path.join(kit, rel.replace("/", os.sep)), "rb").read()
        b = open(os.path.join(kit2, rel.replace("/", os.sep)), "rb").read()
        assert a == b, f"kit file not deterministic: {rel}"
    print(f"  ableton kit: OK ({len(files)} files, no .als, deterministic)")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="timbor_manifest_")
    try:
        for t in (test_schema_completeness, test_no_timestamps_and_sorted,
                  test_export_file_deterministic, test_ableton_kit):
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
    print("\nAll manifest export tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
