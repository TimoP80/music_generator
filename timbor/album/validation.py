"""timbor.album.validation — EP-level continuity checks (spec §15).

Validates musical continuity across the album: one key world, one BPM
neighborhood, one motif family with recognizable variants, no accidental
identical tracks, and (in render mode) palette propagation. Deterministic;
each issue is a structured record, never a subjective quality score.
"""
from __future__ import annotations

from .motif import motif_fingerprint
from .palette import PseudoSong, same_path  # noqa: F401 (PseudoSong re-export)

IDENTITY_FLOOR = 0.35


def album_pseudo_song(cfg, genre: str) -> "PseudoSong":
    """Album-level pseudo-song for a genre (used by palette scoring)."""
    return PseudoSong(cfg, genre)


def _issue(code: str, severity: str, message: str,
           track: int | None = None) -> dict:
    d = {"code": code, "severity": severity, "message": message}
    if track is not None:
        d["track"] = track
    return d


def validate_album(cfg, dna, track_records: list[dict],
                   project_dicts: list[dict | None] | None = None,
                   palette: dict | None = None) -> list[dict]:
    """Return structured issues; empty list = coherent album.

    severity 'error' = album is broken (regenerate); 'warning' = documented
    deviation worth knowing about (e.g. a genre-snapped BPM outside the
    neighborhood — musically right, geometrically outside).
    """
    issues: list[dict] = []
    tracks = track_records

    # 1) deterministic distinct seeds (spec §4)
    seeds = [t.get("seed") for t in tracks]
    dup_seeds = {s for s in seeds if s is not None and seeds.count(s) > 1}
    if dup_seeds:
        issues.append(_issue("duplicate_seeds", "error",
                             f"tracks share seeds: {sorted(dup_seeds)}"))

    # 2) BPM neighborhood (genre snapping may legitimately stray → warning)
    lo, hi = dna.bpm_window()
    for i, t in enumerate(tracks):
        bpm = t.get("bpm")
        if bpm is None:
            continue
        if not lo - 1e-6 <= bpm <= hi + 1e-6:
            issues.append(_issue(
                "bpm_outside_neighborhood", "warning",
                f"bpm {bpm:.0f} outside [{lo:.0f}, {hi:.0f}] "
                f"(snapped to {t.get('genre')} genre range)", track=i))

    # 3) key/mode coherence (spec §5: shared tonal center)
    for i, t in enumerate(tracks):
        if t.get("key") and t["key"] != f"{dna.root_key} {dna.mode}":
            issues.append(_issue("key_drift", "error",
                                 f"key {t['key']!r} differs from album center "
                                 f"{dna.root_key} {dna.mode}", track=i))

    # 4) motif lineage: identity floor + unique variants (spec §6–§8)
    fps: dict[str, int] = {}
    for i, t in enumerate(tracks):
        lin = t.get("lineage") or {}
        score = lin.get("identity_score")
        if score is not None and score < IDENTITY_FLOOR:
            issues.append(_issue(
                "motif_identity_below_floor", "error",
                f"variant {lin.get('variant')} identity {score:.2f} < "
                f"{IDENTITY_FLOOR:.2f}", track=i))
        fp = lin.get("fingerprint")
        if fp:
            if fp in fps:
                issues.append(_issue(
                    "duplicate_motif_variant", "error",
                    f"tracks {fps[fp]} and {i} share an identical variant",
                    track=i))
            fps[fp] = i

    # 5) track distinctness: no two tracks same (genre, bpm) identity
    combos: dict[tuple, int] = {}
    for i, t in enumerate(tracks):
        combo = (t.get("genre"), t.get("bpm"))
        if combo in combos:
            issues.append(_issue(
                "identical_track_signature", "warning",
                f"tracks {combos[combo]} and {i} share genre+bpm "
                f"({combo[0]} @{combo[1]:.0f}) — check divergence", track=i))
        combos[combo] = i

    # 6) duration window (render mode only)
    dlo, dhi = sorted(cfg.duration_range[:2]) if len(cfg.duration_range) >= 2 \
        else (None, None)
    if dlo:
        for i, t in enumerate(tracks):
            dur = t.get("duration_seconds")
            if dur is None:
                continue
            if not (dlo * 0.7 <= dur <= dhi * 1.4):
                issues.append(_issue(
                    "duration_out_of_range", "warning",
                    f"duration {dur:.0f}s outside [{dlo:.0f}, {dhi:.0f}]s "
                    f"tolerance", track=i))

    # 7) project-level coherence (render mode)
    if project_dicts:
        expected_scale = {"minor": "natural_minor", "major": "major"}.get(
            dna.mode, dna.mode)
        for i, (t, p) in enumerate(zip(tracks, project_dicts)):
            if p is None:
                continue
            if abs(p["song"]["bpm"] - t.get("bpm", -1)) > 1e-6:
                issues.append(_issue("project_bpm_mismatch", "error",
                                     f"project bpm {p['song']['bpm']} != record "
                                     f"{t.get('bpm')}", track=i))
            if p["song"].get("motif") != (t.get("lineage") or {}).get("motif"):
                issues.append(_issue(
                    "project_motif_mismatch", "error",
                    "rendered motif differs from lineage record", track=i))
            if p["song"].get("key") != dna.root_key or \
                    p["song"].get("scale") != expected_scale:
                issues.append(_issue(
                    "project_key_mismatch", "error",
                    f"project key {p['song'].get('key')} "
                    f"{p['song'].get('scale')} differs from album DNA",
                    track=i))

    # 8) palette propagation (render mode, spec §10–§11)
    if project_dicts and palette:
        issues.extend(_palette_issues(cfg, palette, project_dicts))

    return issues


