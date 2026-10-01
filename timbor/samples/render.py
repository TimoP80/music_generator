"""timbor.samples.render — sample bus rendering + sample-aware QC.

Pipeline (spec: sample sequencing): SongPlan -> assignments -> PLACEMENTS
(pure decisions, incl. chop ops / pitch / stretch) -> audio execution.
The renderer consumes placement decisions; it makes no musical choices.
Per-placement RNG is derived from (seed, role, bar) so execution is fully
deterministic and independent of decision order.
"""
from __future__ import annotations

import zlib

import numpy as np

from timbor import dsp
from timbor.theory import NOTE_INDEX
from .transformer import (resample, wsola_stretch, pitch_shift, align_to_bars,
                          chop_sample, bars_to_sec, snap_bars)
from .metadata import SampleMetadata

_AUDIO_CACHE: dict[str, tuple[np.ndarray, int]] = {}
_AUDIO_CACHE_MAX = 64


def load_audio(m: SampleMetadata) -> tuple[np.ndarray, int]:
    """Lazy audio load with a small cache (decodes once per render)."""
    if m.path in _AUDIO_CACHE:
        return _AUDIO_CACHE[m.path]
    lower = m.path.lower()
    if lower.endswith(".wav"):
        fmt = "wav"
    elif lower.endswith((".aiff", ".aif")):
        fmt = "aiff"
    else:
        raise ValueError("flac/mp3 samples are indexed but need a decoder for rendering")
    from .scanner import decode_audio
    x, sr = decode_audio(m.path, fmt, max_seconds=40.0)
    x = x - float(np.mean(x))
    if len(_AUDIO_CACHE) >= _AUDIO_CACHE_MAX:
        _AUDIO_CACHE.pop(next(iter(_AUDIO_CACHE)))
    _AUDIO_CACHE[m.path] = (x, sr)
    return x, sr


def clear_audio_cache() -> None:
    _AUDIO_CACHE.clear()


_PITCHLESS_CATS = {"kick", "snare", "clap", "hat", "ride", "perc", "tom",
                   "fx", "impact", "riser", "texture", "ambience", "drum_loop"}
_PITCHED_CATS = {"bass", "acid", "lead", "chord", "piano", "stab", "hoover"}


def _pitch_for_sample(m: SampleMetadata, song) -> float:
    """Semitone shift to fit the SongPlan key, respecting confidence (spec §9)."""
    if not m.key or m.key_confidence < 0.55 or m.category in _PITCHLESS_CATS:
        return 0.0
    sam = m.key_root
    if sam is None:
        return 0.0
    iv = (NOTE_INDEX[song.key] - sam) % 12
    if iv > 6:
        iv -= 12
    return float(iv)


# roles per section, with musical intensity (spec §16)
_ROLE_SECTION_PLAN = {
    "main_break":   {"intro": 0.0, "groove": 0.5, "theme": 0.7, "dev": 0.8,
                     "break": 0.0, "build": 0.4, "drop": 1.0, "variation": 0.85,
                     "break2": 0.0, "build2": 0.5, "drop2": 1.0, "outro": 0.35},
    "alt_break":    {"groove": 0.4, "theme": 0.6, "dev": 0.7, "variation": 0.9,
                     "drop2": 0.9},
    "perc_loop":    {"groove": 0.6, "theme": 0.6, "dev": 0.6, "drop": 0.7,
                     "variation": 0.7, "drop2": 0.7},
    "vocal_hit":    {"theme": 0.6, "dev": 0.7, "drop": 1.0, "variation": 0.8,
                     "drop2": 1.0},
    "vocal_texture": {"intro": 0.6, "break": 1.0, "break2": 1.0, "outro": 0.5},
    "melodic_loop": {"theme": 0.7, "dev": 0.8, "drop": 0.9, "variation": 0.9,
                     "drop2": 0.9},
    "stab_hit":     {"groove": 0.5, "theme": 0.6, "drop": 1.0, "variation": 0.9,
                     "drop2": 1.0},
    "riser":        {"build": 1.0, "build2": 1.0},
    "impact":       {"drop": 1.0, "drop2": 1.0},
    "texture":      {"intro": 0.8, "break": 1.0, "break2": 0.9, "outro": 0.8},
    "pad":          {"break": 1.0, "break2": 1.0, "theme": 0.5},
    "bass_sample":  {"drop": 0.9, "drop2": 0.9},
}

