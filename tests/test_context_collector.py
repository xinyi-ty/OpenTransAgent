from __future__ import annotations

from pathlib import Path

from tools.context_collector import (
    FindTargetClassAction,
    FindTargetClassExecutor,
    FindTargetImportsAction,
    FindTargetImportsExecutor,
    FindTargetMethodAction,
    FindTargetMethodExecutor,
    GetSourceClassInfoAction,
    GetSourceClassInfoExecutor,
    GetTargetClassInfoAction,
    GetTargetClassInfoExecutor,
    _extract_class_block,
    _methods,
    _resolve,
)


def test_resolve_rejects_workspace_escape(tmp_path: Path) -> None:
    try:
        _resolve("../outside.py", tmp_path)
    except ValueError as e:
        assert "路径超出工作区" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_extract_class_block_supports_struct_and_template() -> None:
    content = """
template <typename T>
struct Box {
  T value;
  void set(T v);
};
class Other {};
"""
    block = _extract_class_block(content, "Box")

    assert block is not None
    assert "struct Box" in block
    assert "class Other" not in block


def test_methods_support_python_and_filter_control_flow() -> None:
    content = """
class Foo:
    def run(self): pass
    async def load(self): pass
if (ready) {
}
int compute(int x) {
}
"""
    methods = _methods(content)

    assert "run" in methods
    assert "load" in methods
    assert "compute" in methods
    assert "if" not in methods


def test_get_source_class_info_extracts_python_fields_methods(tmp_path: Path) -> None:
    src = tmp_path / "foo.py"
    src.write_text(
        "class Foo:\n"
        "    def __init__(self):\n"
        "        self.value = 1\n"
        "    def run(self):\n"
        "        return self.value\n",
        encoding="utf-8",
    )

    obs = GetSourceClassInfoExecutor(str(tmp_path))(
        GetSourceClassInfoAction(filepath="foo.py", class_name="Foo")
    )

    assert obs.is_error is False
    assert "value" in obs.fields
    assert "run" in obs.methods


def test_get_target_class_info_falls_back_to_cpp_implementation(tmp_path: Path) -> None:
    src = tmp_path / "foo.cpp"
    src.write_text("int Foo::run() { return 1; }\n", encoding="utf-8")

    obs = GetTargetClassInfoExecutor(str(tmp_path))(
        GetTargetClassInfoAction(filepath="foo.cpp", class_name="Foo")
    )

    assert obs.is_error is False
    assert "run" in obs.methods
    assert "整个文件" in obs.text


def test_find_target_imports_deduplicates_and_supports_spaced_include(tmp_path: Path) -> None:
    src = tmp_path / "foo.cpp"
    src.write_text(
        "# include <vector>\n#include <vector>\nimport os\nfrom sys import path\n",
        encoding="utf-8",
    )

    obs = FindTargetImportsExecutor(str(tmp_path))(
        FindTargetImportsAction(filepath="foo.cpp")
    )

    assert obs.imports == ["# include <vector>", "#include <vector>", "import os", "from sys import path"]


def test_find_target_class_skips_venv_and_finds_code_extensions(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "bad.py").write_text("class Target: pass\n", encoding="utf-8")
    src = tmp_path / "src.hxx"
    src.write_text("struct Target {};\n", encoding="utf-8")

    obs = FindTargetClassExecutor(str(tmp_path))(
        FindTargetClassAction(class_name="Target")
    )

    assert obs.filepath == "src.hxx"
    assert "line 1" in obs.text


def test_find_target_method_returns_matching_line(tmp_path: Path) -> None:
    src = tmp_path / "foo.cpp"
    src.write_text("int Foo::compute(int x) { return x; }\n", encoding="utf-8")

    obs = FindTargetMethodExecutor(str(tmp_path))(
        FindTargetMethodAction(method_name="compute")
    )

    assert obs.filepath == "foo.cpp"
    assert "line 1" in obs.text
