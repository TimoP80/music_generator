"""
timbor.voices — instrument layer: (instrument, midi, dur, vel, extra) -> waveform.

Keeps dsp.py generic and lets the genre packs stay purely musical.
"""
from __future__ import annotations

import numpy as np

from . import dsp
from .theory import hz


def synth_voice(inst: str, midi: float, dur: float, vel: float,
                seed: int | None = None, last_midi: float | None = None,
                **extra) -> np.ndarray:
    f = hz(midi)
    if inst == "supersaw":
        return dsp.supersaw_lead(f, dur, seed=seed,
                                 detune=float(extra.get("detune", 16)),
                                 unison=int(extra.get("unison", 7)))
    if inst == "hoover":
        return dsp.hoover(f, dur, seed=seed)
    if inst == "stab":
        return dsp.rave_stab(f, min(dur, 0.5), seed=seed)
    if inst == "piano":
        return dsp.rave_piano(f, dur, seed=seed)
    if inst == "pluck":
        return dsp.pluck(f, dur, seed=seed)
    if inst == "pad":
        return dsp.pad_chord([f, f * 1.26, f * 1.5], dur, seed=seed)  # minor-ish stack
    if inst == "square_lead":
        n = int(dur * dsp.SR)
        out = dsp.osc_square(f, n, 0.35)
        out = dsp.lp(out, 4200)
        out *= dsp.adsr(n, 0.004, 0.08, 0.7, 0.06)
        return out
    if inst == "acid":
        return dsp.acid_bass(f, dur, accent=bool(extra.get("accent", False)),
                             slide_from=hz(last_midi) if last_midi is not None
                             and extra.get("slide", False) else None,
                             seed=seed)
    if inst == "reese":
        return dsp.reese(f, dur, seed=seed)
    if inst == "sub":
        n = int(dur * dsp.SR)
        out = dsp.osc_sine(f * 0.5, n)
        out *= dsp.adsr(n, 0.004, 0.1, 0.85, 0.05)
        return out * 0.9
    if inst == "reverse_bass":
        return dsp.reverse_bass(f, dur, seed=seed)
    if inst == "rumble":
        return dsp.rumble_bass(f, dur, seed=seed)
    if inst == "fm_bass":
        n = int(dur * dsp.SR)
        out = dsp.fm_pair(f, f * 1.5, 2.0, n, index_env=dsp.exp_env(n, 0.07))
        out = dsp.lp(out, 900)
        out *= dsp.adsr(n, 0.003, 0.06, 0.7, 0.04)
        return out
    if inst == "dist_bass":
        n = int(dur * dsp.SR)
        out = dsp.osc_saw(f, n) + 0.5 * dsp.osc_square(f, n)
        out = dsp.dist_drive(out, 3.5)
        out = dsp.lp(out, 700)
        out *= dsp.adsr(n, 0.003, 0.07, 0.8, 0.04)
        return dsp.normalize(out, 0.85)
    if inst == "vox":
        return dsp.vox_chop(f, min(dur, 0.6), seed=seed)
    raise ValueError(f"unknown instrument {inst}")


# ----------------------------------------------------------------------------
# breakbeat engine
# ----------------------------------------------------------------------------

def render_break(kit: dict, bar_steps: int, step_dur: float,
                 pattern: list[tuple[int, str, float]],
                 seed: int | None = None) -> np.ndarray:
    """Render a step pattern [(step, voice, vel)] into one bar of audio."""
    n = int(bar_steps * step_dur * dsp.SR)
    out = np.zeros(n + dsp.SR)  # tail room
    for step, voice, vel in pattern:
        if voice not in kit:
            continue
        w = kit[voice]
        i0 = int(step * step_dur * dsp.SR)
        e = min(len(out), i0 + len(w))
        out[i0:e] += w[: e - i0] * vel
    return out[:n]


