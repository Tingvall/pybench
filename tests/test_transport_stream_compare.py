from __future__ import annotations

import os
from pathlib import Path

import pytest

from pybench.operation_packs import OperationPackError
from pybench.protocol_v3 import _changed_snapshot, _restore, _snapshot, _tree

BLOCK = 1024 * 1024


def _old_changed(root: Path, before: dict[str, bytes]) -> list[str]:
    after = _snapshot(root)
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


@pytest.mark.parametrize("size", [0, 1, BLOCK - 1, BLOCK, BLOCK + 1, 2 * BLOCK, 2 * BLOCK + 1])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_stream_comparison_matches_byte_map_at_block_boundaries(tmp_path, size, position):
    target = tmp_path / "payload.bin"
    original = b"a" * size
    target.write_bytes(original)
    before = _snapshot(tmp_path)
    if size:
        index = {"first": 0, "middle": size // 2, "last": size - 1}[position]
        replacement = bytearray(original)
        replacement[index] = ord("b")
        target.write_bytes(replacement)
    assert _changed_snapshot(tmp_path, before) == _old_changed(tmp_path, before)


def test_stream_comparison_matches_byte_map_for_stage_changes(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "empty").mkdir()
    for name, value in {
        "same": b"same",
        "replace_same": b"same",
        "changed": b"old",
        "shortened": b"long",
        "lengthened": b"x",
        "deleted": b"gone",
        "nested/payload": b"nested",
        "empty_file": b"",
    }.items():
        (tmp_path / name).write_bytes(value)
    before = _snapshot(tmp_path)
    tree_before = _tree(tmp_path)
    (tmp_path / "replace_same").unlink()
    (tmp_path / "replace_same").write_bytes(b"same")
    (tmp_path / "changed").write_bytes(b"new")
    (tmp_path / "shortened").write_bytes(b"lon")
    (tmp_path / "lengthened").write_bytes(b"xx")
    (tmp_path / "deleted").unlink()
    (tmp_path / "nested/payload").write_bytes(b"changed nested")
    (tmp_path / "created").write_bytes(b"new file")
    (tmp_path / "empty_added").write_bytes(b"")
    assert _changed_snapshot(tmp_path, before) == _old_changed(tmp_path, before)
    assert _changed_snapshot(tmp_path, before) == [
        "changed",
        "created",
        "deleted",
        "empty_added",
        "lengthened",
        "nested/payload",
        "shortened",
    ]
    assert _tree(tmp_path) - tree_before == {"created", "empty_added"}
    _restore(tmp_path, before)
    assert _snapshot(tmp_path) == before


def test_stream_comparison_reads_every_later_file_after_first_mismatch(tmp_path, monkeypatch):
    for index in range(8):
        (tmp_path / f"{index:02d}.bin").write_bytes(b"a" * (BLOCK + 1))
    before = _snapshot(tmp_path)
    (tmp_path / "00.bin").write_bytes(b"b" * (BLOCK + 1))
    reads: dict[str, list[int]] = {}
    original_open = Path.open

    class RecordingHandle:
        def __init__(self, handle, name):
            self.handle = handle
            self.name = name

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size):
            block = self.handle.read(size)
            reads[self.name].append(len(block))
            return block

    def record_open(path, *args, **kwargs):
        if path.parent == tmp_path:
            reads[path.name] = []
            return RecordingHandle(original_open(path, *args, **kwargs), path.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_open)
    assert _changed_snapshot(tmp_path, before) == ["00.bin"]
    assert list(reads) == [f"{index:02d}.bin" for index in range(8)]
    assert all(parts == [BLOCK, 1, 0] for parts in reads.values())


def test_later_read_error_precedes_earlier_mutation(tmp_path, monkeypatch):
    first = tmp_path / "a.bin"
    last = tmp_path / "z.bin"
    first.write_bytes(b"old")
    last.write_bytes(b"last")
    before = _snapshot(tmp_path)
    first.write_bytes(b"new")
    original_open = Path.open

    def fail_late(path, *args, **kwargs):
        if path == last:
            raise OSError("late read failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_late)
    with pytest.raises(OSError, match="late read failed"):
        _changed_snapshot(tmp_path, before)


def test_later_symlink_precedes_earlier_mutation(tmp_path):
    first = tmp_path / "a.bin"
    first.write_bytes(b"old")
    before = _snapshot(tmp_path)
    first.write_bytes(b"new")
    (tmp_path / "z-link").symlink_to(first)
    with pytest.raises(OperationPackError) as caught:
        _changed_snapshot(tmp_path, before)
    assert caught.value.code == "PYBENCH_OUT_OF_SCOPE_MUTATION"


def test_fifo_and_empty_directory_keep_old_snapshot_and_tree_behavior(tmp_path):
    (tmp_path / "empty").mkdir()
    os.mkfifo(tmp_path / "fifo")
    before = _snapshot(tmp_path)
    tree_before = _tree(tmp_path)
    (tmp_path / "added_empty").mkdir()
    os.mkfifo(tmp_path / "added_fifo")
    assert _changed_snapshot(tmp_path, before) == _old_changed(tmp_path, before) == []
    assert _tree(tmp_path) - tree_before == {"added_empty", "added_fifo"}
