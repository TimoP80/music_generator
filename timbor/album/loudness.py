"""timbor.album.loudness — album loudness analysis (spec §9–§10, §11).

Measurement method (documented, not faked):

* **Loudness** — ITU-R BS.1770-style: K-weighting (stage 1 high-shelf +
  stage 2 high-pass RLB), 400 ms blocks with 75% overlap, absolute gate at
  −70 LUFS and relative gate at −10 LU below the ungated mean, channel
  weights 1.0 for L/R. Implemented in-process with biquad coefficients
  derived at the true sample rate (the standard's 48 kHz coefficients are
  re-bilinearized here — a documented deviation from stock 48 kHz
  coefficient tables). Values are labeled ``lufs_approx``: faithful to the
  algorithm, but this is not certified meter software. RMS is reported
  separately and never labeled LUFS.
* **True peak** — 4x linear-interpolation oversampling of the digital peak
  (a practical approximation, not a certified 4x polyphase phase-linear
  oversampler; labeled accordingly).
* **Loudness range** — ABS 1770-style: LRA = P95(short-term, gated) −
  P10(short-term, gated) with the integrated-loudness relative gate.

All measurements are deterministic: no RNG, no time, no threading.
"""
from __future__ import annotations

import json
import os
import struct

import numpy as np

# ---------------------------------------------------------------------------
# WAV I/O (assembly layer reads; never writes track masters)
# ---------------------------------------------------------------------------

def read_wav_stereo(path: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Read a RIFF/WAVE file as float64 L/R at native gain.

    Supports 32-bit IEEE float, 16-bit PCM and 24-bit PCM, mono/stereo.
    Returns (left, right, sample_rate); mono is duplicated.
    """
    with open(path, "rb") as f:
        hdr = f.read(12)
        if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            raise ValueError(f"not a RIFF/WAVE file: {path}")
        fmt = None
        raw = None
        while True:
            ch_hdr = f.read(8)
            if len(ch_hdr) < 8:
                break
            cid, csz = ch_hdr[:4], struct.unpack("<I", ch_hdr[4:8])[0]
            body = f.read(csz + (csz & 1))  # chunks are word-aligned
            if cid == b"fmt ":
                fmt = struct.unpack("<HHIIHH", body[:16])
            elif cid == b"data":
                raw = body[:csz]
    if fmt is None or raw is None:
        raise ValueError(f"missing fmt/data chunk: {path}")
    tag, ch, sr, _brate, _align, bits = fmt
    if tag not in (1, 3, 0xFFFE):
        raise ValueError(f"unsupported WAV format tag {tag}: {path}")
    if bits == 32:
        a = np.frombuffer(raw, "<f4").astype(np.float64)
    elif bits == 16:
        a = np.frombuffer(raw, "<i2").astype(np.float64) / 32767.0
    elif bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        a = ((b[:, 0] << 8) | (b[:, 1] << 16) | (b[:, 2] << 24)) / float(1 << 31)
    else:
        raise ValueError(f"unsupported bit depth {bits}: {path}")
    if ch == 1:
        return a, a.copy(), sr
    if ch == 2:
        return a[0::2], a[1::2], sr
    raise ValueError(f"unsupported channel count {ch}: {path}")


def read_master_master(project_root: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Read <project_root>/audio/master.wav (the track master, unmodified)."""
    return read_wav_stereo(os.path.join(project_root, "audio", "master.wav"))


def db(x: float) -> float:
    return 10.0 * float(np.log10(max(x, 1e-12)))


def dbfs(x: float) -> float:
    return 20.0 * float(np.log10(max(x, 1e-12)))


# ---------------------------------------------------------------------------
# K-weighting (BS.1770) — biquads designed at the true sample rate,
# applied as the exact truncated impulse response via FFT convolution
# (numpy FFT is deterministic per version, matching engine.py's FFT-filter
# convention; a per-sample biquad loop would be too slow at album length)
# ---------------------------------------------------------------------------

class _Biquad:
    """Direct-form-1 biquad coefficients (normalized)."""

    __slots__ = ("b0", "b1", "b2", "a1", "a2")

    def __init__(self, b0, b1, b2, a1, a2):
        self.b0, self.b1, self.b2 = b0, b1, b2
        self.a1, self.a2 = a1, a2

    def impulse_response(self, n: int = 8192) -> np.ndarray:
        """First n samples of the filter's impulse response (exact recurrence)."""
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        z1 = z2 = 0.0
        out = np.empty(n)
        for i in range(n):
            xi = 1.0 if i == 0 else 0.0
            y = b0 * xi + z1
            z1 = b1 * xi - a1 * y + z2
            z2 = b2 * xi - a2 * y
            out[i] = y
        return out


def _high_shelf(fc: float, gain_db: float, q: float, sr: int) -> _Biquad:
    # RBJ cookbook high shelf, bilinear transform
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * fc / sr
    cw = np.cos(w0)
    alpha = np.sin(w0) / (2.0 * q)
    two_sq_A_alpha = 2.0 * np.sqrt(A) * alpha
    a0 = (A + 1.0) - (A - 1.0) * cw + two_sq_A_alpha
    b0 = A * ((A + 1.0) + (A - 1.0) * cw + two_sq_A_alpha)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cw)
    b2 = A * ((A + 1.0) + (A - 1.0) * cw - two_sq_A_alpha)
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cw)
    a2 = (A + 1.0) - (A - 1.0) * cw - two_sq_A_alpha
    return _Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def _high_pass(fc: float, q: float, sr: int) -> _Biquad:
    # RBJ cookbook high-pass, bilinear transform
    w0 = 2.0 * np.pi * fc / sr
    cw = np.cos(w0)
    alpha = np.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    b0 = (1.0 + cw) / 2.0
    b1 = -(1.0 + cw)
    b2 = (1.0 + cw) / 2.0
    a1 = -2.0 * cw
    a2 = 1.0 - alpha
    return _Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


