"""timbor.album.generator — EP/album orchestration (spec §13–§14).

Pipeline (an orchestration layer ABOVE the authoritative single-song flow):

    AlbumConfig + master seed
        ↓
    AlbumDNA (shared identity layer)
        ↓
    motif lineage (shared family) + palette (built once from the index)
        ↓
    per-track Plan (derived seeds, BPM neighborhood, DNA key)
        ↓
    render_track (UNCHANGED single-song pipeline)
        → independent project per track (stems + project.json + exports)
        ↓
    validation + album.json

Nothing inside a track depends on mutable shared state: seeds derive by
stable label, the lineage/palette derive from (seed, library), so
regenerating the album — or any single track — reproduces identical
children (spec §4, §17).
"""
from __future__ import annotations

import json
import os

from .dna import AlbumConfig, AlbumDNA, derive_seed
from .motif import base_motif, build_lineage, rhythm_fingerprint
from .palette import build_palette
from .planning import make_plan, track_identity, genre_pack, default_bpm_center
from . import serialization
from ..timeline.serialization import load_project
from ..timeline.project import write_project_directory

BUSES = ("drums", "bass", "mel", "fx", "samples")


def build_dna(cfg: AlbumConfig) -> AlbumDNA:
    """Derive the shared identity layer from config + seed (spec §5)."""
    genres = [g for g in (cfg.genres or ["rave"])][: cfg.tracks]
    while len(genres) < cfg.tracks:
        genres.append(genres[-1] if genres else "rave")
    anchor = genres[0]

    center = float(cfg.bpm_center) if cfg.bpm_center else \
        default_bpm_center(cfg, anchor)

    import numpy as np
    rng = np.random.default_rng(derive_seed(cfg.seed, "album-dna"))

    from ..theory import NOTE_INDEX
    mode = cfg.mode or ("minor" if cfg.mood in (None, "dark") else "major")
    mode = {"minor": "minor", "major": "major", "phrygian": "phrygian",
            "dorian": "dorian", "harmonic_minor": "harmonic_minor",
            "melodic_minor": "melodic_minor", "lydian": "lydian"}.get(mode, "minor")
    root = (cfg.key or "A").strip().capitalize()
    root = root if root in NOTE_INDEX else "A"

    # harmonic vocabulary: a progression family from the anchor genre's own
    # pool (same table engine.SongPlan uses), chosen deterministically
    fam_pool = {
        "gabber": ("dark_phryg", "anthem", "epic"),
        "frenchcore": ("dark_phryg", "anthem", "epic"),
        "uk_hardcore": ("anthem", "trance_roller", "epic"),
        "freeform": ("epic", "dramatic", "trance_epic"),
        "jcore": ("jcore_bounce", "jcore_pop", "anthem"),
        "dnb": ("amen_dark", "anthem", "dramatic"),
        "jungle": ("amen_dark", "anthem", "sus_engine"),
        "hard_house": ("hardhouse", "rave_loop", "anthem_b"),
        "trance": ("trance_roller", "trance_epic", "uplifting"),
        "acid_trance": ("sus_engine", "trance_roller", "uplifting"),
        "big_beat": ("big_beat", "anthem", "amen_dark"),
        "rave": ("rave_loop", "anthem", "epic"),
    }
    fam = fam_pool.get(anchor, ("anthem", "epic"))[int(rng.integers(0, 3))]

    # album-level Motif A in the DNA's key/scale; per-track variants derive
    # from this single source (spec §6)
    from .motif import genre_phrase_steps
    m, contour = base_motif(cfg.seed, mode, root,
                            steps=genre_phrase_steps(anchor))

    pack = genre_pack(anchor)
    return AlbumDNA(
        root_key=root, mode=mode, bpm_center=center,
        bpm_range=float(cfg.bpm_range),
        harmonic_family=fam, motif_family=m, motif_contour=contour,
        rhythm_fingerprint=rhythm_fingerprint(m),
        signature_instruments={
            "bass": pack["bass"], "lead": pack["lead"],
            "kick": pack["kick"], "drums": pack["drums"],
        },
        era=cfg.era or pack["era"], mood=cfg.mood or "dark",
        authenticity=cfg.authenticity, palette_mode=cfg.palette_mode,
        genre_anchor=anchor, seed=cfg.seed)


