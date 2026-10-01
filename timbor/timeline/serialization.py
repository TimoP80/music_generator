"""timbor.timeline.serialization — versioned project JSON (format v1)."""
from __future__ import annotations

import json
import os
import platform
import sys
import time

PROJECT_FORMAT = "timbor-project"
PROJECT_VERSION = 1

TIMBOR_VERSION = "0.3.0"


def _generator_meta(seed: int, extra: dict | None = None) -> dict:
    meta = {
        "name": "TIMBOR",
        "version": TIMBOR_VERSION,
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "sample_rate": 44100,
        "seed": seed,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if extra:
        meta.update(extra)
    return meta


def save_project(path: str, song, timeline, qc_notes: list[str],
                 stems: list[str] | None = None, audio_dir: str = "audio",
                 extra_meta: dict | None = None) -> dict:
    """Serialize the full project. `song` is a SongPlan; `timeline` a Timeline.

    The generation timestamp is metadata only — it never affects rendering.
    """
    plan = song.plan
    project = {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "generator": _generator_meta(
            plan.seed if plan.seed is not None else -1,
            {"genre": plan.genre, "mood": plan.mood, "era": plan.era,
             "sample_mode": getattr(plan, "sample_mode", "balanced"),
             "sample_library": getattr(plan, "sample_dir", None),
             "kit_seed": getattr(song, "kit_seed", None),
             "kick_type": song.plan.pack["kick"],
             "drums_kind": song.plan.pack["drums"]}),
        "song": {
            "genre": plan.genre,
            "bpm": song.bpm,
            "key": song.key,
            "scale": song.scale,
            "progression": song.prog_name,
            "motif": song.motif,
            "bass_instrument": song.bass_inst,
            "bass_style": song.bass_style,
            "lead_instrument": song.lead_inst,
            "duration_seconds": round(song.duration(), 2),
            "total_bars": song.total_bars(),
        },
        "plan": {
            "prompt": plan.prompt,
            "authenticity": plan.authenticity,
            "era": plan.era,
            "secondary_genres": plan.secondary,
            "variants": {k: v for k, v in song.variants.items()},
        },
        "sections": [s.to_json() for s in timeline.sections],
        "timeline": timeline.to_json()["events"],
        "samples": timeline.to_json()["samples"],
        "stems": stems or [],
        "audio_dir": audio_dir,
        "qc": {"notes": list(qc_notes)},
    }
    if extra_meta:
        project["metadata"] = extra_meta
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=1)
    return project


def load_project(path: str) -> dict:
    """Load a project file. Rejects unknown formats/versions with clear errors."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != PROJECT_FORMAT:
        raise ValueError(f"not a TIMBOR project (format={data.get('format')!r})")
    v = data.get("version")
    if not isinstance(v, int):
        raise ValueError(f"invalid project version: {v!r}")
    if v > PROJECT_VERSION:
        raise ValueError(f"project version {v} is newer than supported "
                         f"version {PROJECT_VERSION}; please update TIMBOR")
    if v < PROJECT_VERSION:
        raise ValueError(f"project version {v} is older than supported "
                         f"version {PROJECT_VERSION}; migration not implemented")
    return data


def timeline_of(project: dict):
    """Rebuild a Timeline object from a loaded project dict."""
    from .timeline import Timeline
    return Timeline.from_json({"bpm": project["song"]["bpm"],
                               "sections": project["sections"],
                               "events": project["timeline"],
                               "samples": project["samples"]})
