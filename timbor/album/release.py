"""timbor.album.release — album assembly, artifacts, verification (§15–§29).

render_album() is an ASSEMBLY operation: it reads existing track masters,
applies the sequencing plan (integer-sample positions, transitions, fades),
applies the album loudness policy (non-destructive gains, explicit limiter)
and writes the album audio + release artifacts. It never regenerates music,
never calls the song pipeline, and never modifies a source master.

Determinism: every boundary is an integer sample count derived from the
sequence; the same album.json + same masters ⇒ byte-identical album.wav
(§16, §31).
"""
from __future__ import annotations

import hashlib
import json
import os

import numpy as np

from .mastering import (apply_gain, compute_gains, limit, peak_report,
                        policy_from)
from .loudness import read_wav_stereo, analyze_album, db
from .sequencing import SequenceConfig, build_sequence, validate_sequence
from .transitions import (boundary_diagnostics, click_status,
                          crossfade_curves, fade_envelopes)
from .dna import crc32_json

RELEASE_FORMAT = "timbor-release"
RELEASE_VERSION = 1

FLOAT_EQ = 1e-9


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_track_root(album_root: str, entry: dict) -> str:
    """Absolute project root of a sequence entry's source track."""
    return os.path.dirname(os.path.dirname(
        os.path.join(album_root, entry["master"])))


# ---------------------------------------------------------------------------
# album render (assembly only — §15, §16)
# ---------------------------------------------------------------------------

def render_album(album: dict, album_root: str,
                 cfg: SequenceConfig | None = None,
                 out_dir: str | None = None,
                 write_int16: bool = True) -> dict:
    """Assemble the album master from existing track masters.

    Reads only WAVs + album.json. Returns a result document with the render
    report; album.wav / album_int16.wav / sequence.json land in
    <album_root>/album/ unless out_dir overrides.
    """
    cfg = cfg or SequenceConfig.from_album(album)
    seq = build_sequence(album, album_root, cfg)
    problems = validate_sequence(seq, cfg)
    if problems:
        raise ValueError(f"invalid sequence: {problems}")

    policy = policy_from(album)
    gains = compute_gains(album, album_root, policy) \
        if policy["mode"] != "preserve" else None
    gain_by_id = {t["track_id"]: t for t in (gains or {}).get("tracks", [])}

    sr = seq["sample_rate"]
    total = int(seq["total_samples"])
    album_l = np.zeros(total)
    album_r = np.zeros(total)

    # source-master immutability: hash every master BEFORE reading
    master_hashes = {e["track_id"]: _md5(os.path.join(album_root,
                                                       e["master"]))
                     for e in seq["entries"]}

    from .transitions import CROSSFADE_TYPES
    for e in seq["entries"]:
        l, r, sr_chk = read_wav_stereo(os.path.join(album_root, e["master"]))
        if sr_chk != sr:
            raise ValueError(f"track {e['track_id']} sample rate {sr_chk} "
                             f"!= album {sr}")
        if len(l) != e["source_samples"]:
            raise ValueError(f"track {e['track_id']} length changed between "
                             f"sequence plan and render")
        ginfo = gain_by_id.get(e["track_id"])
        if ginfo and abs(ginfo["gain_db"]) > 1e-9:
            l, r = apply_gain(l, r, float(ginfo["gain_db"]))
        fi, fo = e["fade_in"], e["fade_out"]
        if fi > 0:
            ramp = np.linspace(0.0, 1.0, fi, endpoint=False)
            l[:fi] *= ramp
            r[:fi] *= ramp
        if fo > 0:
            ramp = np.linspace(1.0, 0.0, fo, endpoint=False)
            l[-fo:] *= ramp
            r[-fo:] *= ramp
        pos = e["start_sample"]
        end = pos + len(l)
        ov = e["overlap_samples"]
        if ov > 0:
            tr = next(t for t in seq["transitions"]
                      if t["destination_position"] == e["position"] - 1)
            fo_curve, fi_curve = crossfade_curves(ov, tr.get("curve",
                                                             "equal_power"))
            album_l[pos:pos + ov] = (album_l[pos:pos + ov] * fo_curve
                                     + l[:ov] * fi_curve)
            album_r[pos:pos + ov] = (album_r[pos:pos + ov] * fo_curve
                                     + r[:ov] * fi_curve)
            album_l[pos + ov:end] += l[ov:]
            album_r[pos + ov:end] += r[ov:]
        else:
            album_l[pos:end] += l
            album_r[pos:end] += r

    # explicit album limiter (default OFF) — assembled audio only
    limiter_applied = False
    if policy["limiter_enabled"]:
        album_l, album_r = limit(album_l, album_r, sr,
                                 ceiling_db=policy["limiter_ceiling_db"],
                                 release_seconds=policy[
                                     "limiter_release_seconds"],
                                 lookahead_seconds=policy[
                                     "limiter_lookahead_seconds"])
        limiter_applied = True

    peak = peak_report(album_l, album_r)

    out_dir = out_dir or os.path.join(album_root, "album")
    os.makedirs(out_dir, exist_ok=True)
    wav_path = os.path.join(out_dir, "album.wav")
    _write_wav_float32(wav_path, album_l, album_r, sr)
    written = {"album_wav": wav_path}
    if write_int16:
        p16 = os.path.join(out_dir, "album_int16.wav")
        _write_wav_int16(p16, album_l, album_r, sr)
        written["album_int16_wav"] = p16

    result = {
        "format": RELEASE_FORMAT,
        "version": RELEASE_VERSION,
        "mode": seq["mode"],
        "sample_rate": sr,
        "channels": 2,
        "bit_depths": [32, 16] if write_int16 else [32],
        "total_samples": total,
        "duration_seconds": seq["duration_seconds"],
        "peak": peak,
        "limiter_applied": limiter_applied,
        "loudness_mode": policy["mode"],
        "gains": gain_by_id and {k: v["gain_db"] for k, v in
                                 gain_by_id.items()},
        "master_hashes_before": master_hashes,
        "outputs": {k: os.path.relpath(v, album_root).replace(os.sep, "/")
                    for k, v in written.items()},
        "sequence": seq,
    }
    return result


