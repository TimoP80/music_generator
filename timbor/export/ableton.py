"""timbor.export.ableton — Ableton Live "export kit" (spec §20).

Ableton Live's .als format is an undocumented gzipped Live-specific XML
format; synthesizing a fake .als that Live might mis-load or silently
corrupt is worse than not shipping one. Instead this writes an honest,
complete **Ableton import kit** under <project_root>/ableton/:

  ableton/
    README.md          step-by-step Live import instructions
    manifest.json      the DAW-neutral manifest (same bytes as export/)
    midi/<name>.mid    the format-1 SMF (Drums/Bass/Melody/Chords + FX/Samples
                       text meta) — drag onto Live MIDI tracks
    stems.md           reference table of the exported bus stems
    samples.csv        per-event sample cue sheet (path, role, transform)

No .als file is produced. Everything here loads into Live via documented
interfaces (MIDI drag-and-drop, audio import), which cannot corrupt a
session.
"""
from __future__ import annotations

import csv
import io
import json
import os

from .manifest import build_manifest
from .midi import build_midi


def _stems_table(project: dict, manifest: dict) -> str:
    lines = [
        "| track | bus | stem file | events |",
        "|---|---|---|---|",
    ]
    for t in manifest["tracks"]:
        lines.append(f"| {t['name']} | {t['bus']} | "
                     f"`{t['stem'] or '—'}` | {t['events']} |")
    audio_dir = project.get("audio_dir", "audio")
    lines.append("")
    lines.append(f"Stems live in `{audio_dir}/` inside the project directory "
                 "(relative paths in the table are from the project root).")
    return "\n".join(lines)


def _readme(project: dict, manifest: dict, midi_name: str) -> str:
    song = project["song"]
    secs = manifest["sections"]
    return f"""# Ableton Live import kit — {manifest["source_project"]["generator"]["name"]} {song["bpm"]} BPM {song["key"]} {song["scale"]}

TIMBOR does not write `.als` files (undocumented format; a synthesized one
could corrupt a Live session). This kit instead contains everything needed
to rebuild the track in Ableton Live in a few minutes using only documented
Live interfaces.

## Project facts

* Tempo: **{song["bpm"]} BPM**, time signature **4/4**
* Key: **{song["key"]} {song["scale"]}**
* Length: {manifest["project_length_beats"]:g} beats ({len(secs)} sections:
  {" → ".join(s["name"] for s in secs)})
* Generator: {manifest["source_project"]["generator"]["name"]} v{manifest["source_project"]["generator"]["version"]},
  seed **{manifest["source_project"]["generator"]["seed"]}**

## Import steps

1. **Set the session tempo** to {song["bpm"]} BPM (and enable the global
   quantization you prefer).
2. **Stems:** import the six bus stems listed in `stems.md` from the
   project's `{project.get("audio_dir", "audio")}/` directory onto six audio
   tracks named Drums / Bass / Melody / FX / Samples / Master. Drop them at
   bar 1 with warping **off** — they are already correctly timed and mixed.
3. **MIDI (optional, for editing):** drag `midi/{midi_name}.mid` onto a MIDI
   track; Live splits it into the named tracks (Drums on channel 10,
   Bass, Melody, Chords; FX/Samples tracks carry text meta-events only).
   Set Live's MIDI import to *merge into one track* if you prefer manual
   routing.
4. **Samples:** every sample placement is documented in `samples.csv`
   (source path, role, bar position, pitch/stretch/reverse transform) so
   you can re-create the sample bus with your own Simpler/Sampler racks
   and the exact source files.
5. **Cue points:** section positions (INTRO, DROP, …) are in the manifest
   under `sections`; place locators at `start_beat / 4 + 1` bars.
6. **Verification:** the manifest in this kit is byte-identical to the
   DAW-neutral `export/{midi_name}_export.json`.

## What is not included

* `.als` — intentionally not synthesized (see above).
* Instrument sounds — MIDI notes reference GM mappings (channel 10 drum
  map, melodic notes at written pitch); pick your own instruments.
* Mix/finalization — the exported stems are the mix; Live's master chain
  stays empty.
"""


def export_ableton(project: dict, project_root: str,
                   out_subdir: str = "ableton") -> str:
    """Write the Ableton import kit under <project_root>/<out_subdir>/.

    Returns the kit root path. Deterministic file set; no timestamps.
    """
    name = os.path.basename(os.path.normpath(project_root)) or "track"
    manifest = build_manifest(project)

    kit = os.path.join(project_root, out_subdir)
    os.makedirs(kit, exist_ok=True)

    # 1. manifest.json — same dict, same deterministic bytes as export_manifest
    with open(os.path.join(kit, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)

    # 2. MIDI file (binary-identical to the midi/ export)
    midi_rel = os.path.join("midi", f"{name}.mid")
    os.makedirs(os.path.join(kit, "midi"), exist_ok=True)
    with open(os.path.join(kit, midi_rel), "wb") as f:
        f.write(build_midi(project))

    # 3. stems reference table
    with open(os.path.join(kit, "stems.md"), "w", encoding="utf-8") as f:
        f.write(_stems_table(project, manifest) + "\n")

    # 4. samples cue sheet
    sample_by_id = {s["id"]: s for s in project.get("samples", [])}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["start_beat", "bar", "role", "section", "sample_file",
                "path", "pitch_semitones", "stretch_ratio", "reverse",
                "gain_db"])
    for ev in sorted(project["timeline"], key=lambda e: (e["start_beat"], e["id"])):
        if not ev.get("sample_id"):
            continue
        s = sample_by_id.get(ev["sample_id"], {})
        w.writerow([
            f"{ev['start_beat']:g}", int(ev["start_beat"] // 4) + 1,
            ev["role"], ev.get("section", ""),
            s.get("filename", ev["sample_id"]),
            s.get("path", ""),
            f"{ev.get('pitch_semitones', 0):+g}",
            f"{ev.get('stretch_ratio', 1.0):g}",
            1 if ev.get("reverse") else 0,
            f"{ev.get('gain_db', 0.0):+g}",
        ])
    with open(os.path.join(kit, "samples.csv"), "w", encoding="utf-8",
              newline="") as f:
        f.write(buf.getvalue())

    # 5. README
    with open(os.path.join(kit, "README.md"), "w", encoding="utf-8") as f:
        f.write(_readme(project, manifest, name) + "\n")

    return kit
