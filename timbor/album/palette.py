"""timbor.album.palette — shared album sample palette (spec §9–§11).

The palette is built ONCE from the existing sample intelligence (index +
selector scoring), never per track. Selection mirrors select_for_song:
candidates per role are scored with the existing explainable score_sample
against album-level pseudo-songs (one per distinct album genre — a sample
qualifies if it fits the EP somewhere), then a seeded pick from the top
slice. No engine code is modified.

The role set depends on the palette mode:

  strict   — 7 roles: every major sample slot comes from the palette
  balanced — 4 signature roles (break, impact, vocal, melodic loop)
  loose    — 1 signature role (the album break)

Tracks enforce the palette by substitution after their own selection (see
generator.enforce_palette): a role that exists in the palette always uses
the palette entry, so the album's sonic anchors propagate to every track
while per-track selections still fill everything else.
"""
from __future__ import annotations

import numpy as np

from .dna import crc32_json, derive_seed
from ..samples.selector import score_sample
from ..theory import NOTE_INDEX

SIGNATURE_ROLES = ("main_break", "impact", "vocal_hit", "melodic_loop")
EXTRA_STRICT_ROLES = ("perc_loop", "riser", "texture")

_ROLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "main_break": ("breakbeat", "drum_loop"),
    "impact": ("impact", "fx"),
    "vocal_hit": ("vocal",),
    "vocal_texture": ("vocal",),
    "melodic_loop": ("lead", "chord", "piano", "acid"),
    "perc_loop": ("perc", "drum_loop"),
    "riser": ("riser", "fx"),
    "texture": ("texture", "ambience", "pad"),
}

PALETTE_ROLES: dict[str, tuple[str, ...]] = {
    "strict": SIGNATURE_ROLES + EXTRA_STRICT_ROLES,
    "balanced": SIGNATURE_ROLES,
    "loose": ("main_break",),
}


def palette_roles_for(mode: str) -> tuple[str, ...]:
    return PALETTE_ROLES.get(mode, PALETTE_ROLES["balanced"])


def same_path(a: str | None, b: str | None) -> bool:
    """Compare a project-relative sample path with a library-absolute one.

    Falls back to basename equality when absolute resolution is ambiguous
    (e.g. a manifest built later from a different working directory).
    """
    if not a or not b:
        return False
    import os as _os
    a_norm = a.replace("\\", "/")
    b_norm = b.replace("\\", "/")
    if _os.path.normcase(_os.path.abspath(a_norm)) == \
            _os.path.normcase(_os.path.abspath(b_norm)):
        return True
    return _os.path.basename(a_norm) == _os.path.basename(b_norm)


# ------------------------------------------------------------- pseudo-song

class _PseudoPlan:
    """Minimal plan surface required by selector.score_sample."""

    def __init__(self, genre: str, mood: str):
        self.genre = genre
        self.mood = mood
        self.secondary: list[str] = []
        self.prompt = ""


class PseudoSong:
    """Minimal song surface required by selector.score_sample.

    Stands in for a real SongPlan when scoring the album palette: carries
    the DNA's key/scale/BPM neighborhood and the genre being scored against.
    """

    def __init__(self, cfg, genre: str):
        from .planning import default_bpm_center
        self.plan = _PseudoPlan(genre, cfg.mood or "dark")
        self.key = cfg.key if cfg.key in NOTE_INDEX else "A"
        self.scale = cfg.mode if cfg.mode in ("major",) else "natural_minor"
        self.bpm = float(cfg.bpm_center) if cfg.bpm_center else \
            default_bpm_center(cfg, genre)


def pseudo_songs(cfg, genres: list[str]) -> dict[str, PseudoSong]:
    """One pseudo-song per distinct album genre (deterministic order)."""
    return {g: PseudoSong(cfg, g) for g in sorted(set(genres or ["rave"]))}


# ---------------------------------------------------------------- palette

def build_palette(cfg, sample_index, genres: list[str]) -> dict:
    """Build the shared album palette. Deterministic in (cfg.seed, library).

    Returns {mode, genres, roles: {role: entry}} where each entry carries
    full provenance: path, filename, category, bpm, key, duration,
    selection_score, scored_for_genre — tracks/placements are tallied later
    (manifest.py) from the generated projects.
    """
    roles = palette_roles_for(cfg.palette_mode)
    rng = np.random.default_rng(derive_seed(cfg.seed, "album-palette"))
    songs = pseudo_songs(cfg, genres)
    distinct = sorted(songs)

    palette: dict = {"mode": cfg.palette_mode, "genres": distinct, "roles": {}}
    for role in roles:
        cats = _ROLE_CATEGORIES[role]
        cands: list[tuple[float, object, str]] = []
        seen: set[str] = set()
        for cat in cats:
            for m in sample_index.by_category(cat, analyzed_only=True, limit=80):
                if m.path in seen:
                    continue
                seen.add(m.path)
                best_sc, best_g = -1.0, distinct[0]
                for g in distinct:
                    sc, _why = score_sample(m, songs[g], role, 0.7, rng)
                    if sc > best_sc:
                        best_sc, best_g = sc, g
                cands.append((best_sc, m, best_g))
        if not cands:
            continue
        cands.sort(key=lambda t: (-t[0], t[1].path))
        top = cands[: max(3, len(cands) // 8)]
        sc, m, g = top[int(rng.integers(0, len(top)))]
        palette["roles"][role] = {
            "id": f"smp{crc32_json({'p': m.path}) & 0xFFFF:04x}",
            "path": m.path,
            "filename": m.filename,
            "category": m.category,
            "bpm": m.bpm,
            "key": m.key,
            "duration": round(float(m.duration), 3),
            "selection_score": round(float(sc), 2),
            "scored_for_genre": g,
            "roles": [role],
        }

    # merge: the same file winning two roles becomes one entry, two roles
    by_path: dict[str, dict] = {}
    merged: dict[str, dict] = {}
    for role, entry in palette["roles"].items():
        p = entry["path"]
        if p in by_path:
            kept = by_path[p]
            kept["roles"] = sorted(set(kept["roles"]) | {role})
            if entry["selection_score"] > kept["selection_score"]:
                kept["selection_score"] = entry["selection_score"]
                kept["scored_for_genre"] = entry["scored_for_genre"]
        else:
            by_path[p] = entry
            merged[role] = entry
    palette["roles"] = merged
    return palette


def role_gate(mode: str, role: str, palette_roles: dict) -> bool:
    """True when a track's `role` assignment must come from the palette."""
    return role in palette_roles


def palette_summary(palette: dict) -> str:
    """Human-readable palette overview for the CLI."""
    lines = [f"album palette (mode={palette['mode']}, "
             f"genres={','.join(palette['genres'])})"]
    for role, e in palette["roles"].items():
        bpm = f"{e['bpm']:.0f}bpm" if e["bpm"] else "?bpm"
        lines.append(f"  {role:13s} {e['filename']}  ({e['category']}, "
                     f"{bpm}, score {e['selection_score']})")
    return "\n".join(lines)
