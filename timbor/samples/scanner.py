"""timbor.samples.scanner — recursive file discovery.

Never decodes audio at scan time; only stats files and reads cheap headers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

EXT_FORMATS = {
    ".wav": "wav", ".wave": "wav",
    ".aif": "aiff", ".aiff": "aiff",
    ".flac": "flac",
    ".mp3": "mp3",
}


@dataclass
class ScannedFile:
    path: str
    size: int
    mtime: float
    format: str


def scan_directory(root: str, follow_symlinks: bool = False) -> list[ScannedFile]:
    """Recursively find supported audio files. No audio decoding here."""
    out: list[ScannedFile] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        dirnames.sort()
        if not follow_symlinks:
            is_junction = getattr(os.path, "isjunction", lambda _path: False)
            dirnames[:] = [name for name in dirnames
                           if not os.path.islink(os.path.join(dirpath, name))
                           and not is_junction(os.path.join(dirpath, name))]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            fmt = EXT_FORMATS.get(ext)
            if not fmt:
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append(ScannedFile(p, st.st_size, st.st_mtime, fmt))
    return out


def read_header(path: str, fmt: str) -> dict:
    """Cheap header info without decoding audio."""
    info = {"sample_rate": 44100, "channels": 1, "duration": 0.0, "frames": 0}
    if fmt == "wav":
        import wave
        try:
            with wave.open(path, "rb") as w:
                info["sample_rate"] = w.getframerate()
                info["channels"] = w.getnchannels()
                info["frames"] = w.getnframes()
                info["duration"] = info["frames"] / max(1, info["sample_rate"])
        except (wave.Error, EOFError, OSError):
            info["error"] = "unreadable wav header"
    elif fmt == "aiff":
        import aifc
        try:
            with aifc.open(path, "rb") as a:
                info["sample_rate"] = a.getframerate()
                info["channels"] = a.getnchannels()
                info["frames"] = a.getnframes()
                info["duration"] = info["frames"] / max(1, info["sample_rate"])
        except Exception:
            info["error"] = "unreadable aiff header"
    elif fmt == "flac":
        try:
            with open(path, "rb") as f:
                data = f.read(64)
            # fLaC magic + STREAMINFO: sample rate 20 bits, channels 3 bits
            if data[:4] == b"fLaC" and len(data) >= 42:
                b = data[18:26]
                sr = ((b[10] & 0xF0) << 8) | (b[11] << 4) | (b[12] >> 4)
                ch = ((b[12] >> 1) & 0x07) + 1
                total = ((b[13] & 0x0F) << 32) | (b[14] << 24) | (b[15] << 16) | \
                        (b[16] << 8) | b[17]
                info["sample_rate"] = sr
                info["channels"] = ch
                info["frames"] = total
                info["duration"] = total / max(1, sr)
        except OSError:
            info["error"] = "unreadable flac header"
    elif fmt == "mp3":
        # duration from Xing/Info header or CBR estimate via file size/bitrate
        try:
            with open(path, "rb") as f:
                data = f.read(min(os.path.getsize(path), 1 << 16))
            dur = _mp3_duration_estimate(path, data)
            info["duration"] = dur
            info["sample_rate"] = 44100
        except OSError:
            info["error"] = "unreadable mp3"
    return info


def _mp3_duration_estimate(path: str, head: bytes) -> float:
    """CBR estimate from the first valid frame header; Xing frame count if present."""
    br_map = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    sr_map = {0: 44100, 1: 48000, 2: 32000}
    size = os.path.getsize(path)
    i = 0
    while i < len(head) - 4:
        if head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0:
            br = br_map[(head[i + 2] >> 4) & 0x0F]
            sr = sr_map.get((head[i + 2] >> 2) & 0x03, 44100)
            if br and sr:
                # check for Xing/Info with frame count
                if b"Xing" in head[i:i + 200] or b"Info" in head[i:i + 200]:
                    j = head.find(b"Xing", i, i + 200)
                    if j < 0:
                        j = head.find(b"Info", i, i + 200)
                    flags = int.from_bytes(head[j + 8:j + 12], "big")
                    if flags & 0x01 and len(head) > j + 16:
                        frames = int.from_bytes(head[j + 12:j + 16], "big")
                        return frames * 1152 / sr
                return size * 8 / (br * 1000)
        i += 1
    return 0.0


def decode_audio(path: str, fmt: str, max_seconds: float | None = None) -> tuple:
    """Decode to (mono float64 np.ndarray at native SR, sr). Lazy: only when
    analysis or rendering actually needs the audio."""
    import numpy as np
    if fmt == "wav":
        import wave
        with wave.open(path, "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
            n = w.getnframes()
            if max_seconds:
                n = min(n, int(max_seconds * sr))
            raw = w.readframes(n)
        if sw == 2:
            data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        elif sw == 1:
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128.0
        elif sw == 3:
            a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16))
            v = np.where(v & 0x800000, v - (1 << 24), v)
            data = v.astype(np.float64) / float(1 << 23)
        elif sw == 4:
            data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
        else:
            raise ValueError(f"unsupported wav bit depth {sw * 8}")
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        return data, sr
    if fmt == "aiff":
        import aifc
        with aifc.open(path, "rb") as a:
            sr = a.getframerate()
            ch = a.getnchannels()
            sw = a.getsampwidth()
            n = a.getnframes()
            if max_seconds:
                n = min(n, int(max_seconds * sr))
            raw = a.readframes(n)
        dtypes = {1: np.int8, 2: ">i2", 3: None, 4: ">i4"}
        if sw == 3:
            u = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = (u[:, 0] << 16) | (u[:, 1] << 8) | u[:, 2]
            v = np.where(v & 0x800000, v - (1 << 24), v)
            data = v.astype(np.float64) / float(1 << 23)
        else:
            data = np.frombuffer(raw, dtype=dtypes[sw]).astype(np.float64)
            data /= float(1 << (8 * sw - 1))
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        return data, sr
    # flac / mp3: no native decoder in stdlib — skip analysis audio
    raise ValueError(f"no native decoder for format {fmt}")
