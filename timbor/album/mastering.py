"""timbor.album.mastering — album-level loudness policy (spec §11–§14).

Three explicit modes (default: preserve):

* ``preserve`` — track masters are used bit-exactly; no gain at all.
* ``match``    — deterministic per-track gain offsets reduce large loudness
  differences (anchored to the loudest track; offsets capped, never boosted
  above +max_boost_db).
* ``target``   — every track moves to the configured integrated target.

Gains live in the sequence/metadata only; the source WAVs are never
rewritten (source master → album gain → sequencing). The album limiter is
OFF by default and, when enabled, is applied to the assembled album only —
never to individual track masters.
"""
from __future__ import annotations

import json
import os

import numpy as np

from .loudness import analyze_file, db, dbfs
from .dna import crc32_json

MASTERING_FORMAT = "timbor-mastering"
MASTERING_VERSION = 1

MODES = ("preserve", "match", "target")
MAX_MATCH_OFFSET_DB = 6.0     # beyond this, matching reports a warning
MAX_BOOST_DB = 3.0            # never boost a track by more than this


def policy_from(album: dict) -> dict:
    """Read the loudness policy from an album document (conservative defaults)."""
    pol = album.get("loudness") or {}
    mode = pol.get("mode", "preserve")
    if mode not in MODES:
        raise ValueError(f"loudness mode must be one of {MODES}")
    return {
        "mode": mode,
        "target_lufs": pol.get("target_lufs", -14.0),
        "limiter_enabled": bool(pol.get("limiter", False)),
        "limiter_ceiling_db": float(pol.get("limiter_ceiling_db", -1.0)),
        "limiter_release_seconds": float(pol.get("limiter_release_seconds",
                                                 0.05)),
        "limiter_lookahead_seconds": float(
            pol.get("limiter_lookahead_seconds", 0.005)),
        "match_window_db": float(pol.get("match_window_db", 3.0)),
    }


def compute_gains(album: dict, album_root: str, policy: dict) -> dict:
    """Deterministic per-track gain plan (dB, 0.0 = untouched).

    Analyzes each track master's integrated loudness (measured, cached in
    the returned document — never estimated from filenames).
    """
    tracks = []
    for t in album["tracks"]:
        master = os.path.join(album_root, t["directory"], "audio", "master.wav")
        m = analyze_file(master)
        tracks.append({"track_id": t["number"], "title": t.get("title"),
                       "master": os.path.join(t["directory"], "audio",
                                              "master.wav").replace(os.sep, "/"),
                       "integrated_lufs_approx": m["integrated_lufs_approx"],
                       "peak_dbfs": m["peak_dbfs"],
                       "gain_db": 0.0, "warnings": []})
    doc = {"format": MASTERING_FORMAT, "version": MASTERING_VERSION,
           "mode": policy["mode"], "target_lufs": policy["target_lufs"],
           "limiter": {
               "enabled": policy["limiter_enabled"],
               "ceiling_db": policy["limiter_ceiling_db"],
               "release_seconds": policy["limiter_release_seconds"],
               "lookahead_seconds": policy["limiter_lookahead_seconds"],
           },
           "tracks": tracks}

    if policy["mode"] == "preserve":
        return doc

    if policy["mode"] == "target":
        target = float(policy["target_lufs"])
        for t in tracks:
            if t["integrated_lufs_approx"] is None:
                t["warnings"].append("no integrated loudness; gain 0 dB")
                continue
            g = round(target - t["integrated_lufs_approx"], 3)
            if g > MAX_BOOST_DB:
                t["warnings"].append(
                    f"requested +{g:.2f} dB exceeds max boost "
                    f"+{MAX_BOOST_DB:.1f} dB; clamped")
                g = MAX_BOOST_DB
            t["gain_db"] = g
        return doc

    # match: anchor on the loudest track and lift quieter ones toward it;
    # tracks inside match_window_db stay untouched, boosts are capped at
    # MAX_BOOST_DB (clip risk is caught downstream by peak-safety / limiter)
    lufs = [t["integrated_lufs_approx"] for t in tracks]
    if any(v is None for v in lufs):
        for t in tracks:
            t["warnings"].append("match skipped (missing loudness); gain 0 dB")
        doc["warnings"] = ["match mode: some tracks had no loudness"]
        return doc
    anchor = max(lufs)
    for t in tracks:
        diff = t["integrated_lufs_approx"] - anchor          # <= 0
        g = 0.0 if -diff <= policy["match_window_db"] else round(-diff, 3)
        if g > MAX_BOOST_DB:
            t["warnings"].append(
                f"match boost +{g:.2f} dB clamped to +{MAX_BOOST_DB:.1f} dB")
            g = MAX_BOOST_DB
        t["gain_db"] = g
        if -diff > MAX_MATCH_OFFSET_DB:
            t["warnings"].append(
                f"loudness {t['integrated_lufs_approx']:.1f} LUFS differs "
                f"from album anchor by {-diff:.1f} LU")
    doc["anchor_lufs_approx"] = round(anchor, 2)
    return doc


def _gain_curve(x: np.ndarray, gain_db: float) -> np.ndarray:
    if abs(gain_db) < 1e-9:
        return x
    return x * (10.0 ** (gain_db / 20.0))


def apply_gain(l: np.ndarray, r: np.ndarray, gain_db: float) -> tuple[
        np.ndarray, np.ndarray]:
    """Deterministic sample-domain album gain (returns new arrays)."""
    if abs(gain_db) < 1e-9:
        return l.copy(), r.copy()
    g = 10.0 ** (gain_db / 20.0)
    return l * g, r * g


def limit(l: np.ndarray, r: np.ndarray, sr: int, ceiling_db: float = -1.0,
          release_seconds: float = 0.05,
          lookahead_seconds: float = 0.005) -> tuple[np.ndarray, np.ndarray]:
    """Explicit album limiter: block look-ahead peak limiter with release.

    Deterministic; applied ONLY to the assembled album in release.render_album
    when policy.limiter_enabled — never to track masters. The look-ahead
    envelope uses ceil-block maxima over the lookahead window (guarantees no
    sample above the ceiling survives, at block granularity), then a
    per-sample linear release brings gain back toward unity.
    """
    ceiling = 10.0 ** (ceiling_db / 20.0)
    la = max(1, int(lookahead_seconds * sr))
    rel = max(1, int(release_seconds * sr))
    out = []
    for ch in (l, r):
        a = np.abs(ch)
        nb = int(np.ceil(len(a) / la))
        pad = np.zeros(nb * la)
        pad[: len(a)] = a
        bm = pad.reshape(nb, la).max(axis=1)
        # look ahead: each block's gain must also cover the next block's peak
        env = np.maximum(bm, np.concatenate([bm[1:], [0.0]]))
        env = np.repeat(env, la)[: len(a)]
        target = np.minimum(1.0, ceiling / np.maximum(env, 1e-12))
        coeff = 1.0 / rel
        gain = np.empty(len(a))
        g = 1.0
        for i in range(len(a)):
            t = target[i]
            g = t if t < g else min(1.0, g + coeff)
            gain[i] = g
        out.append(ch * gain)
    return out[0], out[1]


def peak_report(l: np.ndarray, r: np.ndarray) -> dict:
    peak = float(max(np.max(np.abs(l)), np.max(np.abs(r))))
    return {"peak_dbfs": round(dbfs(peak), 2), "clipped_samples":
            int((np.abs(l) >= 1.0).sum() + (np.abs(r) >= 1.0).sum())}
