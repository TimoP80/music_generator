"""timbor.replay — deterministic re-render of a saved project.

Pipeline: project.json -> Timeline -> execute recorded events (procedural
voices via stored seeds, samples via stored transformations) -> finalize_mix
(shared with live rendering) -> buses. No musical decisions are made here:
every value executed was recorded at render time.

Sample chops are reproduced by rebuilding the identical per-placement RNG
(seed, bar, crc32(role)) used in the original render, so probabilities in
`chop_ops` replay to the exact same slice decisions.
"""
from __future__ import annotations

import os
import zlib

import numpy as np

from . import dsp
from . import voices
from .theory import hz
from .engine import finalize_mix, _build_kit
from .timeline import Timeline
from .timeline.validation import validate_timeline


# ----------------------------------------------------------------------------
# sample reference resolution
# ----------------------------------------------------------------------------

def resolve_samples(project: dict, sample_root: str | None = None) -> list[dict]:
    """Resolve every sample reference against the filesystem.

    Statuses: "ok" | "missing" | "changed". Never substitutes another sample.
    """
    out = []
    for u in project.get("samples", []):
        p = u.get("path", "")
        cands = [p]
        if sample_root:
            cands.insert(0, os.path.join(sample_root, u.get("filename", "")))
            cands.append(os.path.join(sample_root, os.path.basename(p)))
        found = next((c for c in cands if c and os.path.exists(c)), None)
        if found is None:
            out.append({"id": u["id"], "path": p, "status": "missing",
                        "detail": (f"Missing sample:\n    {p}\n"
                                   f"Expected:\n    size: {u.get('file_size')}\n"
                                   "Possible:\n    file was moved\n"
                                   "    file was renamed\n"
                                   "    sample library changed")})
            continue
        st = os.stat(found)
        if u.get("file_size") and st.st_size != u["file_size"]:
            out.append({"id": u["id"], "path": found, "status": "changed",
                        "detail": f"size {st.st_size} != recorded {u['file_size']} "
                                  f"(SAMPLE_CHANGED)"})
        else:
            out.append({"id": u["id"], "path": found, "status": "ok",
                        "detail": os.path.basename(found)})
    return out


# ----------------------------------------------------------------------------
# audio replay
# ----------------------------------------------------------------------------

