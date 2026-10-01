"""timbor.export.manifest — DAW-neutral export manifest.

The manifest is the canonical interchange representation: tempo, key,
sections, tracks, every event, sample references and stem files. No DAW-specific
assumptions; no timestamps (deterministic bytes, spec §28).
"""
from __future__ import annotations

import json
import os
import zlib


def _sid(path: str) -> str:
    return f"smp_{zlib.crc32(path.encode()) & 0xFFFFFFFF:08x}"


def build_manifest(project: dict) -> dict:
    """Build the DAW-neutral manifest dict (shared by export_manifest and the
    Ableton export kit). Deterministic: no timestamps, sort_keys on write."""
    song = project["song"]
    stems = project.get("stems", [])
    audio_dir = project.get("audio_dir", "audio")

    events = []
    for ev in sorted(project["timeline"], key=lambda e: (e["start_beat"], e["id"])):
        e = {
            "id": ev["id"], "type": ev["type"], "bus": ev["bus"],
            "role": ev.get("role", ""), "section": ev.get("section", ""),
            "start_beat": ev["start_beat"],
            "duration_beats": ev["duration_beats"],
            "gain_db": ev.get("gain_db", 0.0),
        }
        if ev.get("instrument"):
            e["instrument"] = ev["instrument"]
        if ev.get("pitch") is not None:
            e["pitch"] = ev["pitch"]
            e["velocity"] = ev.get("velocity", 1.0)
        if ev.get("sample_id"):
            u = next((s for s in project.get("samples", [])
                      if s["id"] == ev["sample_id"]), {})
            e.update({
                "sample": {
                    "id": ev["sample_id"],
                    "path": u.get("path"),
                    "filename": u.get("filename"),
                },
                "pitch_semitones": ev.get("pitch_semitones", 0.0),
                "stretch_ratio": ev.get("stretch_ratio", 1.0),
                "reverse": bool(ev.get("reverse")),
                "chop_ops": ev.get("chop_ops", {}),
            })
        events.append(e)

    manifest = {
        "format": "timbor-export-manifest",
        "version": 1,
        "source_project": {
            "format": project.get("format"),
            "version": project.get("version"),
            "generator": {"name": project["generator"]["name"],
                          "version": project["generator"]["version"],
                          "seed": project["generator"]["seed"]},
        },
        "tempo": {"bpm": song["bpm"], "time_signature": "4/4"},
        "key": {"root": song["key"], "scale": song["scale"]},
        "sections": [
            {"name": s["name"], "index": s["index"],
             "start_beat": s["start_beat"], "end_beat": s["end_beat"],
             "energy": s["energy"], "bars": s["bars"]}
            for s in project.get("sections", [])
        ],
        "tracks": [
            {"name": "Drums", "bus": "drums",
             "stem": next((p for p in stems if p.endswith("drums.wav")), None),
             "events": sum(1 for e in events if e["bus"] == "drums")},
            {"name": "Bass", "bus": "bass",
             "stem": next((p for p in stems if p.endswith("bass.wav")), None),
             "events": sum(1 for e in events if e["bus"] == "bass")},
            {"name": "Melody", "bus": "mel",
             "stem": next((p for p in stems if p.endswith("mel.wav")), None),
             "events": sum(1 for e in events if e["bus"] == "mel")},
            {"name": "FX", "bus": "fx",
             "stem": next((p for p in stems if p.endswith("fx.wav")), None),
             "events": sum(1 for e in events if e["bus"] == "fx")},
            {"name": "Samples", "bus": "samples",
             "stem": next((p for p in stems if p.endswith("samples.wav")), None),
             "events": sum(1 for e in events if e["bus"] == "samples")},
            {"name": "Master", "bus": "master",
             "stem": next((p for p in stems if p.endswith("master.wav")), None),
             "events": 0},
        ],
        "events": events,
        "samples": [
            {"id": s["id"], "path": s.get("path"), "filename": s.get("filename"),
             "file_size": s.get("file_size"), "mtime": s.get("mtime"),
             "bpm": s.get("bpm"), "key": s.get("key"), "category": s.get("category")}
            for s in project.get("samples", [])
        ],
        "project_length_beats": max((s["end_beat"] for s in project.get("sections", [])),
                                     default=0.0),
    }
    return manifest


def export_manifest(project: dict, out_dir: str, name: str = "track") -> str:
    """Write <out_dir>/<name>_export.json. Returns the path."""
    manifest = build_manifest(project)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}_export.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return path
