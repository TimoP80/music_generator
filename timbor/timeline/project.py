"""timbor.timeline.project — project directory writer + timeline report."""
from __future__ import annotations

import os

from .serialization import save_project, PROJECT_VERSION


def write_project_directory(out_path: str, song, qc_notes: list[str],
                            buses: dict, l, r, stem_fmt: str = "float32",
                            stem_names: list[str] | None = None) -> str:
    """Write <dir>/project.json + <dir>/audio/{stems,master}.wav.

    out_path: the --o target (…/audio/master.wav or …/master.wav); the project
    root is derived from it. Returns the project.json path.
    """
    from timbor.render import write_stems
    out_path = os.path.abspath(out_path)
    # normalize: if out_path ends in audio/master.wav, root is its parent.parent
    parent = os.path.dirname(out_path)
    if os.path.basename(parent) == "audio":
        root = os.path.dirname(parent)
    else:
        root = parent
        parent = os.path.join(root, "audio")
    os.makedirs(parent, exist_ok=True)

    stems = write_stems(parent, buses, l, r, fmt=stem_fmt)
    rel_stems = [os.path.relpath(p, root).replace("\\\\", "/") for p in stems]
    from timbor.render import qc_stems
    stem_qc = qc_stems(buses, l, r)
    all_qc = list(qc_notes) + [f"stem qc: {n}" for n in stem_qc]
    pj = os.path.join(root, "project.json")
    save_project(pj, song, song.timeline, all_qc, stems=rel_stems,
                 audio_dir=os.path.relpath(parent, root).replace("\\\\", "/"))
    return pj


def timeline_report(project: dict) -> str:
    """Human-readable timeline (spec §21)."""
    t = project["song"]["bpm"]
    key = f"{project['song']['key']} {project['song']['scale']}"
    dur = project["song"]["duration_seconds"]
    out = [f"TIMBOR TIMELINE", "",
           f"{t} BPM", f"{key}", f"{dur} seconds", ""]
    bars_total = project["song"]["total_bars"]
    for s in project["sections"]:
        b0, b1 = int(s["start_beat"] / 4) + 1, int(s["end_beat"] / 4)
        out.append(f"{s['name'].upper():10s} {b0:>4}-{b1:<4} bars  energy={s['energy']:.2f}")
    out.append("")
    from collections import Counter
    counts = Counter(e["bus"] for e in project["timeline"])
    out.append("EVENTS")
    for bus in ("drums", "bass", "mel", "fx", "samples"):
        out.append(f"  {bus:8s} {counts.get(bus, 0):5d}")
    if project.get("samples"):
        out.append("")
        out.append("SAMPLES")
        uses: dict[str, list] = {}
        for e in project["timeline"]:
            if e.get("sample_id"):
                uses.setdefault(e["sample_id"], []).append(e)
        by_id = {u["id"]: u for u in project["samples"]}
        for sid, evs in uses.items():
            u = by_id.get(sid, {})
            chopped = sum(1 for e in evs if e.get("chop_ops"))
            rev = sum(1 for e in evs if e.get("reverse"))
            trans = [e for e in evs if e.get("pitch_semitones")]
            out.append(f"  {u.get('filename', sid)}")
            out.append(f"    {len(evs)} uses, {chopped} chopped, {rev} reversed")
            if trans:
                st = trans[0]["pitch_semitones"]
                out.append(f"    transposed {st:+.0f} semitones")
    return "\n".join(out)
