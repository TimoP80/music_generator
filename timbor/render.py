"""
timbor.render — stereo imaging, mastering, WAV export.
"""
from __future__ import annotations

import wave
import numpy as np

from . import dsp


def _haas(x: np.ndarray, delay_ms: float = 12.0, gain: float = 0.35) -> tuple[np.ndarray, np.ndarray]:
    d = int(dsp.SR * delay_ms / 1000.0)
    left = x
    right = np.copy(x)
    right[d:] = right[d:] * (1 - gain) + x[:-d] * gain
    return left, right


def stereoize(buses: dict[str, np.ndarray], seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Buses -> stereo pair. Bass/drums mostly centered, mel/fx widened.
    Optional `samples` bus sits with the drums (mostly centered, slight width)."""
    rng = np.random.default_rng(seed)
    drums, bass = buses["drums"], buses["bass"]
    mel, fx = buses["mel"], buses["fx"]
    smp = buses.get("samples")
    others = [b for b in (drums, bass, mel, fx, smp) if b is not None]
    n = max(len(b) for b in others)

    def fit(b):
        if b is None:
            return np.zeros(n)
        if len(b) < n:
            return np.pad(b, (0, n - len(b)))
        return b[:n]

    drums, bass, mel, fx = fit(drums), fit(bass), fit(mel), fit(fx)
    smp = fit(smp) if smp is not None else None

    # subtle drum width: hats-ish top end slightly widened via short haas on HP
    d_hp = dsp.hp(drums, 6000)
    d_core = drums - d_hp
    dl, dr = _haas(d_hp, 9.0, 0.5)
    drums_l = d_core + dl
    drums_r = d_core + dr

    # bass mono (club correctness)
    bass_l = bass_r = bass

    # melody wide: decorrelate via allpass-ish micro delays per side
    mel_hp = dsp.hp(mel, 220)
    mel_lo = mel - mel_hp
    ml, mr = _haas(mel_hp, 14.0, 0.4)
    mel_l = mel_lo + ml
    mel_r = mel_lo + mr

    # fx very wide with slow LFO movement
    fx_l, fx_r = _haas(fx, 21.0, 0.55)
    lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.07 * np.arange(n) / dsp.SR)
    mid = (fx_l + fx_r) * 0.5
    width = 0.5 + 0.3 * lfo
    fx_l = mid + (fx_l - fx_r) * 0.5 * width * 2
    fx_r = mid - (fx_l - fx_r) * 0.5 * width * 2

    mix_l = drums_l * 1.0 + bass_l * 0.85 + mel_l * 0.62 + fx_l * 0.55
    mix_r = drums_r * 1.0 + bass_r * 0.85 + mel_r * 0.62 + fx_r * 0.55
    if smp is not None and float(np.max(np.abs(smp))) > 1e-9:
        s_hp = dsp.hp(smp, 300)
        s_lo = smp - s_hp
        sl, sr_ = _haas(s_hp, 11.0, 0.45)  # slight width on the top of samples
        mix_l = mix_l + s_lo + sl
        mix_r = mix_r + s_lo + sr_
    return mix_l, mix_r


def master(l: np.ndarray, r: np.ndarray, loud: float = 0.94) -> tuple[np.ndarray, np.ndarray]:
    """Gentle glue compression (tanh), HP rumble control, soft-clip limit."""
    m = (l + r) * 0.5
    s = (l - r) * 0.5
    m = dsp.hp(m, 24)
    m = dsp.dist_drive(m, 1.25, mix=0.5)
    s = dsp.dist_drive(s, 1.15, mix=0.4)
    s *= 1.1
    l, r = m + s, m - s
    # normalize into the soft clipper so tanh works as a loudness limiter
    peak = max(float(np.max(np.abs(l))), float(np.max(np.abs(r)))) or 1.0
    l, r = l / peak, r / peak
    l = np.tanh(l * 2.8) * 0.6
    r = np.tanh(r * 2.8) * 0.6
    # final loudness: RMS target, peak-safety limited
    rms = float(np.sqrt(np.mean((l * 0.5 + r * 0.5) ** 2))) or 1e-9
    rms_target = 0.20
    gain = min(rms_target / rms, loud / max(float(np.max(np.abs(l))),
                                            float(np.max(np.abs(r)))) or 1.0)
    l, r = l * gain, r * gain
    # micro fades to kill clicks
    f = min(64, len(l))
    l[:f] *= np.linspace(0, 1, f); r[:f] *= np.linspace(0, 1, f)
    l[-f:] *= np.linspace(1, 0, f); r[-f:] *= np.linspace(1, 0, f)
    return l, r


def write_wav(path: str, l: np.ndarray, r: np.ndarray) -> None:
    assert len(l) == len(r)
    data = np.empty((len(l), 2))
    data[:, 0] = l
    data[:, 1] = r
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(dsp.SR)
        w.writeframes(pcm.tobytes())


def write_wav_float32(path: str, l: np.ndarray, r: np.ndarray) -> None:
    """32-bit float WAV (no clipping on export; peak data preserved)."""
    import struct
    assert len(l) == len(r)
    inter = np.empty((len(l), 2), dtype="<f4")
    inter[:, 0] = l
    inter[:, 1] = r
    data = inter.tobytes()
    byte_rate = dsp.SR * 2 * 4
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16,
        3,             # IEEE float
        2, dsp.SR, byte_rate, 8, 32,
        b"data", len(data))
    with open(path, "wb") as f:
        f.write(hdr)
        f.write(data)


STEM_BUSES = ("drums", "bass", "mel", "fx", "samples", "master")


def write_stems(directory: str, buses: dict[str, np.ndarray],
                l: np.ndarray, r: np.ndarray,
                fmt: str = "float32") -> list[str]:
    """Write per-bus stems + master into `directory`. Returns written paths.

    Mastering note (spec §19): `buses` are POST mix leveling but PRE
    stereoize/master. Stems therefore sum to the pre-master mix; only the
    `master` stem has the master chain applied. Mastering is never
    double-applied to the other stems.
    """
    import os
    os.makedirs(directory, exist_ok=True)
    writer = write_wav_float32 if fmt == "float32" else write_wav
    n = len(l)
    written = []
    for bus in STEM_BUSES[:-1]:
        b = buses.get(bus)
        if b is None:
            b = np.zeros(n)  # keep the layout complete (e.g. procedural-only runs)
        b = b[:n] if len(b) >= n else np.pad(b, (0, n - len(b)))
        p = os.path.join(directory, f"{bus}.wav")
        writer(p, b, b.copy())
        written.append(p)
    p = os.path.join(directory, "master.wav")
    writer(p, l, r)
    written.append(p)
    return written


def qc_stems(buses: dict[str, np.ndarray], l: np.ndarray, r: np.ndarray) -> list[str]:
    """Per-stem QC (spec §18). Buses differ musically; so do the checks."""
    notes = []
    checks = {
        "drums": dict(silence=True, clip=True),
        "bass": dict(silence=True, clip=True, lowend=True),
        "mel": dict(silence=True, clip=True),
        "fx": dict(clip=True),
        "samples": dict(silence=False, clip=True),
    }
    for bus, c in checks.items():
        b = buses.get(bus)
        if b is None or not len(b):
            continue
        rms = float(np.sqrt(np.mean(b ** 2)))
        pk = float(np.max(np.abs(b)))
        if c.get("silence") and rms < 1e-5:
            notes.append(f"stem {bus}: silent")
        if c.get("clip") and pk > 0.999:
            notes.append(f"stem {bus}: clipping (peak {pk:.3f})")
        if c.get("lowend"):
            lo = float(np.sqrt(np.mean(dsp.lp(b, 120) ** 2)))
            if lo > rms * 0.9 and bus == "bass" and lo > 0.3:
                notes.append(f"stem {bus}: excessive sub energy ({lo:.2f} rms)")
    mrms = float(np.sqrt(np.mean(((l + r) * 0.5) ** 2)))
    mpk = max(float(np.max(np.abs(l))), float(np.max(np.abs(r))))
    if mrms < 0.05:
        notes.append(f"stem master: very quiet (rms {mrms:.3f})")
    if mpk > 0.999:
        notes.append(f"stem master: clipping (peak {mpk:.3f})")
    return notes
