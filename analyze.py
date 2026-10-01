"""analyze.py — quick sanity checks: section RMS (energy curve), spectral balance."""
import sys
import wave
import numpy as np


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64) / 32767.0
    return data.reshape(-1, ch), sr


def main(path, n_sections=8):
    data, sr = read_wav(path)
    mono = data.mean(axis=1)
    dur = len(mono) / sr
    print(f"{path}: {dur:.1f}s  peak={np.max(np.abs(mono)):.2f}")

    # energy curve in sections
    chunks = np.array_split(mono, n_sections)
    rms = [float(np.sqrt(np.mean(c ** 2))) for c in chunks]
    print("energy curve (RMS per ~{:.0f}s):".format(dur / n_sections))
    for i, r in enumerate(rms):
        bar = "#" * int(r * 200)
        print(f"  section {i}: {r:.3f} {bar}")

    # spectral thirds — Welch-style average over fixed-size chunks (no decimation)
    win = 2 ** 17
    n_chunks = min(8, len(mono) // win)
    mag = np.zeros(win // 2 + 1)
    for c in range(n_chunks):
        seg = mono[c * win:(c + 1) * win] * np.hanning(win)
        mag += np.abs(np.fft.rfft(seg)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    bands = {"sub/bass 20-120": (20, 120), "low 120-400": (120, 400),
             "mid 400-2k": (400, 2000), "high 2k-8k": (2000, 8000),
             "air 8k+": (8000, 20000)}
    tot = float(mag.sum()) or 1.0
    print("spectral balance:")
    for name, (lo, hi) in bands.items():
        m = (freqs >= lo) & (freqs < hi)
        share = float(mag[m].sum()) / tot
        print(f"  {name:16s} {share*100:5.1f}%")

    # silence detection
    hop = 4410
    fr = mono[: len(mono) // hop * hop].reshape(-1, hop)
    rms_f = np.sqrt(np.mean(fr ** 2, axis=1))
    silent = float((rms_f < 1e-4).mean())
    print(f"silent frames: {silent*100:.1f}%")
    clipped = float((np.abs(data) > 0.985).mean())
    print(f"clipped samples: {clipped*100:.3f}%")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "track.wav",
         int(sys.argv[2]) if len(sys.argv) > 2 else 8)
