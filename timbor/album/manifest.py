"""timbor.album.manifest — EP-level manifest (spec §12 output).

The album manifest ties the independent projects together: shared DNA,
motif lineage per track, palette provenance (who used what, how often,
selection rationale), per-track stems/projects and validation summary.
Deterministic bytes: sort_keys, no wall-clock content.
"""
from __future__ import annotations

import json
import os

from .palette import same_path


def build_manifest(album: dict, out_root: str | None = None) -> dict:
    """Assemble the EP-level manifest from a generated album document."""
    dna = album["dna"]
    tracks_out = []
    for t in album["tracks"]:
        rec = {
            "number": t["number"],
            "title": t["title"],
            "genre": t.get("genre"),
            "seed": t.get("seed"),
            "bpm": t.get("bpm"),
            "key": t.get("key"),
            "bars": t.get("bars"),
            "duration_seconds": t.get("duration_seconds"),
            "lineage": t.get("lineage"),
        }
        if t.get("directory"):
            rec["directory"] = t["directory"]
        if t.get("project"):
            rec["project"] = t["project"]
        if t.get("exports"):
            rec["exports"] = t["exports"]
        tracks_out.append(rec)

    palette_prov = _palette_provenance(album, out_root)
    manifest = {
        "format": "timbor-album-manifest",
        "version": 1,
        "name": album["name"],
        "generator": {k: album["generator"][k] for k in
                      ("name", "version", "python", "os", "seed")},
        "dna": {
            "root_key": dna["root_key"],
            "mode": dna["mode"],
            "bpm_center": dna["bpm_center"],
            "bpm_range": dna["bpm_range"],
            "harmonic_family": dna["harmonic_family"],
            "motif_contour": dna["motif_contour"],
            "motif_family": dna["motif_family"],
            "rhythm_fingerprint": dna["rhythm_fingerprint"],
            "signature_instruments": dna["signature_instruments"],
            "era": dna["era"],
            "mood": dna["mood"],
            "authenticity": dna["authenticity"],
            "genre_anchor": dna["genre_anchor"],
            "dna_hash": album["dna_hash"],
        },
        "config": {k: album["config"][k] for k in album["config"]
                   if k != "track_overrides"},
        "palette": palette_prov,
        "tracks": tracks_out,
        "validation": album.get("validation", []),
    }
    return manifest


def _palette_provenance(album: dict, out_root: str | None) -> dict:
    """Palette entries with usage: tracks using it + placement counts (§11)."""
    pal = album.get("palette") or {}
    roles = pal.get("roles", {})
    usage: dict[str, dict] = {}

    for t in album["tracks"]:
        sid_map = None
        placements: list[dict] = []
        if t.get("project") and out_root:
            pj = os.path.join(out_root, t["project"])
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except OSError:
                continue
            sid_map = {s["id"]: s.get("path", "") for s in doc.get("samples", [])}
            for ev in doc.get("timeline", []):
                sid = ev.get("sample_id")
                if sid and sid in sid_map:
                    placements.append({"path": sid_map[sid],
                                       "beat": ev.get("start_beat"),
                                       "section": ev.get("section")})
        # unique entries only (merged entries can serve several roles)
        seen_paths: set[str] = set()
        for e in roles.values():
            if e["path"] in seen_paths:
                continue
            seen_paths.add(e["path"])
            u = usage.setdefault(e["path"], {
                "id": e["id"], "path": e["path"], "filename": e["filename"],
                "category": e["category"], "bpm": e["bpm"], "key": e["key"],
                "selection_score": e["selection_score"],
                "scored_for_genre": e.get("scored_for_genre"),
                "roles": sorted(e.get("roles", [])),
                "tracks_using": [], "placements": 0,
            })
            n = sum(1 for p in placements if same_path(p["path"], e["path"]))
            if n:
                if t["number"] not in u["tracks_using"]:
                    u["tracks_using"].append(t["number"])
                u["placements"] += n
    for u in usage.values():
        u["tracks_using"].sort()
    return {"mode": pal.get("mode"), "genres": pal.get("genres", []),
            "entries": [usage[k] for k in sorted(usage)]}


def export_manifest(album: dict, out_root: str, name: str | None = None) -> str:
    """Write <album_root>/<name>_manifest.json with deterministic bytes."""
    slug = name or _slug(album["name"])
    path = os.path.join(out_root, f"{slug}_manifest.json")
    doc = build_manifest(album, out_root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    return path


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in (text or "album").lower())
    return "-".join(p for p in keep.split("-") if p) or "album"
