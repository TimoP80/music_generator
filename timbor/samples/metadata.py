"""timbor.samples.metadata — the SampleMetadata dataclass."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict


@dataclass
class SampleMetadata:
    path: str
    filename: str
    duration: float = 0.0
    sample_rate: int = 44100
    channels: int = 1
    size: int = 0
    mtime: float = 0.0
    fingerprint: str = ""          # size:mtime — cheap change detection

    # audio features (all optional/uncertain by nature)
    bpm: float | None = None
    bpm_confidence: float = 0.0
    key: str | None = None         # e.g. "F:min"
    root_note: str | None = None   # e.g. "F"
    key_confidence: float = 0.0
    loudness: float = 0.0          # approx LUFS-ish (dBFS of RMS)
    rms: float = 0.0
    peak: float = 0.0
    spectral_centroid: float = 0.0
    spectral_bandwidth: float = 0.0
    spectral_rolloff: float = 0.0
    sub_energy: float = 0.0        # 20-120 Hz share
    low_energy: float = 0.0        # 120-400
    mid_energy: float = 0.0        # 400-2000
    high_energy: float = 0.0       # 2k-8k
    transient_density: float = 0.0  # onsets per second
    zero_crossing_rate: float = 0.0
    tonalness: float = 0.0         # 0 = noise/percussive, 1 = tonal

    # classification
    category: str = "unknown"
    subcategory: str = ""
    genre_tags: list[str] = field(default_factory=list)
    energy: float = 0.0            # 0..1 perceptual energy
    is_loop: bool = False
    is_one_shot: bool = False
    classification_confidence: float = 0.0
    file_tags: list[str] = field(default_factory=list)  # hints from filename/path

    analyzed: bool = False
    error: str = ""

    def to_row(self) -> dict:
        d = asdict(self)
        d["genre_tags"] = json.dumps(self.genre_tags)
        d["file_tags"] = json.dumps(self.file_tags)
        return d

    @classmethod
    def from_row(cls, row: dict) -> "SampleMetadata":
        m = cls(**{k: row[k] for k in row.keys() if k in
                   {f for f in cls.__dataclass_fields__}})
        m.genre_tags = json.loads(row.get("genre_tags") or "[]")
        m.file_tags = json.loads(row.get("file_tags") or "[]")
        return m

    @property
    def key_root(self) -> int | None:
        """Root as MIDI pitch class, or None."""
        if not self.root_note:
            return None
        from timbor.theory import NOTE_INDEX
        return NOTE_INDEX.get(self.root_note.capitalize())

    @property
    def is_minor(self) -> bool:
        return (self.key or "").endswith("min")