def _write_wav_float32(path: str, l: np.ndarray, r: np.ndarray,
                       sr: int) -> None:
    """Chunked writer: byte-identical output to a whole-buffer write, but
    never allocates a full album-length interleaved copy."""
    import struct
    n = len(l)
    data_bytes = n * 2 * 4
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes,
                      b"WAVE", b"fmt ", 16, 3, 2, sr, sr * 8, 8, 32,
                      b"data", data_bytes)
    step = 1 << 18
    with open(path, "wb") as f:
        f.write(hdr)
        for s in range(0, n, step):
            e = min(n, s + step)
            inter = np.empty((e - s, 2), dtype="<f4")
            inter[:, 0] = l[s:e]
            inter[:, 1] = r[s:e]
            f.write(inter.tobytes())


def _write_wav_int16(path: str, l: np.ndarray, r: np.ndarray,
                     sr: int) -> None:
    import wave
    n = len(l)
    step = 1 << 18
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        for s in range(0, n, step):
            e = min(n, s + step)
            data = np.empty((e - s, 2))
            data[:, 0] = l[s:e]
            data[:, 1] = r[s:e]
            w.writeframes((np.clip(data, -1.0, 1.0) * 32767)
                          .astype("<i2").tobytes())


# ---------------------------------------------------------------------------
# cue sheet + tracklist (§18, §19)
# ---------------------------------------------------------------------------

def _cue_time(seconds: float) -> str:
    """CUE MM:SS:FF (75 fps) — deterministic integer frame rounding."""
    total_frames = int(round(seconds * 75.0))
    mm, rem = divmod(total_frames, 75 * 60)
    ss, ff = divmod(rem, 75)
    return f"{mm:02d}:{ss:02d}:{ff:02d}"


