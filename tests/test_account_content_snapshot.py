"""Copy a role's committed content generation while the source remains editable."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
import threading

import pytest

from azusa_bot import accounts, lore
from azusa_bot.settings import private_write


def test_copy_snapshot_blocks_concurrent_import_until_all_source_files_are_copied(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    private_write(source / "content/persona.md", "# Synthetic persona\nOnly a test role.")
    lore.import_documents(source, [{"title": "Existing source", "text": "Old synthetic background."}])
    # Fix traversal order so the import would otherwise commit a new manifest
    # after enumeration but before that manifest is copied.
    original_walk = Path.rglob
    def ordered_walk(path, pattern):
        rows = list(original_walk(path, pattern))
        return iter(sorted(rows, key=lambda row: (row.name != "persona.md", str(row)))) if path == source / "content" else iter(rows)
    monkeypatch.setattr(Path, "rglob", ordered_walk)
    original_copy = accounts.shutil.copyfileobj
    attempted = threading.Event()
    future = None
    def import_document():
        attempted.set()
        return lore.import_documents(source, [{"title": "Concurrent source", "text": "New synthetic background."}])
    with ThreadPoolExecutor(max_workers=1) as executor:
        def copy_with_concurrent_import(reader, writer, *args, **kwargs):
            nonlocal future
            result = original_copy(reader, writer, *args, **kwargs)
            if Path(reader.name).name == "persona.md":
                future = executor.submit(import_document)
                assert attempted.wait(2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=.2)
            return result
        monkeypatch.setattr(accounts.shutil, "copyfileobj", copy_with_concurrent_import)
        accounts._copy_content(source / "content", target / "content")
        assert future.result(timeout=2)["imported"] == 1
    # The source accepts the edit after copy, while the destination contains a
    # complete earlier generation rather than a manifest with a missing blob.
    assert lore.list_documents(source)["total"] == 2
    copied = lore.list_documents(target)
    assert copied["total"] == 1
    assert lore.get_document(target, copied["documents"][0]["id"])["text"] == "Old synthetic background."