def _palette_issues(cfg, palette: dict, project_dicts) -> list[dict]:
    """Album-level propagation (spec §10: tracks are NOT forced to reuse
    every palette sample — but a shared anchor that reaches no majority of
    tracks is not doing its job)."""
    from .palette import palette_roles_for
    gated = set(palette_roles_for(cfg.palette_mode))
    entries = [e for e in (palette or {}).get("roles", {}).values()
               if any(r in gated for r in e.get("roles", []))]
    issues: list[dict] = []
    usable = [p for p in project_dicts if p is not None]
    if not usable:
        return issues
    used_per_track = [_sample_paths(p) for p in usable]
    majority = len(usable) // 2 + 1
    for e in entries:
        n = sum(1 for used in used_per_track
                if any(same_path(e["path"], up) for up in used))
        if n == 0:
            issues.append(_issue(
                "palette_unused", "warning",
                f"palette {e.get('roles')} ({e['filename']}) used by no "
                f"track (no track needed that role — spec §10: tracks are "
                f"not forced to reuse every sample)"))
        elif n < majority:
            issues.append(_issue(
                "palette_weak_propagation", "warning",
                f"palette {e['filename']} reaches {n}/{len(usable)} tracks "
                f"(< majority)"))
    return issues


def _sample_paths(project: dict) -> set[str]:
    by_id = {s["id"]: s.get("path", "") for s in project.get("samples", [])}
    out = set()
    for ev in project.get("timeline", []):
        sid = ev.get("sample_id")
        if sid and sid in by_id:
            out.add(by_id[sid])
    return out


def revalidate_album(album: dict, out_root: str) -> list[dict]:
    """Re-run continuity validation against the projects currently on disk.

    Used by --album-report: inspection re-checks without regenerating (spec
    §25). Missing project files count as render-mode gaps, not errors.
    """
    import os
    from ..timeline.serialization import load_project
    from .dna import AlbumConfig, AlbumDNA
    cfg = AlbumConfig.from_json(album["config"])
    dna = AlbumDNA.from_json(album["dna"])
    pds: list[dict | None] = []
    for t in album["tracks"]:
        pj = None
        if t.get("project"):
            try:
                pj = load_project(os.path.join(out_root, t["project"]))
            except OSError:
                pj = None
        pds.append(pj)
    return validate_album(cfg, dna, album["tracks"], pds,
                          palette=album.get("palette"))


def format_album_report(album: dict) -> str:
    """Console summary of a generated album."""
    dna = album["dna"]
    lines = [
        f"TIMBOR ALBUM — {album['name']}",
        f"  dna: key {dna['root_key']} {dna['mode']}, "
        f"bpm {dna['bpm_center']:.0f}±{dna['bpm_range']:.0f}, "
        f"progression {dna['harmonic_family']}, motif {dna['motif_contour']}, "
        f"palette {album['palette'].get('mode')}",
        "",
        "  #  title                     genre        bpm  motif  identity",
    ]
    for t in album["tracks"]:
        lin = t.get("lineage") or {}
        title = (t.get("title") or "")[:23]
        lines.append(f"  {t['number']}  {title:23s} {t.get('genre') or '-':12s} "
                     f"{(t.get('bpm') or 0):4.0f}  {lin.get('variant', '-'):5s}  "
                     f"{lin.get('identity_score', 0):.2f}")
    v = album.get("validation") or {}
    n_err = sum(1 for i in v if i["severity"] == "error")
    n_warn = sum(1 for i in v if i["severity"] == "warning")
    lines.append("")
    lines.append(f"  validation: {n_err} errors, {n_warn} warnings")
    for i in v:
        lines.append(f"    [{i['severity']}] {i['code']}: {i['message']}")
    return "\n".join(lines)