_LOOP_ROLES = {"main_break", "alt_break", "perc_loop", "vocal_texture",
               "melodic_loop", "texture", "pad", "bass_sample"}
_ONESHOT_ROLES = {"vocal_hit", "stab_hit", "impact", "riser"}

# timeline event type per role
_ROLE_EVENT_TYPE = {
    "main_break": "breakbeat", "alt_break": "breakbeat", "perc_loop": "sample_loop",
    "vocal_hit": "vocal", "vocal_texture": "vocal", "melodic_loop": "sample_loop",
    "stab_hit": "stab", "bass_sample": "sample_loop", "texture": "texture",
    "pad": "texture", "impact": "impact", "riser": "riser",
}


def snap_loop_bars(m: SampleMetadata, bpm: float) -> float:
    """Choose a musical length for this loop based on its natural duration."""
    d = m.duration or 2.0
    for cand in (8, 4, 2, 1):
        if d >= cand * (60.0 / bpm) * 4 * 0.85:
            return float(cand)
    return float(snap_bars(d, bpm, allowed=(0.5, 0.25, 1.0)))


def _bar_list(song) -> list[tuple[str, float, int, int]]:
    """[(section, energy, abs_bar, total_bars)] for every bar of the track."""
    total = song.total_bars()
    out = []
    abs_bar = 0
    for sec in song.sections:
        for b in range(sec.bars):
            out.append((sec.name, sec.energy, abs_bar, total))
            abs_bar += 1
    return out


def _section_entries(bars) -> set[int]:
    """Absolute bar indices that begin a section appearance."""
    entries = {0}
    for i in range(1, len(bars)):
        if bars[i][0] != bars[i - 1][0]:
            entries.add(i)
    return entries


# ----------------------------------------------------------------------------
# PHASE A — decisions (pure; consumes its own seeded rng only)
# ----------------------------------------------------------------------------

def decide_placements(song, assignments: list[dict]) -> list[dict]:
    """Turn assignments into per-bar placement decisions (no audio)."""
    seed = song.plan.seed if song.plan.seed is not None else 0
    rng = np.random.default_rng(seed)
    bars = _bar_list(song)
    entries = _section_entries(bars)
    mode = getattr(song.plan, "sample_mode", "balanced")
    rep_thresh = {"subtle": 6, "balanced": 4, "heavy": 3}.get(mode, 4)
    placements: list[dict] = []
    role_use: dict[str, int] = {}
    role_last_bar: dict[str, int] = {}

    for (sec_name, energy, abs_bar, total_bars) in bars:
        for a in assignments:
            role = a["role"]
            m: SampleMetadata = a["sample"]
            plan_int = _ROLE_SECTION_PLAN.get(role, {}).get(sec_name, 0.0)
            if plan_int <= 0:
                continue
            p_rng = np.random.default_rng([seed, abs_bar,
                                           zlib.crc32(role.encode())])
            base = dict(role=role, section=sec_name, energy=energy,
                        abs_bar=abs_bar, sample=m,
                        event_type=_ROLE_EVENT_TYPE.get(role, "sample_loop"),
                        plan_intensity=plan_int, ops={}, pitch_semitones=0.0,
                        stretch_ratio=1.0, reverse=False, kind="loop",
                        variant="drop")
            if role in _ONESHOT_ROLES:
                if abs_bar not in entries:
                    continue
                base["kind"] = "oneshot"
                if role == "riser":
                    base["variant"] = "build"
                    target = bars_to_sec(2, song.bpm)
                    if m.duration and abs(1.0 - m.duration / target) < 0.6:
                        base["stretch_ratio"] = round(target / max(m.duration, 1e-6), 4)
                    base["start_offset_bars"] = 8
                if role == "vocal_hit" and p_rng.random() < 0.4:
                    base["start_offset_eighths"] = 2
                if p_rng.random() < 0.7:
                    base["pitch_semitones"] = _pitch_for_sample(m, song)
                placements.append(base)
                continue
            # loops
            bars_len = snap_loop_bars(m, song.bpm)
            base["loop_bars"] = bars_len
            if m.bpm and m.bpm_confidence > 0.4:
                r = song.bpm / m.bpm
                while r > 2:
                    r /= 2
                while r < 0.5:
                    r *= 2
                if 0.9 <= r <= 1.1 and abs(r - 1.0) > 0.005:
                    base["stretch_ratio"] = round(r, 4)
            use = role_use.get(role, 0)
            consecutive = (abs_bar - role_last_bar.get(role, -99)) == 1
            heavy_rep = use >= rep_thresh and consecutive
            ops = dict(reverse=0.12, reorder=0.30, repeat=0.08, mute=0.0, stutter=0.0)
            rep_mode = None
            if heavy_rep:
                rep_mode = int(p_rng.integers(0, 4))
                if rep_mode == 0:
                    ops = dict(reverse=0.35, reorder=0.6, stutter=0.15)
                elif rep_mode == 1:
                    ops = dict(reverse=0.0, reorder=0.15, mute=0.25)
                elif rep_mode == 2:
                    ops = dict(reverse=0.2, reorder=0.2, reverse_fill=0.3)
                else:
                    ops = dict(reverse=0.1, reorder=0.5, stutter=0.25)
            base["ops"] = ops
            base["rep_mode"] = rep_mode
            base["reverse"] = ops.get("reverse", 0) >= 0.3
            # occasional half-bar dropout at low intensity
            if plan_int < 0.6 and p_rng.random() < 0.4:
                base["dropout"] = "first_half" if p_rng.random() < 0.5 else "second_half"
            if _pitch_for_sample(m, song) and m.category in _PITCHED_CATS:
                base["pitch_semitones"] = _pitch_for_sample(m, song)
            base["variant"] = {"intro": "intro", "build": "intro",
                               "break": "breakdown", "break2": "breakdown",
                               "outro": "outro"}.get(sec_name, "drop")
            placements.append(base)
            role_use[role] = use + 1
            role_last_bar[role] = abs_bar
    return placements


