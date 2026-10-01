"""timbor.album.sequencing — album sequence construction (spec §3–§5, §36).

Builds the canonical album sequence from an album.json: explicit ordering
(never filesystem order), integer-sample positions, gap / crossfade /
continuous transition modes with optional bar quantization, and per-track
fades. The sequence describes the album; it never touches the source
masters. Defaults are the conservative §36 release configuration.
"""
from __future__ import annotations

import json
import os
import struct

import numpy as np

from .transitions import (Transition, CROSSFADE_TYPES, plan_boundary,
                          quantize_duration)
from .dna import crc32_json

SEQUENCE_FORMAT = "timbor-sequence"
SEQUENCE_VERSION = 1

MODES = ("gap", "crossfade", "continuous")


class SequenceConfig:
    """Sequencing + transition configuration with conservative defaults."""

    def __init__(self, enabled: bool = True, transition_mode: str = "gap",
                 gap_seconds: float = 2.0, fade_in_seconds: float = 0.0,
                 fade_out_seconds: float = 0.0,
                 crossfade_seconds: float = 4.0, curve: str = "equal_power",
                 quantize: str = "none", order: list | None = None,
                 preserve_track_boundaries: bool = True):
        if transition_mode not in MODES:
            raise ValueError(f"transition_mode must be one of {MODES}")
        if quantize not in ("none", "beat", "bar", "phrase"):
            raise ValueError("quantize must be none|beat|bar|phrase")
        if curve not in ("linear", "equal_power"):
            raise ValueError("curve must be linear|equal_power")
        if gap_seconds < 0 or crossfade_seconds < 0:
            raise ValueError("gap/crossfade seconds must be >= 0")
        self.enabled = bool(enabled)
        self.transition_mode = transition_mode
        self.gap_seconds = float(gap_seconds)
        self.fade_in_seconds = float(fade_in_seconds)
        self.fade_out_seconds = float(fade_out_seconds)
        self.crossfade_seconds = float(crossfade_seconds)
        self.curve = curve
        self.quantize = quantize
        self.order = list(order) if order else None
        self.preserve_track_boundaries = bool(preserve_track_boundaries)

    @classmethod
    def from_album(cls, album: dict) -> "SequenceConfig":
        seq = album.get("sequencing") or {}
        tr = album.get("transition") or {}
        mode = seq.get("transition_mode", seq.get("mode", "gap"))
        return cls(
            enabled=bool(seq.get("enabled", True)),
            transition_mode=mode,
            gap_seconds=float(seq.get("gap_seconds", 2.0)),
            fade_in_seconds=float(seq.get("fade_in_seconds",
                                          seq.get("fade_in", 0.0))),
            fade_out_seconds=float(seq.get("fade_out_seconds",
                                           seq.get("fade_out", 0.0))),
            crossfade_seconds=float(seq.get("crossfade_seconds",
                                            seq.get("overlap_seconds", 4.0))),
            curve=str(tr.get("curve", seq.get("curve", "equal_power"))),
            quantize=str(tr.get("quantize", seq.get("quantize", "none"))),
            order=seq.get("order"),
            preserve_track_boundaries=bool(
                seq.get("preserve_track_boundaries", True)))

    def to_json(self) -> dict:
        return dict(self.__dict__)

    def config_hash(self) -> str:
        return f"{crc32_json(self.to_json()):08x}"


def wav_info(path: str) -> tuple[int, int, int, int]:
    """Header-only WAV probe: (frames, sample_rate, channels, bits)."""
    with open(path, "rb") as f:
        hdr = f.read(12)
        if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            raise ValueError(f"not a RIFF/WAVE file: {path}")
        fmt = None
        data_size = None
        while True:
            ch_hdr = f.read(8)
            if len(ch_hdr) < 8:
                break
            cid, csz = ch_hdr[:4], struct.unpack("<I", ch_hdr[4:8])[0]
            if cid == b"fmt ":
                fmt = struct.unpack("<HHIIHH", f.read(csz))
            elif cid == b"data":
                data_size = csz
                break
            else:
                f.seek(csz + (csz & 1), 1)
    if fmt is None or data_size is None:
        raise ValueError(f"missing fmt/data chunk: {path}")
    _tag, ch, sr, _brate, block, bits = fmt
    return data_size // block, sr, ch, bits


