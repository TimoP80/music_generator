"""TIMBOR Album Studio — local web interface server.

Serves the studio frontend (``studio/www``) and a JSON API over the
existing TIMBOR album engine (phase 5/6). The Python engine stays
authoritative: this server only reads project documents, streams audio,
extracts waveform peaks, writes sequencing/loudness *configuration* into
album.json, and runs ``generate.py`` operations as background jobs.

stdlib only (http.server; no web frameworks, no scipy).

Run from the project root:

    python studio/server.py [--album albums/timbor-machine-rave-ep] [--port 8765]
    python studio/server.py --list-albums
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# memory-constrained hosts: single-threaded BLAS keeps allocations bounded
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np  # noqa: E402  (engine dependency, already required)

from timbor.album.serialization import load_album, save_album  # noqa: E402
from timbor.album.sequencing import SequenceConfig, wav_info  # noqa: E402
from timbor.album import release as release_mod  # noqa: E402
from studio.sample_library import SampleLibraryService  # noqa: E402

WWW_DIR = os.path.join(ROOT, "studio", "www")
BUILD = "studio-1.3"

# ---------------------------------------------------------------------------
# document cache (mtime-keyed) + derived-state caches
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached_json(path: str):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    with _cache_lock:
        if len(_cache) > 24:
            _cache.pop(next(iter(_cache)))
        _cache[path] = (mtime, doc)
    return doc


# slim per-project extracts: project.json documents are large (tens of
# thousands of lines); caching them whole exhausts memory. Parse once,
# keep only what the studio screens need, drop the rest.
_slim_cache: dict[str, tuple[float, dict]] = {}
_slim_lock = threading.Lock()


def slim_project(rel_path: str) -> dict | None:
    path = rel_path if os.path.isabs(rel_path) else \
        os.path.join(ALBUM_DIR, *rel_path.split("/"))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _slim_lock:
        hit = _slim_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            p = json.load(f)
    except (OSError, ValueError):
        return None
    events = [
        {"id": e.get("id"), "type": e.get("type"), "bus": e.get("bus"),
         "role": e.get("role"), "section": e.get("section"),
         "selection_why": e.get("selection_why"), "selection_score": e.get("selection_score"),
         "start_beat": e.get("start_beat"),
         "duration_beats": e.get("duration_beats"),
         "velocity": e.get("velocity"), "instrument": e.get("instrument"),
         "pitch": e.get("pitch"), "sample_id": e.get("sample_id"),
         "gain_db": e.get("gain_db"),
         "source_path": (e.get("source_path") or "").replace("\\", "/"),
         "pitch_semitones": e.get("pitch_semitones"),
         "stretch_ratio": e.get("stretch_ratio"),
         "chop_ops": e.get("chop_ops"), "metadata": e.get("metadata"),
         "reverse": e.get("reverse")}
        for e in (p.get("timeline") or [])]
    slim = {
        "song": p.get("song") or {},
        "plan": p.get("plan") or {},
        "sections": p.get("sections") or [],
        "samples": p.get("samples") or [],
        "stems": [s.replace("\\", "/") for s in (p.get("stems") or [])],
        "qc_notes": (p.get("qc") or {}).get("notes", []),
        "events": events,
        "sample_events": [e for e in events if e.get("sample_id")],
        "event_count": len(events),
    }
    del p
    with _slim_lock:
        if len(_slim_cache) > 8:
            _slim_cache.pop(next(iter(_slim_cache)))
        _slim_cache[path] = (mtime, slim)
    return slim


_album_lock = threading.Lock()


def read_album() -> dict:
    with _album_lock:
        return load_album(os.path.join(ALBUM_DIR, "album.json"))


def write_album(album: dict) -> None:
    with _album_lock:
        save_album(os.path.join(ALBUM_DIR, "album.json"), album)
    invalidate("album")


def invalidate(what: str | None = None) -> None:
    with _cache_lock:
        if what is None:
            _cache.clear()
        else:
            # album.json (or any doc) changed: its stale cache entry is
            # dropped per-album, never across every album on disk
            _cache.pop(os.path.join(ALBUM_DIR, "album.json"), None)
        _peaks_cache.clear()


# ---------------------------------------------------------------------------
# audio: WAV header parsing, streaming, peak extraction
# ---------------------------------------------------------------------------

def parse_wav(path: str) -> dict:
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"not a RIFF/WAVE file: {path}")
        fmt = None
        data_off = data_size = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, csz = struct.unpack("<4sI", hdr)
            if cid == b"fmt ":
                fmt = struct.unpack("<HHIIHH", f.read(csz))
            elif cid == b"data":
                data_off = f.tell()
                data_size = csz
                break
            else:
                f.seek(csz + (csz & 1), 1)
    if fmt is None or data_off is None:
        raise ValueError(f"missing fmt/data chunk: {path}")
    tag, channels, rate, _brate, block, bits = fmt
    return {"tag": tag, "channels": channels, "rate": rate, "block": block,
            "bits": bits, "data_off": data_off, "data_size": data_size,
            "frames": data_size // block}


def read_wav_mono(path: str, max_frames: int = 12_000_000) -> tuple[np.ndarray, int]:
    """Decode a WAV to float32 mono (channel-mean) at native gain."""
    info = parse_wav(path)
    frames, ch, bits, tag = info["frames"], info["channels"], info["bits"], info["tag"]
    if frames > max_frames:
        raise ValueError(f"wav too large for peak extraction: {frames} frames")
    with open(path, "rb") as f:
        f.seek(info["data_off"])
        raw = f.read(info["data_size"])
    if tag == 3 and bits == 32:
        x = np.frombuffer(raw, dtype="<f4")
    elif tag == 3 and bits == 64:
        x = np.frombuffer(raw, dtype="<f8").astype(np.float32)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v = np.where(v & 0x800000, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif tag == 1 and bits == 32:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise ValueError(f"unsupported wav format tag={tag} bits={bits}")
    x = x[: frames * ch].reshape(frames, ch)
    mono = x.mean(axis=1) if ch > 1 else x[:, 0]
    return np.ascontiguousarray(mono, dtype=np.float32), info["rate"]


_peaks_cache: dict[tuple, object] = {}
_peaks_lock = threading.Lock()


def extract_peaks(path: str, bins: int) -> dict:
    bins = max(64, min(int(bins), 4000))
    mtime = os.path.getmtime(path)
    key = (os.path.abspath(path), mtime, bins)
    with _peaks_lock:
        hit = _peaks_cache.get(key)
        if hit:
            return hit
    mono, rate = read_wav_mono(path)
    n = mono.shape[0]
    base = n // bins
    rem = n - base * bins
    counts = np.full(bins, base, dtype=np.int64)
    counts[:rem] += 1
    idx = np.concatenate(([0], np.cumsum(counts)[:-1]))
    mins = np.minimum.reduceat(mono, idx)
    maxs = np.maximum.reduceat(mono, idx)
    sums = np.add.reduceat(mono.astype(np.float64), idx)
    sumsq = np.add.reduceat((mono.astype(np.float64)) ** 2, idx)
    means = sums / counts
    rms = np.sqrt(np.maximum(sumsq / counts - means ** 2, 0.0))
    out = {
        "bins": bins,
        "rate": rate,
        "frames": int(n),
        "duration_seconds": round(n / rate, 4),
        "min": [round(float(v), 5) for v in mins],
        "max": [round(float(v), 5) for v in maxs],
        "rms": [round(float(v), 5) for v in rms],
    }
    with _peaks_lock:
        if len(_peaks_cache) > 12:
            _peaks_cache.pop(next(iter(_peaks_cache)))
        _peaks_cache[key] = out
    return out


# ---------------------------------------------------------------------------
# studio state: paths, derived health
# ---------------------------------------------------------------------------
def list_album_dirs() -> list[str]:
    """All album directories under albums/ (each must contain album.json)."""
    albums_root = os.path.join(ROOT, "albums")
    if not os.path.isdir(albums_root):
        return []
    found = []
    for name in sorted(os.listdir(albums_root)):
        d = os.path.join(albums_root, name)
        if os.path.isfile(os.path.join(d, "album.json")):
            found.append(d)
    return found


def detect_album_dir() -> str:
    found = list_album_dirs()
    if not found:
        raise SystemExit("no album directory with album.json found under albums/")
    if len(found) > 1:
        print("note: multiple albums detected — picking the first "
              "(use --album to choose; the UI picker can switch at runtime):")
        for d in found:
            mark = " *" if d == found[0] else ""
            print(f"  {os.path.relpath(d, ROOT)}{mark}")
    return found[0]

ALBUM_DIR = detect_album_dir()
ALBUM_REL = os.path.relpath(ALBUM_DIR, ROOT).replace(os.sep, "/")
sample_library = SampleLibraryService(ROOT)
sample_library.peaks_fn = extract_peaks

MD5_CACHE: dict[str, tuple[float, int, str]] = {}


def file_md5(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    hit = MD5_CACHE.get(path)
    if hit and hit[0] == key[0] and hit[1] == key[1]:
        return hit[2]
    h = release_mod._md5(path)
    MD5_CACHE[path] = (key[0], key[1], h)
    return h


def artifact_paths() -> dict[str, str]:
    a = os.path.join(ALBUM_DIR, "album")
    return {
        "album_wav": os.path.join(a, "album.wav"),
        "album_int16": os.path.join(a, "album_int16.wav"),
        "sequence": os.path.join(a, "sequence.json"),
        "loudness": os.path.join(a, "loudness.json"),
        "qc": os.path.join(a, "qc.json"),
        "cue": os.path.join(a, "album.cue"),
        "tracklist": os.path.join(a, "tracklist.json"),
        "release_manifest": os.path.join(a, "release_manifest.json"),
        "release_readme": os.path.join(a, "README.md"),
        "album_manifest": os.path.join(ALBUM_DIR, "release_manifest.json") ,
    }


def release_tree() -> dict:
    def walk(dirpath: str, prefix: str) -> list:
        rows = []
        if not os.path.isdir(dirpath):
            return rows
        for name in sorted(os.listdir(dirpath)):
            p = os.path.join(dirpath, name)
            if os.path.isfile(p):
                rows.append({"path": f"{prefix}{name}",
                             "bytes": os.path.getsize(p)})
            elif os.path.isdir(p):
                rows.append({"path": f"{prefix}{name}/", "bytes": None,
                             "children": walk(p, f"{prefix}{name}/")})
        return rows

    album_dir = os.path.join(ALBUM_DIR, "album")
    rel_dir = os.path.join(ALBUM_DIR, "release")
    return {"album": walk(album_dir, "album/"),
            "release": walk(rel_dir, "release/") if os.path.isdir(rel_dir) else []}


def albums_listing() -> dict:
    """Every album on disk; read-only summary, engine-accurate or defaults.

    The active album (per ALBUM_DIR) is flagged so the UI picker can mark
    it; job submissions always target the active album directory.
    """
    albums = []
    for d in list_album_dirs():
        doc = _cached_json(os.path.join(d, "album.json")) or {}
        gen = doc.get("generator") or {}
        cfg = doc.get("config") or {}
        seq = doc.get("sequencing") or {}
        art = os.path.join(d, "album", "album.wav")
        rel = os.path.relpath(d, ROOT).replace(os.sep, "/")
        albums.append({
            "dir": rel,
            "name": doc.get("name") or os.path.basename(d),
            "active": os.path.abspath(d) == os.path.abspath(ALBUM_DIR),
            "format": doc.get("format") or "",
            "version": doc.get("version") or "",
            "seed": gen.get("seed"),
            "generated_at": gen.get("generated_at"),
            "track_count": len(doc.get("tracks") or []),
            "bpm_center": cfg.get("bpm_center"),
            "sequence_mode": seq.get("mode") or "gap",
            "duration_seconds": round(wav_info(art)[0] / wav_info(art)[1], 3)
                                if os.path.isfile(art) else None,
            "master_rendered": os.path.isfile(art),
        })
    return {"albums": albums, "active": ALBUM_REL}


def switch_album(rel: str | None) -> dict:
    """Point the studio at another album directory at runtime.

    All state is read through module globals, so this is the only
    mutation needed — per-request readers pick up the new album on
    their next call. Derived caches are cleared so nothing leaks
    across albums; engine jobs then target the newly active album.
    """
    global ALBUM_DIR, ALBUM_REL
    if not rel:
        raise ValueError("missing dir")
    rel_norm = str(rel).replace("\\", "/").strip("/")
    full = os.path.abspath(os.path.join(ROOT, rel_norm))
    root_abs = os.path.abspath(ROOT)
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise ValueError("path outside project root")
    if not os.path.isfile(os.path.join(full, "album.json")):
        raise ValueError(f"no album.json under {rel}")
    if os.path.abspath(full) == os.path.abspath(ALBUM_DIR):
        return {"switched": False, "album_dir": ALBUM_REL, "health": health()}
    ALBUM_DIR = full
    ALBUM_REL = os.path.relpath(full, ROOT).replace(os.sep, "/")
    _cache.clear()
    _slim_cache.clear()
    _peaks_cache.clear()
    MD5_CACHE.clear()
    job_runner.log(f"active album switched → {ALBUM_REL}", src="studio")
    return {"switched": True, "album_dir": ALBUM_REL, "health": health()}


def health() -> dict:
    album = read_album()
    art = artifact_paths()
    artifacts = {k: os.path.isfile(p) for k, p in art.items()}
    job = job_runner.current()
    running = job["kind"] if job and job["status"] == "running" else None

    errors = [v for v in album.get("validation", [])
              if v.get("severity") == "error"]
    qc = _cached_json(art["qc"]) if artifacts["qc"] else None

    cfg = None
    try:
        cfg = SequenceConfig.from_album(album)
    except ValueError as exc:
        return {"album_dir": ALBUM_REL, "state": "ERROR",
                "message": f"invalid sequencing configuration: {exc}",
                "artifacts": artifacts, "errors": len(errors),
                "warnings": len([v for v in album.get("validation", [])
                                 if v.get("severity") == "warning"]),
                "sequence_config_hash": None,
                "stored_sequence_hash": None,
                "album_master_hash": None, "release_manifest_hash": None,
                "format": None, "job": job}
    seq = _cached_json(art["sequence"]) if artifacts["sequence"] else None
    stale_sequence = bool(seq) and seq.get("config_hash") != cfg.config_hash()
    palette_state = album.get("palette") or {}
    # Studio palette edits change future generation inputs, not rendered tracks.
    # Keep that distinction explicit until the album generator rewrites projects.
    palette_pending = bool(palette_state.get("studio_user_modified"))

    manifest = _cached_json(art["release_manifest"]) \
        if artifacts["release_manifest"] else None
    wav_hash = file_md5(art["album_wav"]) if artifacts["album_wav"] else None
    manifest_hash = ((manifest.get("audio") or {}).get("album_master_hash")
                     if manifest else None)
    master_differs = bool(wav_hash and manifest_hash
                          and wav_hash != manifest_hash)

    state, message = "READY", "Album matches project documents."
    if running in ("render", "resequence"):
        state, message = "RENDERING", f"{running} job running."
    elif running == "verify":
        state, message = "VERIFYING", "Album verification running."
    elif errors:
        state = "ERROR"
        message = errors[0].get("message", "album validation error")
    elif qc is not None and not qc.get("ok", False):
        state = "ERROR"
        message = "album QC reported failures"
    elif stale_sequence:
        state = "DIRTY"
        message = "Sequencing configuration changed. Re-sequence and render."
    elif palette_pending:
        state = "DIRTY"
        message = "Album palette changed in Studio. Existing tracks were not regenerated; regenerate the album to apply it."
    elif master_differs:
        state = "DIRTY"
        message = "Album master differs from release manifest. Render required."
    elif not artifacts["album_wav"]:
        message = "Configuration valid — album master not rendered yet."
    elif wav_hash:
        state = "VERIFIED"
        message = f"Album master matches release manifest ({wav_hash[:8]}…)."

    fmt_info = None
    masters = [os.path.join(ALBUM_DIR, t["directory"], "audio", "master.wav")
               for t in album.get("tracks", [])]
    if masters and os.path.isfile(masters[0]):
        try:
            frames, rate, ch, bits = wav_info(masters[0])
            fmt_info = {"sample_rate": rate, "channels": ch, "bits": bits,
                        "frames": frames}
        except (OSError, ValueError):
            pass

    return {
        "build": BUILD,
        "album_dir": ALBUM_REL,
        "state": state,
        "message": message,
        "artifacts": artifacts,
        "errors": len(errors),
        "warnings": len([v for v in album.get("validation", [])
                         if v.get("severity") == "warning"]),
        "sequence_config_hash": cfg.config_hash(),
        "stored_sequence_hash": (seq or {}).get("config_hash"),
        "album_master_hash": wav_hash,
        "release_manifest_hash": manifest_hash,
        "format": fmt_info,
        "job": job,
    }


# ---------------------------------------------------------------------------
# derived API documents (real data only — no invented values)
# ---------------------------------------------------------------------------

def tracks_doc() -> dict:
    album = read_album()
    art = artifact_paths()
    loud = (_cached_json(art["loudness"]) or {}) if artifacts_ok(art, "loudness") else {}
    loud_by_id = {t.get("track_id", t.get("position")): t
                  for t in loud.get("tracks", [])}
    qc = (_cached_json(art["qc"]) or {}) if artifacts_ok(art, "qc") else {}
    rows = []
    for i, t in enumerate(album.get("tracks", [])):
        master_rel = f"{t['directory']}/audio/master.wav"
        master_abs = os.path.join(ALBUM_DIR, *master_rel.split("/"))
        project = slim_project(t["project"])
        stems = ([f"{t['directory']}/{s}" for s in project["stems"]]
                 if project else [])
        lin = t.get("lineage", {})
        row = {
            "track_id": t["number"],
            "position": i + 1,
            "title": t.get("title"),
            "genre": t.get("genre"),
            "bpm": t.get("bpm"),
            "key": t.get("key"),
            "bars": t.get("bars"),
            "duration_seconds": t.get("duration_seconds"),
            "samples_used": t.get("samples_used"),
            "seed": t.get("seed"),
            "directory": t.get("directory"),
            "project": t.get("project"),
            "exports": t.get("exports", {}),
            "lineage": {"variant": lin.get("variant"),
                        "parent": lin.get("parent"),
                        "identity_score": lin.get("identity_score"),
                        "transformations": lin.get("transformations", []),
                        "fingerprint": lin.get("fingerprint")},
            "master_exists": os.path.isfile(master_abs),
            "master_rel": master_rel,
            "stems": stems,
            "loudness": loud_by_id.get(t["number"],
                                       loud_by_id.get(i + 1)),
        }
        rows.append(row)
    return {"tracks": rows}


def artifacts_ok(art: dict, key: str) -> bool:
    return os.path.isfile(art.get(key, ""))


def track_doc(track_id: str) -> dict:
    album = read_album()
    t = next((x for x in album.get("tracks", []) if x["number"] == track_id),
             None)
    if not t:
        raise KeyError(track_id)
    project = slim_project(t["project"])
    if not project:
        raise FileNotFoundError(t["project"])
    song = project["song"]
    bpm = song.get("bpm") or t.get("bpm")
    spb = 60.0 / bpm if bpm else None
    sections = []
    for s in project["sections"]:
        row = dict(s)
        if spb:
            row["start_seconds"] = round(s["start_beat"] * spb, 3)
            row["end_seconds"] = round(s["end_beat"] * spb, 3)
        sections.append(row)
    usage = {}
    for e in project["sample_events"]:
        usage[e["sample_id"]] = usage.get(e["sample_id"], 0) + 1
    return {
        "track": tracks_doc_entry(album, t),
        "song": song,
        "plan": project["plan"],
        "sections": sections,
        "seconds_per_beat": spb,
        "samples": project["samples"],
        "sample_events": project["sample_events"],
        "sample_usage": usage,
        "qc_notes": project["qc_notes"],
        "event_count": project["event_count"],
    }


def tracks_doc_entry(album: dict, t: dict) -> dict:
    return {"track_id": t["number"], "title": t.get("title"),
            "genre": t.get("genre"), "bpm": t.get("bpm"), "key": t.get("key"),
            "bars": t.get("bars"), "directory": t.get("directory"),
            "project": t.get("project"), "seed": t.get("seed"),
            "duration_seconds": t.get("duration_seconds"),
            "exports": t.get("exports", {}),
            "lineage": t.get("lineage", {})}


def motifs_doc() -> dict:
    album = read_album()
    dna = album.get("dna", {})
    nodes = []
    for t in album.get("tracks", []):
        lin = t.get("lineage", {})
        nodes.append({"track_id": t["number"], "title": t.get("title"),
                      "variant": lin.get("variant"), "parent": lin.get("parent"),
                      "motif": lin.get("motif"),
                      "identity_score": lin.get("identity_score"),
                      "transformations": lin.get("transformations", []),
                      "fingerprint": lin.get("fingerprint")})
    variants = {}
    for t in album.get("tracks", []):
        project = slim_project(t["project"])
        if project:
            variants[t["number"]] = {
                "song_motif": project["song"].get("motif"),
                "plan_variants": project["plan"].get("variants", {}),
            }
    return {"root_key": dna.get("root_key"), "mode": dna.get("mode"),
            "motif_family": dna.get("motif_family"),
            "motif_contour": dna.get("motif_contour"),
            "nodes": nodes, "plan_variants": variants}


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MINOR_STEPS = [0, 2, 3, 5, 7, 8, 10]
MAJOR_STEPS = [0, 2, 4, 5, 7, 9, 11]


def motif_note_names(motif: list, root_key: str, mode: str) -> list:
    steps = MINOR_STEPS if (mode or "minor") == "minor" else MAJOR_STEPS
    base = NOTE_NAMES.index(root_key) if root_key in NOTE_NAMES else 2
    out = []
    for d in motif:
        if d is None:
            out.append(None)
            continue
        octv, deg = divmod(int(d), 7)
        semi = base + steps[deg] + 12 * octv
        out.append(f"{NOTE_NAMES[semi % 12]}{4 + semi // 12}")
    return out


def palette_doc() -> dict:
    album = read_album()
    pal = album.get("palette", {})
    entries = []
    for role, s in (pal.get("roles") or {}).items():
        detail = sample_library.lookup_path(s.get("path", "")) or {}
        entries.append({**detail, **s, "role": role,
                        "library": detail.get("library", ""),
                        "sample_id": detail.get("id")})
    # usage keyed by canonical sample path (never basename; libraries may
    # contain different files sharing a name). Resolve project refs + events.
    usage: dict[str, dict] = {}
    for t in album.get("tracks", []):
        project = slim_project(t["project"])
        if not project:
            continue
        by_id = {}
        for sample in project["samples"]:
            p = sample.get("path") or ""
            full = os.path.realpath(p if os.path.isabs(p) else os.path.join(ROOT, p))
            by_id[sample.get("id")] = full
        for event in project["sample_events"]:
            path = by_id.get(event.get("sample_id"))
            if not path:
                continue
            key = os.path.normcase(path)
            row = usage.setdefault(key, {"path": path,
                "filename": os.path.basename(path), "by_track": {}})
            row["by_track"][t["number"]] = row["by_track"].get(t["number"], 0) + 1
    for sample in samples_of_album(album):
        p = sample.get("path") or ""
        full = os.path.realpath(p if os.path.isabs(p) else os.path.join(ROOT, p))
        usage.setdefault(os.path.normcase(full), {"path": full,
            "filename": sample["filename"], "by_track": {}})
    for entry in entries:
        p = entry.get("path") or ""
        full = os.path.realpath(p if os.path.isabs(p) else os.path.join(ROOT, p))
        row = usage.get(os.path.normcase(full),
                        {"path": full, "filename": entry["filename"], "by_track": {}})
        detail = sample_library.lookup_path(full)
        row = {**row, "sample_id": (detail or {}).get("id")}
        entry["usage"] = row
    for row in usage.values():
        row["tracks_count"] = len(row["by_track"])
        row["uses"] = sum(row["by_track"].values())
    listed = sorted(usage.values(),
                    key=lambda r: (-r["uses"], r["path"].casefold(), r["path"]))
    role_counts = {}
    for entry in entries:
        for role in entry.get("roles") or [entry.get("role")]:
            if role:
                role_counts[role] = role_counts.get(role, 0) + 1
    return {"mode": pal.get("mode"), "entries": entries,
            "usage": listed,
            "total_samples": len(entries),
            "role_counts": role_counts,
            "shared_count": sum(1 for r in listed if r["tracks_count"] > 1)}


def samples_of_album(album: dict) -> list:
    seen, out = set(), []
    for t in album.get("tracks", []):
        project = slim_project(t["project"])
        for s in (project["samples"] if project else []):
            if s.get("filename") and s["filename"] not in seen:
                seen.add(s["filename"])
                out.append({k: s.get(k) for k in
                            ("id", "filename", "path", "category", "bpm",
                             "key")})
    return out


def sample_usage_doc(sample_id: str) -> dict:
    sample = sample_library.detail(sample_id)
    sample_path = sample["path"]
    album = read_album()
    by_track = []
    for track in album.get("tracks", []):
        project = slim_project(track["project"])
        if not project:
            continue
        paths_by_id = {}
        for ref in project.get("samples", []):
            p = ref.get("path", "")
            full = os.path.realpath(p if os.path.isabs(p) else os.path.join(ROOT, p))
            paths_by_id[ref.get("id")] = full
        events = [e for e in project.get("events", [])
                  if e.get("sample_id") in paths_by_id
                  and os.path.normcase(paths_by_id[e["sample_id"]]) ==
                      os.path.normcase(os.path.realpath(sample_path))]
        if not events:
            continue
        transforms = []
        roles = set()
        selection_records = []
        for e in events:
            ops = e.get("chop_ops") or {}
            labels = []
            if any(float(v or 0) > 0 for v in ops.values()): labels.append("CHOP")
            if e.get("reverse"): labels.append("REVERSE")
            ratio = float(e.get("stretch_ratio") or 1.0)
            if abs(ratio - 1.0) > 0.005: labels.append("TIME-STRETCH")
            pitch = float(e.get("pitch_semitones") or 0.0)
            if abs(pitch) > 0.001: labels.append("PITCH")
            if e.get("metadata", {}).get("rep_mode") is not None: labels.append("STUTTER")
            if e.get("role"):
                roles.add(e["role"])
            if e.get("selection_why") is not None or e.get("selection_score") is not None:
                selection_records.append({"role": e.get("role"),
                    "score": e.get("selection_score"),
                    "why": e.get("selection_why")})
            transforms.append({"labels": labels or ["ORIGINAL"],
                "stretch_ratio": ratio, "pitch_semitones": pitch,
                "reverse": bool(e.get("reverse")), "chop_ops": ops,
                "role": e.get("role"), "section": e.get("section"),
                "start_beat": e.get("start_beat"),
                "duration_beats": e.get("duration_beats")})
        by_track.append({"track_id": track.get("number"),
            "title": track.get("title"), "genre": track.get("genre"),
            "bpm": track.get("bpm"), "placements": len(events),
            "roles": sorted(roles), "selection": selection_records or None,
            "transformations": transforms})
    palette_reasons = []
    for role, entry in (album.get("palette") or {}).get("roles", {}).items():
        if os.path.normcase(os.path.realpath(entry.get("path", ""))) == \
                os.path.normcase(os.path.realpath(sample_path)):
            palette_reasons.append({"role": role,
                "selection_score": entry.get("selection_score"),
                "scored_for_genre": entry.get("scored_for_genre"),
                "palette_mode": (album.get("palette") or {}).get("mode")})
    return {"sample": sample, "tracks": by_track,
        "total_placements": sum(t["placements"] for t in by_track),
        "selection": palette_reasons or None,
        "selector_note": ("TIMBOR deterministic album palette; stored score and genre shown"
                          if palette_reasons else
                          "Per-placement selector scores are not persisted in project provenance")}


def add_palette_sample(body: dict) -> dict:
    from timbor.album.palette import PALETTE_ROLES, palette_roles_for
    from timbor.samples.selector import ROLE_SPECS
    sid, role = body.get("sample_id"), body.get("role")
    if not sid or not role:
        raise ValueError("sample_id and role are required")
    sample = sample_library.detail(str(sid))
    album = read_album()
    mode = (album.get("palette") or {}).get("mode") or \
        (album.get("config") or {}).get("palette_mode") or "balanced"
    allowed = set(palette_roles_for(mode)) & set(PALETTE_ROLES["strict"])
    if role not in allowed or role not in ROLE_SPECS:
        raise ValueError(f"role {role!r} is not available in {mode} palette mode")
    if sample.get("category") not in ROLE_SPECS[role]["categories"]:
        raise ValueError(f"{sample.get('category')} samples cannot fill role {role}")
    palette_state = album.setdefault("palette", {})
    roles = palette_state.setdefault("roles", {})
    if "studio_original_roles" not in palette_state:
        palette_state["studio_original_roles"] = copy.deepcopy(roles)
    sample_path = os.path.normcase(os.path.realpath(sample["path"]))
    # A repeated assignment is a no-op; do not dirty album configuration.
    for old_key, old_entry in list(roles.items()):
        if old_entry.get("path") and os.path.normcase(os.path.realpath(old_entry["path"])) == sample_path \
                and role in old_entry.get("roles", []):
            if role not in roles:
                roles[role] = old_entry
            return {"added": False, "mode": mode, "role": role,
                    "sample": sample, "palette": palette_doc()}
    # The engine stores one active sample per role. Remove a replaced role
    # from prior merged entries so the selector cannot silently keep the old one.
    for old_key, old_entry in list(roles.items()):
        if role in old_entry.get("roles", []) or old_key == role:
            remaining = [r for r in old_entry.get("roles", []) if r != role]
            if remaining:
                old_entry["roles"] = remaining
            else:
                roles.pop(old_key, None)
    for existing in roles.values():
        if existing.get("path") and os.path.normcase(os.path.realpath(existing["path"])) == sample_path:
            assigned_roles = list(existing.get("roles", []))
            if role in assigned_roles:
                return {"added": False, "mode": mode, "role": role,
                        "sample": sample, "palette": palette_doc()}
            existing["roles"] = sorted(set(assigned_roles) | {role})
            palette_state["studio_user_modified"] = True
            write_album(album)
            return {"added": True, "mode": mode, "role": role,
                    "sample": sample, "palette": palette_doc()}
    from timbor.album.dna import crc32_json
    entry = {"id": f"smp{crc32_json({'p': sample['path']}) & 0xffff:04x}",
        "path": sample["path"], "filename": sample["filename"],
        "category": sample.get("category"), "bpm": sample.get("bpm"),
        "key": sample.get("key"), "duration": round(sample.get("duration") or 0, 3),
        "selection_score": None, "scored_for_genre": None,
        "roles": [role], "selection_source": "user-assigned via Studio"}
    roles[role] = entry
    palette_state["studio_user_modified"] = (
        _palette_role_paths(roles) != _palette_role_paths(palette_state["studio_original_roles"]))
    write_album(album)
    job_runner.log(f"palette sample assigned: {sample['filename']} → {role}", src="studio")
    return {"added": True, "mode": mode, "role": role,
            "sample": sample, "palette": palette_doc()}


def _palette_role_paths(roles: dict) -> dict[str, str]:
    assigned = {}
    for key, entry in roles.items():
        path = entry.get("path")
        if not path:
            continue
        canonical_path = os.path.normcase(os.path.realpath(path))
        for role in entry.get("roles") or [key]:
            assigned[role] = canonical_path
    return assigned


def remove_palette_role(role: str) -> dict:
    from timbor.album.palette import PALETTE_ROLES
    if role not in PALETTE_ROLES["strict"]:
        raise ValueError("unknown palette role")
    album = read_album()
    palette_state = album.setdefault("palette", {})
    roles = palette_state.setdefault("roles", {})
    original_before = copy.deepcopy(roles)
    removed = False
    for key, entry in list(roles.items()):
        current_roles = list(entry.get("roles", []))
        if key == role or role in current_roles:
            removed = True
            remaining = [r for r in current_roles if r != role]
            if remaining:
                entry["roles"] = remaining
            else:
                roles.pop(key, None)
    if removed:
        original = palette_state.get("studio_original_roles")
        if original is not None:
            for original_key, original_entry in original.items():
                original_roles = original_entry.get("roles") or [original_key]
                if role not in original_roles:
                    continue
                original_path = os.path.normcase(os.path.realpath(original_entry.get("path", "")))
                same_sample = next((entry for entry in roles.values()
                    if entry.get("path") and
                    os.path.normcase(os.path.realpath(entry["path"])) == original_path), None)
                if same_sample:
                    same_sample["roles"] = sorted(set(same_sample.get("roles", [])) | {role})
                else:
                    restored = copy.deepcopy(original_entry)
                    restored["roles"] = [role]
                    restore_key = original_key if original_key not in roles else role
                    roles[restore_key] = restored
            palette_state["studio_user_modified"] = (
                _palette_role_paths(roles) != _palette_role_paths(original))
            if not palette_state["studio_user_modified"]:
                palette_state.pop("studio_user_modified", None)
                palette_state.pop("studio_original_roles", None)
        else:
            palette_state["studio_user_modified"] = True
    write_album(album)
    return {"removed": removed, "role": role, "palette": palette_doc()}


# ---------------------------------------------------------------------------
# job runner: background generate.py operations (one at a time)
# ---------------------------------------------------------------------------

JOB_DEFS = {
    # "{album}" expands to the album directory in each stage command
    "render": {"label": "RENDER ALBUM",
               "commands": [["--render-album", "{album}"],
                            ["--album-loudness", "{album}"]]},
    "verify": {"label": "VERIFY ALBUM",
               "commands": [["--verify-album", "{album}"]]},
    "loudness": {"label": "LOUDNESS ANALYSIS",
                 "commands": [["--album-loudness", "{album}"]]},
    "package": {"label": "PACKAGE RELEASE",
                "commands": [["--package-album", "{album}",
                              "--include-track-masters"]]},
    "resequence": {"label": "SEQUENCE ALBUM",
                   "commands": [["--sequence-album", "{album}"]]},
}


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: "deque[dict]" = deque()
        self._current: dict | None = None
        self._counter = 0
        self.logs: deque = deque(maxlen=4000)
        self._log_seq = 0
        self._wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True,
                         name="timbor-jobs").start()

    # -- logging ----------------------------------------------------------
    def log(self, text: str, src: str = "studio") -> None:
        with self._lock:
            self._log_seq += 1
            self.logs.append({"seq": self._log_seq,
                              "ts": time.strftime("%H:%M:%S"),
                              "src": src, "text": text})

    def logs_since(self, since: int) -> tuple[list, int]:
        with self._lock:
            lines = [l for l in self.logs if l["seq"] > since]
            return lines, self._log_seq

    # -- job control ------------------------------------------------------
    def submit(self, kind: str) -> dict:
        if kind not in JOB_DEFS:
            raise KeyError(kind)
        with self._lock:
            self._counter += 1
            job = {
                "id": f"job-{self._counter}",
                "kind": kind,
                "label": JOB_DEFS[kind]["label"],
                "status": "queued",
                "stages": [{"name": (JOB_DEFS[kind]["label"] if i == 0 else
                                     JOB_DEFS[kind]["commands"][i][0].lstrip("-").upper()),
                            "state": "pending"}
                           for i in range(len(JOB_DEFS[kind]["commands"]))],
                "started": None, "ended": None,
                "exit_codes": [], "error": None,
            }
            self._queue.append(job)
            queued_note = f"queued {job['label']} ({job['id']})"
            self._wake.set()
        # log OUTSIDE the lock: threading.Lock is non-reentrant and log()
        # takes the same lock — calling it inside deadlocked every job POST
        self.log(queued_note)
        return job

    def current(self) -> dict | None:
        with self._lock:
            if self._current:
                return dict(self._current)
            return None

    def jobs_snapshot(self) -> dict:
        with self._lock:
            cur = dict(self._current) if self._current else None
            return {"current": cur,
                    "queued": sum(1 for _ in self._queue),
                    "definitions": {k: v["label"]
                                    for k, v in JOB_DEFS.items()}}

    # -- worker -----------------------------------------------------------
    def _worker(self) -> None:
        while True:
            with self._lock:
                job = self._queue.popleft() if self._queue else None
                if job:
                    self._current = job
                    job["status"] = "running"
                    job["started"] = time.time()
            if not job:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self.log(f"started {job['label']}")
            ok = True
            for i, extra in enumerate(JOB_DEFS[job["kind"]]["commands"]):
                with self._lock:
                    job["stages"][i]["state"] = "running"
                cmd = [sys.executable, os.path.join(ROOT, "generate.py"),
                       *[a.replace("{album}", os.path.abspath(ALBUM_DIR))
                         for a in extra]]
                self.log(f"$ {' '.join(os.path.basename(c) for c in cmd)}",
                         src="engine")
                try:
                    # below-normal priority + single-threaded BLAS in the
                    # child keeps the box responsive while heavy engine
                    # operations run (memory-constrained hosts)
                    flags = 0x4000 if os.name == "nt" else 0
                    env = dict(os.environ,
                               OPENBLAS_NUM_THREADS="1",
                               OMP_NUM_THREADS="1",
                               MKL_NUM_THREADS="1")
                    proc = subprocess.run(
                        cmd, cwd=ROOT, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=600,
                        creationflags=flags, env=env)
                    for line in (proc.stdout or "").splitlines():
                        self.log(line, src="engine")
                    for line in (proc.stderr or "").splitlines():
                        self.log(line, src="engine:error")
                    with self._lock:
                        job["exit_codes"].append(proc.returncode)
                    if proc.returncode != 0:
                        ok = False
                        self.log(f"stage failed rc={proc.returncode}, "
                                 f"aborting {job['label']}", src="studio")
                        break
                except Exception as exc:  # noqa: BLE001 — report, never crash
                    self.log(f"stage error: {exc}", src="studio:error")
                    ok = False
                    break
            with self._lock:
                # recompute stage states cleanly
                for st in job["stages"]:
                    if st["state"] == "running":
                        st["state"] = "done" if ok else "failed"
                job["status"] = "done" if ok else "error"
                if not ok and job["error"] is None:
                    job["error"] = "engine stage failed — see console log"
                job["ended"] = time.time()
                self._current = None
            invalidate()
            self.log(f"{job['label']} "
                     f"{'complete' if ok else 'FAILED'} — caches refreshed")
            self._wake.set()


job_runner = JobRunner()

# ---------------------------------------------------------------------------
# config writes (real album.json edits; the engine applies them on render)
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    pass


def apply_sequencing_config(body: dict) -> dict:
    album = read_album()
    seq = album.get("sequencing") or {}
    cur = SequenceConfig.from_album(album)
    mode = body.get("transition_mode", cur.transition_mode)
    gap = float(body.get("gap_seconds", cur.gap_seconds))
    xin = float(body.get("crossfade_seconds", cur.crossfade_seconds))
    fin = float(body.get("fade_in_seconds", cur.fade_in_seconds))
    fout = float(body.get("fade_out_seconds", cur.fade_out_seconds))
    quantize = body.get("quantize", cur.quantize)
    curve = body.get("curve", cur.curve)
    order = body.get("order", cur.order)
    try:
        cfg = SequenceConfig(enabled=bool(body.get("enabled", True)),
                             transition_mode=mode, gap_seconds=gap,
                             crossfade_seconds=xin, fade_in_seconds=fin,
                             fade_out_seconds=fout, quantize=quantize,
                             curve=curve, order=order)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    numbers = [t["number"] for t in album["tracks"]]
    if cfg.order is not None:
        if sorted(cfg.order) != sorted(numbers):
            raise ConfigError(
                f"order must be a permutation of track numbers {numbers}")
    album["sequencing"] = {
        "enabled": cfg.enabled, "transition_mode": cfg.transition_mode,
        "gap_seconds": cfg.gap_seconds,
        "fade_in_seconds": cfg.fade_in_seconds,
        "fade_out_seconds": cfg.fade_out_seconds,
        "crossfade_seconds": cfg.crossfade_seconds,
        "preserve_track_boundaries": True,
    }
    if cfg.order is not None:
        album["sequencing"]["order"] = list(cfg.order)
    elif "order" in album["sequencing"]:
        del album["sequencing"]["order"]
    album["transition"] = {"quantize": cfg.quantize, "curve": cfg.curve}
    write_album(album)
    job_runner.log(f"sequencing config updated: mode={cfg.transition_mode} "
                   f"gap={cfg.gap_seconds}s xfade={cfg.crossfade_seconds}s "
                   f"quantize={cfg.quantize} curve={cfg.curve} "
                   f"hash={cfg.config_hash()}")
    return {"sequencing": album["sequencing"],
            "transition": album["transition"],
            "config_hash": cfg.config_hash()}


def apply_loudness_config(body: dict) -> dict:
    album = read_album()
    mode = body.get("mode", "preserve")
    if mode not in ("preserve", "match", "target"):
        raise ConfigError("loudness mode must be preserve|match|target")
    target = float(body.get("target_lufs", -14.0))
    ceiling = float(body.get("limiter_ceiling_db", -1.0))
    if not -40.0 <= target <= 0.0:
        raise ConfigError("target_lufs out of range [-40, 0]")
    if ceiling >= 0.0:
        raise ConfigError("limiter_ceiling_db must be < 0 dBTP")
    album["loudness"] = {
        "mode": mode, "target_lufs": target,
        "limiter": bool(body.get("limiter", False)),
        "limiter_ceiling_db": ceiling,
    }
    write_album(album)
    job_runner.log(f"loudness policy updated: mode={mode} "
                   f"target={target} LUFS limiter={album['loudness']['limiter']} "
                   f"ceiling={ceiling} dBTP")
    return album["loudness"]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

MAX_BODY = 1 << 20
ROUTE_RE = re.compile(r"^/api/tracks/(\d{2})(/timeline|/peaks|/stems)?$")


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "TIMBOR-Studio/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet default logging
        pass

    def send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, title: str, detail: str) -> None:
        self.send_json({"error": {"status": status, "title": title,
                                  "detail": detail}}, status)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def guard_fs_path(self, rel: str) -> str:
        p = rel.replace("\\", os.sep).replace("/", os.sep)
        full = os.path.abspath(p if os.path.isabs(p)
                               else os.path.join(ROOT, p))
        root_real = os.path.realpath(ROOT)
        full_real = os.path.realpath(full)
        try:
            inside = os.path.commonpath((full_real, root_real)) == root_real
        except ValueError:
            inside = False
        if not inside:
            raise PermissionError("path outside project root")
        return full_real

    # -- GET --------------------------------------------------------------
    def do_GET(self):  # noqa: N802 (stdlib naming)
        try:
            path, _, query = self.path.partition("?")
            params = {urllib.parse.unquote_plus(p.split("=", 1)[0]):
                  urllib.parse.unquote_plus(p.split("=", 1)[1])
                  for p in query.split("&") if "=" in p}
            if path.startswith("/api/"):
                self.api_get(path, params)
            elif path.startswith("/media/"):
                self.stream_media(path[len("/media/"):], params)
            else:
                self.static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as exc:
            try:
                self.send_error_json(400, "unsafe path", str(exc))
            except Exception:  # noqa: BLE001
                pass
        except ValueError as exc:
            try:
                self.send_error_json(400, "bad request", str(exc))
            except Exception:  # noqa: BLE001
                pass
        except FileNotFoundError as exc:
            try:
                self.send_error_json(404, "not found", str(exc))
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            try:
                self.send_error_json(500, "server error", repr(exc))
            except Exception:  # noqa: BLE001
                pass

    def api_get(self, path: str, params: dict) -> None:
        art = artifact_paths()
        if path == "/api/health":
            self.send_json(health())
        elif path == "/api/sample-libraries":
            import importlib.util
            aiff_decoder = importlib.util.find_spec("aifc") is not None
            self.send_json({"roots": sample_library.roots(),
                            "stats": self._sample_library_stats(),
                            "formats": {
                                "wav": {"indexed": True, "analyzed": True, "preview": True},
                                "aiff": {"indexed": True, "analyzed": aiff_decoder,
                                         "preview": aiff_decoder,
                                         "note": None if aiff_decoder else "Python aifc decoder unavailable"},
                                "flac": {"indexed": True, "analyzed": False, "preview": False,
                                         "note": "decoder required"},
                                "mp3": {"indexed": True, "analyzed": False, "preview": False,
                                        "note": "decoder required"}}})
        elif path == "/api/album/palette/roles":
            from timbor.album.palette import PALETTE_ROLES, palette_roles_for
            from timbor.samples.selector import ROLE_SPECS
            album = read_album()
            mode = (album.get("palette") or {}).get("mode") or "balanced"
            allowed = sorted(set(palette_roles_for(mode)) & set(PALETTE_ROLES["strict"]))
            self.send_json({"mode": mode, "roles": [
                {"id": role, "categories": sorted(ROLE_SPECS[role]["categories"]),
                 "current": any(role in e.get("roles", [])
                                for e in (album.get("palette", {}).get("roles", {}) or {}).values())}
                for role in allowed if role in ROLE_SPECS]})
        elif path == "/api/sample-libraries/scan/status":
            self.send_json(sample_library.scan_status())
        elif path == "/api/samples":
            self.send_json(sample_library.search(params))
        elif path.startswith("/api/samples/"):
            m_sample = re.match(r"^/api/samples/([^/]+)(/waveform|/usage)?$", path)
            if not m_sample:
                return self.send_error_json(404, "not found", path)
            sid, tail = m_sample.group(1), m_sample.group(2) or ""
            try:
                if tail == "/waveform":
                    self.send_json(sample_library.waveform(sid, int(params.get("bins", "1200"))))
                elif tail == "/usage":
                    self.send_json(sample_usage_doc(sid))
                else:
                    self.send_json(sample_library.detail(sid))
            except KeyError:
                self.send_error_json(404, "not found", "unknown sample/library")
            except FileNotFoundError as exc:
                self.send_error_json(404, "not found", str(exc))
            except ValueError as exc:
                self.send_error_json(400, "bad request", str(exc))
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(500, "sample service error", repr(exc))
        elif path == "/api/albums":
            self.send_json(albums_listing())
        elif path == "/api/album":
            self.send_json(read_album())
        elif path == "/api/album/manifest":
            doc = _cached_json(art["album_manifest"])
            if doc is None:
                return self.send_error_json(404, "not found",
                                            "album-level manifest not written")
            self.send_json(doc)
        elif path == "/api/album/peaks":
            self.send_json(self._peaks(art["album_wav"], params))
        elif path == "/api/sequence":
            doc = _cached_json(art["sequence"])
            if doc is None:
                return self.send_error_json(404, "not found",
                                            "sequence.json not written yet")
            self.send_json(doc)
        elif path == "/api/loudness":
            doc = _cached_json(art["loudness"])
            if doc is None:
                return self.send_error_json(404, "not found",
                                            "loudness.json not written yet")
            self.send_json(doc)
        elif path == "/api/qc":
            doc = _cached_json(art["qc"])
            if doc is None:
                return self.send_error_json(404, "not found",
                                            "qc.json not written yet")
            self.send_json(doc)
        elif path == "/api/release":
            doc = _cached_json(art["release_manifest"])
            if doc is None:
                return self.send_error_json(404, "not found",
                                            "release manifest not written")
            self.send_json(doc)
        elif path == "/api/release/tree":
            self.send_json(release_tree())
        elif path == "/api/tracks":
            self.send_json(tracks_doc())
        elif path == "/api/motifs":
            self.send_json(motifs_doc())
        elif path == "/api/palette":
            self.send_json(palette_doc())
        elif path == "/api/palette/peaks":
            rel = params.get("path", "")
            detail = sample_library.lookup_path(rel)
            if detail is None:
                return self.send_error_json(404, "not found",
                                            "sample is not in a registered library")
            try:
                self.send_json(sample_library.waveform(detail["id"],
                                                       int(params.get("bins", "1200"))))
            except ValueError as exc:
                self.send_error_json(400, "bad request", str(exc))
        elif path == "/api/jobs":
            self.send_json(job_runner.jobs_snapshot())
        elif path == "/api/logs":
            since = int(params.get("since", "0"))
            lines, nxt = job_runner.logs_since(since)
            self.send_json({"lines": lines, "next": nxt})
        else:
            m = ROUTE_RE.match(path)
            if not m:
                return self.send_error_json(404, "not found", path)
            tid, tail = m.group(1), m.group(2) or ""
            try:
                if tail == "":
                    self.send_json(track_doc(tid))
                elif tail == "/timeline":
                    doc = track_doc(tid)
                    project = slim_project(doc["track"]["project"])
                    events = [{k: e.get(k) for k in
                               ("id", "type", "bus", "role", "section", "start_beat",
                                "duration_beats", "velocity", "instrument",
                                "pitch", "sample_id", "source_path",
                                "pitch_semitones", "stretch_ratio", "chop_ops",
                                "reverse", "metadata")}
                              for e in (project["events"] if project else [])]
                    self.send_json({"track_id": tid, "events": events})
                elif tail == "/peaks":
                    master = os.path.join(ALBUM_DIR, self.doc_master(tid))
                    self.send_json(self._peaks(master, params))
                elif tail == "/stems":
                    doc = track_doc(tid)
                    project = slim_project(doc["track"]["project"])
                    tdir = doc["track"]["directory"]
                    stems = []
                    for rel in (project["stems"] if project else []):
                        full_rel = f"{tdir}/{rel}"
                        full = os.path.join(ALBUM_DIR, tdir,
                                            *rel.split("/"))
                        if os.path.isfile(full):
                            info = parse_wav(full)
                            stems.append({"name": os.path.splitext(
                                os.path.basename(rel))[0],
                                "rel": full_rel, "duration_seconds":
                                    round(info["frames"] / info["rate"], 3),
                                "rate": info["rate"], "bits": info["bits"]})
                    self.send_json({"track_id": tid, "stems": stems})
            except KeyError:
                self.send_error_json(404, "not found", f"track {tid}")
            except FileNotFoundError as exc:
                self.send_error_json(404, "not found", str(exc))
            except ValueError as exc:
                self.send_error_json(400, "bad request", str(exc))

    def _sample_library_stats(self) -> dict:
        roots = sample_library.roots()
        aggregate = {"files": 0, "analyzed": 0, "pending": 0, "errors": 0,
                     "formats": {}, "categories": {}, "duplicate_groups": 0}
        for root in roots:
            stats = root["stats"]
            for key in ("files", "analyzed", "pending", "errors", "duplicate_groups"):
                aggregate[key] += stats.get(key, 0)
            for group in ("formats", "categories"):
                for name, count in stats.get(group, {}).items():
                    aggregate[group][name] = aggregate[group].get(name, 0) + count
        return aggregate

    def doc_master(self, tid: str) -> str:
        album = read_album()
        t = next((x for x in album["tracks"] if x["number"] == tid), None)
        if not t:
            raise KeyError(tid)
        return f"{t['directory']}/audio/master.wav"

    def _peaks(self, path: str, params: dict) -> dict:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        return extract_peaks(path, int(params.get("bins", "1200")))

    # -- media streaming (Range support) ----------------------------------
    def stream_media(self, rel: str, params: dict) -> None:
        if rel.startswith("sample/"):
            sid = rel[len("sample/"):]
            try:
                full, _ctype = sample_library.media_path(sid)
            except PermissionError as exc:
                return self.send_error_json(400, "unsafe path", str(exc))
            except KeyError:
                return self.send_error_json(404, "not found", "unknown sample")
            except FileNotFoundError as exc:
                return self.send_error_json(404, "not found", str(exc))
            except ValueError as exc:
                return self.send_error_json(400, "bad request", str(exc))
        else:
            # Resolve only within the active album, whether the caller used an
            # album-relative track path or its project-relative album path.
            rel_clean = rel.replace("\\", "/")
            if any(part in ("", ".", "..") for part in rel_clean.split("/")):
                return self.send_error_json(400, "unsafe path", rel)
            parts = rel_clean.split("/")
            album_real = os.path.realpath(ALBUM_DIR)
            candidates = [os.path.realpath(os.path.join(ALBUM_DIR, *parts)),
                          os.path.realpath(os.path.join(ROOT, *parts))]
            def in_active_album(candidate: str) -> bool:
                try:
                    return os.path.commonpath((candidate, album_real)) == album_real
                except ValueError:
                    return False
            full = next((p for p in candidates if os.path.isfile(p)
                         and in_active_album(p)), None)
            if full is None:
                return self.send_error_json(404, "not found", rel)
        if not full.lower().endswith((".wav", ".wave")) or not os.path.isfile(full):
            return self.send_error_json(404, "not found", rel)
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if not m:
                return self.send_error_json(416, "bad range", rng)
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(size - int(m.group(2)), 0)
            if start > end or start >= size:
                return self.send_error_json(416, "range not satisfiable", rng)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 18, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # -- static files ------------------------------------------------------
    STATIC_TYPES = {".html": "text/html; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png", ".svg": "image/svg+xml",
                    ".ico": "image/x-icon", ".woff2": "font/woff2"}

    def static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.abspath(os.path.join(WWW_DIR, rel))
        if not full.startswith(WWW_DIR) or not os.path.isfile(full):
            return self.send_error_json(404, "not found", path)
        ext = os.path.splitext(full)[1].lower()
        ctype = self.STATIC_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- POST --------------------------------------------------------------
    def do_DELETE(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            match = re.match(r"^/api/sample-libraries/([a-f0-9]{16})$", path)
            if match:
                removed = sample_library.remove_root(match.group(1))
                if not removed:
                    return self.send_error_json(404, "not found", "unknown library")
                return self.send_json({"removed": True, "physical_files_deleted": False,
                                       "roots": sample_library.roots()})
            match = re.match(r"^/api/album/palette/roles/([a-z_]+)$", path)
            if match:
                return self.send_json(remove_palette_role(match.group(1)))
            self.send_error_json(404, "not found", path)
        except RuntimeError as exc:
            self.send_error_json(409, "operation conflict", str(exc))
        except ValueError as exc:
            self.send_error_json(400, "bad request", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, "server error", repr(exc))

    def do_POST(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/sample-libraries":
                body = self.read_body()
                self.send_json({"root": sample_library.add_root(body.get("path"))}, 201)
            elif path == "/api/sample-libraries/scan":
                body = self.read_body()
                self.send_json(sample_library.start_scan(body.get("root_id")), 202)
            elif path == "/api/album/palette/samples":
                result = add_palette_sample(self.read_body())
                self.send_json(result)
            elif path == "/api/album/config/sequencing":
                self.send_json(apply_sequencing_config(self.read_body()))
            elif path == "/api/album/config/loudness":
                self.send_json(apply_loudness_config(self.read_body()))
            elif path == "/api/album/order":
                body = self.read_body()
                self.send_json(apply_sequencing_config(
                    {"order": body.get("order")}))
            elif path == "/api/albums/switch":
                body = self.read_body()
                self.send_json(switch_album(body.get("dir")))
            elif path == "/api/jobs":
                body = self.read_body()
                kind = body.get("kind")
                if kind not in JOB_DEFS:
                    return self.send_error_json(
                        400, "unknown job kind",
                        f"kind must be one of {sorted(JOB_DEFS)}")
                if job_runner.current():
                    self.send_json({"submitted": False, "busy": True,
                                    "job": job_runner.current()})
                else:
                    self.send_json({"submitted": True,
                                    "job": job_runner.submit(kind)})
            else:
                self.send_error_json(404, "not found", path)
        except RuntimeError as exc:
            self.send_error_json(409, "operation conflict", str(exc))
        except KeyError as exc:
            self.send_error_json(404, "not found", str(exc))
        except PermissionError as exc:
            self.send_error_json(400, "unsafe path", str(exc))
        except ConfigError as exc:
            self.send_error_json(400, "invalid configuration", str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error_json(400, "bad request", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, "server error", repr(exc))


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="TIMBOR Album Studio server")
    ap.add_argument("--album", default=None,
                    help="album directory (default: auto-detect under albums/)")
    ap.add_argument("--list-albums", action="store_true",
                    help="list albums under albums/ and exit")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.list_albums:
        found = list_album_dirs()
        if not found:
            raise SystemExit("no album directories with album.json under albums/")
        for d in found:
            print(os.path.relpath(d, ROOT).replace(os.sep, "/"))
        return

    global ALBUM_DIR, ALBUM_REL
    if args.album:
        ALBUM_DIR = os.path.abspath(
            args.album if os.path.isabs(args.album)
            else os.path.join(ROOT, args.album))
        if not os.path.isfile(os.path.join(ALBUM_DIR, "album.json")):
            raise SystemExit(f"no album.json under {ALBUM_DIR}")
        ALBUM_REL = os.path.relpath(ALBUM_DIR, ROOT).replace(os.sep, "/")

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), StudioHandler)
    job_runner.log(f"TIMBOR Album Studio serving {ALBUM_REL} "
                   f"at http://127.0.0.1:{args.port}")
    if len(list_album_dirs()) > 1:
        job_runner.log("multiple albums available — switch from the "
                       "album picker in the top bar (jobs always target "
                       "the active album)", src="studio")
    print(f"TIMBOR Album Studio — {ALBUM_REL}")
    print(f"  http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
