"""timbor.album.dna — album configuration, seed derivation and shared DNA.

The AlbumDNA is the constraint/identity layer from which per-track SongPlans
are derived: key/mode neighborhood, BPM neighborhood, harmonic vocabulary,
motif family, rhythmic fingerprint, sample palette, era/mood and the
recurring sonic signatures every track shares.

It is NOT a copy of any SongPlan — each track derives its own arrangement,
progression and variant motifs from this DNA.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# stable derivation helpers (spec §4 — never Python's salted hash())
# ---------------------------------------------------------------------------


def derive_seed(album_seed: int, label: str) -> int:
    """Stable child seed from (album_seed, label) via SHA-256.

    Properties: same album seed → same child seeds; changing track 3 never
    reshuffles tracks 1/2/4/5; adding a track leaves existing seeds untouched
    (derivation depends only on the track's own label).
    """
    h = hashlib.sha256(f"{int(album_seed)}:{label}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % (2**31)


def stable_tag(label: str, album_seed: int, n: int) -> str:
    """Short deterministic hex tag for naming/id purposes."""
    h = hashlib.sha256(f"{int(album_seed)}:{label}:{n}".encode("utf-8")).hexdigest()
    return h[:8]


def crc32_json(obj) -> int:
    """Stable content hash (crc32) of canonically-serialized JSON."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")
    return zlib.crc32(blob) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

MOODS = ("dark", "euphoric", "fun", "cinematic")
AUTH = ("authentic", "modern", "hybrid", "experimental")
PALETTE_MODES = ("strict", "balanced", "loose")
DIVERGENCE = ("subtle", "medium", "high")


@dataclass
class AlbumConfig:
    """Everything the album orchestrator needs (spec §3)."""

    name: str = "TIMBOR EP"
    seed: int = 424242
    tracks: int = 4
    genres: list[str] = field(default_factory=list)          # cycle if shorter than tracks
    prompts: list[str] = field(default_factory=list)          # optional per-track prompt
    titles: list[str] = field(default_factory=list)           # optional per-track title
    bpm_center: float | None = None
    bpm_range: float = 8.0
    key: str | None = None
    mode: str | None = None
    mood: str | None = None
    authenticity: str = "hybrid"
    era: str | None = None
    duration_range: list[float] = field(default_factory=lambda: [90.0, 180.0])
    motif_strategy: str = "family"       # family | anchored
    divergence: str = "medium"           # subtle | medium | high (arrangement)
    shared_palette: bool = True
    palette_mode: str = "balanced"       # strict | balanced | loose
    sample_dir: str | None = None
    sample_mode: str = "balanced"        # off/subtle/balanced/heavy pass-through
    sample_root: str | None = None
    # per-track overrides: index -> dict of AlbumConfig fields
    track_overrides: dict[int, dict] = field(default_factory=dict)

    def validate(self) -> list[str]:
        errs = []
        if self.tracks < 1:
            errs.append("tracks must be >= 1")
        if self.tracks > 12:
            errs.append("tracks > 12 not supported (keep EPs/albums reasonable)")
        if self.palette_mode not in PALETTE_MODES:
            errs.append(f"palette_mode must be one of {PALETTE_MODES}")
        if self.divergence not in DIVERGENCE:
            errs.append(f"divergence must be one of {DIVERGENCE}")
        if self.motif_strategy not in ("family", "anchored"):
            errs.append("motif_strategy must be 'family' or 'anchored'")
        if self.mood and self.mood not in MOODS:
            errs.append(f"mood must be one of {MOODS}")
        if self.authenticity not in AUTH:
            errs.append(f"authenticity must be one of {AUTH}")
        if len(self.duration_range) == 2:
            lo, hi = sorted(self.duration_range)
            if lo <= 0 or hi <= lo:
                errs.append("duration_range must be [min_seconds, max_seconds], min < max")
        if self.track_overrides:
            bad = [i for i in self.track_overrides if not 0 <= i < self.tracks]
            if bad:
                errs.append(f"track_overrides indices out of range: {bad}")
        return errs

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, data: dict) -> "AlbumConfig":
        fields = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in data.items() if k in fields})
        return cfg

    def track_config(self, i: int) -> "AlbumConfig":
        """Config copy with per-track overrides applied (deterministic)."""
        if i in self.track_overrides:
            data = self.to_json()
            data.update(self.track_overrides[i])
            return AlbumConfig.from_json(data)
        return self


# ---------------------------------------------------------------------------
# AlbumDNA
# ---------------------------------------------------------------------------

@dataclass
class AlbumDNA:
    """Shared musical identity of an EP/album (spec §5).

    Built once from the album config + album seed; every track derives its
    SongPlan constrained by (but not copied from) this DNA.
    """

    root_key: str
    mode: str
    bpm_center: float
    bpm_range: float
    harmonic_family: str                 # progression family name from theory.py
    motif_family: list[int | None]       # album-level base motif (scale degrees)
    motif_contour: str
    rhythm_fingerprint: list[int]        # 16-step 0/1 kick-grid affinity
    signature_instruments: dict          # bass/lead/kick/drums tendencies
    era: str
    mood: str
    authenticity: str
    palette_mode: str
    genre_anchor: str                    # primary genre of the album
    seed: int

    def to_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, data: dict) -> "AlbumDNA":
        return cls(**data)

    def dna_hash(self) -> str:
        return f"{crc32_json(self.to_json()):08x}"

    def bpm_window(self) -> tuple[float, float]:
        return self.bpm_center - self.bpm_range, self.bpm_center + self.bpm_range
