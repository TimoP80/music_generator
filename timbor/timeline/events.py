"""timbor.timeline.events — canonical event representation.

Musical time is canonical: positions and durations are in BEATS (float).
Seconds are derived from BPM at conversion time, never stored as the
primary representation. This prevents drift when tempo changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# event type vocabularies
NOTE_TYPES = {"bass", "melody", "chord", "pad", "lead"}
DRUM_TYPES = {"kick", "snare", "clap", "hat", "perc", "tom", "crash", "ride",
              "break_chop"}
SAMPLE_TYPES = {"breakbeat", "vocal", "texture", "impact", "riser", "stab",
                "sample_loop"}
ARRANGEMENT_TYPES = {"section_start", "section_end", "fill", "transition",
                     "build", "drop", "breakdown"}

VALID_BUSES = {"drums", "bass", "mel", "fx", "samples"}
VALID_ROLES = {"kick", "snare", "clap", "hat", "perc", "tom", "crash", "ride",
               "bass", "melody", "chord", "pad", "lead", "fx", "riser", "impact",
               "main_break", "alt_break", "perc_loop", "vocal_hit",
               "vocal_texture", "melodic_loop", "stab_hit", "bass_sample",
               "texture", "pad", "section"}


@dataclass
class TimelineEvent:
    """One musical event: what happens, when, and where its audio comes from."""
    start_beat: float
    duration_beats: float
    bus: str
    type: str
    role: str = ""
    section: str = ""
    instrument: str = ""          # procedural voice name
    pitch: float | None = None    # MIDI note for note events
    velocity: float = 1.0
    # sample events
    sample_id: str | None = None
    source_path: str | None = None
    pitch_semitones: float = 0.0
    stretch_ratio: float = 1.0
    reverse: bool = False
    chop_ops: dict = field(default_factory=dict)
    gain_db: float = 0.0
    # free-form extras (era treatment, variant tag, qc hints)
    metadata: dict = field(default_factory=dict)
    id: int = 0

    def to_json(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "bus": self.bus,
            "role": self.role,
            "section": self.section,
            "start_beat": round(self.start_beat, 4),
            "duration_beats": round(self.duration_beats, 4),
            "gain_db": round(self.gain_db, 2),
        }
        if self.instrument:
            d["instrument"] = self.instrument
        if self.type in NOTE_TYPES:
            d.update(pitch=self.pitch,
                     velocity=round(self.velocity, 3))
        else:
            d["velocity"] = round(self.velocity, 3)
        if self.type in SAMPLE_TYPES or self.sample_id:
            d.update(sample_id=self.sample_id,
                     source_path=self.source_path,
                     pitch_semitones=round(self.pitch_semitones, 2),
                     stretch_ratio=round(self.stretch_ratio, 4),
                     reverse=self.reverse,
                     chop_ops={k: round(v, 2) for k, v in self.chop_ops.items()})
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_json(cls, d: dict) -> "TimelineEvent":
        return cls(
            id=d.get("id", 0),
            type=d["type"],
            bus=d["bus"],
            role=d.get("role", ""),
            section=d.get("section", ""),
            start_beat=float(d["start_beat"]),
            duration_beats=float(d["duration_beats"]),
            instrument=d.get("instrument", ""),
            pitch=d.get("pitch"),
            velocity=float(d.get("velocity", 1.0)),
            sample_id=d.get("sample_id"),
            source_path=d.get("source_path"),
            pitch_semitones=float(d.get("pitch_semitones", 0.0)),
            stretch_ratio=float(d.get("stretch_ratio", 1.0)),
            reverse=bool(d.get("reverse", False)),
            chop_ops=dict(d.get("chop_ops", {})),
            gain_db=float(d.get("gain_db", 0.0)),
            metadata=dict(d.get("metadata", {})),
        )