def build_cue(album: dict, seq: dict, album_filename: str = "album.wav") -> str:
    """Standard CUE sheet. Overlapping transitions are documented as the
    known limitation: cue INDEX points at each track's start position in the
    rendered sequence (the crossfade seam belongs to no single track)."""
    lines = [f'PERFORMER "TIMBOR"',
             f'TITLE "{album.get("name", "TIMBOR Album")}"',
             f'FILE "{album_filename}" WAVE']
    for e in seq["entries"]:
        lines.append(f"  TRACK {e['position']:02d} AUDIO")
        lines.append(f"    TITLE \"{e.get('title') or e['track_id']}\"")
        lines.append(f"    PERFORMER \"{e.get('genre') or 'TIMBOR'} / "
                     f"{e['bpm']:.0f} BPM\"")
        lines.append(f"    INDEX 01 {_cue_time(e['start_seconds'])}")
    return "\n".join(lines) + "\n"


def build_tracklist(album: dict, seq: dict) -> dict:
    """Machine-readable tracklist (spec §19 shape), deterministic."""
    return {
        "album": album.get("name"),
        "tracks": [{
            "position": e["position"],
            "title": e.get("title"),
            "start_seconds": e["start_seconds"],
            "duration_seconds": e["duration_seconds"],
            "gap_before": e["gap_before"],
            "bpm": e["bpm"],
            "genre": e.get("genre"),
        } for e in seq["entries"]],
    }


# ---------------------------------------------------------------------------
# release manifest + README (§20, §23)
# ---------------------------------------------------------------------------

def build_release_manifest(album: dict, album_root: str, seq: dict,
                           result: dict, gains: dict | None) -> dict:
    tracks = []
    for e in seq["entries"]:
        proj = os.path.join(album_root, e["project"] or "")
        tracks.append({
            "order": e["position"],
            "title": e.get("title"),
            "genre": e.get("genre"),
            "bpm": e["bpm"],
            "key": (album.get("dna") or {}).get("root_key", "") + " "
                   + (album.get("dna") or {}).get("mode", ""),
            "duration_seconds": e["duration_seconds"],
            "source_project": e.get("project"),
            "source_master_hash": result["master_hashes_before"].get(
                e["track_id"]),
            "album_gain_db": (gains or {}).get(e["track_id"], 0.0),
        })
    return {
        "format": RELEASE_FORMAT + "-manifest",
        "version": RELEASE_VERSION,
        "album": {"name": album.get("name"),
                  "master_seed": album.get("generator", {}).get("seed"),
                  "generator_version": album.get("generator", {}).get(
                      "version"),
                  "source_album_project": album.get("name"),
                  "dna_hash": album.get("dna_hash")},
        "sequence": {
            "mode": seq["mode"],
            "quantize": seq.get("quantize"),
            "duration_seconds": seq["duration_seconds"],
            "transitions": seq.get("transitions", []),
            "config_hash": seq.get("config_hash"),
        },
        "audio": {"sample_rate": seq["sample_rate"], "channels": 2,
                  "bit_depth": 32,
                  "album_master_hash": result["album_master_hash"]},
        "tracks": tracks,
        "provenance": {
            "generator_version": album.get("generator", {}).get("version"),
            "configuration_hash": seq.get("config_hash"),
            "loudness_mode": result.get("loudness_mode"),
            "limiter_applied": result.get("limiter_applied"),
        },
    }


