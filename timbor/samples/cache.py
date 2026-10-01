"""timbor.samples.cache — persistent SQLite sample index."""
from __future__ import annotations

import os
import sqlite3
import time

from .metadata import SampleMetadata

DEFAULT_DB = os.path.join("data", "samples.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    path TEXT PRIMARY KEY,
    filename TEXT,
    duration REAL, sample_rate INTEGER, channels INTEGER,
    size INTEGER, mtime REAL, fingerprint TEXT,
    bpm REAL, bpm_confidence REAL, key TEXT, root_note TEXT, key_confidence REAL,
    loudness REAL, rms REAL, peak REAL,
    spectral_centroid REAL, spectral_bandwidth REAL, spectral_rolloff REAL,
    sub_energy REAL, low_energy REAL, mid_energy REAL, high_energy REAL,
    transient_density REAL, zero_crossing_rate REAL, tonalness REAL,
    category TEXT, subcategory TEXT,
    genre_tags TEXT, energy REAL, is_loop INTEGER, is_one_shot INTEGER,
    classification_confidence REAL, file_tags TEXT,
    analyzed INTEGER, error TEXT,
    indexed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_category ON samples(category);
CREATE INDEX IF NOT EXISTS idx_samples_bpm ON samples(bpm);
CREATE INDEX IF NOT EXISTS idx_samples_duration ON samples(duration);
"""


class SampleIndex:
    DEFAULT_DB = DEFAULT_DB

    def __init__(self, db_path: str = DEFAULT_DB):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- fingerprint helpers -------------------------------------------------
    @staticmethod
    def fingerprint(size: int, mtime: float) -> str:
        return f"{size}:{int(mtime * 1000)}"

    def known_fingerprints(self) -> dict[str, str]:
        cur = self.conn.execute("SELECT path, fingerprint FROM samples")
        return {r["path"]: (r["fingerprint"] or "") for r in cur}

    # -- writes ---------------------------------------------------------------
    def upsert(self, m: SampleMetadata, analyzed: bool = False) -> None:
        row = m.to_row()
        row["analyzed"] = 1 if analyzed else 0
        row["indexed_at"] = time.time()
        cols = ", ".join(row.keys())
        qs = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO samples ({cols}) VALUES ({qs}) "
            f"ON CONFLICT(path) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in row)}",
            list(row.values()))
        self.conn.commit()

    def upsert_many(self, metas: list[SampleMetadata], analyzed_flags: list[bool]) -> None:
        now = time.time()
        rows = []
        for m, a in zip(metas, analyzed_flags):
            row = m.to_row()
            row["analyzed"] = 1 if a else 0
            row["indexed_at"] = now
            rows.append(row)
        if not rows:
            return
        cols = list(rows[0].keys())
        qs = ", ".join("?" for _ in cols)
        sets = ", ".join(f"{k}=excluded.{k}" for k in cols)
        self.conn.executemany(
            f"INSERT INTO samples ({', '.join(cols)}) VALUES ({qs}) "
            f"ON CONFLICT(path) DO UPDATE SET {sets}",
            [list(r.values()) for r in rows])
        self.conn.commit()

    def delete_paths(self, paths: list[str]) -> int:
        if not paths:
            return 0
        q = ",".join("?" for _ in paths)
        cur = self.conn.execute(f"DELETE FROM samples WHERE path IN ({q})", paths)
        self.conn.commit()
        return cur.rowcount

    # -- reads ----------------------------------------------------------------
    def get(self, path: str) -> SampleMetadata | None:
        cur = self.conn.execute("SELECT * FROM samples WHERE path = ?", (path,))
        r = cur.fetchone()
        return SampleMetadata.from_row(dict(r)) if r else None

    def analyzed_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) c FROM samples WHERE analyzed=1 AND error=''")
        return int(cur.fetchone()["c"])

    def by_category(self, category: str, analyzed_only: bool = True,
                    limit: int | None = None) -> list[SampleMetadata]:
        q = "SELECT * FROM samples WHERE category = ?"
        if analyzed_only:
            q += " AND analyzed=1 AND error=''"
        q += " ORDER BY classification_confidence DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [SampleMetadata.from_row(dict(r)) for r in self.conn.execute(q, (category,))]

    def query(self, category: str | None = None, min_duration: float | None = None,
              max_duration: float | None = None, bpm_lo: float | None = None,
              bpm_hi: float | None = None, is_loop: bool | None = None,
              analyzed_only: bool = True, limit: int = 200) -> list[SampleMetadata]:
        conds, args = [], []
        if analyzed_only:
            conds.append("analyzed=1 AND error=''")
        if category:
            conds.append("category = ?")
            args.append(category)
        if min_duration is not None:
            conds.append("duration >= ?")
            args.append(min_duration)
        if max_duration is not None:
            conds.append("duration <= ?")
            args.append(max_duration)
        if bpm_lo is not None:
            conds.append("bpm >= ?")
            args.append(bpm_lo)
        if bpm_hi is not None:
            conds.append("bpm <= ?")
            args.append(bpm_hi)
        if is_loop is not None:
            conds.append("is_loop = ?")
            args.append(1 if is_loop else 0)
        q = "SELECT * FROM samples"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY classification_confidence DESC LIMIT ?"
        args.append(int(limit))
        return [SampleMetadata.from_row(dict(r)) for r in self.conn.execute(q, args)]

    def stats(self) -> dict:
        out: dict = {}
        cur = self.conn.execute("SELECT COUNT(*) c FROM samples")
        out["total"] = int(cur.fetchone()["c"])
        cur = self.conn.execute("SELECT COUNT(*) c FROM samples WHERE analyzed=1 AND error=''")
        out["analyzed"] = int(cur.fetchone()["c"])
        cur = self.conn.execute("SELECT COUNT(*) c FROM samples WHERE error != ''")
        out["errors"] = int(cur.fetchone()["c"])
        out["categories"] = {
            r["category"]: int(r["c"]) for r in self.conn.execute(
                "SELECT category, COUNT(*) c FROM samples WHERE analyzed=1 AND error='' "
                "GROUP BY category ORDER BY c DESC")
        }
        out["keys"] = {
            "high": int(self.conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE key_confidence >= 0.6").fetchone()["c"]),
            "medium": int(self.conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE key_confidence > 0 AND key_confidence < 0.6"
            ).fetchone()["c"]),
            "none": int(self.conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE key IS NULL OR key=''").fetchone()["c"]),
        }
        bands = ((70, 90), (90, 120), (120, 140), (140, 160), (160, 180), (180, 220))
        out["bpm_bands"] = {}
        for lo, hi in bands:
            c = int(self.conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE bpm >= ? AND bpm < ?",
                (lo, hi)).fetchone()["c"])
            if c:
                out["bpm_bands"][f"{lo}-{hi}"] = c
        return out
