"""
timbor.theory — notes, scales, chords, progressions, motifs.

Musical intelligence lives here; dsp.py only knows frequencies.
"""
from __future__ import annotations

import math
import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_INDEX = {n: i for i, n in enumerate(NOTE_NAMES)}

SCALES: dict[str, list[int]] = {
    "natural_minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "major": [0, 2, 4, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "aeolian": [0, 2, 3, 5, 7, 8, 10],
    "phrygian_dominant": [0, 1, 4, 5, 7, 8, 10],
    "whole_tone": [0, 2, 4, 6, 8, 10],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
}


def hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def note_midi(name: str) -> int:
    """'F#' -> 6, 'A' -> 9 (pitch class)."""
    return NOTE_INDEX[name]


def scale_notes(root: str, scale: str) -> list[int]:
    """Pitch classes of the scale as MIDI pc integers."""
    r = NOTE_INDEX[root]
    return [(r + d) % 12 for d in SCALES[scale]]


def scale_degree_to_midi(root: str, scale: str, degree: int, octave: int = 4) -> int:
    """degree is 0-based within the scale, can be negative or beyond one octave."""
    r = NOTE_INDEX[root]
    sc = SCALES[scale]
    n = len(sc)
    oct_shift = degree // n
    idx = degree % n
    return r + 12 * (octave + oct_shift) + sc[idx]


# ----------------------------------------------------------------------------
# chords
# ----------------------------------------------------------------------------

CHORD_SHAPES: dict[str, list[int]] = {
    "min": [0, 3, 7],
    "maj": [0, 4, 7],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "min7": [0, 3, 7, 10],
    "maj7": [0, 4, 7, 11],
    "dom7": [0, 4, 7, 10],
    "min9": [0, 3, 7, 14],
    "add9": [0, 4, 7, 14],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
}


def build_chord(root_pc: int, shape: str, octave: int = 3) -> list[int]:
    return [root_pc + 12 * octave + iv for iv in CHORD_SHAPES[shape]]


def chord_from_scale_degree(root: str, scale: str, degree: int, octave: int = 3,
                            seven: bool = False) -> list[int]:
    """Diatonic triad/7th built by stacking scale degrees."""
    midis = [
        scale_degree_to_midi(root, scale, degree, octave),
        scale_degree_to_midi(root, scale, degree + 2, octave),
        scale_degree_to_midi(root, scale, degree + 4, octave),
    ]
    if seven:
        midis.append(scale_degree_to_midi(root, scale, degree + 6, octave))
    return midis


# ----------------------------------------------------------------------------
# genre progression vocabulary (degree indexes into the scale; minor-centric)
# ----------------------------------------------------------------------------

PROGRESSIONS: dict[str, list[list[int]]] = {
    # each entry: list of (degree, shape_override) — shape None means diatonic
    "anthem": [[0, None], [5, None], [3, None], [4, None]],          # i-VI-iv-v
    "anthem_b": [[0, None], [3, None], [5, None], [4, None]],        # i-iv-VI-v
    "rave_loop": [[0, None], [0, None], [5, None], [4, None]],       # i-i-VI-v
    "epic": [[0, None], [6, "maj"], [5, None], [4, None]],           # i-VII-VI-v
    "epic_b": [[5, None], [3, None], [0, None], [4, None]],          # VI-iv-i-v
    "uplifting": [[5, None], [3, None], [0, None], [0, None]],       # VI-iv-i-i
    "uplifting_b": [[0, None], [5, None], [3, None], [6, "maj"]],
    "trance_roller": [[0, None], [0, None], [3, None], [4, None]],
    "trance_epic": [[0, None], [5, "maj"], [3, None], [4, "maj"]],
    "sus_engine": [[0, "sus2"], [0, "sus2"], [3, "sus2"], [4, "sus2"]],
    "dramatic": [[0, None], [1, "dim"], [0, None], [6, "maj"]],
    "dark_phryg": [[0, None], [1, None], [0, None], [1, None]],      # i-bII
    "jcore_bounce": [[0, None], [4, None], [5, None], [4, None]],
    "jcore_pop": [[3, "maj"], [4, None], [0, None], [5, None]],      # iv-v-i-VI
    "hardhouse": [[0, "min"], [0, "min"], [5, "maj"], [4, "maj"]],
    "big_beat": [[0, "min"], [0, "min"], [3, None], [3, None]],
    "amen_dark": [[0, None], [4, None], [0, None], [4, None]],
}


def pick_progression(rng: np.random.Generator, family: str) -> tuple[str, list[list[int]]]:
    keys = [k for k in PROGRESSIONS if k.startswith(family)]
    if not keys:
        keys = ["anthem", "epic", "trance_roller"]
    k = keys[int(rng.integers(0, len(keys)))]
    return k, PROGRESSIONS[k]


# ----------------------------------------------------------------------------
# motifs — the heart of melodic identity
# ----------------------------------------------------------------------------

