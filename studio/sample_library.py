"""Studio-facing access to TIMBOR's existing sample index and scanner.

This module deliberately delegates decoding, analysis and classification to
``timbor.samples``. It adds only library registration, root-scoped incremental
indexing, paginated SQL search, stable opaque sample IDs and safe resolution.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import threading
import time
from typing import Any

from timbor.samples.cache import SampleIndex
from timbor.samples.metadata import SampleMetadata
from timbor.samples.scanner import scan_directory

SUPPORTED_FORMATS = {"wav", "aiff", "flac", "mp3"}
SORTS = {
    "name": "lower(filename) ASC, path ASC",
    "bpm": "bpm IS NULL, bpm ASC, path ASC",
    "key": "key IS NULL, key ASC, path ASC",
    "duration": "duration ASC, path ASC",
    "type": "category ASC, filename COLLATE NOCASE ASC, path ASC",
    "match": "classification_confidence DESC, path ASC",
    "indexed": "indexed_at DESC, path ASC",
    "path": "path COLLATE NOCASE ASC, path ASC",
}


def canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((canonical(path), canonical(root))) == canonical(root)
    except (ValueError, OSError):
        return False


def stable_root_id(path: str) -> str:
    return hashlib.sha256(canonical(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]


class SampleLibraryService:
    def __init__(self, project_root: str, db_path: str | None = None,
                 registry_path: str | None = None):
        self.project_root = os.path.realpath(project_root)
        self.db_path = db_path or os.path.join(project_root, "data", "samples.db")
        self.registry_path = registry_path or os.path.join(
            project_root, "data", "sample_libraries.json")
        self._lock = threading.RLock()
        self._scan: dict[str, Any] = {"state": "IDLE", "root_id": None,
            "discovered": 0, "changed": 0, "unchanged": 0, "deleted": 0,
            "analyzed": 0, "failed": 0, "current_file": "", "progress": 0.0}
        self._thread: threading.Thread | None = None
        self.peaks_fn = None
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        idx = SampleIndex(self.db_path)
        try:
            columns = {r[1] for r in idx.conn.execute("PRAGMA table_info(samples)")}
            if "content_hash" not in columns:
                idx.conn.execute("ALTER TABLE samples ADD COLUMN content_hash TEXT")
                idx.conn.commit()
        finally:
            idx.close()

    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _read_registry(self) -> list[dict]:
        registry_exists = os.path.isfile(self.registry_path)
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            roots = data.get("roots", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            roots = []
        valid = []
        for row in roots:
            path = row.get("path") if isinstance(row, dict) else None
            if not path or not os.path.isdir(path):
                continue
            real = os.path.realpath(os.path.abspath(path))
            valid.append({"id": stable_root_id(real), "path": real,
                          "name": row.get("name") or os.path.basename(real)})
        if not valid and not registry_exists:
            # Surface an existing indexed root once. Removal writes an explicit
            # empty registry, preventing silent re-addition on later requests.
            demo = os.path.join(self.project_root, "data", "demo_library")
            if os.path.isdir(demo) and self._has_rows_under(demo):
                real = os.path.realpath(demo)
                valid.append({"id": stable_root_id(real), "path": real,
                              "name": os.path.basename(real)})
        return valid

    def _write_registry(self, roots: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        temp = self.registry_path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "roots": [
                    {"path": r["path"], "name": r["name"]} for r in roots]}, f, indent=2)
            os.replace(temp, self.registry_path)
        finally:
            try:
                if os.path.exists(temp): os.remove(temp)
            except OSError:
                pass

    def roots(self) -> list[dict]:
        roots = self._read_registry()
        return [{**r, "stats": self.root_stats(r["path"]),
                 "last_scan": self._scan.get("ended")
                    if self._scan.get("root_id") == r["id"] else None}
                for r in roots]

    def add_root(self, path: str) -> dict:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("library path is required")
        # Expand user/home and environment, then canonicalize before storing.
        expanded = os.path.expandvars(os.path.expanduser(path.strip()))
        real = os.path.realpath(os.path.abspath(expanded))
        if not os.path.isdir(real):
            raise ValueError("library path must be an existing directory")
        roots = self._read_registry()
        rid = stable_root_id(real)
        if any(r["id"] == rid for r in roots):
            return next(r for r in self.roots() if r["id"] == rid)
        roots.append({"id": rid, "path": real,
                      "name": os.path.basename(real.rstrip(os.sep)) or real})
        roots.sort(key=lambda r: (r["path"].casefold(), r["path"]))
        self._write_registry(roots)
        return next(r for r in self.roots() if r["id"] == rid)

    def remove_root(self, root_id: str) -> bool:
        with self._lock:
            if (self._scan.get("state") in ("SCANNING", "ANALYZING")
                    and self._scan.get("root_id") == root_id):
                raise RuntimeError("cannot remove a library while it is scanning")
        roots = self._read_registry()
        kept = [r for r in roots if r["id"] != root_id]
        if len(kept) == len(roots):
            return False
        self._write_registry(kept)  # index rows and physical files are retained
        return True

    def _root(self, root_id: str) -> dict:
        root = next((r for r in self._read_registry() if r["id"] == root_id), None)
        if root is None:
            raise KeyError(root_id)
        return root

    def _has_rows_under(self, root: str) -> bool:
        idx = SampleIndex(self.db_path)
        try:
            full = os.path.realpath(root)
            prefix = full.rstrip(os.sep) + os.sep
            return idx.conn.execute(
                "SELECT 1 FROM samples WHERE lower(path)=lower(?) OR "
                "lower(substr(path,1,?))=lower(?) LIMIT 1",
                (full, len(prefix), prefix)).fetchone() is not None
        finally:
            idx.close()

    def _root_sql(self, roots: list[dict]) -> tuple[str, list]:
        clauses, args = [], []
        for r in roots:
            prefix = r["path"].rstrip(os.sep) + os.sep
            clauses.append("(lower(path)=lower(?) OR lower(substr(path,1,?))=lower(?))")
            args.extend((r["path"], len(prefix), prefix))
        return ("(" + " OR ".join(clauses) + ")", args) if clauses else ("0", [])

    def root_stats(self, root: str) -> dict:
        root = os.path.realpath(root)
        prefix = root.rstrip(os.sep) + os.sep
        scope = "(lower(path)=lower(?) OR lower(substr(path,1,?))=lower(?))"
        args = (root, len(prefix), prefix)
        idx = SampleIndex(self.db_path)
        try:
            return self._root_stats(idx, root, prefix, scope, args)
        finally:
            idx.close()

    @staticmethod
    def _root_stats(idx: SampleIndex, root: str, prefix: str,
                    scope: str, args: tuple) -> dict:
        total = int(idx.conn.execute(
            f"SELECT COUNT(*) FROM samples WHERE {scope}", args).fetchone()[0])
        analyzed = int(idx.conn.execute(
            f"SELECT COUNT(*) FROM samples WHERE {scope} AND analyzed=1 AND error=''",
            args).fetchone()[0])
        errors = int(idx.conn.execute(
            f"SELECT COUNT(*) FROM samples WHERE {scope} AND error!='' AND error!='no-decoder'",
            args).fetchone()[0])
        decoder_required = int(idx.conn.execute(
            f"SELECT COUNT(*) FROM samples WHERE {scope} AND error='no-decoder'",
            args).fetchone()[0])
        formats: dict[str, int] = {}
        # Group extensions in SQL by the formats the scanner recognizes;
        # this avoids pulling every path row into Python for large libraries.
        for name, suffixes in (("wav", (".wav", ".wave")),
                               ("aiff", (".aif", ".aiff")),
                               ("flac", (".flac",)), ("mp3", (".mp3",))):
            clauses = " OR ".join("lower(path) LIKE ?" for _ in suffixes)
            count = int(idx.conn.execute(
                f"SELECT COUNT(*) FROM samples WHERE {scope} AND ({clauses})",
                (*args, *("%" + s for s in suffixes))).fetchone()[0])
            if count:
                formats[name] = count
        categories = {row[0] or "unknown": int(row[1]) for row in idx.conn.execute(
            f"SELECT category,COUNT(*) FROM samples WHERE {scope} "
            "AND analyzed=1 AND error='' GROUP BY category", args)}
        duplicate_prefix = os.path.realpath(root).rstrip(os.sep) + os.sep
        duplicate_count = int(idx.conn.execute(
            "SELECT COUNT(*) FROM (SELECT content_hash FROM samples "
            "WHERE content_hash IS NOT NULL AND "
            "(lower(path)=lower(?) OR lower(substr(path,1,?))=lower(?)) "
            "GROUP BY content_hash HAVING COUNT(*)>1)",
            (root, len(duplicate_prefix), duplicate_prefix)).fetchone()[0])
        return {"files": total, "analyzed": analyzed,
                "pending": max(0, total - analyzed - errors - decoder_required),
                "errors": errors, "decoder_required": decoder_required,
                "formats": formats, "categories": categories,
                "duplicate_groups": duplicate_count}

    def identity(self, path: str, roots: list[dict] | None = None) -> str | None:
        roots = roots if roots is not None else self._read_registry()
        matching = [r for r in roots if within(path, r["path"])]
        if not matching:
            return None
        root = max(matching, key=lambda r: len(r["path"]))
        rel = os.path.relpath(os.path.realpath(path), root["path"]).replace(os.sep, "/")
        raw = json.dumps([root["id"], rel], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def resolve_id(self, sample_id: str) -> tuple[str, SampleMetadata]:
        try:
            raw = base64.urlsafe_b64decode(sample_id + "=" * (-len(sample_id) % 4))
            root_id, rel = json.loads(raw)
        except Exception as exc:
            raise ValueError("invalid sample id") from exc
        if not isinstance(rel, str) or not isinstance(root_id, str):
            raise ValueError("invalid sample id")
        root = self._root(root_id)
        rel = rel.replace("\\", "/")
        if os.path.isabs(rel) or any(part in ("", ".", "..") for part in rel.split("/")):
            raise ValueError("invalid sample path")
        full = os.path.realpath(os.path.join(root["path"], *rel.split("/")))
        if not within(full, root["path"]):
            raise ValueError("sample path escapes its registered library")
        if not os.path.isfile(full):
            raise FileNotFoundError(rel)
        idx = SampleIndex(self.db_path)
        try:
            meta = idx.get(full)
        finally:
            idx.close()
        if meta is None:
            raise FileNotFoundError("sample is not indexed")
        return full, meta

    def _row_json(self, row: dict, roots: list[dict]) -> dict:
        meta = SampleMetadata.from_row(row)
        matching = [r for r in roots if within(meta.path, r["path"])]
        root = max(matching, key=lambda r: len(r["path"])) if matching else None
        ident = self.identity(meta.path, roots)
        ext = os.path.splitext(meta.path)[1].lower()
        fmt = {".wave": "wav", ".aif": "aiff", ".aiff": "aiff"}.get(ext, ext.lstrip("."))
        status = "ANALYZED" if meta.analyzed and not meta.error else (
            "DECODER REQUIRED" if meta.error == "no-decoder" else
            "ERROR" if meta.error else "PENDING")
        analysis_status = "DECODER REQUIRED" if meta.error == "no-decoder" else status
        return {"id": ident, "filename": meta.filename, "path": meta.path,
            "library_id": root["id"] if root else None,
            "library": root["name"] if root else "", "format": fmt,
            "duration": meta.duration, "sample_rate": meta.sample_rate,
            "channels": meta.channels, "bpm": meta.bpm,
            "bpm_confidence": meta.bpm_confidence, "key": meta.key,
            "key_confidence": meta.key_confidence, "category": meta.category,
            "subcategory": meta.subcategory, "genre_tags": meta.genre_tags,
            "file_tags": meta.file_tags, "tags": sorted(set(meta.genre_tags + meta.file_tags)),
            "energy": meta.energy, "is_loop": meta.is_loop,
            "is_one_shot": meta.is_one_shot,
            "classification_confidence": meta.classification_confidence,
            "spectral_centroid": meta.spectral_centroid,
            "spectral_bandwidth": meta.spectral_bandwidth,
            "spectral_rolloff": meta.spectral_rolloff,
            "sub_energy": meta.sub_energy, "low_energy": meta.low_energy,
            "mid_energy": meta.mid_energy, "high_energy": meta.high_energy,
            "transient_density": meta.transient_density,
            "tonalness": meta.tonalness, "size": meta.size,
            "mtime": meta.mtime, "indexed_at": row.get("indexed_at"),
            "analyzed": meta.analyzed, "status": status,
            "analysis_status": analysis_status, "error": meta.error,
            "duplicate_group": (hashlib.sha256(row["content_hash"].encode()).hexdigest()[:16]
                                if row.get("content_hash") else None),
            "preview_supported": fmt == "wav"}

    def search(self, params: dict) -> dict:
        roots = self._read_registry()
        root_sql, args = self._root_sql(roots)
        where = [root_sql]
        if params.get("q"):
            term = str(params["q"]).strip().lower()
            if len(term) > 200:
                raise ValueError("search term too long")
            where.append("(instr(lower(filename),?)>0 OR instr(lower(path),?)>0 "
                         "OR instr(lower(coalesce(category,'')),?)>0 "
                         "OR instr(lower(coalesce(subcategory,'')),?)>0 "
                         "OR instr(lower(coalesce(genre_tags,'')),?)>0 "
                         "OR instr(lower(coalesce(file_tags,'')),?)>0 "
                         "OR instr(cast(bpm as text),?)>0 OR instr(lower(coalesce(key,'')),?)>0)")
            args.extend([term] * 8)
        filters = (("category", "lower(category) = lower(?)"),
                   ("key", "lower(key) = lower(?)"))
        for key, sql in filters:
            value = params.get(key)
            if value and key == "key":
                where.append(sql); args.append(str(value).strip())
            elif value:
                where.append(sql); args.append(value)
        if params.get("library"):
            root = next((r for r in roots if r["id"] == params["library"]), None)
            if not root:
                raise ValueError("unknown library filter")
            prefix = root["path"].rstrip(os.sep) + os.sep
            where.append("(lower(path)=lower(?) OR lower(substr(path,1,?))=lower(?))")
            args.extend((root["path"], len(prefix), prefix))
        ranges = (("bpm_min", "bpm >= ?"), ("bpm_max", "bpm <= ?"),
                  ("duration_min", "duration >= ?"), ("duration_max", "duration <= ?"))
        for key, sql in ranges:
            if params.get(key) not in (None, ""):
                try:
                    val = float(params[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid {key}") from exc
                if not math.isfinite(val):
                    raise ValueError(f"invalid {key}")
                where.append(sql); args.append(val)
        if params.get("tag"):
            tag = str(params["tag"]).lower().strip()
            if len(tag) > 100:
                raise ValueError("tag filter too long")
            where.append("(instr(lower(coalesce(genre_tags,'')),?)>0 OR "
                         "instr(lower(coalesce(file_tags,'')),?)>0)")
            args.extend((tag, tag))
        status = params.get("status")
        if status == "analyzed": where.append("analyzed=1 AND error='' ")
        elif status == "pending": where.append("analyzed=0 AND (error='' OR error='no-decoder') ")
        elif status == "error": where.append("error != '' AND error != 'no-decoder'")
        elif status == "decoder": where.append("error='no-decoder'")
        elif status: raise ValueError("invalid analysis status")
        try:
            page = int(params.get("page", 1)); size = int(params.get("page_size", 50))
        except (TypeError, ValueError) as exc:
            raise ValueError("page and page_size must be integers") from exc
        if page < 1 or size < 1 or size > 100:
            raise ValueError("page must be >=1 and page_size must be 1..100")
        order = SORTS.get(params.get("sort", "name"))
        if not order: raise ValueError("invalid sort")
        clause = " AND ".join(where)
        idx = SampleIndex(self.db_path)
        try:
            total = int(idx.conn.execute(f"SELECT COUNT(*) FROM samples WHERE {clause}", args).fetchone()[0])
            cur = idx.conn.execute(f"SELECT * FROM samples WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                                   [*args, size, (page - 1) * size])
            rows = [self._row_json(dict(r), roots) for r in cur.fetchall()]
        finally:
            idx.close()
        return {"page": page, "page_size": size, "total": total,
                "pages": (total + size - 1) // size, "results": rows,
                "libraries": [{"id": r["id"], "name": r["name"]} for r in roots]}

    def detail(self, sample_id: str) -> dict:
        full, meta = self.resolve_id(sample_id)
        idx = SampleIndex(self.db_path)
        try:
            row = idx.conn.execute("SELECT * FROM samples WHERE path=?", (full,)).fetchone()
            if row is None:
                raise FileNotFoundError("sample is not indexed")
            return self._row_json(dict(row), self._read_registry())
        finally:
            idx.close()

    def lookup_path(self, path: str) -> dict | None:
        """Look up an already-indexed path only if it belongs to a registered root."""
        roots = self._read_registry()
        if not isinstance(path, str) or not path:
            return None
        full = os.path.realpath(path if os.path.isabs(path)
                                else os.path.join(self.project_root, path))
        if not any(within(full, r["path"]) for r in roots):
            return None
        idx = SampleIndex(self.db_path)
        try:
            row = idx.conn.execute("SELECT * FROM samples WHERE path=? COLLATE NOCASE",
                                   (full,)).fetchone()
            return self._row_json(dict(row), roots) if row else None
        finally:
            idx.close()

    def scan_status(self) -> dict:
        with self._lock:
            return dict(self._scan)

    def start_scan(self, root_id: str) -> dict:
        root = self._root(root_id)
        with self._lock:
            if self._scan.get("state") in ("SCANNING", "ANALYZING"):
                raise RuntimeError("a library scan is already running")
            self._scan = {"state": "SCANNING", "root_id": root_id,
                "root": root["path"], "discovered": 0, "changed": 0,
                "unchanged": 0, "deleted": 0, "analyzed": 0, "failed": 0,
                "current_file": "", "progress": 0.0, "started": time.time()}
            self._thread = threading.Thread(target=self._scan_worker,
                args=(root,), daemon=True, name="timbor-sample-scan")
            self._thread.start()
            return dict(self._scan)

    def _scan_worker(self, root: dict) -> None:
        idx = None
        try:
            from timbor.samples.analyzer import analyze_file
            from timbor.samples.classifier import classify
            files = scan_directory(root["path"], follow_symlinks=False)
            # Never index symlink/junction aliases, even if their targets happen
            # to remain inside the root. This keeps identity stable and avoids
            # scanning cycles or platform-specific junction surprises.
            is_junction = getattr(os.path, "isjunction", lambda _path: False)
            files = [f for f in files
                     if not os.path.islink(f.path) and
                     not is_junction(f.path) and within(f.path, root["path"])]
            idx = SampleIndex(self.db_path)
            prefix = root["path"].rstrip(os.sep) + os.sep
            scoped_rows = idx.conn.execute(
                "SELECT path,fingerprint,content_hash FROM samples "
                "WHERE lower(path)=lower(?) OR lower(substr(path,1,?))=lower(?)",
                (root["path"], len(prefix), prefix)).fetchall()
            known = {os.path.normcase(os.path.realpath(r["path"])):
                     (r["path"], r["fingerprint"] or "", r["content_hash"])
                     for r in scoped_rows
                     if within(r["path"], root["path"])}
            by_path = {os.path.normcase(os.path.realpath(f.path)): f for f in files}
            pending = []
            unchanged = 0
            for f in files:
                key = os.path.normcase(os.path.realpath(f.path))
                old = known.get(key, (None, None, None))[1]
                fp = SampleIndex.fingerprint(f.size, f.mtime)
                if old == fp:
                    unchanged += 1
                else:
                    pending.append(f)
            gone = [v[0] for key, v in known.items() if key not in by_path]
            hash_pending = [(f.path, known[os.path.normcase(os.path.realpath(f.path))][2])
                            for f in files
                            if os.path.normcase(os.path.realpath(f.path)) in known
                            and not known[os.path.normcase(os.path.realpath(f.path))][2]]
            with self._lock:
                self._scan.update({"state": "ANALYZING", "discovered": len(files),
                    "changed": len(pending), "unchanged": unchanged,
                    "deleted": len(gone), "current_file": ""})
            for start in range(0, len(gone), 500):
                idx.delete_paths(gone[start:start + 500])
            analyzed = failed = 0
            metas: list[SampleMetadata] = []
            flags: list[bool] = []
            hashes: list[tuple[str, str]] = []
            total_work = max(1, len(pending) + len(hash_pending))
            work_done = 0

            def flush_batch() -> None:
                if metas:
                    idx.upsert_many(metas, flags)
                    metas.clear(); flags.clear()
                if hashes:
                    idx.conn.executemany(
                        "UPDATE samples SET content_hash=? WHERE path=?",
                        [(digest, path) for path, digest in hashes])
                    idx.conn.commit()
                    hashes.clear()

            # Populate missing content hashes once; this detects exact duplicate
            # files without re-running analysis for unchanged index entries.
            for path, _old_hash in hash_pending:
                try:
                    hashes.append((path, self._sha256(path)))
                except OSError:
                    pass
                work_done += 1
                if len(hashes) >= 250:
                    flush_batch()
                with self._lock:
                    self._scan["current_file"] = path
                    self._scan["progress"] = work_done / total_work

            for f in pending:
                with self._lock:
                    self._scan["current_file"] = f.path
                    self._scan["progress"] = work_done / total_work
                try:
                    meta = analyze_file(f.path, f.format)
                    if not meta.error or meta.error == "no-decoder":
                        classify(meta)
                    if meta.analyzed and not meta.error:
                        analyzed += 1
                except Exception as exc:  # unreadable/disappeared file
                    try:
                        st = os.stat(f.path)
                    except OSError:
                        failed += 1
                        work_done += 1
                        with self._lock:
                            self._scan["failed"] = failed
                        continue
                    meta = SampleMetadata(path=f.path, filename=os.path.basename(f.path),
                        size=st.st_size, mtime=st.st_mtime,
                        fingerprint=SampleIndex.fingerprint(st.st_size, st.st_mtime),
                        error=str(exc)[:200])
                    failed += 1
                metas.append(meta); flags.append(meta.analyzed and not meta.error)
                try:
                    hashes.append((f.path, self._sha256(f.path)))
                except OSError:
                    pass
                work_done += 1
                if len(metas) >= 250 or len(hashes) >= 250:
                    flush_batch()
                with self._lock:
                    self._scan["analyzed"] = analyzed
                    self._scan["failed"] = failed
                    self._scan["progress"] = work_done / total_work
            flush_batch()
            with self._lock:
                self._scan.update({"state": "COMPLETE", "progress": 1.0,
                    "current_file": "", "analyzed": analyzed, "failed": failed,
                    "ended": time.time()})
        except Exception as exc:  # surface worker failures to UI
            with self._lock:
                self._scan.update({"state": "ERROR", "error": str(exc),
                                   "current_file": "", "ended": time.time()})
        finally:
            if idx is not None:
                try:
                    idx.close()
                except Exception:
                    pass

    def waveform(self, sample_id: str, bins: int = 1200) -> dict:
        full, meta = self.resolve_id(sample_id)
        bins = max(64, min(int(bins), 4000))
        ext = os.path.splitext(full)[1].lower()
        if ext in (".wav", ".wave"):
            if not self.peaks_fn:
                raise RuntimeError("WAV peak extractor is unavailable")
            result = self.peaks_fn(full, bins)
            return {**result, "sample_id": sample_id, "duration_seconds": meta.duration}
        if ext in (".aif", ".aiff"):
            if meta.duration > 300:
                raise ValueError("AIFF waveform extraction is limited to 300 seconds")
            import numpy as np
            from timbor.samples.scanner import decode_audio
            audio, rate = decode_audio(full, "aiff")
            n = len(audio)
            if n == 0: raise ValueError("empty audio file")
            cuts = np.linspace(0, n, bins + 1, dtype=np.int64)
            minimum = np.array([float(np.min(audio[a:b])) for a, b in zip(cuts[:-1], cuts[1:]) if b > a])
            maximum = np.array([float(np.max(audio[a:b])) for a, b in zip(cuts[:-1], cuts[1:]) if b > a])
            return {"sample_id": sample_id, "bins": len(minimum), "rate": rate,
                    "frames": n, "duration_seconds": meta.duration,
                    "min": minimum.round(5).tolist(), "max": maximum.round(5).tolist(),
                    "rms": [0.0] * len(minimum)}
        raise ValueError(f"waveform decoder unavailable for {meta.format if hasattr(meta, 'format') else ext.lstrip('.')}")

    def media_path(self, sample_id: str) -> tuple[str, str]:
        full, meta = self.resolve_id(sample_id)
        if os.path.splitext(full)[1].lower() not in (".wav", ".wave"):
            raise ValueError("browser preview is supported only for WAV samples")
        return full, "audio/wav"
