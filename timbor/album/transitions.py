"""timbor.album.transitions — transition abstraction + boundary diagnostics.

Transition types (spec §6): hard_cut, gap, linear_crossfade,
equal_power_crossfade. Default: gap. Beat-aware quantization (spec §7):
durations align to the SOURCE track's beat/bar/phrase grid; both source and
destination BPM are stored — differing BPMs are reported, never silently
treated as identical.

Safety invariants (spec §8): overlap never exceeds what either track can
give without truncation, positions never go negative, and nothing here ever
writes to a source master.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

TRANSITION_TYPES = ("hard_cut", "gap", "linear_crossfade",
                    "equal_power_crossfade")
CROSSFADE_TYPES = ("linear_crossfade", "equal_power_crossfade")
QUANTIZES = ("none", "beat", "bar", "phrase")
DEFAULT_TYPE = "gap"

# explicit click-detection thresholds on max sample-to-sample delta.
# In dense electronic music the seam sits INSIDE loud material whose normal
# inter-sample slopes reach ~0.3+, so a click is detected as an OUTLIER:
# the seam's max|diff| must exceed the surrounding context's max|diff| by a
# clear ratio (or be large in near-silence) to count.
CLICK_WARN = 0.10
CLICK_ERROR = 0.30
CLICK_SILENCE_CONTEXT = 0.02   # below this, context is "quiet" → absolute
RATIO_WARN = 1.8
RATIO_ERROR = 2.5
SEAM_WINDOW_S = 0.002          # ±2 ms around the boundary itself
CONTEXT_WINDOW_S = 0.5


@dataclass
class Transition:
    """One boundary between two adjacent tracks (spec §6)."""

    type: str = DEFAULT_TYPE
    duration_seconds: float = 0.0
    curve: str = "linear"           # linear | equal_power
    source_position: int = 0        # sequence index of outgoing track
    destination_position: int = 0   # sequence index of incoming track
    source_bpm: float = 0.0
    destination_bpm: float = 0.0
    quantize: str = "none"
    overlap_samples: int = 0
    gap_samples: int = 0
    bpm_mismatch: bool = False
    parameters: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d: dict) -> "Transition":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


def beats_per(quantize: str) -> float:
    """How many beats one quantization unit covers."""
    return {"none": 0.0, "beat": 1.0, "bar": 4.0, "phrase": 16.0}[quantize]


def quantize_duration(seconds: float, bpm: float, quantize: str,
                      sr: int) -> int:
    """Quantize a duration to the musical grid of `bpm` (integer samples).

    'none' rounds to the nearest sample; beat/bar/phrase snap UP to the next
    grid boundary so a transition never falls short of the requested length.
    """
    if seconds < 0:
        raise ValueError("transition duration must be >= 0")
    unit_beats = beats_per(quantize)
    if unit_beats == 0.0:
        return int(round(seconds * sr))
    unit_seconds = unit_beats * 60.0 / float(bpm)
    import math
    units = max(1, math.ceil(seconds / unit_seconds - 1e-9))
    return int(round(units * unit_seconds * sr))


def crossfade_curves(n: int, curve: str) -> tuple[np.ndarray, np.ndarray]:
    """Fade-out (for the outgoing track tail) and fade-in (incoming head)
    gain envelopes of n samples. For equal_power the gains are amplitude-
    domain sin/cos so that power (g_out² + g_in²) stays constant at 1.0."""
    if n <= 0:
        return np.zeros(0), np.zeros(0)
    t = np.linspace(0.0, 1.0, n, endpoint=False)
    if curve == "equal_power":
        fade_out = np.cos(t * np.pi / 2.0)
        fade_in = np.sin(t * np.pi / 2.0)
    elif curve == "linear":
        fade_out = 1.0 - t
        fade_in = t
    else:
        raise ValueError(f"unknown crossfade curve: {curve}")
    return fade_out, fade_in


def fade_envelopes(n: int, fade_in_samples: int,
                   fade_out_samples: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-track head/tail gain envelopes (linear, deterministic)."""
    g = np.ones(max(0, n))
    fi = max(0, min(int(fade_in_samples), n))
    fo = max(0, min(int(fade_out_samples), n))
    if fi:
        g[:fi] *= np.linspace(0.0, 1.0, fi, endpoint=False)
    if fo:
        g[n - fo:] *= np.linspace(1.0, 0.0, fo, endpoint=False)
    return g, g