# ----------------------------------------------------------------------------
# PHASE B — execution (deterministic; no musical decisions)
# ----------------------------------------------------------------------------

def render_sample_bus(song, assignments: list[dict],
                      placements: list[dict] | None = None) -> dict[str, np.ndarray]:
    """Render the samples bus from placement decisions."""
    if not assignments:
        return {}
    if placements is None:
        placements = decide_placements(song, assignments)
    total_n = int(song.total_bars() * 16 * song.step * dsp.SR)
    bus = np.zeros(total_n)
    bar_n = int(16 * song.step * dsp.SR)
    seed = song.plan.seed if song.plan.seed is not None else 0

    audio: dict[str, tuple[np.ndarray, int]] = {}
    for p in placements:
        m: SampleMetadata = p["sample"]
        if m.path not in audio:
            try:
                audio[m.path] = load_audio(m)
            except Exception:
                audio[m.path] = None
    stretched: dict[tuple[str, float], np.ndarray] = {}

    for p in placements:
        m: SampleMetadata = p["sample"]
        aud = audio.get(m.path)
        if aud is None:
            continue
        x, sr = aud
        p_rng = np.random.default_rng([seed, p["abs_bar"],
                                       zlib.crc32(p["role"].encode())])
        i0 = p["abs_bar"] * bar_n
        if p["kind"] == "oneshot":
            y, off = _exec_oneshot(song, p, m, x, sr, p_rng, bar_n)
            i0 += off
        else:
            y = _exec_loop(song, p, m, x, sr, p_rng, bar_n, stretched)
        if y is None or not len(y):
            continue
        n = len(bus)
        if i0 >= n or i0 < 0:
            continue
        e = min(n, i0 + len(y))
        bus[i0:e] += y[: e - i0]
    return {"samples": bus}


