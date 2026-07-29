from __future__ import annotations

from config.languages import (
    LANGUAGE_CONFIG,
    get_source_extensions,
    get_target_extensions,
    normalize_language,
)


def test_get_target_extensions_returns_copy() -> None:
    exts = get_target_extensions("python")
    exts.append(".tmp")

    assert LANGUAGE_CONFIG["python"]["target_exts"] == [".py"]
    assert get_target_extensions("python") == [".py"]


def test_get_source_extensions_returns_copy() -> None:
    exts = get_source_extensions("cpp")
    exts.clear()

    assert ".cpp" in LANGUAGE_CONFIG["cpp"]["source_exts"]
    assert ".cpp" in get_source_extensions("cpp")


def test_language_aliases_are_supported_for_extensions() -> None:
    assert normalize_language("C++") == "cpp"
    assert get_target_extensions("c++") == [".cpp", ".h", ".hpp"]
    assert get_source_extensions("py") == [".py"]
    assert get_target_extensions("C#") == [".cs"]


def test_unknown_language_fallback_is_normalized() -> None:
    assert get_target_extensions("kotlin") == [".kotlin"]
    assert get_source_extensions(".zig") == [".zig"]
    assert get_target_extensions("  ") == [".txt"]