def render_project(project: dict, sample_root: str | None = None) -> tuple[dict, dict]:
    """Execute the stored timeline. Returns (buses, diagnostics).

    buses are post leveling / pre master — byte-identical to the original
    render given the same project + sample files.
    """
    diags: dict = {"sample_status": resolve_samples(project, sample_root),
                   "warnings": [], "skipped": 0}
    timeline = Timeline.from_json({
        "bpm": project["song"]["bpm"],
        "sections": project.get("sections", []),
        "events": project.get("timeline", []),
        "samples": project.get("samples", []),
    })
    issues = validate_timeline(timeline)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise ValueError("project timeline failed validation: " +
                         "; ".join(i["msg"] for i in errors))
    missing = [d for d in diags["sample_status"] if d["status"] == "missing"]
    if missing:
        raise FileNotFoundError(
            "SAMPLE_NOT_FOUND: " + "; ".join(d["path"] for d in missing))

    bpm = project["song"]["bpm"]
    beat = 60.0 / bpm
    step = beat / 4
    total_bars = int(project["song"]["total_bars"])
    total_n = int(total_bars * 16 * step * dsp.SR) + dsp.SR
    buses = {"drums": np.zeros(total_n), "bass": np.zeros(total_n),
             "mel": np.zeros(total_n), "fx": np.zeros(total_n)}
    # live rendering doubles the drop-section melody content (notes only, pre
    # stab/pad) as mel = 0.8*notes + 0.4*hp(notes) + chords; track separately
    mel_notes = np.zeros(total_n)
    mel_chords = np.zeros(total_n)
    bar_n = int(16 * step * dsp.SR)

    gen = project.get("generator", {})
    seed = int(gen.get("seed", 0)) & 0x7FFFFFFF
    kit = None
    if gen.get("kit_seed") is not None:
        kit = _build_kit({"kick": gen.get("kick_type", "four"),
                          "drums": gen.get("drums_kind", "hardcore")},
                         bpm, int(gen["kit_seed"]))

    # sample audio lazy cache
    sample_audio: dict[str, tuple[np.ndarray, int]] = {}
    resolved_by_id = {d["id"]: d.get("path") for d in diags["sample_status"]}

    def _load(path: str):
        if path not in sample_audio:
            lower = path.lower()
            fmt = "wav" if lower.endswith(".wav") else \
                "aiff" if lower.endswith((".aiff", ".aif")) else None
            if fmt is None:
                raise ValueError("no decoder for this format")
            from .samples.scanner import decode_audio
            x, sr = decode_audio(path, fmt, max_seconds=40.0)
            sample_audio[path] = (x - float(np.mean(x)), sr)
        return sample_audio[path]

    from .samples.transformer import (resample, wsola_stretch, pitch_shift,
                                      align_to_bars, chop_sample, bars_to_sec)

    # Section sample offsets, accumulated EXACTLY like finalize_mix does
    # (pos += int(bars * 16 * step * SR)) so replay placement arithmetic is
    # bit-identical to the live renderer.
    sec_offsets: dict[str, tuple[int, int, int]] = {}  # name -> (off, base_steps, n_samples)
    _acc_n = 0
    _acc_steps = 0
    for s in project.get("sections", []):
        bars = int(round((s["end_beat"] - s["start_beat"]) / 4))
        n_samples = int(bars * 16 * step * dsp.SR)
        sec_offsets[s["name"]] = (_acc_n, _acc_steps, n_samples)
        _acc_n += n_samples
        _acc_steps += bars * 16
    sec_span = {s["name"]: (s["start_beat"], s["end_beat"])
                for s in project.get("sections", [])}

    def _i0(ev_start_beat: float, sec_name: str) -> int:
        """Sample position via the exact float expression the live render used:
        int(step_local * step * SR) + section sample offset."""
        off, base_steps, _n = sec_offsets.get(sec_name, (0, 0, 0))
        step_local = int(round(ev_start_beat / 0.25)) - base_steps
        return off + int(step_local * step * dsp.SR)

    def _place(bus_arr, i0: int, w: np.ndarray, sec_name: str, gain: float) -> None:
        """Place a voice exactly like the live per-section renderer: clipped at
        the section end (no bleed across section boundaries)."""
        n_sec = sec_offsets.get(sec_name, (0, 0, total_n))[2]
        end = min(total_n, sec_offsets.get(sec_name, (0, 0, total_n))[0] + n_sec)
        e = min(end, i0 + len(w))
        if e > i0:
            bus_arr[i0:e] += w[: e - i0] * gain

    # ---- procedural passes -------------------------------------------------
    chop_after: list[tuple[str, int, dict]] = []  # (section, local_bar, decisions)
    for ev in timeline.sorted_events():
        i0 = _i0(ev.start_beat, ev.section)
        if i0 >= total_n:
            continue
        # voice duration uses the exact step count stored on the event
        # (dur_steps can be fractional, e.g. jungle bass = 2.5)
        dur_s = float((ev.metadata or {}).get("dur_steps", ev.duration_beats / 0.25)) * step
        if ev.bus == "drums":
            if ev.type == "break_chop":
                dec = (ev.metadata or {}).get("extra", {}).get("decisions")
                if dec:
                    _off, base, _n = sec_offsets.get(ev.section, (0, 0, 0))
                    chop_after.append((ev.section,
                                       int(round(ev.start_beat / 4)) - base // 16,
                                       dec))
                continue
            w = (kit or {}).get(ev.instrument or ev.type)
            if w is None:
                diags["skipped"] += 1
                continue
            _place(buses["drums"], i0, w, ev.section, ev.velocity)
        elif ev.bus == "bass":
            seed_v = (ev.metadata or {}).get("voice_seed")
            if seed_v is None or ev.pitch is None:
                diags["skipped"] += 1
                continue
            # live passes last_midi=None and any logged accent/slide extras
            w = voices.synth_voice(ev.instrument, ev.pitch, dur_s, ev.velocity,
                                   seed=int(seed_v), last_midi=None,
                                   **((ev.metadata or {}).get("extra", {}) or {}))
            _place(buses["bass"], i0, w, ev.section, ev.velocity)
        elif ev.bus == "mel":
            meta = ev.metadata or {}
            if ev.type in ("stab", "pad"):
                seed_v = meta.get("voice_seed")
                notes = meta.get("extra", {}).get("notes", [])
                if seed_v is None or not notes:
                    diags["skipped"] += 1
                    continue
                if ev.type == "stab":
                    w = sum(voices.synth_voice("stab", nn, dur_s, 0.8,
                                               seed=int(seed_v)) for nn in notes[:3])
                    w = np.asarray(w)
                    w = w * (0.7 / (float(np.max(np.abs(w))) or 1.0))
                else:
                    w = dsp.pad_chord([hz(nn) for nn in notes], dur_s, seed=int(seed_v))
                _place(buses["mel"], i0, w, ev.section, 0.7)
                _place(mel_chords, i0, w, ev.section, 0.7)
            elif str(ev.instrument).startswith("experimental"):
                diags["warnings"].append(
                    "experimental mel transform is a mix-level effect; not applied "
                    "per-event on re-render (see limitations)")
                continue
            else:
                seed_v = meta.get("voice_seed")
                if seed_v is None or ev.pitch is None:
                    diags["skipped"] += 1
                    continue
                w = voices.synth_voice(ev.instrument, ev.pitch, dur_s, ev.velocity,
                                       seed=int(seed_v))
                if meta.get("extra", {}).get("soft"):
                    w = dsp.lp(w, 2200) * 0.6
                # live doubles note content; stash notes-only and compose below
                # (velocity-scaled, matching the original bus placement)
                _place(mel_notes, i0, w, ev.section, float(ev.velocity))
        elif ev.bus == "fx":
            kind = ev.instrument
            seed_v = (ev.metadata or {}).get("voice_seed")
            if seed_v is None:
                diags["skipped"] += 1
                continue
            # riser/impact/crackle/downlifter were rendered section-locally in
            # the live path; position them inside their section span using the
            # exact accumulated section offsets (not beat-float arithmetic,
            # which rounds differently at non-integer-step tempos)
            si0, _sb_steps, sn = sec_offsets.get(
                ev.section, (int(ev.start_beat * beat * dsp.SR), 0,
                             int(ev.duration_beats * beat * dsp.SR)))
            if kind == "riser":
                # live derives the duration from the section's integer sample
                # length (n / SR round trip), NOT from beat-float seconds —
                # the two round differently and a 1-sample shift decorrelates
                # the noise tail (max|diff| = 2.0). Mirror the live length.
                r = dsp.riser(sn / dsp.SR, seed=int(seed_v))
                # live: placed at the END of the section (i0 = max(0, n - len(r)))
                base = si0 + sn - len(r)
                ri = max(si0, base)
                buses["fx"][ri:ri + len(r)] += r[: min(total_n, ri + len(r)) - ri] * 0.5
            elif kind == "impact":
                im = dsp.impact(seed=int(seed_v))
                e = min(total_n, si0 + len(im))
                buses["fx"][si0:e] += im[: e - si0] * 0.4
                if kit is not None:
                    cr = kit.get("crash")
                    if cr is not None:
                        e2 = min(total_n, si0 + len(cr))
                        buses["fx"][si0:e2] += cr[: e2 - si0] * 0.5
            elif kind == "crackle":
                cr = dsp.vinyl_crackle(sn / dsp.SR, seed=int(seed_v),
                                       level=ev.velocity)
                # the float seconds→samples round trip inside the DSP can be
                # off by one at some section lengths — normalize to exactly
                # the section length so the slice addition always lines up
                if len(cr) < sn:
                    cr = np.concatenate([cr, np.zeros(sn - len(cr))])
                elif len(cr) > sn:
                    cr = cr[:sn]
                e = min(total_n, si0 + sn)
                buses["fx"][si0:e] += cr[: e - si0]
            elif kind == "downlifter":
                # live: duration = min(section_len, 2 bars), placed at section start
                dl = dsp.downlifter(min(sn / dsp.SR, bars_to_sec(2, bpm)),
                                    seed=int(seed_v))
                e = min(total_n, si0 + len(dl))
                buses["fx"][si0:e] += dl[: e - si0] * 0.4
            elif kind == "vox":
                w = voices.synth_voice("vox", ev.pitch, dur_s, ev.velocity,
                                       seed=int(seed_v))
                e = min(total_n, i0 + len(w))
                buses["fx"][i0:e] += w[: e - i0] * 0.4

    # break chops apply after every hit is placed (matches live rendering,
    # where chops run at the end of each section's drum pass)
    for sec_name, bar_local, dec in chop_after:
        off, _base, _n = sec_offsets.get(sec_name, (0, 0, 0))
        a = off + bar_local * bar_n
        if a + bar_n <= total_n:
            buses["drums"][a:a + bar_n] = voices.apply_chop_decisions(
                buses["drums"][a:a + bar_n], dec)

    # ---- samples bus -------------------------------------------------------
    smp = np.zeros(total_n)
    for ev in timeline.sorted_events():
        if not ev.sample_id:
            continue
        path = resolved_by_id.get(ev.sample_id)
        if not path:
            continue
        try:
            x, sr = _load(path)
        except Exception as e:
            diags["warnings"].append(
                f"sample {ev.sample_id} undecodable -> skipped ({str(e)[:50]})")
            continue
        meta = ev.metadata or {}
        gain = float(meta.get("gain_linear", 10 ** (ev.gain_db / 20.0)))
        # sample placements used absolute bars * bar_n in the live path
        abs_bar = int(round(ev.start_beat / 4))
        i0 = abs_bar * bar_n
        off8 = int(round((ev.start_beat - abs_bar * 4) / 0.5))
        if off8 == 1:
            i0 += bar_n // 2  # vocal_hit offbeat entry
        if ev.metadata.get("kind") == "oneshot":
            y = resample(x, sr, dsp.SR)
            if ev.pitch_semitones:
                y = pitch_shift(y, ev.pitch_semitones)
            if ev.role == "riser":
                target = int(bars_to_sec(2, bpm) * dsp.SR)
                if abs(1.0 - ev.stretch_ratio) > 0.01 and len(y) > dsp.SR * 0.5:
                    y = wsola_stretch(y, ev.stretch_ratio)
                if len(y) < target:
                    y = np.pad(y, (0, target - len(y)))
                y = y[:target]
                i0 = max(0, int(round(ev.start_beat / 4)) - 8) * bar_n
            elif ev.role == "impact":
                y = dsp.hp(y, 40)
                y = dsp.normalize(y, 0.8)
            elif ev.role == "stab_hit":
                y = dsp.normalize(y, 0.6)
                if len(y) > bar_n // 2:
                    y = y[: bar_n // 2]
            elif ev.role == "vocal_hit":
                y = dsp.normalize(y, 0.55)
            if i0 >= total_n:
                continue
            e = min(total_n, i0 + len(y))
            smp[i0:e] += y[: e - i0] * gain
        else:
            loop_bars = float(meta.get("loop_bars") or 1.0)
            base = align_to_bars(x, bpm, loop_bars)
            if abs(ev.stretch_ratio - 1.0) > 0.005:
                base = wsola_stretch(base, ev.stretch_ratio)
            # identical per-placement RNG as the original render
            prng = np.random.default_rng([seed, int(round(ev.start_beat / 4)),
                                          zlib.crc32(ev.role.encode())])
            y_bar = chop_sample(base, dsp.SR, bpm, 1.0, prng, slices_per_bar=4,
                                ops=ev.chop_ops)[: int(bars_to_sec(1, bpm) * dsp.SR)]
            drop = meta.get("dropout")
            if drop == "first_half":
                y_bar = y_bar[: len(y_bar) // 2]
            elif drop == "second_half":
                y_bar = np.concatenate([np.zeros(len(y_bar) // 2),
                                        y_bar[len(y_bar) // 2:]])
            if ev.pitch_semitones:
                y_bar = pitch_shift(y_bar, ev.pitch_semitones)
            variant = meta.get("variant", "drop")
            if variant == "intro":
                y_bar = dsp.lp(y_bar, 1200)
            elif variant == "breakdown":
                y_bar = dsp.lp(y_bar, 2500) * 0.8
            elif variant == "outro":
                y_bar = dsp.lp(y_bar, 900) * 0.7
            if i0 >= total_n:
                continue
            e = min(total_n, i0 + len(y_bar))
            smp[i0:e] += y_bar[: e - i0] * gain

    # drop-section octave doubling, exactly like the live renderer:
    # mel = 0.8*content + 0.4*hp(content) over the full section bus
    # (empirically the closest match across all four genre packs; the notes-
    # only variant measured worse — see README limitations)
    buses["mel"] += mel_notes
    lead = project["song"].get("lead_instrument", "supersaw")
    if lead in ("supersaw", "hoover"):
        for s in project.get("sections", []):
            if s["name"] not in ("drop", "drop2"):
                continue
            off, base_steps, n_sec = sec_offsets[s["name"]]
            a, b = off, min(off + n_sec, total_n)
            content = buses["mel"][a:b]
            buses["mel"][a:b] = content * 0.8 + dsp.hp(content, 800) * 0.4

    leveled = finalize_mix(_sections_to_arrays(project, buses, step),
                           {"samples": smp} if float(np.max(np.abs(smp))) > 1e-6 else None,
                           bass_inst=project["song"].get("bass_instrument", "rumble"),
                           genre=project["song"]["genre"],
                           era=project.get("plan", {}).get("era", "modern"),
                           sample_mode=gen.get("sample_mode", "balanced"),
                           total_bars=total_bars, step=step,
                           duration=total_bars * 16 * step,
                           mel_inst=project["song"].get("lead_instrument", "supersaw"))
    return leveled, diags


def _sections_to_arrays(project: dict, buses: dict, step: float) -> list[dict]:
    """Adapt whole-track arrays to finalize_mix's per-section input format.

    Section offsets are accumulated with the exact truncation finalize_mix
    uses (`pos += int(bars * 16 * step * SR)`); rounding the float beat math
    here instead drifts by ±1 sample per section at non-integer-step tempos.
    """
    out = []
    acc = 0
    for s in project.get("sections", []):
        bars = int(round((s["end_beat"] - s["start_beat"]) / 4))
        n = int(bars * 16 * step * dsp.SR)
        i0 = acc
        acc += n
        chunk = {}
        for bus, arr in buses.items():
            a = arr[i0:i0 + n]
            chunk[bus] = a if len(a) == n else np.pad(a, (0, max(0, n - len(a))))
        out.append({"bars": bars, "buses": chunk, "name": s["name"],
                    "energy": s["energy"]})
    return out


# ----------------------------------------------------------------------------
# verification helper
# ----------------------------------------------------------------------------

def compare_buses(a: dict, b: dict) -> dict[str, dict]:
    """Tiered bus comparison (honest verification, spec §7).

    Returns per bus: {"status": "exact"|"float_equiv"|"approx"|"different",
                      "max_diff": float, "samples": int}.
    """
    out = {}
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None or len(va) != len(vb):
            out[k] = {"status": "different", "max_diff": float("inf"),
                      "samples": 0}
            continue
        n = len(va)
        if np.array_equal(va, vb):
            out[k] = {"status": "exact", "max_diff": 0.0, "samples": n}
            continue
        d = float(np.max(np.abs(va - vb)))
        if d <= 1e-9:
            out[k] = {"status": "float_equiv", "max_diff": d, "samples": n}
        elif d <= 0.5:
            out[k] = {"status": "approx", "max_diff": d, "samples": n}
        else:
            out[k] = {"status": "different", "max_diff": d, "samples": n}
    return out
