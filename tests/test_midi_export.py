"""tests.test_midi_export — SMF export: header, tracks, tempi, markers,
deterministic bytes, event placement.

Run:  python tests/test_midi_export.py
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor.timeline import Timeline, TimelineEvent, SectionSpan, SampleUse
from timbor.timeline.serialization import load_project, PROJECT_FORMAT, PROJECT_VERSION
from timbor.export.midi import build_midi, export_midi, PPQ

TMP = None
FAILED = []


def _write_project(path: str, bpm: float, key: str, scale: str,
                   sections: list, events: list, samples: list) -> dict:
    """Hand-build a minimal v1 project.json (the export modules consume the
    dict schema, and load_project enforces the format/version gate)."""
    import json
    project = {
        "format": PROJECT_FORMAT, "version": PROJECT_VERSION,
        "generator": {"name": "TIMBOR", "version": "0.3.0",
                      "python": "3.x", "os": "test", "sample_rate": 44100,
                      "seed": 424242, "generated_at": "2026-01-01T00:00:00",
                      "genre": "gabber", "mood": "dark", "era": "classic",
                      "sample_mode": "balanced", "sample_library": None,
                      "kit_seed": 7, "kick_type": "distorted",
                      "drums_kind": "909"},
        "song": {"genre": "gabber", "bpm": bpm, "key": key, "scale": scale,
                 "progression": "i-VI-III-VII", "motif": [0, 3, 7],
                 "bass_instrument": "rumble", "bass_style": "offbeat",
                 "lead_instrument": "supersaw", "duration_seconds": 6.86,
                 "total_bars": 4},
        "plan": {"prompt": "test", "authenticity": "hybrid", "era": "classic",
                 "secondary_genres": [], "variants": {}},
        "sections": sections, "timeline": events, "samples": samples,
        "stems": [], "audio_dir": "audio", "qc": {"notes": []},
    }
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=1)
    return load_project(path)


def _tiny_project(path: str) -> dict:
    t = Timeline(140.0)
    t.add_section(SectionSpan("intro", 0, 0.0, 8.0, 0.4, 2))
    t.add_section(SectionSpan("drop", 1, 8.0, 16.0, 1.0, 2))
    t.add(TimelineEvent(start_beat=0.0, duration_beats=0.25, bus="drums",
                        type="kick", role="kick", section="intro", velocity=0.9))
    t.add(TimelineEvent(start_beat=1.0, duration_beats=0.25, bus="drums",
                        type="snare", role="snare", section="intro", velocity=0.8))
    t.add(TimelineEvent(start_beat=2.0, duration_beats=0.25, bus="drums",
                        type="hat", role="hat", section="drop", velocity=0.7))
    t.add(TimelineEvent(start_beat=0.0, duration_beats=1.0, bus="bass",
                        type="bass", role="bass", pitch=33.0, velocity=0.8,
                        section="intro"))
    t.add(TimelineEvent(start_beat=8.0, duration_beats=0.5, bus="mel",
                        type="melody", role="lead", pitch=69.0, velocity=0.8,
                        section="drop"))
    t.add(TimelineEvent(start_beat=8.0, duration_beats=2.0, bus="mel",
                        type="stab", role="stab", section="drop",
                        metadata={"extra": {"notes": [69, 72, 76]}}))
    t.add(TimelineEvent(start_beat=8.0, duration_beats=4.0, bus="samples",
                        type="breakbeat", role="main_break", section="drop",
                        sample_id="s1", source_path="lib/amen.wav",
                        pitch_semitones=-2.0, stretch_ratio=1.03))
    t.add(TimelineEvent(start_beat=8.0, duration_beats=8.0, bus="fx",
                        type="riser", role="riser", section="drop",
                        instrument="riser"))
    t.register_sample(SampleUse(id="s1", path="lib/amen.wav", filename="amen.wav",
                                file_size=1, mtime=0.0, bpm=170.0))
    return _write_project(path, 140.0, "A", "minor",
                          [s.to_json() for s in t.sections],
                          t.to_json()["events"], t.to_json()["samples"])


def _chunks(data: bytes):
    """Yield (chunk_id, payload) pairs from a MIDI byte stream."""
    i = 0
    while i < len(data):
        cid = data[i:i + 4]
        n = struct.unpack(">I", data[i + 4:i + 8])[0]
        yield cid, data[i + 8:i + 8 + n]
        i += 8 + n


def test_header_and_track_count():
    p = _tiny_project(os.path.join(TMP, "tiny", "project.json"))
    data = build_midi(p)
    assert data[:4] == b"MThd" and data[14:18] == b"MTrk", "MThd/MTrk chunks"
    fmt, ntrk, division = struct.unpack(">HHH", data[8:14])
    assert fmt == 1, f"format 1, got {fmt}"
    assert division == PPQ == 480, f"480 PPQ, got {division}"
    chunks = list(_chunks(data))
    assert len(chunks) == 8, f"7 tracks + header, got {len(chunks)}"
    names = []
    for cid, payload in chunks[1:]:
        assert cid == b"MTrk"
        assert b"\xff\x2f\x00" in payload, "every track has EOT"
        assert payload[0] == 0x00 and payload[1] == 0xFF and payload[2] == 0x03, \
            "first event is a track-name meta"
        ln = payload[3]
        names.append(payload[4:4 + ln].decode())
    assert names == ["TIMBOR", "Drums", "Bass", "Melody", "Chords", "FX",
                     "Samples"], names
    print("  header/tracks: OK (format 1, 480 PPQ, 7 named tracks)")


def test_tempo_key_markers():
    p = _tiny_project(os.path.join(TMP, "tiny2", "project.json"))
    data = build_midi(p)
    conductor = next(payload for cid, payload in _chunks(data)
                     if cid == b"MTrk" and b"TIMBOR" in payload[:16])
    # tempo meta: FF 51 03 usec-per-quarter = 60e6/140 = 428571
    tempo_us = 60_000_000 // 140
    assert struct.pack(">I", tempo_us)[1:4] in conductor, "tempo event"
    assert b"\xff\x59\x02" in conductor, "key signature event"
    assert b"INTRO" in conductor and b"DROP" in conductor, "section markers"
    # markers are text meta (FF 06) — first lands at beat 0 (delta 0)
    assert bytes([0x00, 0xFF, 0x06, 0x05]) + b"INTRO" in conductor
    print("  tempo/key/markers: OK (428571 usec/qn, A-minor, INTRO+DROP)")


def test_note_placement_gm_drums():
    p = _tiny_project(os.path.join(TMP, "tiny3", "project.json"))
    data = build_midi(p)
    tracks = {}
    for cid, payload in _chunks(data):
        if cid != b"MTrk":
            continue
        ln = payload[3]
        tracks[payload[4:4 + ln]] = payload
    drums = tracks[b"Drums"]
    # kick GM 36 (0x24) note-on 0x99 ch10 at tick 0; snare 38 at beat 1
    assert b"\x99\x24" in drums, "kick note-on ch10 (GM ch10 = 0x99)"
    assert b"\x89\x24" in drums, "kick note-off ch10"
    assert b"\x99\x26" in drums, "snare note-on ch10 (GM 38=0x26)"
    assert b"\x99\x2a" in drums, "closed hat ch10 (GM 42=0x2a)"
    assert b"\x90" not in drums, "no ch0 notes on the drum track"
    bass = tracks[b"Bass"]
    # pitch 33 (0x21) on ch0 note-on 0x90, beat 0 -> first delta 0
    assert b"\x90\x21" in bass, "bass note A0 ch0"
    mel = tracks[b"Melody"]
    assert b"\x91\x45" in mel, "melody A4 (69) ch1"
    chords = tracks[b"Chords"]
    for nn in (69, 72, 76):
        assert bytes([0x92, nn]) in chords, f"chord note {nn} ch2"
    # samples + fx carry text meta, no notes
    samples = tracks[b"Samples"]
    assert b"[SAMPLE] amen.wav ROLE=main_break" in samples
    assert b"PITCH=-2" in samples and b"STRETCH=1.03" in samples
    note_ons = [b for b in samples if 0x80 <= b <= 0xEF]
    assert not note_ons, "Samples track has no note events (text meta only)"
    fx = tracks[b"FX"]
    assert b"[FX] role=riser" in fx
    print("  note placement: OK (GM drum map ch10, bass ch0, mel ch1, chords ch2)")


def test_deterministic_bytes_and_file():
    p = _tiny_project(os.path.join(TMP, "tiny4", "project.json"))
    a = build_midi(p)
    b = build_midi(p)
    assert a == b, "build_midi not deterministic"
    out_dir = os.path.join(TMP, "out")
    out = os.path.join(out_dir, "x.mid")
    p1 = export_midi(p, out_dir, name="x")
    p2 = export_midi(p, out_dir, name="x")
    assert p1 == p2 == out
    assert open(p1, "rb").read() == a, "file export matches build_midi"
    try:
        export_midi(p, out, name="x", midi_format=0)
        raise AssertionError("format 0 must be rejected")
    except ValueError:
        pass
    print(f"  determinism: OK ({len(a)} bytes, byte-identical, format-0 rejected)")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="timbor_midi_")
    try:
        for t in (test_header_and_track_count, test_tempo_key_markers,
                  test_note_placement_gm_drums,
                  test_deterministic_bytes_and_file):
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
    print("\nAll MIDI export tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