def _exec_oneshot(song, p, m, x, sr, rng, bar_n) -> tuple[np.ndarray, int]:
    """Return (audio, placement_offset_samples)."""
    role = p["role"]
    y = resample(x, sr, dsp.SR)
    if p.get("pitch_semitones"):
        y = pitch_shift(y, p["pitch_semitones"])
    off = 0
    if role == "riser":
        target = int(bars_to_sec(2, song.bpm) * dsp.SR)
        if abs(1.0 - p.get("stretch_ratio", 1.0)) > 0.01 and len(y) > dsp.SR * 0.5:
            y = wsola_stretch(y, p["stretch_ratio"])
        if len(y) < target:
            y = np.pad(y, (0, target - len(y)))
        y = y[:target]
        off = -int(p.get("start_offset_bars", 0)) * bar_n  # before the build
    elif role == "impact":
        y = dsp.hp(y, 40)
        y = dsp.normalize(y, 0.8)
    elif role == "stab_hit":
        y = dsp.normalize(y, 0.6)
        if len(y) > bar_n // 2:
            y = y[: bar_n // 2]
    elif role == "vocal_hit":
        y = dsp.normalize(y, 0.55)
        if p.get("start_offset_eighths"):
            off = int(p["start_offset_eighths"] * (bar_n / 8))
    energy = p.get("energy", 0.8)
    gain = 0.5 * (0.6 + 0.4 * energy)
    return y * gain, off


def _exec_loop(song, p, m, x, sr, rng, bar_n, stretched) -> np.ndarray:
    bars_len = p["loop_bars"]
    key = (m.path, bars_len)
    if key not in stretched:
        base = align_to_bars(x, song.bpm, bars_len)
        if abs(p.get("stretch_ratio", 1.0) - 1.0) > 0.005:
            base = wsola_stretch(base, p["stretch_ratio"])
        stretched[key] = base
    base = stretched[key]

    y_bar = chop_sample(base, dsp.SR, song.bpm, 1.0, rng, slices_per_bar=4,
                        ops=p["ops"])[: int(bars_to_sec(1, song.bpm) * dsp.SR)]
    drop = p.get("dropout")
    if drop == "first_half":
        y_bar = y_bar[: len(y_bar) // 2]
    elif drop == "second_half":
        y_bar = np.concatenate([np.zeros(len(y_bar) // 2), y_bar[len(y_bar) // 2:]])
    if p.get("pitch_semitones"):
        y_bar = pitch_shift(y_bar, p["pitch_semitones"])
    variant = p.get("variant", "drop")
    if variant == "intro":
        y_bar = dsp.lp(y_bar, 1200)
    elif variant == "breakdown":
        y_bar = dsp.lp(y_bar, 2500) * 0.8
    elif variant == "outro":
        y_bar = dsp.lp(y_bar, 900) * 0.7
    gain = (0.45 + 0.35 * p.get("energy", 0.8)) * \
           (0.6 + 0.4 * p.get("plan_intensity", 0.8))
    return y_bar * gain


# ----------------------------------------------------------------------------
# sample QC (spec §21)
# ----------------------------------------------------------------------------

def qc_samples(song, assignments: list[dict]) -> list[str]:
    """Diagnose sample problems; report the repair applied (not rejection)."""
    notes: list[str] = []
    if not assignments:
        return notes
    for a in assignments:
        m = a["sample"]
        try:
            x, _sr = load_audio(m)
        except Exception as e:
            notes.append(f"sample {m.filename}: unreadable at render time -> excluded "
                         f"({str(e)[:60]})")
            continue
        if len(x) == 0 or float(np.max(np.abs(x))) < 1e-4:
            notes.append(f"sample {m.filename}: silent source -> excluded")
        pk = float(np.max(np.abs(x)))
        if pk > 0.995:
            notes.append(f"sample {m.filename}: near-clipping source ({pk:.3f}) "
                         f"-> gain-staged")
    for a in assignments:
        m = a["sample"]
        if m.bpm and m.bpm_confidence > 0.4:
            r = song.bpm / m.bpm
            while r > 2:
                r /= 2
            while r < 0.5:
                r *= 2
            if 0.10 >= abs(r - 1) > 0.005:
                notes.append(f"sample {m.filename}: bpm {m.bpm:.0f} vs song "
                             f"{song.bpm:.0f} -> time-stretched "
                             f"{abs(r - 1) * 100:.0f}% (conf {m.bpm_confidence:.2f})")
            elif abs(r - 1) > 0.10:
                notes.append(f"sample {m.filename}: bpm {m.bpm:.0f} vs song "
                             f"{song.bpm:.0f} -> stretch beyond +/-10% range, "
                             f"bar-aligned instead (conf {m.bpm_confidence:.2f})")
    for a in assignments:
        m = a["sample"]
        if m.key and m.key_confidence >= 0.55 and m.category in (
                "bass", "acid", "lead", "chord", "piano"):
            shift = _pitch_for_sample(m, song)
            if shift:
                notes.append(f"sample {m.filename}: key {m.key} -> song {song.key} "
                             f"{song.scale} -> transposed {shift:+.0f} st "
                             f"(conf {m.key_confidence:.2f})")
    if any(a["role"] == "bass_sample" for a in assignments):
        notes.append("sampled bass layered over procedural bass -> sample HP'd in "
                     "mix to keep low end clean")
    return notes
