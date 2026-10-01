"""timbor.export.reaper — REAPER project (.rpp) export.

Generates a structurally valid RPP text project: tracks per bus with the
exported stems as media items, tempo, time signature, section markers and
project length. Sample events become media items pointing at the actual
source files where their transformations can be represented (playrate for
stretch, pitch adjustment); transformations that cannot be represented are
noted in the item notes rather than silently approximated.
"""
from __future__ import annotations

import json
import os
import posixpath


def _esc(s: str) -> str:
    return s.replace('"', "'")


def export_reaper(project: dict, project_root: str, out_subdir: str = "reaper") -> str:
    """Write <project_root>/<out_subdir>/<name>.rpp. Returns the path."""
    name = os.path.basename(os.path.normpath(project_root)) or "track"
    song = project["song"]
    bpm = float(song["bpm"])
    sr = int(project["generator"].get("sample_rate", 44100))
    total_beats = max((s["end_beat"] for s in project.get("sections", [])),
                      default=0.0)
    length_s = total_beats * 60.0 / bpm

    audio_dir = project.get("audio_dir", "audio")
    # the .rpp lives in <project_root>/reaper/, so one level up is the root
    stems = {os.path.basename(p): os.path.join("..", audio_dir,
                                               os.path.basename(p))
             for p in project.get("stems", [])}

    def rel(p: str) -> str:
        # forward slashes: RPP-portable and byte-identical across platforms
        return "/".join(seg for seg in p.replace("\\", "/").split("/") if seg)

    out = []
    out.append("<REAPER_PROJECT 0.1 \"TIMBOR export\" 1")
    out.append(f"  SAMPLERATE {sr} 0 0")
    out.append(f"  TEMPO {bpm} 4 4")
    out.append(f"  LENGTH {length_s:.6f}")
    # section markers (index must increment: REAPER rejects duplicate IDs)
    for mi, s in enumerate(project.get("sections", []), start=1):
        pos_s = s["start_beat"] * 60.0 / bpm
        out.append(f"  MARKER {mi} {pos_s:.6f} {_esc(s['name'].upper())} 0 R 0")
    # master track
    out.append("  <TRACK")
    out.append("    NAME Master")
    m = stems.get("master.wav")
    if m:
        out.append("    <ITEM")
        out.append(f"      POSITION 0")
        out.append(f"      LENGTH {length_s:.6f}")
        out.append(f"      NAME master")
        out.append(f"      SOURCE FILE \"{rel(m)}\"")
        out.append("    >")
    out.append("  >")

    track_defs = [("Drums", "drums.wav"), ("Bass", "bass.wav"),
                  ("Melody", "mel.wav"), ("FX", "fx.wav"),
                  ("Samples", "samples.wav")]
    for tname, stem in track_defs:
        out.append("  <TRACK")
        out.append(f"    NAME {tname}")
        p = stems.get(stem)
        if p:
            out.append("    <ITEM")
            out.append("      POSITION 0")
            out.append(f"      LENGTH {length_s:.6f}")
            out.append(f"      NAME {stem.split('.')[0]}")
            out.append(f"      SOURCE FILE \"{rel(p)}\"")
            out.append("    >")
        out.append("  >")

    # sample events as media items on the Samples track (before the stem item
    # would overlap; place them after project end region is not useful — so
    # write them into a dedicated 'TIMBOR Sample Events' track as items with
    # playrate/pitch where representable, else with SOBSCOPE notes)
    out.append("  <TRACK")
    out.append("    NAME TIMBOR Sample Events")
    sample_by_id = {s["id"]: s for s in project.get("samples", [])}
    idx = 0
    for ev in sorted(project["timeline"], key=lambda e: (e["start_beat"], e["id"])):
        if not ev.get("sample_id"):
            continue
        u = sample_by_id.get(ev["sample_id"], {})
        src = u.get("path", "")
        pos_s = ev["start_beat"] * 60.0 / bpm
        dur_s = ev["duration_beats"] * 60.0 / bpm
        rate = float(ev.get("stretch_ratio", 1.0)) if ev.get("stretch_ratio") else 1.0
        out.append("    <ITEM")
        out.append(f"      POSITION {pos_s:.6f}")
        out.append(f"      LENGTH {dur_s:.6f}")
        out.append(f"      NAME {_esc(u.get('filename', ev['sample_id']))}")
        out.append(f"      SOFFS 0")
        out.append(f"      PLAYRATE {rate:.6f} 1 0 -1 1")
        if ev.get("pitch_semitones"):
            out.append(f"      PITCH {float(ev['pitch_semitones']) / 12.0:.6f}")
        notes = [f"role={ev['role']}", f"reverse={1 if ev.get('reverse') else 0}"]
        if ev.get("chop_ops"):
            notes.append("chop_ops present (see project.json; chop is baked into "
                         "the samples stem)")
        if ev.get("reverse"):
            notes.append("REVERSE not representable in RPP item; use stem audio")
        out.append("      NOTES \"" + _esc("; ".join(notes)) + "\"")
        out.append(f"      SOURCE FILE \"{_esc(src)}\"")
        out.append("    >")
        idx += 1
    out.append("  >")
    out.append(">")
    out.append("")

    out_dir = os.path.join(project_root, out_subdir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.rpp")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path