# ---------------------------------------------------------------------------
# boundary planning (integer samples; spec §5, §7, §8)
# ---------------------------------------------------------------------------

def plan_boundary(out_track: dict, in_track: dict, transition: Transition,
                  sr: int) -> dict:
    """Compute integer-sample boundary layout for one track pair.

    out_track / in_track: sequence entries with bpm, duration_seconds,
    source_samples. Returns a deterministic layout dict; raises ValueError
    on impossible requests (spec §8 safety).
    """
    bpm_out = float(out_track["bpm"])
    bpm_in = float(in_track["bpm"])
    n_out = int(out_track["source_samples"])
    ttype = transition.type
    overlap = gap = 0

    if ttype in CROSSFADE_TYPES:
        want = int(round(transition.duration_seconds * sr))
        want = quantize_duration(transition.duration_seconds, bpm_out,
                                 transition.quantize, sr) \
            if transition.quantize != "none" else want
        # cannot eat the whole outgoing track nor require the incoming track
        # to be cut: overlap bounded by 50% of the shorter side. An
        # impossible request is an explicit error (spec §8), never a
        # silent clamp that would truncate musical content.
        max_overlap = min(n_out // 2, int(in_track["source_samples"]) // 2)
        if want > max_overlap:
            raise ValueError(
                f"crossfade of {transition.duration_seconds}s impossible "
                f"between tracks of {n_out/sr:.1f}s and "
                f"{in_track['source_samples']/sr:.1f}s — would take more "
                f"than half of the shorter track "
                f"(max {max_overlap / sr:.2f}s)")
        overlap = want
        if overlap <= 0:
            raise ValueError(
                f"crossfade of {transition.duration_seconds}s impossible "
                f"between tracks of {n_out/sr:.1f}s and "
                f"{in_track['source_samples']/sr:.1f}s")
    elif ttype == "gap":
        gap = int(round(transition.duration_seconds * sr))
    elif ttype == "hard_cut":
        pass
    else:
        raise ValueError(f"unknown transition type: {ttype}")

    return {
        "type": ttype,
        "overlap_samples": overlap,
        "gap_samples": gap,
        "source_bpm": bpm_out,
        "destination_bpm": bpm_in,
        "bpm_mismatch": abs(bpm_out - bpm_in) > 1e-9,
        "quantize": transition.quantize,
        # the transition TYPE names the curve family (deterministic); a
        # generic config curve is resolved upstream via default_transition_type
        "curve": ("equal_power" if ttype == "equal_power_crossfade"
                  else "linear" if ttype == "linear_crossfade" else "none"),
        "duration_seconds": round((overlap + gap) / sr, 6),
    }


def apply_boundary(album_l: np.ndarray, album_r: np.ndarray,
                   out_l: np.ndarray, out_r: np.ndarray,
                   in_l: np.ndarray, in_r: np.ndarray,
                   write_pos: int, layout: dict) -> int:
    """Write the incoming track into the album buffers at write_pos,
    blending over `overlap_samples` with the outgoing track's tail.

    Returns the exclusive end sample. All positions are non-negative
    integers; the incoming track is never truncated (caller guarantees
    buffer capacity via build_sequence). Non-destructive: operates on
    in-memory copies only.
    """
    ov = int(layout["overlap_samples"])
    n_in = len(in_l)
    end = write_pos + n_in
    if ov <= 0 or write_pos + ov <= 0:
        album_l[write_pos:end] += in_l
        album_r[write_pos:end] += in_r
        return end
    ov = min(ov, n_in)
    fade_out, fade_in = crossfade_curves(ov, layout.get("curve", "linear"))
    # blend zone: the album already holds the outgoing track's tail there,
    # so this is a replace-and-mix (not +=), avoiding tail double-count
    album_l[write_pos:write_pos + ov] = \
        album_l[write_pos:write_pos + ov] * fade_out + in_l[:ov] * fade_in
    album_r[write_pos:write_pos + ov] = \
        album_r[write_pos:write_pos + ov] * fade_out + in_r[:ov] * fade_in
    rest = n_in - ov
    if rest > 0:
        album_l[write_pos + ov:end] += in_l[ov:]
        album_r[write_pos + ov:end] += in_r[ov:]
    return end


# ---------------------------------------------------------------------------
# boundary diagnostics + click detection (spec §25, §26)
# ---------------------------------------------------------------------------

def measure_boundary(audio: np.ndarray, boundary_sample: int, sr: int,
                     window_seconds: float = CONTEXT_WINDOW_S) -> dict:
    """Measurable diagnostics around one boundary (mono mix).

    seam_discontinuity  — max sample-to-sample delta within ±2 ms of the
                          boundary itself (where a click would live);
    context_discontinuity — max delta in the surrounding ±500 ms (how much
                          slope the music produces here anyway).
    No quality scores — measured numbers plus the explicit click_status().
    """
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    n = len(mono)
    w = int(window_seconds * sr)
    lo, hi = max(0, boundary_sample - w), min(n, boundary_sample + w)
    seg = mono[lo:hi]
    peak = float(np.max(np.abs(seg))) if len(seg) else 0.0

    def max_diff(a: int, b: int) -> float:
        a, b = max(1, a), min(n, b)
        return float(np.max(np.abs(np.diff(mono[a - 1:b])))) if b - a > 1 \
            else 0.0

    sw = int(SEAM_WINDOW_S * sr)
    seam = max_diff(boundary_sample - sw, boundary_sample + sw)
    # context EXCLUDES the seam itself — otherwise a huge click defines its
    # own baseline (ratio 1.0) and no click is ever an outlier.
    context = max(max_diff(lo, boundary_sample - sw),
                  max_diff(boundary_sample + sw, hi))
    rms_pre = float(np.sqrt(np.mean(mono[max(0, boundary_sample - w):
                                         boundary_sample] ** 2))) \
        if boundary_sample > 0 else 0.0
    rms_post = float(np.sqrt(np.mean(mono[boundary_sample:
                                          min(n, boundary_sample + w)] ** 2))) \
        if boundary_sample < n else 0.0
    return {
        "boundary_sample": int(boundary_sample),
        "boundary_seconds": round(boundary_sample / sr, 4),
        "peak_around": round(db_local(peak), 2),
        "discontinuity": round(seam, 6),
        "context_discontinuity": round(context, 6),
        "rms_pre_dbfs": round(db_local(rms_pre), 2),
        "rms_post_dbfs": round(db_local(rms_post), 2),
    }


def db_local(x: float) -> float:
    return 20.0 * float(np.log10(max(x, 1e-12)))


def click_status(seam: float, context: float | None = None) -> str:
    """PASS / WARNING / ERROR from explicit thresholds (no repair).

    Near-silent context (gap boundaries): absolute thresholds apply.
    Loud context (crossfade/continuous seams inside music): the seam delta
    must be an outlier versus the context delta (RATIO_WARN/RATIO_ERROR).
    """
    ctx = CLICK_SILENCE_CONTEXT if context is None else context
    if ctx < CLICK_SILENCE_CONTEXT:
        if seam >= CLICK_ERROR:
            return "ERROR"
        if seam >= CLICK_WARN:
            return "WARNING"
        return "PASS"
    ratio = seam / max(ctx, 1e-9)
    if seam >= CLICK_ERROR and ratio >= RATIO_ERROR:
        return "ERROR"
    if seam >= CLICK_WARN and ratio >= RATIO_WARN:
        return "WARNING"
    return "PASS"


def boundary_diagnostics(album_audio: np.ndarray, entries: list[dict],
                         sr: int) -> list[dict]:
    """One diagnostic record per track boundary (spec §25 format)."""
    out = []
    mono = album_audio.mean(axis=1) if album_audio.ndim == 2 else album_audio
    for e in entries:
        b = e["start_sample"]
        if b <= 0 or b >= len(mono):
            continue
        d = measure_boundary(album_audio, b, sr)
        d.update({"from": e.get("from"), "to": e.get("to"),
                  "mode": e.get("mode"),
                  "status": click_status(d["discontinuity"],
                                         d["context_discontinuity"])})
        out.append(d)
    return out
