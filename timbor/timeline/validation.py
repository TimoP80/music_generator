"""timbor.timeline.validation — structured diagnostics for projects/timelines."""
from __future__ import annotations

from .timeline import Timeline
from .events import VALID_BUSES
from timbor.theory import NOTE_NAMES


def validate_timeline(t: Timeline, expected_beats: float | None = None,
                      check_audio_refs: bool = False) -> list[dict]:
    """Return a list of diagnostics: {"level": "error"|"warning", "code", "msg"}."""
    issues: list[dict] = []

    def err(code, msg):
        issues.append({"level": "error", "code": code, "msg": msg})

    def warn(code, msg):
        issues.append({"level": "warning", "code": code, "msg": msg})

    # bus names
    for e in t.events:
        if e.bus not in VALID_BUSES:
            err("bus-invalid", f"event {e.id}: unknown bus {e.bus!r}")
    # negative positions / durations
    for e in t.events:
        if e.start_beat < 0:
            err("negative-position", f"event {e.id} starts at {e.start_beat}")
        if e.duration_beats <= 0:
            err("invalid-duration", f"event {e.id} has duration {e.duration_beats}")
    # event ordering: sorted_events must be stable & non-decreasing in start
    evs = t.sorted_events()
    for a, b in zip(evs, evs[1:]):
        if b.start_beat < a.start_beat:
            err("ordering", f"event {b.id} starts before event {a.id}")
    # section boundaries
    for s in t.sections:
        if s.end_beat <= s.start_beat:
            err("section-invalid", f"section {s.name}#{s.index} has empty span")
        if s.start_beat < 0:
            err("section-invalid", f"section {s.name}#{s.index} negative start")
    secs = sorted(t.sections, key=lambda s: s.start_beat)
    for a, b in zip(secs, secs[1:]):
        if b.start_beat < a.end_beat - 1e-6:
            err("section-overlap", f"section {b.name}#{b.index} overlaps "
                                   f"{a.name}#{a.index}")
    # timeline end
    if expected_beats is not None:
        end = t.total_beats()
        if abs(end - expected_beats) > 0.25:  # > 1/16th note tolerance
            err("duration-mismatch",
                f"timeline ends at {end} beats, expected {expected_beats}")
    # bpm / key validity
    if not (20 <= t.bpm <= 400):
        err("bpm-invalid", f"bpm {t.bpm} outside 20..400")
    # sample references exist
    for e in t.events:
        if e.sample_id and e.sample_id not in t.samples:
            err("sample-ref-missing",
                f"event {e.id} references unknown sample {e.sample_id}")
    if check_audio_refs:
        import os
        for sid, u in t.samples.items():
            if not os.path.exists(u.path) and not os.path.isabs(u.path):
                warn("sample-file-missing",
                     f"sample {u.filename} ({u.path}) not found on disk")
    # sample file sanity
    for u in t.samples.values():
        if u.bpm is not None and not (40 <= u.bpm <= 250):
            warn("sample-bpm-odd", f"sample {u.filename} has odd bpm {u.bpm}")
    return issues


def format_diagnostics(issues: list[dict]) -> str:
    if not issues:
        return "validation: clean"
    out = []
    for i in issues:
        mark = "ERROR" if i["level"] == "error" else "warn"
        out.append(f"  [{mark}] {i['code']}: {i['msg']}")
    return "\n".join(out)
