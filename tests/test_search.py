from __future__ import annotations

from pathlib import Path

from tools.search import SearchContentAction, SearchContentExecutor


def test_search_rejects_empty_keyword(tmp_path: Path) -> None:
    obs = SearchContentExecutor(str(tmp_path))(
        SearchContentAction(keyword="   ")
    )

    assert obs.is_error is True
    assert "不能为空" in obs.text


def test_search_rejects_workspace_escape(tmp_path: Path) -> None:
    obs = SearchContentExecutor(str(tmp_path))(
        SearchContentAction(keyword="x", path="..")
    )

    assert obs.is_error is True
    assert "路径超出工作区" in obs.text


def test_search_allows_absolute_path_inside_workspace(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("class Target:\n    pass\n", encoding="utf-8")

    obs = SearchContentExecutor(str(tmp_path))(
        SearchContentAction(keyword="target", path=str(src))
    )

    assert obs.matches == ["src/main.py"]
    assert "src/main.py:1" in obs.text


def test_search_skips_venv_and_build_dirs(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "bad.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "bad.cpp").write_text("needle\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("needle\n", encoding="utf-8")

    obs = SearchContentExecutor(str(tmp_path))(
        SearchContentAction(keyword="needle")
    )

    assert obs.matches == ["src.py"]


def test_search_supports_extended_code_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.hxx").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.tsx").write_text("needle\n", encoding="utf-8")

    obs = SearchContentExecutor(str(tmp_path), max_results=5)(
        SearchContentAction(keyword="needle")
    )

    assert set(obs.matches) == {"a.hxx", "b.tsx"}


def test_search_skips_large_files(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("needle" + "x" * 100, encoding="utf-8")

    obs = SearchContentExecutor(str(tmp_path), max_file_bytes=10)(
        SearchContentAction(keyword="needle")
    )

    assert obs.matches == []


def test_search_limits_results_and_returns_matching_line(tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"f{i}.py").write_text(f"line {i}\nneedle {i}\n", encoding="utf-8")

    obs = SearchContentExecutor(str(tmp_path), max_results=2)(
        SearchContentAction(keyword="needle")
    )

    assert len(obs.matches) == 2
    assert ":2: needle" in obs.text


def test_search_limits_scanned_files(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("nope\n", encoding="utf-8")

    obs = SearchContentExecutor(str(tmp_path), max_scan_files=2)(
        SearchContentAction(keyword="needle")
    )

    assert obs.matches == []
    assert "截断" in obs.text