def motif_generate(rng: np.random.Generator, scale: str, root: str,
                   n_steps: int, contour: str = "arch",
                   octave: int = 4, rest_p: float = 0.12) -> list[int | None]:
    """Generate a scale-degree motif (None = rest). Returns degrees (0-based)."""
    sc = SCALES[scale]
    n = len(sc)
    deg = int(rng.integers(0, 3))
    out: list[int | None] = []
    for i in range(n_steps):
        if rng.random() < rest_p and 0 < i < n_steps - 1:
            out.append(None)
            continue
        if contour == "arch":
            target = 2 + 3 * math.sin(math.pi * i / max(1, n_steps - 1))
        elif contour == "rise":
            target = 1 + 5 * (i / max(1, n_steps - 1))
        elif contour == "dawn":
            target = 5 - 4 * (i / max(1, n_steps - 1))
        else:  # wave
            target = 3 + 2.5 * math.sin(2.0 * math.pi * i / max(4, n_steps - 1) + rng.random())
        step_pool = [-2, -1, -1, 1, 1, 2, 3, -3]
        move = int(rng.choice(step_pool))
        pull = 0 if abs(deg - target) < 0.8 else (1 if deg < target else -1)
        deg = max(-2, min(2 * n + 1, deg + move + pull))
        out.append(deg)
    return out


def motif_variate(rng: np.random.Generator, motif: list[int | None],
                  style: str = "transpose", amount: int = 2) -> list[int | None]:
    """Variation operators that keep the motif recognizable (spec §13)."""
    m = list(motif)
    if style == "transpose":
        m = [None if x is None else x + amount for x in m]
    elif style == "octave":
        m = [None if x is None else x + 7 for x in m]
    elif style == "retrograde":
        m = m[::-1]
    elif style == "invert":
        anchor = [x for x in m if x is not None]
        c = anchor[len(anchor) // 2] if anchor else 0
        m = [None if x is None else (2 * c - x) for x in m]
    elif style == "augment":  # double some notes (rhythmic transformation)
        out: list[int | None] = []
        for x in m:
            out.append(x)
            if x is not None and rng.random() < 0.25:
                out.append(x)
        m = out
    elif style == "thin":
        m = [x if (i % 2 == 0 or x is None or rng.random() < 0.5) else None
             for i, x in enumerate(m)]
    elif style == "embellish":  # passing notes between leaps
        out = []
        for i, x in enumerate(m):
            out.append(x)
            if x is not None and rng.random() < 0.3:
                nxt = m[i + 1] if i + 1 < len(m) and m[i + 1] is not None else x
                out.append((x + nxt) // 2 + int(rng.integers(-1, 2)))
        m = out
    return m


def motif_identity(a: list[int | None], b: list[int | None]) -> float:
    """Similarity 0..1 between two motifs via relative contour + rhythm.

    A transposed motif must score ~1.0 — it is the same melody.
    """
    def intervals(m: list[int | None]) -> list[int]:
        xs = [x for x in m if x is not None]
        return [xs[i + 1] - xs[i] for i in range(len(xs) - 1)] if len(xs) > 1 else []

    ia, ib = intervals(a), intervals(b)
    L = min(len(ia), len(ib))
    contour = 0.0
    if L > 0:
        contour = sum(1 for i in range(L) if ia[i] == ib[i]) / L
    else:
        # single-note motifs: compare pitch directly
        pa = [x for x in a if x is not None]
        pb = [x for x in b if x is not None]
        if pa and pb:
            contour = 1.0 if pa[0] == pb[0] else 0.0
    ra = [0 if x is None else 1 for x in a]
    rb = [0 if x is None else 1 for x in b]
    Lr = min(len(ra), len(rb))
    rhythm = sum(1 for i in range(Lr) if ra[i] == rb[i]) / max(1, Lr) if Lr else 0.0
    return 0.7 * contour + 0.3 * rhythm


# ----------------------------------------------------------------------------
# bassline helpers
# ----------------------------------------------------------------------------

def offbeat_pattern(steps: int, rng: np.random.Generator,
                    style: str = "off8") -> list[bool]:
    """Where bass hits within a bar (kick-avoidance handled by caller)."""
    hits = [False] * steps
    if style == "off8":       # 8th offbeats (hard house / hard trance)
        for i in range(2, steps, 4):
            hits[i] = True
    elif style == "roll16":   # rolling 16ths (frenchcore/hardstyle)
        hits = [True] * steps
    elif style == "off8_roll":
        for i in range(2, steps, 4):
            hits[i] = True
            if rng.random() < 0.35:
                hits[i + 2] = True
    elif style == "acid":     # sparse syncopation
        for i in range(steps):
            hits[i] = rng.random() < 0.4
        hits[0] = True
    elif style == "jungle":   # syncopated 2-bar feel compressed to 1 bar
        for i in (0, 3, 6, 10, 12, 14):
            hits[i] = True
    return hits
