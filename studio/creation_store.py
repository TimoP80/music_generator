"""Durable metadata storage for Studio-created music; audio remains on disk."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
import time
import uuid


class CreationStore:
    """SQLite metadata store; never copies or embeds generated audio."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS creations (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, params TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS takes (
                    id TEXT PRIMARY KEY, creation_id TEXT NOT NULL REFERENCES creations(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS takes_creation_idx ON takes(creation_id, number);
                CREATE TABLE IF NOT EXISTS presets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    params TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _dump(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value, default=None):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    def create(self, params: dict, name: str, tags=None, notes="", status="queued",
               creation_id: str | None = None) -> dict:
        now = time.time()
        ident = creation_id or uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO creations(id,name,params,tags,notes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (ident, name[:120], self._dump(params), self._dump(tags or []),
                        str(notes)[:5000], status, now, now))
        return self.get_creation(ident, include_takes=False)

    def add_take(self, creation_id: str, data: dict, number: int) -> dict:
        now = time.time()
        ident = str(data.get("id") or uuid.uuid4().hex)
        payload = {key: value for key, value in data.items() if key != "output"}
        payload.update({"id": ident, "creation_id": creation_id,
                       "take_number": int(number), "generated_at": data.get("generated_at")})
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO takes(id,creation_id,number,data,created_at) VALUES(?,?,?,?,?)",
                       (ident, creation_id, int(number), self._dump(payload), now))
            db.execute("UPDATE creations SET updated_at=?, status='running' WHERE id=?",
                       (now, creation_id))
        return payload

    def update_take(self, take_id: str, changes: dict) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT creation_id,data FROM takes WHERE id=?", (take_id,)).fetchone()
            if row is None:
                raise KeyError(take_id)
            payload = self._load(row["data"], {})
            payload.update({key: value for key, value in changes.items() if key != "output"})
            payload.pop("output", None)
            db.execute("UPDATE takes SET data=? WHERE id=?", (self._dump(payload), take_id))
            db.execute("UPDATE creations SET updated_at=? WHERE id=?", (time.time(), row["creation_id"]))
        return payload

    def get_take(self, take_id: str) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT data FROM takes WHERE id=?", (take_id,)).fetchone()
        if row is None:
            raise KeyError(take_id)
        return self._load(row["data"], {})

    def list_takes(self, creation_id: str) -> list[dict]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT data FROM takes WHERE creation_id=? ORDER BY number", (creation_id,)).fetchall()
        return [self._load(row["data"], {}) for row in rows]

    def update_creation(self, creation_id: str, changes: dict) -> dict:
        allowed = {"name", "tags", "notes", "archived", "status", "params"}
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM creations WHERE id=?", (creation_id,)).fetchone()
            if row is None:
                raise KeyError(creation_id)
            values = {"name": row["name"], "tags": self._load(row["tags"], []),
                      "notes": row["notes"], "archived": bool(row["archived"]),
                      "status": row["status"], "params": self._load(row["params"], {})}
            for key, value in changes.items():
                if key in allowed:
                    values[key] = value
            db.execute("UPDATE creations SET name=?,tags=?,notes=?,archived=?,status=?,params=?,updated_at=? WHERE id=?",
                       (str(values["name"])[:120], self._dump(values["tags"]),
                        str(values["notes"])[:5000], int(bool(values["archived"])),
                        str(values["status"]), self._dump(values["params"]), time.time(), creation_id))
        return self.get_creation(creation_id)

    def get_creation(self, creation_id: str, include_takes=True) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM creations WHERE id=?", (creation_id,)).fetchone()
        if row is None:
            raise KeyError(creation_id)
        result = {"id": row["id"], "name": row["name"], "params": self._load(row["params"], {}),
                  "tags": self._load(row["tags"], []), "notes": row["notes"],
                  "archived": bool(row["archived"]), "status": row["status"],
                  "created_at": row["created_at"], "updated_at": row["updated_at"]}
        if include_takes:
            result["takes"] = self.list_takes(creation_id)
        return result

    def list_creations(self, q="", filter_by="all", page=1, page_size=30) -> dict:
        """Search all creation metadata, then return a bounded result page."""
        page, page_size = int(page), int(page_size)
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("page must be >=1 and page_size must be 1..100")
        term = str(q).strip().casefold()
        if len(term) > 200:
            raise ValueError("search term too long")
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM creations ORDER BY updated_at DESC").fetchall()
            selected = []
            for row in rows:
                creation = {"id": row["id"], "name": row["name"],
                    "params": self._load(row["params"], {}), "tags": self._load(row["tags"], []),
                    "notes": row["notes"], "archived": bool(row["archived"]),
                    "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
                takes = db.execute("SELECT data FROM takes WHERE creation_id=? ORDER BY number", (row["id"],)).fetchall()
                creation["takes"] = [self._load(t["data"], {}) for t in takes]
                take_values = creation["takes"]
                text = " ".join([creation["name"], creation["notes"], *creation["tags"],
                    json.dumps(creation["params"], ensure_ascii=False),
                    *(json.dumps(t, ensure_ascii=False) for t in take_values)]).casefold()
                if term and term not in text:
                    continue
                if filter_by == "favorites" and not any(t.get("favorite") for t in take_values):
                    continue
                if filter_by == "recent" and time.time() - creation["updated_at"] > 7 * 86400:
                    continue
                if filter_by == "completed" and not any(t.get("status") == "done" for t in take_values):
                    continue
                if filter_by == "failed" and not any(t.get("status") == "error" for t in take_values):
                    continue
                if filter_by not in {"all", "favorites", "recent", "completed", "failed", "archived"}:
                    raise ValueError("invalid creation filter")
                if filter_by == "all" and creation["archived"]:
                    continue
                if filter_by != "archived" and creation["archived"]:
                    continue
                if filter_by == "archived" and not creation["archived"]:
                    continue
                selected.append(creation)
        start = (page - 1) * page_size
        return {"page": page, "page_size": page_size, "total": len(selected),
                "pages": (len(selected) + page_size - 1) // page_size,
                "creations": selected[start:start + page_size]}

    def delete_creation(self, creation_id: str) -> dict:
        creation = self.get_creation(creation_id)
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM creations WHERE id=?", (creation_id,))
        return creation

    def delete_take(self, take_id: str) -> dict:
        take = self.get_take(take_id)
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM takes WHERE id=?", (take_id,))
            remaining = db.execute("SELECT data FROM takes WHERE creation_id=?",
                                   (take["creation_id"],)).fetchall()
            statuses = [self._load(row["data"], {}).get("status") for row in remaining]
            status = ("running" if any(s in {"queued", "running"} for s in statuses)
                      else "error" if "error" in statuses else "done" if statuses else "ready")
            db.execute("UPDATE creations SET updated_at=?,status=? WHERE id=?",
                       (time.time(), status, take["creation_id"]))
        return take

    def list_presets(self) -> list[dict]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM presets ORDER BY name COLLATE NOCASE").fetchall()
        return [{"id": r["id"], "name": r["name"], "params": self._load(r["params"], {}),
                 "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]

    def save_preset(self, name: str, params: dict, preset_id: str | None = None) -> dict:
        name = str(name).strip()
        if not name or len(name) > 80:
            raise ValueError("preset name must be 1..80 characters")
        if not isinstance(params, dict):
            raise ValueError("preset parameters must be an object")
        ident, now = preset_id or uuid.uuid4().hex, time.time()
        with self._lock, self._connect() as db:
            try:
                db.execute("INSERT INTO presets(id,name,params,created_at,updated_at) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET name=excluded.name,params=excluded.params,updated_at=excluded.updated_at",
                           (ident, name, self._dump(params), now, now))
            except sqlite3.IntegrityError as exc:
                raise ValueError("a preset with that name already exists") from exc
        return next(p for p in self.list_presets() if p["id"] == ident)

    def delete_preset(self, preset_id: str) -> bool:
        with self._lock, self._connect() as db:
            cur = db.execute("DELETE FROM presets WHERE id=?", (preset_id,))
            return cur.rowcount > 0

    def recover_interrupted(self) -> list[dict]:
        """Mark work interrupted by a process restart failed, never successful."""
        recovered = []
        with self._lock, self._connect() as db:
            creation_ids = [row[0] for row in db.execute(
                "SELECT DISTINCT creation_id FROM takes").fetchall()]
        creations = []
        for creation_id in creation_ids:
            try:
                creations.append(self.get_creation(creation_id))
            except KeyError:
                continue
        for creation in creations:
            for take in creation["takes"]:
                if take.get("status") in {"queued", "running"}:
                    updated = self.update_take(take["id"], {
                        "status": "error", "error": "Studio restarted before generation completed.",
                        "ended": time.time(), "audio_url": None})
                    recovered.append(updated)
        for creation in creations:
            statuses = [t.get("status") for t in self.list_takes(creation["id"])]
            if statuses and all(s not in {"queued", "running"} for s in statuses):
                self.update_creation(creation["id"], {"status": "error" if "error" in statuses else "done"})
        return recovered
