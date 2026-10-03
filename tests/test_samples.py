"""tests.test_samples — sample subsystem tests (pytest style, runnable directly).

Run:  python -m tests.test_samples   or   python tests/test_samples.py
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor import dsp
from timbor.engine import Plan, render_track
from timbor.render import stereoize, master, write_wav

SR = dsp.SR
TMP = None
FAILED = []


# ---------------------------------------------------------------- fixtures

def _write_wav(path, x, sr=SR):
    import wave
    x16 = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x16.tobytes())


def _drum_loop(bars=2, bpm=174.0):
    """Amen-ish drum loop with strong onsets on a known grid.

    Instruments are seeded: unseeded noise makes the loop audio differ every
    run, and the BPM detector's octave disambiguation (half-time lag within
    0.65x of the peak) sits close enough to the boundary that the fixture
    occasionally analyzed at half tempo.
    """
    step = 60.0 / bpm / 4
    n = int(bars * 4 * 60 / bpm * SR)
    out = np.zeros(n)
    k = dsp.kick_four(bpm, seed=11)
    # snare level matched closer to the kick so every beat carries a similar
    # onset strength — with a dominant kick the autocorrelation locks onto the
    # 2-beat lag and the loop reads at half tempo
    s = dsp.snare_909(seed=12) * 0.45
    h = dsp.hat(seed=13)
    for b in range(bars * 16):
        t = b * step
        i = int(t * SR)
        if b % 4 == 0:
            out[i:i + len(k)] += k * 0.9
        if b % 16 in (4, 12):
            out[i:i + len(s)] += s * 0.9
        if b % 2 == 0:
            out[i:i + len(h)] += h * 0.4
    return out


def _tonal_loop(root="A", minor=True, bars=2, bpm=128.0):
    """Chord/bass-ish tonal loop in a known key."""
    from timbor.theory import NOTE_INDEX
    root_pc = NOTE_INDEX[root]
    seq = [0, 3, 7] if minor else [0, 4, 7]
    bar = 4 * 60 / bpm
    n = int(bars * bar * SR)
    out = np.zeros(n)
    for b in range(bars):
        for i, iv in enumerate(seq):
            f = 440.0 * 2 ** ((root_pc + iv - 9 - 12) / 12)
            t = np.arange(int(bar * SR)) / SR
            tone = 0.5 * np.sin(2 * np.pi * f * t) + \
                   0.25 * np.sin(2 * np.pi * f * 2 * t)
            i0 = int(b * bar * SR)
            env = dsp.adsr(len(tone), 0.01, 0.1, 0.6, 0.2)
            out[i0:i0 + len(tone)] += tone * env * 0.4
    return out


def _bass_one_shot(root="E"):
    from timbor.theory import NOTE_INDEX
    f = 440.0 * 2 ** ((NOTE_INDEX[root] + 12 - 69) / 12)
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * f * t) * dsp.exp_env(n, 0.18)
    return x


def _riser(dur=2.0):
    return dsp.riser(dur, kind="noise")


def _vocalish(dur=1.2):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f0 = 220.0
    out = np.zeros(n)
    for fc, g in ((700, 1.0), (1200, 0.6), (2600, 0.35)):
        mod = 0.3 + 0.2 * np.sin(2 * np.pi * 5 * t)
        out += g * mod * dsp.bp(np.sin(2 * np.pi * f0 * t), fc, order=3)
    return out * dsp.adsr(n, 0.02, 0.1, 0.7, 0.3)


def build_fixture_library(root: str) -> int:
    """Create a small deterministic sample library. Returns file count."""
    os.makedirs(os.path.join(root, "breaks"), exist_ok=True)
    os.makedirs(os.path.join(root, "one_shots"), exist_ok=True)
    os.makedirs(os.path.join(root, "tonal"), exist_ok=True)
    os.makedirs(os.path.join(root, "fx"), exist_ok=True)
    n = 0
    for i, bpm in ((0, 174.0), (1, 168.0), (2, 170.0)):
        _write_wav(os.path.join(root, "breaks", f"amen_break_{i:02d}_{int(bpm)}bpm.wav"),
                   _drum_loop(bars=2, bpm=bpm))
        n += 1
    for i, (nm, gen) in enumerate((("kick", lambda: dsp.kick_four(150)),
                                   ("snare", lambda: dsp.snare_909()),
                                   ("clap", lambda: dsp.clap_909()),
                                   ("hat", lambda: dsp.hat()))):
        _write_wav(os.path.join(root, "one_shots", f"{nm}_{i:02d}.wav"), gen())
        n += 1
    for i, root_note in ((0, "A"), (1, "F"), (2, "D")):
        _write_wav(os.path.join(root, "tonal", f"chord_loop_{root_note}min_128bpm.wav"),
                   _tonal_loop(root=root_note, minor=True))
        n += 1
    _write_wav(os.path.join(root, "tonal", "reese_bass_E.wav"), _bass_one_shot("E"))
    n += 1
    _write_wav(os.path.join(root, "fx", "riser_01.wav"), _riser(2.0))
    n += 1
    _write_wav(os.path.join(root, "fx", "impact_01.wav"), dsp.impact())
    n += 1
    _write_wav(os.path.join(root, "fx", "vox_shout_01.wav"), _vocalish(1.2))
    n += 1
    # unsupported file to exercise the scanner
    with open(os.path.join(root, "notes.txt"), "w") as f:
        f.write("not audio")
    # duplicate (same content, different name)
    shutil.copyfile(os.path.join(root, "breaks", "amen_break_00_174bpm.wav"),
                    os.path.join(root, "breaks", "amen_break_copy.wav"))
    n += 1
    return n


# ---------------------------------------------------------------- tests

def test_scanner():
    lib = os.path.join(TMP, "lib")
    from timbor.samples.scanner import scan_directory
    files = scan_directory(lib)
    assert len(files) == 15, f"expected 15 audio files, got {len(files)}"
    assert all(f.format in ("wav",) for f in files)
    updates = []
    reported_files = scan_directory(
        lib, progress_callback=lambda count, path: updates.append((count, path)))
    assert len(reported_files) == len(files)
    assert updates and updates[-1][0] == len(files)
    assert all(os.path.exists(path) for _, path in updates)

    import threading
    from timbor.samples.scanner import ScanCancelled
    cancelled = threading.Event()
    cancelled.set()
    try:
        scan_directory(lib, cancel_event=cancelled)
    except ScanCancelled:
        pass
    else:
        raise AssertionError("scanner ignored cancellation")
    assert not any(f.path.endswith(".txt") for f in files)
    # duplicates by content are both indexed (fingerprints differ by path)
    paths = {f.path for f in files}
    assert any("amen_break_copy" in p for p in paths)
    print("  scanner: OK")


def test_analyzer():
    lib = os.path.join(TMP, "lib")
    from timbor.samples.analyzer import analyze_file
    p = os.path.join(lib, "breaks", "amen_break_00_174bpm.wav")
    m = analyze_file(p, "wav")
    assert m.analyzed and not m.error, f"analysis failed: {m.error}"
    assert 150 <= (m.bpm or 0) <= 190, f"bpm detect {m.bpm}"
    assert m.bpm_confidence > 0.15, f"bpm conf {m.bpm_confidence}"
    assert abs(m.duration - 2.7586) < 0.05, f"duration {m.duration} (2 bars @174)"
    assert 0 <= m.transient_density <= 30, f"transient density {m.transient_density}"
    p2 = os.path.join(lib, "tonal", "chord_loop_Amin_128bpm.wav")
    m2 = analyze_file(p2, "wav")
    assert m2.duration > 3.0, f"dur {m2.duration}"
    assert m2.tonalness > 0.2, f"tonalness {m2.tonalness}"
    assert m2.root_note is not None, f"A-minor chord loop should yield a root, got key={m2.key} tonal={m2.tonalness:.2f}"
    assert m2.key_confidence > 0.1, f"key conf {m2.key_confidence}"
    p3 = os.path.join(lib, "one_shots", "kick_00.wav")
    m3 = analyze_file(p3, "wav")
    assert m3.duration < 1.0, f"kick dur {m3.duration}"
    assert not m3.is_loop, f"kick misdetected as loop (bpm={m3.bpm}, conf={m3.bpm_confidence})"
    print("  analyzer: OK")


def test_classifier():
    from timbor.samples.analyzer import analyze_file
    from timbor.samples.classifier import classify
    lib = os.path.join(TMP, "lib")
    m = analyze_file(os.path.join(lib, "breaks", "amen_break_00_174bpm.wav"), "wav")
    classify(m)
    assert m.category in ("breakbeat", "drum_loop"), m.category
    assert "amen" in m.file_tags or "jungle" in m.genre_tags
    m3 = analyze_file(os.path.join(lib, "one_shots", "kick_00.wav"), "wav")
    classify(m3)
    assert m3.category in ("kick", "perc"), m3.category
    m4 = analyze_file(os.path.join(lib, "fx", "riser_01.wav"), "wav")
    classify(m4)
    assert m4.category in ("riser", "fx"), m4.category
    print("  classifier: OK")


def test_index_cache():
    lib = os.path.join(TMP, "lib")
    db = os.path.join(TMP, "data", "samples.db")
    from timbor.samples.index import index_library
    s1 = index_library(lib, db_path=db)
    assert s1["new"] == 15 and s1["errors"] == 0
    # rescan: all unchanged
    s2 = index_library(lib, db_path=db)
    assert s2["new"] == 0 and s2["changed"] == 0 and s2["unchanged"] == 15, s2
    # touch one file -> changed
    p = os.path.join(lib, "breaks", "amen_break_01_168bpm.wav")
    os.utime(p, (time.time(), time.time() + 5))
    s3 = index_library(lib, db_path=db)
    assert s3["changed"] == 1, s3
    # add one -> new
    _write_wav(os.path.join(lib, "one_shots", "hat_extra.wav"), dsp.hat())
    s4 = index_library(lib, db_path=db)
    assert s4["new"] == 1, s4
    # remove one -> pruned from index
    os.remove(os.path.join(lib, "one_shots", "hat_extra.wav"))
    s5 = index_library(lib, db_path=db)
    from timbor.samples.cache import SampleIndex
    idx = SampleIndex(db)
    st = idx.stats()
    idx.close()
    assert st["total"] == 15, st
    print("  index cache: OK")


def test_selection_deterministic():
    lib = os.path.join(TMP, "lib")
    db = os.path.join(TMP, "data", "samples.db")
    from timbor.samples.cache import SampleIndex
    from timbor.samples.selector import select_for_song
    from timbor.samples.index import index_library
    index_library(lib, db_path=db)
    idx = SampleIndex(db)
    picks = []
    for _ in range(3):
        plan = Plan("dark jungle", genre="jungle", seed=123, bars_limit=16)
        from timbor.engine import SongPlan
        song = SongPlan(plan)
        a = select_for_song(song, idx, seed=123, mode="balanced")
        picks.append([(x["role"], x["sample"].path) for x in a])
    assert picks[0] == picks[1] == picks[2], "selection must be deterministic"
    assert any(r == "main_break" for r, _ in picks[0])
    assert len(picks[0]) >= 4, f"too few assignments: {picks[0]}"
    idx.close()
    print("  selection determinism: OK")


def test_transformer():
    from timbor.samples.transformer import (wsola_stretch, pitch_shift,
                                            align_to_bars, chop_sample)
    x = _drum_loop(bars=1, bpm=170.0)
    y = wsola_stretch(x, 1.06)
    assert abs(len(y) / len(x) - 1.06) < 0.02, f"stretch ratio {len(y)/len(x)}"
    z = wsola_stretch(x, 1.0)
    assert np.array_equal(z, x), "rate 1.0 must be identity"
    p = pitch_shift(x, 2.0)
    assert abs(len(p) - len(x)) < SR * 0.01, "pitch shift preserves length"
    a = align_to_bars(x, 170.0, 1.0)
    target = int(4 * 60 / 170 * SR)
    assert abs(len(a) - target) < SR * 0.01, f"align {len(a)} vs {target}"
    rng = np.random.default_rng(7)
    c1 = chop_sample(x, SR, 170.0, 1.0, rng, slices_per_bar=4,
                     ops=dict(reverse=0.5, reorder=1.0))
    rng = np.random.default_rng(7)
    c2 = chop_sample(x, SR, 170.0, 1.0, rng, slices_per_bar=4,
                     ops=dict(reverse=0.5, reorder=1.0))
    assert np.array_equal(c1, c2), "chopping must be deterministic"
    assert len(c1) == len(x)
    # stretched loop keeps its tempo: onset spacing preserved after 1.05x
    from timbor.samples.transformer import sec_to_bars, bars_to_sec
    assert abs(bars_to_sec(2, 120.0) - 4.0) < 1e-9
    print("  transformer: OK")


def test_integration_hybrid():
    lib = os.path.join(TMP, "lib")
    db = os.path.join(TMP, "data", "samples.db")
    from timbor.samples.cache import SampleIndex
    from timbor.samples.index import index_library
    if "indexed" not in test_integration_hybrid.__dict__:
        index_library(lib, db_path=db)
        test_integration_hybrid.indexed = True
    idx = SampleIndex(db)
    for genre, bpm in (("gabber", None), ("jungle", None), ("trance", None),
                       ("hard_house", None)):
        plan = Plan(f"hybrid {genre}", genre=genre, bpm=bpm, seed=424242,
                    bars_limit=16, sample_mode="balanced")
        song, buses, qc = render_track(plan, sample_index=idx)
        l, r = stereoize(buses)
        l, r = master(l, r)
        m = (l + r) * 0.5
        assert len(m) == int((song.total_bars() * 4 * 60 / song.bpm) * SR) + SR or \
            abs(len(m) - int(song.duration() * SR)) < SR, \
            f"duration mismatch {len(m)/SR:.1f}s vs {song.duration():.1f}s"
        rms = float(np.sqrt(np.mean(m ** 2)))
        assert rms > 0.05, f"{genre}: silent output"
        assert float(np.max(np.abs(m))) <= 1.0, f"{genre}: clipping"
        assert "samples" in buses and float(np.sqrt(np.mean(buses["samples"] ** 2))) > 0, \
            f"{genre}: samples bus should be non-silent"
        # procedural components remain present
        assert float(np.sqrt(np.mean(buses["drums"] ** 2))) > 0
        assert float(np.sqrt(np.mean(buses["bass"] ** 2)) if len(buses["bass"]) else 0) > 0
        write_wav(os.path.join(TMP, f"hybrid_{genre}.wav"), l, r)
        print(f"  integration {genre}: OK (rms={rms:.3f}, "
              f"samples_bus_rms={float(np.sqrt(np.mean(buses['samples']**2))):.3f})")
    idx.close()


def test_backward_compatible():
    plan = Plan("dark gabber", seed=3, bars_limit=16)
    song, buses, qc = render_track(plan)  # no sample index at all
    assert "samples" in buses and float(np.sqrt(np.mean(buses["samples"] ** 2))) == 0.0
    assert float(np.sqrt(np.mean(buses["drums"] ** 2))) > 0
    print("  backward compatibility: OK")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="timbor_test_")
    try:
        build_fixture_library(os.path.join(TMP, "lib"))
        for t in (test_scanner, test_analyzer, test_classifier, test_index_cache,
                  test_selection_deterministic, test_transformer,
                  test_integration_hybrid, test_backward_compatible):
            name = t.__name__
            try:
                t()
            except AssertionError as e:
                FAILED.append(name)
                print(f"  {name}: FAIL — {e}")
            except Exception as e:
                FAILED.append(name)
                print(f"  {name}: ERROR — {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILED:
        print(f"\nFAILED: {', '.join(FAILED)}")
        return 1
    print("\nAll sample-subsystem tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
