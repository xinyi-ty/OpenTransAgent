from __future__ import annotations

from pathlib import Path

from tools.file_ops import (
    CreateFileAction,
    CreateFileExecutor,
    ListFilesAction,
    ListFilesExecutor,
    ReadFileAction,
    ReadFileExecutor,
    _LIST_MAX_ENTRIES,
    set_layer_ctrl,
)


class DummyLayerCtrl:
    active = True

    def __init__(self, unlocked: bool):
        self.unlocked = unlocked
        self.seen: list[str] = []

    def is_unlocked(self, filepath: str) -> bool:
        self.seen.append(filepath)
        return self.unlocked


def test_read_file_rejects_workspace_escape(tmp_path: Path) -> None:
    obs = ReadFileExecutor(str(tmp_path))(
        ReadFileAction(filepath="../outside.txt")
    )

    assert obs.is_error is True
    assert "路径超出工作区" in obs.text


def test_create_file_rejects_workspace_escape(tmp_path: Path) -> None:
    obs = CreateFileExecutor(str(tmp_path))(
        CreateFileAction(filepath="../outside.txt", content="bad")
    )

    assert obs.is_error is True
    assert not (tmp_path.parent / "outside.txt").exists()


def test_list_files_rejects_workspace_escape(tmp_path: Path) -> None:
    obs = ListFilesExecutor(str(tmp_path))(
        ListFilesAction(path="..")
    )

    assert obs.is_error is True
    assert "路径超出工作区" in obs.text


def test_absolute_path_inside_workspace_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "src" / "a.txt"
    obs = CreateFileExecutor(str(tmp_path))(
        CreateFileAction(filepath=str(path), content="hello")
    )

    assert obs.is_error is False
    assert obs.path == "src/a.txt"
    assert path.read_text(encoding="utf-8") == "hello"


def test_read_file_handles_invalid_utf8_and_marks_truncation(tmp_path: Path) -> None:
    path = tmp_path / "bad.bin"
    path.write_bytes(b"abc\xff" + b"x" * 5100)

    obs = ReadFileExecutor(str(tmp_path))(
        ReadFileAction(filepath="bad.bin")
    )

    assert obs.is_error is False
    assert obs.filepath == "bad.bin"
    assert "truncated" in obs.text
    assert "�" in obs.result


def test_create_file_returns_relative_path_and_overwrites_atomically(tmp_path: Path) -> None:
    executor = CreateFileExecutor(str(tmp_path))
    first = executor(CreateFileAction(filepath="src/out.py", content="one"))
    second = executor(CreateFileAction(filepath="src/out.py", content="two"))

    assert first.path == "src/out.py"
    assert second.path == "src/out.py"
    assert (tmp_path / "src" / "out.py").read_text(encoding="utf-8") == "two"


def test_create_file_layer_lock_uses_relative_path(tmp_path: Path) -> None:
    ctrl = DummyLayerCtrl(unlocked=False)
    set_layer_ctrl(ctrl)
    try:
        obs = CreateFileExecutor(str(tmp_path))(
            CreateFileAction(filepath="future.py", content="x")
        )
    finally:
        set_layer_ctrl(None)

    assert obs.is_error is True
    assert obs.path == "future.py"
    assert ctrl.seen == ["future.py"]
    assert not (tmp_path / "future.py").exists()


def test_list_files_truncates_large_directory(tmp_path: Path) -> None:
    for i in range(_LIST_MAX_ENTRIES + 5):
        (tmp_path / f"f{i:03}.txt").write_text("x", encoding="utf-8")

    obs = ListFilesExecutor(str(tmp_path))(ListFilesAction(path="."))

    assert obs.is_error is False
    assert obs.count == _LIST_MAX_ENTRIES
    assert "truncated" in obs.text