def build_release_readme(album: dict, seq: dict, doc: dict) -> str:
    """Deterministic release README — no generated timestamp (§23)."""
    bpms = [e["bpm"] for e in seq["entries"]]
    dna = album.get("dna") or {}
    lines = [
        "TIMBOR Album Release", "=" * 20, "",
        f"Album: {album.get('name')}",
        f"Tracks: {len(seq['entries'])}",
        f"BPM range: {min(bpms):.0f}-{max(bpms):.0f}",
        f"Key neighborhood: {dna.get('root_key', '?')} "
        f"{dna.get('mode', '')} (±{dna.get('bpm_range', 0):.0f} BPM)",
        "", "Tracklist:",
    ]
    for e in seq["entries"]:
        lines.append(f"{e['position']:02d} {e.get('title') or e['track_id']} "
                     f"({e.get('genre')}, {e['bpm']:.0f} BPM, "
                     f"{e['duration_seconds']:.1f}s)")
    lines += ["", "Audio:",
              f"Sample rate: {seq['sample_rate']} Hz",
              "Channels: 2 (stereo)",
              "Master format: 32-bit float WAV (+ 16-bit PCM distribution "
              "copy)",
              f"Sequence mode: {seq['mode']}", "", "Source:",
              "TIMBOR project version: "
              f"{album.get('generator', {}).get('version', '?')}",
              f"Seed: {album.get('generator', {}).get('seed', '?')}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# album QC + boundary diagnostics (§24–§26)
# ---------------------------------------------------------------------------

def album_qc(album: dict, album_root: str, seq: dict, result: dict,
             policy: dict, gains: dict | None) -> dict:
    """Album-level QC: structure, audio, transitions, loudness,
    reproducibility. Returns the qc.json document (§24)."""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # structure
    try:
        problems = validate_sequence(seq)
        check("sequence_structure", not problems, "; ".join(problems))
    except Exception as e:  # noqa: BLE001
        check("sequence_structure", False, str(e))

    # audio: re-read what we wrote and validate
    wav_path = os.path.join(album_root, result["outputs"]["album_wav"])
    l, r, sr = read_wav_stereo(wav_path)
    check("sample_rate", sr == seq["sample_rate"], f"{sr}")
    check("channels", True, "2")
    finite = bool(np.isfinite(l).all() and np.isfinite(r).all())
    check("finite_samples", finite, "no NaN/Inf" if finite else "NaN/Inf!")
    pr = peak_report(l, r)
    if policy["limiter_enabled"]:
        ceiling = policy["limiter_ceiling_db"]
        check("no_clipping", pr["peak_dbfs"] <= ceiling + 0.05,
              f"peak {pr['peak_dbfs']} dBFS vs ceiling {ceiling} dBFS")
    else:
        check("no_clipping", pr["clipped_samples"] == 0,
              f"{pr['clipped_samples']} samples at or above 0 dBFS; "
              f"peak {pr['peak_dbfs']} dBFS")
    expected = seq["total_samples"]
    check("expected_duration", abs(len(l) - expected) <= 1,
          f"{len(l)} samples vs planned {expected}")

    # transitions: boundary diagnostics from the rendered audio. Mono is
    # precomputed once — (l+r)/2 is exactly what mean(axis=1) would give,
    # without a full album-length stereo stack.
    diags = boundary_diagnostics((l + r) * 0.5,
                                 seq.get("boundaries", []), sr)
    for d in diags:
        check(f"boundary_{d.get('from')}_{d.get('to')}",
              d["status"] != "ERROR",
              f"{d['status']} disc={d['discontinuity']:.4f} "
              f"mode={d.get('mode')}")

    # loudness bookkeeping
    check("loudness_policy_recorded", policy["mode"] in ("preserve", "match",
                                                          "target"),
          f"mode={policy['mode']} limiter={policy['limiter_enabled']}")
    if gains:
        check("gains_recorded", all("gain_db" in t for t in gains.get(
            "tracks", [])), f"{len(gains.get('tracks', []))} tracks")

    # reproducibility: source masters unchanged since the render
    for e in seq["entries"]:
        h = _md5(os.path.join(album_root, e["master"]))
        check(f"master_unchanged_{e['track_id']}",
              h == result["master_hashes_before"].get(e["track_id"]), h)

    errors = [c for c in checks if not c["ok"]]
    return {
        "format": "timbor-album-qc", "version": 1,
        "ok": not errors,
        "errors": len(errors), "checks": checks,
        "boundary_diagnostics": diags,
    }


# ---------------------------------------------------------------------------
# full artifact assembly, replay + verification (§27, §28, §31)
# ---------------------------------------------------------------------------

def assemble_release(album: dict, album_root: str,
                     cfg: SequenceConfig | None = None) -> dict:
    """One deterministic pass: sequence → render → loudness → artifacts.

    Writes into <album_root>/album/: album.wav, album_int16.wav,
    sequence.json, loudness.json, qc.json, album.cue, tracklist.json,
    release_manifest.json, README.md.
    """
    cfg = cfg or SequenceConfig.from_album(album)
    result = render_album(album, album_root, cfg)
    seq = result["sequence"]
    policy = policy_from(album)
    gains_doc = compute_gains(album, album_root, policy) \
        if policy["mode"] != "preserve" else None
    gains = ({t["track_id"]: t["gain_db"] for t in gains_doc["tracks"]}
             if gains_doc else None)

    out_dir = os.path.join(album_root, "album")
    wav_path = os.path.join(album_root, result["outputs"]["album_wav"])
    result["album_master_hash"] = _md5(wav_path)

    seq_path = save_json(os.path.join(out_dir, "sequence.json"), {
        k: v for k, v in seq.items()})
    loud = analyze_album(album, album_root, assembled_path=wav_path)
    loud_path = save_json(os.path.join(out_dir, "loudness.json"), loud)
    qc = album_qc(album, album_root, seq, result, policy, gains_doc)
    qc_path = save_json(os.path.join(out_dir, "qc.json"), qc)
    cue_path_text = build_cue(album, seq,
                              os.path.basename(wav_path))
    cue_path = os.path.join(out_dir, "album.cue")
    with open(cue_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(cue_path_text)
    tl_path = save_json(os.path.join(out_dir, "tracklist.json"),
                        build_tracklist(album, seq))
    man = build_release_manifest(album, album_root, seq, result, gains)
    man_path = save_json(os.path.join(out_dir, "release_manifest.json"), man)
    readme_path = os.path.join(out_dir, "README.md")
    with open(readme_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_release_readme(album, seq, loud))

    return {
        "result": result,
        "outputs": {
            "album_wav": wav_path,
            "album_int16_wav": os.path.join(album_root,
                                            result["outputs"].get(
                                                "album_int16_wav",
                                                "album/album_int16.wav")),
            "sequence": seq_path, "loudness": loud_path, "qc": qc_path,
            "cue": cue_path, "tracklist": tl_path, "manifest": man_path,
            "readme": readme_path,
        },
        "qc_ok": qc["ok"],
        "album_master_hash": result["album_master_hash"],
    }


def save_json(path: str, doc: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    return path


def verify_album(album: dict, album_root: str,
                 cfg: SequenceConfig | None = None) -> dict:
    """§28: freshly assemble and compare against the stored album.wav.
    Same tiered philosophy as phase 4 — differences are never forgiven."""
    cfg = cfg or SequenceConfig.from_album(album)
    stored_path = os.path.join(album_root, "album", "album.wav")
    if not os.path.isfile(stored_path):
        raise FileNotFoundError(f"no stored album master: {stored_path}")
    fresh = render_album(album, album_root, cfg, out_dir=os.path.join(
        album_root, "album_verify_tmp"), write_int16=False)
    fresh_path = os.path.join(album_root, "album_verify_tmp", "album.wav")

    stored_hash = _md5(stored_path)
    fresh_hash = _md5(fresh_path)
    l_s, r_s, _ = read_wav_stereo(stored_path)
    l_f, r_f, _ = read_wav_stereo(fresh_path)
    n = min(len(l_s), len(l_f))
    if n == 0 or len(l_s) != len(l_f):
        status, maxdiff = "DIFFERENT", float("inf")
    else:
        # chunked comparison: album-length float64 stacks exhaust RAM
        status = "IDENTICAL"
        maxdiff = 0.0
        chunk = 1 << 20
        for s0 in range(0, n, chunk):
            e0 = min(n, s0 + chunk)
            qs = np.stack([l_s[s0:e0], r_s[s0:e0]]).astype(np.float32) \
                .astype(np.float64)
            qf = np.stack([l_f[s0:e0], r_f[s0:e0]]).astype(np.float32) \
                .astype(np.float64)
            if not np.array_equal(qs, qf):
                d = float(np.max(np.abs(qs - qf)))
                maxdiff = max(maxdiff, d)
                if d > FLOAT_EQ:
                    status = "DIFFERENT"
                elif status != "DIFFERENT":
                    status = "FLOAT-EQUIVALENT"
        if status == "DIFFERENT":
            maxdiff = max(maxdiff, 0.0)
    try:
        os.remove(fresh_path)
        os.rmdir(os.path.dirname(fresh_path))
    except OSError:
        pass
    return {
        "stored_hash": stored_hash, "fresh_hash": fresh_hash,
        "status": status, "max_abs_diff": maxdiff,
        "samples_compared": n,
    }


def verify_master_immutability(album: dict, album_root: str,
                               before_hashes: dict) -> dict:
    """§29: mandatory source-master immutability check."""
    now = {t["number"]: _md5(os.path.join(
        album_root, t["directory"], "audio", "master.wav"))
        for t in album["tracks"]}
    changed = [k for k in now if before_hashes.get(k) != now[k]]
    return {"unchanged": not changed, "changed": changed,
            "hashes": now}


# ---------------------------------------------------------------------------
# release packaging (§22)
# ---------------------------------------------------------------------------

def package_release(album: dict, album_root: str,
                    include_track_masters: bool = False) -> dict:
    """Build release/ from the assembled album/ artifacts.

    Copies album.wav, album_int16.wav, tracklist.json, album.cue,
    README.md and the release manifest (as manifest.json). Never touches
    sample libraries; track masters only with include_track_masters=True.
    Verifies copied WAV hashes match the assembled album master.
    """
    album_dir = os.path.join(album_root, "album")
    required = ["album.wav", "album_int16.wav", "tracklist.json",
                "album.cue", "release_manifest.json", "README.md"]
    missing = [f for f in required if not os.path.isfile(
        os.path.join(album_dir, f))]
    if missing:
        raise FileNotFoundError(
            f"album artifacts missing (run --render-album first): {missing}")

    release_dir = os.path.join(album_root, "release")
    os.makedirs(release_dir, exist_ok=True)
    import shutil

    def cp(src_name, dst_name):
        shutil.copy2(os.path.join(album_dir, src_name),
                     os.path.join(release_dir, dst_name))

    cp("album.wav", "album.wav")
    cp("album_int16.wav", "album_int16.wav")
    cp("tracklist.json", "tracklist.json")
    cp("album.cue", "album.cue")
    cp("release_manifest.json", "manifest.json")
    cp("README.md", "README.md")

    tracks_copied = []
    if include_track_masters:
        tdir = os.path.join(release_dir, "tracks")
        os.makedirs(tdir, exist_ok=True)
        for t in album["tracks"]:
            src = os.path.join(album_root, t["directory"], "audio",
                               "master.wav")
            dst = os.path.join(tdir, f"{t['number']}_"
                                     f"{(t.get('title') or t['number'])}"
                                     ".wav")
            shutil.copy2(src, dst)
            tracks_copied.append(os.path.relpath(dst, release_dir))

    # verify the packaged album master is the assembled one
    h_pack = _md5(os.path.join(release_dir, "album.wav"))
    h_asm = _md5(os.path.join(album_dir, "album.wav"))
    if h_pack != h_asm:
        raise ValueError("packaged album.wav does not match the assembled "
                         "album master")
    return {"release_dir": release_dir, "files": sorted(os.listdir(
        release_dir)), "album_master_hash": h_pack,
        "track_masters": tracks_copied}
