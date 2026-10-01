"""timbor.samples.transformer — musical sample adaptation.

BPM via WSOLA (transient-aware), pitch via resample+compensating stretch,
chopping ops that keep musical coherence. All randomness comes from the
caller's seeded Generator so the same seed reproduces the same transforms.
"""
from __future__ import annotations

import numpy as np

from timbor import dsp
from .metadata import SampleMetadata


# ----------------------------------------------------------------------------
# rate conversion
# ----------------------------------------------------------------------------

def resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to or len(x) == 0:
        return x.astype(np.float64)
    n_new = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0, len(x) - 1, n_new),
                     np.arange(len(x)), x).astype(np.float64)


# ----------------------------------------------------------------------------
# WSOLA time stretching
# ----------------------------------------------------------------------------

def wsola_stretch(x: np.ndarray, rate: float, sr: int = dsp.SR,
                  win_ms: float = 46.0, tol_ms: float = 8.0) -> np.ndarray:
    """Time-stretch by `rate` (1.0 = unchanged, 2.0 = twice as long).

    WSOLA with transient protection: when a strong onset appears inside the
    window, the tolerance search is skipped to avoid smearing attacks.
    """
    if abs(rate - 1.0) < 1e-4 or len(x) < 4096:
        return x.copy()
    Hs = int(sr * 0.010)            # synthesis hop 10 ms
    Ha = int(Hs / rate)             # analysis hop
    win = int(sr * win_ms / 1000.0)
    tol = int(sr * tol_ms / 1000.0)
    tol = min(tol, win // 2)
    fade = np.linspace(0, 1, Hs)

    # onset envelope for transient protection
    frame = win // 2
    n_frames = max(1, (len(x) - win) // frame)
    flux = np.zeros(max(1, n_frames))
    prev = None
    for i in range(n_frames):
        seg = x[i * frame:i * frame + win]
        if len(seg) < win:
            break
        mag = np.abs(np.fft.rfft(seg * np.hanning(win)))
        if prev is not None:
            d = mag - prev
            flux[i] = float(d[d > 0].sum())
        prev = mag
    thr = flux.mean() + 1.2 * flux.std() if len(flux) else 0.0

    out_len = int(len(x) * rate)
    out = np.zeros(out_len + win)
    norm = np.zeros(out_len + win)
    w = np.hanning(win)

    ana = 0        # analysis pointer
    syn = 0        # synthesis pointer
    prev_tail = x[:0].copy()
    while syn + win < out_len and ana + win < len(x):
        # transient in this window? -> lock analysis position (no search)
        fi = min(int(ana // frame), len(flux) - 1)
        is_transient = flux[fi] > thr if len(flux) else False
        seg = x[ana:ana + win]
        if not is_transient and len(prev_tail) > 0:
            # align into the natural continuation region
            region_lo = max(0, ana - tol)
            region_hi = min(len(x) - win, ana + tol)
            if region_hi > region_lo:
                best_off = ana
                best_v = -1e18
                ref = x[ana:ana + Hs]
                for off in range(region_lo, region_hi, max(1, tol // 16)):
                    cand = x[off:off + Hs]
                    v = float(np.dot(ref, cand)) if len(cand) == len(ref) else -1e18
                    if v > best_v:
                        best_v = v
                        best_off = off
                ana = best_off
                seg = x[ana:ana + win]
        if len(seg) < win:
            seg = np.pad(seg, (0, win - len(seg)))
        windowed = seg * w
        out[syn:syn + win] += windowed
        norm[syn:syn + win] += w
        prev_tail = seg[-Hs:]
        ana += Ha
        syn += Hs
    norm[norm < 1e-6] = 1.0
    return (out[:out_len] / norm[:out_len])


def pitch_shift(x: np.ndarray, semitones: float, sr: int = dsp.SR) -> np.ndarray:
    """Pitch shift via resample + compensating WSOLA stretch."""
    if abs(semitones) < 1e-3:
        return x.copy()
    r = 2.0 ** (semitones / 12.0)
    up = resample(x, sr, int(round(sr / r)))  # changes pitch and length
    return _stretch_back(up, r, sr)


def _stretch_back(up: np.ndarray, r: float, sr: int) -> np.ndarray:
    # up has length len(x)/r; stretch by r to restore original length
    return wsola_stretch(up, r, sr)


# ----------------------------------------------------------------------------
# musical time
# ----------------------------------------------------------------------------

def sec_to_bars(seconds: float, bpm: float) -> float:
    return seconds / (60.0 / bpm * 4)


def bars_to_sec(bars: float, bpm: float) -> float:
    return bars * 60.0 / bpm * 4


def snap_bars(seconds: float, bpm: float, allowed=(0.25, 0.5, 1, 2, 4, 8)) -> float:
    """Snap a duration to the nearest musical length (in bars)."""
    b = sec_to_bars(seconds, bpm)
    return min(allowed, key=lambda a: abs(np.log2(max(a, 1e-6) / max(b, 1e-6))))


def align_to_bars(x: np.ndarray, bpm: float, bars: float,
                  sr: int = dsp.SR) -> np.ndarray:
    """Trim or stretch so the loop is exactly `bars` long at `bpm`."""
    target = int(bars_to_sec(bars, bpm) * sr)
    if len(x) == 0:
        return np.zeros(target)
    if abs(len(x) - target) < sr * 0.002:
        out = np.zeros(target)
        out[:len(x)] = x[:target]
        return out
    if len(x) < target:
        # stretch up (loop-friendly, mild rates)
        rate = target / len(x)
        if 0.5 <= rate <= 2.0:
            y = wsola_stretch(x, rate, sr)
        else:
            y = x
        out = np.zeros(target)
        out[:len(y)] = y[:target]
        return out
    return x[:target]


# ----------------------------------------------------------------------------
# chopping / variation operations
# ----------------------------------------------------------------------------

def chop_sample(x: np.ndarray, sr: int, bpm: float, bars: float,
                rng: np.random.Generator, slices_per_bar: int = 4,
                ops: dict | None = None) -> np.ndarray:
    """Slice on the musical grid and apply controlled transformations.

    ops (all optional, applied per slice with the given probability):
      reverse, reorder, repeat, mute, stutter, reverse_fill
    """
    ops = ops or {}
    total = int(bars_to_sec(bars, bpm) * sr)
    y = align_to_bars(x, bpm, bars, sr)
    n_slices = max(1, int(bars * slices_per_bar))
    sl = total // n_slices
    pieces = [y[i * sl:(i + 1) * sl].copy() for i in range(n_slices)]

    p_rev = ops.get("reverse", 0.15)
    p_mute = ops.get("mute", 0.0)
    p_stut = ops.get("stutter", 0.0)
    p_rep = ops.get("repeat", 0.1)
    reorder = ops.get("reorder", 0.25)
    rev_fill = ops.get("reverse_fill", 0.0)

    order = list(range(n_slices))
    if rng.random() < reorder and n_slices >= 4:
        # keep the downbeat slice first for coherence
        head, rest = order[:1], order[1:]
        rng.shuffle(rest)
        order = head + rest
    out = np.zeros_like(y)
    for i, idx in enumerate(order):
        seg = pieces[idx].copy()
        if rng.random() < p_rev:
            seg = seg[::-1]
        if rng.random() < p_rep:
            seg = np.tile(seg, 2)[:sl]
        if rng.random() < p_mute:
            seg = np.zeros_like(seg)
        if rng.random() < p_stut:
            e = max(sl // 8, 64)
            seg = np.tile(seg[:e], sl // e + 1)[:sl]
        if rev_fill and rng.random() < rev_fill:
            seg = seg * 0.7 + pieces[max(0, idx - 1)][::-1][:len(seg)] * 0.3
        out[i * sl:(i + 1) * sl] += seg[:len(out) - i * sl]
    return out


def variation_version(x: np.ndarray, sr: int, song_bpm: float, kind: str,
                      rng: np.random.Generator,
                      key_shift: float = 0.0) -> np.ndarray:
    """Section-flavoured versions of one source sample (spec §17)."""
    y = resample(x, sr, dsp.SR)
    if kind == "intro":
        y = dsp.lp(y, 900) * 0.8
    elif kind == "drop":
        y = dsp.hp(y, 60)
        y = dsp.dist_drive(y, 1.3, mix=0.4) if rng.random() < 0.4 else y
    elif kind == "breakdown":
        y = dsp.lp(y, 1800)
        if key_shift:
            y = pitch_shift(y, key_shift)
    elif kind == "fill":
        bars = min(1.0, max(0.25, sec_to_bars(len(y) / dsp.SR, song_bpm)))
        y = chop_sample(y, dsp.SR, song_bpm, bars,
                        rng, slices_per_bar=8, ops=dict(stutter=0.4, reverse=0.3))
    elif kind == "outro":
        y = dsp.lp(y, 700) * 0.7
    # safety: no DC / runaway peaks
    if len(y):
        y = y - float(np.mean(y))
        pk = float(np.max(np.abs(y))) or 1.0
        if pk > 1.0:
            y = y / pk * 0.98
    return y
