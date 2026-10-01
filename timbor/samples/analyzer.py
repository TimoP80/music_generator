"""timbor.samples.analyzer — practical offline DSP analysis.

Reliable DSP, not ML: onset-envelope autocorrelation for BPM, chroma +
Krumhansl profiles for key, windowed stats for spectral shape. Confidence
values reflect evidence quality honestly.
"""
from __future__ import annotations

import numpy as np

from timbor import dsp
from timbor.theory import NOTE_NAMES
from .metadata import SampleMetadata
from . import scanner

# Krumhansl-Schmuckler key profiles (major, minor)
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

_ANALYSIS_SR = 22050  # downsample analysis audio for speed
_MAX_ANALYSIS_S = 30.0


def analyze_file(path: str, fmt: str) -> SampleMetadata:
    """Full analysis of one file. Raises on undecodable audio."""
    import os
    st = os.stat(path)
    m = SampleMetadata(path=path, filename=os.path.basename(path),
                       size=st.st_size, mtime=st.st_mtime)
    m.fingerprint = SampleIndex_fingerprint(m.size, m.mtime)

    hdr = scanner.read_header(path, fmt)
    if hdr.get("error"):
        raise ValueError(hdr["error"])
    m.sample_rate = hdr.get("sample_rate", 44100)
    m.channels = hdr.get("channels", 1)
    m.duration = hdr.get("duration", 0.0)

    try:
        mono, sr = scanner.decode_audio(path, fmt, max_seconds=_MAX_ANALYSIS_S)
    except ValueError as e:
        if "no native decoder" in str(e):
            # index without audio analysis (flac/mp3) — usable for stats only
            m.error = "no-decoder"
            return m
        raise

    if len(mono) < sr // 4:  # < 0.25 s: still classify as one-shot
        _basic_features(m, mono, sr)
        m.analyzed = True
        return m

    # resample to analysis SR by linear interp (good enough for features)
    if sr != _ANALYSIS_SR:
        n_new = int(len(mono) * _ANALYSIS_SR / sr)
        mono = np.interp(np.linspace(0, len(mono) - 1, n_new),
                         np.arange(len(mono)), mono)
        sr = _ANALYSIS_SR

    _basic_features(m, mono, sr)
    _spectral_features(m, mono, sr)
    _rhythm(m, mono, sr)
    _tonal(m, mono, sr)
    m.analyzed = True
    return m


def SampleIndex_fingerprint(size: int, mtime: float) -> str:
    from .cache import SampleIndex
    return SampleIndex.fingerprint(size, mtime)


def _basic_features(m: SampleMetadata, x: np.ndarray, sr: int) -> None:
    m.peak = float(np.max(np.abs(x))) if len(x) else 0.0
    m.rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    m.loudness = 20 * np.log10(max(m.rms, 1e-6))
    m.energy = float(np.clip(m.rms * 3.0, 0.0, 1.0))
    # zero crossing rate
    if len(x) > 1:
        zc = np.nonzero(np.diff(np.signbit(x)))[0]
        m.zero_crossing_rate = float(len(zc) / (len(x) / sr))