def default_transition_type(mode: str, curve: str = "equal_power") -> str:
    if mode == "crossfade":
        return ("equal_power_crossfade" if curve == "equal_power"
                else "linear_crossfade")
    return {"gap": "gap", "continuous": "hard_cut"}.get(mode, "gap")


# ---------------------------------------------------------------------------
# sequence construction
# ---------------------------------------------------------------------------

def ordered_tracks(album: dict) -> list[dict]:
    """Explicit album order (spec §4): config['sequencing']['order'] wins,
    then album['order'], then the stored album.json track order. Never the
    filesystem listing."""
    tracks = album["tracks"]
    by_number = {t["number"]: t for t in tracks}
    order = None
    seq = album.get("sequencing") or {}
    if seq.get("order"):
        order = [str(x) for x in seq["order"]]
    elif album.get("order"):
        order = [str(x) for x in album["order"]]
    elif isinstance(album.get("config"), dict) and \
            (album["config"].get("sequencing") or {}).get("order"):
        order = [str(x) for x in album["config"]["sequencing"]["order"]]
    if order:
        missing = [x for x in order if x not in by_number]
        if missing:
            raise ValueError(f"sequence order references unknown tracks: {missing}")
        extra = [t["number"] for t in tracks if t["number"] not in order]
        if extra:
            raise ValueError(f"sequence order misses tracks: {extra}")
        return [by_number[x] for x in order]
    return list(tracks)


def build_sequence(album: dict, album_root: str,
                   cfg: SequenceConfig | None = None) -> dict:
    """Compute the deterministic album sequence: per-track integer positions,
    transitions, overlaps and total length. Reads WAV headers only.

    Continuous mode keeps a 1-sample-continuous hard cut (spec §24's
    'no discontinuity' applies to click diagnostics; musically the tracks
    simply butt together — crossfades are the tool for soft seams).
    """
    cfg = cfg or SequenceConfig.from_album(album)
    tracks = ordered_tracks(album)
    if not tracks:
        raise ValueError("album has no tracks")

    entries = []
    transitions = []
    boundaries = []
    cursor = 0  # integer sample position (spec §5/§16 — never fp accumulation)
    first = True
    prev_entry = None

    for i, t in enumerate(tracks):
        master = os.path.join(album_root,
                              t["directory"], "audio", "master.wav")
        frames, sr, ch, bits = wav_info(master)
        gap_smp = ov_smp = 0
        if first:
            start = 0
        else:
            tr = Transition(
                type=default_transition_type(cfg.transition_mode, cfg.curve),
                duration_seconds=(0.0 if cfg.transition_mode == "continuous"
                                  else (cfg.crossfade_seconds
                                        if cfg.transition_mode == "crossfade"
                                        else cfg.gap_seconds)),
                curve=cfg.curve,
                source_position=i - 1, destination_position=i,
                source_bpm=float(prev_entry["bpm"]),
                destination_bpm=float(t["bpm"]),
                quantize=cfg.quantize)
            layout = plan_boundary(prev_entry, {"bpm": t["bpm"],
                                                "source_samples": frames},
                                   tr, sr)
            tr.overlap_samples = layout["overlap_samples"]
            tr.gap_samples = layout["gap_samples"]
            tr.duration_seconds = layout["duration_seconds"]
            transitions.append(tr)
            ov_smp = int(layout["overlap_samples"])
            gap_smp = int(layout["gap_samples"]) \
                if cfg.transition_mode != "continuous" else 0
            # continuous: next track starts exactly at the previous end
            # (integer position, no float accumulation)
            start = prev_entry["end_sample"] + gap_smp - ov_smp
            if start < 0:
                raise ValueError("sequence produced a negative position")
            boundaries.append({
                "from": prev_entry["track_id"], "to": t["number"],
                "mode": layout["type"],
                "start_sample": int(start),
            })
        first = False

        entry = {
            "position": i + 1,
            "track_id": t["number"],
            "title": t.get("title"),
            "genre": t.get("genre"),
            "bpm": float(t["bpm"]),
            "project": t.get("project"),
            "master": os.path.join(t["directory"], "audio",
                                   "master.wav").replace(os.sep, "/"),
            "source_samples": frames,
            "duration_seconds": round(frames / sr, 3),
            "duration_beats": round(frames / sr * float(t["bpm"]) / 60.0, 2),
            "start_sample": int(start),
            "end_sample": int(start + frames),
            "start_seconds": round(start / sr, 4),
            "end_seconds": round((start + frames) / sr, 4),
            "gap_before_samples": gap_smp,
            "gap_before": round(gap_smp / sr, 4),
            "overlap_samples": ov_smp,
            "fade_in": int(round(cfg.fade_in_seconds * sr)),
            "fade_out": int(round(cfg.fade_out_seconds * sr)),
        }
        entries.append(entry)
        prev_entry = entry

    total = prev_entry["end_sample"]
    return {
        "format": SEQUENCE_FORMAT,
        "version": SEQUENCE_VERSION,
        "sample_rate": sr,
        "channels": 2,
        "mode": cfg.transition_mode,
        "quantize": cfg.quantize,
        "curve": cfg.curve,
        "config_hash": cfg.config_hash(),
        "total_samples": int(total),
        "duration_seconds": round(total / sr, 4),
        "entries": entries,
        "transitions": [tr.to_json() for tr in transitions],
        "boundaries": boundaries,
    }


