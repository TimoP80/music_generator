"""timbor.album — EP/album generation with shared musical DNA (phase 5).

An orchestration layer above the authoritative single-song pipeline:

    AlbumConfig → AlbumDNA → lineage + palette → per-track Plans
    → independent projects (phase-4 exports) → validation + manifest

Public API:
    AlbumConfig, AlbumDNA, derive_seed     (dna)
    build_lineage, base_motif              (motif)
    build_palette, palette_summary         (palette)
    make_plan, assign_bpm                  (planning)
    generate_album, regen_track            (generator)
    validate_album, format_album_report    (validation)
    save_album, load_album, album_json     (serialization)
    build_manifest, export_manifest        (manifest)
"""
from .dna import AlbumConfig, AlbumDNA, derive_seed, crc32_json
from .motif import base_motif, build_lineage, motif_fingerprint
from .palette import build_palette, palette_summary, palette_roles_for
from .planning import make_plan, assign_bpm, track_identity
from .generator import generate_album, regen_track, build_dna
from .validation import (validate_album, format_album_report,
                         revalidate_album)
from .serialization import (album_json, save_album, load_album,
                            ALBUM_FORMAT, ALBUM_VERSION)
from .manifest import build_manifest, export_manifest

__all__ = [
    "AlbumConfig", "AlbumDNA", "derive_seed", "crc32_json",
    "base_motif", "build_lineage", "motif_fingerprint",
    "build_palette", "palette_summary", "palette_roles_for",
    "make_plan", "assign_bpm", "track_identity",
    "generate_album", "regen_track", "build_dna",
    "validate_album", "format_album_report", "revalidate_album",
    "album_json", "save_album", "load_album", "ALBUM_FORMAT", "ALBUM_VERSION",
    "build_manifest", "export_manifest",
]