def _spectral_features(m: SampleMetadata, x: np.ndarray, sr: int) -> None:
    win = 2048
    hop = 1024
    n = len(x) // hop
    if n < 2:
        m.tonalness = 0.0
        return
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    w = np.hanning(win)
    cent = bw = roll = 0.0
    sub = lo = mid = hi = 0.0
    flat_sum = 0.0
    count = 0
    for i in range(0, (len(x) - win) // hop, max(1, (len(x) - win) // hop // 32)):
        seg = x[i * hop:i * hop + win] * w
        S = np.abs(np.fft.rfft(seg)) ** 2
        tot = S.sum() + 1e-12
        cent += float((S * freqs).sum() / tot)
        bw += float(np.sqrt(((freqs - cent / (count + 1)) ** 2 * S).sum() / tot))
        cum = np.cumsum(S)
        ri = min(int(np.searchsorted(cum, 0.85 * tot)), len(freqs) - 1)
        roll += float(freqs[ri])
        sub += float(S[(freqs >= 20) & (freqs < 120)].sum() / tot)
        lo += float(S[(freqs >= 120) & (freqs < 400)].sum() / tot)
        mid += float(S[(freqs >= 400) & (freqs < 2000)].sum() / tot)
        hi += float(S[(freqs >= 2000) & (freqs < 8000)].sum() / tot)
        flat_sum += float(np.exp(np.mean(np.log(S[1:] + 1e-12))) *
                          (len(S) - 1) / (tot / (len(S) - 1) + 1e-12))
        count += 1
    if count:
        m.spectral_centroid = cent / count
        m.spectral_bandwidth = bw / count
        m.spectral_rolloff = roll / count
        m.sub_energy = sub / count
        m.low_energy = lo / count
        m.mid_energy = mid / count
        m.high_energy = hi / count
        m.tonalness = float(np.clip(1.0 - flat_sum / count, 0.0, 1.0))


def _onset_envelope(x: np.ndarray, sr: int, hop: int = 256) -> np.ndarray:
    """Spectral-flux onset strength envelope."""
    win = 1024
    w = np.hanning(win)
    n_frames = max(1, (len(x) - win) // hop)
    prev = None
    env = np.zeros(n_frames)
    for i in range(n_frames):
        seg = x[i * hop:i * hop + win] * w
        mag = np.abs(np.fft.rfft(seg))
        if prev is not None:
            d = mag - prev
            env[i] = float(d[d > 0].sum())
        prev = mag
    if len(env) > 8:
        env -= env.mean()
        env = np.clip(env, 0, None)
    return env


def _rhythm(m: SampleMetadata, x: np.ndarray, sr: int) -> None:
    env = _onset_envelope(x, sr)
    fps = sr / 256
    # transient density: normalized peak count above adaptive threshold
    if len(env) < 8:
        m.transient_density = 0.0
        return
    thr = env.mean() + 0.8 * env.std()
    peaks = 0
    for i in range(1, len(env) - 1):
        if env[i] > thr and env[i] >= env[i - 1] and env[i] >= env[i + 1]:
            peaks += 1
    dur = len(x) / sr
    m.transient_density = peaks / max(dur, 0.1)

    # BPM via autocorrelation of the onset envelope (70-190 BPM search)
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    ac /= (ac[0] + 1e-12)
    lag_lo = int(fps * 60 / 190)
    lag_hi = int(fps * 60 / 70)
    if lag_hi <= lag_lo + 1 or lag_hi >= len(ac):
        m.bpm = None
        m.bpm_confidence = 0.0
        return
    seg = ac[lag_lo:lag_hi]
    best = int(np.argmax(seg)) + lag_lo
    # parabolic refine
    if 0 < best - lag_lo < len(seg) - 1:
        y0, y1, y2 = seg[best - lag_lo - 1], seg[best - lag_lo], seg[best - lag_lo + 1]
        denom = (y0 - 2 * y1 + y2)
        shift = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-9 else 0.0
        best = best + shift
    bpm = 60.0 * fps / best
    # octave disambiguation: dance rhythms — prefer the faster reading when the
    # double-time lag is nearly as strong (avoid half-time lock on 4x4 loops)
    half_lag = best / 2.0
    if half_lag >= lag_lo:
        lo_i, hi_i = lag_lo, lag_hi
        hl = int(round(half_lag))
        if lag_lo <= hl < len(ac):
            if ac[hl] >= 0.65 * ac[int(lag_lo + (best - lag_lo))]:
                bpm *= 2
    # fold into 70-190
    while bpm < 70:
        bpm *= 2
    while bpm > 190:
        bpm /= 2
    m.bpm = round(bpm, 2)
    m.bpm_confidence = float(np.clip(ac[int(lag_lo + (best - lag_lo))], 0.0, 1.0))
    m.is_loop = bool(dur >= 1.5 and m.bpm_confidence > 0.25)
    m.is_one_shot = bool(dur < 1.5 and not m.is_loop)


def _tonal(m: SampleMetadata, x: np.ndarray, sr: int) -> None:
    """Chroma + Krumhansl key estimate; honest confidence."""
    if m.tonalness < 0.25:  # percussive/noise material: don't pretend
        m.key = None
        m.key_confidence = 0.0
        return
    win = 4096
    w = np.hanning(win)
    chroma = np.zeros(12)
    n_frames = 0
    for i in range(0, max(1, (len(x) - win) // win), 2):
        seg = x[i * win:i * win + win] * w
        S = np.abs(np.fft.rfft(seg))
        freqs = np.fft.rfftfreq(win, 1.0 / sr)
        for pc in range(12):
            mask = np.zeros(len(freqs), dtype=bool)
            for octv in range(1, 9):
                f = 440.0 * 2 ** ((pc - 9) / 12 + octv - 4)
                if f < freqs[-1]:
                    mask |= (freqs > f * 0.985) & (freqs < f * 1.015)
            chroma[pc] += float((S * mask).sum())
        n_frames += 1
    if not n_frames or chroma.sum() <= 0:
        m.key = None
        m.key_confidence = 0.0
        return
    chroma /= chroma.sum()
    best_corr = -2
    best = None
    second = -2
    for root in range(12):
        c = np.roll(chroma, -root)
        for prof, mode in ((_KS_MAJOR, "maj"), (_KS_MINOR, "min")):
            r = float(np.corrcoef(c, prof)[0, 1])
            if r > best_corr:
                second = best_corr
                best_corr = r
                best = (root, mode)
            elif r > second:
                second = r
    if best is None:
        m.key = None
        m.key_confidence = 0.0
        return
    root, mode = best
    m.root_note = NOTE_NAMES[root]
    m.key = f"{NOTE_NAMES[root]}:{'min' if mode == 'min' else 'maj'}"
    # confidence: correlation strength + margin over second-best
    margin = best_corr - max(second, 0)
    m.key_confidence = float(np.clip(best_corr * 0.7 + margin * 1.2, 0.0, 1.0))