# ---------------------------------------------------------------------------
# sequence validation (spec §8, §24 structure checks)
# ---------------------------------------------------------------------------

def validate_sequence(seq: dict, cfg: SequenceConfig | None = None) -> list[str]:
    """Structural checks; returns problems (empty = valid)."""
    problems: list[str] = []
    entries = seq["entries"]
    if not entries:
        return ["sequence has no tracks"]
    if [e["position"] for e in entries] != list(range(1, len(entries) + 1)):
        problems.append("positions not contiguous 1..N")
    if seq.get("total_samples") != entries[-1]["end_sample"]:
        problems.append("total_samples does not match last end_sample")
    for e in entries:
        if e["start_sample"] < 0:
            problems.append(f"track {e['track_id']}: negative start position")
        if e["end_sample"] < e["start_sample"]:
            problems.append(f"track {e['track_id']}: end before start")
        # truncation guard: the entry must cover the full source length
        if e["end_sample"] - e["start_sample"] != e["source_samples"]:
            problems.append(f"track {e['track_id']}: sequence window differs "
                            f"from source length (truncation?)")
    if cfg is not None and cfg.transition_mode not in ("crossfade",):
        ov = sum(e["overlap_samples"] for e in entries)
        if ov:
            problems.append("overlaps present without crossfade mode")
    # transitions consistent with entries
    for tr, a, b in zip(seq.get("transitions", []), entries, entries[1:]):
        if tr["destination_position"] != b["position"] - 1:
            problems.append(f"transition {a['track_id']}->{b['track_id']} "
                            f"has stale positions")
        if tr["overlap_samples"] > min(a["source_samples"],
                                        b["source_samples"]) // 2:
            problems.append(f"transition {a['track_id']}->{b['track_id']} "
                            f"overlap exceeds half of the shorter track")
    return problems


def save_sequence(path: str, seq: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seq, f, indent=1, sort_keys=True)
    return path


def load_sequence(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        seq = json.load(f)
    if seq.get("format") != SEQUENCE_FORMAT:
        raise ValueError(f"not a TIMBOR sequence: {seq.get('format')!r}")
    return seq