def break_pattern(rng: np.random.Generator, style: str, bar_steps: int,
                  intensity: float = 1.0) -> list[tuple[int, str, float]]:
    """Genre break patterns, chopped-and-rearranged rather than static loops.

    style: "jungle" | "dnb" | "bigbeat" | "hardcore" | "breakbeat_rave"
    """
    pat: list[tuple[int, str, float]] = []
    q = bar_steps // 4  # 16th grid assumed (16 steps/bar)
    if style in ("jungle", "dnb"):
        kick, snr, ghost = "kick", "snare", "snare"
        base_k = [0, 10]
        base_s = [4, 12]
        if rng.random() < 0.5:
            base_s.append(14)
        if rng.random() < 0.4:
            base_k.append(7)
        # ghost-note skank: quantized 16th snares off the grid
        ghosts = [i for i in range(bar_steps) if i % 4 != 0 and rng.random() < 0.08 * intensity]
        for s in base_k:
            pat.append((s, kick, 1.0))
        for s in base_s:
            pat.append((s, snr, 0.95))
        for g in ghosts:
            pat.append((g, ghost, 0.28))
        for s in range(2, bar_steps, 4):
            if rng.random() < 0.8:
                pat.append((s, "hat", 0.4))
        if rng.random() < 0.5:
            pat.append((bar_steps - 1, "hat", 0.5))
    elif style == "bigbeat":
        base_k = [0, 6, 10]
        base_s = [4, 12]
        if rng.random() < 0.6:
            base_s.append(11)
        if rng.random() < 0.5:
            base_k.append(3)
        for s in base_k:
            pat.append((s, "kick", 1.0))
        for s in base_s:
            pat.append((s, "snare", 1.0))
        for s in range(2, bar_steps, 2):
            if rng.random() < 0.75:
                pat.append((s, "hat", 0.45))
        if rng.random() < 0.4:
            pat.append((14, "tom", 0.6))
    elif style == "hardcore":
        for s in range(0, bar_steps, 4):
            pat.append((s, "kick", 1.0))
        for s in (4, 12):
            pat.append((s, "clap", 0.5))
        for s in range(2, bar_steps, 4):
            pat.append((s, "hat", 0.35))
    else:  # breakbeat_rave: 4x4 + chopped break overlay
        for s in range(0, bar_steps, 4):
            pat.append((s, "kick", 1.0))
        for s in (4, 12):
            pat.append((s, "clap", 0.6))
        ghosts = [i for i in range(bar_steps) if i % 4 != 0 and rng.random() < 0.10 * intensity]
        for g in ghosts:
            pat.append((g, "snare", 0.25))
        for s in range(2, bar_steps, 2):
            if rng.random() < 0.7:
                pat.append((s, "hat", 0.4))
    return pat


def chop_with_decisions(rng: np.random.Generator, bar: np.ndarray, slice_len: float,
                        stutters: int = 2,
                        reverse_p: float = 0.12) -> tuple[np.ndarray, dict]:
    """Chop a bar into slices; also return the decisions for replay.

    decisions = {"slice_len": float, "order": [...], "reverse": [bool, ...]}
    """
    n = len(bar)
    m = max(2, int(slice_len * dsp.SR))
    slices = [bar[i:i + m] for i in range(0, n - m + 1, m)]
    if len(slices) < 2:
        return bar, {"slice_len": slice_len, "order": [0], "reverse": [False]}
    order = list(range(len(slices)))
    for _ in range(stutters):
        i = int(rng.integers(0, len(order)))
        j = int(rng.integers(0, len(order)))
        order[i], order[j] = order[j], order[i]
    reverse_flags = []
    out = np.zeros(n)
    pos = 0
    for idx in order:
        s = slices[idx]
        rev = bool(rng.random() < reverse_p)
        reverse_flags.append(rev)
        if rev:
            s = s[::-1]
        e = min(n, pos + len(s))
        out[pos:e] += s[: e - pos]
        pos = e
        if pos >= n:
            break
    return out, {"slice_len": float(slice_len), "order": [int(i) for i in order],
                 "reverse": reverse_flags}


def apply_chop_decisions(bar: np.ndarray, decisions: dict) -> np.ndarray:
    """Re-apply recorded chop decisions without any RNG (timeline replay)."""
    n = len(bar)
    m = max(2, int(decisions["slice_len"] * dsp.SR))
    slices = [bar[i:i + m] for i in range(0, n - m + 1, m)]
    out = np.zeros(n)
    pos = 0
    for k, idx in enumerate(decisions["order"]):
        if idx >= len(slices):
            break
        s = slices[idx]
        if decisions["reverse"][k]:
            s = s[::-1]
        e = min(n, pos + len(s))
        out[pos:e] += s[: e - pos]
        pos = e
        if pos >= n:
            break
    return out


def chop(rng: np.random.Generator, bar: np.ndarray, slice_len: float,
         stutters: int = 2, reverse_p: float = 0.12) -> np.ndarray:
    """Chop a rendered break bar into slices, shuffle a few, keep bar length."""
    out, _ = chop_with_decisions(rng, bar, slice_len, stutters, reverse_p)
    return out


def kick_bass_plan(rng: np.random.Generator, steps: int, style: str) -> list[bool]:
    """Which 16th steps carry bass, co-ordinated with the kick grid."""
    hits = [False] * steps
    if style == "off8":
        for i in range(2, steps, 4):
            hits[i] = True
    elif style == "roll16":
        hits = [True] * steps
    elif style == "four":
        for i in range(0, steps, 4):
            hits[i] = True
    elif style == "four_off":
        for i in range(0, steps, 4):
            hits[i] = True
        for i in range(2, steps, 4):
            hits[i] = True
    elif style == "acid":
        for i in range(steps):
            hits[i] = rng.random() < 0.45
        hits[0] = True
    elif style == "jungle":
        for i in (0, 3, 6, 10, 12, 14):
            hits[i] = True
    return hits
