"""timbor.samples.indexer — scan -> diff -> analyze -> classify -> persist."""
from __future__ import annotations

import time

from .scanner import scan_directory, EXT_FORMATS
from .analyzer import analyze_file
from .classifier import classify
from .cache import SampleIndex
from .metadata import SampleMetadata


def index_library(root: str, db_path: str = SampleIndex.DEFAULT_DB,
                  verbose: bool = False) -> dict:
    """Index (and analyze) a sample library. Returns stats for CLI reporting."""
    idx = SampleIndex(db_path)
    try:
        print("Scanning sample library...")
        files = scan_directory(root)
        known = idx.known_fingerprints()
        new: list = []
        changed: list = []
        unchanged: list = []
        for f in files:
            fp = SampleIndex.fingerprint(f.size, f.mtime)
            if f.path not in known:
                new.append(f)
            elif known[f.path] != fp:
                changed.append(f)
            else:
                unchanged.append(f)
        gone = [p for p in known if p not in {f.path for f in files}]

        errors = 0
        analyzed = 0
        metas: list[SampleMetadata] = []
        flags: list[bool] = []
        t0 = time.time()
        for i, f in enumerate(new + changed):
            try:
                m = analyze_file(f.path, f.format)
            except Exception as e:  # unreadable/corrupt: index the failure
                import os
                st = os.stat(f.path)
                m = SampleMetadata(path=f.path, filename=os.path.basename(f.path),
                                   size=st.st_size, mtime=st.mtime,
                                   fingerprint=SampleIndex.fingerprint(st.st_size, st.mtime),
                                   error=str(e)[:200])
                errors += 1
                metas.append(m)
                flags.append(False)
                continue
            classify(m)
            metas.append(m)
            flags.append(m.analyzed and not m.error)
            if m.analyzed and not m.error:
                analyzed += 1
            if verbose and (i % 200 == 0):
                print(f"  analyzed {i + 1}/{len(new + changed)} "
                      f"({time.time() - t0:.0f}s)")
        if gone:
            idx.delete_paths(gone)

        # save in chunks to keep memory flat
        CH = 500
        for i in range(0, len(metas), CH):
            idx.upsert_many(metas[i:i + CH], flags[i:i + CH])

        stats = idx.stats()
        stats.update({"discovered": len(files), "new": len(new),
                      "changed": len(changed), "unchanged": len(unchanged),
                      "analyzed_now": analyzed, "errors_now": errors})
        return stats
    finally:
        idx.close()


def format_stats(stats: dict) -> str:
    lines = [
        f"Files discovered: {stats.get('discovered', 0):,}",
        f"New: {stats.get('new', 0):,}",
        f"Changed: {stats.get('changed', 0):,}",
        f"Unchanged: {stats.get('unchanged', 0):,}",
        f"Analyzed: {stats.get('analyzed_now', 0):,} "
        f"(total in db: {stats.get('analyzed', 0):,})",
        f"Errors: {stats.get('errors_now', 0):,}",
        "Index complete.",
    ]
    return "\n".join(lines)
