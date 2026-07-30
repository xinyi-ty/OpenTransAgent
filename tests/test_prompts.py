from __future__ import annotations

from agent import prompts
from agent.prompts import build_react_prompt
from tools.registry import BUILTIN_TOOL_DEFINITIONS, TOOL_DEFINITIONS


def test_tool_list_is_registry_driven_and_ordered() -> None:
    tool_list = prompts._build_tool_list()
    lines = tool_list.splitlines()
    expected_names = list(TOOL_DEFINITIONS) + list(BUILTIN_TOOL_DEFINITIONS)

    assert [line.split(" — ", 1)[0].removeprefix("## ") for line in lines] == expected_names
    for name, desc in {**TOOL_DEFINITIONS, **BUILTIN_TOOL_DEFINITIONS}.items():
        assert f"## {name} — {desc}" in tool_list


def test_tool_list_cache_refreshes_when_registry_changes() -> None:
    first = prompts._build_tool_list()
    TOOL_DEFINITIONS["temporary_test_tool"] = "Temporary test tool"
    try:
        updated = prompts._build_tool_list()
    finally:
        del TOOL_DEFINITIONS["temporary_test_tool"]
        prompts._build_tool_list()

    assert "temporary_test_tool" not in first
    assert "## temporary_test_tool — Temporary test tool" in updated


def test_known_unregistered_language_pair_uses_effective_route() -> None:
    prompt = build_react_prompt("java", "go", "demo")

    assert "Your task is to translate a java project to go." in prompt
    assert "For each source file, create the equivalent go file" in prompt
    assert "ENVIRONMENT: Windows" in prompt


def test_translation_order_takes_precedence_over_project_tree() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        project_tree="noise.txt\nunused.py",
        translation_order=["src/main.py"],
    )

    assert "  - src/main.cpp" in prompt
    assert "  - unused.cpp" not in prompt
    assert "  - noise.txt" not in prompt


def test_source_files_take_precedence_over_translation_order() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        source_files=["explicit.py"],
        translation_order=["ordered.py"],
    )

    assert "  - explicit.cpp" in prompt
    assert "  - ordered.cpp" not in prompt


def test_project_tree_fallback_filters_tree_formatting() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        project_tree="""demo
├── src
│   ├── main.py
│   └── helper.py
└── README.md

2 directories, 3 files
...""",
    )

    assert "  - main.cpp" in prompt
    assert "  - helper.cpp" in prompt
    assert "  - README.md" in prompt
    assert "  - demo" not in prompt
    assert "directories" not in prompt.split("FILES TO CREATE:", 1)[1]


def test_dependency_layers_prompt_is_static_not_stale() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        layers=[["base.py"], ["app.py"]],
        current_layer=0,
    )

    assert "DEPENDENCY LAYERS (2 layers):" in prompt
    assert "Layer 0: base.py" in prompt
    assert "Layer 1: app.py" in prompt
    assert "You are currently on Layer 0" not in prompt
    assert "→" not in prompt
    assert "The runtime will announce which layer is currently unlocked" in prompt


def test_python_to_cpp_prompt_mentions_cpp_target_guidelines() -> None:
    prompt = build_react_prompt("python", "cpp", "demo")

    assert "## edit_file — Make a targeted exact replacement in an existing file; old_string must be non-empty" in prompt
    assert "old_string MUST be non-empty" in prompt
    assert "Use create_file for a new/empty file" in prompt
    assert "Do not run build/tests until every expected target file" in prompt
    assert "While fixing compile errors, run build only" in prompt
    assert "do not rerun the same full test command unless relevant files changed" in prompt
    assert "execute_command already runs inside the translation workspace" in prompt
    assert "Do not repeatedly run configure/build/ctest" in prompt
    assert "read at most 3 representative test files" in prompt.lower()
    assert "Do NOT use `cd /d` into guessed external directories" in prompt
    assert "Do not call think for layers with 5 or fewer source files" in prompt
    assert "call finish immediately" in prompt
    assert "CPP TARGET GUIDELINES:" in prompt
    assert "Do NOT create_file, edit_file, or rewrite CMakeLists.txt" in prompt
    assert "public_tests/*, tests/*, or test/*" in prompt
    assert "PYTHON TARGET GUIDELINES:" not in prompt
    assert "Avoid fragile patterns like `sys.path.insert(0, '..')`" not in prompt
    assert "API CONTRACT STRATEGY:" in prompt
    assert "expected API contract" in prompt
    assert "do NOT invent different" in prompt


def test_cpp_to_python_prompt_mentions_python_target_guidelines_only() -> None:
    prompt = build_react_prompt("cpp", "python", "demo")

    assert "PYTHON TARGET GUIDELINES:" in prompt
    assert "Avoid fragile patterns like `sys.path.insert(0, '..')`" in prompt
    assert "package-relative imports" in prompt
    assert "standard-library module name conflicts" in prompt
    assert "compatibility shim" in prompt
    assert "CPP TARGET GUIDELINES:" not in prompt
    assert "cmake --build build --config Release" not in prompt
    assert "CMakeLists.txt" not in prompt
    assert "API CONTRACT STRATEGY:" not in prompt


def test_generic_route_prompt_omits_api_contract_guidance() -> None:
    prompt = build_react_prompt("java", "go", "demo")

    assert "Your task is to translate a java project to go." in prompt
    assert "API CONTRACT STRATEGY:" not in prompt


def test_small_project_fast_path_is_added_for_five_or_fewer_files() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        source_files=["a.py", "b.py"],
    )

    assert "SMALL PROJECT FAST PATH:" in prompt
    assert "This layer has 5 or fewer source files" in prompt
    assert "Create each expected target file once" in prompt
    assert "after all expected target files exist" in prompt


def test_large_project_batching_is_added_for_larger_layers() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        source_files=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"],
    )

    assert "SMALL PROJECT FAST PATH:" not in prompt
    assert "LARGE LAYER BATCHING:" in prompt
    assert "Read at most 4 source files" in prompt
    assert "more than 5 read_file/create_file/edit_file calls" in prompt
    assert "Read at most 3 representative test files" in prompt


def test_reflection_guidelines_match_tool_schema() -> None:
    prompt = build_react_prompt("python", "cpp", "demo")

    assert "reflect(source_function, translated_code, error_message, test_results)" in prompt
    assert "reflect(source, code, error_message)" not in prompt


def test_reflection_guidelines_can_be_disabled() -> None:
    prompt = build_react_prompt(
        "python",
        "cpp",
        "demo",
        reflection_enabled=False,
    )

    assert "Reflection-based Error Correction" not in prompt
    assert "reflect(source_function" not in prompt
