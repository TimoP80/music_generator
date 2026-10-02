"""Persistence tests for the local-first Studio creation library."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio.creation_store import CreationStore


def test_creation_take_search_favorite_notes_and_reopen():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "creations.db")
        store = CreationStore(path)
        creation = store.create({"prompt": "gabber amen"}, "Gabber Lab", ["rave"], "kick keeper")
        store.add_take(creation["id"], {"id": "take-a", "status": "done", "seed": 10482,
                                      "engine": "procedural", "favorite": False}, 1)
        store.update_take("take-a", {"favorite": True, "notes": "excellent kick"})
        reopened = CreationStore(path)
        result = reopened.get_creation(creation["id"])
        assert result["takes"][0]["favorite"] is True
        assert result["takes"][0]["notes"] == "excellent kick"
        assert reopened.list_creations(q="amen")["total"] == 1
        assert reopened.list_creations(q="175 bpm")["total"] == 0
        assert reopened.list_creations(filter_by="favorites")["total"] == 1
        assert reopened.list_creations(filter_by="completed")["total"] == 1


def test_duplicate_metadata_has_no_audio_copy_and_preset_crud():
    with tempfile.TemporaryDirectory() as directory:
        store = CreationStore(os.path.join(directory, "creations.db"))
        original = store.create({"bpm": 175, "engine": "procedural"}, "Original", ["keeper"], "notes")
        duplicate = store.create(original["params"], "Original copy", original["tags"], original["notes"], status="ready")
        assert duplicate["id"] != original["id"]
        assert store.list_takes(duplicate["id"]) == []
        preset = store.save_preset("1994 RAVE", {"bpm": 175, "genre": "gabber"})
        assert preset in store.list_presets()
        assert store.save_preset("1994 RAVE", {"bpm": 180}, preset["id"])["params"]["bpm"] == 180
        assert store.delete_preset(preset["id"])
        assert not store.list_presets()


def test_interrupted_generation_is_marked_failed():
    with tempfile.TemporaryDirectory() as directory:
        store = CreationStore(os.path.join(directory, "creations.db"))
        creation = store.create({}, "Interrupted")
        store.add_take(creation["id"], {"id": "take-b", "status": "running"}, 1)
        recovered = store.recover_interrupted()
        assert recovered[0]["status"] == "error"
        assert store.get_take("take-b")["status"] == "error"


def main():
    for test in (test_creation_take_search_favorite_notes_and_reopen,
                 test_duplicate_metadata_has_no_audio_copy_and_preset_crud,
                 test_interrupted_generation_is_marked_failed):
        test()
        print(f"{test.__name__}: OK")


if __name__ == "__main__":
    main()