def _inject_album_metadata(project_json_path: str, meta: dict) -> None:
    """Attach album lineage metadata to a written project.json (additive)."""
    with open(project_json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    md = dict(doc.get("metadata") or {})
    md["album"] = meta
    doc["metadata"] = md
    with open(project_json_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)


def _export_track(project: dict, root: str, name: str,
                  album_root: str | None = None) -> dict:
    """Run the phase-4 export suite for one track (deterministic bytes).

    Paths are recorded album-relative with forward slashes so album.json is
    portable and byte-stable across runs/directories.
    """
    from ..export import export_midi, export_manifest
    from ..export.reaper import export_reaper
    from ..export.ableton import export_ableton
    raw = {}
    raw["midi"] = export_midi(project, os.path.join(root, "midi"), name=name)
    raw["manifest"] = export_manifest(project, os.path.join(root, "export"),
                                      name=name)
    raw["reaper"] = export_reaper(project, root)
    raw["ableton"] = export_ableton(project, root)
    if album_root:
        out = {}
        for k, v in raw.items():
            try:
                out[k] = os.path.relpath(v, album_root).replace(os.sep, "/")
            except ValueError:      # different drive on Windows
                out[k] = v.replace(os.sep, "/")
        return out
    return raw


def _track_dirname(ident: dict) -> str:
    """'01' for default titles, '01-<slug>' when a real title is given."""
    if ident["title"] == f"Track {ident['number']}":
        return ident["number"]
    return f"{ident['number']}-{ident['slug']}"


def generate_album(cfg: AlbumConfig, out_root: str, render: bool = True,
                   db_path: str | None = None) -> dict:
    """Generate the full album under out_root/<album-slug>/.

    render=False (spec §25: inspection never regenerates audio) produces the
    deterministic plan-level documents only — no audio, no project dirs.
    db_path overrides the sample index database (defaults to data/samples.db).
    """
    errs = cfg.validate()
    if errs:
        raise ValueError(f"invalid album config: {errs}")

    dna = build_dna(cfg)
    genres = [cfg.genres[i] if i < len(cfg.genres) else dna.genre_anchor
              for i in range(cfg.tracks)]

    # ---- shared DNA artifacts, built once (spec §6, §9) -------------------
    lineage = build_lineage(cfg.seed, dna.motif_family, genres,
                            strategy=cfg.motif_strategy)
    palette: dict = {"mode": cfg.palette_mode, "genres": sorted(set(genres)),
                     "roles": {}}
    sample_index = None
    if cfg.shared_palette and cfg.sample_dir and cfg.sample_mode != "off":
        from ..samples.cache import SampleIndex
        sample_index = SampleIndex(db_path=db_path) if db_path else SampleIndex()
        try:
            palette = build_palette(cfg, sample_index, genres)
        finally:
            if not render:      # in render mode the loop still needs the index
                sample_index.close()
                sample_index = None

    album_slug = _album_slug(cfg.name)
    album_root = os.path.join(out_root, album_slug)

    track_records: list[dict] = []
    project_dicts: list[dict | None] = []
    try:
        for i in range(cfg.tracks):
            cfg_t = cfg.track_config(i)
            ident = track_identity(cfg_t, i)
            plan = make_plan(cfg_t, dna, i, cfg_t.sample_dir)
            plan.motif_override = lineage[i][0]
            plan.album_palette = palette if cfg.shared_palette else None

            record: dict = {
                "number": ident["number"],
                "title": ident["title"],
                "directory": None,
                "project": None,
                "genre": plan.genre,
                "seed": plan.seed,
                "bpm": plan.bpm,
                "key": f"{dna.root_key} {dna.mode}",
                "bars": plan.bars_limit,
                "lineage": lineage[i][1],
                "exports": None,
            }
            project_dict = None

            if render:
                from ..engine import render_track
                from ..render import stereoize, master

                song, buses, qc = render_track(plan, sample_index=sample_index)
                l, r = stereoize(buses)
                l, r = master(l, r)

                t_root = os.path.join(album_root, _track_dirname(ident))
                pj = write_project_directory(
                    os.path.join(t_root, "audio", "master.wav"),
                    song, qc, buses, l, r)
                _inject_album_metadata(pj, {
                    "name": cfg.name, "position": i + 1, "of": cfg.tracks,
                    "lineage": lineage[i][1],
                })
                project_dict = load_project(pj)
                record.update({
                    # album-relative forward-slash paths (consistent with exports)
                    "directory": os.path.relpath(t_root, album_root).replace(os.sep, "/"),
                    "project": os.path.relpath(pj, album_root).replace(os.sep, "/"),
                    "bpm": song.bpm,
                    "duration_seconds": round(song.duration(), 2),
                    "samples_used": len(project_dict.get("samples", [])),
                })
                if getattr(cfg_t, "exports", True):
                    record["exports"] = _export_track(
                        project_dict, t_root,
                        name=_track_dirname(ident) or ident["slug"],
                        album_root=album_root)

            track_records.append(record)
            project_dicts.append(project_dict)
    finally:
        if sample_index is not None:
            sample_index.close()

    # ---- continuity validation (spec §15) --------------------------------
    from .validation import validate_album
    validation = validate_album(cfg, dna, track_records, project_dicts,
                                palette=palette)

    album_doc = serialization.album_json(cfg, dna, track_records, palette,
                                         validation)
    serialization.save_album(os.path.join(album_root, "album.json"), album_doc)
    album_doc["root"] = album_root
    return album_doc


def regen_track(album_path: str, position: int,
                db_path: str | None = None) -> dict:
    """Regenerate ONE track of a saved album (spec §18).

    Seeds, DNA, lineage and palette all derive by stable labels, so the
    rebuilt track is identical to the original render — without touching
    sibling projects.
    """
    album = serialization.load_album(album_path)
    cfg = serialization.config_of(album)
    dna = serialization.dna_of(album)
    if not 0 <= position < cfg.tracks:
        raise ValueError(f"track position {position} out of range")
    palette = album.get("palette", {})
    i = position
    cfg_t = cfg.track_config(i)
    ident = track_identity(cfg_t, i)
    plan = make_plan(cfg_t, dna, i, cfg_t.sample_dir)
    plan.motif_override = album["tracks"][i]["lineage"]["motif"]
    plan.album_palette = palette if cfg.shared_palette else None

    from ..engine import render_track
    from ..render import stereoize, master
    sample_index = None
    if cfg.sample_dir and cfg.sample_mode != "off":
        from ..samples.cache import SampleIndex
        sample_index = SampleIndex(db_path=db_path) if db_path else SampleIndex()
    try:
        song, buses, qc = render_track(plan, sample_index=sample_index)
    finally:
        if sample_index is not None:
            sample_index.close()
    l, r = stereoize(buses)
    l, r = master(l, r)

    album_root = os.path.dirname(os.path.abspath(album_path))
    t_root = os.path.join(album_root, _track_dirname(ident))
    pj = write_project_directory(
        os.path.join(t_root, "audio", "master.wav"), song, qc, buses, l, r)
    _inject_album_metadata(pj, {
        "name": cfg.name, "position": i + 1, "of": cfg.tracks,
        "lineage": album["tracks"][i]["lineage"],
    })
    project_dict = load_project(pj)
    exports = _export_track(project_dict, t_root,
                            name=_track_dirname(ident) or ident["slug"],
                            album_root=album_root)
    return {"project": pj, "exports": exports, "record": album["tracks"][i]}


def _album_slug(name: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in (name or "album").lower())
    slug = "-".join(p for p in keep.split("-") if p)
    return slug or "album"
