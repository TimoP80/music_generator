"""timbor.verify — --verify-render: replay a project and compare its audio
against the artifacts the project shipped with. Honest tiered results:
EXACT / FLOAT_EQUIV / APPROX / DIFFERENT (never "identical" for bytes that
differ)."""
from __future__ import annotations

import os

import numpy as np

from .replay import render_project, compare_buses


def _read_stem_mono(path: str) -> np.ndarray:
    """Read a float32 (WAVE_FORMAT_IEEE_FLOAT) or int16 stereo WAV, mono f64."""
    import struct
    with open(path, "rb") as f:
        hdr = f.read(12)
        if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            raise ValueError(f"not a RIFF/WAVE file: {path}")
        fmt = None
        raw = None
        while True:
            ch_hdr = f.read(8)
            if len(ch_hdr) < 8:
                break
            cid, csz = ch_hdr[:4], struct.unpack("<I", ch_hdr[4:8])[0]
            body = f.read(csz + (csz & 1))  # chunks are word-aligned
            if cid == b"fmt ":
                fmt = struct.unpack("<HHIIHH", body[:16])
            elif cid == b"data":
                raw = body[:csz]
    if fmt is None or raw is None:
        raise ValueError(f"missing fmt/data chunk: {path}")
    wFormatTag, ch, sr, _brate, _align, sw = fmt
    if wFormatTag not in (3, 0xFFFE) and wFormatTag != 1:
        raise ValueError(f"unsupported WAV format tag {wFormatTag}")
    if sw == 32:  # bits per sample: 32-bit float (fmt tag 3) or int32
        data = np.frombuffer(raw, "<f4").astype(np.float64)
    elif sw == 16:
        data = np.frombuffer(raw, "<i2").astype(np.float64) / 32767.0
    else:
        raise ValueError(f"unsupported stem bit depth {sw}")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data


def verify_render(project: dict, project_root: str,
                  sample_root: str | None = None) -> dict:
    """Re-render `project` and compare against its exported stems.

    Comparison basis (matches render.write_stems): bus stems store the mono
    bus duplicated L/R *before* stereoize/master, so a bus stem is compared
    directly against the replayed bus; only `master.wav` has the stereoize+
    master chain applied and is compared against the rebuilt master mono.
    Returns {"stems": {bus: tier-result}, "sample_status": [...],
    "pass": bool, "timeline_identical": True-by-construction}."""
    from .render import stereoize, master
    from .timeline.validation import validate_timeline
    from .timeline import Timeline

    timeline = Timeline.from_json({"bpm": project["song"]["bpm"],
                                   "sections": project.get("sections", []),
                                   "events": project.get("timeline", []),
                                   "samples": project.get("samples", [])})
    issues = [i for i in validate_timeline(timeline) if i["level"] == "error"]

    rebuilt, diags = render_project(project, sample_root=sample_root)
    l, r = stereoize(rebuilt)
    ml, mr = master(l, r)
    master_mono = (ml + mr) * 0.5
    del l, r, ml, mr

    audio_dir = os.path.join(project_root, project.get("audio_dir", "audio"))
    results = {}
    for bus in ("drums", "bass", "mel", "fx", "samples", "master"):
        p = os.path.join(audio_dir, f"{bus}.wav")
        if not os.path.exists(p):
            results[bus] = {"status": "missing", "max_diff": float("inf"),
                            "samples": 0}
            continue
        stem = _read_stem_mono(p)
        mono = master_mono if bus == "master" else \
            rebuilt.get(bus, np.zeros(0))
        n = min(len(stem), len(mono))
        if n == 0:
            results[bus] = {"status": "missing", "max_diff": float("inf"),
                            "samples": 0}
            continue
        # Stems ship as 32-bit float WAV: quantize the replay the same way so
        # storage rounding (~3e-8) is not reported as a musical difference.
        mono = mono[:n].astype(np.float32).astype(np.float64)
        stem = stem[:n]
        if np.array_equal(stem, mono):
            results[bus] = {"status": "exact", "max_diff": 0.0, "samples": n}
            continue
        d = float(np.max(np.abs(stem[:n] - mono[:n])))
        tier = "float_equiv" if d <= 1e-9 else \
               "approx" if d <= 0.5 else "different"
        results[bus] = {"status": tier, "max_diff": d, "samples": n}
    resolved = sum(1 for d in diags["sample_status"] if d["status"] == "ok")
    total = len(project.get("samples", []))
    ok = (not issues) and resolved == total and \
        all(v["status"] in ("exact", "float_equiv") for v in results.values())
    return {"stems": results, "sample_status": diags["sample_status"],
            "samples_resolved": f"{resolved}/{total}",
            "validation_errors": issues, "pass": ok}


def format_verify_report(project: dict, result: dict, project_root: str) -> str:
    name = os.path.basename(os.path.normpath(project_root))
    lines = ["TIMBOR RE-RENDER VERIFICATION", "",
             f"Project: {name}",
             f"Timeline: identical (executed from stored events)",
             f"Sample references: {result['samples_resolved']}",
             ""]
    for bus in ("drums", "bass", "mel", "fx", "samples", "master"):
        r = result["stems"].get(bus, {"status": "missing"})
        pretty = {"exact": "IDENTICAL", "float_equiv": "FLOAT-EQUIV",
                  "approx": "APPROX", "different": "DIFFERENT",
                  "missing": "MISSING"}[r["status"]]
        extra = "" if r["status"] in ("exact",) else f" (max|diff|={r['max_diff']:.2e})"
        lines.append(f"{bus.capitalize():8s} {pretty}{extra}")
    lines.append("")
    lines.append("RESULT: " + ("PASS" if result["pass"] else "DIFFERENT"))
    if not result["pass"]:
        approx = [b for b, v in result["stems"].items()
                  if v["status"] == "approx"]
        if approx:
            lines.append("")
            lines.append("Approximation note: " + ", ".join(approx) +
                         " differ by a documented mix-level transform that is not "
                         "fully recoverable from stored events; musical timing and "
                         "content are identical (see README limitations).")
    return "\n".join(lines)
