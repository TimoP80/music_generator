"""
timbor.dsp — low-level DSP: oscillators, envelopes, filters, FX, drum synthesis.

Everything is numpy; no external DSP deps. All signals are mono float64
numpy arrays at SR. Stereo is handled by the renderer.

Filtering is FFT-based (zero-phase magnitude shaping) so full tracks render
fast; time-varying filters use overlap-add STFT with per-frame response.
"""
from __future__ import annotations

import math
import numpy as np

SR = 44100
TWO_PI = 2.0 * math.pi


# ----------------------------------------------------------------------------
# envelopes
# ----------------------------------------------------------------------------

def adsr(n: int, a: float = 0.005, d: float = 0.1, s: float = 0.7,
         r: float = 0.1, hold: float | None = None) -> np.ndarray:
    a_n = max(1, int(a * SR))
    d_n = max(1, int(d * SR))
    r_n = max(1, int(r * SR))
    if hold is None:
        hold = n / SR
    s_n = max(1, int(hold * SR))
    env = np.concatenate([
        np.linspace(0.0, 1.0, a_n, endpoint=False),
        np.linspace(1.0, s, d_n, endpoint=False),
        np.full(s_n, s),
        np.linspace(s, 0.0, r_n, endpoint=False),
    ])
    if len(env) < n:
        env = np.pad(env, (0, n - len(env)))
    return env[:n]


