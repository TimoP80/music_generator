"""timbor.album.planning — per-track musical parameter planning (spec §12).

BPM neighborhood with genre respect: every track gets a deterministic BPM
near the DNA's center, snapped to the genre pack's authentic range — a track
never receives a musically unreasonable tempo merely to satisfy the
neighborhood. Durations map to bar counts through the same genre tempo.
"""
from __future__ import annotations

from .dna import AlbumConfig, derive_seed

# theory scale-name aliases accepted in Plan key strings
_SCALE_ALIAS = {
    "minor": "minor", "major": "major", "phrygian": "phrygian",
    "dorian": "dorian", "harmonic_minor": "harmonic_minor",
    "melodic_minor": "melodic_minor", "lydian": "lydian",
    "natural_minor": "minor",
}


def genre_pack(genre: str) -> dict:
    from ..engine import GENRES
    return GENRES.get(genre, GENRES["rave"])


def default_bpm_center(cfg: AlbumConfig, genre: str) -> float:
    """Explicit center, else the genre pack's range midpoint."""
    if cfg.bpm_center:
        return float(cfg.bpm_center)
    lo, hi = genre_pack(genre)["bpm"]
    return (lo + hi) / 2.0


def assign_bpm(cfg: AlbumConfig, dna, genre: str, index: int) -> float:
    """Deterministic BPM for track `index`: center ± jitter, genre-snapped.

    Same (seed, index) → same BPM; tracks are independent of each other.
    """
    lo_g, hi_g = genre_pack(genre)["bpm"]
    center = float(dna.bpm_center)
    lo, hi = dna.bpm_window()
    rng = np_random(derive_seed(cfg.seed, f"bpm-{index}"))
    span = max(1.0, hi - lo)
    raw = center + float(rng.uniform(-span / 2.0, span / 2.0))
    # respect the genre above the neighborhood: snap into the pack window
    raw = min(max(raw, float(lo_g)), float(hi_g))
    return float(int(round(raw)))  # integer BPM keeps grids clean


def duration_target(cfg: AlbumConfig, index: int) -> float:
    """Deterministic target duration in seconds for track `index`.

    Divergence controls spread: subtle → album midpoint, medium → ±15%
    jitter, high → uniform across the configured range.
    """
    lo, hi = sorted(cfg.duration_range[:2]) if len(cfg.duration_range) >= 2 \
        else (90.0, 180.0)
    mid = (lo + hi) / 2.0
    rng = np_random(derive_seed(cfg.seed, f"dur-{index}"))
    if cfg.divergence == "subtle":
        return mid
    if cfg.divergence == "medium":
        return mid * (1.0 + float(rng.uniform(-0.15, 0.15)))
    return float(rng.uniform(lo, hi))


def bars_for_duration(genre: str, bpm: float, seconds: float) -> int:
    """Convert a duration target into whole 4-bar phrases at the track tempo."""
    bar = 4.0 * 60.0 / bpm
    bars = int(round(seconds / bar))
    bars = max(16, min(128, bars))
    return int(round(bars / 4)) * 4


def track_identity(cfg: AlbumConfig, index: int) -> dict:
    """Stable identity for track `index`: number, title, prompt, seed label."""
    number = f"{index + 1:02d}"
    title = cfg.titles[index] if index < len(cfg.titles) and cfg.titles[index] \
        else f"Track {number}"
    prompt = cfg.prompts[index] if index < len(cfg.prompts) and \
        cfg.prompts[index] else \
        f"{cfg.genres[index] if index < len(cfg.genres) else 'rave'} " \
        f"{cfg.mood or ''}".strip()
    return {"number": number, "title": title, "prompt": prompt,
            "slug": _slug(title)}


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(p for p in keep.split("-") if p) or "track"


def make_plan(cfg: AlbumConfig, dna, index: int, sample_dir: str | None,
              sample_mode: str | None = None):
    """Build the engine.Plan for track `index` from the shared DNA.

    Constrains: seed (derived), genre, bpm (neighborhood), key/scale (DNA),
    progression family (DNA harmonic vocabulary), mood/era/authenticity
    (DNA), bar budget (duration target + divergence). The motif itself is
    injected later from the lineage (motif_override), after SongPlan build.
    """
    from ..engine import Plan

    genre = cfg.genres[index] if index < len(cfg.genres) else \
        (cfg.genres[0] if cfg.genres else "rave")
    ident = track_identity(cfg, index)
    bpm = assign_bpm(cfg, dna, genre, index)
    seed = derive_seed(cfg.seed, f"track-{ident['number']}")
    scale = _SCALE_ALIAS.get(dna.mode, dna.mode or "minor")
    bars = bars_for_duration(genre, bpm, duration_target(cfg, index))
    plan = Plan(
        prompt=ident["prompt"],
        genre=genre,
        bpm=bpm,
        key=f"{dna.root_key}:{scale}",
        mood=dna.mood,
        authenticity=dna.authenticity,
        seed=seed,
        bars_limit=bars,
        sample_mode=sample_mode or cfg.sample_mode,
        prog_family=[dna.harmonic_family],
    )
    plan.sample_dir = sample_dir
    return plan


def np_random(seed: int):
    import numpy as np
    return np.random.default_rng(seed)
