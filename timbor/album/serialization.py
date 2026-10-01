"""timbor.album.serialization — album.json / DNA persistence (spec §16).

The album config, DNA, palette, per-track records and validation results are
persisted in one versioned album.json (sibling of the per-track
project.json). Like project.json, the timestamp is metadata only: generation
is deterministic in (config, seed, library).
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time

from .dna import AlbumConfig, AlbumDNA

ALBUM_FORMAT = "timbor-album"
ALBUM_VERSION = 1


def _generator_meta(seed: int) -> dict:
    return {
        "name": "TIMBOR",
        "version": _timbor_version(),
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "sample_rate": 44100,
        "seed": seed,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _timbor_version() -> str:
    from ..timeline.serialization import TIMBOR_VERSION
    return TIMBOR_VERSION


def album_json(cfg: AlbumConfig, dna: AlbumDNA, tracks: list[dict],
               palette: dict, validation: dict) -> dict:
    """Assemble the full album document."""
    return {
        "format": ALBUM_FORMAT,
        "version": ALBUM_VERSION,
        "name": cfg.name,
        "generator": _generator_meta(cfg.seed),
        "config": cfg.to_json(),
        "dna": dna.to_json(),
        "dna_hash": dna.dna_hash(),
        "tracks": tracks,
        "palette": palette,
        "validation": validation,
    }


def save_album(path: str, album: dict) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(album, f, indent=1)
    return path


def load_album(path: str) -> dict:
    """Load album.json; rejects unknown formats/versions like load_project."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != ALBUM_FORMAT:
        raise ValueError(f"not a TIMBOR album (format={data.get('format')!r})")
    v = data.get("version")
    if not isinstance(v, int) or v > ALBUM_VERSION or v < ALBUM_VERSION:
        raise ValueError(f"unsupported album version: {v!r}")
    return data


def config_of(album: dict) -> AlbumConfig:
    return AlbumConfig.from_json(album["config"])


def dna_of(album: dict) -> AlbumDNA:
    return AlbumDNA.from_json(album["dna"])