def exp_env(n: int, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.exp(-t / max(decay, 1e-5))


def env_seg(points: list[tuple[float, float]], length: int) -> np.ndarray:
    """Piecewise-linear envelope from (time_seconds, value) points."""
    pts = [(0.0, points[0][1])] + list(points)
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    if xs[-1] < length / SR:
        xs = np.append(xs, length / SR)
        ys = np.append(ys, ys[-1])
    t = np.arange(length) / SR
    return np.interp(t, xs, ys)


# ----------------------------------------------------------------------------
# oscillators (freq may be scalar or per-sample array; phase handled by cumsum)
# ----------------------------------------------------------------------------

def _phase(freq, n: int, phase0: float) -> np.ndarray:
    if np.isscalar(freq):
        return phase0 + np.arange(n) * (float(freq) / SR)
    return phase0 + np.cumsum(np.asarray(freq, dtype=np.float64)) / SR


def osc_saw(freq, n: int, phase0: float = 0.0) -> np.ndarray:
    ph = _phase(freq, n, phase0)
    return 2.0 * (ph % 1.0) - 1.0


def osc_square(freq, n: int, pw: float = 0.5, phase0: float = 0.0) -> np.ndarray:
    ph = _phase(freq, n, phase0)
    return np.where((ph % 1.0) < pw, 1.0, -1.0)


def osc_sine(freq, n: int, phase0: float = 0.0) -> np.ndarray:
    return np.sin(TWO_PI * _phase(freq, n, phase0))


def osc_tri(freq, n: int, phase0: float = 0.0) -> np.ndarray:
    ph = _phase(freq, n, phase0)
    return 4.0 * np.abs((ph + 0.25) % 1.0 - 0.5) - 1.0


def supersaw(freq: float, n: int, detune_cents: float = 14.0, voices: int = 7,
             spread: float = 0.0, seed: int | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    offs = np.linspace(-1, 1, voices)
    for i, o in enumerate(offs):
        cents = o * detune_cents
        if spread > 0:
            cents += rng.uniform(-spread, spread)
        f = freq * 2.0 ** (cents / 1200.0)
        out += osc_saw(f, n, phase0=rng.random())
    return out / voices


def fm_pair(c_freq: float, m_freq: float, m_index: float, n: int,
            index_env: np.ndarray | None = None) -> np.ndarray:
    mph = TWO_PI * m_freq * np.arange(n) / SR
    idx = m_index if index_env is None else m_index * index_env
    return np.sin(TWO_PI * c_freq * np.arange(n) / SR + idx * np.sin(mph))


def noise(n: int, seed: int | None = None) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n)


# ----------------------------------------------------------------------------
# FFT-based filtering (zero-phase, stable, fast)
# ----------------------------------------------------------------------------

def _resp(freqs: np.ndarray, kind: str, fc: float, q: float,
          order: int, res: float) -> np.ndarray:
    f = np.maximum(freqs, 1e-6)
    if kind == "lp":
        g = 1.0 / np.sqrt(1.0 + (f / fc) ** (2 * order))
    elif kind == "hp":
        g = (f / fc) ** order / np.sqrt(1.0 + (f / fc) ** (2 * order))
    elif kind == "bp":
        g = ((f / fc) ** order / np.sqrt(1.0 + (f / fc) ** (2 * order)))
        g = g * (1.0 / np.sqrt(1.0 + (fc / np.maximum(f, 1.0)) ** (2 * order)))
    else:
        raise ValueError(kind)
    if res > 0:
        w = 0.28
        g = g * (1.0 + res * np.exp(-((np.log2(f / fc)) ** 2) / (2 * w * w)))
    return g


def fft_filter(x: np.ndarray, kind: str, fc: float, q: float = 0.9,
               order: int = 2, res: float = 0.0) -> np.ndarray:
    n = len(x)
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    return np.fft.irfft(X * _resp(freqs, kind, fc, q, order, res), n)


def lp(x: np.ndarray, fc: float, res: float = 0.0, order: int = 2) -> np.ndarray:
    return fft_filter(x, "lp", fc, order=order, res=res)


def hp(x: np.ndarray, fc: float, res: float = 0.0, order: int = 2) -> np.ndarray:
    return fft_filter(x, "hp", fc, order=order, res=res)


def bp(x: np.ndarray, fc: float, res: float = 0.0, order: int = 2) -> np.ndarray:
    return fft_filter(x, "bp", fc, order=order, res=res)


def filter_sweep(x: np.ndarray, kind: str, f_start: float, f_end: float,
                 q: float = 0.9, blocks: int = 64, res: float = 0.0,
                 win: int = 1024, hop: int = 256) -> np.ndarray:
    """Time-varying filter: cutoff glides f_start -> f_end (log) over buffer."""
    n = len(x)
    frames = max(2, min(blocks, n // hop + 1))
    fc_arr = f_start * (f_end / max(f_start, 1.0)) ** np.linspace(0, 1, frames)
    w = np.hanning(win)
    pad = win // 2
    xx = np.pad(x, (pad, pad))
    total = len(xx)
    n_frames = 1 + (total - win) // hop
    out = np.zeros(total)
    wsum = np.zeros(total)
    freqs = np.fft.rfftfreq(win, 1.0 / SR)
    for k in range(n_frames):
        frac = k / max(1, n_frames - 1)
        idx = min(int(frac * (frames - 1)), frames - 1)
        s = k * hop
        seg = xx[s:s + win] * w
        g = _resp(freqs, kind, float(fc_arr[idx]), q, 2, res)
        out[s:s + win] += np.fft.irfft(np.fft.rfft(seg) * g, win) * w
        wsum[s:s + win] += w * w
    out /= np.maximum(wsum, 1e-9)
    return out[pad:pad + n]


def one_pole_lp(x: np.ndarray, fc: float) -> np.ndarray:
    return lp(x, fc, order=1)


def one_pole_hp(x: np.ndarray, fc: float) -> np.ndarray:
    return hp(x, fc, order=1)


# ----------------------------------------------------------------------------
# FX
# ----------------------------------------------------------------------------

def _comb_fb(x: np.ndarray, d: int, g: float) -> np.ndarray:
    """Vectorized feedback comb: y[n] = x[n] + g*y[n-d]."""
    n = len(x)
    buf = np.zeros(n + d)
    buf[:n] = x
    s = 0
    while s < n:
        e = min(s + d, n)
        buf[s + d:e + d] += g * buf[s:e]
        s += d
    return buf[d:n + d]


def simple_reverb(x: np.ndarray, size: float = 0.9, damp: float = 5000.0,
                  wet: float = 0.3) -> np.ndarray:
    n = len(x)
    out = np.zeros(n)
    combs = [0.0251, 0.0313, 0.0377, 0.0419, 0.0503]
    for t in combs:
        d = max(16, int(SR * t * (0.7 + 0.6 * size)))
        out += _comb_fb(x, d, 0.78 * size)
    out /= len(combs)
    out = lp(out, damp)
    return (1.0 - wet) * x + wet * out


def delay_line(x: np.ndarray, time_s: float, feedback: float = 0.35,
               taps: int = 4, tone: float = 6000.0) -> np.ndarray:
    n = len(x)
    out = x.copy()
    d = int(time_s * SR)
    gain = feedback
    src = x
    for _ in range(taps):
        delayed = np.zeros(n)
        if d < n:
            delayed[d:] = src[: n - d]
        delayed = lp(delayed, tone)
        out += gain * delayed
        src = delayed
        gain *= feedback
    return out


def dist_drive(x: np.ndarray, drive: float = 3.0, mix: float = 1.0) -> np.ndarray:
    shaped = np.tanh(x * drive) / np.tanh(drive) if drive > 0 else x
    return mix * shaped + (1 - mix) * x


def bitcrush(x: np.ndarray, bits: int = 8, downsample: int = 1) -> np.ndarray:
    levels = 2.0 ** (bits - 1)
    y = np.round(x * levels) / levels
    if downsample > 1:
        idx = (np.arange(len(y)) // downsample) * downsample
        y = y[idx]
    return y


def normalize(x: np.ndarray, peak: float = 0.98) -> np.ndarray:
    m = float(np.max(np.abs(x))) or 1.0
    return x * (peak / m)


# ----------------------------------------------------------------------------
# drums
# ----------------------------------------------------------------------------

def kick_gabber(bpm: float, seed: int | None = None, tone: float = 1.0) -> np.ndarray:
    """Distorted gabber/hardcore kick: pitched 909 body -> hard clip."""
    dur = 60.0 / bpm * 0.5
    n = int(dur * SR)
    t = np.arange(n) / SR
    f0, f1 = 190.0 * tone, 48.0
    freq = f1 + (f0 - f1) * np.exp(-t / 0.012)
    body = np.sin(TWO_PI * np.cumsum(freq) / SR)
    body *= exp_env(n, 0.075)
    click = hp(noise(n, seed), 2500) * exp_env(n, 0.002) * 0.7
    punch = dist_drive(body + click, 9.0)
    return normalize(hp(punch, 28), 0.95)


def kick_four(bpm: float, seed: int | None = None, punchy: bool = True) -> np.ndarray:
    dur = 60.0 / bpm * 0.5
    n = int(dur * SR)
    t = np.arange(n) / SR
    freq = 50 + (160 - 50) * np.exp(-t / 0.008)
    body = np.sin(TWO_PI * np.cumsum(freq) / SR) * exp_env(n, 0.09)
    thump = np.sin(TWO_PI * 55 * t) * exp_env(n, 0.05) * 0.6
    click = hp(noise(n, seed), 3000) * exp_env(n, 0.0015) * (0.55 if punchy else 0.3)
    return normalize(body + thump + click, 0.95)


def kick_808(bpm: float, seed: int | None = None, long_tail: bool = True) -> np.ndarray:
    dur = 60.0 / bpm * (0.9 if long_tail else 0.5)
    n = int(dur * SR)
    t = np.arange(n) / SR
    freq = 41 + (120 - 41) * np.exp(-t / 0.02)
    body = np.sin(TWO_PI * np.cumsum(freq) / SR)
    body *= exp_env(n, 0.35 if long_tail else 0.18)
    click = hp(noise(n, seed), 2000) * exp_env(n, 0.001) * 0.4
    return normalize(body + click, 0.95)


def snare_909(seed: int | None = None, bright: bool = True) -> np.ndarray:
    n = int(0.22 * SR)
    tone = osc_tri(185, n) * exp_env(n, 0.045) * 0.5
    tone += osc_tri(330, n) * exp_env(n, 0.03) * 0.3
    nz = hp(noise(n, seed), 1800 if bright else 1200)
    nz *= exp_env(n, 0.055)
    return normalize(tone + nz, 0.9)


def clap_909(seed: int | None = None) -> np.ndarray:
    n = int(0.3 * SR)
    out = np.zeros(n)
    rng = np.random.default_rng(seed)
    for k, off in enumerate([0.0, 0.010, 0.020, 0.030]):
        m = max(8, n - int(off * SR))
        nz = hp(noise(m, int(rng.integers(0, 1 << 30))), 1100)
        nz *= exp_env(m, 0.012 if k < 3 else 0.09)
        i0 = int(off * SR)
        out[i0:i0 + m] += nz * (0.6 if k < 3 else 1.0)
    return normalize(out[:n], 0.85)


def hat(seed: int | None = None, open_hat: bool = False) -> np.ndarray:
    dur = 0.28 if open_hat else 0.055
    n = int(dur * SR)
    nz = hp(noise(n, seed), 7000)
    nz = bitcrush(nz, 9) * exp_env(n, 0.09 if open_hat else 0.012)
    ring = osc_sine(8200, n) * exp_env(n, 0.01) * 0.3
    return normalize(nz + ring, 0.55)


def ride(seed: int | None = None) -> np.ndarray:
    n = int(0.5 * SR)
    nz = hp(noise(n, seed), 5500) * exp_env(n, 0.25)
    ping = osc_sine(3100, n) * exp_env(n, 0.18) * 0.35
    ping += osc_sine(4700, n) * exp_env(n, 0.1) * 0.2
    return normalize(nz + ping, 0.4)


def tom(freq: float = 120, seed: int | None = None) -> np.ndarray:
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    f = freq * np.exp(-t / 0.06) + 60
    body = np.sin(TWO_PI * np.cumsum(f) / SR) * exp_env(n, 0.1)
    return normalize(body, 0.8)


def crash(seed: int | None = None) -> np.ndarray:
    n = int(1.6 * SR)
    nz = hp(noise(n, seed), 4500) * exp_env(n, 0.5)
    shimmer = osc_sine(6200, n) * exp_env(n, 0.4) * 0.12
    return normalize(nz + shimmer, 0.45)


# ----------------------------------------------------------------------------
# melodic voices
# ----------------------------------------------------------------------------

def rave_stab(root_hz: float, dur: float = 0.28, seed: int | None = None) -> np.ndarray:
    """Classic 'mentasm' rave stab: detuned saws through aggressive HP + drive."""
    n = int(dur * SR)
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    for det, pan in ((0.0, 1.0), (11.0, 0.9), (-9.0, 1.1), (24.5, 1.05)):
        out += osc_saw(root_hz * 2.0 ** (det / 1200.0), n, rng.random()) * pan
    out /= 4
    out = hp(out, 220)
    out = dist_drive(out, 2.2)
    out *= adsr(n, 0.002, 0.12, 0.25, 0.15)
    return normalize(out, 0.8)


def hoover(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    """Alpha-Juno 'hoover': detuned saws with drift + drive."""
    n = int(dur * SR)
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    for det in (-14, -5, 0, 6, 13, 21):
        f = freq * 2.0 ** (det / 1200.0)
        drift = 1.0 + 0.012 * np.sin(TWO_PI * rng.uniform(0.4, 1.7) * np.arange(n) / SR)
        out += osc_saw(f * drift, n, rng.random())
    out /= 6
    out = hp(out, 180)
    out = dist_drive(out, 1.8)
    out *= adsr(n, 0.01, 0.2, 0.8, 0.25)
    return normalize(out, 0.7)


def reese(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    n = int(dur * SR)
    a = osc_saw(freq * 1.008, n, 0.1)
    b = osc_saw(freq * 0.992, n, 0.6)
    out = (a + b) * 0.5
    out = lp(out, 950)
    out += np.sin(TWO_PI * freq * np.arange(n) / SR) * 0.55  # true sub layer
    return out


def acid_bass(freq: float, dur: float, accent: bool = False,
              slide_from: float | None = None, seed: int | None = None) -> np.ndarray:
    """303-style: saw through resonant LP with envelope on cutoff."""
    n = int(dur * SR)
    if slide_from is not None:
        f = np.linspace(slide_from, freq, n)
    else:
        f = np.full(n, freq)
    ph = np.cumsum(f) / SR
    wave = 2.0 * (ph % 1.0) - 1.0
    base = 380 if accent else 260
    fc0 = base
    fc1 = base * 0.7
    out = filter_sweep(wave, "lp", fc0, fc1, q=7.0, blocks=32,
                       res=1.4 if accent else 0.9)
    if accent:
        out = dist_drive(out, 1.4)
    out *= adsr(n, 0.003, 0.08, 0.55, 0.05)
    return out


def reverse_bass(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    """Hard-house offbeat reverse-bass stab: reverse-env saw + sub."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    grow = (t / (n / SR)) ** 2.2
    saw = osc_saw(freq, n) * grow
    saw += osc_square(freq / 2, n, 0.5) * grow * 0.5
    saw = lp(saw, 1400)
    sub = np.sin(TWO_PI * freq * 0.5 * t) * grow
    return normalize(saw + sub * 0.7, 0.8)  # sub an octave down is intentional here


def rumble_bass(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    """Gabber-style distorted 16th rumble: driven saw + sub, LP-tamed."""
    n = int(dur * SR)
    out = osc_saw(freq, n) + 0.4 * osc_saw(freq * 1.005, n, 0.3)
    out = dist_drive(out, 4.2)
    out = lp(out, 340)
    out += np.sin(TWO_PI * freq * np.arange(n) / SR) * 0.65
    out *= adsr(n, 0.004, 0.05, 0.9, 0.03)
    return normalize(out, 0.85)


def supersaw_lead(freq: float, dur: float, seed: int | None = None,
                  detune: float = 16, unison: int = 7) -> np.ndarray:
    n = int(dur * SR)
    out = supersaw(freq, n, detune_cents=detune, voices=unison, spread=2.0, seed=seed)
    out = hp(out, 130)
    out *= adsr(n, 0.006, 0.15, 0.75, 0.12)
    return out


def pluck(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    n = int(dur * SR)
    out = fm_pair(freq, freq * 2.0, 2.2, n, index_env=exp_env(n, 0.05))
    out = lp(out, 5200)
    out *= adsr(n, 0.002, 0.09, 0.35, 0.08)
    return out


def rave_piano(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    """Cheap digital piano: harmonics + FM sparkle, quick attack."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for h, g in ((1, 1.0), (2, 0.5), (3, 0.25), (4, 0.12), (5, 0.08)):
        out += g * np.sin(TWO_PI * freq * h * t + 0.7 * h)
    spark = fm_pair(freq * 2, freq * 5.01, 1.4, n, index_env=exp_env(n, 0.02)) * 0.2
    out = (out / 1.95 + spark) * adsr(n, 0.002, 0.18, 0.4, 0.25)
    return normalize(lp(out, 4200), 0.6)


def pad_chord(freqs: list[float], dur: float, seed: int | None = None) -> np.ndarray:
    """Slow-attack pad from detuned saw stacks + slow LP sweep."""
    n = int(dur * SR)
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    for f in freqs:
        out += supersaw(f, n, detune_cents=9, voices=5, spread=1.5,
                        seed=int(rng.integers(0, 1 << 30)))
    out /= max(1, len(freqs))
    out = filter_sweep(out, "lp", 500, 2400, q=0.9, blocks=16)
    out *= adsr(n, 0.45, 0.3, 0.85, 0.6)
    return normalize(out, 0.55)


def vox_chop(freq: float, dur: float, seed: int | None = None) -> np.ndarray:
    """Formant-ish 'vocal chop': saw source through 3 formant bandpasses."""
    n = int(dur * SR)
    src = osc_saw(freq, n, 0.2) * 0.7 + 0.3 * noise(n, seed)
    out = np.zeros(n)
    for fc, g in ((700, 1.0), (1200, 0.7), (2600, 0.4)):
        out += g * bp(src, fc, order=3)
    out *= adsr(n, 0.004, 0.06, 0.6, 0.07)
    return normalize(out, 0.5)


# ----------------------------------------------------------------------------
# risers / impacts / texture
# ----------------------------------------------------------------------------

def riser(dur: float, seed: int | None = None, kind: str = "noise") -> np.ndarray:
    n = int(dur * SR)
    if kind == "noise":
        nz = noise(n, seed)
        out = filter_sweep(nz, "bp", 300, 6000, q=1.2, blocks=48)
    else:
        out = filter_sweep(osc_saw(110, n), "lp", 400, 5000, q=3.0, blocks=24)
    shape = env_seg([(0.0, 0.15), (0.75, 0.55), (0.97, 1.0), (1.0, 0.0)], n)
    return out * shape


def impact(seed: int | None = None) -> np.ndarray:
    n = int(1.2 * SR)
    boom = kick_808(120.0, seed=seed, long_tail=False)
    rev = lp(noise(int(0.35 * SR), seed), 1200)[::-1] * 0.5
    out = np.zeros(n)
    m = min(n, len(boom))
    out[:m] += boom[:m]
    out[: len(rev)] += rev
    return normalize(out, 0.95)


def vinyl_crackle(dur: float, seed: int | None = None, level: float = 0.03) -> np.ndarray:
    n = int(dur * SR)
    rng = np.random.default_rng(seed)
    clicks = (rng.random(n) > 0.9995).astype(float)
    clicks *= rng.uniform(0.3, 1.0, n)
    hiss = hp(noise(n, int(rng.integers(0, 1 << 30))), 4000) * 0.06
    return (clicks + hiss) * level


def downlifter(dur: float, seed: int | None = None) -> np.ndarray:
    n = int(dur * SR)
    out = filter_sweep(osc_saw(220, n), "lp", 4000, 300, q=2.0, blocks=24)
    out *= env_seg([(0.0, 0.7), (1.0, 0.05)], n)
    return out
