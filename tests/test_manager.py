from __future__ import annotations

from pathlib import Path

from workspace.manager import (
    _resolve_within,
    compute_layers,
    get_project_tree,
    LayerController,
)


def test_resolve_within_rejects_workspace_escape(tmp_path: Path) -> None:
    try:
        _resolve_within(tmp_path, "../outside.txt")
    except ValueError as e:
        assert "路径超出工作区" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_within_allows_normal_path(tmp_path: Path) -> None:
    resolved = _resolve_within(tmp_path, "src/foo.py")

    assert resolved == (tmp_path / "src" / "foo.py").resolve()


def test_compute_layers_handles_missing_keys() -> None:
    layers = compute_layers(
        ["a.py", "b.py", "c.py"],
        [{"file": "b.py", "depends_on": "a.py"}, {}, {"bad": "x"}],
    )

    assert len(layers) >= 2
    assert any("a.py" in layer for layer in layers)
    assert any("b.py" in layer for layer in layers)


def test_layer_controller_stem_dedup_ignores_conflicts() -> None:
    lc = LayerController([
        ["src/a/util.py"],
        ["src/b/util.py"],
    ])

    assert lc.is_unlocked("util.h") is True  # 重复 stem 不应误阻挡


def test_layer_controller_stem_matches_unique() -> None:
    lc = LayerController([
        ["unique.py"],
        ["other.py"],
    ])

    assert lc.is_unlocked("unique.h") is True   # unique 在 layer 0
    assert lc.is_unlocked("unique.cpp") is True
    assert lc.is_unlocked("other.h") is False   # other 在 layer 1


def test_layer_controller_advance_unlocks_next_layer() -> None:
    lc = LayerController([["a.py"], ["b.py"]])

    assert lc.is_unlocked("b.h") is False
    lc.advance()
    assert lc.is_unlocked("b.h") is True


def test_get_project_tree_fallback_skips_venv_and_limited_depth(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "bad.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "a").mkdir()
    (tmp_path / "deep" / "a" / "b").mkdir()
    (tmp_path / "deep" / "a" / "b" / "c.py").write_text("", encoding="utf-8")

    tree = get_project_tree(str(tmp_path), max_depth=2)

    assert "src/main.py" in tree
    assert ".venv" not in tree
    assert "bad.py" not in tree
