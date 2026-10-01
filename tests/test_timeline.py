"""tests.test_timeline — timeline, stems, project JSON, reproducibility.

Run:  python tests/test_timeline.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor import Plan, render_track, GENRES
from timbor.render import stereoize, master, write_wav, write_wav_float32
from timbor.timeline import (Timeline, TimelineEvent, SectionSpan, SampleUse,
                             beats_to_seconds, seconds_to_beats, save_project,
                             load_project, PROJECT_FORMAT, PROJECT_VERSION)
from timbor.timeline.validation import validate_timeline, format_diagnostics
from timbor.timeline.project import write_project_directory, timeline_report

TMP = None
FAILED = []


def _md5(p: str) -> str:
    return hashlib.md5(open(p, "rb").read()).hexdigest()


# ---------------------------------------------------------------- timeline

def test_beat_math():
    assert abs(beats_to_seconds(4.0, 174.0) - 4 * 60 / 174) < 1e-9
    assert abs(seconds_to_beats(beats_to_seconds(7.0, 120.0), 120.0) - 7.0) < 1e-9
    t = Timeline(174.0)
    assert abs(t.duration_seconds() - 0.0) < 1e-9
    print("  beat math: OK")


def test_event_ordering_and_roundtrip():
    t = Timeline(120.0)
    t.add_section(SectionSpan("drop", 0, 16.0, 32.0, 1.0, 4))
    t.add_section(SectionSpan("intro", 1, 0.0, 16.0, 0.3, 4))
    e1 = t.add(TimelineEvent(start_beat=4.0, duration_beats=0.25, bus="drums",
                             type="kick", role="kick", section="intro", velocity=0.9))
    e2 = t.add(TimelineEvent(start_beat=2.0, duration_beats=0.25, bus="bass",
                             type="bass", role="bass", pitch=45.0, velocity=0.8))
    e3 = t.add(TimelineEvent(start_beat=8.0, duration_beats=4.0, bus="samples",
                             type="breakbeat", role="main_break",
                             sample_id="s1", source_path="lib/amen.wav",
                             pitch_semitones=-2.0, stretch_ratio=1.03,
                             chop_ops={"reverse": 0.3}))
    t.register_sample(SampleUse(id="s1", path="lib/amen.wav", filename="amen.wav",
                                file_size=123, mtime=1.0, bpm=174.0))
    evs = t.sorted_events()
    assert [e.id for e in evs] == [2, 1, 3], "ordering by start_beat"
    j = json.dumps(t.to_json())
    t2 = Timeline.from_json(json.loads(j))
    assert json.dumps(t2.to_json()) == j, "timeline JSON round trip"
    assert t2.samples["s1"].bpm == 174.0
    assert t2.by_bus("samples")[0].chop_ops == {"reverse": 0.3}
    print("  event ordering + round trip: OK")


def test_validation():
    t = Timeline(174.0)
    t.add(TimelineEvent(start_beat=-1.0, duration_beats=0.0, bus="x", type="kick"))
    issues = validate_timeline(t)
    codes = {i["code"] for i in issues}
    assert "negative-position" in codes and "invalid-duration" in codes \
        and "bus-invalid" in codes
    t2 = Timeline(174.0)
    t2.add(TimelineEvent(start_beat=0, duration_beats=1, bus="drums", type="kick",
                         sample_id="missing"))
    codes2 = {i["code"] for i in validate_timeline(t2)}
    assert "sample-ref-missing" in codes2
    # clean timeline validates empty
    plan = Plan("dark gabber", seed=1, bars_limit=8)
    song, _, _ = render_track(plan)
    assert validate_timeline(song.timeline) == []
    print("  validation diagnostics: OK")


# ---------------------------------------------------------------- serialization

def test_project_roundtrip_and_versions():
    plan = Plan("dark gabber", seed=5, bars_limit=8)
    song, buses, qc = render_track(plan)
    p1 = os.path.join(TMP, "p1.json")
    saved = save_project(p1, song, song.timeline, qc)
    loaded = load_project(p1)
    assert loaded["format"] == PROJECT_FORMAT and loaded["version"] == PROJECT_VERSION
    assert loaded["song"]["bpm"] == song.bpm
    assert loaded["song"]["motif"] == song.motif
    # mutating version fields is rejected
    data = json.load(open(p1))
    data["version"] = PROJECT_VERSION + 1
    bad = os.path.join(TMP, "bad.json")
    json.dump(data, open(bad, "w"))
    try:
        load_project(bad)
        raise AssertionError("newer version must be rejected")
    except ValueError as e:
        assert "newer" in str(e)
    data["version"] = 0
    json.dump(data, open(bad, "w"))
    try:
        load_project(bad)
        raise AssertionError("older version must be rejected")
    except ValueError:
        pass
    data["format"] = "other"
    json.dump(data, open(bad, "w"))
    try:
        load_project(bad)
        raise AssertionError("wrong format must be rejected")
    except ValueError:
        pass
    print("  project round trip + version gate: OK")


# ---------------------------------------------------------------- stems

def test_stems():
    plan = Plan("dark jungle track", genre="jungle", seed=11, bars_limit=16)
    song, buses, qc = render_track(plan)
    l, r = stereoize(buses)
    l, r = master(l, r)
    d = os.path.join(TMP, "stems")
    from timbor.render import write_stems, STEM_BUSES, qc_stems
    files = write_stems(d, buses, l, r, fmt="float32")
    assert len(files) == len(STEM_BUSES), files
    import wave as wavemod
    for p in files:
        assert os.path.exists(p) and os.path.getsize(p) > 44
    # float32 header check: format tag 3, 32 bits
    with open(files[0], "rb") as f:
        hdr = f.read(44)
    assert hdr[12:16] == b"fmt "  # RIFF: 12-byte prologue before the fmt chunk
    fmt_tag = int.from_bytes(hdr[20:22], "little")
    bits = int.from_bytes(hdr[34:36], "little")
    assert fmt_tag == 3 and bits == 32, (fmt_tag, bits)
    # master equals what stereoize+master produced (same length)
    m = os.path.join(d, "master.wav")
    with open(m, "rb") as f:
        f.seek(40)
        data_n = int.from_bytes(f.read(4), "little")
    assert data_n == len(l) * 2 * 4
    # non-silent expected buses
    for bus in ("drums", "bass", "mel", "fx"):
        b = buses[bus]
        assert float(np.sqrt(np.mean(b ** 2))) > 1e-5, bus
    notes = qc_stems(buses, l, r)
    assert isinstance(notes, list)
    print("  stems: OK")


# ---------------------------------------------------------------- reproducibility

def test_reproducibility():
    ws_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib = os.path.join(ws_root, "data", "demo_library")
    if not os.path.isdir(lib):
        print("  reproducibility: SKIPPED (no demo library)")
        return
    from timbor.samples.cache import SampleIndex
    idx = SampleIndex()
    hashes = []
    for run in range(2):
        plan = Plan("1994 Rotterdam gabber anthem, dark but euphoric",
                    seed=424242)
        plan.sample_dir = lib  # mirrors what the CLI does
        song, buses, qc = render_track(plan, sample_index=idx)
        l, r = stereoize(buses)
        l, r = master(l, r)
        d = os.path.join(TMP, f"repro_{run}")
        write_project_directory(os.path.join(d, "audio", "master.wav"), song, qc,
                                buses, l, r)
        # project.json carries a generation timestamp by design (metadata only,
        # spec §25) — normalize it before hashing so the comparison targets
        # the actual musical content
        pdata = json.load(open(os.path.join(d, "project.json")))
        pdata["generator"].pop("generated_at", None)
        pnorm = os.path.join(d, "project.norm.json")
        json.dump(pdata, open(pnorm, "w"), sort_keys=True)
        hashes.append({
            "master": _md5(os.path.join(d, "audio", "master.wav")),
            "drums": _md5(os.path.join(d, "audio", "drums.wav")),
            "samples": _md5(os.path.join(d, "audio", "samples.wav")),
            "project": _md5(pnorm),
            "plan": json.dumps({"bpm": song.bpm, "key": song.key,
                                "motif": song.motif,
                                "sections": [s.name for s in song.sections]}),
            "timeline": len(song.timeline.events),
            "assignments": [(a["role"], a["sample"].path)
                            for a in song.sample_assignments],
        })
    idx.close()
    a, b = hashes
    assert a["plan"] == b["plan"], "SongPlan differs between runs"
    assert a["timeline"] == b["timeline"], "timeline length differs"
    assert a["assignments"] == b["assignments"], "sample selections differ"
    assert a["project"] == b["project"], "project.json differs (beyond timestamp)"
    for k in ("master", "drums", "samples"):
        assert a[k] == b[k], f"{k} hash differs between runs"
    print(f"  reproducibility: OK (master {a['master'][:12]}…, "
          f"drums {a['drums'][:12]}…, {a['timeline']} events)")
    test_reproducibility.hashes = a


def test_project_directory_layout():
    plan = Plan("euphoric trance", seed=9, bars_limit=16)
    song, buses, qc = render_track(plan)
    l, r = stereoize(buses)
    l, r = master(l, r)
    root = os.path.join(TMP, "proj_layout")
    pj = write_project_directory(os.path.join(root, "audio", "master.wav"),
                                 song, qc, buses, l, r)
    assert os.path.exists(pj)
    assert os.path.exists(os.path.join(root, "audio", "drums.wav"))
    assert os.path.exists(os.path.join(root, "audio", "samples.wav"))
    proj = load_project(pj)
    rep = timeline_report(proj)
    assert "TIMBOR TIMELINE" in rep and "EVENTS" in rep
    print("  project directory layout: OK")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="timbor_tl_")
    try:
        for t in (test_beat_math, test_event_ordering_and_roundtrip,
                  test_validation, test_project_roundtrip_and_versions,
                  test_stems, test_reproducibility, test_project_directory_layout):
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
    print("\nAll timeline/stem/project tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
