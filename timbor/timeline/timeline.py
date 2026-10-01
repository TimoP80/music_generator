"""timbor.timeline.timeline — the Timeline: ordered events + sections + time math.

Beat position is authoritative; seconds are derived from BPM. The timeline
records what the generators decided so any render can be inspected or
reproduced.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .events import TimelineEvent, VALID_BUSES


def beats_to_seconds(beats: float, bpm: float) -> float:
    return beats * 60.0 / bpm


def seconds_to_beats(seconds: float, bpm: float) -> float:
    return seconds * bpm / 60.0


def bars_to_beats(bars: float) -> float:
    return bars * 4.0


@dataclass
class SectionSpan:
    name: str
    index: int
    start_beat: float
    end_beat: float
    energy: float
    bars: int

    def to_json(self) -> dict:
        return {"name": self.name, "index": self.index,
                "start_beat": round(self.start_beat, 3),
                "end_beat": round(self.end_beat, 3),
                "energy": round(self.energy, 3), "bars": self.bars}

    @classmethod
    def from_json(cls, d: dict) -> "SectionSpan":
        return cls(name=d["name"], index=int(d["index"]),
                   start_beat=float(d["start_beat"]),
                   end_beat=float(d["end_beat"]),
                   energy=float(d["energy"]), bars=int(d["bars"]))


@dataclass
class SampleUse:
    """Snapshot of one sample used by the project (reference, not audio)."""
    id: str
    path: str                      # project-relative when possible
    filename: str
    file_size: int
    mtime: float
    hash: str | None = None        # optional content hash
    bpm: float | None = None
    bpm_confidence: float = 0.0
    key: str | None = None
    key_confidence: float = 0.0
    category: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "path": self.path, "filename": self.filename,
                "file_size": self.file_size, "mtime": self.mtime,
                "hash": self.hash, "bpm": self.bpm,
                "bpm_confidence": round(self.bpm_confidence, 3),
                "key": self.key,
                "key_confidence": round(self.key_confidence, 3),
                "category": self.category}

    @classmethod
    def from_json(cls, d: dict) -> "SampleUse":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


class Timeline:
    def __init__(self, bpm: float):
        self.bpm = float(bpm)
        self.events: list[TimelineEvent] = []
        self.sections: list[SectionSpan] = []
        self.samples: dict[str, SampleUse] = {}
        self._next_id = 1

    # -- building -------------------------------------------------------------
    def add(self, ev: TimelineEvent) -> TimelineEvent:
        ev.id = self._next_id
        self._next_id += 1
        self.events.append(ev)
        return ev

    def add_section(self, span: SectionSpan) -> None:
        self.sections.append(span)

    def register_sample(self, use: SampleUse) -> str:
        self.samples[use.id] = use
        return use.id

    # -- queries --------------------------------------------------------------
    def sorted_events(self) -> list[TimelineEvent]:
        return sorted(self.events, key=lambda e: (e.start_beat, e.bus, e.id))

    def by_bus(self, bus: str) -> list[TimelineEvent]:
        return [e for e in self.events if e.bus == bus]

    def bus_counts(self) -> dict[str, int]:
        out = {b: 0 for b in VALID_BUSES}
        for e in self.events:
            out[e.bus] = out.get(e.bus, 0) + 1
        return out

    def total_beats(self) -> float:
        if self.sections:
            return max(s.end_beat for s in self.sections)
        return max((e.start_beat + e.duration_beat for e in self.events), default=0.0)

    def duration_seconds(self) -> float:
        return beats_to_seconds(self.total_beats(), self.bpm)

    def bar_count(self) -> int:
        return int(round(self.total_beats() / 4.0))

    def to_json(self) -> dict:
        return {
            "bpm": self.bpm,
            "sections": [s.to_json() for s in self.sections],
            "events": [e.to_json() for e in self.sorted_events()],
            "samples": [u.to_json() for u in self.samples.values()],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Timeline":
        t = cls(d["bpm"])
        t.sections = [SectionSpan.from_json(s) for s in d.get("sections", [])]
        t.events = [TimelineEvent.from_json(e) for e in d.get("events", [])]
        t._next_id = max((e.id for e in t.events), default=0) + 1
        for u in d.get("samples", []):
            use = SampleUse.from_json(u)
            t.samples[use.id] = use
        return t