_FIR_TAPS = 8192


def _k_weight_fir(sr: int) -> np.ndarray:
    """Combined K-weighting impulse response: the cascade of shelf → RLB
    is the convolution of the two stages' impulse responses (truncated)."""
    shelf = _high_shelf(1681.974450955533, 3.99984385397, 0.7071752369554196, sr)
    hp = _high_pass(38.13547087602444, 0.5003270373238773, sr)
    h = np.convolve(shelf.impulse_response(_FIR_TAPS),
                    hp.impulse_response(_FIR_TAPS))
    return h[:_FIR_TAPS]


_OLA_BLOCK = 1 << 20


def k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770 K-weighting via FFT fast convolution with the exact 8192-tap
    truncated cascade impulse response. Long inputs use deterministic
    overlap-add (mathematically exact linear convolution, bounded memory,
    fixed block size ⇒ reproducible)."""
    fir = _k_weight_fir(sr)
    L = len(fir)
    x = np.asarray(x, dtype=np.float64)  # no copy when already float64
    n = len(x)
    if n + L - 1 <= _OLA_BLOCK * 2:
        nfft = 1 << (n + L - 2).bit_length()
        X = np.fft.rfft(x, nfft)
        H = np.fft.rfft(fir, nfft)
        return np.fft.irfft(X * H, nfft)[:n]
    # overlap-add: partial convolutions summed at their exact offsets
    nfft = 1 << (_OLA_BLOCK + L - 2).bit_length()
    H = np.fft.rfft(fir, nfft)
    out = np.zeros(n + L - 1)
    for s0 in range(0, n, _OLA_BLOCK):
        seg = x[s0:s0 + _OLA_BLOCK]
        Y = np.fft.irfft(np.fft.rfft(seg, nfft) * H, nfft)
        out[s0:s0 + len(Y)] += Y[: min(len(Y), len(out) - s0)]
    return out[:n]


# ---------------------------------------------------------------------------
# measurement core
# ---------------------------------------------------------------------------

def _blocks(x: np.ndarray, sr: int, block_s: float = 0.400,
            overlap: float = 0.75):
    """Yield (start_sample, block_length) for overlapped analysis blocks."""
    step = int(round(sr * block_s * (1.0 - overlap)))  # 100 ms at defaults
    blen = int(round(sr * block_s))
    n = len(x)
    if n < blen:
        return
    for start in range(0, n - blen + 1, step):
        yield start, blen


def _block_loudness(xk: np.ndarray, start: int, blen: int) -> float:
    """BS.1770 block loudness in LUFS from K-weighted channel audio."""
    seg = xk[start:start + blen]
    mean_sq = float(np.mean(seg * seg))
    if mean_sq <= 0.0:
        return -70.0 - 1.0
    return -0.691 + db(mean_sq)


def _gated_mean(blocks_l: list[float]) -> float:
    """Two-stage gate: absolute −70 LUFS, then relative −10 LU. Returns
    the gated mean loudness (LUFS), or −inf when nothing passes."""
    if not blocks_l:
        return float("-inf")
    arr = np.asarray(blocks_l)
    pass1 = arr[arr > -70.0]
    if len(pass1) == 0:
        return float("-inf")
    rel_thresh = float(pass1.mean()) - 10.0
    pass2 = pass1[pass1 > rel_thresh]
    if len(pass2) == 0:
        return float("-inf")
    return float(pass2.mean())


def measure_track(l: np.ndarray, r: np.ndarray, sr: int) -> dict:
    """Full loudness/level analysis of one stereo master (deterministic)."""
    l = np.asarray(l, dtype=np.float64)   # never mutated below
    r = np.asarray(r, dtype=np.float64)
    n = len(l)
    dur = n / float(sr)
    peak = float(max(np.max(np.abs(l)), np.max(np.abs(r)))) if n else 0.0

    # true-peak approximation: 4x linear-interpolation oversampling,
    # computed in chunks (a full 4x float64 upsample of an album would
    # allocate hundreds of MB)
    tp = peak
    if n > 2:
        m = max(l, r, key=len)
        tp = 0.0
        chunk = 1 << 20
        for s0 in range(0, max(1, len(m) - 1), chunk):
            seg = m[s0:min(len(m), s0 + chunk + 1)]
            if len(seg) < 2:
                continue
            up = np.interp(np.arange(0, len(seg) - 1, 0.25),
                           np.arange(len(seg)), seg)
            tp = max(tp, float(np.max(np.abs(up))))

    rms = float(np.sqrt(np.mean((l * l + r * r) / 2.0))) if n else 0.0

    lk = k_weight(l, sr)
    rk = k_weight(r, sr)
    mono_k = (lk + rk) / np.sqrt(2.0)   # equal-power L/R sum, weight 1.0 each
    del lk, rk                          # release the channel buffers early

    block_l: list[float] = []
    for start, blen in _blocks(mono_k, sr):
        block_l.append(_block_loudness(mono_k, start, blen))
    integrated = _gated_mean(block_l)

    # short-term: 3 s window, 1 s hop, per-window gated-style value
    st: list[float] = []
    wlen = int(round(sr * 3.0))
    if n >= wlen:
        for s0 in range(0, n - wlen + 1, sr):
            seg = mono_k[s0:s0 + wlen]
            ms = float(np.mean(seg * seg))
            st.append(-0.691 + db(ms) if ms > 0 else -70.0 - 1.0)

    # loudness range: ABS 1770-style percentiles of gated short-term
    lra = 0.0
    if st:
        st_arr = np.asarray(st)
        st_g = st_arr[st_arr > integrated - 20.0] if np.isfinite(integrated) \
            else st_arr
        if len(st_g) >= 2:
            lra = float(np.percentile(st_g, 95) - np.percentile(st_g, 10))

    # silence at boundaries: leading/trailing samples below −60 dBFS
    # (chunked scan of (l+r)/2 — same values, no album-length mono copy)
    thresh = 10.0 ** (-60.0 / 20.0)
    step = 1 << 20
    lead_i = trail_i = -1
    for s0 in range(0, n, step):
        e0 = min(n, s0 + step)
        seg = (l[s0:e0] + r[s0:e0]) * 0.5
        hit = np.flatnonzero(np.abs(seg) > thresh)
        if len(hit):
            lead_i = s0 + int(hit[0])
            break
    for s0 in range(n, 0, -step):
        s1 = max(0, s0 - step)
        seg = (l[s1:s0] + r[s1:s0]) * 0.5
        hit = np.flatnonzero(np.abs(seg) > thresh)
        if len(hit):
            trail_i = s1 + int(hit[-1])
            break
    lead = float(lead_i) / sr if lead_i >= 0 else dur
    trail = float(n - 1 - trail_i) / sr if trail_i >= 0 else 0.0

    return {
        "method": "itu_bs1770_style_k_weighted_gated",
        "duration_seconds": round(dur, 3),
        "peak_dbfs": round(dbfs(peak), 2),
        "true_peak_dbfs_approx": round(dbfs(tp), 2),
        "rms_dbfs": round(dbfs(rms), 2),
        "integrated_lufs_approx": (round(integrated, 2)
                                   if np.isfinite(integrated) else None),
        "short_term_lufs": [round(v, 2) for v in st],
        "loudness_range_lu": round(lra, 2),
        "silence_lead_seconds": round(lead, 4),
        "silence_tail_seconds": round(trail, 4),
        "samples": n,
    }


# ---------------------------------------------------------------------------
# file-level analysis + deterministic report
# ---------------------------------------------------------------------------

def analyze_file(path: str) -> dict:
    """Measure a master WAV from disk."""
    l, r, sr = read_wav_stereo(path)
    return measure_track(l, r, sr)


def analyze_album(album: dict, album_root: str,
                  assembled_path: str | None = None) -> dict:
    """Measure every track master (album-relative paths) + optionally the
    assembled album master. Returns the loudness.json document."""
    tracks = []
    for t in album["tracks"]:
        master_rel = os.path.join(t["directory"], "audio", "master.wav")
        p = os.path.join(album_root, master_rel)
        m = analyze_file(p)
        m.update({"position": int(t["number"]), "title": t.get("title"),
                  "genre": t.get("genre"), "bpm": t.get("bpm"),
                  "master": master_rel.replace(os.sep, "/")})
        tracks.append(m)
    doc = {
        "format": "timbor-loudness",
        "version": 1,
        "method": tracks[0]["method"] if tracks else
                  "itu_bs1770_style_k_weighted_gated",
        "method_note": "K-weighted, 400ms/75% gated blocks; in-process "
                       "implementation (not certified metering). True peak: "
                       "4x linear-interpolation approximation. RMS is "
                       "reported separately and never labeled LUFS.",
        "tracks": tracks,
    }
    if assembled_path and os.path.isfile(assembled_path):
        doc["album"] = analyze_file(assembled_path)
    return doc


def format_loudness_report(doc: dict) -> str:
    """Deterministic diagnostics table (spec §10 — no ranking)."""
    lines = [f"TIMBOR LOUDNESS REPORT  (method: {doc.get('method')})", "",
             f"{'Track':22s} {'Peak':>9s} {'TruePk':>9s} {'RMS':>9s} "
             f"{'Loudness':>10s} {'LRA':>7s} {'Dur':>7s}",
             "-" * 79]
    for t in doc["tracks"]:
        title = f"{t['position']:02d} {(t.get('title') or '')[:16]}"
        integ = (f"{t['integrated_lufs_approx']:.1f}"
                 if t.get("integrated_lufs_approx") is not None else "--")
        lines.append(f"{title:22s} {t['peak_dbfs']:8.1f}dB "
                     f"{t['true_peak_dbfs_approx']:8.1f}dB "
                     f"{t['rms_dbfs']:8.1f}dB {integ:>8s}LU "
                     f"{t['loudness_range_lu']:6.1f} {t['duration_seconds']:6.1f}s")
    if "album" in doc:
        a = doc["album"]
        integ = (f"{a['integrated_lufs_approx']:.1f}"
                 if a.get("integrated_lufs_approx") is not None else "--")
        lines.append("-" * 79)
        lines.append(f"{'ALBUM':22s} {a['peak_dbfs']:8.1f}dB "
                     f"{a['true_peak_dbfs_approx']:8.1f}dB "
                     f"{a['rms_dbfs']:8.1f}dB {integ:>8s}LU "
                     f"{a['loudness_range_lu']:6.1f} {a['duration_seconds']:6.1f}s")
    lines.append("")
    lines.append("measurement: " + doc.get("method_note", ""))
    return "\n".join(lines)


def save_loudness(path: str, doc: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    return path
